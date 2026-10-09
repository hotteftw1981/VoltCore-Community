"""P03 full OCPP handler cycle and admission behavior (without physical charger)."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="p03-ocpp-import-"))
from app import db, ocpp_server


class P03OcppCycle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="p03-ocpp-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in (("DATA_DIR", root), ("DB_PATH", root / "test.sqlite3")):
            context = patch.object(db, name, value)
            context.start()
            self.addCleanup(context.stop)
        db.init_db()
        db.upsert_charge_point("P03-OCPP", status="Available", connector_count=2)
        for connector in (1, 2):
            db.discover_connector("P03-OCPP", connector, status="Available")
        self.user = db.create_user("OCPP test driver", gamification_enabled=False)
        self.card_id = db.create_rfid_card("P03-OCPP-A", user_id=self.user)
        self.cp = ocpp_server.ChargePoint("P03-OCPP", Mock())

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def test_authorize_start_block_stop_restart(self):
        authorized = self.run_async(self.cp.on_authorize("P03-OCPP-A"))
        self.assertEqual(authorized.id_tag_info["status"], "Accepted")
        first = self.run_async(self.cp.on_start_transaction(
            connector_id=1, id_tag="P03-OCPP-A", meter_start=10000,
            timestamp="2026-10-09T10:00:00Z"))
        self.assertEqual(first.id_tag_info["status"], "Accepted")
        transaction = first.transaction_id
        self.assertGreater(transaction, 0)
        denied = self.run_async(self.cp.on_start_transaction(
            connector_id=2, id_tag="P03-OCPP-A", meter_start=12000,
            timestamp="2026-10-09T10:01:00Z"))
        self.assertEqual(denied.id_tag_info["status"], "Blocked")
        self.assertEqual(db.get_transaction(transaction)["status"], "Active")
        self.run_async(self.cp.on_stop_transaction(
            transaction_id=transaction, meter_stop=11000,
            timestamp="2026-10-09T10:05:00Z", reason="Local"))
        self.assertNotEqual(db.get_transaction(transaction)["status"], "Active")
        again = self.run_async(self.cp.on_start_transaction(
            connector_id=2, id_tag="P03-OCPP-A", meter_start=12000,
            timestamp="2026-10-09T10:06:00Z"))
        self.assertEqual(again.id_tag_info["status"], "Accepted")
        self.assertNotEqual(again.transaction_id, transaction)

    def test_higher_limit_allows_two_connectors(self):
        db.set_charging_session_limit("user", self.user, 2)
        db.set_charging_session_limit("rfid", self.card_id, 2)
        first = self.run_async(self.cp.on_start_transaction(
            1, "P03-OCPP-A", 0, "2026-10-09T10:00:00Z"))
        second = self.run_async(self.cp.on_start_transaction(
            2, "P03-OCPP-A", 0, "2026-10-09T10:01:00Z"))
        self.assertEqual(first.id_tag_info["status"], "Accepted")
        self.assertEqual(second.id_tag_info["status"], "Accepted")
        self.assertEqual(db.charging_session_limit_snapshot("user", self.user)["active"], 2)

    def test_occupied_connector_cannot_be_replaced(self):
        first = self.run_async(self.cp.on_start_transaction(
            1, "P03-OCPP-A", 0, "2026-10-09T10:00:00Z"))
        duplicate = self.run_async(self.cp.on_start_transaction(
            1, "P03-OCPP-A", 0, "2026-10-09T10:00:01Z"))
        self.assertEqual(duplicate.id_tag_info["status"], "Blocked")
        self.assertEqual(db.get_transaction(first.transaction_id)["status"], "Active")

if __name__ == "__main__":
    unittest.main()
