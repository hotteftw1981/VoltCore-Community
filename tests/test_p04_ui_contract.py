"""P04 RFID sync diagnosis HTML and API contract guards."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class P04UI(unittest.TestCase):
    def test_diagnostic_api_and_ui_connected(self):
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        ui = (ROOT / "app/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("states=db.local_list_diagnostics()", main)
        self.assertIn("sync_diagnostic", ui)
        for code in ("unsupported", "unknown", "out_of_sync", "station_ahead", "needs_verification", "version_match"):
            self.assertIn(code, ui)

    def test_does_not_invent_station_count(self):
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        segment = main.split('@app.get("/api/rfid/local-list")', 1)[1].split('@app.post(', 1)[0]
        self.assertNotIn('item["station_entry_count"]=item["authorized_count"]', segment)

if __name__ == "__main__":
    unittest.main()
