import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OcppDocumentationTests(unittest.TestCase):
    def _read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_ocpp_guides_exist(self):
        self.assertTrue((ROOT / "docs" / "OCPP_CONNECTION.md").is_file())
        self.assertTrue((ROOT / "docs" / "OCPP_CONNECTION.en.md").is_file())

    def test_docs_show_expected_url_shape(self):
        de = self._read("docs/OCPP_CONNECTION.md")
        self.assertIn("ws://192.168.1.50:9000/AMTRON-01", de)
        self.assertIn("wss://192.168.1.50:9000/AMTRON-01", de)
        self.assertIn("ocpp1.6", de)

    def test_docs_match_charge_point_id_rules(self):
        de = self._read("docs/OCPP_CONNECTION.md")
        self.assertIn("128", de)
        self.assertIn("Nicht verwenden:", de)

    def test_installation_docs_link_ocpp_guides(self):
        self.assertIn("OCPP_CONNECTION.md", self._read("docs/INSTALLATION.md"))
        self.assertIn("OCPP_CONNECTION.en.md", self._read("docs/INSTALLATION.en.md"))

    def test_readmes_link_ocpp_guides(self):
        self.assertIn("docs/OCPP_CONNECTION.md", self._read("README.md"))
        self.assertIn("docs/OCPP_CONNECTION.en.md", self._read("README.en.md"))


if __name__ == "__main__":
    unittest.main()
