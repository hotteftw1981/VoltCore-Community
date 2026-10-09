"""P10/P11 regression: locale selection and security headers remain present."""
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MAIN=(ROOT/"app/main.py").read_text(encoding="utf-8")
BASE=(ROOT/"app/templates/base.html").read_text(encoding="utf-8")

class P10P11Contracts(unittest.TestCase):
    def test_layout_uses_dynamic_language(self):
        self.assertIn('lang="{{ lang',BASE)
    def test_supported_locales(self):
        self.assertIn('("de", "en")',MAIN)
        self.assertIn('context.setdefault("lang", locale)',MAIN)
        self.assertIn('accept-language',MAIN)
    def test_security_headers_still_present(self):
        for header in ("Content-Security-Policy","Strict-Transport-Security","Cache-Control","X-Content-Type-Options"):
            self.assertIn(header,MAIN)
    def test_api_auth_and_admin_scopes(self):
        self.assertIn('Administratorrechte erforderlich',MAIN)

if __name__=="__main__":
    unittest.main()
