import asyncio
import os
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

    def test_registration_is_opt_in_and_has_no_weekly_hours(self):
        cfg = db.registration_settings()
        self.assertFalse(cfg["enabled"])
        self.assertNotIn("weekly_hours", {field["id"] for field in cfg["fields"]})


if __name__ == "__main__":
    unittest.main()
