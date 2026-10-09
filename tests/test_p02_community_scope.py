"""Community-only P02 scope regressions: no personal portal, no gamification.

All database operations run in a temporary SQLite database, never /data.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="voltcore-p02-import-"))
from app import db, main


class CommunityScopeRoutes(unittest.TestCase):
    def test_no_portal_or_gamification_routes_are_registered(self):
        banned = (
            "/public/ladeguthaben",
            "/admin/ladeguthaben",
            "/api/public/portal",
            "/api/public/charging-budgets",
            "/api/portal-admin",
            "/api/engagement",
            "/engagement",
            "/api/settings/bonus-policy",
            "/api/settings/portal-leaderboard-names",
            "/api/rfid/requests",
        )
        paths = [route.path for route in main.app.routes]
        for path in paths:
            for prefix in banned:
                with self.subTest(path=path, prefix=prefix):
                    self.assertFalse(path == prefix or path.startswith(prefix + "/"), path)
        for path in ("/public/ladeguthaben", "/api/public/charging-budgets"):
            self.assertFalse(main._is_public_path(path))

    def test_disallowed_templates_are_not_shipped(self):
        root = Path(__file__).resolve().parents[1] / "app/templates"
        for name in ("engagement.html", "public_budgets.html", "portal_pin_reset.html"):
            with self.subTest(template=name):
                self.assertFalse((root / name).exists())

    def test_community_user_screen_has_no_portal_editor(self):
        html = (Path(__file__).resolve().parents[1] / "app/templates/users.html").read_text(encoding="utf-8")
        self.assertNotIn("portalPinInput", html)
        self.assertNotIn("loadPortalStatus(", html)
        self.assertNotIn("RFID-Self-Service", html)

    def test_diagnostic_filter_handler_is_valid(self):
        html = (Path(__file__).resolve().parents[1] / "app/templates/charge_point.html").read_text(encoding="utf-8")
        self.assertIn("$('diagnosticFilter').addEventListener('click'", html)
        self.assertNotIn("addEventListener$('diagnosticFilter')", html)


class CommunityNeutralUsers(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="voltcore-p02-db-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for key, value in (("DATA_DIR", root), ("DB_PATH", root / "ocpp.sqlite3")):
            monkey = patch.object(db, key, value)
            monkey.start()
            self.addCleanup(monkey.stop)
        db.init_db()

    def test_normal_user_cannot_gain_gamification_or_portal(self):
        user_id = db.create_user("P02 no XP", gamification_enabled=True, weekly_hours=39)
        with db._connect() as conn:
            row = conn.execute("SELECT portal_enabled,portal_pin_hash,gamification_enabled,weekly_hours FROM users WHERE id=?", (user_id,)).fetchone()
            self.assertEqual(int(row["portal_enabled"]), 0)
            self.assertEqual(int(row["gamification_enabled"]), 0)
            self.assertIsNone(row["portal_pin_hash"])
            self.assertIsNone(row["weekly_hours"])
        db.update_user(user_id, gamification_enabled=True, name="P02 neutral")
        with db._connect() as conn:
            self.assertEqual(conn.execute("SELECT gamification_enabled FROM users WHERE id=?", (user_id,)).fetchone()[0], 0)

    def test_legacy_portal_flags_are_reset_without_deleting_user(self):
        uid = db.create_user("P02 legacy")
        with db._connect() as conn:
            conn.execute("UPDATE users SET portal_enabled=1, gamification_enabled=1 WHERE id=?", (uid,))
            conn.commit()
        db.init_db()
        with db._connect() as conn:
            row = conn.execute("SELECT portal_enabled,gamification_enabled FROM users WHERE id=?", (uid,)).fetchone()
        self.assertEqual(tuple(row), (0, 0))

    def test_registration_approval_creates_neutral_charging_user(self):
        now = db.utc_now()
        with db._connect() as conn:
            cur = conn.execute(
                """INSERT INTO access_requests(
                    created_at,updated_at,status,name,street,postal_code,city,email,
                    phone,vehicle_plate,terms_version,terms_snapshot,signature_path,
                    signed_at,verified_at,ip_hash
                ) VALUES (?,?,'Neu',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now,now,"P02 Applicant","Teststrasse","12345","Teststadt",
                 "p02@example.invalid","","","1","{}", "sha256:test",now,now,"p02"),
            )
            request_id = int(cur.lastrowid)
            conn.commit()
        result = db.approve_access_request(request_id, None, None, 42.0, "block", return_details=True)
        uid = result["user_id"]
        with db._connect() as conn:
            user = conn.execute("SELECT portal_enabled,portal_pin_hash,gamification_enabled,monthly_kwh_limit FROM users WHERE id=?", (uid,)).fetchone()
        self.assertEqual(int(user["portal_enabled"]), 0)
        self.assertIsNone(user["portal_pin_hash"])
        self.assertEqual(int(user["gamification_enabled"]), 0)
        self.assertEqual(float(user["monthly_kwh_limit"]), 42.0)


if __name__ == "__main__":
    unittest.main()
