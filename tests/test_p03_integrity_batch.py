"""P03 integration guardrails for delayed OCPP status and persistent admission."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="p03-integrity-import-"))
from app import db, ocpp_server


class P03Integrity(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="p03-integrity-")
        self.addCleanup(temp.cleanup)
        for name, val in (("DATA_DIR", Path(temp.name)), ("DB_PATH", Path(temp.name) / "db.sqlite3")):
            p = patch.object(db, name, val)
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        db.upsert_charge_point("P03-RESTART", status="Available", connector_count=2)
        for connector in (1, 2):
            db.discover_connector("P03-RESTART", connector, status="Available")
        self.driver = db.create_user("P03 Driver", gamification_enabled=False)
        db.create_rfid_card("P03-KEY", user_id=self.driver)

    def test_limit_survives_reinitialization(self):
        self.assertTrue(db.set_charging_session_limit("user", self.driver, 3))
        db.init_db()
        self.assertEqual(db.charging_session_limit_snapshot("user", self.driver)["limit"], 3)

    def test_status_timestamp_guard_is_wired(self):
        # Full OCPP integration still needs an async station simulator.
        source = Path(ocpp_server.__file__).read_text(encoding="utf-8")
        self.assertIn("OlderThanActiveSession", source)
        self.assertIn('status_text == "Available" and kwargs.get("timestamp")', source)

    def test_available_reconciliation_is_connector_scoped(self):
        db.set_charging_session_limit("user", self.driver, 2)
        other = db.create_rfid_card("P03-KEY-2", user_id=self.driver)
        one = db.start_transaction("P03-RESTART", id_tag="P03-KEY", connector_id=1)
        two = db.start_transaction("P03-RESTART", id_tag="P03-KEY-2", connector_id=2)
        db.reconcile_active_transactions_for_connector("P03-RESTART", 1)
        self.assertNotEqual(db.get_transaction(one)["status"], "Active")
        self.assertEqual(db.get_transaction(two)["status"], "Active")

    def test_card_owner_change_does_not_move_active_session(self):
        first = db.start_transaction("P03-RESTART", id_tag="P03-KEY", connector_id=1)
        second_driver = db.create_user("P03 Other", gamification_enabled=False)
        with db._connect() as conn:
            conn.execute("UPDATE rfid_cards SET user_id=? WHERE uid='P03-KEY'", (second_driver,))
            conn.commit()
        self.assertEqual(db.get_transaction(first)["user_id"], self.driver)
        self.assertEqual(db.charging_session_limit_snapshot("user", self.driver)["active"], 1)

if __name__ == "__main__":
    unittest.main()
