from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CommunityBackendOnlyTests(unittest.TestCase):
    def test_personal_portal_templates_are_not_shipped(self):
        for rel in (
            "app/templates/public_budgets.html",
            "app/templates/portal_pin_reset.html",
        ):
            self.assertFalse((ROOT / rel).exists(), rel)

    def test_personal_portal_routes_are_not_exposed(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        for needle in (
            "/public/ladeguthaben",
            "/api/public/portal",
            "/api/public/charging-budgets",
            "/api/portal-admin",
            "PORTAL_COOKIE",
            "PORTAL_SESSION_HOURS",
            "PORTAL_PIN_ITERATIONS",
            "PORTAL_MAX_FAILURES",
            "_portal_pin_hash",
            "_portal_pin_ok",
            "_portal_request_user",
            "_generate_unique_portal_pin",
        ):
            self.assertNotIn(needle, main)

    def test_pin_session_database_infrastructure_is_removed(self):
        db = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
        for needle in (
            "CREATE TABLE IF NOT EXISTS portal_sessions",
            "CREATE TABLE IF NOT EXISTS portal_login_attempts",
            "CREATE TABLE IF NOT EXISTS portal_pin_reset_tokens",
            "CREATE TABLE IF NOT EXISTS portal_pin_reset_requests",
            "def set_user_portal_pin(",
            "def portal_pin_records(",
            "def create_portal_session(",
            "def portal_user_for_session(",
            "def record_portal_login_attempt(",
        ):
            self.assertNotIn(needle, db)
        self.assertIn('DROP TABLE IF EXISTS portal_sessions', db)
        self.assertIn('DROP TABLE IF EXISTS portal_pin_reset_tokens', db)

    def test_backend_user_ui_has_no_pin_or_self_service_portal_controls(self):
        users = (ROOT / "app" / "templates" / "users.html").read_text(encoding="utf-8")
        for needle in (
            "Persönlicher Ladeportal-Zugang",
            "portalPinInput",
            "setPortalPin",
            "togglePortalEnabled",
            "RFID-Self-Service",
            "requestBody",
        ):
            self.assertNotIn(needle, users)


if __name__ == "__main__":
    unittest.main()
