"""P04 RFID synchronization diagnostics: never claim an unverified station is in sync."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="p04-import-"))
from app import db


class P04Diagnostics(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="p04-sync-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name, value in (("DATA_DIR", root), ("DB_PATH", root / "test.sqlite3")):
            ctx = patch.object(db, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)
        db.init_db()
        db.upsert_charge_point("P04-SYNC", status="Available", connector_count=1)

    def state(self):
        return next(row for row in db.local_list_diagnostics() if row["id"] == "P04-SYNC")

    def test_unknown_without_station_version(self):
        state = self.state()
        self.assertEqual(state["sync_diagnostic"], "unknown")
        self.assertFalse(state["sync_verified"])

    def test_station_unsupported(self):
        db.set_local_list_state("P04-SYNC", supported=False, status="Nicht unterstützt")
        self.assertEqual(self.state()["sync_diagnostic"], "unsupported")

    def test_negative_station_version_is_not_a_valid_match(self):
        db.set_local_list_state("P04-SYNC", station_version=-1, pending=False, supported=True)
        self.assertEqual(self.state()["sync_diagnostic"], "unknown")

    def test_pending_does_not_mean_synced(self):
        version = db.rfid_local_list_version()
        db.set_local_list_state("P04-SYNC", station_version=version, pending=True, supported=True)
        self.assertEqual(self.state()["sync_diagnostic"], "out_of_sync")

    def test_matching_versions_with_confirmation(self):
        version = db.rfid_local_list_version()
        db.set_local_list_state("P04-SYNC", station_version=version, pending=False,
                                supported=True, status="Synchronisiert", response="Accepted", synced=True)
        state = self.state()
        self.assertEqual(state["sync_diagnostic"], "version_match")
        self.assertTrue(state["sync_verified"])

    def test_station_behind_or_ahead(self):
        db.set_local_list_state("P04-SYNC", station_version=0, pending=False, supported=True)
        self.assertEqual(self.state()["sync_diagnostic"], "out_of_sync")
        db.set_local_list_state("P04-SYNC", station_version=99999, pending=False, supported=True)
        self.assertEqual(self.state()["sync_diagnostic"], "station_ahead")

    def test_failed_send_is_not_verified(self):
        version = db.rfid_local_list_version()
        db.set_local_list_state("P04-SYNC", station_version=version, pending=False,
                                supported=True, response="Failed")
        self.assertEqual(self.state()["sync_diagnostic"], "needs_verification")

if __name__ == "__main__":
    unittest.main()
