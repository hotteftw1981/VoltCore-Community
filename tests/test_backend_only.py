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


    def test_advanced_enterprise_modules_are_removed(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        db = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
        base = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")
        reports = (ROOT / "app" / "templates" / "reports.html").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        for needle in (
            "/load-management",
            "/cost-centers",
            "/imports",
            "/api/import/ladecloud",
            "/api/integrations/fleet/",
            "/api/smart-charging/",
            "FLEET_INTEGRATION_TOKEN",
            "CostCenterPayload",
            "SmartChargingSettingsPayload",
        ):
            self.assertNotIn(needle, main)

        for needle in (
            "CREATE TABLE IF NOT EXISTS load_rules",
            "CREATE TABLE IF NOT EXISTS cost_centers",
            "def list_load_rules(",
            "def list_cost_centers(",
            "def import_ladecloud_rows(",
            "def import_history_stats(",
            "def fleet_integration_sessions(",
            "def _validate_cost_center_conn(",
        ):
            self.assertNotIn(needle, db)

        self.assertIn('DROP TABLE IF EXISTS load_rules', db)
        self.assertIn('DROP TABLE IF EXISTS cost_centers', db)
        self.assertNotIn("Kostenstelle", reports)
        self.assertNotIn('href="/load-management"', base)
        self.assertNotIn('href="/cost-centers"', base)
        self.assertNotIn('href="/imports"', base)
        self.assertNotIn("openpyxl", requirements)

        for relative in (
            "app/templates/load_management.html",
            "app/templates/cost_centers.html",
            "app/templates/imports.html",
            "app/ladecloud_import.py",
        ):
            self.assertFalse((ROOT / relative).exists())


    def test_backend_only_registration_and_employment_fields_are_removed(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        db = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
        users = (ROOT / "app" / "templates" / "users.html").read_text(encoding="utf-8")
        vehicles = (ROOT / "app" / "templates" / "vehicles.html").read_text(encoding="utf-8")
        login = (ROOT / "app" / "templates" / "login.html").read_text(encoding="utf-8")
        base = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")

        for needle in (
            "/public/access-request",
            "/registration-onboarding",
            "/registration-requests",
            "ACCESS_TERMS_VERSION",
            "ACCESS_SIGNATURE_DIR",
            "registration=db.registration_settings()",
            "role: str = \"Fahrer\"",
            "department: str | None",
            "driver: str | None",
        ):
            self.assertNotIn(needle, main)

        for needle in (
            "role TEXT NOT NULL DEFAULT 'Fahrer'",
            "department TEXT",
            "weekly_hours REAL",
            "budget_source TEXT NOT NULL DEFAULT 'manual'",
            "driver TEXT",
            "CREATE TABLE IF NOT EXISTS access_request_verifications",
            "CREATE TABLE IF NOT EXISTS access_requests",
            "def registration_settings(",
            "def create_access_request(",
            "def approve_access_request(",
            "def create_user(name,role",
            "_validate_registration",
        ):
            self.assertNotIn(needle, db)

        self.assertIn('DROP TABLE IF EXISTS access_request_verifications', db)
        self.assertIn('DROP TABLE IF EXISTS access_requests', db)
        self.assertIn('("users",("role","department","weekly_hours","budget_source"))', db)
        self.assertIn('("vehicles",("driver",))', db)

        for needle in ("Rolle", "Disposition", "Abteilung", "userRole", "department"):
            self.assertNotIn(needle, users)
        for needle in ("Fahrer", "driver", "department", "u.role"):
            self.assertNotIn(needle, vehicles)
        for needle in ("Zum Ladeportal", "/public/ladeguthaben", "/public/access-request", "persönliche PIN"):
            self.assertNotIn(needle, login)
        for needle in ("/registration-onboarding", "/registration-requests"):
            self.assertNotIn(needle, base)

        for relative in (
            "app/templates/access_request.html",
            "app/templates/registration_settings.html",
            "app/templates/access_requests.html",
            "app/templates/registration_request_detail.html",
        ):
            self.assertFalse((ROOT / relative).exists())


    def test_documentation_matches_backend_only_scope(self):
        readme_de = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_en = (ROOT / "README.en.md").read_text(encoding="utf-8")
        scope = (ROOT / "docs" / "COMMUNITY_SCOPE.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn("Backend-only", readme_de)
        self.assertIn("Backend-only", readme_en)
        self.assertIn("intentionally **backend-only**", scope)
        self.assertIn("0.9.7.76 — Backend-only Community scope", changelog)

        for stale in (
            "Public charging portal restored",
            "Public registration restored",
            "Restored Smart Charging, Engagement, Cost Centers",
            "The Community edition now includes additional neutral modules restored",
        ):
            self.assertNotIn(stale, scope)
            self.assertNotIn(stale, readme_de)
            self.assertNotIn(stale, readme_en)

if __name__ == "__main__":
    unittest.main()
