import unittest
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / "app" / "static" / "branding"


class BrandingAssetTests(unittest.TestCase):
    def test_expected_brand_assets_exist(self):
        expected = [
            BRANDING / "voltcore-community-horizontal.svg",
            BRANDING / "voltcore-community-horizontal-dark.svg",
            BRANDING / "voltcore-community-stacked.svg",
            BRANDING / "voltcore-community-icon.svg",
            BRANDING / "voltcore-community-app-icon.svg",
            ROOT / "app" / "static" / "favicon.svg",
        ]
        for path in expected:
            self.assertTrue(path.is_file(), f"Missing branding asset: {path}")

    def test_svg_assets_are_valid_xml(self):
        for path in list(BRANDING.glob("*.svg")) + [ROOT / "app" / "static" / "favicon.svg"]:
            root = ET.parse(path).getroot()
            self.assertTrue(root.tag.endswith("svg"), f"Not an SVG root: {path}")

    def test_primary_logo_contains_community_edition_label(self):
        content = (BRANDING / "voltcore-community-horizontal.svg").read_text(encoding="utf-8")
        self.assertIn("COMMUNITY EDITION", content)
        self.assertIn("Volt", content)
        self.assertIn("Core", content)


if __name__ == "__main__":
    unittest.main()
