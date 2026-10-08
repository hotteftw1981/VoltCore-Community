import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import db, migration_package


class MigrationPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="voltcore-migration-")
        self.old_data_dir = db.DATA_DIR
        self.old_db_path = db.DB_PATH
        self._use_db(Path(self.tmp.name) / "source")

    def tearDown(self):
        db.DATA_DIR = self.old_data_dir
        db.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _use_db(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        db.DATA_DIR = folder
        db.DB_PATH = folder / "ocpp.sqlite3"
        db.init_db()

    def _seed_source(self):
        with db._lock, db._connect() as conn:
            cp_columns={row[1] for row in conn.execute("PRAGMA table_info(charge_points)").fetchall()}
            cp_values={
                "id":"MIG-CP-1","name":"Migration Wallbox","vendor":"QA","model":"Model",
                "status":"Available","connector_count":1,"max_power_kw":22.0,
                "onboarded":1,"ignored":0,"simulated":0,
            }
            cp_values={k:v for k,v in cp_values.items() if k in cp_columns}
            conn.execute(
                f"INSERT INTO charge_points({','.join(cp_values)}) VALUES({','.join('?' for _ in cp_values)})",
                list(cp_values.values()),
            )

            user_columns={row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            user_values={"id":11,"name":"Patrick Migration","email":"patrick@example.org","phone":"012345","status":"Aktiv","charge_access_mode":"all"}
            user_values={k:v for k,v in user_values.items() if k in user_columns}
            conn.execute(
                f"INSERT INTO users({','.join(user_values)}) VALUES({','.join('?' for _ in user_values)})",
                list(user_values.values()),
            )

            vehicle_columns={row[1] for row in conn.execute("PRAGMA table_info(vehicles)").fetchall()}
            vehicle_values={"id":21,"name":"Migration EV","make":"Test","model":"EV","plate":"EN-MI 1","active":1}
            vehicle_values={k:v for k,v in vehicle_values.items() if k in vehicle_columns}
            conn.execute(
                f"INSERT INTO vehicles({','.join(vehicle_values)}) VALUES({','.join('?' for _ in vehicle_values)})",
                list(vehicle_values.values()),
            )

            rfid_columns={row[1] for row in conn.execute("PRAGMA table_info(rfid_cards)").fetchall()}
            rfid_values={"id":31,"uid":"MIG-RFID-1","label":"Migration RFID","user_id":11,"vehicle_id":21,"status":"Aktiv","created_at":"2026-10-08T08:00:00+00:00"}
            if "issued_at" in rfid_columns:
                rfid_values["issued_at"]="2026-10-08T08:00:00+00:00"
            rfid_values={k:v for k,v in rfid_values.items() if k in rfid_columns}
            conn.execute(
                f"INSERT INTO rfid_cards({','.join(rfid_values)}) VALUES({','.join('?' for _ in rfid_values)})",
                list(rfid_values.values()),
            )

            if "user_vehicles" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
                conn.execute(
                    "INSERT INTO user_vehicles(user_id,vehicle_id,primary_vehicle,assigned_at) VALUES(11,21,1,?)",
                    ("2026-10-08T08:00:00+00:00",),
                )

            tx_columns={row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
            tx_values={
                "id":41,"charge_point_id":"MIG-CP-1","connector_id":1,"transaction_id":"source-41",
                "id_tag":"MIG-RFID-1","user_id":11,"rfid_card_id":31,"vehicle_id":21,
                "started_at":"2026-10-08T08:00:00+00:00","ended_at":"2026-10-08T09:00:00+00:00",
                "energy_kwh":12.5,"max_power_kw":11.0,"status":"Completed",
                "stop_reason":"Imported",
            }
            tx_values={k:v for k,v in tx_values.items() if k in tx_columns}
            conn.execute(
                f"INSERT INTO transactions({','.join(tx_values)}) VALUES({','.join('?' for _ in tx_values)})",
                list(tx_values.values()),
            )
            conn.commit()

    def test_roundtrip_preserves_core_ids_and_relationships(self):
        self._seed_source()
        package, filename, manifest = migration_package.build_package("9.9.9","test",anonymize=False)
        self.assertTrue(filename.endswith(".zip"))
        self.assertEqual(migration_package.FORMAT, manifest["format"])

        with zipfile.ZipFile(io.BytesIO(package),"r") as zf:
            self.assertIn("manifest.json", zf.namelist())
            self.assertIn("data/users.csv", zf.namelist())
            self.assertIn("data/rfid_cards.csv", zf.namelist())
            loaded=json.loads(zf.read("manifest.json").decode("utf-8"))
            self.assertEqual(1, loaded["schema_version"])
            self.assertFalse(loaded["anonymized"])

        self._use_db(Path(self.tmp.name) / "target")
        preview=migration_package.preview_package(package,"test-target")
        self.assertTrue(preview["can_import"])
        result=migration_package.import_package(package,"test-target")
        self.assertTrue(result["ok"])

        with db._lock, db._connect() as conn:
            user=conn.execute("SELECT * FROM users WHERE id=11").fetchone()
            vehicle=conn.execute("SELECT * FROM vehicles WHERE id=21").fetchone()
            card=conn.execute("SELECT * FROM rfid_cards WHERE id=31").fetchone()
            tx=conn.execute("SELECT * FROM transactions WHERE id=41").fetchone()
            self.assertEqual("Patrick Migration", user["name"])
            self.assertEqual("EN-MI 1", vehicle["plate"])
            self.assertEqual(11, card["user_id"])
            self.assertEqual(21, card["vehicle_id"])
            self.assertEqual("MIG-RFID-1", tx["id_tag"])
            if "user_id" in tx.keys():
                self.assertEqual(11, tx["user_id"])
            if "vehicle_id" in tx.keys():
                self.assertEqual(21, tx["vehicle_id"])

    def test_anonymized_package_keeps_relations_but_removes_personal_values(self):
        self._seed_source()
        package, _, manifest=migration_package.build_package("9.9.9","test",anonymize=True)
        self.assertTrue(manifest["anonymized"])

        self._use_db(Path(self.tmp.name) / "anon-target")
        migration_package.import_package(package,"test-target")
        with db._lock, db._connect() as conn:
            user=conn.execute("SELECT * FROM users WHERE id=11").fetchone()
            vehicle=conn.execute("SELECT * FROM vehicles WHERE id=21").fetchone()
            card=conn.execute("SELECT * FROM rfid_cards WHERE id=31").fetchone()
            tx=conn.execute("SELECT * FROM transactions WHERE id=41").fetchone()
            self.assertNotEqual("Patrick Migration", user["name"])
            if "email" in user.keys():
                self.assertIsNone(user["email"])
            self.assertIsNone(vehicle["plate"])
            self.assertNotEqual("MIG-RFID-1", card["uid"])
            self.assertEqual(card["uid"], tx["id_tag"])
            self.assertEqual(11, card["user_id"])
            self.assertEqual(21, card["vehicle_id"])

    def test_direct_import_refuses_non_empty_operational_target(self):
        self._seed_source()
        package, _, _=migration_package.build_package("9.9.9","test",anonymize=False)

        self._use_db(Path(self.tmp.name) / "blocked-target")
        with db._lock, db._connect() as conn:
            columns={row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            values={"name":"Existing User","status":"Aktiv"}
            values={k:v for k,v in values.items() if k in columns}
            conn.execute(
                f"INSERT INTO users({','.join(values)}) VALUES({','.join('?' for _ in values)})",
                list(values.values()),
            )
            conn.commit()

        preview=migration_package.preview_package(package,"test-target")
        self.assertFalse(preview["can_import"])
        self.assertGreater(preview["blockers"].get("users",0),0)
        with self.assertRaises(ValueError):
            migration_package.import_package(package,"test-target")

    def test_manifest_checksum_tampering_is_rejected(self):
        self._seed_source()
        package, _, _=migration_package.build_package("9.9.9","test",anonymize=False)
        source=zipfile.ZipFile(io.BytesIO(package),"r")
        out=io.BytesIO()
        with source, zipfile.ZipFile(out,"w",compression=zipfile.ZIP_DEFLATED) as target:
            for name in source.namelist():
                payload=source.read(name)
                if name=="data/users.csv":
                    payload=payload+b"\ncorrupt"
                target.writestr(name,payload)
        with self.assertRaises(ValueError):
            migration_package.preview_package(out.getvalue(),"test-target")


if __name__ == "__main__":
    unittest.main()
