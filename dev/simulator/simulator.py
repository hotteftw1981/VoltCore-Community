#!/usr/bin/env python3
"""Vendor-neutral OCPP 1.6J development simulator for the backend.

This is intentionally not part of the product UI. It connects through the same
OCPP WebSocket endpoint as a real charge point so tests exercise the real OCPP
path end to end.
"""
import argparse
import asyncio
from datetime import datetime, timezone

import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import Action, RemoteStartStopStatus


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class SimulatorChargePoint(OcppChargePoint):
    def __init__(self, charge_point_id, connection):
        super().__init__(charge_point_id, connection)
        self.remote_stop = asyncio.Event()

    @on(Action.remote_stop_transaction)
    async def on_remote_stop_transaction(self, transaction_id, **kwargs):
        print(f"<- RemoteStopTransaction transaction_id={transaction_id}")
        self.remote_stop.set()
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.remote_start_transaction)
    async def on_remote_start_transaction(self, id_tag, connector_id=None, **kwargs):
        print(f"<- RemoteStartTransaction id_tag={id_tag} connector={connector_id}")
        # The simulator does not auto-start a new scenario from a remote request.
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)

    async def boot(self, vendor, model, firmware):
        response = await self.call(call.BootNotification(
            charge_point_vendor=vendor,
            charge_point_model=model,
            firmware_version=firmware,
        ))
        print(f"-> BootNotification: {getattr(response, 'status', response)}")

    async def status(self, connector_id, status, error_code="NoError"):
        await self.call(call.StatusNotification(
            connector_id=connector_id,
            error_code=error_code,
            status=status,
            timestamp=now_iso(),
        ))
        print(f"-> StatusNotification: connector={connector_id} status={status} error={error_code}")

    async def authorize(self, id_tag):
        response = await self.call(call.Authorize(id_tag=id_tag))
        info = getattr(response, "id_tag_info", {}) or {}
        status = info.get("status") if isinstance(info, dict) else getattr(info, "status", None)
        print(f"-> Authorize: {status}")
        return str(status or "").lower() == "accepted"

    async def start_transaction(self, connector_id, id_tag, meter_wh):
        response = await self.call(call.StartTransaction(
            connector_id=connector_id,
            id_tag=id_tag,
            meter_start=int(round(meter_wh)),
            timestamp=now_iso(),
        ))
        tx_id = getattr(response, "transaction_id", None)
        info = getattr(response, "id_tag_info", {}) or {}
        status = info.get("status") if isinstance(info, dict) else getattr(info, "status", None)
        print(f"-> StartTransaction: transaction_id={tx_id} status={status}")
        return tx_id

    async def meter_values(self, connector_id, transaction_id, meter_wh, power_kw, offered_kw,
                           current_a, offered_a, voltage_v, frequency_hz, temperature_c, soc, aligned_data=False):
        aligned_context = "Sample.Clock" if aligned_data else "Sample.Periodic"
        sampled = [
            {"value": f"{meter_wh:.0f}", "measurand": "Energy.Active.Import.Register", "unit": "Wh", "context": "Sample.Periodic"},
            {"value": f"{power_kw * 1000:.0f}", "measurand": "Power.Active.Import", "unit": "W", "context": "Sample.Periodic"},
            {"value": f"{offered_kw * 1000:.0f}", "measurand": "Power.Offered", "unit": "W", "context": aligned_context},
            {"value": f"{frequency_hz:.2f}", "measurand": "Frequency", "unit": "Hertz", "context": aligned_context},
            {"value": f"{temperature_c:.1f}", "measurand": "Temperature", "unit": "Celsius", "location": "Body", "context": "Sample.Periodic"},
        ]
        for phase in ("L1", "L2", "L3"):
            sampled.extend([
                {"value": f"{current_a:.2f}", "measurand": "Current.Import", "unit": "A", "phase": phase, "context": "Sample.Periodic"},
                {"value": f"{offered_a:.2f}", "measurand": "Current.Offered", "unit": "A", "phase": phase, "context": aligned_context},
                {"value": f"{voltage_v:.1f}", "measurand": "Voltage", "unit": "V", "phase": f"{phase}-N", "context": aligned_context},
            ])
        if soc is not None:
            sampled.append({"value": f"{soc:.1f}", "measurand": "SoC", "unit": "Percent", "context": "Sample.Periodic"})
        await self.call(call.MeterValues(
            connector_id=connector_id,
            transaction_id=transaction_id,
            meter_value=[{"timestamp": now_iso(), "sampled_value": sampled}],
        ))
        print(f"-> MeterValues: {power_kw:.2f} kW actual / {offered_kw:.2f} kW offered / {meter_wh/1000:.3f} kWh meter")

    async def stop_transaction(self, transaction_id, meter_wh, reason="Local"):
        await self.call(call.StopTransaction(
            meter_stop=int(round(meter_wh)),
            timestamp=now_iso(),
            transaction_id=transaction_id,
            reason=reason,
        ))
        print(f"-> StopTransaction: transaction_id={transaction_id} reason={reason}")


async def heartbeat_loop(cp, interval):
    while True:
        await asyncio.sleep(interval)
        try:
            await cp.call(call.Heartbeat())
            print("-> Heartbeat")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Heartbeat failed: {exc}")
            return


