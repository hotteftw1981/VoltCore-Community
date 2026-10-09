"""Isolated OCPP/SQLite regression baseline for VoltCore.

No real charging station, host data directory, Docker volume or network socket is used.
Run with: python -m unittest tests.test_ocpp_regression_baseline -v
The expectedFailure cases document *known open defects*, not accepted behavior.
When production code is fixed, remove expectedFailure in the SAME PR.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import db, ocpp_server


CP_ID = "SIM-ISOLATED-AMEDIO"
STAMP = "2026-10-09T08:00:00+00:00"


class OcppRegressionBaseline(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voltcore-p01-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for attribute, value in (("DATA_DIR", root), ("DB_PATH", root / "ocpp.sqlite3")):
            swap = patch.object(db, attribute, value)
            swap.start()
            self.addCleanup(swap.stop)
        db.init_db()
        db.upsert_charge_point(CP_ID, status="Available", connector_count=2)
        db.discover_connector(CP_ID, 1, status="Available")
        db.discover_connector(CP_ID, 2, status="Available")

    def new_card(self, suffix, *, status="Aktiv", user_status="Aktiv"):
        uid = "SIM-TAG-" + suffix
        user_id = db.create_user(
            name="Test " + suffix,
            status=user_status,
            gamification_enabled=False,
        )
        db.create_rfid_card(uid, user_id=user_id, status=status)
        return uid

    def begin(self, connector_id=1, *, meter_start=10.0, uid=None):
        return db.start_transaction(
            CP_ID, connector_id=connector_id, id_tag=uid,
            meter_start_kwh=meter_start,
        )

    def test_known_rfid_is_accepted(self):
        uid = self.new_card("ALLOWED")
        decision = db.authorization_decision(uid, CP_ID)
        self.assertTrue(decision["accepted"], decision)
        self.assertEqual(decision["ocpp_status"], "Accepted")

    def test_blocked_and_unknown_rfid_are_rejected(self):
        blocked = self.new_card("BLOCKED", status="Gesperrt")
        self.assertFalse(db.authorization_decision(blocked, CP_ID)["accepted"])
        self.assertFalse(db.authorization_decision("SIM-NOT-REGISTERED", CP_ID)["accepted"])

    def test_two_connectors_retain_independent_transactions(self):
        first = self.begin(1, meter_start=10.0)
        second = self.begin(2, meter_start=20.0)
        slots = {int(row["connector_id"]): row for row in db.connectors_for_charge_point(CP_ID)}
        self.assertEqual(int(slots[1]["transaction_id"]), first)
        self.assertEqual(int(slots[2]["transaction_id"]), second)
        self.assertEqual(db.get_transaction(first)["status"], "Active")
        self.assertEqual(db.get_transaction(second)["status"], "Active")
        db.update_transaction_from_meter(first, meter_kwh=10.5, measured_at=STAMP)
        self.assertAlmostEqual(float(db.get_transaction(first)["energy_kwh"]), 0.5)
        self.assertTrue(db.get_transaction(second)["energy_kwh"] in (None, 0))

    async def test_ocpp_authorize_start_stop_happy_path(self):
        uid = self.new_card("OCPP")
        charge_point = ocpp_server.ChargePoint(CP_ID, None)
        accepted = await charge_point.on_authorize(uid)
        self.assertEqual(accepted.id_tag_info["status"], "Accepted")
        started = await charge_point.on_start_transaction(
            connector_id=1, id_tag=uid, meter_start=10000, timestamp=STAMP
        )
        tx = int(started.transaction_id)
        self.assertGreater(tx, 0)
        self.assertEqual(db.get_transaction(tx)["status"], "Active")
        with patch.object(ocpp_server, "sync_pending_local_lists", new_callable=AsyncMock):
            await charge_point.on_stop_transaction(
                transaction_id=tx, meter_stop=10500,
                timestamp="2026-10-09T08:30:00+00:00", reason="Local"
            )
            await asyncio.sleep(0)
        finished = db.get_transaction(tx)
        self.assertEqual(finished["status"], "Completed")
        self.assertAlmostEqual(float(finished["energy_kwh"]), 0.5)

    @unittest.expectedFailure
    def test_known_defect_out_of_order_meter_must_not_reduce_energy(self):
        tx = self.begin()
        db.update_transaction_from_meter(tx, meter_kwh=11.0, measured_at="2026-10-09T08:20:00+00:00")
        db.update_transaction_from_meter(tx, meter_kwh=10.5, measured_at="2026-10-09T08:10:00+00:00")
        row = db.get_transaction(tx)
        self.assertAlmostEqual(float(row["energy_kwh"]), 1.0)
        self.assertEqual(row["last_meter_at"], "2026-10-09T08:20:00+00:00")

    @unittest.expectedFailure
    def test_known_defect_late_meter_must_not_modify_finished_session(self):
        tx = self.begin()
        db.stop_transaction(tx, meter_stop_kwh=11.0, ended_at="2026-10-09T08:20:00+00:00")
        db.update_transaction_from_meter(tx, meter_kwh=12.0, measured_at="2026-10-09T08:21:00+00:00")
        self.assertAlmostEqual(float(db.get_transaction(tx)["energy_kwh"]), 1.0)

    @unittest.expectedFailure
    def test_known_defect_duplicate_stop_must_preserve_first_end(self):
        tx = self.begin()
        db.stop_transaction(tx, meter_stop_kwh=11.0, ended_at="2026-10-09T08:20:00+00:00")
        db.stop_transaction(tx, meter_stop_kwh=12.0, ended_at="2026-10-09T08:25:00+00:00")
        row = db.get_transaction(tx)
        self.assertEqual(row["ended_at"], "2026-10-09T08:20:00+00:00")
        self.assertAlmostEqual(float(row["energy_kwh"]), 1.0)


if __name__ == "__main__":
    unittest.main()
