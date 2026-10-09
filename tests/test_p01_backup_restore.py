"""P01 isolated backup/restore regression tests.

All files, databases, backups and restore attempts stay inside a disposable
temporary directory. The expectedFailure tests document unfixed defects.
"""
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="voltcore-p01-backup-import-"))

from app import backup, db


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="voltcore-p01-backup-")
        self.addCleanup(self.sandbox.cleanup)
        self.root = Path(self.sandbox.name)
        replacements = {
            (db, "DATA_DIR"): self.root,
            (db, "DB_PATH"): self.root / "test.sqlite3",
            (backup, "BACKUP_DIR"): self.root / "backups",
            (backup, "CREDENTIAL_FILE"): self.root / ".backup_external_password",
            (backup, "SMTP_CREDENTIAL_FILE"): self.root / ".smtp_password",
            (backup, "SFTP_KNOWN_HOSTS_FILE"): self.root / ".backup_sftp_known_hosts",
        }
        for (module, key), value in replacements.items():
            context = patch.object(module, key, value)
            context.start()
            self.addCleanup(context.stop)
        db.init_db()

    def write_data(self, relative, content):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def archive_path(self, label="snapshot"):
        info = backup.create_backup(label=label)
        return backup.BACKUP_DIR / info["filename"]

    def fake_archive(self, members, manifest=None):
        path = self.root / "untrusted-upload.zip"
        metadata = {"format": 1, "product": "VoltCore", "database": "database/ocpp.sqlite3"}
        if manifest is not None:
            metadata.update(manifest)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as container:
            for name, value in members.items():
                container.writestr(name, value)
            if "manifest.json" not in members:
                container.writestr("manifest.json", json.dumps(metadata))
        return path

    def test_backup_snapshot_is_valid_and_excludes_credentials(self):
        db.set_setting("p01_backup_marker", "initial")
        self.write_data("media/p01.txt", "HELLO")
        self.write_data(".backup_external_password", "never-in-zip")
        self.write_data(".smtp_password", "never-in-zip")
        self.write_data(".update_github_token", "never-in-zip")
        path = self.archive_path()
        self.assertTrue(path.is_file())
        metadata = backup.validate_restore(path)
        self.assertEqual(int(metadata["format"]), 1)
        with zipfile.ZipFile(path) as container:
            names = container.namelist()
            self.assertIn("database/ocpp.sqlite3", names)
            self.assertIn("data/media/p01.txt", names)
            for name in (".backup_external_password", ".smtp_password", ".update_github_token"):
                self.assertNotIn("data/" + name, names)
            self.assertFalse(any(item.startswith("data/backups/") for item in names))

    def test_reject_path_traversal_before_any_safety_backup(self):
        archive = self.fake_archive({
            "database/ocpp.sqlite3": b"dummy",
            "data/../../p01-escape.txt": b"invalid",
        })
        with patch.object(backup, "create_backup", side_effect=AssertionError("Should not run")) as saving:
            with self.assertRaises(ValueError):
                backup.restore_backup(archive)
            saving.assert_not_called()
        self.assertFalse((self.root.parent / "p01-escape.txt").exists())

    def test_reject_missing_database_and_invalid_format(self):
        absent = self.fake_archive({"data/info.txt": b"test"})
        with self.assertRaises(ValueError):
            backup.validate_restore(absent)
        invalid = self.fake_archive({"database/ocpp.sqlite3": b"dummy"}, {"format": 999})
        with self.assertRaises(ValueError):
            backup.validate_restore(invalid)

    def test_reject_archive_above_uncompressed_limit(self):
        path = self.fake_archive({"database/ocpp.sqlite3": b"Z" * 8192})
        with patch.object(backup, "MAX_RESTORE_BYTES", 2048):
            with self.assertRaises(ValueError):
                backup.validate_restore(path)

    def test_backup_restores_database_and_media_on_success(self):
        db.set_setting("p01_restore_marker", "saved")
        attachment = self.write_data("media/p01-state.txt", "saved media")
        archive = self.archive_path("clean")
        db.set_setting("p01_restore_marker", "newer")
        attachment.write_text("newer media", encoding="utf-8")
        result = backup.restore_backup(archive)
        self.assertTrue(result["ok"])
        self.assertTrue(result["safety_backup"])
        self.assertEqual(db.get_setting("p01_restore_marker", ""), "saved")
        self.assertEqual(attachment.read_text(encoding="utf-8"), "saved media")

    def test_known_defect_failed_media_copy_must_rollback_database(self):
        db.set_setting("p01_restore_marker", "archived")
        attachment = self.write_data("media/p01-rollback.txt", "archived media")
        archive = self.archive_path("fault-injection")
        db.set_setting("p01_restore_marker", "current")
        attachment.write_text("current media", encoding="utf-8")
        with patch.object(backup.shutil, "copy2", side_effect=OSError("P01 injected copy failure")):
            with self.assertRaises(OSError):
                backup.restore_backup(archive)
        # Contract: a failed restore must leave BOTH the database and files unchanged.
        self.assertEqual(db.get_setting("p01_restore_marker", ""), "current")
        self.assertEqual(attachment.read_text(encoding="utf-8"), "current media")

    def test_known_community_import_sources_must_not_enter_backups(self):
        if not backup.BACKUP_PREFIX.startswith("voltcore-community-"):
            self.skipTest("Pro already excludes transient import files")
        self.write_data("imports/received.xlsx", "temporary upload")
        path = self.archive_path("import-check")
        with zipfile.ZipFile(path) as container:
            self.assertNotIn("data/imports/received.xlsx", container.namelist())


if __name__ == "__main__":
    unittest.main()
