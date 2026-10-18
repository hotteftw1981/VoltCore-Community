"""Late OCPP MeterValues remain auditable, never regress live connector data."""
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from app import db


class LateMeterTelemetry(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix="voltcore-late-meter-")
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        for key,value in (("DATA_DIR",root),("DB_PATH",root/"telemetry.sqlite3")):
            ctx=patch.object(db,key,value);ctx.start();self.addCleanup(ctx.stop)
        db.init_db()
        db.upsert_charge_point("SYNTHETIC-LATE",status="Available",connector_count=1)
        db.discover_connector("SYNTHETIC-LATE",1,status="Available")
        self.user=db.create_user("Synthetic Late Packet Driver")
        self.card=db.create_rfid_card("SYNTHETIC-RFID",user_id=self.user,status="Aktiv")
        self.when=datetime.now(timezone.utc)

    def start(self):
        return db.start_transaction("SYNTHETIC-LATE",id_tag="SYNTHETIC-RFID",connector_id=1)

    def send(self,kw,time,tx=None,apply_live=True):
        db.add_meter_sample("SYNTHETIC-LATE",connector_id=1,
                            sampled_at=time.isoformat(),power_kw=kw,
                            transaction_id=tx,apply_live=apply_live,
                            raw_json='{"synthetic":true}',values_json='[]')

    def power(self):
        return db.connectors_for_charge_point("SYNTHETIC-LATE")[0]["power_kw"]

    def raw_count(self):
        with db._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM meter_samples").fetchone()[0]

    def test_out_of_order_packet_stays_raw_but_cannot_replace_live(self):
        tx=self.start()
        self.send(11.0,self.when,tx)
        self.send(0.0,self.when-timedelta(seconds=10),tx)
        self.assertEqual(self.power(),11.0)
        self.assertEqual(self.raw_count(),2)

    def test_packet_from_previous_transaction_does_not_change_new_live(self):
        first=self.start()
        self.send(11.0,self.when-timedelta(seconds=5),first)
        db.stop_transaction(first)
        newer=self.start()
        self.send(8.0,self.when,newer)
        self.send(0.0,self.when+timedelta(seconds=1),first)
        self.assertEqual(self.power(),8.0)
        self.assertEqual(self.raw_count(),3)
        self.assertEqual(db.get_transaction(newer)["status"],"Active")

    def test_unmatched_explicit_id_is_archived_only(self):
        active=self.start()
        self.send(9.0,self.when,active)
        self.send(0.0,self.when+timedelta(seconds=1),None,apply_live=False)
        self.assertEqual(self.power(),9.0)
        self.assertEqual(self.raw_count(),2)

    def test_future_timestamp_cannot_freeze_telemetry(self):
        tx=self.start()
        self.send(10.0,self.when,tx)
        self.send(0.0,self.when+timedelta(days=1),tx)
        self.assertEqual(self.power(),10.0)
        self.send(9.0,self.when+timedelta(seconds=1),tx)
        self.assertEqual(self.power(),9.0)
        self.assertEqual(self.raw_count(),3)

    def test_completed_session_cannot_be_reopened_by_late_energy(self):
        tx=self.start()
        self.send(9.0,self.when,tx)
        db.stop_transaction(tx,meter_stop_kwh=51.0)
        original=db.get_transaction(tx)
        db.update_transaction_from_meter(
            tx,meter_kwh=90.0,power_kw=22.0,
            measured_at=(self.when+timedelta(minutes=1)).isoformat())
        updated=db.get_transaction(tx)
        self.assertEqual(updated["status"],"Completed")
        self.assertEqual(updated["meter_stop_kwh"],original["meter_stop_kwh"])
        self.assertEqual(updated["energy_kwh"],original["energy_kwh"])

    def test_older_session_reading_cannot_regress_meter_total(self):
        tx=self.start()
        self.send(9.0,self.when,tx)
        db.update_transaction_from_meter(
            tx,meter_kwh=51.0,power_kw=10.0,measured_at=self.when.isoformat())
        before=db.get_transaction(tx)
        db.update_transaction_from_meter(
            tx,meter_kwh=50.1,power_kw=3.0,
            measured_at=(self.when-timedelta(seconds=20)).isoformat())
        after=db.get_transaction(tx)
        self.assertEqual(after["last_meter_kwh"],before["last_meter_kwh"])
        self.assertEqual(after["energy_kwh"],before["energy_kwh"])

    def test_future_meter_clock_cannot_overwrite_session_history(self):
        tx=self.start()
        db.update_transaction_from_meter(tx,meter_kwh=51.0,power_kw=8.0,
             measured_at=self.when.isoformat())
        original=db.get_transaction(tx)
        db.update_transaction_from_meter(tx,meter_kwh=51.3,power_kw=9.0,
             measured_at=(self.when+timedelta(days=4)).isoformat())
        updated=db.get_transaction(tx)
        self.assertEqual(updated["last_meter_at"],original["last_meter_at"])
        self.assertEqual(updated["last_meter_kwh"],original["last_meter_kwh"])
        self.assertEqual(updated["energy_kwh"],original["energy_kwh"])

    def test_same_timestamp_different_fields_allowed(self):
        tx=self.start()
        self.send(7.0,self.when,tx)
        self.send(6.0,self.when,tx)
        self.assertEqual(self.power(),6.0)


if __name__ == "__main__":
    unittest.main()
