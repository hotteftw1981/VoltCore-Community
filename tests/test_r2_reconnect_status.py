"""Reconnect status arriving late must never close a newer live session."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from app import db, ocpp_server


class ReconnectStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="voltcore-reconnect-")
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        for key,value in (("DATA_DIR",root),("DB_PATH",root/"reconnect.sqlite3")):
            ctx=patch.object(db,key,value);ctx.start();self.addCleanup(ctx.stop)
        db.init_db()
        db.upsert_charge_point("CP-RECONNECTED",status="Available",connector_count=1)
        db.discover_connector("CP-RECONNECTED",1,status="Available")
        uid=db.create_user("Synthetic Reconnect Driver")
        db.create_rfid_card("RECONNECT-RFID",user_id=uid,status="Aktiv")
        self.cp=ocpp_server.ChargePoint("CP-RECONNECTED",MagicMock())

    def start(self):
        return db.start_transaction("CP-RECONNECTED",id_tag="RECONNECT-RFID",connector_id=1)

    async def send_available(self,when):
        await self.cp.on_status_notification(connector_id=1,error_code="NoError",
              status="Available",timestamp=when.isoformat())

    async def test_old_available_notification_does_not_end_new_session(self):
        tx=self.start()
        old=datetime.now(timezone.utc)-timedelta(hours=1)
        await self.send_available(old)
        t=db.get_transaction(tx)
        self.assertEqual(t["status"],"Active")
        self.assertIsNone(t["ended_at"])
        c=db.connectors_for_charge_point("CP-RECONNECTED")[0]
        self.assertEqual(c["transaction_id"],tx)
        self.assertEqual(c["status"],"Charging")

    async def test_atomic_database_guard_ignores_older_status_race(self):
        tx=self.start()
        old=(datetime.now(timezone.utc)-timedelta(minutes=40)).isoformat()
        closed=db.reconcile_active_transactions_for_connector(
            "CP-RECONNECTED",1,ended_at=old,reason="ConnectorAvailable")
        self.assertEqual(closed,[])
        accepted=db.set_status_notification(
            "CP-RECONNECTED",1,"Available",observed_at=old)
        self.assertFalse(accepted)
        self.assertEqual(db.get_transaction(tx)["status"],"Active")
        connector=db.connectors_for_charge_point("CP-RECONNECTED")[0]
        self.assertEqual(connector["status"],"Charging")
        self.assertEqual(connector["transaction_id"],tx)

    async def test_missing_timestamp_must_not_end_active_session(self):
        tx=self.start()
        await self.cp.on_status_notification(connector_id=1,error_code="NoError",status="Available")
        self.assertEqual(db.get_transaction(tx)["status"],"Active")
        self.assertIsNone(db.get_transaction(tx)["ended_at"])
        row=db.connectors_for_charge_point("CP-RECONNECTED")[0]
        self.assertEqual(row["transaction_id"],tx)
        self.assertEqual(row["status"],"Charging")

    async def test_invalid_timestamp_must_not_end_active_session(self):
        tx=self.start()
        await self.cp.on_status_notification(connector_id=1,error_code="NoError",
            status="Available",timestamp="wrong-clock")
        self.assertEqual(db.get_transaction(tx)["status"],"Active")
        self.assertEqual(db.connectors_for_charge_point("CP-RECONNECTED")[0]["status"],"Charging")

    async def test_future_timestamp_must_not_end_active_session(self):
        tx=self.start()
        await self.send_available(datetime.now(timezone.utc)+timedelta(days=2))
        self.assertEqual(db.get_transaction(tx)["status"],"Active")
        self.assertEqual(db.connectors_for_charge_point("CP-RECONNECTED")[0]["transaction_id"],tx)

    async def test_missing_timestamp_without_active_session_remains_available(self):
        await self.cp.on_status_notification(connector_id=1,error_code="NoError",status="Available")
        self.assertEqual(db.connectors_for_charge_point("CP-RECONNECTED")[0]["status"],"Available")

    async def test_stop_transaction_bad_clock_uses_server_time(self):
        for value in (None,"not-an-iso-timestamp","2001-01-01T00:00:00Z",
                      "2099-12-31T23:59:59Z"):
            with self.subTest(timestamp=value):
                tx=self.start()
                started=datetime.fromisoformat(db.get_transaction(tx)["started_at"])
                db.stop_transaction(tx,ended_at=value)
                ended=datetime.fromisoformat(db.get_transaction(tx)["ended_at"])
                self.assertGreaterEqual(ended,started)
                self.assertLessEqual(ended,datetime.now(timezone.utc)+timedelta(minutes=2))

    async def test_valid_historical_stop_time_is_preserved(self):
        tx=self.start()
        old_start=datetime.now(timezone.utc)-timedelta(hours=3)
        stop=old_start+timedelta(hours=1)
        with db._connect() as conn:
            conn.execute("UPDATE transactions SET started_at=? WHERE id=?",(old_start.isoformat(),tx))
            conn.commit()
        db.stop_transaction(tx,ended_at=stop.isoformat())
        self.assertEqual(datetime.fromisoformat(db.get_transaction(tx)["ended_at"]),stop)

    async def test_new_available_notification_reconciles_missing_stop(self):
        tx=self.start()
        await self.send_available(datetime.now(timezone.utc)+timedelta(seconds=1))
        t=db.get_transaction(tx)
        self.assertEqual(t["status"],"Interrupted")
        self.assertIsNotNone(t["ended_at"])


if __name__=="__main__":
    unittest.main()
