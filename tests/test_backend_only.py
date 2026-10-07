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
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        version_line = next((line for line in main.splitlines() if line.startswith("APP_VERSION = ")), None)
        self.assertIsNotNone(version_line)
        version = version_line.split("=", 1)[1].strip().strip("\"'")
        self.assertIn(version, readme_de)
        self.assertIn(version, readme_en)
        self.assertIn(f"## {version}", changelog)

        for stale in (
            "Public charging portal restored",
            "Public registration restored",
            "Restored Smart Charging, Engagement, Cost Centers",
            "The Community edition now includes additional neutral modules restored",
        ):
            self.assertNotIn(stale, scope)
            self.assertNotIn(stale, readme_de)
            self.assertNotIn(stale, readme_en)
            self.assertNotIn(stale, changelog)


    def test_technical_surfaces_have_no_removed_feature_zombies(self):
        css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
        info = (ROOT / "app" / "templates" / "_info_modal.html").read_text(encoding="utf-8")
        first_run = (ROOT / "app" / "templates" / "first_run.html").read_text(encoding="utf-8")
        login_2fa = (ROOT / "app" / "templates" / "login_2fa.html").read_text(encoding="utf-8")
        charge_points = (ROOT / "app" / "templates" / "charge_points.html").read_text(encoding="utf-8")
        charge_point = (ROOT / "app" / "templates" / "charge_point.html").read_text(encoding="utf-8")
        settings = (ROOT / "app" / "templates" / "settings.html").read_text(encoding="utf-8")
        releases = (ROOT / "docs" / "RELEASES.md").read_text(encoding="utf-8")

        self.assertFalse((ROOT / "app" / "templates" / "access_request_detail.html").exists())
        self.assertEqual(css.count("{"), css.count("}"))

        for needle in (
            "portal-", "engagement-", "achievement-", "leaderboard",
            "voucher-", "smart-", "cost-center", "import-",
            "registration-", "access-request", "public-budget",
        ):
            self.assertNotIn(needle, css)

        for needle in ("DRK Ortsverein", "drk-schwelm", "Am Ochsenkamp"):
            self.assertNotIn(needle, info)

        self.assertIn("Optionales Monatslimit", first_run)
        for needle in ("Freies Ladeguthaben", "Wochenarbeitszeit", "Arbeitgeberlogik"):
            self.assertNotIn(needle, first_run)

        self.assertIn("voltcore-community-theme", login_2fa)
        self.assertNotIn("drk-ocpp-theme", login_2fa)

        for needle in ("RFID Self-Service", "rfidSelfEnroll", "rfid_self_enroll_mode"):
            self.assertNotIn(needle, charge_points)
        self.assertNotIn("smart_charging", charge_point)

        for needle in ("event_rfid_requests", "event_pin_reset_admin", "event_access_requests"):
            self.assertNotIn(needle, settings)

        self.assertIn("ghcr.io/hotteftw1981/voltcore-community:latest", releases)
        self.assertNotIn("ghcr.io/hotteftw1981/drk-ocpp-backend", releases)
        self.assertNotIn("ghcr.io/hotteftw1981/voltcore:latest", releases)


    def test_release_pipeline_is_gated_end_to_end(self):
        workflows = ROOT / ".github" / "workflows"
        ci = (workflows / "ci.yml").read_text(encoding="utf-8")
        container = (workflows / "container.yml").read_text(encoding="utf-8")
        release = (workflows / "release.yml").read_text(encoding="utf-8")
        package_qa = (workflows / "community-qa.yml").read_text(encoding="utf-8")
        runtime_qa = (workflows / "community-runtime-qa.yml").read_text(encoding="utf-8")

        self.assertIn("scripts/smoke_community_http.py --phase initial", ci)
        self.assertIn("scripts/smoke_community_http.py --phase restart", ci)
        self.assertIn("python scripts/build_release.py --dist dist", ci)
        self.assertIn("-p 127.0.0.1:18010:8000", ci)
        self.assertIn('workflows: ["Community CI"]', container)
        self.assertIn('workflows: ["Community Container"]', release)
        self.assertIn('gh release view "$RELEASE_TAG"', container)
        self.assertIn("Bump APP_VERSION", container)
        self.assertIn('gh release view "$RELEASE_TAG"', release)
        self.assertIn("Bump APP_VERSION", release)
        self.assertNotIn("steps.existing.outputs.exists", release)
        self.assertNotIn("refactor/community-full-runtime-base", package_qa)

        for workflow in (ci, container, release, package_qa, runtime_qa):
            self.assertNotIn("actions/checkout@v4", workflow)
        self.assertIn("actions/checkout@v7", ci)
        self.assertIn("actions/upload-artifact@v7", runtime_qa)


    def test_runtime_dead_code_and_default_limit_wiring(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        db = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
        ocpp = (ROOT / "app" / "ocpp_server.py").read_text(encoding="utf-8")
        users = (ROOT / "app" / "templates" / "users.html").read_text(encoding="utf-8")
        first_run = (ROOT / "app" / "templates" / "first_run.html").read_text(encoding="utf-8")

        for needle in (
            "BackgroundTasks",
            "import base64",
            "import binascii",
            "PILImage",
            "def _normalize_signature_png(",
            "def _save_access_signature(",
            "free_credit_enabled",
        ):
            self.assertNotIn(needle, main)

        for needle in (
            "def normalize_vehicle_plate(",
            "def vehicle_plate_key(",
            "def is_known_charge_point(",
            "def update_latest_service_operation(",
            "def local_list_uids_for_user(",
            "def get_user_by_email(",
            "def user_analytics(",
            "_PORTAL_MONTH_NAMES",
            "def _portal_period_context(",
            "def _portal_period_summary_conn(",
            "def primary_vehicle_for_user(",
            "def list_users(",
            "def get_tariff(",
            "def resolve_tariff(",
            "def billing_groups_for_user(",
            "def _setting_int(",
            "def active_rfid_count(",
            "def user_has_active_rfid(",
            "def _report_row_amounts(",
            "def recent_security_events(",
        ):
            self.assertNotIn(needle, db)

        self.assertIn("community_default_monthly_limit_enabled", main)
        self.assertIn("community_default_monthly_kwh", main)
        self.assertIn("payload.model_fields_set", main)
        self.assertIn('"defaults":{"monthly_kwh_limit":default_limit', main)
        self.assertIn("default_monthly_limit_enabled", first_run)
        self.assertNotIn("free_credit_enabled", first_run)
        self.assertIn("userDefaults", users)
        self.assertIn("userDefaults.monthly_kwh_limit", users)

        # Keep backward compatibility for Community RC databases, then remove
        # the obsolete setting name from persistent state.
        self.assertIn("community_free_credit_enabled", db)
        self.assertIn("DELETE FROM app_settings WHERE key='community_free_credit_enabled'", db)

        # Offline LocalList authorization must be re-evaluated on month change.
        self.assertIn("def refresh_local_list_for_month(", db)
        self.assertIn("if db.refresh_local_list_for_month():", ocpp)
        self.assertIn('sync_pending_local_lists(reason="Monatswechsel Ladelimit")', ocpp)
        dashboard = (ROOT / "app" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("Restbudget", dashboard)
        self.assertNotIn("Unbegrenztes Monatsbudget", dashboard)
        self.assertIn("Verbleibendes Limit", dashboard)

if __name__ == "__main__":
    unittest.main()
