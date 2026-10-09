"""P01 OCPP edge regression cases. Fully isolated from production charge points."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from app import db, ocpp_server

CP = "P01-SIM-MULTI"


class OcppEdgeRegression(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory(prefix="p01-ocpp-")
        self.addCleanup(sandbox.cleanup)
        root = Path(sandbox.name)
        for key, value in (("DATA_DIR", root), ("DB_PATH", root / "test.sqlite3")):
            context = patch.object(db, key, value)
            context.start()
            self.addCleanup(context.stop)
        db.init_db()
        db.upsert_charge_point(CP, status="Available", connector_count=2)
        db.discover_connector(CP, 1)
        db.discover_connector(CP, 2)

    @unittest.expectedFailure
    async def test_stale_connection_cleanup_cannot_remove_newer_connection(self):
        first_ws, next_ws = object(), object()
        gates = {first_ws: asyncio.Event(), next_ws: asyncio.Event()}
        old_sleep = asyncio.sleep

        class FakeChargePoint:
            def __init__(self, cp_id, ws):
                self.websocket = ws
            async def start(self):
                await gates[self.websocket].wait()

        async def quick_sleep(_time):
            await old_sleep(0)

        with patch.object(ocpp_server, "ChargePoint", FakeChargePoint), \
             patch.object(ocpp_server, "sync_local_list", new=AsyncMock()), \
             patch.object(ocpp_server.asyncio, "sleep", new=quick_sleep), \
             patch.dict(ocpp_server.ACTIVE_CONNECTIONS, {}, clear=True):
            older = asyncio.create_task(ocpp_server.on_connect(first_ws, "/" + CP))
            for _ in range(30):
                if CP in ocpp_server.ACTIVE_CONNECTIONS:
                    break
                await old_sleep(0)
            newer = asyncio.create_task(ocpp_server.on_connect(next_ws, "/" + CP))
            try:
                for _ in range(30):
                    current = ocpp_server.ACTIVE_CONNECTIONS.get(CP, {}).get("charge_point")
                    if current is not None and current.websocket is next_ws:
                        break
                    await old_sleep(0)
                else:
                    self.fail("New simulated connection did not register")
                gates[first_ws].set()
                await older
                self.assertIn(CP, ocpp_server.ACTIVE_CONNECTIONS)
                self.assertIs(ocpp_server.ACTIVE_CONNECTIONS[CP]["charge_point"].websocket, next_ws)
            finally:
                gates[next_ws].set()
                await newer

    @unittest.expectedFailure
    async def test_meter_values_on_idle_connector_cannot_use_other_connector_session(self):
        db.start_transaction(CP, connector_id=1, meter_start_kwh=10.0)
        cp = ocpp_server.ChargePoint(CP, None)
        with patch.object(cp, "_enforce_monthly_budget", new=AsyncMock()), \
             patch.object(cp, "_enforce_zero_flow_policy", new=AsyncMock()):
            await cp.on_meter_values(connector_id=2, meter_value=[])
            await asyncio.sleep(0)
        with db._connect() as conn:
            row = conn.execute(
                "SELECT transaction_id FROM meter_samples WHERE charge_point_id=? "
                "AND connector_id=2 ORDER BY id DESC LIMIT 1", (CP,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["transaction_id"])

    @unittest.expectedFailure
    def test_same_rfid_single_session_default_not_enforced_yet(self):
        user_id = db.create_user("P01 Operator", gamification_enabled=False)
        db.create_rfid_card("P01-TEST-RFID", user_id=user_id)
        db.start_transaction(CP, connector_id=1, id_tag="P01-TEST-RFID")
        self.assertFalse(db.authorization_decision("P01-TEST-RFID", CP)["accepted"])


if __name__ == "__main__":
    unittest.main()
