import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallationDocumentationTests(unittest.TestCase):
    def _read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_installation_guides_exist(self):
        self.assertTrue((ROOT / "docs" / "INSTALLATION.md").is_file())
        self.assertTrue((ROOT / "docs" / "INSTALLATION.en.md").is_file())
        self.assertTrue((ROOT / ".env.example").is_file())

    def test_readmes_link_installation_guides(self):
        de = self._read("README.md")
        en = self._read("README.en.md")
        self.assertIn("docs/INSTALLATION.md", de)
        self.assertIn("docs/INSTALLATION.en.md", en)

    def test_guides_use_community_identity(self):
        combined = self._read("docs/INSTALLATION.md") + self._read("docs/INSTALLATION.en.md")
        self.assertIn("voltcore-community", combined)
        self.assertIn("ghcr.io/hotteftw1981/voltcore-community:latest", combined)
        self.assertNotIn("drk-ocpp-backend", combined)
        self.assertNotIn("ghcr.io/hotteftw1981/voltcore:latest", combined)

    def test_both_official_paths_are_documented(self):
        de = self._read("docs/INSTALLATION.md")
        self.assertIn("Docker Compose", de)
        self.assertIn("Portainer", de)
        self.assertIn("GitHub Releases", de)


if __name__ == "__main__":
    unittest.main()
