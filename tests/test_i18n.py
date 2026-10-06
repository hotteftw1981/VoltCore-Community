import unittest

from app import i18n


class I18nTests(unittest.TestCase):
    def setUp(self):
        i18n.clear_locale_cache()

    def test_normalize_language(self):
        self.assertEqual(i18n.normalize_language("de-DE"), "de")
        self.assertEqual(i18n.normalize_language("EN_us"), "en")
        self.assertIsNone(i18n.normalize_language("fr-FR"))

    def test_accept_language_quality(self):
        header = "fr-FR, de-DE;q=0.8, en-US;q=0.9"
        self.assertEqual(i18n.language_from_accept_header(header), "en")

    def test_accept_language_order_breaks_equal_quality(self):
        self.assertEqual(i18n.language_from_accept_header("de-DE,en-US"), "de")

    def test_manual_language_wins(self):
        self.assertEqual(
            i18n.resolve_language(explicit="de", accept_language="en-US"),
            "de",
        )

    def test_english_is_fallback(self):
        self.assertEqual(
            i18n.resolve_language(explicit="fr", accept_language="fr-FR"),
            "en",
        )

    def test_translation(self):
        self.assertEqual(i18n.translate("nav.charge_points", "de"), "Ladepunkte")
        self.assertEqual(i18n.translate("nav.charge_points", "en"), "Charge points")

    def test_missing_key_returns_key(self):
        self.assertEqual(i18n.translate("missing.key", "de"), "missing.key")


if __name__ == "__main__":
    unittest.main()
