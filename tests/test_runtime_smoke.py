import asyncio
import os
import sqlite3
import tempfile
import unittest


os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="voltcore-community-test-"))
os.environ.setdefault("WEB_PORT", "18010")
os.environ.setdefault("OCPP_PORT", "19010")
os.environ.setdefault("UPDATE_GITHUB_REPOSITORY", "hotteftw1981/VoltCore-Community")

from app import db, main, updates


class CommunityRuntimeSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def test_application_identity(self):
        self.assertEqual(main.app.title, "VoltCore Community")
        self.assertEqual(updates.REPOSITORY, "hotteftw1981/VoltCore-Community")

    def test_health_handler(self):
        payload = asyncio.run(main.health())
        self.assertEqual(payload.get("status"), "ok")
        self.assertEqual(payload.get("version"), main.APP_VERSION)

    def test_fresh_branding_is_community(self):
        branding = db.branding_settings()
        self.assertEqual(branding["product_name"], "VoltCore Community")
        self.assertEqual(branding["display_name"], "VoltCore Community")

    def test_public_surface_is_backend_only(self):
        self.assertNotIn("/public/ladeguthaben", main.PUBLIC_PATHS)
        self.assertNotIn("/public/access-request", main.PUBLIC_PATHS)

    def test_fresh_schema_contains_only_backend_scope(self):
        removed_tables = {
            "load_rules", "portal_pin_reset_requests", "portal_pin_reset_tokens",
            "access_request_verifications", "access_requests", "rfid_enrollment_sessions",
            "billing_groups", "user_billing_groups", "portal_sessions", "portal_login_attempts",
            "achievements", "achievement_awards", "gamification_events",
            "gamification_event_rewards", "gamification_event_results", "bonus_vouchers",
            "bonus_grants", "bonus_usage", "bonus_voucher_redemptions", "bonus_transfers",
            "rfid_replacement_requests", "cost_centers",
        }
        with sqlite3.connect(db.DB_PATH) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue(removed_tables.isdisjoint(tables), sorted(removed_tables & tables))
            user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            self.assertTrue(
                {"weekly_hours", "gamification_enabled", "portal_pin_hash", "portal_enabled"}.isdisjoint(user_columns),
                sorted(user_columns),
            )
            tariff_columns = {row[1] for row in conn.execute("PRAGMA table_info(tariffs)")}
            self.assertTrue({"cost_center", "billing_group_id"}.isdisjoint(tariff_columns), sorted(tariff_columns))


if __name__ == "__main__":
    unittest.main()
