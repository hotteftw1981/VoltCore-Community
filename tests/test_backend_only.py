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


    def test_rfid_self_service_is_removed(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        db = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
        for needle in (
            "/api/rfid/requests",
            "rfid_self_enroll_mode",
            "RFIDEnrollmentStartPayload",
            "RFIDEnrollmentConfirmPayload",
        ):
            self.assertNotIn(needle, main)
        for needle in (
            "CREATE TABLE IF NOT EXISTS rfid_enrollment_sessions",
            "CREATE TABLE IF NOT EXISTS rfid_replacement_requests",
            "def rfid_enrollment_charge_points(",
            "def start_rfid_enrollment(",
            "def capture_rfid_enrollment_candidate(",
            "def rfid_enrollment_status(",
            "def confirm_rfid_enrollment(",
            "def cancel_rfid_enrollment(",
            "def create_rfid_replacement_request(",
            "rfid_self_enroll_mode",
        ):
            self.assertNotIn(needle, db)
        self.assertIn('DROP TABLE IF EXISTS rfid_enrollment_sessions', db)
        self.assertIn('DROP TABLE IF EXISTS rfid_replacement_requests', db)


    def test_engagement_bonus_and_rankings_are_removed(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        db = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
        base = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")

        for needle in (
            "/engagement",
            "AchievementPayload",
            "BonusGrantPayload",
            "BonusVoucherPayload",
            "/api/settings/bonus-policy",
            "/api/settings/portal-leaderboard-names",
        ):
            self.assertNotIn(needle, main)

        for needle in (
            "CREATE TABLE IF NOT EXISTS achievements",
            "CREATE TABLE IF NOT EXISTS gamification_events",
            "CREATE TABLE IF NOT EXISTS bonus_grants",
            "def evaluate_user_achievements(",
            "def bonus_wallet(",
            "def general_leaderboard(",
            "def seed_default_achievements(",
            "_allocate_bonus_for_transaction_conn(",
            "_bonus_wallet_conn(",
            "ACHIEVEMENT_METRICS",
            "LEADERBOARD_METRIC_META",
            '"voucher_prefix":"VOLT"',
        ):
            self.assertNotIn(needle, db)

        for table in (
            "achievements",
            "achievement_awards",
            "gamification_events",
            "gamification_event_rewards",
            "gamification_event_results",
            "bonus_vouchers",
            "bonus_grants",
            "bonus_usage",
            "bonus_voucher_redemptions",
            "bonus_transfers",
        ):
            self.assertIn(f'"{table}"', db)

        self.assertNotIn('href="/engagement"', base)
        self.assertFalse((ROOT / "app" / "templates" / "engagement.html").exists())

if __name__ == "__main__":
    unittest.main()
