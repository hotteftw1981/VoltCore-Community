"""P03 backend UI and REST contract smoke checks (both editions)."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "app/templates/users.html").read_text(encoding="utf-8")
API = (ROOT / "app/main.py").read_text(encoding="utf-8")


class ChargingLimitUISmoke(unittest.TestCase):
    def test_both_limit_fields_present(self):
        for field in ("p03UserLimit", "p03CardLimit"):
            self.assertIn('id="' + field + '"', TEMPLATE)
        self.assertIn("type=\"number\"", TEMPLATE)
        self.assertIn('max="100"', TEMPLATE)

    def test_modals_load_and_submit_limits(self):
        for field in ("p03UserLimit", "p03CardLimit"):
            self.assertIn(field, TEMPLATE)
        self.assertIn("p03Save('user'", TEMPLATE)
        self.assertIn("p03Save('rfid'", TEMPLATE)
        self.assertIn("p03Load('user'", TEMPLATE)
        self.assertIn("p03Load('rfid'", TEMPLATE)

    def test_admin_gate_on_put_endpoints(self):
        for name in ("api_p03_set_user_charging_limit", "api_p03_set_rfid_charging_limit"):
            start = API.index("async def " + name)
            stop = API.find("\n@app.", start + 1)
            body = API[start:stop if stop >= 0 else None]
            self.assertIn("_p03_require_admin(request)", body)
        self.assertIn('raise HTTPException(403, "Administratorberechtigung erforderlich")', API)

if __name__ == "__main__":
    unittest.main()
