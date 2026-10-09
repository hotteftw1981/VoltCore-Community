"""Community cleanup regression tests; only temporary databases are used."""
import ast
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="community-cleanup-import-"))
from app import db
class CommunityLegacyRuntime(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="community-cleanup-test-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name, value in (("DATA_DIR", root), ("DB_PATH", root / "test.sqlite3")):
            swap = patch.object(db, name, value)
            swap.start()
            self.addCleanup(swap.stop)
        db.init_db()
        db.upsert_charge_point("CLEANUP-CP", status="Available", connector_count=1)
        db.discover_connector("CLEANUP-CP", 1, status="Available")
        self.user = db.create_user("Cleanup Driver", monthly_kwh_limit=1, monthly_limit_mode="block")
        db.create_rfid_card("CLEANUP-CARD", user_id=self.user)
        now = datetime.now(timezone.utc)
        with db._connect() as conn:
            conn.execute("INSERT INTO bonus_grants(user_id,amount_kwh,remaining_kwh,granted_at,expires_at) VALUES(?,100,100,?,?)", (self.user, (now-timedelta(days=1)).isoformat(), (now+timedelta(days=30)).isoformat()))
            conn.commit()
    def finish_session(self):
        tx = db.start_transaction("CLEANUP-CP", id_tag="CLEANUP-CARD", connector_id=1, meter_start_kwh=10)
        with db._connect() as conn:
            conn.execute("UPDATE transactions SET price_cents_per_kwh=30 WHERE id=?", (tx,))
            conn.commit()
        db.stop_transaction(tx, meter_stop_kwh=12)
        return tx
    def test_pro_runtime_functions_are_physically_absent(self):
        tree = ast.parse(Path(db.__file__).read_text(encoding="utf-8"))
        banned = ("portal_", "_portal_", "achievement", "gamification", "bonus_", "_bonus_", "leaderboard", "_xp_")
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.assertFalse(any(word in node.name for word in banned), node.name)
        self.assertFalse(hasattr(db, "public_charging_budgets"))
    def test_old_bonus_cannot_override_rfid_local_list_block(self):
        self.finish_session()
        self.assertNotIn("CLEANUP-CARD", [entry["idTag"] for entry in db.rfid_local_list_full()])
        self.assertFalse(db.authorization_decision("CLEANUP-CARD", "CLEANUP-CP")["accepted"])
    def test_new_session_cost_ignores_legacy_bonus(self):
        tx = self.finish_session()
        self.assertEqual(db.get_transaction(tx)["cost_cents"], 30)
    def test_budget_warning_does_not_advertise_a_bonus(self):
        self.finish_session()
        db.sync_notifications()
        with db._connect() as conn:
            rows = conn.execute("SELECT title,severity FROM notifications WHERE title LIKE 'Monats%' ").fetchall()
        self.assertTrue(rows)
        self.assertTrue(any("gesperrt" in row["title"] for row in rows))
        self.assertFalse(any("Bonus" in row["title"] for row in rows))
    def test_initialization_preserves_history_without_reactivation(self):
        tx = self.finish_session()
        before = db.get_transaction(tx)
        db.init_db()
        self.assertEqual(db.get_transaction(tx), before)
        with db._connect() as conn:
            self.assertEqual(conn.execute("SELECT remaining_kwh FROM bonus_grants WHERE user_id=?", (self.user,)).fetchone()[0], 100)
            self.assertEqual(tuple(conn.execute("SELECT portal_enabled,gamification_enabled FROM users WHERE id=?", (self.user,)).fetchone()), (0, 0))
    def test_legacy_import_has_no_missing_achievement_callback(self):
        now = db.utc_now()
        result = db.import_ladecloud_rows([{"rfid_tag": "CLEANUP-CARD", "source_charge_point": "Imported CP", "import_key": "cleanup-import-test", "started_at": now, "ended_at": now, "energy_kwh": 1, "duration_seconds": 60}], {"CLEANUP-CARD": self.user}, {"Imported CP": "CLEANUP-CP"})
        self.assertEqual(result["imported"], 1)
    def test_occupancy_finalization_has_no_achievement_callback(self):
        tx = self.finish_session()
        now = db.utc_now()
        with db._connect() as conn:
            conn.execute("UPDATE transactions SET post_session_occupied_started_at=? WHERE id=?", (now, tx))
            conn.commit()
        result = db.finalize_post_session_occupancy("CLEANUP-CP", 1, now)
        self.assertEqual(result["transaction_id"], tx)
if __name__ == "__main__":
    unittest.main()
