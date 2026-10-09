"""P03 core concurrency tests, shared between editions."""
import concurrent.futures
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="p03-import-"))
from app import db

class ChargingLimits(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="p03-limits-")
        self.addCleanup(tmp.cleanup)
        for key, value in (("DATA_DIR", Path(tmp.name)), ("DB_PATH", Path(tmp.name) / "charging.sqlite3")):
            p = patch.object(db, key, value)
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        db.upsert_charge_point("P03", status="Available", connector_count=3)
        for i in range(1, 4):
            db.discover_connector("P03", i, status="Available")
        self.user = db.create_user("P03 driver", gamification_enabled=False)
        db.create_rfid_card("P03-A", user_id=self.user, status="Aktiv")
        db.create_rfid_card("P03-B", user_id=self.user, status="Aktiv")

    def test_one_card_default_rejects_second_start(self):
        db.start_transaction("P03", id_tag="P03-A", connector_id=1)
        with self.assertRaisesRegex(ValueError, "RFID_CONCURRENT_SESSION_LIMIT"):
            db.start_transaction("P03", id_tag="P03-A", connector_id=2)

    def test_different_cards_obey_user_limit(self):
        db.start_transaction("P03", id_tag="P03-A", connector_id=1)
        with self.assertRaisesRegex(ValueError, "USER_CONCURRENT_SESSION_LIMIT"):
            db.start_transaction("P03", id_tag="P03-B", connector_id=2)

    def test_configurable_limits(self):
        with db._connect() as conn:
            conn.execute("UPDATE users SET max_concurrent_sessions=2 WHERE id=?", (self.user,))
            conn.execute("UPDATE rfid_cards SET max_concurrent_sessions=2 WHERE uid='P03-A'")
            conn.commit()
        a = db.start_transaction("P03", id_tag="P03-A", connector_id=1)
        b = db.start_transaction("P03", id_tag="P03-A", connector_id=2)
        self.assertNotEqual(a, b)
        with self.assertRaisesRegex(ValueError, "RFID_CONCURRENT_SESSION_LIMIT"):
            db.start_transaction("P03", id_tag="P03-A", connector_id=3)

    def test_session_limit_settings_and_usage(self):
        snapshot = db.charging_session_limit_snapshot("rfid", 1)
        self.assertEqual(snapshot["limit"], 1)
        self.assertTrue(db.set_charging_session_limit("user", self.user, 2))
        self.assertTrue(db.set_charging_session_limit("rfid", 1, 2))
        self.assertEqual(db.charging_session_limit_snapshot("user", self.user)["limit"], 2)
        with self.assertRaisesRegex(ValueError, "INVALID_CHARGING_LIMIT_VALUE"):
            db.set_charging_session_limit("user", self.user, 0)
        with self.assertRaisesRegex(ValueError, "INVALID_CHARGING_LIMIT_KIND"):
            db.set_charging_session_limit("vehicle", self.user, 2)

    def test_simultaneous_two_threads_one_slot(self):
        gate = threading.Barrier(2, timeout=5)
        def start(connector):
            gate.wait()
            try:
                return db.start_transaction("P03", id_tag="P03-A", connector_id=connector)
            except ValueError as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(start, 1)
            b = pool.submit(start, 2)
            results = (a.result(timeout=15), b.result(timeout=15))
        self.assertEqual(sum(isinstance(r, int) for r in results), 1)
        self.assertIn("RFID_CONCURRENT_SESSION_LIMIT", results)

if __name__ == "__main__":
    unittest.main()
