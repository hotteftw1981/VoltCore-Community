"""Exercise the real billing function with SQLite, without starting OCPP servers.

AST extraction intentionally avoids importing the entire application. The
function body comes from app/db.py, not from a second test implementation.
"""
import ast
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sqlite3
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def load_billing_function():
    source = ROOT / "app" / "db.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef)
             and node.name == "_update_tx_cost_conn"]
    if len(nodes) != 1:
        raise AssertionError("Expected exactly one billing implementation")
    # Supply legacy date helpers so the old implementation fails on its
    # incorrect result, rather than an unrelated missing-name error.
    def parse_date(value):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def month_bounds(now):
        local = now.astimezone(ZoneInfo("Europe/Berlin"))
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        return (start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(), start.strftime("%Y-%m"))

    namespace = {"Decimal": Decimal, "ROUND_HALF_UP": ROUND_HALF_UP,
                 "datetime": datetime, "timezone": timezone,
                 "_parse_iso_utc": parse_date, "_month_bounds_utc": month_bounds}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["_update_tx_cost_conn"]


class CommunityBillingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calculate = staticmethod(load_billing_function())

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, monthly_kwh_limit REAL, rfid TEXT);
            CREATE TABLE rfid_cards(uid TEXT PRIMARY KEY, user_id INTEGER);
            CREATE TABLE tariffs(id INTEGER PRIMARY KEY, price_cents_per_kwh INTEGER);
            CREATE TABLE transactions(
                id INTEGER PRIMARY KEY, energy_kwh REAL, price_cents_per_kwh INTEGER,
                cost_cents INTEGER, user_id INTEGER, id_tag TEXT,
                started_at TEXT, ended_at TEXT, status TEXT);
            INSERT INTO users VALUES(1, NULL, 'legacy-card');
            INSERT INTO rfid_cards VALUES('card-1', 1);
            INSERT INTO tariffs VALUES(1, 35);
        """)

    def add_session(self, energy=10, price=35, user=1, tag=None, tx_id=1,
                    cost=None, status="Active"):
        self.conn.execute("""INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?)""",
                          (tx_id, energy, price, cost, user, tag,
                           "2026-10-06T10:00:00+00:00",
                           None if status == "Active" else "2026-10-06T11:00:00+00:00", status))
        return tx_id

    def amount(self, tx_id=1):
        self.calculate(self.conn, tx_id)
        return self.conn.execute("SELECT cost_cents FROM transactions WHERE id=?", (tx_id,)).fetchone()[0]

    def test_unlimited_user_pays_snapshot_tariff(self):
        self.add_session()
        self.assertEqual(350, self.amount())

    def test_monthly_limit_is_not_free_credit(self):
        self.conn.execute("UPDATE users SET monthly_kwh_limit=100 WHERE id=1")
        self.add_session()
        self.assertEqual(350, self.amount())

    def test_crossing_limit_does_not_discount_part_of_session(self):
        self.conn.execute("UPDATE users SET monthly_kwh_limit=100 WHERE id=1")
        self.add_session(energy=95, tx_id=2, status="Completed", cost=3325)
        self.add_session()
        self.assertEqual(350, self.amount())

    def test_zero_limit_does_not_change_price(self):
        self.conn.execute("UPDATE users SET monthly_kwh_limit=0 WHERE id=1")
        self.add_session()
        self.assertEqual(350, self.amount())

    def test_explicit_zero_tariff_is_free(self):
        self.add_session(price=0)
        self.assertEqual(0, self.amount())

    def test_absent_tariff_stays_unknown(self):
        self.add_session(price=None)
        self.assertIsNone(self.amount())

    def test_zero_energy_with_absent_tariff_stays_unknown(self):
        self.add_session(energy=0, price=None)
        self.assertIsNone(self.amount())

    def test_zero_energy_with_tariff_costs_zero(self):
        self.add_session(energy=0)
        self.assertEqual(0, self.amount())

    def test_unknown_energy_clears_non_snapshot_estimate(self):
        self.add_session(energy=None, cost=999)
        self.assertIsNone(self.amount())

    def test_unassigned_session_uses_its_snapshot(self):
        self.add_session(user=None)
        self.assertEqual(350, self.amount())

    def test_rfid_only_assignment_does_not_make_session_free(self):
        self.add_session(user=None, tag="card-1")
        self.assertEqual(350, self.amount())

    def test_legacy_rfid_does_not_make_session_free(self):
        self.add_session(user=None, tag="legacy-card")
        self.assertEqual(350, self.amount())

    def test_fractional_cent_rounds_half_up(self):
        self.add_session(energy=0.1)
        self.assertEqual(4, self.amount())

    def test_repeated_calculation_is_idempotent(self):
        self.add_session()
        self.assertEqual(350, self.amount())
        self.assertEqual(350, self.amount())

    def test_changing_master_tariff_does_not_change_snapshot(self):
        self.add_session()
        self.conn.execute("UPDATE tariffs SET price_cents_per_kwh=99 WHERE id=1")
        self.assertEqual(350, self.amount())

    def test_only_requested_transaction_is_updated(self):
        self.add_session()
        self.add_session(tx_id=2, cost=1234, status="Completed")
        self.amount()
        self.assertEqual(1234, self.conn.execute("SELECT cost_cents FROM transactions WHERE id=2").fetchone()[0])

    def test_missing_transaction_is_noop(self):
        self.calculate(self.conn, 999)
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def test_invalid_energy_stays_unknown(self):
        for energy in (-1, float("inf"), "not-a-number"):
            with self.subTest(energy=energy):
                self.conn.execute("DELETE FROM transactions")
                self.add_session(energy=energy)
                self.assertIsNone(self.amount())

    def test_invalid_price_stays_unknown(self):
        for price in (-35, float("inf"), "not-a-number", 35.5):
            with self.subTest(price=price):
                self.conn.execute("DELETE FROM transactions")
                self.add_session(price=price)
                self.assertIsNone(self.amount())

    def test_costs_do_not_depend_on_live_user_or_tariff_tables(self):
        self.add_session()
        for table in ("users", "rfid_cards", "tariffs"):
            self.conn.execute("DROP TABLE " + table)
        self.assertEqual(350, self.amount())


if __name__ == "__main__":
    unittest.main()
