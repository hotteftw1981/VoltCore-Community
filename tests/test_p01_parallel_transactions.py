"""P01 concurrent, isolated OCPP session regression probes.

Exercises the database with concurrent callers, not real chargers or sockets.
Expected failures represent acknowledged bugs/planned concurrency policy.
"""
import concurrent.futures
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="voltcore-p01-parallel-import-"))

from app import db, ocpp_server


CP_ID = "P01-PARALLEL-2-CONNECTORS"


class ParallelSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voltcore-p01-parallel-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, target in (("DATA_DIR", root), ("DB_PATH", root / "test.sqlite3")):
            context = patch.object(db, name, target)
            context.start()
            self.addCleanup(context.stop)
        db.init_db()
        db.upsert_charge_point(CP_ID, status="Available", connector_count=2)
        db.discover_connector(CP_ID, 1, status="Available")
        db.discover_connector(CP_ID, 2, status="Available")

    def add_user_card(self, suffix):
        uid = "P01-" + suffix
        user_id = db.create_user("P01 Driver " + suffix, gamification_enabled=False)
        db.create_rfid_card(uid, user_id=user_id, status="Aktiv")
        return uid

    def active_sessions(self):
        with db._connect() as conn:
            return list(conn.execute(
                "SELECT id,connector_id,id_tag FROM transactions "
                "WHERE charge_point_id=? AND status='Active' AND ended_at IS NULL ORDER BY connector_id",
                (CP_ID,),
            ).fetchall())

    def test_simultaneous_starts_on_different_connectors_are_isolated(self):
        left_uid, right_uid = self.add_user_card("LEFT"), self.add_user_card("RIGHT")
        gate = threading.Barrier(2, timeout=5)
        def begin(connector, uid, meter):
            gate.wait()
            return db.start_transaction(
                CP_ID, connector_id=connector, id_tag=uid, meter_start_kwh=meter,
            )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            left = pool.submit(begin, 1, left_uid, 10.0)
            right = pool.submit(begin, 2, right_uid, 50.0)
            a, b = left.result(timeout=10), right.result(timeout=10)
        self.assertNotEqual(a, b)
        active = self.active_sessions()
        self.assertEqual(len(active), 2)
        self.assertEqual({int(row["connector_id"]) for row in active}, {1, 2})

    def test_simultaneous_meter_updates_remain_transaction_scoped(self):
        first = db.start_transaction(CP_ID, connector_id=1, meter_start_kwh=10.0)
        second = db.start_transaction(CP_ID, connector_id=2, meter_start_kwh=50.0)
        gate = threading.Barrier(2, timeout=5)
        def meter(tx, register, at):
            gate.wait()
            db.update_transaction_from_meter(tx, meter_kwh=register, measured_at=at)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(meter, first, 10.25, "2026-10-09T09:05:00+00:00")
            b = pool.submit(meter, second, 51.50, "2026-10-09T09:05:00+00:00")
            a.result(timeout=10)
            b.result(timeout=10)
        self.assertAlmostEqual(float(db.get_transaction(first)["energy_kwh"]), 0.25)
        self.assertAlmostEqual(float(db.get_transaction(second)["energy_kwh"]), 1.50)

    @unittest.expectedFailure
    def test_known_missing_atomic_same_rfid_limit_under_parallel_starts(self):
        uid = self.add_user_card("SHARED")
        gate = threading.Barrier(2, timeout=5)
        def authorize_and_start(connector):
            gate.wait()
            permission = db.authorization_decision(uid, CP_ID)
            if permission.get("accepted"):
                return db.start_transaction(CP_ID, connector_id=connector, id_tag=uid)
            return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(authorize_and_start, 1)
            b = pool.submit(authorize_and_start, 2)
            a.result(timeout=10)
            b.result(timeout=10)
        self.assertLessEqual(len(self.active_sessions()), 1)

    @unittest.expectedFailure
    def test_known_second_connector_must_survive_first_connector_stop(self):
        one = db.start_transaction(CP_ID, connector_id=1, meter_start_kwh=10)
        two = db.start_transaction(CP_ID, connector_id=2, meter_start_kwh=20)
        # Recreate station-level clear performed by StopTransaction handler.
        db.stop_transaction(one, meter_stop_kwh=11, ended_at="2026-10-09T09:20:00+00:00")
        db.upsert_charge_point(CP_ID, transaction_id=None, power_kw=0)
        self.assertEqual(db.get_transaction(two)["status"], "Active")
        self.assertEqual(db.get_charge_point(CP_ID)["transaction_id"], two)


if __name__ == "__main__":
    unittest.main()
