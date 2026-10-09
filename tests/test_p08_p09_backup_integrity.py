"""P08/P09 backup integrity and user-facing validity flags."""
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from app import backup

class BackupValidation(unittest.TestCase):
    def create_archive(self,root,checksum=True,duplicate=False):
        path=root/"test.zip"
        database=b"SQLite format 3\\x00"+b"test data"
        manifest={"format":1,"database":"database/ocpp.sqlite3"}
        if checksum:manifest["database_sha256"]=hashlib.sha256(database).hexdigest()
        with zipfile.ZipFile(path,"w") as z:
            z.writestr("database/ocpp.sqlite3",database)
            z.writestr("manifest.json",json.dumps(manifest))
            if duplicate:z.writestr("database/ocpp.sqlite3",database)
        return path

    def test_valid_archive_flag(self):
        with tempfile.TemporaryDirectory() as td:
            p=self.create_archive(Path(td))
            self.assertTrue(backup._archive_integrity_ok(p))
            self.assertTrue(backup.backup_info(p)["valid"])

    def test_bad_checksum_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=self.create_archive(Path(td))
            with zipfile.ZipFile(p,"a") as z:
                pass
            with zipfile.ZipFile(p,"w") as z:
                z.writestr("database/ocpp.sqlite3",b"malformed")
                z.writestr("manifest.json",json.dumps({"format":1,"database":"database/ocpp.sqlite3","database_sha256":"0"*64}))
            self.assertFalse(backup._archive_integrity_ok(p))
            with self.assertRaises(ValueError):
                backup.validate_restore(p)

    def test_legacy_archive_without_checksum(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(backup._archive_integrity_ok(self.create_archive(Path(td),checksum=False)))

    def test_duplicate_paths_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(backup._archive_integrity_ok(self.create_archive(Path(td),duplicate=True)))

    def test_corrupted_zip_marked_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"bad.zip";p.write_bytes(b"not a zip")
            self.assertFalse(backup.backup_info(p)["valid"])

if __name__=="__main__":
    unittest.main()
