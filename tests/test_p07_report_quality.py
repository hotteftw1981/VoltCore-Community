"""P07 financial reports identify data anomalies without changing historical totals."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault("DATA_DIR",tempfile.mkdtemp(prefix="p07-import-"))
from app import db

class P07ReportQuality(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory(prefix="p07-");self.addCleanup(folder.cleanup)
        root=Path(folder.name)
        for name,value in (("DATA_DIR",root),("DB_PATH",root/"sqlite.db")):
            p=patch.object(db,name,value);p.start();self.addCleanup(p.stop)
        db.init_db()
        db.upsert_charge_point("P07",status="Available")
        db.discover_connector("P07",1,status="Available")
        self.tx=db.start_transaction("P07",connector_id=1,meter_start_kwh=10)
        db.stop_transaction(self.tx,meter_stop_kwh=12,status="Completed")

    def test_normal_report_has_no_quality_findings(self):
        report=db.reporting_bundle(charge_point_id="P07")
        self.assertEqual(report["quality"]["flagged_sessions"],0)
        self.assertTrue(report["quality"]["totals_preserved"])

    def test_bad_end_timestamp_flagged_but_not_modified(self):
        with db._connect() as conn:
            conn.execute("UPDATE transactions SET ended_at='2020-01-01T00:00:00Z' WHERE id=?",(self.tx,))
            conn.commit()
        before=db.get_transaction(self.tx)
        report=db.reporting_bundle(charge_point_id="P07")
        self.assertEqual(report["quality"]["flagged_sessions"],1)
        self.assertIn("end_before_start",report["quality"]["issues"][0]["flags"])
        self.assertEqual(db.get_transaction(self.tx)["ended_at"],before["ended_at"])

    def test_negative_energy_flagged(self):
        with db._connect() as conn:
            conn.execute("UPDATE transactions SET energy_kwh=-5 WHERE id=?",(self.tx,))
            conn.commit()
        report=db.reporting_bundle(charge_point_id="P07")
        self.assertIn("invalid_energy",report["transactions"][0]["quality_flags"])
        self.assertTrue(report["summary"]["quality_review_required"])

if __name__=="__main__":
    unittest.main()
