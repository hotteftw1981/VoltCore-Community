"""OCPP status replay after StopTransaction must not fabricate new charging."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from app import db, ocpp_server


class PostStopStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp=tempfile.TemporaryDirectory(prefix="vc-r2-poststop-")
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        for name,value in (("DATA_DIR",root),("DB_PATH",root/"ocpp.sqlite3")):
            ctx=patch.object(db,name,value)
            ctx.start()
            self.addCleanup(ctx.stop)
        db.init_db()
        db.upsert_charge_point("R2-STATUS",status="Available",connector_count=2)
        for cid in (1,2):
            db.discover_connector("R2-STATUS",cid,status="Available")
        user=db.create_user("Status QA Driver")
        db.create_rfid_card("STATUS-QA",user_id=user,status="Aktiv")
        self.cp=ocpp_server.ChargePoint("R2-STATUS",MagicMock())

    def connector(self,cid=1):
        return next(x for x in db.connectors_for_charge_point("R2-STATUS")
                    if int(x["connector_id"])==cid)

    def start(self):
        return db.start_transaction("R2-STATUS",id_tag="STATUS-QA",connector_id=1)

    def complete(self):
        tx=self.start()
        db.stop_transaction(tx)
        db.set_status_notification("R2-STATUS",1,"Available")
        return tx

    async def notify(self,status,timestamp=None):
        params={"connector_id":1,"error_code":"NoError","status":status}
        if timestamp is not None:
            params["timestamp"]=timestamp
        with patch.object(ocpp_server,"_trigger_smart_charging",create=True) as trigger:
            response=await self.cp.on_status_notification(**params)
            return response,trigger

    async def test_stop_without_followup_status_does_not_leave_charging(self):
        tx=self.start()
        db.upsert_charge_point("R2-STATUS",station_status="Charging")
        db.stop_transaction(tx)
        c=self.connector()
        self.assertNotIn(c["status"],{"Charging","SuspendedEV","SuspendedEVSE"})
        self.assertIsNone(c["transaction_id"])
        self.assertEqual(c["power_kw"],0)

    async def test_stop_one_socket_keeps_second_active_but_clears_first(self):
        uid=db.create_user("Second synthetic driver")
        db.create_rfid_card("STATUS-SECOND",user_id=uid,status="Aktiv")
        first=self.start()
        second=db.start_transaction("R2-STATUS",id_tag="STATUS-SECOND",connector_id=2)
        db.stop_transaction(first)
        self.assertNotIn(self.connector(1)["status"],{"Charging","SuspendedEV","SuspendedEVSE"})
        self.assertEqual(self.connector(2)["status"],"Charging")
        self.assertEqual(self.connector(2)["transaction_id"],second)
        self.assertEqual(db.get_transaction(second)["status"],"Active")

    async def test_legacy_duplicate_stop_keeps_newest_connector_reference(self):
        first=self.start()
        with db._connect() as conn:
            newer=conn.execute(
                """INSERT INTO transactions(charge_point_id,connector_id,started_at,status)
                   VALUES(?,?,?,'Active')""",
                ("R2-STATUS",1,db.utc_now()),
            ).lastrowid
            conn.execute("UPDATE connectors SET transaction_id=?,status='Charging' WHERE charge_point_id=? AND connector_id=1",
                         (newer,"R2-STATUS"))
            conn.execute("UPDATE charge_points SET transaction_id=? WHERE id=?",
                         (newer,"R2-STATUS"))
            conn.commit()
        db.stop_transaction(first)
        self.assertEqual(self.connector()["transaction_id"],newer)
        self.assertEqual(self.connector()["status"],"Charging")
        self.assertEqual(db.get_transaction(newer)["status"],"Active")

    async def test_trusted_finishing_before_stop_is_preserved(self):
        tx=self.start()
        db.set_status_notification("R2-STATUS",1,"Finishing")
        db.stop_transaction(tx)
        self.assertEqual(self.connector()["status"],"Finishing")

    async def test_late_charging_does_not_revive_completed_charge(self):
        ended=self.complete()
        await self.notify("Charging",(datetime.now(timezone.utc)-timedelta(minutes=8)).isoformat())
        self.assertEqual(self.connector()["status"],"Available")
        self.assertIsNone(self.connector()["transaction_id"])
        self.assertEqual(db.get_transaction(ended)["status"],"Completed")

    async def test_post_stop_suspension_is_not_a_live_pause(self):
        self.complete()
        for status in ("SuspendedEV","SuspendedEVSE"):
            with self.subTest(status=status):
                _,trigger=await self.notify(status,datetime.now(timezone.utc).isoformat())
                self.assertEqual(self.connector()["status"],"Available")
                self.assertIsNone(self.connector()["transaction_id"])
                trigger.assert_not_called()

    async def test_old_status_does_not_paint_next_session_as_suspended(self):
        self.complete()
        current=self.start()
        old=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        await self.notify("SuspendedEV",old)
        self.assertEqual(self.connector()["status"],"Charging")
        self.assertEqual(self.connector()["transaction_id"],current)
        self.assertEqual(db.get_transaction(current)["status"],"Active")

    async def test_missing_or_invalid_status_time_cannot_override_active_session(self):
        current=self.start()
        for ts in (None,"invalid-date",(datetime.now(timezone.utc)+timedelta(days=3)).isoformat()):
            with self.subTest(timestamp=ts):
                await self.notify("SuspendedEV",ts)
                self.assertEqual(self.connector()["status"],"Charging")
                self.assertEqual(self.connector()["transaction_id"],current)

    async def test_current_status_during_active_session_still_works(self):
        tx=self.start()
        _,trigger=await self.notify("SuspendedEV",datetime.now(timezone.utc).isoformat())
        self.assertEqual(self.connector()["status"],"SuspendedEV")
        self.assertEqual(self.connector()["transaction_id"],tx)
        # Community has no SmartCharging extension; status itself must update.

    async def test_preparing_and_faulted_without_session_remain_visible(self):
        await self.notify("Preparing")
        self.assertEqual(self.connector()["status"],"Preparing")
        await self.notify("Faulted")
        self.assertEqual(self.connector()["status"],"Faulted")

    async def test_db_guard_is_atomic_on_unowned_charging_status(self):
        self.complete()
        changed=db.set_status_notification("R2-STATUS",1,"Charging",
            observed_at=datetime.now(timezone.utc).isoformat(),verify_live=True)
        self.assertFalse(changed)
        self.assertEqual(self.connector()["status"],"Available")


if __name__=="__main__":
    unittest.main()
