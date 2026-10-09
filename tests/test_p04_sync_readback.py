"""P04 station acknowledgement must be followed by GetLocalListVersion verification."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="p04-verify-import-"))
from app import db, ocpp_server


class P04SyncReadback(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix="p04-readback-")
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        for name,value in (("DATA_DIR",root),("DB_PATH",root/"test.sqlite3")):
            p=patch.object(db,name,value)
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        db.upsert_charge_point("P04-VERIFY",status="Available")
        self.cp=SimpleNamespace(call=AsyncMock(return_value=SimpleNamespace(status="Accepted")))
        current=ocpp_server.ACTIVE_CONNECTIONS.get("P04-VERIFY")
        ocpp_server.ACTIVE_CONNECTIONS["P04-VERIFY"]={"charge_point":self.cp}
        self.addCleanup(lambda: (ocpp_server.ACTIVE_CONNECTIONS.pop("P04-VERIFY",None) if current is None else ocpp_server.ACTIVE_CONNECTIONS.__setitem__("P04-VERIFY",current)))

    def sync_with_readbacks(self,values):
        with patch.object(ocpp_server,"_local_list_version_from_station",new=AsyncMock(side_effect=values)), patch.object(
            ocpp_server,"_ensure_offline_authorization",new=AsyncMock(return_value={"status":"Aktiv"})):
            return asyncio.run(ocpp_server.sync_local_list("P04-VERIFY",force_full=True,reason="P04-Test"))

    def test_accepted_without_readback_is_not_verified(self):
        result=self.sync_with_readbacks([0,asyncio.TimeoutError()])
        self.assertTrue(result["accepted"])
        self.assertFalse(result["ok"])
        state=db.ensure_local_list_state("P04-VERIFY")
        self.assertEqual(state["pending"],1)
        self.assertIsNone(state["station_entry_count"])

    def test_accepted_with_matching_readback_is_verified(self):
        version=db.rfid_local_list_version()
        result=self.sync_with_readbacks([0,version])
        self.assertTrue(result["ok"])
        state=db.ensure_local_list_state("P04-VERIFY")
        self.assertEqual(state["pending"],0)
        self.assertEqual(state["station_version"],version)
        self.assertIsNone(state["station_entry_count"])

    def test_accepted_with_version_mismatch_remains_pending(self):
        version=db.rfid_local_list_version()
        result=self.sync_with_readbacks([0,version+3])
        self.assertFalse(result["ok"])
        self.assertEqual(db.ensure_local_list_state("P04-VERIFY")["pending"],1)

if __name__=="__main__":
    unittest.main()
