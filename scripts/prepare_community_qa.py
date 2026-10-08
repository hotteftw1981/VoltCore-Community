"""Seed only an offline-update setting for a brand-new disposable QA database."""
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3


def main():
    if os.environ.get("VOLTCORE_QA_ALLOW_EMPTY_SETUP") != "1":
        raise SystemExit("Disposable QA opt-in required")
    root = Path(os.environ["RUNNER_TEMP"]) / "community-runtime-data"
    root.mkdir(mode=0o700, exist_ok=True)
    database = root / "ocpp.sqlite3"
    if database.exists():
        raise SystemExit("Refusing to overwrite an existing database")
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO app_settings VALUES(?,?,?)",
                     ("update_check_enabled", "0", datetime.now(timezone.utc).isoformat()))
    print("Prepared isolated QA setting; application will create all remaining schema on first start")


if __name__ == "__main__":
    main()
