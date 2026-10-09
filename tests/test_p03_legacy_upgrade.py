"""P03 upgrade contract: simulate a persisted database from before the new limit columns."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="p03-upgrade-import-"))
from app import db


class P03LegacySchemaUpgrade(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="p03-upgrade-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name, value in (("DATA_DIR", root), ("DB_PATH", root / "sqlite.db")):
            ctx = patch.object(db, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)
        db.init_db()
        db.upsert_charge_point("P03-LEGACY", status="Available", connector_count=1)
        db.discover_connector("P03-LEGACY", 1, status="Available")
        self.driver = db.create_user("Legacy Driver", gamification_enabled=False)
        self.card = db.create_rfid_card("P03-LEGACY-CARD", user_id=self.driver)
        self.transaction = db.start_transaction("P03-LEGACY", id_tag="P03-LEGACY-CARD", connector_id=1)

    def test_upgrade_legacy_columns_preserves_session_and_identity(self):
        # A pre-P03 schema has no max_concurrent_sessions in either entity table.
        # The new field is not referenced by old foreign key constraints.
        with db._connect() as conn:
            conn.execute("ALTER TABLE rfid_cards DROP COLUMN max_concurrent_sessions")
            conn.execute("ALTER TABLE users DROP COLUMN max_concurrent_sessions")
            conn.commit()
        db.init_db()
        with db._connect() as conn:
            for table in ("users", "rfid_cards"):
                columns = [row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")]
                self.assertIn("max_concurrent_sessions", columns)
        self.assertEqual(db.get_transaction(self.transaction)["status"], "Active")
        self.assertEqual(db.get_transaction(self.transaction)["user_id"], self.driver)
        self.assertEqual(db.charging_session_limit_snapshot("user", self.driver)["limit"], 1)
        self.assertEqual(db.charging_session_limit_snapshot("rfid", self.card)["limit"], 1)
        with self.assertRaisesRegex(ValueError, "RFID_CONCURRENT_SESSION_LIMIT"):
            db.start_transaction("P03-LEGACY", id_tag="P03-LEGACY-CARD", connector_id=2)

if __name__ == "__main__":
    unittest.main()
