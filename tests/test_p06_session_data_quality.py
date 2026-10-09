"""P06 read-only session data quality regressions."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="p06-import-"))
from app import db


class P06SessionDataQuality(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix="p06-")
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        for name,value in (("DATA_DIR",root),("DB_PATH",root/"test.sqlite3")):
            p=patch.object(db,name,value);p.start();self.addCleanup(p.stop)
        db.init_db()
        db.upsert_charge_point("P06",status="Available",connector_count=1)
        db.discover_connector("P06",1,status="Available")
        self.tx=db.start_transaction("P06",connector_id=1,meter_start_kwh=12.0)

    def test_normal_active_session_not_flagged(self):
        report=db.session_data_quality(charge_point_id="P06")
        self.assertEqual(report["checked"],1)
        self.assertEqual(report["flagged"],0)
        self.assertTrue(report["read_only"])

    def test_meter_rollback_detected_without_mutation(self):
        with db._connect() as conn:
            conn.execute("UPDATE transactions SET last_meter_kwh=10 WHERE id=?",(self.tx,))
            conn.commit()
        before=db.get_transaction(self.tx)
        report=db.session_data_quality(charge_point_id="P06")
        self.assertIn("meter_rollback",report["items"][0]["flags"])
        self.assertEqual(db.get_transaction(self.tx)["last_meter_kwh"],before["last_meter_kwh"])

    def test_inconsistent_completion_is_detected(self):
        with db._connect() as conn:
            conn.execute("UPDATE transactions SET status='Completed',ended_at=NULL WHERE id=?",(self.tx,))
            conn.commit()
        self.assertIn("closed_without_end_timestamp",db.session_data_quality()["items"][0]["flags"])

    def test_end_before_start_detected(self):
        with db._connect() as conn:
            conn.execute("UPDATE transactions SET ended_at='2020-01-01T00:00:00Z' WHERE id=?",(self.tx,))
            conn.commit()
        self.assertIn("end_before_start",db.session_data_quality()["items"][0]["flags"])

    def test_limit_is_bounded(self):
        self.assertEqual(db.session_data_quality(limit=100000)["checked"],1)

if __name__=="__main__":
    unittest.main()
