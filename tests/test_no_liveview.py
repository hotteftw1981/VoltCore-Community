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

    def test_liveview_runtime_code_is_gone(self):
        allowed_suffixes = {".py", ".html", ".css", ".js", ".json"}
        offenders = []
        for path in (ROOT / "app").rglob("*"):
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            # db.py intentionally contains one migration cleanup for stale
            # 0.9.7.74 app_settings keys. It must not expose LiveView helpers.
            if path == ROOT / "app" / "db.py":
                continue
            text = path.read_text(encoding="utf-8").lower()
            if "liveview" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders, "LiveView runtime references remain")

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
        self.assertIn("DELETE FROM app_settings WHERE key LIKE 'liveview_%'", db)

    def test_public_feature_lists_do_not_advertise_liveview(self):
        for rel in ("README.md", "README.en.md"):
            text = (ROOT / rel).read_text(encoding="utf-8").lower()
            self.assertNotIn("liveview", text, rel)

        scope = (ROOT / "docs" / "COMMUNITY_SCOPE.md").read_text(encoding="utf-8")
        self.assertIn("LiveView / Kiosk", scope)
        self.assertIn("Explicitly not part of VoltCore Community", scope)


if __name__ == "__main__":
    unittest.main()
