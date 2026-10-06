from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CommunityNoLiveViewTests(unittest.TestCase):
    def test_liveview_template_is_not_shipped(self):
        self.assertFalse((ROOT / "app" / "templates" / "liveview.html").exists())

    def test_liveview_routes_are_not_exposed(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        for needle in (
            '@app.get("/liveview"',
            '@app.get("/api/liveview"',
            '@app.get("/api/liveview/history/',
            '@app.get("/api/settings/liveview"',
            '@app.put("/api/settings/liveview"',
            "LiveviewSettingsPayload",
            "_liveview_snapshot",
        ):
            self.assertNotIn(needle, main)

    def test_liveview_ui_is_not_present(self):
        for rel in (
            "app/templates/base.html",
            "app/templates/settings.html",
            "app/static/style.css",
            "README.md",
            "README.en.md",
            "docs/COMMUNITY_SCOPE.md",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8").lower()
            self.assertNotIn("liveview", text, rel)

    def test_liveview_database_helpers_are_removed(self):
        db = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
        for needle in (
            "LIVEVIEW_PRESETS",
            "LIVEVIEW_SORTS",
            "LIVEVIEW_COLUMNS",
            "def liveview_settings(",
            "def save_liveview_settings(",
        ):
            self.assertNotIn(needle, db)


if __name__ == "__main__":
    unittest.main()
