"""P05 read-only charging connector consistency and recovery diagnostics."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="p05-import-"))
from app import db


class P05ConnectorIntegrity(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix="p05-")
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        for name,value in (("DATA_DIR",root),("DB_PATH",root/"db.sqlite3")):
            p=patch.object(db,name,value);p.start();self.addCleanup(p.stop)
        db.init_db()
        db.upsert_charge_point("P05",status="Available",connector_count=2)
        db.discover_connector("P05",1,status="Available")
        db.discover_connector("P05",2,status="Available")

    def report(self):
        return db.ocpp_connector_integrity("P05")

    def test_normal_connectors(self):
        self.assertEqual(self.report()["level"],"ok")
        self.assertEqual(self.report()["findings"],[])

    def test_occupied_connector_is_not_changed_by_diagnostics(self):
        tx=db.start_transaction("P05",connector_id=1,meter_start_kwh=10)
        before=db.get_transaction(tx)
        report=self.report()
        self.assertEqual(report["active_sessions"],1)
        self.assertEqual(db.get_transaction(tx)["status"],"Active")
        self.assertEqual(db.get_transaction(tx)["started_at"],before["started_at"])

    def test_available_with_active_session_is_warning(self):
        tx=db.start_transaction("P05",connector_id=1)
        db.set_status_notification("P05",1,"Available")
        self.assertIn("available_with_active_session",[x["code"] for x in self.report()["findings"]])
        self.assertEqual(db.get_transaction(tx)["status"],"Active")

    def test_status_without_start_is_warning(self):
        db.set_status_notification("P05",2,"Charging")
        self.assertIn("charging_without_backend_session",[x["code"] for x in self.report()["findings"]])

    def test_fault_is_critical(self):
        db.set_status_notification("P05",1,"Faulted",error_code="GroundFailure")
        self.assertEqual(self.report()["level"],"critical")
        self.assertIn("connector_fault",[x["code"] for x in self.report()["findings"]])

    def test_unknown_charge_point(self):
        self.assertIsNone(db.ocpp_connector_integrity("NO-SUCH-CHARGER"))

    def test_stale_reference(self):
        with db._connect() as conn:
            conn.execute("UPDATE connectors SET transaction_id=99999 WHERE charge_point_id='P05' AND connector_id=1")
            conn.commit()
        self.assertIn("stale_connector_transaction_reference",[x["code"] for x in self.report()["findings"]])

if __name__=="__main__":
    unittest.main()
