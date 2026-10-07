"""UI contract checks for the backend-only Community edition."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CommunityUIContractTests(unittest.TestCase):
    def test_role_aware_shell_and_viewer_write_hiding(self):
        base = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")
        css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn('data-role="{{ auth_user.role if auth_user else \'public\' }}"', base)
        self.assertIn("auth_user.role == 'viewer'", base)
        self.assertIn("Nur-Lese-Zugang", base)
        self.assertIn('body[data-role="viewer"] .write-action{display:none!important}', css)
        self.assertIn('body[data-role="viewer"] .session-assignment{display:none!important}', css)

        for href in ("/tariffs", "/system-users", "/security", "/backups", "/updates", "/settings"):
            self.assertIn(f'href="{href}"', base)
        self.assertGreaterEqual(base.count("auth_user.role == 'admin'"), 6)

    def test_dark_mode_and_mobile_shell_stay_available(self):
        base = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")
        css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn("voltcore-community-theme", base)
        self.assertNotIn("drk-ocpp-theme", base)
        self.assertIn('html[data-theme="dark"]', css)
        self.assertIn("@media(max-width:700px)", css)
        self.assertIn("mobile-topbar", base)
        self.assertIn("mobile-menu-button", base)
        self.assertIn("mobile-nav-backdrop", base)
        self.assertIn(".mobile-topbar", css)
        self.assertIn(".mobile-menu-button", css)

    def test_visible_limit_wording_is_neutral(self):
        users = (ROOT / "app" / "templates" / "users.html").read_text(encoding="utf-8")
        dashboard = (ROOT / "app" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        db = (ROOT / "app" / "db.py").read_text(encoding="utf-8")

        self.assertIn("Monatliches Ladelimit", users)
        self.assertIn("Limits auffällig", dashboard)
        self.assertIn('action="Monatslimit geändert"', main)
        self.assertIn("Monatslimit vollständig erreicht", db)
        self.assertNotIn("Monatliches Ladebudget", users)
        self.assertNotIn("Budgets auffällig", dashboard)
        self.assertNotIn('action="Ladebudget geändert"', main)

    def test_charge_point_viewer_controls_are_read_only(self):
        detail = (ROOT / "app" / "templates" / "charge_point.html").read_text(encoding="utf-8")
        listing = (ROOT / "app" / "templates" / "charge_points.html").read_text(encoding="utf-8")

        self.assertIn('class="primary button write-action" href="/charge-points#discovery"', detail)
        self.assertIn('id="editMasterData" class="secondary button write-action"', detail)
        self.assertIn('id="saveSessionPolicy" class="primary write-action"', detail)
        self.assertIn("$('standGrace').disabled=!canWrite", detail)
        self.assertIn("$('autoStopZero').disabled=!canWrite", detail)
        self.assertIn("auth_user.role == 'admin'", detail)
        self.assertIn('data-tab="remote"', detail)

        self.assertIn("const canWrite=", listing)
        self.assertIn("function openEditor(v){if(!canWrite)return;", listing)
        self.assertIn("if(canWrite&&edit", listing)
        self.assertIn("function openOnboard(cp){if(!canWrite)return;", listing)
        self.assertIn("function openDeleteDevice(cp){if(!canWrite)return;", listing)


if __name__ == "__main__":
    unittest.main()
