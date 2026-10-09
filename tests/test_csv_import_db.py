import os
import tempfile
import unittest
from pathlib import Path

from app import backup, csv_import, db


class NeutralCSVImportDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="voltcore-csv-import-")
        self.old_data_dir = db.DATA_DIR
        self.old_db_path = db.DB_PATH
        db.DATA_DIR = Path(self.tmp.name)
        db.DB_PATH = db.DATA_DIR / "ocpp.sqlite3"
        db.init_db()
        csv_import.ensure_schema()

    def tearDown(self):
        db.DATA_DIR = self.old_data_dir
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_master_data_session_and_duplicate_detection(self):
        cp = csv_import.execute(
            "charge_points",
            [{"row_no": 2, "charge_point_id": "CP-IMPORT-1", "name": "Import CP", "connector_count": 2, "max_power_kw": 22.0,
              "vendor": "", "model": "", "serial_number": "", "firmware": "", "location": ""}],
            provider="generic",
        )
        self.assertEqual(1, cp["imported"])

        user = csv_import.execute(
            "users",
            [{"row_no": 2, "name": "Max Import", "email": "max@example.org", "phone": "", "status": "Aktiv"}],
            provider="generic",
        )
        self.assertEqual(1, user["imported"])

        card = csv_import.execute(
            "rfids",
            [{"row_no": 2, "uid": "RFID-IMPORT-1", "label": "Test", "user_name": "Max Import",
              "user_email": "max@example.org", "vehicle_plate": "", "status": "Aktiv",
              "expires_at": None, "notes": ""}],
            provider="generic",
        )
        self.assertEqual(1, card["imported"])

        raw = [{
            "Start": "08.10.2026 08:00",
            "Ende": "08.10.2026 09:00",
            "kWh": "11,5",
            "CP": "CP-IMPORT-1",
            "RFID": "RFID-IMPORT-1",
            "Session": "external-123",
        }]
        rows, errors = csv_import.normalize_rows(
            "sessions",
            raw,
            {
                "started_at": "Start",
                "ended_at": "Ende",
                "energy_kwh": "kWh",
                "charge_point_id": "CP",
                "rfid_uid": "RFID",
                "external_id": "Session",
            },
            "Europe/Berlin",
        )
        self.assertEqual([], errors)

        first = csv_import.execute("sessions", rows, provider="generic")
        self.assertEqual(1, first["imported"])
        self.assertEqual(0, first["duplicates"])
        self.assertAlmostEqual(11.5, first["energy_kwh"], places=3)

        second = csv_import.execute("sessions", rows, provider="generic")
        self.assertEqual(0, second["imported"])
        self.assertEqual(1, second["duplicates"])

        with db._lock, db._connect() as conn:
            tx = conn.execute(
                "SELECT import_source,id_tag,user_id,energy_kwh,status FROM transactions WHERE import_source='csv:generic'"
            ).fetchone()
            self.assertIsNotNone(tx)
            self.assertEqual("RFID-IMPORT-1", tx["id_tag"])
            self.assertIsNotNone(tx["user_id"])
            self.assertAlmostEqual(11.5, tx["energy_kwh"], places=3)
            self.assertEqual("Completed", tx["status"])

    def test_temporary_csv_uploads_expire(self):
        token = csv_import.save_upload(b"name;email\nTest;test@example.org\n")
        path = csv_import._upload_path(token)
        self.assertTrue(path.exists())
        os.utime(path, (1, 1))
        csv_import._cleanup_uploads(max_age_seconds=60)
        self.assertFalse(path.exists())

    def test_temporary_imports_are_excluded_from_backups(self):
        folder = db.DATA_DIR / "imports"
        folder.mkdir(parents=True, exist_ok=True)
        sensitive = folder / "neutral-sensitive.csv"
        sensitive.write_text("name;rfid\nMax;SECRET\n", encoding="utf-8")
        persistent = {path.resolve() for path in backup._persistent_files()}
        self.assertNotIn(sensitive.resolve(), persistent)

    def test_session_can_create_missing_stammdaten_when_enabled(self):
        rows = [{
            "row_no": 2,
            "started_at": "2026-10-08T06:00:00+00:00",
            "ended_at": "2026-10-08T06:30:00+00:00",
            "duration_seconds": 1800,
            "energy_kwh": 5.0,
            "charge_point_id": "CP-AUTO-1",
            "connector_id": 1,
            "rfid_uid": "AUTO-RFID-1",
            "user_name": "Auto User",
            "user_email": "auto@example.org",
            "vehicle_name": "Auto EV",
            "vehicle_plate": "EN-AU 1",
            "max_power_kw": 11.0,
            "cost_eur": None,
            "price_eur_per_kwh": None,
            "external_id": "auto-session-1",
            "evse_id": "",
        }]
        result = csv_import.execute("sessions", rows, provider="reev", create_missing=True)
        self.assertEqual(1, result["imported"])
        self.assertEqual(1, result["created"]["charge_points"])
        self.assertEqual(1, result["created"]["users"])
        self.assertEqual(1, result["created"]["vehicles"])
        self.assertEqual(1, result["created"]["rfids"])


if __name__ == "__main__":
    unittest.main()
