"""P03 direct API authorization and validation regression tests."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="p03-api-import-"))
from fastapi import HTTPException
from app import db, main


class P03ApiPermissions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="p03-api-")
        self.addCleanup(temp.cleanup)
        for name, value in (("DATA_DIR", Path(temp.name)), ("DB_PATH", Path(temp.name) / "db.sqlite3")):
            ctx = patch.object(db, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)
        db.init_db()
        self.user = db.create_user("P03 API driver", gamification_enabled=False)
        self.card = db.create_rfid_card("P03-API-KEY", user_id=self.user)

    def request(self, role):
        return SimpleNamespace(state=SimpleNamespace(auth_user={"role": role}))

    def execute(self, fn, *args):
        return asyncio.run(fn(*args))

    def test_non_admin_cannot_modify_user_limit(self):
        for role in ("viewer", "user", None):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException) as context:
                    self.execute(main.api_p03_set_user_charging_limit,
                                 self.request(role), self.user,
                                 main.ChargingSessionLimitPayload(limit=2))
                self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(db.charging_session_limit_snapshot("user", self.user)["limit"], 1)

    def test_non_admin_cannot_modify_rfid_limit(self):
        for role in ("viewer", "user", None):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException) as context:
                    self.execute(main.api_p03_set_rfid_charging_limit,
                                 self.request(role), self.card,
                                 main.ChargingSessionLimitPayload(limit=2))
                self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(db.charging_session_limit_snapshot("rfid", self.card)["limit"], 1)

    def test_admin_can_update_and_read_both_limits(self):
        admin = self.request("admin")
        user = self.execute(main.api_p03_set_user_charging_limit, admin, self.user,
                            main.ChargingSessionLimitPayload(limit=3))
        card = self.execute(main.api_p03_set_rfid_charging_limit, admin, self.card,
                            main.ChargingSessionLimitPayload(limit=2))
        self.assertTrue(user["ok"])
        self.assertEqual(user["limit"], 3)
        self.assertEqual(card["limit"], 2)
        self.assertEqual(self.execute(main.api_p03_user_charging_limit, self.user)["limit"], 3)
        self.assertEqual(self.execute(main.api_p03_rfid_charging_limit, self.card)["limit"], 2)

    def test_invalid_limit_and_missing_id(self):
        admin = self.request("admin")
        with self.assertRaises(HTTPException) as invalid:
            self.execute(main.api_p03_set_user_charging_limit, admin, self.user,
                         main.ChargingSessionLimitPayload(limit=0))
        self.assertEqual(invalid.exception.status_code, 400)
        with self.assertRaises(HTTPException) as missing:
            self.execute(main.api_p03_set_rfid_charging_limit, admin, 99999999,
                         main.ChargingSessionLimitPayload(limit=2))
        self.assertEqual(missing.exception.status_code, 404)

if __name__ == "__main__":
    unittest.main()