async def run_scenario(cp, args):
    await cp.boot(args.vendor, args.model, args.firmware)
    await cp.status(0, "Available")
    await cp.status(args.connector, "Available")

    if args.scenario == "fault":
        await cp.status(args.connector, "Faulted", "OtherError")
        await asyncio.sleep(max(2, args.interval))
        await cp.status(args.connector, "Available")
        return

    if args.scenario == "telemetry":
        await cp.status(args.connector, "Charging")
        meter_wh = args.meter_start_wh
        for step in range(max(1, args.samples)):
            meter_wh += args.power_kw * (args.interval / 3600.0) * 1000.0
            soc = None if args.soc_start is None else min(100.0, args.soc_start + step * args.soc_step)
            await cp.meter_values(args.connector, None, meter_wh, args.power_kw, args.offered_kw,
                                  args.current_a, args.offered_a, args.voltage_v, args.frequency_hz,
                                  args.temperature_c, soc, args.aligned_data)
            await asyncio.sleep(args.interval)
        await cp.status(args.connector, "Available")
        return

    if not await cp.authorize(args.id_tag):
        raise RuntimeError(
            f"RFID/idTag '{args.id_tag}' was rejected. Create/enable this RFID in the backend or pass --id-tag with a valid test RFID."
        )

    await cp.status(args.connector, "Preparing")
    tx_id = await cp.start_transaction(args.connector, args.id_tag, args.meter_start_wh)
    if not tx_id:
        raise RuntimeError("StartTransaction was not accepted by the backend.")
    await cp.status(args.connector, "Charging")

    meter_wh = args.meter_start_wh
    for step in range(max(1, args.samples)):
        if cp.remote_stop.is_set():
            await cp.stop_transaction(tx_id, meter_wh, "Remote")
            await cp.status(args.connector, "Finishing")
            await cp.status(args.connector, "Available")
            return
        meter_wh += args.power_kw * (args.interval / 3600.0) * 1000.0
        soc = None if args.soc_start is None else min(100.0, args.soc_start + step * args.soc_step)
        await cp.meter_values(args.connector, tx_id, meter_wh, args.power_kw, args.offered_kw,
                              args.current_a, args.offered_a, args.voltage_v, args.frequency_hz,
                              args.temperature_c, soc, args.aligned_data)
        await asyncio.sleep(args.interval)

    if args.scenario == "standtime":
        # Charging has ended, but the vehicle remains connected. This is exactly
        # the state used to develop Ladezeit vs. Standzeit vs. Anschlussdauer.
        await cp.status(args.connector, "SuspendedEV")
        stand_samples = max(1, int(args.stand_seconds / max(1, args.interval)))
        for _ in range(stand_samples):
            if cp.remote_stop.is_set():
                break
            await cp.meter_values(args.connector, tx_id, meter_wh, 0.0, args.offered_kw,
                                  0.0, args.offered_a, args.voltage_v, args.frequency_hz,
                                  args.temperature_c, 100.0 if args.soc_start is not None else None, args.aligned_data)
            await asyncio.sleep(args.interval)

    reason = "Remote" if cp.remote_stop.is_set() else "Local"
    await cp.stop_transaction(tx_id, meter_wh, reason)
    await cp.status(args.connector, "Finishing")
    await cp.status(args.connector, "Available")


async def main_async(args):
    uri = args.backend.rstrip("/") + "/" + args.charge_point_id
    print(f"Connecting {args.charge_point_id} -> {uri}")
    async with websockets.connect(uri, subprotocols=["ocpp1.6"], ping_interval=20, ping_timeout=20) as ws:
        cp = SimulatorChargePoint(args.charge_point_id, ws)
        receiver = asyncio.create_task(cp.start())
        heartbeat = asyncio.create_task(heartbeat_loop(cp, args.heartbeat))
        try:
            await run_scenario(cp, args)
            if args.stay_connected:
                print(f"-> Scenario complete. Staying connected; Heartbeat every {args.heartbeat:g}s. Press Ctrl+C to stop.")
                await asyncio.Event().wait()
        finally:
            heartbeat.cancel()
            receiver.cancel()
            await asyncio.gather(heartbeat, receiver, return_exceptions=True)


def parse_args():
    p = argparse.ArgumentParser(description="Vendor-neutral OCPP 1.6J backend test simulator")
    p.add_argument("--backend", default="ws://127.0.0.1:9000", help="OCPP WebSocket base URL")
    p.add_argument("--charge-point-id", default="DEV_SIM_001")
    p.add_argument("--connector", type=int, default=1)
    p.add_argument("--id-tag", default="DEV-SIM-RFID")
    p.add_argument("--scenario", choices=("full", "standtime", "telemetry", "fault"), default="standtime")
    p.add_argument("--vendor", default="OCPP Test Lab")
    p.add_argument("--model", default="Vendor Neutral Sim")
    p.add_argument("--firmware", default="dev-1")
    p.add_argument("--samples", type=int, default=6)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--heartbeat", type=float, default=30.0)
    p.add_argument("--stay-connected", action="store_true", help="Keep the WebSocket open after the scenario and continue sending Heartbeats")
    p.add_argument("--aligned-data", action="store_true", help="Send offered current/power, voltage and frequency with OCPP Sample.Clock context")
    p.add_argument("--stand-seconds", type=float, default=180.0)
    p.add_argument("--meter-start-wh", type=float, default=100000.0)
    p.add_argument("--power-kw", type=float, default=7.4)
    p.add_argument("--offered-kw", type=float, default=11.0)
    p.add_argument("--current-a", type=float, default=10.7)
    p.add_argument("--offered-a", type=float, default=16.0)
    p.add_argument("--voltage-v", type=float, default=230.0)
    p.add_argument("--frequency-hz", type=float, default=50.0)
    p.add_argument("--temperature-c", type=float, default=31.0)
    p.add_argument("--soc-start", type=float, default=55.0)
    p.add_argument("--soc-step", type=float, default=1.0)
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
