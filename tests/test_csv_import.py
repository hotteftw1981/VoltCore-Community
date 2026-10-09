import unittest

from app import csv_import


class NeutralCSVImportTests(unittest.TestCase):
    def test_semicolon_cp1252_csv_is_detected_and_mapped(self):
        data = "Name;E-Mail;Telefon\nMüller;mueller@example.org;0123\n".encode("cp1252")
        parsed = csv_import.parse_csv_bytes(data)
        self.assertEqual(";", parsed["delimiter"])
        self.assertEqual(1, len(parsed["rows"]))
        mapping = csv_import.suggest_mapping("users", parsed["headers"])
        self.assertEqual("Name", mapping["name"])
        self.assertEqual("E-Mail", mapping["email"])

    def test_session_normalization_supports_decimal_comma_and_duration(self):
        raw = [{
            "Start": "08.10.2026 08:00",
            "Dauer": "01:30:00",
            "kWh": "12,345",
            "Station ID": "CP-1",
            "RFID": "ABC123",
        }]
        rows, errors = csv_import.normalize_rows(
            "sessions",
            raw,
            {
                "started_at": "Start",
                "duration_seconds": "Dauer",
                "energy_kwh": "kWh",
                "charge_point_id": "Station ID",
                "rfid_uid": "RFID",
            },
            "Europe/Berlin",
        )
        self.assertEqual([], errors)
        self.assertEqual(1, len(rows))
        self.assertEqual(5400, rows[0]["duration_seconds"])
        self.assertAlmostEqual(12.345, rows[0]["energy_kwh"], places=3)
        self.assertEqual("CP-1", rows[0]["charge_point_id"])
        self.assertTrue(rows[0]["ended_at"])

    def test_invalid_session_requires_end_or_duration(self):
        rows, errors = csv_import.normalize_rows(
            "sessions",
            [{"Start": "08.10.2026 08:00", "kWh": "1.2", "CP": "CP-1"}],
            {"started_at": "Start", "energy_kwh": "kWh", "charge_point_id": "CP"},
            "UTC",
        )
        self.assertEqual([], rows)
        self.assertEqual(1, len(errors))
        self.assertIn("Endzeit oder Dauer", errors[0]["message"])

    def test_provider_source_is_namespaced(self):
        self.assertEqual("csv:reev", csv_import._source("reev"))
        self.assertEqual("csv:generic", csv_import._source("generic"))


if __name__ == "__main__":
    unittest.main()
