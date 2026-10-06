from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CommunityBackendOnlyScopeTests(unittest.TestCase):
    def test_public_charging_surfaces_are_not_shipped(self):
        removed = (
            "app/templates/public_budgets.html",
            "app/templates/portal_pin_reset.html",
            "app/templates/access_request.html",
            "app/templates/access_requests.html",
            "app/templates/access_request_detail.html",
            "app/templates/registration_settings.html",
        )
        for rel in removed:
            self.assertFalse((ROOT / rel).exists(), rel)

    def test_non_community_modules_are_not_shipped(self):
        removed = (
            "app/templates/engagement.html",
            "app/templates/load_management.html",
            "app/templates/cost_centers.html",
            "app/templates/imports.html",
            "app/ladecloud_import.py",
        )
        for rel in removed:
            self.assertFalse((ROOT / rel).exists(), rel)

    def test_removed_routes_are_not_exposed(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        forbidden = (
            "/public/ladeguthaben",
            "/public/access-request",
            "/registration-onboarding",
            "/registration-requests",
            "/api/portal-admin",
            "/api/engagement",
            "/api/import/ladecloud",
            "/api/integrations/fleet",
            "/api/smart-charging",
            "/api/cost-centers",
            "/api/rfid/requests",
        )
        for route in forbidden:
            self.assertNotIn(route, main, route)

    def test_backend_ui_has_no_personal_portal_links(self):
        for rel in (
            "app/templates/base.html",
            "app/templates/login.html",
            "app/templates/users.html",
            "app/templates/charge_points.html",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8").lower()
            for needle in (
                "/public/ladeguthaben",
                "/public/access-request",
                "persönlicher ladeportal",
                "rfid-self-service",
                "rfid self-service",
            ):
                self.assertNotIn(needle, text, f"{rel}: {needle}")


if __name__ == "__main__":
    unittest.main()
