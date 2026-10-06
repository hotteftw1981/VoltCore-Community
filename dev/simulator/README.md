# OCPP Test Lab (development only)

The simulator is deliberately separate from the production UI. It speaks standard OCPP 1.6J to the normal WebSocket endpoint, so the backend exercises the same path as a real vendor-neutral charge point.

## Important

Use a staging/test backend whenever possible. Simulator sessions are real OCPP sessions from the backend's point of view and therefore appear in its database, audit/events and statistics.

The normal product contains no demo users, demo RFIDs or demo controls. The simulator is an optional development tool only.

## Scenarios

- `standtime` (default): authorization -> transaction -> extended MeterValues -> zero power while still connected -> stop
- `full`: normal charging transaction with extended MeterValues
- `telemetry`: MeterValues without an authenticated transaction
- `fault`: Faulted -> Available status cycle

The generated MeterValues include `Energy.Active.Import.Register`, `Power.Active.Import`, `Power.Offered`, `Current.Import`, `Current.Offered`, `Voltage`, `Frequency`, `Temperature` and optional `SoC`.

## Example

Install the two simulator requirements and run:

```bash
python tools/ocpp_simulator.py --backend ws://BACKEND-IP:9000 --charge-point-id DEV_SIM_001 --id-tag YOUR-TEST-RFID --scenario standtime
```

For `full`/`standtime`, the RFID must exist and be enabled in the backend. `telemetry` and `fault` can be used without a charging user.

A separate container can be built with:

```bash
docker build -f Dockerfile.simulator -t ocpp-test-lab .
docker run --rm ocpp-test-lab --backend ws://BACKEND-IP:9000 --scenario telemetry
```

No simulator service is added to the production `docker-compose.yml` on purpose.

## OCPP 1.6 schema notes

The simulator uses the OCPP 1.6 schema values `Hertz` for frequency and `Body` for the simulated station temperature location. The default charge point model is kept within the OCPP 1.6 20-character limit.

## Verbindung nach einem Szenario offen halten

Für die visuelle Prüfung im Backend kann der Simulator nach dem Szenario verbunden bleiben:

```bash
docker run --rm --network host ocpp-test-lab \
  --backend ws://127.0.0.1:9000 \
  --charge-point-id DEV_SIM_001 \
  --scenario telemetry \
  --stay-connected
```

Danach sendet das Test Lab weiter Heartbeats, bis es mit `Ctrl+C` beendet wird.


V0.8.8.3: Das Szenario `standtime` verwendet standardmäßig 180 Sekunden 0-kW-Phase, damit die Standard-Karenzzeit von 120 Sekunden sicher überschritten und die Standzeiterkennung getestet wird. Mit `--stand-seconds` kann der Wert überschrieben werden.

## Sampled + Aligned MeterValues testen (V0.8.8.8)

Mit `--aligned-data` sendet das Test Lab angebotene Leistung/Strom, Spannung und Frequenz mit dem OCPP-Kontext `Sample.Clock`; die übrigen Werte bleiben `Sample.Periodic`. Damit lässt sich die neue MeterValues-Matrix ohne herstellerspezifische Sonderlogik prüfen:

```bash
docker run --rm --network host ocpp-test-lab \
  --backend ws://127.0.0.1:9000 \
  --charge-point-id DEV_SIM_001 \
  --scenario telemetry \
  --aligned-data \
  --stay-connected
```

Für den optionalen 0-kW-Auto-Stop zuerst am Ladepunkt unter **Einstellungen -> Session & Standzeit** z. B. `5 Minuten` wählen. Danach kann `--scenario standtime --stand-seconds 360` verwendet werden. Das Test Lab akzeptiert `RemoteStopTransaction` und beendet die Session mit Stop-Grund `Remote`.
