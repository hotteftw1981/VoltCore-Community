import os
import json
import sqlite3
import threading
import hashlib
import hmac
import secrets
import re
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from . import totp

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "ocpp.sqlite3"
_lock = threading.Lock()
STANDTIME_GRACE_SECONDS = max(0, int(os.getenv("STANDTIME_GRACE_SECONDS", "120")))
POWER_FLOW_THRESHOLD_KW = max(0.0, float(os.getenv("POWER_FLOW_THRESHOLD_KW", "0.05")))
LIVE_TELEMETRY_MAX_AGE_SECONDS = max(30, int(os.getenv("LIVE_TELEMETRY_MAX_AGE_SECONDS", "300")))
LIVE_SOC_MAX_AGE_SECONDS = max(30, int(os.getenv("LIVE_SOC_MAX_AGE_SECONDS", "300")))
PROMPT_UNPLUG_SECONDS = max(60, int(os.getenv("PROMPT_UNPLUG_SECONDS", "600")))
OCPP_AUTH_MAX_FAILURES = max(3, int(os.getenv("OCPP_AUTH_MAX_FAILURES", "8")))
OCPP_AUTH_FAILURE_WINDOW_MINUTES = max(1, int(os.getenv("OCPP_AUTH_FAILURE_WINDOW_MINUTES", "10")))


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_vehicle_plate(value):
    """Normalize common German plate input without mangling unusual/foreign plates."""
    raw=str(value or "").strip().upper()
    if not raw:
        return ""
    raw=raw.replace("–","-").replace("—","-")
    raw=re.sub(r"\s+"," ",raw)
    raw=re.sub(r"\s*-\s*","-",raw)
    # Clear German formats: district (1-3 letters), group (1-2 letters), digits, optional E/H.
    patterns=(
        r"^([A-ZÄÖÜ]{1,3})-([A-ZÄÖÜ]{1,2})\s*-?\s*(\d{1,4})([EH]?)$",
        r"^([A-ZÄÖÜ]{1,3})\s+([A-ZÄÖÜ]{1,2})\s*-?\s*(\d{1,4})([EH]?)$",
    )
    for pattern in patterns:
        m=re.fullmatch(pattern,raw)
        if m:
            return f"{m.group(1)}-{m.group(2)} {m.group(3)}{m.group(4)}"
    # If the structure is ambiguous, keep it readable rather than guessing a district code.
    return raw


def vehicle_plate_key(value):
    return re.sub(r"[^A-ZÄÖÜ0-9]","",normalize_vehicle_plate(value))


def _purge_legacy_demo_data(conn):
    """Remove legacy development/demo records from older releases.

    Only records carrying the legacy demo markers are removed. Operational
    history belonging to real/non-demo charge points is left untouched.
    """
    demo_cp_rows = conn.execute(
        "SELECT id FROM charge_points WHERE COALESCE(simulated,0)=1 OR id LIKE 'DEMO_LP_%' OR UPPER(COALESCE(vendor,'')) LIKE '%DEMO%' OR UPPER(COALESCE(model,'')) LIKE '%DEMO%' OR UPPER(COALESCE(firmware,'')) LIKE '%DEMO%'"
    ).fetchall()
    demo_cp_ids = [r[0] for r in demo_cp_rows]

    demo_user_rows = conn.execute(
        "SELECT id FROM users WHERE UPPER(COALESCE(rfid,'')) LIKE 'DEMO-%'"
    ).fetchall()
    demo_user_ids = [r[0] for r in demo_user_rows]

    demo_vehicle_rows = []
    if demo_user_ids:
        marks=','.join('?' for _ in demo_user_ids)
        demo_vehicle_rows = conn.execute(
            f"SELECT DISTINCT vehicle_id FROM user_vehicles WHERE user_id IN ({marks}) AND vehicle_id IS NOT NULL", demo_user_ids
        ).fetchall()
    demo_vehicle_ids = [r[0] for r in demo_vehicle_rows]

    # Remove demo transactions/events/meter samples first.
    if demo_cp_ids:
        marks=','.join('?' for _ in demo_cp_ids)
        conn.execute(f"DELETE FROM meter_samples WHERE charge_point_id IN ({marks})", demo_cp_ids)
        conn.execute(f"DELETE FROM events WHERE charge_point_id IN ({marks})", demo_cp_ids)
        conn.execute(f"DELETE FROM transactions WHERE charge_point_id IN ({marks})", demo_cp_ids)
        conn.execute(f"DELETE FROM connectors WHERE charge_point_id IN ({marks})", demo_cp_ids)
        conn.execute(f"DELETE FROM charge_points WHERE id IN ({marks})", demo_cp_ids)

    # Remove cards and demo users.
    conn.execute("DELETE FROM rfid_cards WHERE UPPER(COALESCE(uid,'')) LIKE 'DEMO-%'")
    if demo_user_ids:
        marks=','.join('?' for _ in demo_user_ids)
        conn.execute(f"DELETE FROM user_vehicles WHERE user_id IN ({marks})", demo_user_ids)
        conn.execute(f"DELETE FROM users WHERE id IN ({marks})", demo_user_ids)

    # Remove legacy seeded vehicles only when they were tied to demo users and
    # have no remaining transaction or user association.
    seeded_names=('ELW 1','RTW 1','KdoW 1','Dienstwagen 1')
    for vid in demo_vehicle_ids:
        if conn.execute("SELECT 1 FROM transactions WHERE vehicle_id=? LIMIT 1", (vid,)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM user_vehicles WHERE vehicle_id=? LIMIT 1", (vid,)).fetchone():
            continue
        conn.execute("DELETE FROM rfid_cards WHERE vehicle_id=?", (vid,))
        conn.execute("DELETE FROM vehicles WHERE id=? AND name IN (?,?,?,?)", (vid, *seeded_names))

    # Legacy simulator marker is no longer used by the product.
    conn.execute("UPDATE charge_points SET simulated=0 WHERE COALESCE(simulated,0)<>0")


def _migrate_branding_identity_v51_conn(conn, existing_settings_count):
    """Keep the historical migration marker without seeding organization branding."""
    marker=conn.execute("SELECT value FROM app_settings WHERE key='branding_identity_v51_migrated'").fetchone()
    if marker:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
        ("branding_identity_v51_migrated","1",utc_now()),
    )
    return False

def init_db():
    with _lock, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS charge_points (
                id TEXT PRIMARY KEY,
                vendor TEXT,
                model TEXT,
                serial_number TEXT,
                firmware TEXT,
                status TEXT NOT NULL DEFAULT 'Unknown',
                last_seen TEXT,
                power_kw REAL NOT NULL DEFAULT 0,
                energy_kwh REAL NOT NULL DEFAULT 0,
                voltage_l1 REAL, voltage_l2 REAL, voltage_l3 REAL, voltage_v REAL,
                current_l1 REAL, current_l2 REAL, current_l3 REAL,
                current_import_a REAL, current_import_l1 REAL, current_import_l2 REAL, current_import_l3 REAL,
                current_offered_a REAL, current_offered_l1 REAL, current_offered_l2 REAL, current_offered_l3 REAL,
                power_offered_kw REAL, frequency_hz REAL, temperature_c REAL, temperature_location TEXT,
                power_factor REAL, soc_percent REAL,
                transaction_id INTEGER,
                connector_count INTEGER NOT NULL DEFAULT 1,
                max_power_kw REAL NOT NULL DEFAULT 22,
                simulated INTEGER NOT NULL DEFAULT 0,
                onboarded INTEGER NOT NULL DEFAULT 1,
                ignored INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT,
                last_message_at TEXT,
                last_message_type TEXT
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                charge_point_id TEXT NOT NULL,
                connector_id INTEGER NOT NULL DEFAULT 1,
                transaction_id TEXT,
                id_tag TEXT,
                vehicle_id INTEGER,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                energy_kwh REAL NOT NULL DEFAULT 0,
                max_power_kw REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'Active'
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                charge_point_id TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'IN',
                event_type TEXT NOT NULL,
                transaction_id INTEGER,
                payload TEXT
            );
            CREATE TABLE IF NOT EXISTS ocpp_capability_snapshots (
                charge_point_id TEXT PRIMARY KEY,
                checked_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ok',
                response_ms INTEGER,
                profiles_json TEXT NOT NULL DEFAULT '[]',
                capabilities_json TEXT NOT NULL DEFAULT '{}',
                configuration_json TEXT NOT NULL DEFAULT '{}',
                unknown_keys_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS ocpp_remote_capabilities (
                charge_point_id TEXT NOT NULL,
                command TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'untested',
                last_status TEXT,
                last_detail TEXT,
                last_attempt_at TEXT,
                last_success_at TEXT,
                response_ms INTEGER,
                success_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0,
                not_supported_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(charge_point_id, command)
            );
            CREATE TABLE IF NOT EXISTS ocpp_service_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                charge_point_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL DEFAULT 'Requested',
                summary TEXT,
                detail TEXT,
                external_ref TEXT,
                requested_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ocpp_service_operations_cp
              ON ocpp_service_operations(charge_point_id,id DESC);
            CREATE TABLE IF NOT EXISTS ocpp_reservations (
                reservation_id INTEGER PRIMARY KEY,
                charge_point_id TEXT NOT NULL,
                connector_id INTEGER NOT NULL,
                id_tag TEXT NOT NULL,
                expiry_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ocpp_reservations_cp
              ON ocpp_reservations(charge_point_id,updated_at DESC);
            CREATE TABLE IF NOT EXISTS meter_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                charge_point_id TEXT NOT NULL,
                transaction_id INTEGER,
                power_kw REAL,
                energy_kwh REAL,
                voltage_l1 REAL, voltage_l2 REAL, voltage_l3 REAL, voltage_v REAL,
                current_l1 REAL, current_l2 REAL, current_l3 REAL,
                current_import_a REAL, current_import_l1 REAL, current_import_l2 REAL, current_import_l3 REAL,
                current_offered_a REAL, current_offered_l1 REAL, current_offered_l2 REAL, current_offered_l3 REAL,
                power_offered_kw REAL, frequency_hz REAL, temperature_c REAL, temperature_location TEXT,
                sampled_at TEXT, power_factor REAL, soc_percent REAL
            );
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                make TEXT,
                model TEXT,
                plate TEXT,
                battery_kwh REAL,
                ac_power_kw REAL,
                dc_power_kw REAL,
                range_km REAL,
                drivetrain TEXT,
                assigned_charge_point TEXT,
                driver TEXT,
                image_path TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'Fahrer',
                department TEXT,
                rfid TEXT,
                status TEXT NOT NULL DEFAULT 'Aktiv',
                vehicle TEXT,
                monthly_kwh_limit REAL,
                monthly_limit_mode TEXT NOT NULL DEFAULT 'warn',
                charge_access_mode TEXT NOT NULL DEFAULT 'all',
                image_path TEXT
            );
            CREATE TABLE IF NOT EXISTS rfid_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL UNIQUE, label TEXT, user_id INTEGER, vehicle_id INTEGER,
                status TEXT NOT NULL DEFAULT 'Aktiv', created_at TEXT NOT NULL, last_used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_vehicles (
                user_id INTEGER NOT NULL, vehicle_id INTEGER NOT NULL, primary_vehicle INTEGER NOT NULL DEFAULT 0,
                assigned_at TEXT NOT NULL, PRIMARY KEY(user_id, vehicle_id)
            );
            CREATE TABLE IF NOT EXISTS user_charge_point_access (
                user_id INTEGER NOT NULL,
                charge_point_id TEXT NOT NULL,
                assigned_at TEXT NOT NULL,
                PRIMARY KEY(user_id, charge_point_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_charge_point_access_cp ON user_charge_point_access(charge_point_id,user_id);
            CREATE TABLE IF NOT EXISTS connectors (
                charge_point_id TEXT NOT NULL,
                connector_id INTEGER NOT NULL,
                connector_type TEXT,
                max_power_kw REAL NOT NULL DEFAULT 22,
                status TEXT NOT NULL DEFAULT 'Unknown',
                meter_start_kwh REAL,
                meter_current_kwh REAL,
                PRIMARY KEY(charge_point_id, connector_id)
            );
            CREATE TABLE IF NOT EXISTS system_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_login_at TEXT,
                totp_secret TEXT,
                totp_pending_secret TEXT,
                totp_enabled INTEGER NOT NULL DEFAULT 0,
                totp_enabled_at TEXT,
                totp_last_counter INTEGER
            );
            CREATE TABLE IF NOT EXISTS system_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                system_user_id INTEGER,
                username TEXT,
                display_name TEXT,
                action TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'system',
                target TEXT,
                method TEXT,
                path TEXT,
                details TEXT,
                status_code INTEGER
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'state',
                severity TEXT NOT NULL DEFAULT 'info',
                title TEXT NOT NULL,
                message TEXT,
                link TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS notification_reads (
                notification_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                read_at TEXT NOT NULL,
                PRIMARY KEY(notification_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS notification_dismissals (
                notification_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                dismissed_at TEXT NOT NULL,
                PRIMARY KEY(notification_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_notification_dismissals_user
              ON notification_dismissals(user_id,dismissed_at DESC);
            CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                min_severity TEXT NOT NULL DEFAULT 'warning',
                user_agent TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_success_at TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_web_push_subscriptions_user
              ON web_push_subscriptions(user_id,updated_at DESC);
            CREATE TABLE IF NOT EXISTS web_push_deliveries (
                subscription_id INTEGER NOT NULL,
                notification_id INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                delivered_at TEXT NOT NULL,
                PRIMARY KEY(subscription_id,notification_id,revision)
            );
            CREATE TABLE IF NOT EXISTS tariffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                scope TEXT NOT NULL,
                target_id TEXT,
                price_cents_per_kwh INTEGER NOT NULL,
                valid_from TEXT NOT NULL,
                valid_until TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rfid_card_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT,
                note TEXT,
                source TEXT NOT NULL DEFAULT 'admin',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnostic_states (
                state_key TEXT PRIMARY KEY,
                charge_point_id TEXT NOT NULL,
                connector_id INTEGER,
                transaction_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                severity TEXT NOT NULL DEFAULT 'warning',
                category TEXT NOT NULL DEFAULT 'system',
                code TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_changed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnostic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                charge_point_id TEXT NOT NULL,
                connector_id INTEGER,
                transaction_id INTEGER,
                severity TEXT NOT NULL DEFAULT 'info',
                category TEXT NOT NULL DEFAULT 'system',
                code TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT,
                state TEXT NOT NULL DEFAULT 'event'
            );
            CREATE TABLE IF NOT EXISTS rfid_local_list_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                uid TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS charge_point_local_list_state (
                charge_point_id TEXT PRIMARY KEY,
                backend_version INTEGER NOT NULL DEFAULT 0,
                station_version INTEGER,
                station_entry_count INTEGER,
                offline_auth_status TEXT,
                offline_auth_detail TEXT,
                offline_auth_readonly INTEGER,
                offline_auth_checked_at TEXT,
                pending INTEGER NOT NULL DEFAULT 1,
                supported INTEGER,
                status TEXT NOT NULL DEFAULT 'Ausstehend',
                last_update_type TEXT,
                last_response TEXT,
                last_attempt_at TEXT,
                last_sync_at TEXT
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS security_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                category TEXT NOT NULL DEFAULT 'security',
                event_type TEXT NOT NULL,
                charge_point_id TEXT,
                system_user_id INTEGER,
                username TEXT,
                remote TEXT,
                success INTEGER,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS web_login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_hash TEXT NOT NULL,
                username_key TEXT NOT NULL,
                ts TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ocpp_auth_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_key TEXT NOT NULL,
                charge_point_id TEXT,
                ts TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS system_2fa_challenges (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_path TEXT NOT NULL DEFAULT '/'
            );
            CREATE TABLE IF NOT EXISTS system_2fa_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ip_hash TEXT NOT NULL,
                ts TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS system_recovery_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                used_at TEXT
            );
            """
        )
        # V0.9.7.51: decide upgrade-vs-fresh immediately after app_settings exists,
        # before this init run inserts any regular default settings.
        existing_settings_before_v51=int(conn.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] or 0)
        _migrate_branding_identity_v51_conn(conn,existing_settings_before_v51)
        system_user_columns = {r[1] for r in conn.execute("PRAGMA table_info(system_users)").fetchall()}
        for name, statement in {
            "totp_secret": "ALTER TABLE system_users ADD COLUMN totp_secret TEXT",
            "totp_pending_secret": "ALTER TABLE system_users ADD COLUMN totp_pending_secret TEXT",
            "totp_enabled": "ALTER TABLE system_users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0",
            "totp_enabled_at": "ALTER TABLE system_users ADD COLUMN totp_enabled_at TEXT",
            "totp_last_counter": "ALTER TABLE system_users ADD COLUMN totp_last_counter INTEGER",
        }.items():
            if name not in system_user_columns:
                conn.execute(statement)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_system_2fa_challenges_expiry ON system_2fa_challenges(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_system_2fa_attempts_user_ip ON system_2fa_attempts(user_id,ip_hash,ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_system_recovery_codes_user ON system_recovery_codes(user_id,used_at)")

        user_columns = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        for col in ("email", "phone"):
            if col not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        if "monthly_kwh_limit" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN monthly_kwh_limit REAL")
        if "monthly_limit_mode" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN monthly_limit_mode TEXT NOT NULL DEFAULT 'warn'")
        if "image_path" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN image_path TEXT")
        if "charge_access_mode" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN charge_access_mode TEXT NOT NULL DEFAULT 'all'")
        rfid_columns = {r[1] for r in conn.execute("PRAGMA table_info(rfid_cards)").fetchall()}
        for name, statement in {
            "issued_at": "ALTER TABLE rfid_cards ADD COLUMN issued_at TEXT",
            "expires_at": "ALTER TABLE rfid_cards ADD COLUMN expires_at TEXT",
            "blocked_at": "ALTER TABLE rfid_cards ADD COLUMN blocked_at TEXT",
            "blocked_reason": "ALTER TABLE rfid_cards ADD COLUMN blocked_reason TEXT",
            "replacement_for_id": "ALTER TABLE rfid_cards ADD COLUMN replacement_for_id INTEGER",
            "notes": "ALTER TABLE rfid_cards ADD COLUMN notes TEXT",
        }.items():
            if name not in rfid_columns:
                conn.execute(statement)
        local_list_state_columns = {r[1] for r in conn.execute("PRAGMA table_info(charge_point_local_list_state)").fetchall()}
        if "station_entry_count" not in local_list_state_columns:
            conn.execute("ALTER TABLE charge_point_local_list_state ADD COLUMN station_entry_count INTEGER")
        for name,statement in {
            "offline_auth_status":"ALTER TABLE charge_point_local_list_state ADD COLUMN offline_auth_status TEXT",
            "offline_auth_detail":"ALTER TABLE charge_point_local_list_state ADD COLUMN offline_auth_detail TEXT",
            "offline_auth_readonly":"ALTER TABLE charge_point_local_list_state ADD COLUMN offline_auth_readonly INTEGER",
            "offline_auth_checked_at":"ALTER TABLE charge_point_local_list_state ADD COLUMN offline_auth_checked_at TEXT",
        }.items():
            if name not in local_list_state_columns:
                conn.execute(statement)
        notification_columns = {r[1] for r in conn.execute("PRAGMA table_info(notifications)").fetchall()}
        if "audience" not in notification_columns:
            conn.execute("ALTER TABLE notifications ADD COLUMN audience TEXT NOT NULL DEFAULT 'all'")
        if "revision" not in notification_columns:
            conn.execute("ALTER TABLE notifications ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rfid_events_card ON rfid_card_events(card_id,created_at)")
        conn.execute("UPDATE rfid_cards SET issued_at=COALESCE(issued_at,created_at) WHERE issued_at IS NULL")
        transaction_columns = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
        for col in ("user_id", "rfid_card_id"):
            if col not in transaction_columns: conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} INTEGER")
        vehicle_columns = {r[1] for r in conn.execute("PRAGMA table_info(vehicles)").fetchall()}
        for name, statement in {
            "ac_power_kw": "ALTER TABLE vehicles ADD COLUMN ac_power_kw REAL",
            "dc_power_kw": "ALTER TABLE vehicles ADD COLUMN dc_power_kw REAL",
            "range_km": "ALTER TABLE vehicles ADD COLUMN range_km REAL",
            "drivetrain": "ALTER TABLE vehicles ADD COLUMN drivetrain TEXT",
            "image_path": "ALTER TABLE vehicles ADD COLUMN image_path TEXT",
        }.items():
            if name not in vehicle_columns:
                conn.execute(statement)
        transaction_columns = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
        if "vehicle_id" not in transaction_columns:
            conn.execute("ALTER TABLE transactions ADD COLUMN vehicle_id INTEGER")
            conn.execute("""UPDATE transactions SET vehicle_id = (
                SELECT v.id FROM vehicles v JOIN users u ON u.vehicle = v.name WHERE u.rfid = transactions.id_tag LIMIT 1
            ) WHERE vehicle_id IS NULL""")
        transaction_columns = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
        for name, statement in {
            "meter_start_kwh": "ALTER TABLE transactions ADD COLUMN meter_start_kwh REAL",
            "meter_stop_kwh": "ALTER TABLE transactions ADD COLUMN meter_stop_kwh REAL",
            "last_meter_kwh": "ALTER TABLE transactions ADD COLUMN last_meter_kwh REAL",
            "last_meter_at": "ALTER TABLE transactions ADD COLUMN last_meter_at TEXT",
        }.items():
            if name not in transaction_columns:
                conn.execute(statement)
        event_columns = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "transaction_id" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN transaction_id INTEGER")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(charge_points)").fetchall()}
        for name, statement in {
            "onboarded": "ALTER TABLE charge_points ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 1",
            "ignored": "ALTER TABLE charge_points ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0",
            "first_seen": "ALTER TABLE charge_points ADD COLUMN first_seen TEXT",
            "last_message_at": "ALTER TABLE charge_points ADD COLUMN last_message_at TEXT",
            "last_message_type": "ALTER TABLE charge_points ADD COLUMN last_message_type TEXT",
        }.items():
            if name not in columns:
                conn.execute(statement)
        for name, statement in {
            "power_source": "ALTER TABLE charge_points ADD COLUMN power_source TEXT",
            "power_calculated_at": "ALTER TABLE charge_points ADD COLUMN power_calculated_at TEXT",
            "location": "ALTER TABLE charge_points ADD COLUMN location TEXT",
            "connector_type": "ALTER TABLE charge_points ADD COLUMN connector_type TEXT",
            "ocpp_version": "ALTER TABLE charge_points ADD COLUMN ocpp_version TEXT DEFAULT '1.6J'",
            "notes": "ALTER TABLE charge_points ADD COLUMN notes TEXT",
            "retired": "ALTER TABLE charge_points ADD COLUMN retired INTEGER NOT NULL DEFAULT 0",
            "archived": "ALTER TABLE charge_points ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
            "archived_at": "ALTER TABLE charge_points ADD COLUMN archived_at TEXT",
            "archive_reason": "ALTER TABLE charge_points ADD COLUMN archive_reason TEXT",
            "source_type": "ALTER TABLE charge_points ADD COLUMN source_type TEXT NOT NULL DEFAULT 'manual'",
            "voltage_l1": "ALTER TABLE charge_points ADD COLUMN voltage_l1 REAL",
            "voltage_l2": "ALTER TABLE charge_points ADD COLUMN voltage_l2 REAL",
            "voltage_l3": "ALTER TABLE charge_points ADD COLUMN voltage_l3 REAL",
            "voltage_v": "ALTER TABLE charge_points ADD COLUMN voltage_v REAL",
            "current_l1": "ALTER TABLE charge_points ADD COLUMN current_l1 REAL",
            "current_l2": "ALTER TABLE charge_points ADD COLUMN current_l2 REAL",
            "current_l3": "ALTER TABLE charge_points ADD COLUMN current_l3 REAL",
            "current_import_a": "ALTER TABLE charge_points ADD COLUMN current_import_a REAL",
            "current_import_l1": "ALTER TABLE charge_points ADD COLUMN current_import_l1 REAL",
            "current_import_l2": "ALTER TABLE charge_points ADD COLUMN current_import_l2 REAL",
            "current_import_l3": "ALTER TABLE charge_points ADD COLUMN current_import_l3 REAL",
            "current_offered_a": "ALTER TABLE charge_points ADD COLUMN current_offered_a REAL",
            "current_offered_l1": "ALTER TABLE charge_points ADD COLUMN current_offered_l1 REAL",
            "current_offered_l2": "ALTER TABLE charge_points ADD COLUMN current_offered_l2 REAL",
            "current_offered_l3": "ALTER TABLE charge_points ADD COLUMN current_offered_l3 REAL",
            "power_offered_kw": "ALTER TABLE charge_points ADD COLUMN power_offered_kw REAL",
            "frequency_hz": "ALTER TABLE charge_points ADD COLUMN frequency_hz REAL",
            "temperature_c": "ALTER TABLE charge_points ADD COLUMN temperature_c REAL",
            "temperature_location": "ALTER TABLE charge_points ADD COLUMN temperature_location TEXT",
            "power_factor": "ALTER TABLE charge_points ADD COLUMN power_factor REAL",
            "soc_percent": "ALTER TABLE charge_points ADD COLUMN soc_percent REAL",
            "station_status": "ALTER TABLE charge_points ADD COLUMN station_status TEXT",
            "station_error_code": "ALTER TABLE charge_points ADD COLUMN station_error_code TEXT",
            "last_status_at": "ALTER TABLE charge_points ADD COLUMN last_status_at TEXT",
            "stand_grace_seconds": f"ALTER TABLE charge_points ADD COLUMN stand_grace_seconds INTEGER NOT NULL DEFAULT {STANDTIME_GRACE_SECONDS}",
            "auto_stop_zero_minutes": "ALTER TABLE charge_points ADD COLUMN auto_stop_zero_minutes INTEGER NOT NULL DEFAULT 0",
            "ocpp_secret_hash": "ALTER TABLE charge_points ADD COLUMN ocpp_secret_hash TEXT",
            "ocpp_secret_set_at": "ALTER TABLE charge_points ADD COLUMN ocpp_secret_set_at TEXT",
            "ocpp_auth_last_success_at": "ALTER TABLE charge_points ADD COLUMN ocpp_auth_last_success_at TEXT",
            "ocpp_auth_last_failure_at": "ALTER TABLE charge_points ADD COLUMN ocpp_auth_last_failure_at TEXT",
            "ocpp_auth_last_reason": "ALTER TABLE charge_points ADD COLUMN ocpp_auth_last_reason TEXT",
            "ocpp_last_transport": "ALTER TABLE charge_points ADD COLUMN ocpp_last_transport TEXT",
            "ocpp_last_remote": "ALTER TABLE charge_points ADD COLUMN ocpp_last_remote TEXT",
        }.items():
            if name not in columns:
                conn.execute(statement)
        connector_columns = {r[1] for r in conn.execute("PRAGMA table_info(connectors)").fetchall()}
        for name, statement in {
            "power_source": "ALTER TABLE connectors ADD COLUMN power_source TEXT",
            "power_calculated_at": "ALTER TABLE connectors ADD COLUMN power_calculated_at TEXT",
            "transaction_id": "ALTER TABLE connectors ADD COLUMN transaction_id INTEGER",
            "power_kw": "ALTER TABLE connectors ADD COLUMN power_kw REAL",
            "energy_kwh": "ALTER TABLE connectors ADD COLUMN energy_kwh REAL",
            "voltage_l1": "ALTER TABLE connectors ADD COLUMN voltage_l1 REAL",
            "voltage_l2": "ALTER TABLE connectors ADD COLUMN voltage_l2 REAL",
            "voltage_l3": "ALTER TABLE connectors ADD COLUMN voltage_l3 REAL",
            "voltage_v": "ALTER TABLE connectors ADD COLUMN voltage_v REAL",
            "current_l1": "ALTER TABLE connectors ADD COLUMN current_l1 REAL",
            "current_l2": "ALTER TABLE connectors ADD COLUMN current_l2 REAL",
            "current_l3": "ALTER TABLE connectors ADD COLUMN current_l3 REAL",
            "current_import_a": "ALTER TABLE connectors ADD COLUMN current_import_a REAL",
            "current_import_l1": "ALTER TABLE connectors ADD COLUMN current_import_l1 REAL",
            "current_import_l2": "ALTER TABLE connectors ADD COLUMN current_import_l2 REAL",
            "current_import_l3": "ALTER TABLE connectors ADD COLUMN current_import_l3 REAL",
            "current_offered_a": "ALTER TABLE connectors ADD COLUMN current_offered_a REAL",
            "current_offered_l1": "ALTER TABLE connectors ADD COLUMN current_offered_l1 REAL",
            "current_offered_l2": "ALTER TABLE connectors ADD COLUMN current_offered_l2 REAL",
            "current_offered_l3": "ALTER TABLE connectors ADD COLUMN current_offered_l3 REAL",
            "power_offered_kw": "ALTER TABLE connectors ADD COLUMN power_offered_kw REAL",
            "frequency_hz": "ALTER TABLE connectors ADD COLUMN frequency_hz REAL",
            "temperature_c": "ALTER TABLE connectors ADD COLUMN temperature_c REAL",
            "temperature_location": "ALTER TABLE connectors ADD COLUMN temperature_location TEXT",
            "power_factor": "ALTER TABLE connectors ADD COLUMN power_factor REAL",
            "soc_percent": "ALTER TABLE connectors ADD COLUMN soc_percent REAL",
            "last_meter_at": "ALTER TABLE connectors ADD COLUMN last_meter_at TEXT",
            "last_error_code": "ALTER TABLE connectors ADD COLUMN last_error_code TEXT",
        }.items():
            if name not in connector_columns:
                conn.execute(statement)
        transaction_columns = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
        for name, statement in {
            "stop_reason": "ALTER TABLE transactions ADD COLUMN stop_reason TEXT",
            "charging_seconds": "ALTER TABLE transactions ADD COLUMN charging_seconds REAL NOT NULL DEFAULT 0",
            "stand_seconds": "ALTER TABLE transactions ADD COLUMN stand_seconds REAL NOT NULL DEFAULT 0",
            "connection_seconds": "ALTER TABLE transactions ADD COLUMN connection_seconds REAL NOT NULL DEFAULT 0",
            "stand_grace_seconds": f"ALTER TABLE transactions ADD COLUMN stand_grace_seconds INTEGER NOT NULL DEFAULT {STANDTIME_GRACE_SECONDS}",
            "auto_stop_zero_minutes": "ALTER TABLE transactions ADD COLUMN auto_stop_zero_minutes INTEGER NOT NULL DEFAULT 0",
            "tariff_id": "ALTER TABLE transactions ADD COLUMN tariff_id INTEGER",
            "tariff_name": "ALTER TABLE transactions ADD COLUMN tariff_name TEXT",
            "tariff_source": "ALTER TABLE transactions ADD COLUMN tariff_source TEXT",
            "price_cents_per_kwh": "ALTER TABLE transactions ADD COLUMN price_cents_per_kwh INTEGER",
            "cost_cents": "ALTER TABLE transactions ADD COLUMN cost_cents INTEGER",
            "timing_quality": "ALTER TABLE transactions ADD COLUMN timing_quality TEXT",
            "post_session_occupied_started_at": "ALTER TABLE transactions ADD COLUMN post_session_occupied_started_at TEXT",
            "unplugged_at": "ALTER TABLE transactions ADD COLUMN unplugged_at TEXT",
            "post_session_occupied_seconds": "ALTER TABLE transactions ADD COLUMN post_session_occupied_seconds REAL",
        }.items():
            if name not in transaction_columns:
                conn.execute(statement)

        # V0.9.7.74 migration recovery: if the previous release already persisted
        # a connector as Finishing, arm the latest ended session immediately.
        # This runs before the OCPP server reconnects, so an immediate Available
        # notification cannot erase the already elapsed post-session occupancy.
        conn.execute("""UPDATE transactions
            SET post_session_occupied_started_at=ended_at
            WHERE ended_at IS NOT NULL
              AND post_session_occupied_started_at IS NULL
              AND unplugged_at IS NULL
              AND julianday(ended_at) >= julianday('now','-7 days')
              AND id=(
                SELECT MAX(t2.id) FROM transactions t2
                WHERE t2.charge_point_id=transactions.charge_point_id
                  AND t2.connector_id=transactions.connector_id
                  AND t2.ended_at IS NOT NULL
              )
              AND EXISTS(
                SELECT 1 FROM connectors c
                WHERE c.charge_point_id=transactions.charge_point_id
                  AND c.connector_id=transactions.connector_id
                  AND c.status='Finishing'
              )""")

        meter_columns = {r[1] for r in conn.execute("PRAGMA table_info(meter_samples)").fetchall()}
        for name, statement in {
            "power_source": "ALTER TABLE meter_samples ADD COLUMN power_source TEXT",
            "power_calculated_at": "ALTER TABLE meter_samples ADD COLUMN power_calculated_at TEXT",
            "connector_id": "ALTER TABLE meter_samples ADD COLUMN connector_id INTEGER",
            "sampled_at": "ALTER TABLE meter_samples ADD COLUMN sampled_at TEXT",
            "voltage_v": "ALTER TABLE meter_samples ADD COLUMN voltage_v REAL",
            "current_import_a": "ALTER TABLE meter_samples ADD COLUMN current_import_a REAL",
            "current_import_l1": "ALTER TABLE meter_samples ADD COLUMN current_import_l1 REAL",
            "current_import_l2": "ALTER TABLE meter_samples ADD COLUMN current_import_l2 REAL",
            "current_import_l3": "ALTER TABLE meter_samples ADD COLUMN current_import_l3 REAL",
            "current_offered_a": "ALTER TABLE meter_samples ADD COLUMN current_offered_a REAL",
            "current_offered_l1": "ALTER TABLE meter_samples ADD COLUMN current_offered_l1 REAL",
            "current_offered_l2": "ALTER TABLE meter_samples ADD COLUMN current_offered_l2 REAL",
            "current_offered_l3": "ALTER TABLE meter_samples ADD COLUMN current_offered_l3 REAL",
            "power_offered_kw": "ALTER TABLE meter_samples ADD COLUMN power_offered_kw REAL",
            "frequency_hz": "ALTER TABLE meter_samples ADD COLUMN frequency_hz REAL",
            "temperature_c": "ALTER TABLE meter_samples ADD COLUMN temperature_c REAL",
            "temperature_location": "ALTER TABLE meter_samples ADD COLUMN temperature_location TEXT",
            "raw_json": "ALTER TABLE meter_samples ADD COLUMN raw_json TEXT",
            "values_json": "ALTER TABLE meter_samples ADD COLUMN values_json TEXT",
        }.items():
            if name not in meter_columns:
                conn.execute(statement)

        # MeterValues may arrive every few seconds. Keep live/history lookups fast
        # without imposing a vendor-specific retention policy.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meter_samples_cp_connector_id ON meter_samples(charge_point_id, connector_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meter_samples_transaction_id ON meter_samples(transaction_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_diagnostic_events_cp_ts ON diagnostic_events(charge_point_id, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_diagnostic_events_category ON diagnostic_events(category, severity, ts DESC)")

        # V0.8.8.1: legacy current_l* represented imported current. Preserve it
        # as the explicit Current.Import value while keeping the old columns as
        # compatibility aliases for older templates/exports.
        conn.execute("UPDATE charge_points SET current_import_l1=COALESCE(current_import_l1,current_l1), current_import_l2=COALESCE(current_import_l2,current_l2), current_import_l3=COALESCE(current_import_l3,current_l3)")
        conn.execute("UPDATE connectors SET current_import_l1=COALESCE(current_import_l1,current_l1), current_import_l2=COALESCE(current_import_l2,current_l2), current_import_l3=COALESCE(current_import_l3,current_l3)")
        conn.execute("UPDATE meter_samples SET current_import_l1=COALESCE(current_import_l1,current_l1), current_import_l2=COALESCE(current_import_l2,current_l2), current_import_l3=COALESCE(current_import_l3,current_l3)")

        # Keep connector records in sync with every known charge point.
        cp_rows = conn.execute("SELECT id, connector_count, max_power_kw, connector_type, status FROM charge_points").fetchall()
        for cp in cp_rows:
            count = max(1, int(cp[1] or 1))
            for connector_id in range(1, count + 1):
                conn.execute(
                    "INSERT OR IGNORE INTO connectors(charge_point_id,connector_id,connector_type,max_power_kw,status) VALUES(?,?,?,?,?)",
                    (cp[0], connector_id, cp[3] or "Type 2", float(cp[2] or 22), cp[4] or "Unknown")
                )

        _purge_legacy_demo_data(conn)
        # V0.8.8.9: one connector can never have multiple simultaneous OCPP
        # transactions. Older releases could leave stale Active rows behind if
        # a simulator/process vanished before StopTransaction arrived. Keep the
        # newest row and repair only impossible duplicates.
        _reconcile_duplicate_active_transactions_conn(conn)

        now_setting=utc_now()
        for key,value in (("ocpp_auth_mode","off"),("ocpp_reject_unknown","0"),("ocpp_require_tls","0"),("ocpp_require_subprotocol","0"),
                          ("rfid_local_list_version","1")):
            conn.execute("INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",(key,value,now_setting))
        current_local_version=int((conn.execute("SELECT value FROM app_settings WHERE key='rfid_local_list_version'").fetchone() or [1])[0] or 1)

        # V0.9.7.75: LiveView is not part of Community. Remove settings left
        # behind by the short-lived 0.9.7.74 release candidate.
        conn.execute("DELETE FROM app_settings WHERE key LIKE 'liveview_%'")
        conn.execute("DROP TABLE IF EXISTS load_rules")
        conn.execute("DROP TABLE IF EXISTS portal_pin_reset_requests")
        conn.execute("DROP TABLE IF EXISTS portal_pin_reset_tokens")
        conn.execute("DROP TABLE IF EXISTS access_request_verifications")
        conn.execute("DROP TABLE IF EXISTS access_requests")
        conn.execute("DROP TABLE IF EXISTS rfid_enrollment_sessions")
        conn.execute("DROP TABLE IF EXISTS billing_groups")
        conn.execute("DROP TABLE IF EXISTS user_billing_groups")
        conn.execute("DROP TABLE IF EXISTS portal_sessions")
        conn.execute("DROP TABLE IF EXISTS portal_login_attempts")
        conn.execute("DROP TABLE IF EXISTS achievements")
        conn.execute("DROP TABLE IF EXISTS achievement_awards")
        conn.execute("DROP TABLE IF EXISTS gamification_events")
        conn.execute("DROP TABLE IF EXISTS gamification_event_rewards")
        conn.execute("DROP TABLE IF EXISTS gamification_event_results")
        conn.execute("DROP TABLE IF EXISTS bonus_vouchers")
        conn.execute("DROP TABLE IF EXISTS bonus_grants")
        conn.execute("DROP TABLE IF EXISTS bonus_usage")
        conn.execute("DROP TABLE IF EXISTS bonus_voucher_redemptions")
        conn.execute("DROP TABLE IF EXISTS bonus_transfers")
        conn.execute("DROP TABLE IF EXISTS rfid_replacement_requests")
        conn.execute("DROP TABLE IF EXISTS cost_centers")
        conn.execute("DELETE FROM app_settings WHERE key LIKE 'registration_%'")

        # Community does not rewrite historical billing data during initialization.
        # Existing transaction costs and tariff snapshots are preserved verbatim.

        conn.execute("""INSERT OR IGNORE INTO charge_point_local_list_state(charge_point_id,backend_version,pending,status)
            SELECT id,?,1,'Ausstehend' FROM charge_points WHERE COALESCE(onboarded,1)=1 AND COALESCE(ignored,0)=0 AND COALESCE(retired,0)=0 AND COALESCE(archived,0)=0""",(current_local_version,))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rfid_local_list_changes_version ON rfid_local_list_changes(version,id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_security_events_ts ON security_events(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_security_events_cp ON security_events(charge_point_id,ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_web_login_attempts_ip_ts ON web_login_attempts(ip_hash,ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_web_login_attempts_user_ts ON web_login_attempts(username_key,ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ocpp_auth_attempts_key_ts ON ocpp_auth_attempts(client_key,ts DESC)")

        conn.commit()


def upsert_charge_point(cp_id, **fields):
    now = utc_now()
    defaults = {
        "vendor": None, "model": None, "serial_number": None, "firmware": None,
        "status": "Unknown", "power_kw": 0, "energy_kwh": 0,
        "voltage_l1": None, "voltage_l2": None, "voltage_l3": None, "voltage_v": None,
        "current_l1": None, "current_l2": None, "current_l3": None,
        "current_import_a": None, "current_import_l1": None, "current_import_l2": None, "current_import_l3": None,
        "current_offered_a": None, "current_offered_l1": None, "current_offered_l2": None, "current_offered_l3": None,
        "power_offered_kw": None, "frequency_hz": None, "temperature_c": None, "temperature_location": None,
        "power_factor": None, "soc_percent": None, "transaction_id": None, "power_source": None, "power_calculated_at": None,
        "station_status": None, "station_error_code": None, "last_status_at": None,
        "connector_count": 1, "max_power_kw": 22, "simulated": 0,
        "onboarded": 1, "ignored": 0, "source_type": "ocpp", "first_seen": now, "last_message_at": now, "last_message_type": None,
    }
    with _lock, _connect() as conn:
        current = conn.execute("SELECT * FROM charge_points WHERE id=?", (cp_id,)).fetchone()
        if current is None:
            values = {k: fields.get(k, v) for k, v in defaults.items()}
            conn.execute(
                """INSERT INTO charge_points
                (id,vendor,model,serial_number,firmware,status,last_seen,power_kw,energy_kwh,
                 voltage_l1,voltage_l2,voltage_l3,current_l1,current_l2,current_l3,power_factor,soc_percent,
                 transaction_id,power_source,power_calculated_at,station_status,station_error_code,last_status_at,connector_count,max_power_kw,simulated,onboarded,ignored,source_type,first_seen,last_message_at,last_message_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cp_id, values["vendor"], values["model"], values["serial_number"], values["firmware"],
                 values["status"], now, float(values["power_kw"] or 0), float(values["energy_kwh"] or 0),
                 values["voltage_l1"], values["voltage_l2"], values["voltage_l3"], values["current_l1"],
                 values["current_l2"], values["current_l3"], values["power_factor"], values["soc_percent"],
                 values["transaction_id"], values["power_source"], values["power_calculated_at"], values["station_status"], values["station_error_code"], values["last_status_at"], int(values["connector_count"] if values["connector_count"] is not None else 1), float(values["max_power_kw"] if values["max_power_kw"] is not None else 22),
                 int(bool(values["simulated"])), int(bool(values["onboarded"])), int(bool(values["ignored"])), values["source_type"] or "ocpp",
                 values["first_seen"] or now, values["last_message_at"] or now, values["last_message_type"],
                )
            )
        else:
            merged = {k: (fields[k] if k in fields and fields[k] is not None else current[k]) for k in defaults}
            # Existing records keep their origin; OCPP discovery must not turn a manually created
            # charge point into an OCPP-managed device merely because a message updates it.
            if "source_type" not in fields:
                merged["source_type"] = current["source_type"] or "manual"
            conn.execute(
                """UPDATE charge_points SET vendor=?,model=?,serial_number=?,firmware=?,status=?,last_seen=?,
                 power_kw=?,energy_kwh=?,voltage_l1=?,voltage_l2=?,voltage_l3=?,current_l1=?,current_l2=?,
                 current_l3=?,power_factor=?,soc_percent=?,transaction_id=?,power_source=?,power_calculated_at=?,station_status=?,station_error_code=?,last_status_at=?,connector_count=?,max_power_kw=?,simulated=?,
                 onboarded=?,ignored=?,source_type=?,first_seen=COALESCE(first_seen,?),last_message_at=?,last_message_type=?
                 WHERE id=?""",
                (merged["vendor"], merged["model"], merged["serial_number"], merged["firmware"], merged["status"], now,
                 float(merged["power_kw"] or 0), float(merged["energy_kwh"] or 0), merged["voltage_l1"], merged["voltage_l2"],
                 merged["voltage_l3"], merged["current_l1"], merged["current_l2"], merged["current_l3"], merged["power_factor"],
                 merged["soc_percent"], merged["transaction_id"], merged["power_source"], merged["power_calculated_at"], merged["station_status"], merged["station_error_code"], merged["last_status_at"], int(merged["connector_count"] if merged["connector_count"] is not None else 1), float(merged["max_power_kw"] if merged["max_power_kw"] is not None else 22),
                 int(bool(merged["simulated"])), int(bool(merged["onboarded"])), int(bool(merged["ignored"])), merged["source_type"] or "ocpp",
                 merged["first_seen"] or now, merged["last_message_at"] or now, merged["last_message_type"], cp_id),
            )
        conn.commit()


def mark_message(cp_id, message_type, **fields):
    fields["last_message_at"] = utc_now()
    fields["last_message_type"] = message_type
    upsert_charge_point(cp_id, **fields)

def is_known_charge_point(cp_id):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT onboarded, ignored FROM charge_points WHERE id=?", (cp_id,)).fetchone()
        return bool(row and int(row["onboarded"] or 0) and not int(row["ignored"] or 0))

def list_discovered_devices():
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM charge_points ORDER BY CASE WHEN onboarded=0 AND ignored=0 THEN 0 ELSE 1 END, last_seen DESC, id").fetchall()
        return [dict(r) for r in rows]

def onboard_charge_point(cp_id, name=None, location=None, max_power_kw=None, notes=None):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT id FROM charge_points WHERE id=?", (cp_id,)).fetchone()
        if not row: return False
        updates = ["onboarded=1", "ignored=0"]
        values = []
        if location is not None: updates.append("location=?"); values.append(location)
        if max_power_kw is not None: updates.append("max_power_kw=?"); values.append(float(max_power_kw))
        if notes is not None: updates.append("notes=?"); values.append(notes)
        # name is represented by id in the current schema; keep Charge Point ID stable.
        values.append(cp_id)
        conn.execute("UPDATE charge_points SET " + ", ".join(updates) + " WHERE id=?", values)
        conn.commit(); return True

def ignore_charge_point(cp_id):
    with _lock, _connect() as conn:
        cur=conn.execute("UPDATE charge_points SET ignored=1,onboarded=0,status='Unknown' WHERE id=?",(cp_id,))
        conn.commit(); return cur.rowcount>0

def unignore_charge_point(cp_id):
    with _lock, _connect() as conn:
        cur=conn.execute("UPDATE charge_points SET ignored=0,onboarded=0,status='Pending' WHERE id=?",(cp_id,))
        conn.commit(); return cur.rowcount>0

def _overall_status_from_rows(station_status, connector_rows):
    statuses = [str(r["status"] or "Unknown") for r in connector_rows if int(r["connector_id"] or 0) > 0]
    if any(s == "Faulted" for s in statuses): return "Faulted"
    if any(s == "Charging" for s in statuses): return "Charging"
    if any(s == "Preparing" for s in statuses): return "Preparing"
    if any(s == "SuspendedEV" for s in statuses): return "SuspendedEV"
    if any(s == "SuspendedEVSE" for s in statuses): return "SuspendedEVSE"
    if any(s == "Finishing" for s in statuses): return "Finishing"
    if statuses and all(s == "Unavailable" for s in statuses): return "Unavailable"
    if any(s == "Available" for s in statuses): return "Available"
    return station_status or (statuses[0] if statuses else "Unknown")


def set_status_notification(cp_id, connector_id, status, error_code=None):
    cid = int(connector_id or 0)
    status_text = str(status or "Unknown")
    now = utc_now()
    with _lock, _connect() as conn:
        if not conn.execute("SELECT id FROM charge_points WHERE id=?", (cp_id,)).fetchone():
            return False
        if cid > 0:
            conn.execute("""UPDATE connectors SET status=?, last_error_code=?
                            WHERE charge_point_id=? AND connector_id=?""", (status_text, error_code, cp_id, cid))
            if not conn.execute("SELECT 1 FROM connectors WHERE charge_point_id=? AND connector_id=?", (cp_id, cid)).fetchone():
                conn.execute("INSERT INTO connectors(charge_point_id,connector_id,connector_type,max_power_kw,status,last_error_code) VALUES(?,?,?,?,?,?)", (cp_id,cid,"Type 2",22,status_text,error_code))
        else:
            conn.execute("UPDATE charge_points SET station_status=?,station_error_code=?,last_status_at=? WHERE id=?", (status_text,error_code,now,cp_id))
        rows = conn.execute("SELECT connector_id,status FROM connectors WHERE charge_point_id=?", (cp_id,)).fetchall()
        station = conn.execute("SELECT station_status FROM charge_points WHERE id=?", (cp_id,)).fetchone()
        overall = _overall_status_from_rows(station[0] if station else None, rows)
        conn.execute("UPDATE charge_points SET status=?,last_seen=?,last_status_at=? WHERE id=?", (overall, now, now, cp_id))
        conn.commit(); return True


def discover_connector(cp_id, connector_id, connector_type=None, max_power_kw=None, status=None):
    if int(connector_id or 0) <= 0: return False
    with _lock, _connect() as conn:
        cp = conn.execute("SELECT connector_count,connector_type,max_power_kw FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        if not cp: return False
        cid=int(connector_id); count=max(int(cp[0] or 1),cid)
        ctype=connector_type or cp[1] or "Type 2"; maxkw=float(max_power_kw if max_power_kw is not None else (cp[2] or 22))
        conn.execute("INSERT OR IGNORE INTO connectors(charge_point_id,connector_id,connector_type,max_power_kw,status) VALUES(?,?,?,?,?)",(cp_id,cid,ctype,maxkw,status or "Unknown"))
        conn.execute("UPDATE connectors SET connector_type=COALESCE(?,connector_type),max_power_kw=COALESCE(?,max_power_kw),status=COALESCE(?,status) WHERE charge_point_id=? AND connector_id=?",(connector_type,max_power_kw,status,cp_id,cid))
        conn.execute("UPDATE charge_points SET connector_count=? WHERE id=?",(count,cp_id))
        conn.commit(); return True



def update_charge_point_telemetry(cp_id, **fields):
    """Update only supported live telemetry fields at station level (connector 0)."""
    allowed = {
        "power_kw", "power_offered_kw", "energy_kwh", "power_source", "power_calculated_at",
        "voltage_v", "voltage_l1", "voltage_l2", "voltage_l3",
        "current_import_a", "current_import_l1", "current_import_l2", "current_import_l3",
        "current_offered_a", "current_offered_l1", "current_offered_l2", "current_offered_l3",
        "frequency_hz", "temperature_c", "temperature_location", "power_factor", "soc_percent",
    }
    updates = [(k, fields[k]) for k in allowed if k in fields and fields[k] is not None]
    if not updates:
        return False
    # Compatibility aliases always mirror actual Current.Import, never Current.Offered.
    for phase in ("l1", "l2", "l3"):
        key = f"current_import_{phase}"
        if key in fields and fields[key] is not None:
            updates.append((f"current_{phase}", fields[key]))
    with _lock, _connect() as conn:
        conn.execute("UPDATE charge_points SET " + ", ".join(f"{k}=?" for k, _ in updates) + " WHERE id=?",
                     [v for _, v in updates] + [cp_id])
        conn.commit()
        return True


def refresh_charge_point_telemetry(cp_id):
    """Aggregate connector telemetry into charge-point level without inventing values."""
    with _lock, _connect() as conn:
        rows = conn.execute("""SELECT power_kw, power_offered_kw, energy_kwh, voltage_v, voltage_l1, voltage_l2, voltage_l3,
            current_import_a, current_import_l1, current_import_l2, current_import_l3,
            current_offered_a, current_offered_l1, current_offered_l2, current_offered_l3,
            frequency_hz, temperature_c, temperature_location, power_factor, soc_percent, status
            FROM connectors WHERE charge_point_id=? AND connector_id>0""", (cp_id,)).fetchall()
        if not rows:
            return
        def first_value(name):
            for r in rows:
                if r[name] is not None:
                    return r[name]
            return None
        power_values = [float(r["power_kw"]) for r in rows if r["power_kw"] is not None]
        offered_values = [float(r["power_offered_kw"]) for r in rows if r["power_offered_kw"] is not None]
        energy_values = [float(r["energy_kwh"]) for r in rows if r["energy_kwh"] is not None]
        power = sum(power_values) if power_values else None
        offered = sum(offered_values) if offered_values else None
        energy = sum(energy_values) if energy_values else None
        conn.execute("""UPDATE charge_points SET
            power_kw=COALESCE(?,power_kw), power_offered_kw=COALESCE(?,power_offered_kw), energy_kwh=COALESCE(?,energy_kwh),
            voltage_v=COALESCE(?,voltage_v), voltage_l1=COALESCE(?,voltage_l1), voltage_l2=COALESCE(?,voltage_l2), voltage_l3=COALESCE(?,voltage_l3),
            current_import_a=COALESCE(?,current_import_a), current_import_l1=COALESCE(?,current_import_l1), current_import_l2=COALESCE(?,current_import_l2), current_import_l3=COALESCE(?,current_import_l3),
            current_offered_a=COALESCE(?,current_offered_a), current_offered_l1=COALESCE(?,current_offered_l1), current_offered_l2=COALESCE(?,current_offered_l2), current_offered_l3=COALESCE(?,current_offered_l3),
            current_l1=COALESCE(?,current_l1), current_l2=COALESCE(?,current_l2), current_l3=COALESCE(?,current_l3),
            frequency_hz=COALESCE(?,frequency_hz), temperature_c=COALESCE(?,temperature_c), temperature_location=COALESCE(?,temperature_location),
            power_factor=COALESCE(?,power_factor), soc_percent=COALESCE(?,soc_percent)
            WHERE id=?""", (power, offered, energy, first_value("voltage_v"), first_value("voltage_l1"), first_value("voltage_l2"), first_value("voltage_l3"),
                              first_value("current_import_a"), first_value("current_import_l1"), first_value("current_import_l2"), first_value("current_import_l3"),
                              first_value("current_offered_a"), first_value("current_offered_l1"), first_value("current_offered_l2"), first_value("current_offered_l3"),
                              first_value("current_import_l1"), first_value("current_import_l2"), first_value("current_import_l3"),
                              first_value("frequency_hz"), first_value("temperature_c"), first_value("temperature_location"),
                              first_value("power_factor"), first_value("soc_percent"), cp_id))
        conn.commit()


def add_meter_sample(cp_id, connector_id=None, raw_json=None, values_json=None, sampled_at=None, **fields):
    received_at = utc_now()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO meter_samples(
                ts,sampled_at,charge_point_id,connector_id,transaction_id,power_kw,power_offered_kw,energy_kwh,power_source,power_calculated_at,
                voltage_v,voltage_l1,voltage_l2,voltage_l3,
                current_l1,current_l2,current_l3,current_import_a,current_import_l1,current_import_l2,current_import_l3,
                current_offered_a,current_offered_l1,current_offered_l2,current_offered_l3,frequency_hz,temperature_c,temperature_location,
                power_factor,soc_percent,raw_json,values_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (received_at, sampled_at, cp_id, connector_id, fields.get("transaction_id"), fields.get("power_kw"), fields.get("power_offered_kw"), fields.get("energy_kwh"), fields.get("power_source"), fields.get("power_calculated_at"),
             fields.get("voltage_v"), fields.get("voltage_l1"), fields.get("voltage_l2"), fields.get("voltage_l3"),
             fields.get("current_import_l1"), fields.get("current_import_l2"), fields.get("current_import_l3"), fields.get("current_import_a"), fields.get("current_import_l1"), fields.get("current_import_l2"), fields.get("current_import_l3"),
             fields.get("current_offered_a"), fields.get("current_offered_l1"), fields.get("current_offered_l2"), fields.get("current_offered_l3"), fields.get("frequency_hz"), fields.get("temperature_c"), fields.get("temperature_location"),
             fields.get("power_factor"), fields.get("soc_percent"), raw_json, values_json),
        )
        if connector_id and int(connector_id) > 0:
            conn.execute("""UPDATE connectors SET power_kw=?, power_offered_kw=COALESCE(?,power_offered_kw), energy_kwh=COALESCE(?,energy_kwh),
                power_source=?, power_calculated_at=?, voltage_v=COALESCE(?,voltage_v),
                voltage_l1=COALESCE(?,voltage_l1), voltage_l2=COALESCE(?,voltage_l2), voltage_l3=COALESCE(?,voltage_l3),
                current_import_a=COALESCE(?,current_import_a), current_import_l1=COALESCE(?,current_import_l1), current_import_l2=COALESCE(?,current_import_l2), current_import_l3=COALESCE(?,current_import_l3),
                current_offered_a=COALESCE(?,current_offered_a), current_offered_l1=COALESCE(?,current_offered_l1), current_offered_l2=COALESCE(?,current_offered_l2), current_offered_l3=COALESCE(?,current_offered_l3),
                current_l1=COALESCE(?,current_l1), current_l2=COALESCE(?,current_l2), current_l3=COALESCE(?,current_l3),
                frequency_hz=COALESCE(?,frequency_hz), temperature_c=COALESCE(?,temperature_c), temperature_location=COALESCE(?,temperature_location),
                power_factor=COALESCE(?,power_factor), soc_percent=COALESCE(?,soc_percent), last_meter_at=?
                WHERE charge_point_id=? AND connector_id=?""",
                (fields.get("power_kw"), fields.get("power_offered_kw"), fields.get("energy_kwh"), fields.get("power_source"), fields.get("power_calculated_at"), fields.get("voltage_v"),
                 fields.get("voltage_l1"), fields.get("voltage_l2"), fields.get("voltage_l3"),
                 fields.get("current_import_a"), fields.get("current_import_l1"), fields.get("current_import_l2"), fields.get("current_import_l3"),
                 fields.get("current_offered_a"), fields.get("current_offered_l1"), fields.get("current_offered_l2"), fields.get("current_offered_l3"),
                 fields.get("current_import_l1"), fields.get("current_import_l2"), fields.get("current_import_l3"),
                 fields.get("frequency_hz"), fields.get("temperature_c"), fields.get("temperature_location"), fields.get("power_factor"), fields.get("soc_percent"), sampled_at or received_at, cp_id, int(connector_id)))
        conn.commit()


def _derived_power_for_connector(conn, cp_id, connector_id, latest_sample):
    """Return (power_kw, source, calculated_at) using direct power first, otherwise delta-energy/time."""
    if latest_sample is None:
        return None, None, None
    direct = latest_sample["power_kw"]
    if direct is not None:
        return float(direct), "measured", latest_sample["ts"]
    if latest_sample["energy_kwh"] is None:
        return None, None, None
    prev = conn.execute(
        "SELECT energy_kwh, COALESCE(sampled_at,ts) AS measured_at FROM meter_samples WHERE charge_point_id=? AND connector_id=? AND energy_kwh IS NOT NULL AND id<? ORDER BY id DESC LIMIT 1",
        (cp_id, connector_id, latest_sample["id"]),
    ).fetchone()
    if not prev:
        return None, None, None
    try:
        from datetime import datetime
        t1 = datetime.fromisoformat(str(prev[1]).replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(str(latest_sample["sampled_at"] or latest_sample["ts"]).replace("Z", "+00:00"))
        seconds = (t2 - t1).total_seconds()
        delta_kwh = float(latest_sample["energy_kwh"]) - float(prev[0])
        if seconds < 5 or delta_kwh < 0:
            return None, None, None
        kw = delta_kwh / (seconds / 3600.0)
        cap=conn.execute("SELECT max_power_kw FROM connectors WHERE charge_point_id=? AND connector_id=?",(cp_id,connector_id)).fetchone()
        configured=float(cap[0] or 0) if cap else 0.0
        plausible_max=max(100.0,configured*3.0)
        if kw > plausible_max:
            return None, None, None
        return kw, "calculated", latest_sample["sampled_at"] or latest_sample["ts"]
    except Exception:
        return None, None, None


def _telemetry_age_seconds(value):
    dt=_parse_iso_utc(value)
    if dt is None:
        return None
    return max(0,int((datetime.now(timezone.utc)-dt).total_seconds()))


def live_telemetry_for_charge_point(cp_id):
    """Return newest telemetry per connector with explicit freshness metadata.

    Meter registers remain available as last-known values. Volatile values such
    as live power and SoC are only promoted to station-level *live* values while
    they are fresh enough. This prevents an Available/idle connector from
    looking as if an old charging value were still current.
    """
    import json
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM connectors WHERE charge_point_id=? AND connector_id>0 ORDER BY connector_id",
            (cp_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            sample = conn.execute(
                "SELECT * FROM meter_samples WHERE charge_point_id=? AND connector_id=? ORDER BY id DESC LIMIT 1",
                (cp_id, int(row["connector_id"])),
            ).fetchone()
            if sample:
                item["last_meter_at"] = sample["sampled_at"] or sample["ts"]
                item["meter_sample_id"] = sample["id"]
                item["meter_age_seconds"] = _telemetry_age_seconds(item["last_meter_at"])
                item["freshness"] = "live" if item["meter_age_seconds"] is not None and item["meter_age_seconds"] <= 90 else ("delayed" if item["meter_age_seconds"] is not None and item["meter_age_seconds"] <= LIVE_TELEMETRY_MAX_AGE_SECONDS else "stale")
                for key in ("power_kw","power_offered_kw","energy_kwh","voltage_v","voltage_l1","voltage_l2","voltage_l3",
                            "current_import_a","current_import_l1","current_import_l2","current_import_l3",
                            "current_offered_a","current_offered_l1","current_offered_l2","current_offered_l3",
                            "frequency_hz","temperature_c","temperature_location","power_factor"):
                    if sample[key] is not None:
                        item[key] = sample[key]
                # SoC is special: many stations send it only occasionally. Use the
                # newest actual SoC sample and expose its own age instead of making
                # it look fresh merely because another MeterValue arrived later.
                soc_sample=conn.execute(
                    "SELECT soc_percent,COALESCE(sampled_at,ts) AS measured_at FROM meter_samples WHERE charge_point_id=? AND connector_id=? AND soc_percent IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (cp_id,int(row["connector_id"])),
                ).fetchone()
                if soc_sample:
                    item["last_soc_percent"]=float(soc_sample["soc_percent"])
                    item["soc_measured_at"]=soc_sample["measured_at"]
                    item["soc_age_seconds"]=_telemetry_age_seconds(soc_sample["measured_at"])
                    item["soc_fresh"]=bool(item["soc_age_seconds"] is not None and item["soc_age_seconds"] <= LIVE_SOC_MAX_AGE_SECONDS)
                    item["soc_percent"]=item["last_soc_percent"] if item["soc_fresh"] else None
                else:
                    item["last_soc_percent"]=None; item["soc_measured_at"]=None; item["soc_age_seconds"]=None; item["soc_fresh"]=False; item["soc_percent"]=None
                item["current_l1"] = item.get("current_import_l1")
                item["current_l2"] = item.get("current_import_l2")
                item["current_l3"] = item.get("current_import_l3")
                power, source, calc_at = _derived_power_for_connector(conn, cp_id, int(row["connector_id"]), sample)
                item["last_power_kw"] = power
                item["last_power_source"] = source
                item["power_calculated_at"] = calc_at
                active_state=str(item.get("status") or "").strip() in {"Charging","SuspendedEV","SuspendedEVSE"}
                power_fresh=bool(item["meter_age_seconds"] is not None and item["meter_age_seconds"] <= LIVE_TELEMETRY_MAX_AGE_SECONDS)
                item["power_kw"] = power if active_state and power_fresh else None
                item["power_source"] = source if item["power_kw"] is not None else None
                try:
                    item["measurements"] = json.loads(sample["values_json"] or "[]")
                except Exception:
                    item["measurements"] = []
            else:
                item["measurements"] = []
                item["power_kw"] = None
                item["power_source"] = None
                item["last_power_kw"] = None
                item["last_power_source"] = None
                item["last_meter_at"] = None
                item["meter_age_seconds"] = None
                item["freshness"] = "none"
                item["soc_percent"] = None
                item["last_soc_percent"] = None
                item["soc_measured_at"] = None
                item["soc_age_seconds"] = None
                item["soc_fresh"] = False
            result.append(item)
        def first(key):
            for item in result:
                if item.get(key) is not None:
                    return item[key]
            return None
        active_result=[x for x in result if x.get("power_kw") is not None]
        power_values=[float(x["power_kw"]) for x in active_result]
        energy_values=[float(x["energy_kwh"]) for x in result if x.get("energy_kwh") is not None]
        sources=[x.get("power_source") for x in active_result if x.get("power_source")]
        latest_power_item=next((x for x in sorted(result,key=lambda y:str(y.get("last_meter_at") or ""),reverse=True) if x.get("last_power_kw") is not None),None)
        fresh_soc=next((x.get("soc_percent") for x in result if x.get("soc_percent") is not None),None)
        last_soc=next((x.get("last_soc_percent") for x in result if x.get("last_soc_percent") is not None),None)
        return {
            "connectors": result,
            "power_kw": sum(power_values) if power_values else None,
            "last_power_kw": float(latest_power_item["last_power_kw"]) if latest_power_item else None,
            "power_source": "measured" if "measured" in sources else ("calculated" if "calculated" in sources else None),
            "last_power_source": latest_power_item.get("last_power_source") if latest_power_item else None,
            # Energy.Active.Import.Register is a cumulative meter register, not
            # session energy. UI labels must therefore call this a meter reading.
            "energy_kwh": sum(energy_values) if energy_values else None,
            "meter_register_kwh": sum(energy_values) if energy_values else None,
            "power_active_import_kw": sum(power_values) if power_values else None,
            "power_offered_kw": sum(float(x["power_offered_kw"]) for x in result if x.get("power_offered_kw") is not None) if any(x.get("power_offered_kw") is not None for x in result) else None,
            "voltage_v": first("voltage_v"), "voltage_l1": first("voltage_l1"), "voltage_l2": first("voltage_l2"), "voltage_l3": first("voltage_l3"),
            "current_import_a": first("current_import_a"), "current_import_l1": first("current_import_l1"), "current_import_l2": first("current_import_l2"), "current_import_l3": first("current_import_l3"),
            "current_offered_a": first("current_offered_a"), "current_offered_l1": first("current_offered_l1"), "current_offered_l2": first("current_offered_l2"), "current_offered_l3": first("current_offered_l3"),
            "current_l1": first("current_import_l1"), "current_l2": first("current_import_l2"), "current_l3": first("current_import_l3"),
            "frequency_hz": first("frequency_hz"), "temperature_c": first("temperature_c"), "temperature_location": first("temperature_location"),
            "power_factor": first("power_factor"), "soc_percent": fresh_soc, "last_soc_percent": last_soc,
            "last_meter_at": max((x.get("last_meter_at") for x in result if x.get("last_meter_at")), default=None),
            "telemetry_max_age_seconds": LIVE_TELEMETRY_MAX_AGE_SECONDS,
            "soc_max_age_seconds": LIVE_SOC_MAX_AGE_SECONDS,
        }


def meter_samples_for_charge_point(cp_id, limit=120):
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM meter_samples WHERE charge_point_id=? ORDER BY id DESC LIMIT ?", (cp_id, limit)
        ).fetchall()]


def meter_history_for_charge_point(cp_id, connector_id=None, hours=6, max_points=240):
    """Return bounded, chronological MeterValues history for live charts.

    No values are synthesized here. Power can already be measured or calculated
    when the sample was stored; all other fields are the actual OCPP values.
    """
    try:
        hours = max(1, min(168, int(hours or 6)))
    except (TypeError, ValueError):
        hours = 6
    try:
        max_points = max(30, min(720, int(max_points or 240)))
    except (TypeError, ValueError):
        max_points = 240
    connector = None
    if connector_id not in (None, "", 0, "0"):
        try:
            connector = int(connector_id)
        except (TypeError, ValueError):
            connector = None
    # Read a generous bounded window, then apply timestamp filtering in Python.
    # This is robust for both ISO timestamps ending in Z and +00:00.
    sql = "SELECT * FROM meter_samples WHERE charge_point_id=?"
    args = [cp_id]
    if connector is not None:
        sql += " AND connector_id=?"
        args.append(connector)
    sql += " ORDER BY id DESC LIMIT 6000"
    with _lock, _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered = []
    for row in reversed(rows):
        stamp = row.get("sampled_at") or row.get("ts")
        try:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if dt < cutoff:
            continue
        filtered.append({
            "id": row.get("id"), "at": stamp, "connector_id": row.get("connector_id"),
            "transaction_id": row.get("transaction_id"), "power_kw": row.get("power_kw"),
            "power_source": row.get("power_source"), "energy_kwh": row.get("energy_kwh"),
            "voltage_v": row.get("voltage_v"), "voltage_l1": row.get("voltage_l1"),
            "voltage_l2": row.get("voltage_l2"), "voltage_l3": row.get("voltage_l3"),
            "current_import_a": row.get("current_import_a"), "current_import_l1": row.get("current_import_l1"),
            "current_import_l2": row.get("current_import_l2"), "current_import_l3": row.get("current_import_l3"),
            "power_offered_kw": row.get("power_offered_kw"), "frequency_hz": row.get("frequency_hz"),
            "temperature_c": row.get("temperature_c"), "soc_percent": row.get("soc_percent"),
            "power_factor": row.get("power_factor"),
        })
    if len(filtered) > max_points:
        # Evenly downsample while preserving first and newest points.
        span = len(filtered) - 1
        indexes = sorted({round(i * span / (max_points - 1)) for i in range(max_points)})
        filtered = [filtered[i] for i in indexes]
    return {
        "charge_point_id": cp_id, "connector_id": connector, "hours": hours,
        "points": filtered, "count": len(filtered),
    }


def meter_diagnostics_for_charge_point(cp_id, limit=12):
    """Return a bounded, read-only diagnostic view of the newest raw OCPP MeterValues."""
    import json
    rows = meter_samples_for_charge_point(cp_id, limit)
    out = []
    for row in rows:
        try:
            values = json.loads(row.get("values_json") or "[]")
        except Exception:
            values = []
        out.append({
            "id": row.get("id"), "ts": row.get("ts"), "sampled_at": row.get("sampled_at"), "connector_id": row.get("connector_id"),
            "transaction_id": row.get("transaction_id"), "values": values,
        })
    return out


def add_event(cp_id, event_type, payload="", direction="IN", transaction_id=None):
    with _lock, _connect() as conn:
        conn.execute("INSERT INTO events(ts,charge_point_id,direction,event_type,transaction_id,payload) VALUES(?,?,?,?,?,?)", (utc_now(), cp_id, direction, event_type, transaction_id, payload))
        conn.commit()

def events_for_transaction(tx_id, limit=240):
    with _lock, _connect() as conn:
        tx=conn.execute("SELECT charge_point_id,started_at,ended_at,connector_id FROM transactions WHERE id=?", (tx_id,)).fetchone()
        if not tx:
            return []
        linked=conn.execute("SELECT * FROM events WHERE transaction_id=? ORDER BY id DESC LIMIT ?", (tx_id, limit)).fetchall()
        if linked:
            return [dict(r) for r in linked]
        end=tx[2] or utc_now()
        rows=conn.execute("SELECT * FROM events WHERE charge_point_id=? AND ts>=? AND ts<=? AND event_type IN ('Authorize','StartTransaction','StatusNotification','MeterValues','StopTransaction') ORDER BY id DESC LIMIT ?", (tx[0], tx[1], end, limit)).fetchall()
        return [dict(r) for r in rows]

def transaction_live_state(tx_id):
    with _lock, _connect() as conn:
        row=conn.execute("""SELECT t.*, v.name AS vehicle_name, u.name AS user_name, r.uid AS rfid_uid,
            (SELECT ms.energy_kwh FROM meter_samples ms WHERE ms.transaction_id=t.id AND ms.energy_kwh IS NOT NULL ORDER BY ms.id DESC LIMIT 1) AS live_meter_kwh,
            (SELECT ms.soc_percent FROM meter_samples ms WHERE ms.transaction_id=t.id AND ms.soc_percent IS NOT NULL ORDER BY ms.id DESC LIMIT 1) AS live_soc_percent,
            (SELECT COALESCE(ms.sampled_at,ms.ts) FROM meter_samples ms WHERE ms.transaction_id=t.id ORDER BY ms.id DESC LIMIT 1) AS live_meter_at
            FROM transactions t LEFT JOIN vehicles v ON v.id=t.vehicle_id LEFT JOIN users u ON u.id=t.user_id LEFT JOIN rfid_cards r ON r.id=t.rfid_card_id WHERE t.id=?""", (tx_id,)).fetchone()
        if not row: return None
        item=dict(row)
        sample=conn.execute("SELECT * FROM meter_samples WHERE transaction_id=? ORDER BY id DESC LIMIT 1", (tx_id,)).fetchone()
        if sample:
            power, source, _=_derived_power_for_connector(conn, item['charge_point_id'], int(item['connector_id'] or 0), sample)
            measured_at=sample['sampled_at'] or sample['ts']
            age=_telemetry_age_seconds(measured_at)
            fresh=bool(age is not None and age <= LIVE_TELEMETRY_MAX_AGE_SECONDS and item.get('status')=='Active' and not item.get('ended_at'))
            item['last_power_kw']=power; item['last_power_source']=source
            item['live_power_kw']=power if fresh else None; item['live_power_source']=source if fresh else None
            item['live_meter_at']=measured_at; item['live_meter_age_seconds']=age; item['live_meter_fresh']=fresh
            soc_row=conn.execute("SELECT soc_percent,COALESCE(sampled_at,ts) AS measured_at FROM meter_samples WHERE transaction_id=? AND soc_percent IS NOT NULL ORDER BY id DESC LIMIT 1",(tx_id,)).fetchone()
            soc_age=_telemetry_age_seconds(soc_row['measured_at']) if soc_row else None
            item['live_soc_percent']=float(soc_row['soc_percent']) if soc_row and soc_age is not None and soc_age <= LIVE_SOC_MAX_AGE_SECONDS and item.get('status')=='Active' else None
            item['last_soc_percent']=float(soc_row['soc_percent']) if soc_row else None
            item['soc_age_seconds']=soc_age
        else:
            item['live_power_kw']=None; item['live_power_source']=None; item['last_power_kw']=None; item['last_power_source']=None; item['live_meter_age_seconds']=None; item['live_meter_fresh']=False
        return item


def _reconcile_active_transactions_for_connector_conn(conn, cp_id, connector_id, ended_at=None, reason="Reconciled", keep_tx_id=None):
    """Close impossible duplicate/stale active sessions for one physical connector.

    OCPP allows a transaction to survive a temporary backend disconnect, so a
    disconnect alone never closes a session. Reconciliation is only performed
    on authoritative connector evidence (Available) or immediately before a
    new transaction starts on the same connector.
    """
    cid = int(connector_id or 0)
    if cid <= 0:
        return []
    rows = conn.execute(
        """SELECT id,started_at FROM transactions
           WHERE charge_point_id=? AND connector_id=? AND status='Active' AND ended_at IS NULL
           ORDER BY id DESC""",
        (cp_id, cid),
    ).fetchall()
    closed = []
    end_ts = ended_at or utc_now()
    end_dt = _parse_iso_utc(end_ts) or datetime.now(timezone.utc)
    for row in rows:
        tx_id = int(row["id"])
        if keep_tx_id is not None and tx_id == int(keep_tx_id):
            continue
        row_start = _parse_iso_utc(row["started_at"])
        row_end_dt = end_dt if row_start is None or end_dt >= row_start else row_start
        row_end_ts = row_end_dt.isoformat()
        cur = conn.execute(
            """UPDATE transactions
               SET ended_at=?, status='Interrupted', stop_reason=COALESCE(stop_reason,?)
               WHERE id=? AND status='Active' AND ended_at IS NULL""",
            (row_end_ts, reason, tx_id),
        )
        if cur.rowcount > 0:
            timing = _transaction_time_breakdown_conn(conn, tx_id, now=row_end_dt)
            if timing:
                conn.execute(
                    "UPDATE transactions SET charging_seconds=?, stand_seconds=?, connection_seconds=? WHERE id=?",
                    (timing["charging_seconds"], timing["stand_seconds"], timing["connection_seconds"], tx_id),
                )
            conn.execute(
                "INSERT INTO events(ts,charge_point_id,direction,event_type,transaction_id,payload) VALUES(?,?,?,?,?,?)",
                (utc_now(), cp_id, "IN", "SessionReconciled", tx_id, f"connector={cid}; reason={reason}"),
            )
            closed.append(tx_id)

    if closed:
        marks = ",".join("?" for _ in closed)
        conn.execute(
            f"UPDATE connectors SET transaction_id=NULL, power_kw=0 WHERE charge_point_id=? AND connector_id=? AND transaction_id IN ({marks})",
            [cp_id, cid, *closed],
        )
        active = conn.execute(
            "SELECT id FROM transactions WHERE charge_point_id=? AND status='Active' AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (cp_id,),
        ).fetchone()
        conn.execute(
            "UPDATE charge_points SET transaction_id=? WHERE id=?",
            (active[0] if active else None, cp_id),
        )
    return closed


def reconcile_active_transactions_for_connector(cp_id, connector_id, ended_at=None, reason="ConnectorAvailable"):
    with _lock, _connect() as conn:
        closed = _reconcile_active_transactions_for_connector_conn(
            conn, cp_id, connector_id, ended_at=ended_at, reason=reason
        )
        conn.commit()
        return closed


def _reconcile_duplicate_active_transactions_conn(conn):
    """Repair legacy duplicate Active rows while preserving the newest per connector."""
    groups = conn.execute(
        """SELECT charge_point_id,connector_id,COUNT(*) AS n
           FROM transactions WHERE status='Active' AND ended_at IS NULL
           GROUP BY charge_point_id,connector_id HAVING COUNT(*)>1"""
    ).fetchall()
    repaired = []
    for group in groups:
        newest = conn.execute(
            """SELECT id,started_at FROM transactions
               WHERE charge_point_id=? AND connector_id=? AND status='Active' AND ended_at IS NULL
               ORDER BY id DESC LIMIT 1""",
            (group["charge_point_id"], group["connector_id"]),
        ).fetchone()
        if not newest:
            continue
        repaired.extend(
            _reconcile_active_transactions_for_connector_conn(
                conn, group["charge_point_id"], group["connector_id"],
                ended_at=newest["started_at"], reason="DuplicateActiveSession", keep_tx_id=int(newest["id"]),
            )
        )
    return repaired


def start_transaction(cp_id, id_tag=None, connector_id=1, ocpp_transaction_id=None, vehicle_id=None, meter_start_kwh=None):
    with _lock, _connect() as conn:
        card=conn.execute("SELECT id,user_id,vehicle_id,status FROM rfid_cards WHERE uid=? LIMIT 1",(id_tag,)).fetchone()
        user_id=card[1] if card else None; rfid_card_id=card[0] if card else None
        if card and card[3] != "Aktiv": user_id=None
        if not card and id_tag:
            legacy_user=conn.execute("SELECT id FROM users WHERE rfid=? AND status='Aktiv' LIMIT 1",(id_tag,)).fetchone()
            user_id=legacy_user[0] if legacy_user else None
        if vehicle_id is None and card and card[2] is not None: vehicle_id=card[2]
        if vehicle_id is None and user_id is not None:
            row=conn.execute("SELECT vehicle_id FROM user_vehicles WHERE user_id=? ORDER BY primary_vehicle DESC, assigned_at ASC LIMIT 1",(user_id,)).fetchone()
            vehicle_id=row[0] if row else None
        policy=conn.execute("SELECT stand_grace_seconds,auto_stop_zero_minutes FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        grace=STANDTIME_GRACE_SECONDS if not policy or policy[0] is None else max(0,int(policy[0]))
        auto_stop=0 if not policy or policy[1] is None else max(0,int(policy[1]))
        started_at=utc_now()
        _reconcile_active_transactions_for_connector_conn(
            conn, cp_id, connector_id, ended_at=started_at, reason="SupersededByNewTransaction"
        )
        cur=conn.execute("""INSERT INTO transactions(
            charge_point_id,connector_id,transaction_id,id_tag,vehicle_id,user_id,rfid_card_id,
            started_at,status,meter_start_kwh,last_meter_kwh,stand_grace_seconds,auto_stop_zero_minutes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cp_id,connector_id,str(ocpp_transaction_id) if ocpp_transaction_id is not None else None,id_tag,
             vehicle_id,user_id,rfid_card_id,started_at,"Active",meter_start_kwh,meter_start_kwh,grace,auto_stop))
        tx=cur.lastrowid
        _apply_tariff_to_tx_conn(conn, tx, user_id, cp_id, started_at)
        conn.execute("UPDATE charge_points SET transaction_id=? WHERE id=?",(tx,cp_id))
        conn.execute("UPDATE connectors SET transaction_id=?,status='Charging',meter_start_kwh=?,meter_current_kwh=? WHERE charge_point_id=? AND connector_id=?",
                     (tx,meter_start_kwh,meter_start_kwh,cp_id,int(connector_id)))
        if rfid_card_id: conn.execute("UPDATE rfid_cards SET last_used_at=? WHERE id=?",(utc_now(),rfid_card_id))
        conn.commit(); return tx

def update_transaction_from_meter(tx, meter_kwh=None, power_kw=None, measured_at=None):
    with _lock, _connect() as conn:
        row=conn.execute("SELECT meter_start_kwh,energy_kwh,max_power_kw FROM transactions WHERE id=?",(tx,)).fetchone()
        if not row: return None
        session_energy=None
        start=row["meter_start_kwh"]
        if meter_kwh is not None:
            current=float(meter_kwh)
            # Some stations may omit meterStart in StartTransaction. In that case
            # the first MeterValues reading becomes the baseline, never the session energy.
            if start is None:
                start=current
                conn.execute("UPDATE transactions SET meter_start_kwh=? WHERE id=?", (start, tx))
                session_energy=0.0
            else:
                session_energy=max(0.0,current-float(start))
        peak=max(float(row["max_power_kw"] or 0), float(power_kw)) if power_kw is not None else float(row["max_power_kw"] or 0)
        conn.execute("UPDATE transactions SET energy_kwh=COALESCE(?,energy_kwh), max_power_kw=?, last_meter_kwh=COALESCE(?,last_meter_kwh), last_meter_at=COALESCE(?,last_meter_at) WHERE id=?",(session_energy,peak,meter_kwh,measured_at,tx))
        conn.execute("UPDATE connectors SET meter_current_kwh=COALESCE(?,meter_current_kwh) WHERE transaction_id=?",(meter_kwh,tx))
        conn.commit(); return session_energy

def load_state_for_charge_point(cp_id):
    """Return a user-facing state without nesting the database lock."""
    with _lock, _connect() as conn:
        cp=conn.execute("SELECT status, station_status FROM charge_points WHERE id=?", (cp_id,)).fetchone()
        if not cp:
            return {"state":"unknown","label":"Unbekannt","detail":"Keine Gerätedaten","power_kw":None,"transaction_active":False}
        status=str(cp["status"] or cp["station_status"] or "Unknown")
        tx=conn.execute("SELECT id FROM transactions WHERE charge_point_id=? AND status='Active' AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (cp_id,)).fetchone()
        transaction_id=int(tx[0]) if tx else None

    # live_telemetry_for_charge_point acquires _lock itself. Calling it while
    # the lock above is held would block the HTTP request indefinitely.
    live=live_telemetry_for_charge_point(cp_id)
    power=live.get("power_kw")
    measured_at=live.get("last_meter_at")
    power_source=live.get("power_source")
    base={"power_kw":float(power) if power is not None else None,"power_source":power_source,"measured_at":measured_at,"transaction_active":bool(transaction_id),"transaction_id":transaction_id,"ocpp_status":status}
    if status == "Faulted":
        return {**base,"state":"fault","label":"Fehler","detail":"Ladepunkt meldet einen Fehler"}
    if status == "SuspendedEV":
        return {**base,"state":"suspended_ev","label":"Laden pausiert – Fahrzeug","detail":"Das Fahrzeug fordert aktuell keine Energie an"}
    if status == "SuspendedEVSE":
        return {**base,"state":"suspended_evse","label":"Laden pausiert – Ladestation","detail":"Die Ladestation hat die Energieübertragung pausiert"}
    if status == "Finishing":
        return {**base,"state":"finishing","label":"Ladevorgang wird beendet","detail":"Ladevorgang ist in der Abschlussphase"}
    if status == "Charging":
        if power is not None:
            if float(power) > 0.05:
                return {**base,"state":"charging","label":"Energie wird übertragen","detail":"Aktiver Energiefluss"}
            return {**base,"state":"paused","label":"Kein Energiefluss","detail":"OCPP-Ladevorgang aktiv, aktuell 0 kW"}
        return {**base,"state":"charging_unknown","label":"Ladevorgang aktiv","detail":"Energiefluss aktuell nicht verfügbar"}
    if status == "Preparing":
        return {**base,"state":"preparing","label":"Bereit zum Laden","detail":"Ladevorgang wird vorbereitet"}
    if status == "Available":
        return {**base,"state":"available","label":"Verfügbar","detail":"Kein aktiver Ladevorgang"}
    if status in {"Unavailable","Offline","Unknown"}:
        return {**base,"state":"offline","label":status,"detail":"Kein aktiver Ladevorgang"}
    return {**base,"state":status.lower(),"label":status,"detail":"OCPP-Status"}

def _parse_iso_utc(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _transaction_time_breakdown_conn(conn, tx_id, now=None):
    """Derive charging, stand and connection time from vendor-neutral OCPP MeterValues.

    Stand time starts only after a continuous zero-flow grace period and only
    after the session has previously transferred energy. This deliberately
    avoids treating a single SuspendedEV sample or short charging pause as
    parking/stand time.
    """
    tx = conn.execute(
        "SELECT id,charge_point_id,connector_id,started_at,ended_at,status,stand_grace_seconds,auto_stop_zero_minutes FROM transactions WHERE id=?",
        (tx_id,),
    ).fetchone()
    if not tx:
        return None
    start = _parse_iso_utc(tx["started_at"])
    if not start:
        return None
    end = _parse_iso_utc(tx["ended_at"]) or (now or datetime.now(timezone.utc))
    if end < start:
        end = start
    grace = max(0, int(tx["stand_grace_seconds"] if tx["stand_grace_seconds"] is not None else STANDTIME_GRACE_SECONDS))
    rows = conn.execute(
        "SELECT id,COALESCE(sampled_at,ts) AS measured_at,power_kw FROM meter_samples WHERE transaction_id=? ORDER BY COALESCE(sampled_at,ts),id",
        (tx_id,),
    ).fetchall()
    points=[]
    for row in rows:
        ts=_parse_iso_utc(row["measured_at"])
        if ts is None or ts < start or ts > end:
            continue
        power=None
        if row["power_kw"] is not None:
            try: power=float(row["power_kw"])
            except (TypeError,ValueError): power=None
        points.append((ts,power))

    charging=0.0
    zero_runs=[]
    has_positive=False
    zero_start=None
    # Classify only measured intervals. The connection duration remains the
    # authoritative total and can therefore include Preparing/Finishing/unknown time.
    for idx,(ts,power) in enumerate(points):
        next_ts = points[idx+1][0] if idx+1 < len(points) else None
        if next_ts is None:
            # For an active session, extend a zero-flow run while the connector
            # explicitly remains suspended. Positive power is not extrapolated
            # indefinitely when MeterValues become stale.
            if tx["ended_at"] is None:
                c=conn.execute("SELECT status FROM connectors WHERE charge_point_id=? AND connector_id=?", (tx["charge_point_id"], int(tx["connector_id"] or 0))).fetchone()
                cstatus=str(c[0] or "") if c else ""
                if power is not None and power <= POWER_FLOW_THRESHOLD_KW and cstatus in {"SuspendedEV","SuspendedEVSE","Charging"}:
                    next_ts=end
                elif power is not None and power > POWER_FLOW_THRESHOLD_KW and cstatus == "Charging" and (end-ts).total_seconds() <= 30:
                    next_ts=end
            elif (end-ts).total_seconds() <= 30:
                next_ts=end
        if power is None:
            if zero_start is not None:
                zero_runs.append((zero_start,ts)); zero_start=None
            continue
        if power > POWER_FLOW_THRESHOLD_KW:
            has_positive=True
            if zero_start is not None:
                zero_runs.append((zero_start,ts)); zero_start=None
            if next_ts is not None and next_ts > ts:
                charging += (next_ts-ts).total_seconds()
        else:
            if has_positive and zero_start is None:
                zero_start=ts
    if zero_start is not None:
        zero_runs.append((zero_start,end))

    stand=0.0
    zero_flow_seconds=0.0
    zero_flow_since=None
    for zs,ze in zero_runs:
        if ze <= zs: continue
        run=(ze-zs).total_seconds()
        if ze == end:
            zero_flow_seconds=run
            zero_flow_since=zs.isoformat()
        stand += max(0.0, run-grace)
    connection=max(0.0,(end-start).total_seconds())
    return {
        "charging_seconds": round(charging,3),
        "stand_seconds": round(stand,3),
        "connection_seconds": round(connection,3),
        "other_seconds": round(max(0.0,connection-charging-stand),3),
        "stand_grace_seconds": grace,
        "zero_flow_seconds": round(zero_flow_seconds,3),
        "zero_flow_since": zero_flow_since,
        "stand_active": bool(zero_flow_seconds > grace),
        "power_threshold_kw": POWER_FLOW_THRESHOLD_KW,
        "auto_stop_zero_minutes": max(0, int(tx["auto_stop_zero_minutes"] or 0)),
        "auto_stop_due": bool(int(tx["auto_stop_zero_minutes"] or 0) > 0 and zero_flow_seconds >= int(tx["auto_stop_zero_minutes"] or 0) * 60),
        "auto_stop_remaining_seconds": max(0.0, round(int(tx["auto_stop_zero_minutes"] or 0) * 60 - zero_flow_seconds, 3)) if int(tx["auto_stop_zero_minutes"] or 0) > 0 else None,
    }


def transaction_time_breakdown(tx_id):
    with _lock, _connect() as conn:
        return _transaction_time_breakdown_conn(conn, tx_id)


def _post_session_occupancy_for_row(row, now=None):
    if not row or "post_session_occupied_started_at" not in row.keys():
        return {"tracked": False, "active": False, "seconds": None, "started_at": None, "unplugged_at": None}
    started=_parse_iso_utc(row["post_session_occupied_started_at"])
    if not started:
        return {"tracked": False, "active": False, "seconds": None, "started_at": None, "unplugged_at": row["unplugged_at"] if "unplugged_at" in row.keys() else None}
    unplugged=_parse_iso_utc(row["unplugged_at"] if "unplugged_at" in row.keys() else None)
    if unplugged is not None:
        seconds=max(0.0,(unplugged-started).total_seconds())
        stored=row["post_session_occupied_seconds"] if "post_session_occupied_seconds" in row.keys() else None
        if stored is not None:
            try: seconds=max(0.0,float(stored))
            except (TypeError,ValueError): pass
    else:
        end=now or datetime.now(timezone.utc)
        if end.tzinfo is None: end=end.replace(tzinfo=timezone.utc)
        seconds=max(0.0,(end.astimezone(timezone.utc)-started).total_seconds())
    return {
        "tracked": True,
        "active": unplugged is None,
        "seconds": round(seconds,3),
        "started_at": started.isoformat(),
        "unplugged_at": unplugged.isoformat() if unplugged else None,
    }


def transaction_post_session_occupancy(tx_id, now=None):
    with _lock, _connect() as conn:
        row=conn.execute("SELECT * FROM transactions WHERE id=?",(int(tx_id),)).fetchone()
        return _post_session_occupancy_for_row(row,now=now)


def mark_post_session_occupied(charge_point_id, connector_id, observed_at=None):
    """Start restart-safe post-session occupancy tracking for a Finishing connector.

    The timer deliberately starts at the transaction end, not at the first
    Finishing notification. This preserves time across backend restarts and
    delayed/repeated StatusNotification messages.
    """
    now_dt=_parse_iso_utc(observed_at) or datetime.now(timezone.utc)
    result=None
    with _lock,_connect() as conn:
        active=conn.execute("""SELECT 1 FROM transactions
            WHERE charge_point_id=? AND connector_id=? AND status='Active' AND ended_at IS NULL
            LIMIT 1""",(str(charge_point_id),int(connector_id))).fetchone()
        if active:
            return None
        row=conn.execute("""SELECT * FROM transactions
            WHERE charge_point_id=? AND connector_id=? AND ended_at IS NOT NULL
            ORDER BY ended_at DESC,id DESC LIMIT 1""",(str(charge_point_id),int(connector_id))).fetchone()
        if not row:
            return None
        ended=_parse_iso_utc(row["ended_at"])
        if ended is None or now_dt < ended or (now_dt-ended).total_seconds() > 7*24*3600:
            return None
        if "unplugged_at" in row.keys() and row["unplugged_at"]:
            return None
        start=row["post_session_occupied_started_at"] if "post_session_occupied_started_at" in row.keys() else None
        newly_started=False
        if not start:
            start=row["ended_at"]
            conn.execute("""UPDATE transactions
                SET post_session_occupied_started_at=COALESCE(post_session_occupied_started_at,?)
                WHERE id=? AND unplugged_at IS NULL""",(start,int(row["id"])))
            conn.commit()
            newly_started=True
        result={"transaction_id":int(row["id"]),"user_id":row["user_id"],"started_at":start,"new":newly_started}
    return result


def finalize_post_session_occupancy(charge_point_id, connector_id, observed_at=None):
    """Close a tracked Finishing period when the connector becomes Available."""
    unplugged_dt=_parse_iso_utc(observed_at) or datetime.now(timezone.utc)
    result=None
    with _lock,_connect() as conn:
        row=conn.execute("""SELECT * FROM transactions
            WHERE charge_point_id=? AND connector_id=?
              AND post_session_occupied_started_at IS NOT NULL
              AND unplugged_at IS NULL
            ORDER BY ended_at DESC,id DESC LIMIT 1""",(str(charge_point_id),int(connector_id))).fetchone()
        if not row:
            return None
        started=_parse_iso_utc(row["post_session_occupied_started_at"])
        if not started:
            return None
        if unplugged_dt < started:
            unplugged_dt=started
        seconds=max(0.0,(unplugged_dt-started).total_seconds())
        conn.execute("""UPDATE transactions SET unplugged_at=?,post_session_occupied_seconds=?
            WHERE id=? AND unplugged_at IS NULL""",(unplugged_dt.isoformat(),seconds,int(row["id"])))
        conn.commit()
        result={"transaction_id":int(row["id"]),"user_id":row["user_id"],"seconds":round(seconds,3),"unplugged_at":unplugged_dt.isoformat()}
    return result


def stop_transaction(tx, energy_kwh=None, meter_stop_kwh=None, status="Completed", stop_reason=None, ended_at=None):
    """Finish a transaction defensively.

    If the station omits/invalidates meterStop, the last valid Energy.Active.Import.Register
    reading is used as a fallback. This keeps the final session energy consistent without
    inventing data.
    """
    with _lock, _connect() as conn:
        row=conn.execute("SELECT charge_point_id,connector_id,meter_start_kwh,last_meter_kwh FROM transactions WHERE id=?",(tx,)).fetchone()
        if not row:
            return None
        start = row[2]
        last = row[3]
        stop = meter_stop_kwh
        try:
            if stop is not None:
                stop=float(stop)
        except (TypeError, ValueError):
            stop=None
        # Ignore a backwards meterStop and prefer the latest valid meter reading.
        if stop is None or (start is not None and stop < float(start)):
            try:
                stop=float(last) if last is not None else None
            except (TypeError, ValueError):
                stop=None
        session_energy=energy_kwh
        if session_energy is None and stop is not None and start is not None:
            session_energy=max(0.0,float(stop)-float(start))
        end_ts=ended_at or utc_now()
        conn.execute("UPDATE transactions SET ended_at=?, energy_kwh=COALESCE(?,energy_kwh), meter_stop_kwh=COALESCE(?,meter_stop_kwh), last_meter_kwh=COALESCE(?,last_meter_kwh), status=?, stop_reason=COALESCE(?,stop_reason) WHERE id=?",(end_ts,session_energy,stop,stop,status,stop_reason,tx))
        _update_tx_cost_conn(conn, tx)
        timing=_transaction_time_breakdown_conn(conn, tx, now=_parse_iso_utc(end_ts) or datetime.now(timezone.utc))
        if timing:
            conn.execute("UPDATE transactions SET charging_seconds=?, stand_seconds=?, connection_seconds=? WHERE id=?", (timing["charging_seconds"],timing["stand_seconds"],timing["connection_seconds"],tx))
        conn.execute("UPDATE charge_points SET transaction_id=NULL, power_kw=0 WHERE transaction_id=?",(tx,))
        connector_row=conn.execute("SELECT status FROM connectors WHERE charge_point_id=? AND connector_id=?",(row[0],int(row[1] or 0))).fetchone()
        connector_status=str(connector_row[0] or "") if connector_row else ""
        conn.execute("UPDATE connectors SET transaction_id=NULL, power_kw=0, meter_start_kwh=NULL, meter_current_kwh=COALESCE(?,meter_current_kwh) WHERE charge_point_id=? AND connector_id=?",(stop,row[0],int(row[1] or 0)))
        if connector_status == "Finishing":
            conn.execute("""UPDATE transactions
                SET post_session_occupied_started_at=COALESCE(post_session_occupied_started_at,?)
                WHERE id=? AND unplugged_at IS NULL""",(end_ts,int(tx)))
        active=conn.execute("SELECT id FROM transactions WHERE charge_point_id=? AND status='Active' AND ended_at IS NULL ORDER BY id DESC LIMIT 1",(row[0],)).fetchone()
        conn.execute("UPDATE charge_points SET transaction_id=? WHERE id=?",(active[0] if active else None,row[0]))
        if active is None:
            station=conn.execute("SELECT station_status FROM charge_points WHERE id=?",(row[0],)).fetchone()
            if station and str(station[0] or "") == "Available":
                conn.execute("UPDATE connectors SET status='Available' WHERE charge_point_id=? AND connector_id=? AND status IN ('Charging','Finishing','SuspendedEV','SuspendedEVSE')",(row[0],int(row[1] or 0)))
        connector_rows=conn.execute("SELECT connector_id,status FROM connectors WHERE charge_point_id=?",(row[0],)).fetchall()
        station=conn.execute("SELECT station_status FROM charge_points WHERE id=?",(row[0],)).fetchone()
        overall=_overall_status_from_rows(station[0] if station else None, connector_rows)
        conn.execute("UPDATE charge_points SET status=? WHERE id=?",(overall,row[0]))
        conn.commit()
    return session_energy

def active_transactions_for_charge_point(cp_id, limit=20):
    with _lock, _connect() as conn:
        rows=conn.execute("""SELECT t.*, v.name AS vehicle_name, v.make AS vehicle_make, v.model AS vehicle_model, v.plate AS vehicle_plate, v.image_path AS vehicle_image_path,
            u.name AS user_name, u.role AS user_role, u.department AS user_department, u.image_path AS user_image_path, r.uid AS rfid_uid
            FROM transactions t LEFT JOIN vehicles v ON v.id=t.vehicle_id
            LEFT JOIN users u ON u.id=t.user_id LEFT JOIN rfid_cards r ON r.id=t.rfid_card_id
            WHERE t.charge_point_id=? AND t.status='Active' AND t.ended_at IS NULL
            ORDER BY t.id DESC LIMIT ?""",(cp_id,limit)).fetchall()
        result=[dict(r) for r in rows]
        for item in result:
            sample=conn.execute("SELECT * FROM meter_samples WHERE transaction_id=? ORDER BY id DESC LIMIT 1", (item['id'],)).fetchone()
            if sample:
                power, source, _=_derived_power_for_connector(conn, item['charge_point_id'], int(item['connector_id'] or 0), sample)
                measured_at=sample['sampled_at'] or sample['ts']; age=_telemetry_age_seconds(measured_at)
                fresh=bool(age is not None and age <= LIVE_TELEMETRY_MAX_AGE_SECONDS)
                item['last_power_kw']=power; item['live_power_kw']=power if fresh else None; item['live_power_source']=source if fresh else None; item['live_meter_at']=measured_at; item['live_meter_age_seconds']=age
            else:
                item['last_power_kw']=None; item['live_power_kw']=None; item['live_power_source']=None; item['live_meter_at']=None; item['live_meter_age_seconds']=None
        return result

def active_transactions(limit=50):
    with _lock, _connect() as conn:
        rows=conn.execute("""SELECT t.*, v.name AS vehicle_name, v.make AS vehicle_make, v.model AS vehicle_model, v.plate AS vehicle_plate, v.image_path AS vehicle_image_path,
            u.name AS user_name, u.role AS user_role, u.department AS user_department, u.image_path AS user_image_path, r.uid AS rfid_uid
            FROM transactions t LEFT JOIN vehicles v ON v.id=t.vehicle_id
            LEFT JOIN users u ON u.id=t.user_id LEFT JOIN rfid_cards r ON r.id=t.rfid_card_id
            WHERE t.status='Active' AND t.ended_at IS NULL
            ORDER BY t.id DESC LIMIT ?""",(limit,)).fetchall()
        result=[dict(r) for r in rows]
        for item in result:
            sample=conn.execute("SELECT * FROM meter_samples WHERE transaction_id=? ORDER BY id DESC LIMIT 1", (item['id'],)).fetchone()
            if sample:
                power, source, _=_derived_power_for_connector(conn, item['charge_point_id'], int(item['connector_id'] or 0), sample)
                measured_at=sample['sampled_at'] or sample['ts']; age=_telemetry_age_seconds(measured_at)
                fresh=bool(age is not None and age <= LIVE_TELEMETRY_MAX_AGE_SECONDS)
                item['last_power_kw']=power; item['live_power_kw']=power if fresh else None; item['live_power_source']=source if fresh else None; item['live_meter_at']=measured_at; item['live_meter_age_seconds']=age
            else:
                item['last_power_kw']=None; item['live_power_kw']=None; item['live_power_source']=None; item['live_meter_at']=None; item['live_meter_age_seconds']=None
        return result


def list_charge_points():
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM charge_points WHERE COALESCE(onboarded,1)=1 AND COALESCE(ignored,0)=0 AND COALESCE(retired,0)=0 AND COALESCE(archived,0)=0 ORDER BY id").fetchall()]


def get_charge_point(cp_id):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM charge_points WHERE id=?", (cp_id,)).fetchone()
        return dict(row) if row else None


def save_ocpp_capability_snapshot(cp_id, *, status="ok", response_ms=None, profiles=None, capabilities=None, configuration=None, unknown_keys=None):
    checked_at=utc_now()
    payloads={
        "profiles_json":json.dumps(profiles or [],ensure_ascii=False,separators=(",",":")),
        "capabilities_json":json.dumps(capabilities or {},ensure_ascii=False,separators=(",",":")),
        "configuration_json":json.dumps(configuration or {},ensure_ascii=False,separators=(",",":")),
        "unknown_keys_json":json.dumps(unknown_keys or [],ensure_ascii=False,separators=(",",":")),
    }
    with _lock,_connect() as conn:
        conn.execute("""INSERT INTO ocpp_capability_snapshots(
            charge_point_id,checked_at,status,response_ms,profiles_json,capabilities_json,configuration_json,unknown_keys_json
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(charge_point_id) DO UPDATE SET
            checked_at=excluded.checked_at,status=excluded.status,response_ms=excluded.response_ms,
            profiles_json=excluded.profiles_json,capabilities_json=excluded.capabilities_json,
            configuration_json=excluded.configuration_json,unknown_keys_json=excluded.unknown_keys_json""",
            (str(cp_id),checked_at,str(status or "ok"),response_ms,payloads["profiles_json"],payloads["capabilities_json"],payloads["configuration_json"],payloads["unknown_keys_json"]))
        conn.commit()
    return get_ocpp_capability_snapshot(cp_id)


def get_ocpp_capability_snapshot(cp_id):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT * FROM ocpp_capability_snapshots WHERE charge_point_id=?",(str(cp_id),)).fetchone()
    if not row:
        return None
    result=dict(row)
    for source,target,default in (
        ("profiles_json","profiles",[]),
        ("capabilities_json","capabilities",{}),
        ("configuration_json","configuration",{}),
        ("unknown_keys_json","unknown_keys",[]),
    ):
        try:
            result[target]=json.loads(result.pop(source) or "")
        except Exception:
            result[target]=default
    return result


REMOTE_CAPABILITY_COMMANDS = (
    "start","stop","unlock","availability","reset","set_charging_profile","clear_charging_profile",
    "change_configuration","trigger_message","get_diagnostics","update_firmware","reserve","cancel_reservation"
)


def record_remote_capability_result(cp_id, command, *, outcome, status="", detail="", response_ms=None):
    command=str(command or "").strip().lower()
    if command not in REMOTE_CAPABILITY_COMMANDS:
        return None
    outcome=str(outcome or "").strip().lower()
    if outcome not in {"supported","unsupported","rejected","error"}:
        raise ValueError("Ungültiges Remote-Capability-Ergebnis")
    now=utc_now()
    with _lock,_connect() as conn:
        row=conn.execute("SELECT * FROM ocpp_remote_capabilities WHERE charge_point_id=? AND command=?",(str(cp_id),command)).fetchone()
        current=dict(row) if row else {}
        success_count=int(current.get("success_count") or 0)
        rejected_count=int(current.get("rejected_count") or 0)
        not_supported_count=int(current.get("not_supported_count") or 0)
        error_count=int(current.get("error_count") or 0)
        last_success_at=current.get("last_success_at")
        if outcome=="supported":
            state="supported"; success_count+=1; last_success_at=now
        elif outcome=="unsupported":
            state="unsupported"; not_supported_count+=1
        elif outcome=="rejected":
            # A rejected operation proves the station understood the OCPP action.
            # It must never be confused with NotSupported.
            state="supported" if success_count>0 else "available"
            rejected_count+=1
        else:
            # Transport/timeouts do not prove support either way. Preserve a
            # previously learned supported/unsupported state.
            state=str(current.get("state") or "untested")
            error_count+=1
        conn.execute("""INSERT INTO ocpp_remote_capabilities(
            charge_point_id,command,state,last_status,last_detail,last_attempt_at,last_success_at,response_ms,
            success_count,rejected_count,not_supported_count,error_count
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(charge_point_id,command) DO UPDATE SET
            state=excluded.state,last_status=excluded.last_status,last_detail=excluded.last_detail,
            last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
            response_ms=excluded.response_ms,success_count=excluded.success_count,
            rejected_count=excluded.rejected_count,not_supported_count=excluded.not_supported_count,
            error_count=excluded.error_count""",
            (str(cp_id),command,state,str(status or ""),str(detail or "")[:500],now,last_success_at,response_ms,
             success_count,rejected_count,not_supported_count,error_count))
        conn.commit()
        row=conn.execute("SELECT * FROM ocpp_remote_capabilities WHERE charge_point_id=? AND command=?",(str(cp_id),command)).fetchone()
        return dict(row) if row else None


def remote_capability_profile(cp_id):
    with _lock,_connect() as conn:
        rows=conn.execute("SELECT * FROM ocpp_remote_capabilities WHERE charge_point_id=?",(str(cp_id),)).fetchall()
    found={str(r["command"]):dict(r) for r in rows}
    result={}
    for command in REMOTE_CAPABILITY_COMMANDS:
        item=found.get(command) or {
            "charge_point_id":str(cp_id),"command":command,"state":"untested","last_status":"",
            "last_detail":"","last_attempt_at":None,"last_success_at":None,"response_ms":None,
            "success_count":0,"rejected_count":0,"not_supported_count":0,"error_count":0,
        }
        result[command]=item
    return result


def reset_remote_capability_profile(cp_id):
    with _lock,_connect() as conn:
        cur=conn.execute("DELETE FROM ocpp_remote_capabilities WHERE charge_point_id=?",(str(cp_id),))
        conn.commit()
        return int(cur.rowcount or 0)


def add_service_operation(cp_id, operation, status="Requested", summary="", detail="", external_ref="", requested_by=""):
    now=utc_now()
    with _lock,_connect() as conn:
        cur=conn.execute("""INSERT INTO ocpp_service_operations(
            charge_point_id,operation,requested_at,updated_at,status,summary,detail,external_ref,requested_by
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (str(cp_id),str(operation),now,now,str(status or "Requested"),str(summary or "")[:300],
         str(detail or "")[:2000],str(external_ref or "")[:300],str(requested_by or "")[:180]))
        conn.commit()
        return int(cur.lastrowid)


def update_service_operation(operation_id, status, detail=None, external_ref=None, completed=False):
    now=utc_now()
    with _lock,_connect() as conn:
        row=conn.execute("SELECT * FROM ocpp_service_operations WHERE id=?",(int(operation_id),)).fetchone()
        if not row:
            return None
        conn.execute("""UPDATE ocpp_service_operations SET status=?,updated_at=?,
            detail=COALESCE(?,detail),external_ref=COALESCE(?,external_ref),
            completed_at=CASE WHEN ? THEN ? ELSE completed_at END WHERE id=?""",
            (str(status or ""),now,detail,external_ref,1 if completed else 0,now,int(operation_id)))
        conn.commit()
        row=conn.execute("SELECT * FROM ocpp_service_operations WHERE id=?",(int(operation_id),)).fetchone()
        return dict(row) if row else None


def update_latest_service_operation(cp_id, operation, status, detail="", external_ref="", completed=False):
    with _lock,_connect() as conn:
        row=conn.execute("""SELECT id FROM ocpp_service_operations
            WHERE charge_point_id=? AND operation=? ORDER BY id DESC LIMIT 1""",
            (str(cp_id),str(operation))).fetchone()
    if not row:
        return None
    return update_service_operation(int(row["id"]),status,detail or None,external_ref or None,completed)


def service_operations_for_charge_point(cp_id, limit=30):
    try: limit=max(1,min(100,int(limit or 30)))
    except (TypeError,ValueError): limit=30
    with _lock,_connect() as conn:
        return [dict(r) for r in conn.execute("""SELECT * FROM ocpp_service_operations
            WHERE charge_point_id=? ORDER BY id DESC LIMIT ?""",(str(cp_id),limit)).fetchall()]


def next_reservation_id():
    with _lock,_connect() as conn:
        row=conn.execute("SELECT COALESCE(MAX(reservation_id),0)+1 FROM ocpp_reservations").fetchone()
        return max(1,int(row[0] or 1))


def save_ocpp_reservation(reservation_id, cp_id, connector_id, id_tag, expiry_date, status):
    now=utc_now()
    with _lock,_connect() as conn:
        conn.execute("""INSERT INTO ocpp_reservations(
            reservation_id,charge_point_id,connector_id,id_tag,expiry_date,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(reservation_id) DO UPDATE SET
            charge_point_id=excluded.charge_point_id,connector_id=excluded.connector_id,
            id_tag=excluded.id_tag,expiry_date=excluded.expiry_date,status=excluded.status,
            updated_at=excluded.updated_at""",
            (int(reservation_id),str(cp_id),int(connector_id),str(id_tag),str(expiry_date),str(status),now,now))
        conn.commit()
        row=conn.execute("SELECT * FROM ocpp_reservations WHERE reservation_id=?",(int(reservation_id),)).fetchone()
        return dict(row) if row else None


def set_ocpp_reservation_status(reservation_id, status):
    now=utc_now()
    with _lock,_connect() as conn:
        cur=conn.execute("UPDATE ocpp_reservations SET status=?,updated_at=? WHERE reservation_id=?",
            (str(status),now,int(reservation_id)))
        conn.commit()
        return int(cur.rowcount or 0)>0


def ocpp_reservations_for_charge_point(cp_id, limit=20):
    try: limit=max(1,min(100,int(limit or 20)))
    except (TypeError,ValueError): limit=20
    with _lock,_connect() as conn:
        return [dict(r) for r in conn.execute("""SELECT * FROM ocpp_reservations
            WHERE charge_point_id=? ORDER BY updated_at DESC LIMIT ?""",(str(cp_id),limit)).fetchall()]


def events_for_charge_point(cp_id, limit=80):
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM events WHERE charge_point_id=? ORDER BY id DESC LIMIT ?", (cp_id, limit)).fetchall()]


def transactions_for_charge_point(cp_id, limit=30):
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM transactions WHERE charge_point_id=? ORDER BY id DESC LIMIT ?", (cp_id, limit)).fetchall()]


def recent_events(limit=40):
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def recent_transactions(limit=20):
    with _lock, _connect() as conn:
        rows=conn.execute("""SELECT t.*, v.name AS vehicle_name, v.plate AS vehicle_plate, u.name AS user_name,
            CASE WHEN t.started_at IS NOT NULL THEN
              MAX(0, (julianday(COALESCE(t.ended_at, CURRENT_TIMESTAMP))-julianday(t.started_at))*24*60)
            ELSE NULL END AS duration_minutes,
            (SELECT AVG(ms.power_kw) FROM meter_samples ms WHERE ms.transaction_id=t.id AND ms.power_kw IS NOT NULL) AS avg_power_kw,
            (SELECT MAX(ms.power_kw) FROM meter_samples ms WHERE ms.transaction_id=t.id AND ms.power_kw IS NOT NULL) AS sampled_peak_kw
            FROM transactions t
            LEFT JOIN vehicles v ON v.id=t.vehicle_id
            LEFT JOIN users u ON u.id=t.user_id
            ORDER BY t.id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]



def get_user_by_rfid(rfid):
    if not rfid: return None
    with _lock, _connect() as conn:
        row=conn.execute("""SELECT u.*, r.id AS rfid_card_id, r.uid AS rfid_uid, r.vehicle_id AS rfid_vehicle_id, r.status AS rfid_status, r.expires_at AS rfid_expires_at, r.blocked_reason AS rfid_blocked_reason FROM rfid_cards r LEFT JOIN users u ON u.id=r.user_id WHERE r.uid=? LIMIT 1""",(rfid,)).fetchone()
        if row: return dict(row)
        row=conn.execute("SELECT * FROM users WHERE rfid=? LIMIT 1",(rfid,)).fetchone()
        return dict(row) if row else None

def authorization_decision(rfid,charge_point_id=None):
    user=get_user_by_rfid(rfid)
    if not user:
        return {"accepted":False,"ocpp_status":"Invalid","reason":"RFID unbekannt","user":None,"budget":None}
    if user.get("rfid_status") not in (None,"Aktiv"):
        return {"accepted":False,"ocpp_status":"Blocked","reason":user.get("rfid_blocked_reason") or "RFID-Karte nicht aktiv","user":user,"budget":None}
    expires=str(user.get("rfid_expires_at") or "")[:10]
    if expires and expires < datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat():
        return {"accepted":False,"ocpp_status":"Expired","reason":"RFID-Karte abgelaufen","user":user,"budget":None}
    if user.get("status","Aktiv") != "Aktiv":
        return {"accepted":False,"ocpp_status":"Blocked","reason":"Benutzer nicht aktiv","user":user,"budget":None}
    if charge_point_id not in (None,"") and not user_may_charge_at(user.get("id"),charge_point_id):
        return {"accepted":False,"ocpp_status":"Blocked","reason":f"Benutzer für Ladepunkt {charge_point_id} nicht freigegeben","user":user,"budget":None}
    budget=user_monthly_budget(user.get("id")) if user.get("id") is not None else None
    if budget and budget.get("blocked"):
        return {"accepted":False,"ocpp_status":"Blocked","reason":"Monatliches kWh-Limit erreicht","user":user,"budget":budget}
    reason="Autorisierung gültig"
    if budget and budget.get("status") in {"warning","critical","exceeded"}:
        reason=f"Autorisierung gültig; Monatslimit {budget.get('percent')} %"
    return {"accepted":True,"ocpp_status":"Accepted","reason":reason,"user":user,"budget":budget}

def get_transaction(tx_id):
    with _lock, _connect() as conn:
        row=conn.execute("""SELECT t.*, v.name AS vehicle_name, v.make AS vehicle_make, v.model AS vehicle_model,
                      v.plate AS vehicle_plate, v.image_path AS vehicle_image_path,
                      u.name AS user_name, u.role AS user_role, r.uid AS rfid_uid, r.label AS rfid_label, r.status AS rfid_status,
                      (SELECT AVG(ms.power_kw) FROM meter_samples ms WHERE ms.transaction_id=t.id AND ms.power_kw IS NOT NULL) AS avg_power_kw,
                      (SELECT MAX(ms.power_kw) FROM meter_samples ms WHERE ms.transaction_id=t.id AND ms.power_kw IS NOT NULL) AS sampled_peak_kw
               FROM transactions t LEFT JOIN vehicles v ON v.id=t.vehicle_id
               LEFT JOIN users u ON u.id=t.user_id LEFT JOIN rfid_cards r ON r.id=t.rfid_card_id
               WHERE t.id=?""",(tx_id,)).fetchone()
        return dict(row) if row else None
def set_transaction_vehicle(tx_id, vehicle_id):
    with _lock, _connect() as conn:
        if vehicle_id is not None and not conn.execute("SELECT id FROM vehicles WHERE id=? AND active=1", (vehicle_id,)).fetchone():
            return False
        cur = conn.execute("UPDATE transactions SET vehicle_id=? WHERE id=?", (vehicle_id, tx_id))
        conn.commit()
        return cur.rowcount > 0


def search_transactions(q="", status="", charge_point="", id_tag="", from_date="", to_date="", page=1, page_size=20, sort_key="id", sort_dir="desc"):
    page = max(1, int(page or 1))
    page_size = min(5000, max(1, int(page_size or 20)))
    allowed_sort = {
        "id": "t.id",
        "started_at": "t.started_at",
        "ended_at": "t.ended_at",
        "charge_point_id": "t.charge_point_id",
        "id_tag": "t.id_tag",
        "transaction_id": "t.transaction_id",
        "vehicle": "COALESCE(v.name, '')",
        "user": "COALESCE(u.name, '')",
        "energy_kwh": "t.energy_kwh",
        "max_power_kw": "t.max_power_kw",
        "status": "t.status",
        "duration": "(julianday(COALESCE(t.ended_at, CURRENT_TIMESTAMP))-julianday(t.started_at))",
        "charging_seconds": "t.charging_seconds",
        "stand_seconds": "t.stand_seconds",
        "connection_seconds": "t.connection_seconds",
    }
    order_expr = allowed_sort.get(sort_key, "id")
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    with _lock, _connect() as conn:
        where = ["1=1"]
        params = []
        if status:
            where.append("t.status=?")
            params.append(status)
        if charge_point:
            where.append("t.charge_point_id=?")
            params.append(charge_point)
        if id_tag:
            where.append("t.id_tag=?")
            params.append(id_tag)
        if q:
            where.append("(CAST(t.id AS TEXT) LIKE ? OR t.charge_point_id LIKE ? OR t.id_tag LIKE ? OR COALESCE(t.transaction_id,'') LIKE ? OR COALESCE(v.name,'') LIKE ? OR COALESCE(v.plate,'') LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like, like, like, like])
        if from_date:
            where.append("substr(t.started_at,1,10) >= ?")
            params.append(from_date)
        if to_date:
            where.append("substr(t.started_at,1,10) <= ?")
            params.append(to_date)
        where_sql = " AND ".join(where)
        total = conn.execute(f"SELECT COUNT(*) AS n FROM transactions t LEFT JOIN vehicles v ON v.id=t.vehicle_id LEFT JOIN users u ON u.id=t.user_id WHERE {where_sql}", params).fetchone()["n"]
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"SELECT t.*, v.name AS vehicle_name, v.plate AS vehicle_plate, v.image_path AS vehicle_image_path, u.name AS user_name FROM transactions t LEFT JOIN vehicles v ON v.id=t.vehicle_id LEFT JOIN users u ON u.id=t.user_id WHERE {where_sql} ORDER BY {order_expr} {direction}, t.id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()
        return [dict(r) for r in rows], int(total)

def meter_samples_for_transaction(tx_id, limit=240):
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM meter_samples WHERE transaction_id=? ORDER BY id DESC LIMIT ?", (tx_id, limit)
        ).fetchall()]

def list_vehicles():
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT * FROM vehicles WHERE active=1 ORDER BY name").fetchall()
        return [dict(r) for r in rows]



def get_vehicle(vehicle_id):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        return dict(row) if row else None


def set_vehicle_image(vehicle_id, image_path):
    with _lock, _connect() as conn:
        cur = conn.execute("UPDATE vehicles SET image_path=? WHERE id=?", (image_path, vehicle_id))
        conn.commit()
        return cur.rowcount > 0



def create_vehicle(name, make=None, model=None, plate=None, battery_kwh=None, ac_power_kw=None, dc_power_kw=None, range_km=None, drivetrain=None, assigned_charge_point=None, driver=None):
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO vehicles(name,make,model,plate,battery_kwh,ac_power_kw,dc_power_kw,range_km,drivetrain,assigned_charge_point,driver,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (name, make, model, plate, battery_kwh, ac_power_kw, dc_power_kw, range_km, drivetrain, assigned_charge_point, driver),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_vehicle(vehicle_id, **fields):
    allowed = {"name", "make", "model", "plate", "battery_kwh", "assigned_charge_point", "driver"}
    updates = [(k, fields[k]) for k in allowed if k in fields]
    if not updates:
        return False
    with _lock, _connect() as conn:
        exists = conn.execute("SELECT id FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if not exists:
            return False
        sql = "UPDATE vehicles SET " + ", ".join(f"{k}=?" for k, _ in updates) + " WHERE id=?"
        conn.execute(sql, [v for _, v in updates] + [vehicle_id])
        conn.commit()
        return True


def deactivate_vehicle(vehicle_id):
    with _lock, _connect() as conn:
        cur = conn.execute("UPDATE vehicles SET active=0, assigned_charge_point=NULL WHERE id=?", (vehicle_id,))
        conn.commit()
        return cur.rowcount > 0


def vehicle_users(vehicle_id):
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT u.*, uv.primary_vehicle, uv.assigned_at
               FROM user_vehicles uv JOIN users u ON u.id=uv.user_id
               WHERE uv.vehicle_id=? ORDER BY uv.primary_vehicle DESC, u.name""", (vehicle_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def assign_vehicle_user(vehicle_id, user_id, primary=False):
    with _lock, _connect() as conn:
        if not conn.execute("SELECT id FROM vehicles WHERE id=? AND active=1", (vehicle_id,)).fetchone():
            return False
        if not conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
            return False
        if primary:
            conn.execute("UPDATE user_vehicles SET primary_vehicle=0 WHERE vehicle_id=?", (vehicle_id,))
        conn.execute(
            "INSERT OR IGNORE INTO user_vehicles(user_id,vehicle_id,primary_vehicle,assigned_at) VALUES(?,?,?,?)",
            (user_id, vehicle_id, 1 if primary else 0, utc_now()),
        )
        conn.commit()
        return True


def unassign_vehicle_user(vehicle_id, user_id):
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM user_vehicles WHERE vehicle_id=? AND user_id=?", (vehicle_id, user_id))
        conn.commit()
        return cur.rowcount > 0


def vehicle_details(vehicle_id):
    with _lock, _connect() as conn:
        vehicle = conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if not vehicle:
            return None
        v = dict(vehicle)
        tx_rows = conn.execute(
            """SELECT t.*, v2.name AS vehicle_name, v2.plate AS vehicle_plate, v2.image_path AS vehicle_image_path
               FROM transactions t LEFT JOIN vehicles v2 ON v2.id=t.vehicle_id
               WHERE t.vehicle_id=? ORDER BY t.id DESC LIMIT 100""", (vehicle_id,)
        ).fetchall()
        txs = [dict(r) for r in tx_rows]
        stats = conn.execute(
            """SELECT COUNT(*) AS sessions, COALESCE(SUM(energy_kwh),0) AS energy,
                      COALESCE(AVG(max_power_kw),0) AS avg_peak,
                      COALESCE(SUM(COALESCE(cost_cents,0)),0)/100.0 AS costs
               FROM transactions WHERE vehicle_id=?""", (vehicle_id,)
        ).fetchone()
        return {"vehicle": v, "transactions": txs, "stats": dict(stats)}

def _rfid_local_entry_conn(conn, uid, charge_point_id=None):
    uid=str(uid or "").strip()
    if not uid:
        return None
    row=conn.execute("""SELECT r.uid,r.status,r.expires_at,u.id AS user_id,u.status AS user_status,u.monthly_kwh_limit,u.monthly_limit_mode,u.charge_access_mode
        FROM rfid_cards r LEFT JOIN users u ON u.id=r.user_id WHERE r.uid=?""",(uid,)).fetchone()
    if not row or str(row[1] or "")!="Aktiv" or str(row[4] or "")!="Aktiv":
        return None
    if charge_point_id not in (None,"") and _normalize_charge_access_mode(row[7])=="selected":
        if row[3] is None or not conn.execute("SELECT 1 FROM user_charge_point_access WHERE user_id=? AND charge_point_id=?",(int(row[3]),str(charge_point_id))).fetchone():
            return None
    expires=str(row[2] or "").strip()
    if expires:
        try:
            exp_date=datetime.fromisoformat(expires.replace("Z","+00:00")).date() if "T" in expires else datetime.strptime(expires[:10],"%Y-%m-%d").date()
            if exp_date < datetime.now(timezone.utc).date():
                return None
        except (TypeError,ValueError):
            pass
    user_id=row[3]
    if user_id is not None and str(row[6] or "warn")=="block" and row[5] is not None:
        try:
            start_utc,end_utc,_=_month_bounds_utc()
            used=_user_month_energy_conn(conn,int(user_id),start_utc,end_utc)
            state=_budget_status(row[5],used,row[6])
            if state.get("blocked"):
                return None
        except Exception:
            pass
    info={"status":"Accepted"}
    if expires:
        info["expiryDate"]=(expires if "T" in expires else expires[:10]+"T23:59:59Z")
    return {"idTag":uid,"idTagInfo":info}

def _rfid_local_list_bump_conn(conn, uids):
    clean=[]
    for uid in uids or []:
        uid=str(uid or "").strip()
        if uid and uid not in clean:
            clean.append(uid)
    if not clean:
        return None
    row=conn.execute("SELECT value FROM app_settings WHERE key='rfid_local_list_version'").fetchone()
    try: version=max(1,int(row[0]))+1 if row else 2
    except (TypeError,ValueError): version=2
    now=utc_now()
    conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('rfid_local_list_version',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(str(version),now))
    for uid in clean:
        entry=_rfid_local_entry_conn(conn,uid) or {"idTag":uid}
        conn.execute("INSERT INTO rfid_local_list_changes(version,uid,payload_json,created_at) VALUES(?,?,?,?)",(version,uid,json.dumps(entry,ensure_ascii=True,separators=(",",":")),now))
    conn.execute("""UPDATE charge_point_local_list_state SET backend_version=?,
        pending=CASE WHEN supported=0 THEN 0 ELSE 1 END,
        status=CASE WHEN supported=0 THEN status ELSE 'Ausstehend' END WHERE 1=1""",(version,))
    conn.execute("""INSERT OR IGNORE INTO charge_point_local_list_state(charge_point_id,backend_version,pending,status)
        SELECT id,?,1,'Ausstehend' FROM charge_points WHERE COALESCE(onboarded,1)=1 AND COALESCE(ignored,0)=0 AND COALESCE(retired,0)=0 AND COALESCE(archived,0)=0""",(version,))
    return version

def rfid_local_list_version():
    try: return max(1,int(get_setting('rfid_local_list_version','1') or 1))
    except (TypeError,ValueError): return 1

def rfid_local_list_full(charge_point_id=None):
    with _lock,_connect() as conn:
        rows=conn.execute("SELECT uid FROM rfid_cards ORDER BY uid").fetchall()
        result=[]
        for row in rows:
            entry=_rfid_local_entry_conn(conn,row[0],charge_point_id)
            if entry: result.append(entry)
        return result

def rfid_local_list_changes_for_version(version,charge_point_id=None):
    with _lock,_connect() as conn:
        rows=conn.execute("SELECT uid,payload_json FROM rfid_local_list_changes WHERE version=? ORDER BY id",(int(version),)).fetchall()
        result=[]
        for row in rows:
            if charge_point_id not in (None,""):
                entry=_rfid_local_entry_conn(conn,row["uid"],charge_point_id)
                result.append(entry or {"idTag":str(row["uid"])})
            else:
                try: result.append(json.loads(row["payload_json"]))
                except Exception: pass
        return result

def ensure_local_list_state(cp_id):
    cp_id=str(cp_id)
    version=rfid_local_list_version()
    with _lock,_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO charge_point_local_list_state(charge_point_id,backend_version,pending,status) VALUES(?,?,1,'Ausstehend')",(cp_id,version))
        conn.execute("UPDATE charge_point_local_list_state SET backend_version=? WHERE charge_point_id=?",(version,cp_id))
        conn.commit()
        row=conn.execute("SELECT * FROM charge_point_local_list_state WHERE charge_point_id=?",(cp_id,)).fetchone()
        return dict(row) if row else None

def set_local_list_state(cp_id, *, station_version=None, station_entry_count=None, pending=None, supported=None, status=None, update_type=None, response=None, synced=False):
    cp_id=str(cp_id); version=rfid_local_list_version(); now=utc_now()
    with _lock,_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO charge_point_local_list_state(charge_point_id,backend_version,pending,status) VALUES(?,?,1,'Ausstehend')",(cp_id,version))
        parts=["backend_version=?","last_attempt_at=?"]; vals=[version,now]
        if station_version is not None: parts.append("station_version=?"); vals.append(int(station_version))
        if station_entry_count is not None: parts.append("station_entry_count=?"); vals.append(max(0,int(station_entry_count)))
        if pending is not None: parts.append("pending=?"); vals.append(1 if pending else 0)
        if supported is not None: parts.append("supported=?"); vals.append(1 if supported else 0)
        if status is not None: parts.append("status=?"); vals.append(str(status))
        if update_type is not None: parts.append("last_update_type=?"); vals.append(str(update_type))
        if response is not None: parts.append("last_response=?"); vals.append(str(response)[:500])
        if synced: parts.append("last_sync_at=?"); vals.append(now)
        vals.append(cp_id)
        conn.execute("UPDATE charge_point_local_list_state SET "+", ".join(parts)+" WHERE charge_point_id=?",vals)
        conn.commit()
        row=conn.execute("SELECT * FROM charge_point_local_list_state WHERE charge_point_id=?",(cp_id,)).fetchone()
        return dict(row) if row else None

def set_local_list_offline_auth_state(cp_id, status, detail=None, readonly=None):
    cp_id=str(cp_id); version=rfid_local_list_version(); now=utc_now()
    with _lock,_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO charge_point_local_list_state(charge_point_id,backend_version,pending,status) VALUES(?,?,1,'Ausstehend')",(cp_id,version))
        conn.execute("""UPDATE charge_point_local_list_state
            SET offline_auth_status=?,offline_auth_detail=?,offline_auth_readonly=?,offline_auth_checked_at=?
            WHERE charge_point_id=?""",
            (str(status or "Unbekannt"),None if detail is None else str(detail)[:500],None if readonly is None else (1 if readonly else 0),now,cp_id))
        conn.commit()
        row=conn.execute("SELECT * FROM charge_point_local_list_state WHERE charge_point_id=?",(cp_id,)).fetchone()
        return dict(row) if row else None


def local_list_send_was_accepted(cp_id):
    with _lock,_connect() as conn:
        row=conn.execute("""SELECT 1 FROM events
            WHERE charge_point_id=? AND event_type='SendLocalList' AND LOWER(payload) LIKE '%response=accepted%'
            ORDER BY id DESC LIMIT 1""",(str(cp_id),)).fetchone()
        return bool(row)

def local_list_states():
    version=rfid_local_list_version()
    with _lock,_connect() as conn:
        cps=conn.execute("""SELECT id,location,vendor,model,last_seen,status FROM charge_points
            WHERE COALESCE(onboarded,1)=1 AND COALESCE(ignored,0)=0 AND COALESCE(retired,0)=0 AND COALESCE(archived,0)=0 ORDER BY COALESCE(NULLIF(TRIM(location),''),id),id""").fetchall()
        result=[]
        for cp in cps:
            conn.execute("INSERT OR IGNORE INTO charge_point_local_list_state(charge_point_id,backend_version,pending,status) VALUES(?,?,1,'Ausstehend')",(cp['id'],version))
            st=conn.execute("SELECT * FROM charge_point_local_list_state WHERE charge_point_id=?",(cp['id'],)).fetchone()
            item=dict(cp); item.update(dict(st) if st else {}); item['backend_version']=version
            result.append(item)
        conn.commit()
        return result

def local_list_uids_for_user(user_id):
    with _lock,_connect() as conn:
        return [str(r[0]) for r in conn.execute("SELECT uid FROM rfid_cards WHERE user_id=?",(int(user_id),)).fetchall()]

def refresh_local_list_for_user(user_id):
    with _lock,_connect() as conn:
        uids=[str(r[0]) for r in conn.execute("SELECT uid FROM rfid_cards WHERE user_id=?",(int(user_id),)).fetchall()]
        version=_rfid_local_list_bump_conn(conn,uids) if uids else None
        conn.commit()
        return version

def refresh_local_list_for_month(month_key=None):
    month_key=month_key or datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m")
    with _lock,_connect() as conn:
        row=conn.execute("SELECT value FROM app_settings WHERE key='rfid_local_list_budget_month'").fetchone()
        if row and str(row[0])==str(month_key): return False
        uids=[str(r[0]) for r in conn.execute("SELECT uid FROM rfid_cards").fetchall()]
        _rfid_local_list_bump_conn(conn,uids) if uids else None
        conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('rfid_local_list_budget_month',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(str(month_key),utc_now()))
        conn.commit(); return True

def get_user_by_email(email):
    key=str(email or "").strip().casefold()
    if not key: return None
    with _connect() as conn:
        row=conn.execute("SELECT * FROM users WHERE LOWER(TRIM(COALESCE(email,'')))=? ORDER BY id LIMIT 1",(key,)).fetchone()
        return dict(row) if row else None

def get_user(user_id):
    with _lock, _connect() as conn:
        row=conn.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone(); return dict(row) if row else None


def set_user_image(user_id, image_path):
    with _lock,_connect() as conn:
        cur=conn.execute("UPDATE users SET image_path=? WHERE id=?",(image_path,int(user_id)))
        conn.commit()
        return cur.rowcount>0


def _normalize_charge_access_mode(value):
    return "selected" if str(value or "").strip().lower()=="selected" else "all"


def _set_user_charge_access_conn(conn,user_id,mode,charge_point_ids=None):
    mode=_normalize_charge_access_mode(mode)
    ids=[]
    for cp_id in charge_point_ids or []:
        cp_id=str(cp_id or "").strip()
        if cp_id and cp_id not in ids:
            ids.append(cp_id)
    if mode=="selected" and ids:
        marks=",".join("?" for _ in ids)
        found={str(r[0]) for r in conn.execute(f"""SELECT id FROM charge_points WHERE id IN ({marks})
            AND COALESCE(onboarded,1)=1 AND COALESCE(ignored,0)=0 AND COALESCE(retired,0)=0 AND COALESCE(archived,0)=0""",ids).fetchall()}
        missing=[x for x in ids if x not in found]
        if missing:
            raise ValueError("Unbekannte oder nicht aktive Ladepunkte: "+", ".join(missing))
    conn.execute("UPDATE users SET charge_access_mode=? WHERE id=?",(mode,int(user_id)))
    conn.execute("DELETE FROM user_charge_point_access WHERE user_id=?",(int(user_id),))
    if mode=="selected":
        now=utc_now()
        conn.executemany("INSERT INTO user_charge_point_access(user_id,charge_point_id,assigned_at) VALUES(?,?,?)",
                         [(int(user_id),cp_id,now) for cp_id in ids])
    return ids


def user_charge_access(user_id):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT charge_access_mode FROM users WHERE id=?",(int(user_id),)).fetchone()
        if not row: return None
        ids=[str(r[0]) for r in conn.execute("SELECT charge_point_id FROM user_charge_point_access WHERE user_id=? ORDER BY charge_point_id",(int(user_id),)).fetchall()]
        return {"mode":_normalize_charge_access_mode(row[0]),"charge_point_ids":ids}


def user_may_charge_at(user_id,charge_point_id):
    if charge_point_id in (None,""):
        return True
    with _lock,_connect() as conn:
        row=conn.execute("SELECT charge_access_mode FROM users WHERE id=?",(int(user_id),)).fetchone()
        if not row: return False
        if _normalize_charge_access_mode(row[0])=="all":
            return True
        return bool(conn.execute("SELECT 1 FROM user_charge_point_access WHERE user_id=? AND charge_point_id=?",(int(user_id),str(charge_point_id))).fetchone())


def create_user(name,role="Fahrer",department=None,email=None,phone=None,status="Aktiv",monthly_kwh_limit=None,monthly_limit_mode="warn",charge_access_mode="all",allowed_charge_point_ids=None):
    limit_value = None if monthly_kwh_limit in (None, "") else max(0.0, float(monthly_kwh_limit))
    with _lock,_connect() as conn:
        mode = "block" if str(monthly_limit_mode).lower() == "block" else "warn"
        access_mode=_normalize_charge_access_mode(charge_access_mode)
        cur=conn.execute(
            "INSERT INTO users(name,role,department,status,email,phone,monthly_kwh_limit,monthly_limit_mode,charge_access_mode) VALUES(?,?,?,?,?,?,?,?,?)",
            (name,role,department,status,email,phone,limit_value,mode,access_mode),
        )
        user_id=int(cur.lastrowid)
        _set_user_charge_access_conn(conn,user_id,access_mode,allowed_charge_point_ids)
        conn.commit()
        return user_id

def update_user(user_id,**fields):
    access_mode=fields.pop("charge_access_mode",None)
    access_ids=fields.pop("allowed_charge_point_ids",None)
    allowed={"name","role","department","email","phone","status","monthly_kwh_limit","monthly_limit_mode"}
    updates=[]
    for k in allowed:
        if k not in fields:
            continue
        value=fields[k]
        if k == "monthly_kwh_limit":
            value = None if value in (None, "") else max(0.0, float(value))
        elif k == "monthly_limit_mode":
            value = "block" if str(value).lower() == "block" else "warn"
        updates.append((k,value))
    if not updates and access_mode is None and access_ids is None:
        return False
    with _lock,_connect() as conn:
        if not conn.execute("SELECT id FROM users WHERE id=?",(user_id,)).fetchone():
            return False
        card_uids=[str(r[0]) for r in conn.execute("SELECT uid FROM rfid_cards WHERE user_id=?",(user_id,)).fetchall()]
        if updates:
            conn.execute("UPDATE users SET "+", ".join(f"{k}=?" for k,_ in updates)+" WHERE id=?",[v for _,v in updates]+[user_id])
        access_changed=access_mode is not None or access_ids is not None
        if access_changed:
            current_access=conn.execute("SELECT charge_access_mode FROM users WHERE id=?",(user_id,)).fetchone()
            effective_mode=_normalize_charge_access_mode(access_mode if access_mode is not None else (current_access[0] if current_access else "all"))
            if access_ids is None:
                access_ids=[str(r[0]) for r in conn.execute("SELECT charge_point_id FROM user_charge_point_access WHERE user_id=?",(user_id,)).fetchall()]
            _set_user_charge_access_conn(conn,user_id,effective_mode,access_ids)
        if (any(k in {"status","monthly_kwh_limit","monthly_limit_mode"} for k,_ in updates) or access_changed) and card_uids:
            _rfid_local_list_bump_conn(conn,card_uids)
        conn.commit()
        return True

def deactivate_user(user_id):
    with _lock,_connect() as conn:
        if not conn.execute("SELECT id FROM users WHERE id=?",(user_id,)).fetchone():return False
        card_uids=[str(r[0]) for r in conn.execute("SELECT uid FROM rfid_cards WHERE user_id=?",(user_id,)).fetchall()]
        conn.execute("UPDATE users SET status='Inaktiv' WHERE id=?",(user_id,)); conn.execute("UPDATE rfid_cards SET status='Deaktiviert' WHERE user_id=?",(user_id,))
        if card_uids: _rfid_local_list_bump_conn(conn,card_uids)
        conn.commit(); return True

def _user_delete_check_conn(conn, user_id):
    uid=int(user_id)
    user=conn.execute("SELECT id,name,rfid,status FROM users WHERE id=?",(uid,)).fetchone()
    if not user:
        return None
    card_rows=conn.execute("SELECT id,uid FROM rfid_cards WHERE user_id=?",(uid,)).fetchall()
    card_ids=[int(r["id"]) for r in card_rows]
    id_tags=[str(r["uid"]).strip() for r in card_rows if str(r["uid"] or "").strip()]
    legacy=str(user["rfid"] or "").strip()
    if legacy and legacy not in id_tags:
        id_tags.append(legacy)
    tx_where=["user_id=?"]; tx_args=[uid]
    if card_ids:
        marks=",".join("?" for _ in card_ids)
        tx_where.append(f"(user_id IS NULL AND rfid_card_id IN ({marks}))"); tx_args.extend(card_ids)
    if id_tags:
        marks=",".join("?" for _ in id_tags)
        tx_where.append(f"(user_id IS NULL AND id_tag IN ({marks}))"); tx_args.extend(id_tags)
    tx_rows=conn.execute("SELECT id,status,ended_at FROM transactions WHERE "+(" OR ".join(tx_where)),tx_args).fetchall()
    transaction_ids=[int(r["id"]) for r in tx_rows]
    transactions=len(transaction_ids)
    active_transactions=sum(1 for r in tx_rows if str(r["status"] or "")=="Active" and not r["ended_at"])
    meter_samples=transaction_events=diagnostic_events=0
    if transaction_ids:
        marks=",".join("?" for _ in transaction_ids)
        meter_samples=int(conn.execute(f"SELECT COUNT(*) FROM meter_samples WHERE transaction_id IN ({marks})",transaction_ids).fetchone()[0] or 0)
        transaction_events=int(conn.execute(f"SELECT COUNT(*) FROM events WHERE transaction_id IN ({marks})",transaction_ids).fetchone()[0] or 0)
        diagnostic_events=int(conn.execute(f"SELECT COUNT(*) FROM diagnostic_events WHERE transaction_id IN ({marks})",transaction_ids).fetchone()[0] or 0)
    return {
        "user_id":uid,"name":user["name"],"status":user["status"],
        "can_delete":transactions==0,"can_purge":active_transactions==0,
        "active_transactions":active_transactions,
        "blockers":({"transactions":transactions} if transactions else {}),
        "reasons":([f"Ladevorgänge: {transactions}"] if transactions else []),
        "purge_reasons":([f"Aktive Ladevorgänge: {active_transactions}"] if active_transactions else []),
        "removable":{
            "transactions":transactions,"meter_samples":meter_samples,
            "transaction_events":transaction_events,"diagnostic_events":diagnostic_events,
            "rfid_cards":len(card_ids),
            "vehicle_links":int(conn.execute("SELECT COUNT(*) FROM user_vehicles WHERE user_id=?",(uid,)).fetchone()[0] or 0),
            "charge_point_links":int(conn.execute("SELECT COUNT(*) FROM user_charge_point_access WHERE user_id=?",(uid,)).fetchone()[0] or 0),
        },
        "card_ids":card_ids,"card_uids":id_tags,"transaction_ids":transaction_ids,
    }

def _public_user_delete_check(check):
    if check is None:
        return None
    hidden={"card_ids","card_uids","transaction_ids"}
    return {k:v for k,v in check.items() if k not in hidden}

def user_delete_check(user_id):
    with _lock,_connect() as conn:
        return _public_user_delete_check(_user_delete_check_conn(conn,user_id))

def _delete_user_core_links_conn(conn, uid, card_ids):
    if card_ids:
        marks=",".join("?" for _ in card_ids)
        conn.execute(f"DELETE FROM rfid_card_events WHERE card_id IN ({marks})",card_ids)
        conn.execute(f"UPDATE rfid_cards SET replacement_for_id=NULL WHERE replacement_for_id IN ({marks})",card_ids)
    conn.execute("DELETE FROM rfid_cards WHERE user_id=?",(uid,))
    conn.execute("DELETE FROM user_vehicles WHERE user_id=?",(uid,))
    conn.execute("DELETE FROM user_charge_point_access WHERE user_id=?",(uid,))
    conn.execute("DELETE FROM users WHERE id=?",(uid,))

def delete_user_permanently(user_id):
    with _lock,_connect() as conn:
        check=_user_delete_check_conn(conn,user_id)
        if check is None:
            return False,"not_found",None
        if not check["can_delete"]:
            return False,"history",_public_user_delete_check(check)
        uid=int(user_id); card_ids=list(check.get("card_ids") or []); card_uids=list(check.get("card_uids") or [])
        try:
            _delete_user_core_links_conn(conn,uid,card_ids)
            if card_uids: _rfid_local_list_bump_conn(conn,card_uids)
            conn.commit()
        except Exception:
            conn.rollback(); raise
        return True,"deleted",_public_user_delete_check(check)

def purge_user_with_history(user_id):
    """Destructive admin-only cleanup for explicit test/error data."""
    with _lock,_connect() as conn:
        check=_user_delete_check_conn(conn,user_id)
        if check is None:
            return False,"not_found",None
        if not check["can_purge"]:
            return False,"unsafe",_public_user_delete_check(check)
        uid=int(user_id); card_ids=list(check.get("card_ids") or []); card_uids=list(check.get("card_uids") or [])
        transaction_ids=list(check.get("transaction_ids") or [])
        try:
            if transaction_ids:
                marks=",".join("?" for _ in transaction_ids)
                conn.execute(f"UPDATE charge_points SET transaction_id=NULL,power_kw=0 WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"UPDATE connectors SET transaction_id=NULL,power_kw=0 WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"DELETE FROM meter_samples WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"DELETE FROM events WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"DELETE FROM diagnostic_events WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"UPDATE diagnostic_states SET transaction_id=NULL WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"DELETE FROM transactions WHERE id IN ({marks})",transaction_ids)
            _delete_user_core_links_conn(conn,uid,card_ids)
            if card_uids: _rfid_local_list_bump_conn(conn,card_uids)
            conn.commit()
        except Exception:
            conn.rollback(); raise
        return True,"purged",_public_user_delete_check(check)

def _rfid_log_event_conn(conn, card_id, event_type, old_status=None, new_status=None, note=None, source="admin"):
    conn.execute("INSERT INTO rfid_card_events(card_id,event_type,old_status,new_status,note,source,created_at) VALUES(?,?,?,?,?,?,?)",
                 (int(card_id),str(event_type),old_status,new_status,note,str(source or "admin"),utc_now()))


def _rfid_block_fields(status, reason=None):
    status=str(status or "Aktiv").strip()
    if status == "Aktiv":
        return status, None, None
    return status, utc_now(), (str(reason).strip() if reason else None)


def list_rfid_cards():
    with _lock,_connect() as conn:
        rows=conn.execute("""SELECT r.*,u.name AS user_name,v.name AS vehicle_name,v.plate AS vehicle_plate,
            (SELECT COUNT(*) FROM transactions t WHERE t.rfid_card_id=r.id OR (t.rfid_card_id IS NULL AND t.id_tag=r.uid)) AS session_count,
            (SELECT COALESCE(SUM(t.energy_kwh),0) FROM transactions t WHERE t.rfid_card_id=r.id OR (t.rfid_card_id IS NULL AND t.id_tag=r.uid)) AS energy_kwh,
            (SELECT id FROM rfid_cards n WHERE n.replacement_for_id=r.id ORDER BY n.id DESC LIMIT 1) AS replaced_by_id
            FROM rfid_cards r LEFT JOIN users u ON u.id=r.user_id LEFT JOIN vehicles v ON v.id=r.vehicle_id ORDER BY r.uid""").fetchall()
        return [dict(r) for r in rows]


def get_rfid_card(card_id):
    with _lock,_connect() as conn:
        r=conn.execute("""SELECT r.*,u.name AS user_name,v.name AS vehicle_name,v.plate AS vehicle_plate,
            (SELECT id FROM rfid_cards n WHERE n.replacement_for_id=r.id ORDER BY n.id DESC LIMIT 1) AS replaced_by_id
            FROM rfid_cards r LEFT JOIN users u ON u.id=r.user_id LEFT JOIN vehicles v ON v.id=r.vehicle_id WHERE r.id=?""",(card_id,)).fetchone()
        return dict(r) if r else None


def create_rfid_card(uid,label=None,user_id=None,vehicle_id=None,status="Aktiv",expires_at=None,notes=None,replacement_for_id=None,source="admin"):
    uid=str(uid or "").strip()
    if not uid: raise ValueError("RFID-UID ist erforderlich.")
    with _lock,_connect() as conn:
        if conn.execute("SELECT id FROM rfid_cards WHERE uid=?",(uid,)).fetchone():raise ValueError("Diese RFID-UID ist bereits vorhanden.")
        if user_id is not None and not conn.execute("SELECT id FROM users WHERE id=?",(user_id,)).fetchone():raise ValueError("Benutzer nicht gefunden.")
        if vehicle_id is not None and not conn.execute("SELECT id FROM vehicles WHERE id=? AND active=1",(vehicle_id,)).fetchone():raise ValueError("Fahrzeug nicht gefunden.")
        if replacement_for_id is not None and not conn.execute("SELECT id FROM rfid_cards WHERE id=?",(replacement_for_id,)).fetchone():raise ValueError("Zu ersetzende RFID-Karte nicht gefunden.")
        normalized,blocked_at,blocked_reason=_rfid_block_fields(status)
        now=utc_now()
        cur=conn.execute("""INSERT INTO rfid_cards(uid,label,user_id,vehicle_id,status,created_at,issued_at,expires_at,blocked_at,blocked_reason,replacement_for_id,notes)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(uid,label,user_id,vehicle_id,normalized,now,now,expires_at,blocked_at,blocked_reason,replacement_for_id,notes))
        cid=int(cur.lastrowid)
        _rfid_log_event_conn(conn,cid,"created",None,normalized,"RFID-Karte angelegt",source)
        if user_id and vehicle_id:conn.execute("INSERT OR IGNORE INTO user_vehicles(user_id,vehicle_id,primary_vehicle,assigned_at) VALUES(?,?,0,?)",(user_id,vehicle_id,utc_now()))
        _rfid_local_list_bump_conn(conn,[uid])
        conn.commit();return cid


def update_rfid_card(card_id,uid,label=None,user_id=None,vehicle_id=None,status="Aktiv",expires_at=None,notes=None):
    uid=str(uid or "").strip()
    if not uid: raise ValueError("RFID-UID ist erforderlich.")
    with _lock,_connect() as conn:
        old=conn.execute("SELECT * FROM rfid_cards WHERE id=?",(card_id,)).fetchone()
        if not old:return False
        if conn.execute("SELECT id FROM rfid_cards WHERE uid=? AND id<>?",(uid,card_id)).fetchone():raise ValueError("Diese RFID-UID ist bereits vorhanden.")
        if user_id is not None and not conn.execute("SELECT id FROM users WHERE id=?",(user_id,)).fetchone():raise ValueError("Benutzer nicht gefunden.")
        if vehicle_id is not None and not conn.execute("SELECT id FROM vehicles WHERE id=? AND active=1",(vehicle_id,)).fetchone():raise ValueError("Fahrzeug nicht gefunden.")
        normalized=str(status or "Aktiv").strip()
        if normalized == "Aktiv":
            blocked_at=blocked_reason=None
        elif normalized == old["status"]:
            blocked_at=old["blocked_at"] or utc_now(); blocked_reason=old["blocked_reason"]
        else:
            blocked_at=utc_now(); blocked_reason=f"Status auf {normalized} geändert"
        conn.execute("""UPDATE rfid_cards SET uid=?,label=?,user_id=?,vehicle_id=?,status=?,expires_at=?,notes=?,blocked_at=?,blocked_reason=? WHERE id=?""",
                     (uid,label,user_id,vehicle_id,normalized,expires_at,notes,blocked_at,blocked_reason,card_id))
        changes=[]
        if old["status"] != normalized: changes.append(f"Status: {old['status']} → {normalized}")
        if old["user_id"] != user_id: changes.append("Benutzerzuordnung geändert")
        if old["vehicle_id"] != vehicle_id: changes.append("Fahrzeugzuordnung geändert")
        if old["uid"] != uid: changes.append("UID geändert")
        if changes: _rfid_log_event_conn(conn,card_id,"updated",old["status"],normalized," · ".join(changes),"admin")
        if user_id and vehicle_id:conn.execute("INSERT OR IGNORE INTO user_vehicles(user_id,vehicle_id,primary_vehicle,assigned_at) VALUES(?,?,0,?)",(user_id,vehicle_id,utc_now()))
        _rfid_local_list_bump_conn(conn,[old["uid"],uid])
        conn.commit();return True


def set_rfid_status(card_id,status,note=None,source="admin"):
    with _lock,_connect() as conn:
        old=conn.execute("SELECT * FROM rfid_cards WHERE id=?",(card_id,)).fetchone()
        if not old:return False
        normalized,blocked_at,blocked_reason=_rfid_block_fields(status,note)
        conn.execute("UPDATE rfid_cards SET status=?,blocked_at=?,blocked_reason=? WHERE id=?",(normalized,blocked_at,blocked_reason,card_id))
        _rfid_log_event_conn(conn,card_id,"status",old["status"],normalized,note or f"Status auf {normalized} gesetzt",source)
        _rfid_local_list_bump_conn(conn,[old["uid"]])
        conn.commit();return True


def rfid_card_detail(card_id):
    with _lock,_connect() as conn:
        card=conn.execute("""SELECT r.*,u.name AS user_name,v.name AS vehicle_name,v.plate AS vehicle_plate,
            (SELECT id FROM rfid_cards n WHERE n.replacement_for_id=r.id ORDER BY n.id DESC LIMIT 1) AS replaced_by_id
            FROM rfid_cards r LEFT JOIN users u ON u.id=r.user_id LEFT JOIN vehicles v ON v.id=r.vehicle_id WHERE r.id=?""",(card_id,)).fetchone()
        if not card:return None
        history=[dict(r) for r in conn.execute("SELECT * FROM rfid_card_events WHERE card_id=? ORDER BY id DESC LIMIT 50",(card_id,)).fetchall()]
        tx=[dict(r) for r in conn.execute("""SELECT id,started_at,ended_at,charge_point_id,connector_id,energy_kwh,status FROM transactions
            WHERE rfid_card_id=? OR (rfid_card_id IS NULL AND id_tag=(SELECT uid FROM rfid_cards WHERE id=?)) ORDER BY id DESC LIMIT 20""",(card_id,card_id)).fetchall()]
        return {"card":dict(card),"history":history,"transactions":tx}


def replace_rfid_card(card_id,new_uid,label=None,expires_at=None,notes=None,source="admin"):
    new_uid=str(new_uid or "").strip()
    if not new_uid: raise ValueError("Neue RFID-UID ist erforderlich.")
    with _lock,_connect() as conn:
        old=conn.execute("SELECT * FROM rfid_cards WHERE id=?",(card_id,)).fetchone()
        if not old: raise ValueError("RFID-Karte nicht gefunden.")
        if old["status"] in {"Ersetzt","Deaktiviert"}: raise ValueError("Diese RFID-Karte kann nicht erneut ersetzt werden.")
        if conn.execute("SELECT id FROM rfid_cards WHERE uid=?",(new_uid,)).fetchone(): raise ValueError("Diese RFID-UID ist bereits vorhanden.")
        now=utc_now()
        cur=conn.execute("""INSERT INTO rfid_cards(uid,label,user_id,vehicle_id,status,created_at,issued_at,expires_at,replacement_for_id,notes)
            VALUES(?,?,?,?, 'Aktiv',?,?,?,?,?)""",(new_uid,label or old["label"] or "Ersatzkarte",old["user_id"],old["vehicle_id"],now,now,expires_at,card_id,notes))
        new_id=int(cur.lastrowid)
        conn.execute("UPDATE rfid_cards SET status='Ersetzt',blocked_at=?,blocked_reason='Durch Ersatzkarte ersetzt' WHERE id=?",(now,card_id))
        _rfid_log_event_conn(conn,card_id,"replaced",old["status"],"Ersetzt",f"Ersetzt durch Karte #{new_id}",source)
        _rfid_log_event_conn(conn,new_id,"created",None,"Aktiv",f"Ersatz für Karte #{card_id}",source)
        _rfid_local_list_bump_conn(conn,[old["uid"],new_uid])
        conn.commit();return new_id


def _month_bounds_utc(now=None):
    now = now or datetime.now(timezone.utc)
    berlin = ZoneInfo("Europe/Berlin")
    local = now.astimezone(berlin)
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    return start_local.astimezone(timezone.utc).isoformat(), end_local.astimezone(timezone.utc).isoformat(), start_local.strftime("%Y-%m")

def _budget_status(limit_value, used, mode):
    if limit_value is None:
        return {"status":"unlimited","status_label":"Unbegrenzt","percent":None,"remaining_kwh":None,"blocked":False}
    limit_value=max(0.0,float(limit_value))
    used=max(0.0,float(used or 0))
    percent = 100.0 if limit_value == 0 and used > 0 else (0.0 if limit_value == 0 else used / limit_value * 100.0)
    remaining=max(0.0,limit_value-used)
    blocked = str(mode or "warn") == "block" and used >= limit_value
    if blocked:
        status, label = "blocked", "Gesperrt"
    elif percent >= 100:
        status, label = "exceeded", "Limit überschritten"
    elif percent >= 90:
        status, label = "critical", "90 % erreicht"
    elif percent >= 70:
        status, label = "warning", "70 % erreicht"
    else:
        status, label = "ok", "Im Limit"
    return {"status":status,"status_label":label,"percent":round(percent,1),"remaining_kwh":round(remaining,3),"blocked":blocked}

def _user_month_energy_conn(conn, user_id, start_utc, end_utc):
    row=conn.execute("""
        SELECT COALESCE(SUM(t.energy_kwh),0)
        FROM transactions t
        WHERE (t.user_id=? OR (t.user_id IS NULL AND (t.id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?) OR t.id_tag=(SELECT rfid FROM users WHERE id=?))))
          AND t.started_at>=? AND t.started_at<?
    """, (user_id,user_id,user_id,start_utc,end_utc)).fetchone()
    return float(row[0] or 0)

def user_monthly_budget(user_id, now=None):
    start_utc,end_utc,month_key=_month_bounds_utc(now)
    with _lock,_connect() as conn:
        user=conn.execute("SELECT id,name,monthly_kwh_limit,monthly_limit_mode FROM users WHERE id=?",(user_id,)).fetchone()
        if not user:return None
        used=_user_month_energy_conn(conn,user_id,start_utc,end_utc)
        limit_value=user["monthly_kwh_limit"]
        mode=user["monthly_limit_mode"] or "warn"
        state=_budget_status(limit_value,used,mode)
        return {
            "month":month_key,"limit_kwh":None if limit_value is None else float(limit_value),
            "used_kwh":round(used,3),"mode":mode,**state
        }

def user_budget_summary(now=None):
    start_utc,end_utc,month_key=_month_bounds_utc(now)
    with _lock,_connect() as conn:
        users=conn.execute("SELECT id,monthly_kwh_limit,monthly_limit_mode FROM users WHERE status='Aktiv'").fetchall()
        summary={"month":month_key,"ok":0,"warning":0,"blocked":0,"unlimited":0,"total":len(users)}
        for user in users:
            used=_user_month_energy_conn(conn,user["id"],start_utc,end_utc)
            state=_budget_status(user["monthly_kwh_limit"],used,user["monthly_limit_mode"] or "warn")
            if state["status"] == "unlimited": summary["unlimited"] += 1
            elif state["blocked"]: summary["blocked"] += 1
            elif (state["percent"] or 0) >= 70: summary["warning"] += 1
            else: summary["ok"] += 1
        return summary


def _analytics_period_bounds(now=None):
    """Return UTC bounds for Berlin-local reporting periods."""
    berlin = ZoneInfo("Europe/Berlin")
    if now is None:
        local_now = datetime.now(berlin)
    else:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local_now = now.astimezone(berlin)
    current_month = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if current_month.month == 1:
        previous_month = current_month.replace(year=current_month.year - 1, month=12)
    else:
        previous_month = current_month.replace(month=current_month.month - 1)
    year_start = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "now_local": local_now,
        "current_month": (current_month.astimezone(timezone.utc), local_now.astimezone(timezone.utc)),
        "previous_month": (previous_month.astimezone(timezone.utc), current_month.astimezone(timezone.utc)),
        "current_year": (year_start.astimezone(timezone.utc), local_now.astimezone(timezone.utc)),
    }


def _analytics_row_timing_conn(conn, row, now_utc=None):
    stored=(
        float(row["charging_seconds"] or 0),
        float(row["stand_seconds"] or 0),
        float(row["connection_seconds"] or 0),
    )
    if row["status"] == "Active" and not row["ended_at"]:
        live = _transaction_time_breakdown_conn(conn, int(row["id"]), now=now_utc) or {}
        return (
            float(live.get("charging_seconds") or 0),
            float(live.get("stand_seconds") or 0),
            float(live.get("connection_seconds") or 0),
        )
    # Older completed sessions may predate persisted timing columns. Rebuild
    # those values from vendor-neutral MeterValues on demand instead of making
    # historical analytics silently look like 0 minutes.
    if stored[2] <= 0 and row["ended_at"]:
        derived=_transaction_time_breakdown_conn(conn,int(row["id"]),now=_parse_iso_utc(row["ended_at"])) or {}
        if float(derived.get("connection_seconds") or 0) > 0:
            return (float(derived.get("charging_seconds") or 0),float(derived.get("stand_seconds") or 0),float(derived.get("connection_seconds") or 0))
    return stored


def _summarize_analytics_rows_conn(conn, rows, start_utc=None, end_utc=None, capacity_slots=1, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    selected=[]
    for row in rows:
        started=_parse_iso_utc(row["started_at"])
        if started is None:
            continue
        if start_utc is not None and started < start_utc:
            continue
        if end_utc is not None and started >= end_utc:
            continue
        selected.append(row)
    sessions=len(selected)
    energy=sum(float(r["energy_kwh"] or 0) for r in selected)
    charging=stand=connection=0.0
    known_timing_connection=0.0
    known_timing_sessions=0
    peak=0.0
    stand_sessions=0
    post_session_occupied=0.0
    unplug_delays=[]
    post_session_active_sessions=0
    prompt_unplug_sessions=0
    for row in selected:
        c,s,x=_analytics_row_timing_conn(conn,row,now_utc)
        charging += c; stand += s; connection += x
        timing_known=str(row["timing_quality"] or "") != "connection_only" if "timing_quality" in row.keys() else True
        if timing_known:
            known_timing_connection += x; known_timing_sessions += 1
        if s > 0: stand_sessions += 1
        occupancy=_post_session_occupancy_for_row(row,now=now_utc)
        if occupancy.get("tracked"):
            seconds=float(occupancy.get("seconds") or 0)
            post_session_occupied += seconds
            if occupancy.get("active"):
                post_session_active_sessions += 1
            else:
                unplug_delays.append(seconds)
                if seconds <= PROMPT_UNPLUG_SECONDS:
                    prompt_unplug_sessions += 1
        peak=max(peak,float(row["max_power_kw"] or 0))
    avg_power=(energy/(charging/3600.0)) if charging > 0 else 0.0
    occupancy_pct=None
    charging_utilization_pct=None
    if start_utc is not None and end_utc is not None and end_utc > start_utc:
        capacity=max(1,int(capacity_slots or 1))*(end_utc-start_utc).total_seconds()
        occupancy_pct=min(100.0,(connection/capacity*100.0)) if capacity > 0 else 0.0
        charging_utilization_pct=min(100.0,(charging/capacity*100.0)) if capacity > 0 else 0.0
    return {
        "sessions": sessions,
        "energy_kwh": round(energy,3),
        "avg_energy_kwh": round(energy/sessions if sessions else 0.0,3),
        "charging_seconds": round(charging,1),
        "stand_seconds": round(stand,1),
        "connection_seconds": round(connection,1),
        "avg_charging_seconds": round(charging/known_timing_sessions if known_timing_sessions else 0.0,1),
        "avg_stand_seconds": round(stand/known_timing_sessions if known_timing_sessions else 0.0,1),
        "avg_connection_seconds": round(connection/sessions if sessions else 0.0,1),
        "stand_ratio_pct": None if known_timing_connection <= 0 else round(stand/known_timing_connection*100.0,1),
        "stand_sessions": stand_sessions,
        "timing_unknown_sessions": sessions-known_timing_sessions,
        "post_session_occupied_seconds": round(post_session_occupied,1),
        "unplug_delay_sessions": len(unplug_delays),
        "post_session_active_sessions": post_session_active_sessions,
        "avg_unplug_delay_seconds": round(sum(unplug_delays)/len(unplug_delays),1) if unplug_delays else None,
        "max_unplug_delay_seconds": round(max(unplug_delays),1) if unplug_delays else None,
        "prompt_unplug_sessions": prompt_unplug_sessions,
        "prompt_unplug_ratio_pct": round(prompt_unplug_sessions/len(unplug_delays)*100.0,1) if unplug_delays else None,
        "avg_power_kw": round(avg_power,2),
        "peak_power_kw": round(peak,2),
        "occupancy_pct": None if occupancy_pct is None else round(occupancy_pct,2),
        "charging_utilization_pct": None if charging_utilization_pct is None else round(charging_utilization_pct,2),
    }


def _month_starts_local(local_now, count=12):
    cursor=local_now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    starts=[]
    for _ in range(count):
        starts.append(cursor)
        if cursor.month == 1:
            cursor=cursor.replace(year=cursor.year-1,month=12)
        else:
            cursor=cursor.replace(month=cursor.month-1)
    return list(reversed(starts))


def _monthly_analytics_series_conn(conn, rows, local_now, count=12, now_utc=None):
    berlin=ZoneInfo("Europe/Berlin")
    starts=_month_starts_local(local_now,count)
    buckets={x.strftime("%Y-%m"):{"month":x.strftime("%Y-%m"),"label":x.strftime("%m/%Y"),"sessions":0,"energy_kwh":0.0,"charging_seconds":0.0,"stand_seconds":0.0,"connection_seconds":0.0,"known_timing_connection_seconds":0.0,"timing_unknown_sessions":0} for x in starts}
    now_utc=now_utc or datetime.now(timezone.utc)
    for row in rows:
        started=_parse_iso_utc(row["started_at"])
        if not started: continue
        key=started.astimezone(berlin).strftime("%Y-%m")
        if key not in buckets: continue
        c,s,x=_analytics_row_timing_conn(conn,row,now_utc)
        b=buckets[key]; b["sessions"]+=1; b["energy_kwh"]+=float(row["energy_kwh"] or 0); b["charging_seconds"]+=c; b["stand_seconds"]+=s; b["connection_seconds"]+=x
        timing_known=str(row["timing_quality"] or "") != "connection_only" if "timing_quality" in row.keys() else True
        if timing_known: b["known_timing_connection_seconds"]+=x
        else: b["timing_unknown_sessions"]+=1
    result=[]
    for b in buckets.values():
        b["energy_kwh"]=round(b["energy_kwh"],3); b["charging_seconds"]=round(b["charging_seconds"],1); b["stand_seconds"]=round(b["stand_seconds"],1); b["connection_seconds"]=round(b["connection_seconds"],1)
        b["stand_ratio_pct"]=None if not b["known_timing_connection_seconds"] else round(b["stand_seconds"]/b["known_timing_connection_seconds"]*100.0,1)
        b.pop("known_timing_connection_seconds",None)
        result.append(b)
    return result


def _analytics_bundle_conn(conn, rows, capacity_slots=1, now=None, include_weekdays=False):
    periods=_analytics_period_bounds(now)
    now_utc=periods["now_local"].astimezone(timezone.utc)
    cm_start,cm_end=periods["current_month"]
    pm_start,pm_end=periods["previous_month"]
    yr_start,yr_end=periods["current_year"]
    bundle={
        "current_month":_summarize_analytics_rows_conn(conn,rows,cm_start,cm_end,capacity_slots,now_utc),
        "previous_month":_summarize_analytics_rows_conn(conn,rows,pm_start,pm_end,capacity_slots,now_utc),
        "current_year":_summarize_analytics_rows_conn(conn,rows,yr_start,yr_end,capacity_slots,now_utc),
        "all_time":_summarize_analytics_rows_conn(conn,rows,None,None,capacity_slots,now_utc),
        "monthly":_monthly_analytics_series_conn(conn,rows,periods["now_local"],12,now_utc),
        "generated_at":now_utc.isoformat(),
    }
    if include_weekdays:
        berlin=ZoneInfo("Europe/Berlin")
        cutoff=(periods["now_local"]-timedelta(days=90)).date()
        labels=["Mo","Di","Mi","Do","Fr","Sa","So"]
        weekday=[{"weekday":i,"label":labels[i],"sessions":0,"energy_kwh":0.0,"connection_seconds":0.0} for i in range(7)]
        for row in rows:
            started=_parse_iso_utc(row["started_at"])
            if not started: continue
            local=started.astimezone(berlin)
            if local.date() < cutoff: continue
            b=weekday[local.weekday()]
            _,_,connection=_analytics_row_timing_conn(conn,row,now_utc)
            b["sessions"]+=1; b["energy_kwh"]+=float(row["energy_kwh"] or 0); b["connection_seconds"]+=connection
        for b in weekday:
            b["energy_kwh"]=round(b["energy_kwh"],3); b["connection_seconds"]=round(b["connection_seconds"],1)
        bundle["weekdays_90d"]=weekday
    return bundle


def user_analytics(user_id, now=None):
    with _lock,_connect() as conn:
        rows=conn.execute("""SELECT t.* FROM transactions t
            WHERE t.user_id=? OR (t.user_id IS NULL AND (t.id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?) OR t.id_tag=(SELECT rfid FROM users WHERE id=?)))
            ORDER BY t.started_at""",(user_id,user_id,user_id)).fetchall()
        return _analytics_bundle_conn(conn,rows,capacity_slots=1,now=now)




def charge_point_analytics(cp_id, now=None):
    with _lock,_connect() as conn:
        cp=conn.execute("SELECT connector_count FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        if not cp: return None
        rows=conn.execute("SELECT * FROM transactions WHERE charge_point_id=? ORDER BY started_at",(cp_id,)).fetchall()
        return _analytics_bundle_conn(conn,rows,capacity_slots=max(1,int(cp["connector_count"] or 1)),now=now,include_weekdays=True)

def list_users_rich():
    start_utc,end_utc,month_key=_month_bounds_utc()
    with _lock,_connect() as conn:
        rows=conn.execute("""
            SELECT u.*,
              (SELECT COUNT(*) FROM rfid_cards r WHERE r.user_id=u.id) AS rfid_count,
              (SELECT COUNT(*) FROM user_vehicles uv WHERE uv.user_id=u.id) AS vehicle_count,
              (SELECT COUNT(*) FROM transactions t WHERE t.user_id=u.id OR (t.user_id IS NULL AND (t.id_tag IN (SELECT uid FROM rfid_cards r2 WHERE r2.user_id=u.id) OR t.id_tag=u.rfid))) AS session_count,
              (SELECT COALESCE(SUM(t.energy_kwh),0) FROM transactions t WHERE t.user_id=u.id OR (t.user_id IS NULL AND (t.id_tag IN (SELECT uid FROM rfid_cards r3 WHERE r3.user_id=u.id) OR t.id_tag=u.rfid))) AS energy_kwh,
              (SELECT MAX(r.last_used_at) FROM rfid_cards r WHERE r.user_id=u.id) AS last_used_at
            FROM users u ORDER BY u.name
        """).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            item["charge_access_mode"]=_normalize_charge_access_mode(item.get("charge_access_mode"))
            item["allowed_charge_point_ids"]=[str(r[0]) for r in conn.execute("SELECT charge_point_id FROM user_charge_point_access WHERE user_id=? ORDER BY charge_point_id",(item["id"],)).fetchall()]
            cost_row=conn.execute("""SELECT COALESCE(SUM(COALESCE(t.cost_cents,0)),0)
                FROM transactions t
                WHERE t.user_id=? OR (t.user_id IS NULL AND (t.id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?) OR t.id_tag=(SELECT rfid FROM users WHERE id=?)))""",(item["id"],item["id"],item["id"])).fetchone()
            item["costs"]=round(float((cost_row or [0])[0] or 0)/100.0,2)
            used=_user_month_energy_conn(conn,item["id"],start_utc,end_utc)
            state=_budget_status(item.get("monthly_kwh_limit"),used,item.get("monthly_limit_mode") or "warn")
            item.update({
                "budget_month":month_key,"month_energy_kwh":round(used,3),
                "monthly_limit_kwh":None if item.get("monthly_kwh_limit") is None else float(item.get("monthly_kwh_limit")),
                "monthly_limit_mode":item.get("monthly_limit_mode") or "warn",
                "budget_status":state["status"],"status_label":state["status_label"],
                "percent":state["percent"],"remaining_kwh":state["remaining_kwh"],"blocked":state["blocked"]
            })
            result.append(item)
        return result

def primary_vehicle_for_user(user_id):
    with _lock,_connect() as conn:
        row=conn.execute("""SELECT v.*,uv.primary_vehicle FROM user_vehicles uv
            JOIN vehicles v ON v.id=uv.vehicle_id
            WHERE uv.user_id=? AND v.active=1
            ORDER BY uv.primary_vehicle DESC,v.name COLLATE NOCASE LIMIT 1""",(int(user_id),)).fetchone()
        return dict(row) if row else None


def assign_user_vehicle(user_id, vehicle_id, primary=False):
    with _lock, _connect() as conn:
        if not conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone(): return False
        if not conn.execute("SELECT id FROM vehicles WHERE id=? AND active=1", (vehicle_id,)).fetchone(): return False
        if primary:
            conn.execute("UPDATE user_vehicles SET primary_vehicle=0 WHERE user_id=?", (user_id,))
        conn.execute("INSERT OR IGNORE INTO user_vehicles(user_id,vehicle_id,primary_vehicle,assigned_at) VALUES(?,?,?,?)", (user_id,vehicle_id,1 if primary else 0,utc_now()))
        conn.commit(); return True

def unassign_user_vehicle(user_id, vehicle_id):
    with _lock, _connect() as conn:
        cur=conn.execute("DELETE FROM user_vehicles WHERE user_id=? AND vehicle_id=?", (user_id,vehicle_id))
        conn.commit(); return cur.rowcount>0

def user_details(user_id, transaction_page=1, transaction_page_size=10):
    start_utc,end_utc,month_key=_month_bounds_utc()
    try:
        transaction_page=max(1,int(transaction_page or 1))
    except (TypeError,ValueError):
        transaction_page=1
    try:
        transaction_page_size=int(transaction_page_size or 10)
    except (TypeError,ValueError):
        transaction_page_size=10
    if transaction_page_size not in {10,20,30}:
        transaction_page_size=10
    with _lock,_connect() as conn:
        u=conn.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
        if not u:return None
        cards=[dict(r) for r in conn.execute("SELECT * FROM rfid_cards WHERE user_id=? ORDER BY uid",(user_id,)).fetchall()]
        vehicles=[dict(r) for r in conn.execute("SELECT v.* FROM user_vehicles uv JOIN vehicles v ON v.id=uv.vehicle_id WHERE uv.user_id=? AND v.active=1 ORDER BY uv.primary_vehicle DESC,v.name",(user_id,)).fetchall()]
        tx_where="""t.user_id=? OR (t.user_id IS NULL AND (t.id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?) OR t.id_tag=(SELECT rfid FROM users WHERE id=?)))"""
        tx_args=(user_id,user_id,user_id)
        tx_total=int(conn.execute(f"SELECT COUNT(*) FROM transactions t WHERE {tx_where}",tx_args).fetchone()[0] or 0)
        tx_pages=max(1,(tx_total+transaction_page_size-1)//transaction_page_size)
        transaction_page=min(transaction_page,tx_pages)
        tx_offset=(transaction_page-1)*transaction_page_size
        txs=[dict(r) for r in conn.execute(f"""SELECT t.*,v.name AS vehicle_name,v.plate AS vehicle_plate FROM transactions t LEFT JOIN vehicles v ON v.id=t.vehicle_id WHERE {tx_where} ORDER BY t.id DESC LIMIT ? OFFSET ?""",tx_args+(transaction_page_size,tx_offset)).fetchall()]
        for tx in txs:
            tx["timing"]=_transaction_time_breakdown_conn(conn,int(tx["id"])) or {"charging_seconds":float(tx.get("charging_seconds") or 0),"stand_seconds":float(tx.get("stand_seconds") or 0),"connection_seconds":float(tx.get("connection_seconds") or 0)}
        st=conn.execute("""SELECT COUNT(*) sessions,COALESCE(SUM(energy_kwh),0) energy,COALESCE(SUM(COALESCE(cost_cents,0)),0)/100.0 costs,MAX(started_at) last_used_at FROM transactions WHERE user_id=? OR (user_id IS NULL AND (id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?) OR id_tag=(SELECT rfid FROM users WHERE id=?)))""",(user_id,user_id,user_id)).fetchone()
        user=dict(u)
        user["charge_access_mode"]=_normalize_charge_access_mode(user.get("charge_access_mode"))
        user["allowed_charge_point_ids"]=[str(r[0]) for r in conn.execute("SELECT charge_point_id FROM user_charge_point_access WHERE user_id=? ORDER BY charge_point_id",(int(user_id),)).fetchall()]
        used=_user_month_energy_conn(conn,user_id,start_utc,end_utc)
        state=_budget_status(user.get("monthly_kwh_limit"),used,user.get("monthly_limit_mode") or "warn")
        budget={"month":month_key,"limit_kwh":None if user.get("monthly_kwh_limit") is None else float(user.get("monthly_kwh_limit")),"used_kwh":round(used,3),"mode":user.get("monthly_limit_mode") or "warn",**state}
        active=conn.execute("""SELECT t.*,v.name AS vehicle_name,v.make AS vehicle_make,v.model AS vehicle_model,v.plate AS vehicle_plate,v.image_path AS vehicle_image_path FROM transactions t LEFT JOIN vehicles v ON v.id=t.vehicle_id WHERE (t.user_id=? OR (t.user_id IS NULL AND (t.id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?) OR t.id_tag=(SELECT rfid FROM users WHERE id=?)))) AND t.status='Active' AND t.ended_at IS NULL ORDER BY t.id DESC LIMIT 1""",(user_id,user_id,user_id)).fetchone()
        active_item=dict(active) if active else None
        if active_item:
            sample=conn.execute("SELECT * FROM meter_samples WHERE transaction_id=? ORDER BY id DESC LIMIT 1",(active_item["id"],)).fetchone()
            if sample:
                power,source,_=_derived_power_for_connector(conn,active_item["charge_point_id"],int(active_item["connector_id"] or 0),sample)
                active_item["live_power_kw"]=power
                active_item["live_power_source"]=source
                active_item["live_meter_at"]=sample["ts"]
        analytics_rows=conn.execute("""SELECT t.* FROM transactions t WHERE t.user_id=? OR (t.user_id IS NULL AND (t.id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?) OR t.id_tag=(SELECT rfid FROM users WHERE id=?))) ORDER BY t.started_at""",(user_id,user_id,user_id)).fetchall()
        analytics=_analytics_bundle_conn(conn,analytics_rows,capacity_slots=1)
        return {"user":user,"rfid_cards":cards,"vehicles":vehicles,"transactions":txs,"transaction_pagination":{"page":transaction_page,"page_size":transaction_page_size,"total":tx_total,"pages":tx_pages},"stats":dict(st),"budget":budget,"active_session":active_item,"analytics":analytics}

def list_users():
    return list_users_rich()

def update_charge_point_session_policy(cp_id, stand_grace_seconds, auto_stop_zero_minutes):
    """Persist vendor-neutral session timing policy for future transactions."""
    try:
        grace = int(stand_grace_seconds)
        auto_stop = int(auto_stop_zero_minutes)
    except (TypeError, ValueError):
        raise ValueError("Session-Zeitwerte müssen ganze Zahlen sein")
    if grace not in {0, 60, 120, 180, 300}:
        raise ValueError("Standzeit-Karenz muss 0, 60, 120, 180 oder 300 Sekunden betragen")
    if auto_stop not in {0, 5, 15, 30, 60}:
        raise ValueError("0-kW-Auto-Stop muss Aus, 5, 15, 30 oder 60 Minuten sein")
    with _lock, _connect() as conn:
        row = conn.execute("SELECT id FROM charge_points WHERE id=?", (cp_id,)).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE charge_points SET stand_grace_seconds=?, auto_stop_zero_minutes=? WHERE id=?",
            (grace, auto_stop, cp_id),
        )
        conn.commit()
        return True


def meter_capabilities_for_charge_point(cp_id, limit=240):
    """Summarize which standard OCPP MeterValues a charge point actually sends.

    Sample.Periodic and Sample.Clock are intentionally treated as the same
    measurand with different contexts: sampled and aligned data use the same
    OCPP MeterValues message and therefore the same vendor-neutral parser.
    """
    import json
    wanted = [
        "Energy.Active.Import.Register", "Power.Active.Import", "Power.Offered",
        "Current.Import", "Current.Offered", "Voltage", "Frequency", "SoC", "Temperature",
    ]
    aliases = {x.casefold(): x for x in wanted}
    aliases["stateofcharge"] = "SoC"
    summary = {name: {"measurand": name, "seen": False, "last_at": None, "last_value": None,
                      "unit": None, "contexts": set(), "phases": set(), "locations": set(), "samples": 0}
               for name in wanted}
    rows = meter_samples_for_charge_point(cp_id, limit)
    for row in rows:
        try:
            values = json.loads(row.get("values_json") or "[]")
        except Exception:
            values = []
        row_at = row.get("sampled_at") or row.get("ts")
        for item in values:
            key = aliases.get(str(item.get("measurand") or "Energy.Active.Import.Register").strip().casefold())
            if not key:
                continue
            entry = summary[key]
            entry["seen"] = True
            entry["samples"] += 1
            if item.get("context"):
                entry["contexts"].add(str(item.get("context")))
            if item.get("phase"):
                entry["phases"].add(str(item.get("phase")))
            if item.get("location"):
                entry["locations"].add(str(item.get("location")))
            if entry["last_at"] is None:
                entry["last_at"] = item.get("timestamp") or row_at
                entry["last_value"] = item.get("value")
                entry["unit"] = item.get("unit")
    out = []
    for name in wanted:
        entry = summary[name]
        entry["contexts"] = sorted(entry["contexts"])
        entry["phases"] = sorted(entry["phases"])
        entry["locations"] = sorted(entry["locations"])
        out.append(entry)
    return out


def list_charge_points_admin(include_retired=True):
    with _lock, _connect() as conn:
        sql = "SELECT * FROM charge_points" if include_retired else "SELECT * FROM charge_points WHERE COALESCE(retired,0)=0 AND COALESCE(archived,0)=0"
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY id").fetchall()]
        for cp in rows:
            cp["connectors"] = [dict(r) for r in conn.execute("SELECT * FROM connectors WHERE charge_point_id=? ORDER BY connector_id", (cp["id"],)).fetchall()]
        return rows

def create_charge_point(cp_id, name=None, vendor=None, model=None, serial_number=None, firmware=None, ocpp_version="1.6J", location=None, connector_count=1, connector_type="Type 2", max_power_kw=22, notes=None):
    cp_id = (cp_id or "").strip()
    if not cp_id:
        raise ValueError("Charge Point ID darf nicht leer sein")
    try:
        count = max(1, min(64, int(connector_count or 1)))
    except (TypeError, ValueError):
        raise ValueError("Connectoren muss eine Zahl zwischen 1 und 64 sein")
    try:
        maxkw = float(max_power_kw if max_power_kw is not None else 22)
    except (TypeError, ValueError):
        raise ValueError("Maximale Leistung muss eine Zahl sein")
    if maxkw < 0 or maxkw > 1000:
        raise ValueError("Maximale Leistung muss zwischen 0 und 1000 kW liegen")
    with _lock, _connect() as conn:
        if conn.execute("SELECT 1 FROM charge_points WHERE id=?", (cp_id,)).fetchone():
            return False
        conn.execute(
            """INSERT INTO charge_points(
                id,vendor,model,serial_number,firmware,status,last_seen,power_kw,energy_kwh,
                connector_count,max_power_kw,simulated,location,connector_type,ocpp_version,notes,retired,source_type
            ) VALUES(?,?,?,?,?,'Unknown',NULL,0,0,?,?,0,?,?,?,?,0,?)""",
            (cp_id, vendor, model, serial_number, firmware, count, maxkw, location, connector_type or "Type 2", ocpp_version or "1.6J", notes, 'manual')
        )
        for cid in range(1, count + 1):
            conn.execute(
                "INSERT INTO connectors(charge_point_id,connector_id,connector_type,max_power_kw,status) VALUES(?,?,?,?,?)",
                (cp_id, cid, connector_type or "Type 2", maxkw, "Unknown")
            )
        conn.commit()
        return True

def update_charge_point(cp_id, **fields):
    allowed={"vendor","model","serial_number","firmware","ocpp_version","location","connector_type","max_power_kw","connector_count","notes"}
    updates=[(k,fields[k]) for k in allowed if k in fields]
    if not updates: return False
    with _lock,_connect() as conn:
        if not conn.execute("SELECT id FROM charge_points WHERE id=?",(cp_id,)).fetchone(): return False
        conn.execute("UPDATE charge_points SET "+", ".join(f"{k}=?" for k,_ in updates)+" WHERE id=?",[v for _,v in updates]+[cp_id])
        if "connector_count" in fields or "connector_type" in fields or "max_power_kw" in fields:
            count=max(1,int(fields.get("connector_count") or conn.execute("SELECT connector_count FROM charge_points WHERE id=?",(cp_id,)).fetchone()[0] or 1))
            ctype=fields.get("connector_type") or conn.execute("SELECT connector_type FROM charge_points WHERE id=?",(cp_id,)).fetchone()[0] or "Type 2"
            maxkw=float(fields.get("max_power_kw") or conn.execute("SELECT max_power_kw FROM charge_points WHERE id=?",(cp_id,)).fetchone()[0] or 22)
            conn.execute("DELETE FROM connectors WHERE charge_point_id=? AND connector_id>?",(cp_id,count))
            for cid in range(1,count+1):
                conn.execute("INSERT OR IGNORE INTO connectors(charge_point_id,connector_id,connector_type,max_power_kw,status) VALUES(?,?,?,?,?)",(cp_id,cid,ctype,maxkw,'Unknown'))
                conn.execute("UPDATE connectors SET connector_type=?,max_power_kw=? WHERE charge_point_id=? AND connector_id=?",(ctype,maxkw,cp_id,cid))
        conn.commit(); return True

def retire_charge_point(cp_id):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT retired,archived FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        if not row or int(row[1] or 0): return False
        cur=conn.execute("UPDATE charge_points SET retired=1,status='Unavailable',power_kw=0 WHERE id=?",(cp_id,))
        conn.commit(); return cur.rowcount>0

def restore_charge_point(cp_id):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT retired,archived FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        if not row or int(row[1] or 0): return False
        cur=conn.execute("UPDATE charge_points SET retired=0,status='Unknown' WHERE id=?",(cp_id,))
        conn.commit(); return cur.rowcount>0

def archive_charge_point(cp_id):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT retired,archived,transaction_id,status FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        if not row or int(row[2] or 0): return False, 'not_found'
        if row[3] in {'Charging','Preparing','Finishing','SuspendedEV','SuspendedEVSE','Online','Connected','Available'} or row[2] is not None:
            return False, 'active'
        if not int(row[0] or 0): return False, 'not_retired'
        cur=conn.execute("UPDATE charge_points SET archived=1,retired=1,status='Unavailable',power_kw=0,archived_at=?,archive_reason=COALESCE(archive_reason,'Manuell archiviert') WHERE id=?",(utc_now(),cp_id))
        conn.commit(); return (cur.rowcount>0, 'archived' if cur.rowcount else 'not_found')

def unarchive_charge_point(cp_id):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT archived FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        if not row or not int(row[0] or 0): return False
        cur=conn.execute("UPDATE charge_points SET archived=0,retired=0,status='Unknown',archive_reason=NULL WHERE id=?",(cp_id,))
        conn.commit(); return cur.rowcount>0

def delete_ocpp_device(cp_id):
    """Remove a disconnected OCPP device record while preserving its historical data.

    Transactions, MeterValues and OCPP events deliberately remain in the database. If the
    physical station reconnects later, normal discovery creates a fresh pending device.
    """
    with _lock, _connect() as conn:
        row=conn.execute("SELECT transaction_id,status FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        if not row:
            return False, 'not_found'
        status=str(row[1] or '')
        active_tx=conn.execute("SELECT 1 FROM transactions WHERE charge_point_id=? AND status='Active' AND ended_at IS NULL LIMIT 1",(cp_id,)).fetchone()
        if row[0] is not None or active_tx or status in {'Charging','Preparing','Finishing','SuspendedEV','SuspendedEVSE'}:
            return False, 'active'
        conn.execute("DELETE FROM connectors WHERE charge_point_id=?",(cp_id,))
        conn.execute("DELETE FROM user_charge_point_access WHERE charge_point_id=?",(cp_id,))
        cur=conn.execute("DELETE FROM charge_points WHERE id=?",(cp_id,))
        conn.commit()
        return (cur.rowcount>0, 'deleted' if cur.rowcount else 'not_found')


def delete_charge_point(cp_id):
    """Hard-delete only an unused manual charge point; preserve history and OCPP devices."""
    with _lock,_connect() as conn:
        row=conn.execute("SELECT retired,archived,transaction_id,onboarded,first_seen,source_type,status FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        if not row: return False, 'not_found'
        status=str(row[6] or '')
        if row[2] is not None or status in {'Charging','Preparing','Finishing','SuspendedEV','SuspendedEVSE'}:
            return False, 'active'
        if int(row[0] or 0) or int(row[1] or 0):
            return False, 'archived'
        if int(row[3] or 0) == 0 and row[4] is not None:
            return False, 'not_manual'
        if str(row[5] or 'manual') == 'ocpp':
            return False, 'linked_device'
        tx=conn.execute("SELECT 1 FROM transactions WHERE charge_point_id=? LIMIT 1",(cp_id,)).fetchone()
        ev=conn.execute("SELECT 1 FROM events WHERE charge_point_id=? LIMIT 1",(cp_id,)).fetchone()
        ms=conn.execute("SELECT 1 FROM meter_samples WHERE charge_point_id=? LIMIT 1",(cp_id,)).fetchone()
        if tx or ev or ms: return False, 'history'
        conn.execute("DELETE FROM connectors WHERE charge_point_id=?",(cp_id,))
        cur=conn.execute("DELETE FROM charge_points WHERE id=?",(cp_id,))
        conn.commit(); return (cur.rowcount>0, 'deleted' if cur.rowcount else 'not_found')


def connectors_for_charge_point(cp_id):
    with _lock,_connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM connectors WHERE charge_point_id=? ORDER BY connector_id",(cp_id,)).fetchall()]

def update_connector(cp_id, connector_id, **fields):
    allowed={"connector_type","max_power_kw"}; updates=[(k,fields[k]) for k in allowed if k in fields]
    if not updates:return False
    with _lock,_connect() as conn:
        if not conn.execute("SELECT 1 FROM connectors WHERE charge_point_id=? AND connector_id=?",(cp_id,connector_id)).fetchone(): return False
        conn.execute("UPDATE connectors SET "+", ".join(f"{k}=?" for k,_ in updates)+" WHERE charge_point_id=? AND connector_id=?",[v for _,v in updates]+[cp_id,connector_id]); conn.commit(); return True


def analytics():
    with _lock, _connect() as conn:
        today = datetime.now(timezone.utc).date().isoformat()
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        row = conn.execute(
            """SELECT COUNT(*) sessions, COALESCE(SUM(energy_kwh),0) energy,
                      COALESCE(AVG((julianday(COALESCE(ended_at, started_at))-julianday(started_at))*24*60),0) avg_minutes,
                      COALESCE(SUM(charging_seconds),0) charging_seconds,
                      COALESCE(SUM(stand_seconds),0) stand_seconds,
                      COALESCE(SUM(connection_seconds),0) connection_seconds,
                      COALESCE(AVG(charging_seconds),0) avg_charging_seconds,
                      COALESCE(AVG(stand_seconds),0) avg_stand_seconds,
                      COALESCE(AVG(connection_seconds),0) avg_connection_seconds,
                      COALESCE(SUM(CASE WHEN COALESCE(timing_quality,'')<>'connection_only' THEN connection_seconds ELSE 0 END),0) known_timing_connection_seconds,
                      COALESCE(SUM(CASE WHEN COALESCE(timing_quality,'')<>'connection_only' THEN 1 ELSE 0 END),0) known_timing_sessions,
                      COALESCE(SUM(CASE WHEN started_at LIKE ? THEN energy_kwh ELSE 0 END),0) month_energy,
                      COUNT(CASE WHEN started_at LIKE ? THEN 1 END) month_sessions
               FROM transactions""", (month_prefix + "%", month_prefix + "%")
        ).fetchone()
        by_cp = conn.execute("SELECT charge_point_id, COUNT(*) sessions, COALESCE(SUM(energy_kwh),0) energy FROM transactions GROUP BY charge_point_id ORDER BY energy DESC").fetchall()
        daily = conn.execute(
            "SELECT substr(started_at,1,10) day, ROUND(COALESCE(SUM(energy_kwh),0),2) energy FROM transactions GROUP BY day ORDER BY day DESC LIMIT 14"
        ).fetchall()
        charging_seconds = float(row["charging_seconds"] or 0)
        stand_seconds = float(row["stand_seconds"] or 0)
        connection_seconds = float(row["connection_seconds"] or 0)
        # Stored timing is finalized when a transaction ends. For currently
        # active sessions, add the live derived values so Analytics remains live.
        active_rows = conn.execute(
            "SELECT id,charging_seconds,stand_seconds,connection_seconds FROM transactions WHERE status='Active' AND ended_at IS NULL"
        ).fetchall()
        for active in active_rows:
            timing = _transaction_time_breakdown_conn(conn, int(active["id"])) or {}
            charging_seconds += float(timing.get("charging_seconds") or 0) - float(active["charging_seconds"] or 0)
            stand_seconds += float(timing.get("stand_seconds") or 0) - float(active["stand_seconds"] or 0)
            connection_seconds += float(timing.get("connection_seconds") or 0) - float(active["connection_seconds"] or 0)
        sessions = int(row["sessions"] or 0)
        known_timing_sessions=int(row["known_timing_sessions"] or 0)
        known_timing_connection=float(row["known_timing_connection_seconds"] or 0)
        # Active sessions are always native OCPP timing and therefore known.
        for active in active_rows:
            timing = _transaction_time_breakdown_conn(conn, int(active["id"])) or {}
            known_timing_connection += max(0.0,float(timing.get("connection_seconds") or 0)-float(active["connection_seconds"] or 0))
        return {
            "sessions": sessions, "energy_kwh": round(float(row["energy"] or 0), 2),
            "avg_minutes": round(float(row["avg_minutes"] or 0), 1), "month_energy_kwh": round(float(row["month_energy"] or 0), 2),
            "month_sessions": int(row["month_sessions"] or 0),
            "charging_seconds": round(charging_seconds, 1), "stand_seconds": round(stand_seconds, 1),
            "connection_seconds": round(connection_seconds, 1),
            "avg_charging_seconds": round(charging_seconds / known_timing_sessions if known_timing_sessions else 0.0, 1),
            "avg_stand_seconds": round(stand_seconds / known_timing_sessions if known_timing_sessions else 0.0, 1),
            "avg_connection_seconds": round(connection_seconds / sessions if sessions else 0.0, 1),
            "stand_ratio_pct": None if known_timing_connection <= 0 else round(stand_seconds / known_timing_connection * 100.0, 1),
            "timing_unknown_sessions": max(0,sessions-known_timing_sessions),
            "by_charge_point": [dict(r) for r in by_cp], "daily": [dict(r) for r in daily],
        }


# V0.8.7 - Web access accounts and public charging-budget view

def system_user_count():
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM system_users").fetchone()[0])


def list_system_users():
    with _connect() as conn:
        rows=conn.execute("SELECT id,username,display_name,role,active,created_at,last_login_at,totp_enabled,totp_enabled_at FROM system_users ORDER BY display_name COLLATE NOCASE, username COLLATE NOCASE").fetchall()
        return [dict(r) for r in rows]


def get_system_user(user_id):
    with _connect() as conn:
        row=conn.execute("SELECT id,username,display_name,role,active,created_at,last_login_at,totp_enabled,totp_enabled_at FROM system_users WHERE id=?",(user_id,)).fetchone()
        return dict(row) if row else None


def get_system_user_auth(username):
    with _connect() as conn:
        row=conn.execute("SELECT * FROM system_users WHERE username=? COLLATE NOCASE LIMIT 1",((username or '').strip(),)).fetchone()
        return dict(row) if row else None


def create_system_user(username, display_name, password_hash, role='viewer', active=True):
    username=(username or '').strip()
    display_name=(display_name or '').strip()
    if not username or not display_name:
        raise ValueError('Benutzername und Anzeigename sind erforderlich.')
    if role not in ('admin','user','viewer'):
        raise ValueError('Unbekannte Rolle.')
    with _lock, _connect() as conn:
        try:
            cur=conn.execute("INSERT INTO system_users(username,display_name,password_hash,role,active,created_at) VALUES(?,?,?,?,?,?)",(username,display_name,password_hash,role,1 if active else 0,utc_now()))
            conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError('Dieser Benutzername ist bereits vergeben.') from exc


def update_system_user(user_id, username, display_name, role, active=True, password_hash=None):
    username=(username or '').strip(); display_name=(display_name or '').strip()
    if not username or not display_name:
        raise ValueError('Benutzername und Anzeigename sind erforderlich.')
    if role not in ('admin','user','viewer'):
        raise ValueError('Unbekannte Rolle.')
    with _lock, _connect() as conn:
        if not conn.execute("SELECT id FROM system_users WHERE id=?",(user_id,)).fetchone():
            return False
        fields=['username=?','display_name=?','role=?','active=?']; values=[username,display_name,role,1 if active else 0]
        if password_hash:
            fields.append('password_hash=?'); values.append(password_hash)
        values.append(user_id)
        try:
            conn.execute("UPDATE system_users SET "+','.join(fields)+" WHERE id=?",values)
            if not active:
                conn.execute("DELETE FROM system_sessions WHERE user_id=?",(user_id,))
            conn.commit(); return True
        except sqlite3.IntegrityError as exc:
            raise ValueError('Dieser Benutzername ist bereits vergeben.') from exc


def delete_system_user(user_id):
    with _lock, _connect() as conn:
        row=conn.execute("SELECT role,active FROM system_users WHERE id=?",(user_id,)).fetchone()
        if not row: return False, 'not_found'
        if row['role']=='admin' and int(row['active'] or 0)==1:
            admins=conn.execute("SELECT COUNT(*) FROM system_users WHERE role='admin' AND active=1").fetchone()[0]
            if admins <= 1: return False, 'last_admin'
        conn.execute("DELETE FROM system_sessions WHERE user_id=?",(user_id,))
        conn.execute("DELETE FROM system_2fa_challenges WHERE user_id=?",(user_id,))
        conn.execute("DELETE FROM system_2fa_attempts WHERE user_id=?",(user_id,))
        conn.execute("DELETE FROM system_recovery_codes WHERE user_id=?",(user_id,))
        push_ids=[int(r[0]) for r in conn.execute("SELECT id FROM web_push_subscriptions WHERE user_id=?",(user_id,)).fetchall()]
        if push_ids:
            marks=",".join("?" for _ in push_ids)
            conn.execute(f"DELETE FROM web_push_deliveries WHERE subscription_id IN ({marks})",push_ids)
        conn.execute("DELETE FROM web_push_subscriptions WHERE user_id=?",(user_id,))
        conn.execute("DELETE FROM system_users WHERE id=?",(user_id,))
        conn.commit(); return True, 'deleted'


def system_user_2fa_state(user_id):
    with _connect() as conn:
        row=conn.execute("""SELECT id,username,display_name,role,active,totp_enabled,totp_enabled_at,
            CASE WHEN COALESCE(totp_pending_secret,'')<>'' THEN 1 ELSE 0 END AS setup_pending
            FROM system_users WHERE id=?""",(int(user_id),)).fetchone()
        if not row:
            return None
        result=dict(row)
        result["recovery_codes_remaining"]=int(conn.execute(
            "SELECT COUNT(*) FROM system_recovery_codes WHERE user_id=? AND used_at IS NULL",(int(user_id),)
        ).fetchone()[0] or 0)
        return result


def system_user_totp_material(user_id):
    with _connect() as conn:
        row=conn.execute("""SELECT id,username,display_name,role,active,totp_secret,totp_pending_secret,
            totp_enabled,totp_enabled_at,totp_last_counter,password_hash
            FROM system_users WHERE id=? LIMIT 1""",(int(user_id),)).fetchone()
        if not row:
            return None
        result=dict(row)
        result["totp_secret"]=totp.unprotect_secret(result.get("totp_secret") or "")
        result["totp_pending_secret"]=totp.unprotect_secret(result.get("totp_pending_secret") or "")
        return result


def set_system_user_totp_pending(user_id, secret):
    secret=str(secret or "").strip()
    if not secret:
        raise ValueError("TOTP-Secret fehlt.")
    with _lock,_connect() as conn:
        cur=conn.execute("UPDATE system_users SET totp_pending_secret=? WHERE id=?",(totp.protect_secret(secret),int(user_id)))
        conn.commit()
        return cur.rowcount>0


def clear_system_user_totp_pending(user_id):
    with _lock,_connect() as conn:
        conn.execute("UPDATE system_users SET totp_pending_secret=NULL WHERE id=?",(int(user_id),))
        conn.commit()


def enable_system_user_totp(user_id, secret):
    now=utc_now()
    with _lock,_connect() as conn:
        cur=conn.execute("""UPDATE system_users SET totp_secret=?,totp_pending_secret=NULL,totp_enabled=1,
            totp_enabled_at=?,totp_last_counter=NULL WHERE id=?""",(totp.protect_secret(secret),now,int(user_id)))
        conn.execute("DELETE FROM system_2fa_challenges WHERE user_id=?",(int(user_id),))
        conn.commit()
        return cur.rowcount>0


def disable_system_user_totp(user_id):
    with _lock,_connect() as conn:
        cur=conn.execute("""UPDATE system_users SET totp_secret=NULL,totp_pending_secret=NULL,totp_enabled=0,
            totp_enabled_at=NULL,totp_last_counter=NULL WHERE id=?""",(int(user_id),))
        conn.execute("DELETE FROM system_2fa_challenges WHERE user_id=?",(int(user_id),))
        conn.execute("DELETE FROM system_recovery_codes WHERE user_id=?",(int(user_id),))
        conn.commit()
        return cur.rowcount>0


def accept_system_totp_counter(user_id, counter):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT totp_last_counter FROM system_users WHERE id=?",(int(user_id),)).fetchone()
        if not row:
            return False
        last=row["totp_last_counter"]
        if last is not None and int(counter)<=int(last):
            return False
        conn.execute("UPDATE system_users SET totp_last_counter=? WHERE id=?",(int(counter),int(user_id)))
        conn.commit()
        return True


def replace_system_recovery_codes(user_id, code_hashes):
    now=utc_now()
    hashes=[str(x) for x in code_hashes if str(x)]
    with _lock,_connect() as conn:
        conn.execute("DELETE FROM system_recovery_codes WHERE user_id=?",(int(user_id),))
        conn.executemany(
            "INSERT INTO system_recovery_codes(user_id,code_hash,created_at,used_at) VALUES(?,?,?,NULL)",
            [(int(user_id),value,now) for value in hashes]
        )
        conn.commit()
    return len(hashes)


def consume_system_recovery_code(user_id, code_hash):
    with _lock,_connect() as conn:
        row=conn.execute("""SELECT id FROM system_recovery_codes
            WHERE user_id=? AND code_hash=? AND used_at IS NULL LIMIT 1""",(int(user_id),str(code_hash))).fetchone()
        if not row:
            return False
        conn.execute("UPDATE system_recovery_codes SET used_at=? WHERE id=?",(utc_now(),int(row["id"])))
        conn.commit()
        return True


def create_system_2fa_challenge(token_hash, user_id, expires_at, next_path="/"):
    now=utc_now()
    with _lock,_connect() as conn:
        conn.execute("DELETE FROM system_2fa_challenges WHERE expires_at<?",(now,))
        conn.execute("DELETE FROM system_2fa_challenges WHERE user_id=?",(int(user_id),))
        conn.execute("""INSERT INTO system_2fa_challenges(token_hash,user_id,created_at,expires_at,attempts,next_path)
            VALUES(?,?,?,?,0,?)""",(str(token_hash),int(user_id),now,str(expires_at),str(next_path or "/")))
        conn.commit()


def system_2fa_challenge(token_hash):
    with _connect() as conn:
        row=conn.execute("""SELECT c.token_hash,c.user_id,c.created_at,c.expires_at,c.attempts,c.next_path,
            u.username,u.display_name,u.role,u.active,u.password_hash,u.totp_secret,u.totp_enabled,u.totp_last_counter
            FROM system_2fa_challenges c JOIN system_users u ON u.id=c.user_id
            WHERE c.token_hash=? AND c.expires_at>=? AND u.active=1 AND u.totp_enabled=1 LIMIT 1""",
            (str(token_hash),utc_now())).fetchone()
        if not row:
            return None
        result=dict(row)
        result["totp_secret"]=totp.unprotect_secret(result.get("totp_secret") or "")
        return result


def record_system_2fa_challenge_failure(token_hash):
    with _lock,_connect() as conn:
        conn.execute("UPDATE system_2fa_challenges SET attempts=attempts+1 WHERE token_hash=?",(str(token_hash),))
        row=conn.execute("SELECT attempts FROM system_2fa_challenges WHERE token_hash=?",(str(token_hash),)).fetchone()
        conn.commit()
        return int(row["attempts"] if row else 0)


def delete_system_2fa_challenge(token_hash):
    with _lock,_connect() as conn:
        conn.execute("DELETE FROM system_2fa_challenges WHERE token_hash=?",(str(token_hash),))
        conn.commit()


def system_2fa_failures(user_id, ip_hash, minutes=10):
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=max(1,int(minutes)))).isoformat()
    with _lock,_connect() as conn:
        conn.execute("DELETE FROM system_2fa_attempts WHERE ts<?",((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),))
        pair=int(conn.execute("""SELECT COUNT(*) FROM system_2fa_attempts
            WHERE user_id=? AND ip_hash=? AND success=0 AND ts>=?""",(int(user_id),str(ip_hash),cutoff)).fetchone()[0] or 0)
        user=int(conn.execute("""SELECT COUNT(*) FROM system_2fa_attempts
            WHERE user_id=? AND success=0 AND ts>=?""",(int(user_id),cutoff)).fetchone()[0] or 0)
        conn.commit()
        return {"pair":pair,"user":user}


def record_system_2fa_attempt(user_id, ip_hash, success):
    with _lock,_connect() as conn:
        if success:
            conn.execute("DELETE FROM system_2fa_attempts WHERE user_id=? AND ip_hash=?",(int(user_id),str(ip_hash)))
        else:
            conn.execute("INSERT INTO system_2fa_attempts(user_id,ip_hash,ts,success) VALUES(?,?,?,0)",(int(user_id),str(ip_hash),utc_now()))
        conn.commit()


def mark_system_login(user_id):
    with _lock, _connect() as conn:
        conn.execute("UPDATE system_users SET last_login_at=? WHERE id=?",(utc_now(),user_id)); conn.commit()


def create_system_session(token_hash, user_id, expires_at):
    with _lock, _connect() as conn:
        now=utc_now()
        conn.execute("DELETE FROM system_sessions WHERE expires_at < ?",(now,))
        conn.execute("INSERT INTO system_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",(token_hash,user_id,now,expires_at))
        conn.commit()


def system_user_for_session(token_hash):
    with _connect() as conn:
        row=conn.execute("""SELECT u.id,u.username,u.display_name,u.role,u.active,s.expires_at
            FROM system_sessions s JOIN system_users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>=? AND u.active=1 LIMIT 1""",(token_hash,utc_now())).fetchone()
        return dict(row) if row else None


def delete_system_session(token_hash):
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM system_sessions WHERE token_hash=?",(token_hash,)); conn.commit()


def delete_system_sessions_for_user(user_id, keep_token_hash=None):
    """End all sessions for an account, optionally preserving the current browser session."""
    with _lock, _connect() as conn:
        if keep_token_hash:
            conn.execute("DELETE FROM system_sessions WHERE user_id=? AND token_hash<>?",(user_id,keep_token_hash))
        else:
            conn.execute("DELETE FROM system_sessions WHERE user_id=?",(user_id,))
        conn.commit()


def active_system_session_counts(user_id=None):
    """Return active web-session counts without mutating session state."""
    now=utc_now()
    with _connect() as conn:
        total=int(conn.execute("SELECT COUNT(*) FROM system_sessions WHERE expires_at>=?",(now,)).fetchone()[0] or 0)
        mine=0
        if user_id is not None:
            mine=int(conn.execute("SELECT COUNT(*) FROM system_sessions WHERE user_id=? AND expires_at>=?",(int(user_id),now)).fetchone()[0] or 0)
        return {"total":total,"mine":mine}


def delete_other_system_sessions(user_id, keep_token_hash):
    """End every other session of one account while preserving this browser."""
    now=utc_now()
    with _lock,_connect() as conn:
        active_before=int(conn.execute("SELECT COUNT(*) FROM system_sessions WHERE user_id=? AND token_hash<>? AND expires_at>=?",(int(user_id),str(keep_token_hash),now)).fetchone()[0] or 0)
        conn.execute("DELETE FROM system_sessions WHERE user_id=? AND token_hash<>?",(int(user_id),str(keep_token_hash)))
        conn.commit()
        return active_before


def security_event_summary(hours=24):
    """Small aggregate used by the security dashboard without exposing event details."""
    hours=max(1,min(int(hours or 24),168))
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
    with _connect() as conn:
        web_failed=int(conn.execute("SELECT COUNT(*) FROM security_events WHERE ts>=? AND category='web_login' AND success=0",(cutoff,)).fetchone()[0] or 0)
        ocpp_rejected=int(conn.execute("SELECT COUNT(*) FROM security_events WHERE ts>=? AND category='ocpp_auth' AND success=0",(cutoff,)).fetchone()[0] or 0)
        critical=int(conn.execute("SELECT COUNT(*) FROM security_events WHERE ts>=? AND severity='critical'",(cutoff,)).fetchone()[0] or 0)
        warnings=int(conn.execute("SELECT COUNT(*) FROM security_events WHERE ts>=? AND severity='warning'",(cutoff,)).fetchone()[0] or 0)
    return {"hours":hours,"web_failed":web_failed,"ocpp_rejected":ocpp_rejected,"critical":critical,"warnings":warnings}


def public_charging_budgets(now=None):
    """Return only deliberately public charging-budget fields; never RFID/internal IDs."""
    now=now or datetime.now(timezone.utc)
    start_utc,end_utc,month_key=_month_bounds_utc(now)
    with _connect() as conn:
        users=conn.execute("""SELECT DISTINCT u.id,u.name,u.monthly_kwh_limit,u.monthly_limit_mode
            FROM users u LEFT JOIN rfid_cards r ON r.user_id=u.id AND r.status='Aktiv'
            WHERE u.status='Aktiv' AND (r.id IS NOT NULL OR TRIM(COALESCE(u.rfid,''))<>'')
            ORDER BY u.name COLLATE NOCASE""").fetchall()
        result=[]
        for u in users:
            used=round(_user_month_energy_conn(conn,u['id'],start_utc,end_utc),3)
            limit=u['monthly_kwh_limit']
            if limit is None or float(limit)<=0:
                result.append({'name':u['name'],'month':month_key,'unlimited':True,'remaining_kwh':None,'limit_kwh':None,'remaining_percent':100.0})
                continue
            limit=float(limit); remaining=max(0.0,limit-used); remaining_pct=max(0.0,min(100.0,remaining/limit*100.0))
            result.append({'name':u['name'],'month':month_key,'unlimited':False,'remaining_kwh':round(remaining,1),'limit_kwh':round(limit,1),'remaining_percent':round(remaining_pct,1)})
        return result

# V0.8.8 - Activity log and notification center

def add_activity(system_user_id=None, username=None, display_name=None, action="Aktion", category="system", target=None, method=None, path=None, details=None, status_code=None):
    """Persist a concise audit entry without storing request bodies or secrets."""
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO activity_log(ts,system_user_id,username,display_name,action,category,target,method,path,details,status_code)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (utc_now(), system_user_id, username, display_name, action, category, target, method, path, details, status_code),
        )
        conn.commit()


def recent_activity(limit=100, category=None, query=None):
    limit=max(1,min(int(limit or 100),500))
    clauses=[]; values=[]
    if category:
        clauses.append("category=?"); values.append(str(category))
    if query:
        q=f"%{str(query).strip()}%"
        clauses.append("(action LIKE ? OR target LIKE ? OR username LIKE ? OR display_name LIKE ? OR details LIKE ?)")
        values.extend([q,q,q,q,q])
    where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows=conn.execute(
            "SELECT * FROM activity_log"+where+" ORDER BY id DESC LIMIT ?", values+[limit]
        ).fetchall()
        return [dict(r) for r in rows]


def _severity_rank(value):
    return {"info":1,"warning":2,"critical":3}.get(str(value or "info"),1)


def _upsert_notification_conn(conn, key, source, severity, title, message=None, link=None, active=True, audience="all"):
    now=utc_now()
    existing=conn.execute("SELECT id,severity,active,source,last_seen_at,audience,revision FROM notifications WHERE notification_key=?",(key,)).fetchone()
    if existing:
        reactivated=not int(existing["active"] or 0) and bool(active)
        escalated=_severity_rank(severity)>_severity_rank(existing["severity"])
        seen_at=existing["last_seen_at"] if source == "event" and existing["source"] == "event" else now
        revision=int(existing["revision"] or 1)+(1 if (reactivated or escalated) else 0)
        conn.execute("""UPDATE notifications SET last_seen_at=?,source=?,severity=?,title=?,message=?,link=?,active=?,audience=?,revision=? WHERE id=?""",
                     (seen_at,source,severity,title,message,link,1 if active else 0,str(audience or "all"),revision,existing["id"]))
        if reactivated or escalated:
            conn.execute("DELETE FROM notification_reads WHERE notification_id=?",(existing["id"],))
        return int(existing["id"])
    cur=conn.execute("""INSERT INTO notifications(notification_key,created_at,last_seen_at,source,severity,title,message,link,active,audience,revision)
                        VALUES(?,?,?,?,?,?,?,?,?,?,1)""",(key,now,now,source,severity,title,message,link,1 if active else 0,str(audience or "all")))
    return int(cur.lastrowid)


def create_notification(key, severity, title, message=None, link=None, audience="all", source="event"):
    """Create/update an explicit notification, optionally restricted to a role."""
    with _lock, _connect() as conn:
        nid=_upsert_notification_conn(conn,str(key),str(source or "event"),str(severity or "info"),str(title),message,link,True,audience)
        conn.commit(); return nid


def deactivate_notification(key):
    with _lock, _connect() as conn:
        cur=conn.execute("UPDATE notifications SET active=0 WHERE notification_key=?",(str(key),))
        conn.commit(); return cur.rowcount>0


def _record_diagnostic_event_conn(conn, cp_id, severity, category, code, title, message=None, connector_id=None, transaction_id=None, state="event"):
    conn.execute("""INSERT INTO diagnostic_events(ts,charge_point_id,connector_id,transaction_id,severity,category,code,title,message,state)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""",
                 (utc_now(),str(cp_id),connector_id,transaction_id,severity,category,code,title,message,state))


def _sync_diagnostic_state_conn(conn, key, cp_id, severity, category, code, title, message=None, connector_id=None, transaction_id=None):
    now=utc_now()
    row=conn.execute("SELECT * FROM diagnostic_states WHERE state_key=?",(key,)).fetchone()
    if row is None:
        conn.execute("""INSERT INTO diagnostic_states(state_key,charge_point_id,connector_id,transaction_id,active,severity,category,code,title,message,first_seen_at,last_seen_at,last_changed_at)
                      VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?)""",
                     (key,str(cp_id),connector_id,transaction_id,severity,category,code,title,message,now,now,now))
        _record_diagnostic_event_conn(conn,cp_id,severity,category,code,title,message,connector_id,transaction_id,"opened")
        return
    was_active=bool(row["active"])
    changed=(str(row["severity"])!=str(severity) or str(row["message"] or "")!=str(message or "") or str(row["title"])!=str(title))
    conn.execute("""UPDATE diagnostic_states SET active=1,severity=?,category=?,code=?,title=?,message=?,connector_id=?,transaction_id=?,last_seen_at=?,last_changed_at=CASE WHEN ? THEN ? ELSE last_changed_at END WHERE state_key=?""",
                 (severity,category,code,title,message,connector_id,transaction_id,now,1 if (not was_active or changed) else 0,now,key))
    if not was_active:
        _record_diagnostic_event_conn(conn,cp_id,severity,category,code,title,message,connector_id,transaction_id,"opened")
    elif changed and _severity_rank(severity)>_severity_rank(row["severity"]):
        _record_diagnostic_event_conn(conn,cp_id,severity,category,code,title,message,connector_id,transaction_id,"updated")


def _resolve_missing_diagnostic_states_conn(conn, active_keys):
    rows=conn.execute("SELECT * FROM diagnostic_states WHERE active=1").fetchall()
    active=set(active_keys)
    now=utc_now()
    for row in rows:
        if row["state_key"] in active:
            continue
        conn.execute("UPDATE diagnostic_states SET active=0,last_seen_at=?,last_changed_at=? WHERE state_key=?",(now,now,row["state_key"]))
        _record_diagnostic_event_conn(conn,row["charge_point_id"],"info",row["category"],row["code"]+"_resolved",row["title"]+" behoben",row["message"],row["connector_id"],row["transaction_id"],"resolved")


def active_diagnostic_states(cp_id):
    with _connect() as conn:
        return [dict(r) for r in conn.execute("""SELECT * FROM diagnostic_states WHERE charge_point_id=? AND active=1
            ORDER BY CASE severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END DESC,last_changed_at DESC""",(str(cp_id),)).fetchall()]


def _event_diagnostic_row(row):
    event_type=str(row.get("event_type") or "")
    payload=str(row.get("payload") or "")
    low=payload.casefold()
    severity="info"; category="communication"; title=event_type or "OCPP-Ereignis"; code="ocpp_"+event_type.casefold()
    if event_type=="Connected": title="OCPP-Verbindung hergestellt"
    elif event_type=="Disconnected": severity="warning"; title="OCPP-Verbindung getrennt"
    elif event_type=="StatusNotification":
        category="connector"; title="Connector-Status geändert"
        if "status=faulted" in low or ("error=" in low and "error=noerror" not in low): severity="critical"; title="Connector meldet Fehler"
    elif event_type in ("RemoteStopTransaction","RemoteStartTransaction","UnlockConnector","ChangeAvailability","Reset"):
        category="remote"; title=event_type
        if "error=" in low or ("response=" in low and not any(x in low for x in ("response=accepted","response=unlocked","response=scheduled"))): severity="warning"
    elif event_type in ("BudgetLimitReached","ZeroFlowAutoStop"):
        category="session"; severity="warning"; title="Session-Automatik"
    elif event_type=="Reconciled": category="session"; title="Session administrativ abgeglichen"
    else:
        return None
    return {"id":"ocpp:"+str(row.get("id")),"ts":row.get("ts"),"charge_point_id":row.get("charge_point_id"),"connector_id":None,"transaction_id":row.get("transaction_id"),"severity":severity,"category":category,"code":code,"title":title,"message":payload,"state":"event"}


def diagnostic_history(cp_id, severity=None, category=None, limit=100):
    limit=max(1,min(int(limit or 100),250)); cp_id=str(cp_id)
    with _connect() as conn:
        synthetic=[dict(r) for r in conn.execute("SELECT * FROM diagnostic_events WHERE charge_point_id=? ORDER BY id DESC LIMIT 250",(cp_id,)).fetchall()]
        event_types=("Connected","Disconnected","StatusNotification","RemoteStopTransaction","RemoteStartTransaction","UnlockConnector","ChangeAvailability","Reset","BudgetLimitReached","ZeroFlowAutoStop","Reconciled")
        marks=','.join('?' for _ in event_types)
        raw=[dict(r) for r in conn.execute(f"SELECT * FROM events WHERE charge_point_id=? AND event_type IN ({marks}) ORDER BY id DESC LIMIT 250",(cp_id,*event_types)).fetchall()]
    items=synthetic+[x for x in (_event_diagnostic_row(r) for r in raw) if x]
    if severity: items=[x for x in items if str(x.get("severity"))==str(severity)]
    if category: items=[x for x in items if str(x.get("category"))==str(category)]
    items.sort(key=lambda x:str(x.get("ts") or ""),reverse=True)
    return items[:limit]


def charge_point_health(cp_id):
    cp_id=str(cp_id); now=datetime.now(timezone.utc)
    with _connect() as conn:
        cp=conn.execute("SELECT * FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        if not cp: return None
        stamps={}
        for kind in ("Heartbeat","StatusNotification","MeterValues"):
            row=conn.execute("SELECT ts FROM events WHERE charge_point_id=? AND event_type=? ORDER BY id DESC LIMIT 1",(cp_id,kind)).fetchone()
            stamps[kind]=row[0] if row else None
        last_session=conn.execute("SELECT id,ended_at,energy_kwh FROM transactions WHERE charge_point_id=? AND status='Completed' AND ended_at IS NOT NULL ORDER BY ended_at DESC,id DESC LIMIT 1",(cp_id,)).fetchone()
        active=conn.execute("SELECT id FROM transactions WHERE charge_point_id=? AND status='Active' AND ended_at IS NULL",(cp_id,)).fetchall()
        connectors=[dict(r) for r in conn.execute("SELECT connector_id,status,last_error_code,last_meter_at FROM connectors WHERE charge_point_id=? ORDER BY connector_id",(cp_id,)).fetchall()]
        issues=[dict(r) for r in conn.execute("""SELECT * FROM diagnostic_states WHERE charge_point_id=? AND active=1
            ORDER BY CASE severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END DESC,last_changed_at DESC""",(cp_id,)).fetchall()]
    def age(value):
        if not value:return None
        try:return max(0,int((now-datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone(timezone.utc)).total_seconds()))
        except Exception:return None
    status=str(cp["status"] or cp["station_status"] or "Unknown")
    severity="ok"; label="Gesund"
    if any(str(x.get("severity"))=="critical" for x in issues) or status=="Faulted": severity="critical"; label="Fehler"
    elif issues or status in ("Offline","Unavailable","Unknown"): severity="warning"; label="Auffällig"
    return {"status":severity,"label":label,"station_status":status,"last_message_at":cp["last_message_at"],"last_message_type":cp["last_message_type"],"last_message_age_seconds":age(cp["last_message_at"]),"last_heartbeat":stamps["Heartbeat"],"last_heartbeat_age_seconds":age(stamps["Heartbeat"]),"last_status_notification":stamps["StatusNotification"],"last_status_age_seconds":age(stamps["StatusNotification"]),"last_meter_values":stamps["MeterValues"],"last_meter_age_seconds":age(stamps["MeterValues"]),"last_successful_session":dict(last_session) if last_session else None,"active_sessions":len(active),"connectors":connectors,"active_issues":issues}


def sync_notifications():
    """Refresh state based notifications and ingest important OCPP events.

    This function is intentionally read-mostly and never changes OCPP/charging state.
    """
    start_utc,end_utc,month_key=_month_bounds_utc()
    now=datetime.now(timezone.utc)
    with _lock, _connect() as conn:
        # Track state keys and deactivate only conditions that disappeared. Historical/event notifications remain available.
        state_keys=[]
        diagnostic_keys=[]

        cps=conn.execute("""SELECT id,status,station_status,station_error_code,last_seen,last_message_at
                            FROM charge_points WHERE COALESCE(onboarded,1)=1 AND COALESCE(ignored,0)=0
                              AND COALESCE(retired,0)=0 AND COALESCE(archived,0)=0""").fetchall()
        for cp in cps:
            status=str(cp["status"] or cp["station_status"] or "Unknown")
            if status == "Faulted" or (cp["station_error_code"] and str(cp["station_error_code"]) not in ("NoError","None","")):
                err=str(cp["station_error_code"] or "Fehlerstatus")
                key=f"cp:{cp['id']}:fault"; state_keys.append(key)
                _upsert_notification_conn(conn,key,"state","critical","Ladepunkt meldet einen Fehler",
                                          f"{cp['id']} · {err}",f"/charge-points/{cp['id']}")
                dkey=f"health:{cp['id']}:fault"; diagnostic_keys.append(dkey)
                _sync_diagnostic_state_conn(conn,dkey,cp['id'],"critical","connector","station_fault","Ladepunkt meldet einen Fehler",err)
            elif status in ("Offline","Unavailable","Unknown"):
                key=f"cp:{cp['id']}:offline"; state_keys.append(key)
                _upsert_notification_conn(conn,key,"state","warning","Ladepunkt nicht verfügbar",
                                          f"{cp['id']} · Status {status}",f"/charge-points/{cp['id']}")
                dkey=f"health:{cp['id']}:offline"; diagnostic_keys.append(dkey)
                _sync_diagnostic_state_conn(conn,dkey,cp['id'],"warning","communication","station_offline","Ladepunkt nicht verfügbar",f"Status {status}")

        users=conn.execute("SELECT id,name,monthly_kwh_limit,monthly_limit_mode FROM users WHERE status='Aktiv'").fetchall()
        for user in users:
            if user["monthly_kwh_limit"] is None:
                continue
            used=_user_month_energy_conn(conn,user["id"],start_utc,end_utc)
            state=_budget_status(user["monthly_kwh_limit"],used,user["monthly_limit_mode"] or "warn")
            pct=float(state.get("percent") or 0)
            if pct < 70:
                continue
            if pct >= 100:
                threshold=100
                if state.get("blocked"):
                    sev,title="critical","Monatslimit erreicht – Laden gesperrt"
                else:
                    sev,title="critical","Monatslimit vollständig erreicht"
            elif pct >= 90:
                threshold=90; sev,title="warning","Monatsbudget bei mindestens 90 %"
            else:
                threshold=70; sev,title="warning","Monatsbudget bei mindestens 70 %"
            remaining=state.get("remaining_kwh")
            msg=f"{user['name']} · {pct:.0f} % verbraucht"
            if remaining is not None:
                msg+=f" · {float(remaining):.1f} kWh verbleibend"
            key=f"budget:{month_key}:{user['id']}:{threshold}"; state_keys.append(key)
            _upsert_notification_conn(conn,key,"state",sev,title,msg,"/users")

        active_rows=conn.execute("""SELECT t.id,t.started_at,t.charge_point_id,t.connector_id,u.name AS user_name,c.last_meter_at,cp.status AS cp_status
                                    FROM transactions t LEFT JOIN users u ON u.id=t.user_id
                                    LEFT JOIN connectors c ON c.charge_point_id=t.charge_point_id AND c.connector_id=t.connector_id
                                    LEFT JOIN charge_points cp ON cp.id=t.charge_point_id
                                    WHERE t.status='Active' AND t.ended_at IS NULL""").fetchall()
        for tx in active_rows:
            try:
                started=datetime.fromisoformat(str(tx["started_at"]).replace("Z","+00:00"))
                hours=(now-started.astimezone(timezone.utc)).total_seconds()/3600.0
            except Exception:
                hours=0
            if hours >= 4:
                key=f"session:{tx['id']}:long"; state_keys.append(key)
                _upsert_notification_conn(conn,key,"state","warning","Ungewöhnlich langer Ladevorgang",
                                          f"{tx['user_name'] or 'Unbekannter Benutzer'} · {tx['charge_point_id']} · seit {hours:.1f} h",
                                          f"/transactions/{tx['id']}")
            cp_status=str(tx['cp_status'] or 'Unknown')
            if cp_status not in ('Offline','Unavailable','Unknown','Faulted') and hours*3600 >= 300:
                try:
                    meter_age=(now-datetime.fromisoformat(str(tx['last_meter_at']).replace('Z','+00:00')).astimezone(timezone.utc)).total_seconds() if tx['last_meter_at'] else hours*3600
                except Exception:
                    meter_age=hours*3600
                if meter_age >= 300:
                    mins=max(5,int(meter_age//60)); sev='critical' if meter_age>=900 else 'warning'
                    title='Keine Live-Messwerte während aktiver Session'
                    msg=f"{tx['charge_point_id']} · Connector {tx['connector_id']} · seit {mins} min keine MeterValues"
                    key=f"session:{tx['id']}:meter-stale"; state_keys.append(key)
                    _upsert_notification_conn(conn,key,'state',sev,title,msg,f"/charge-points/{tx['charge_point_id']}?tab=diagnostics")
                    dkey=f"meter:{tx['id']}"; diagnostic_keys.append(dkey)
                    _sync_diagnostic_state_conn(conn,dkey,tx['charge_point_id'],sev,'telemetry','meter_values_stale',title,msg,int(tx['connector_id'] or 0),int(tx['id']))


        if state_keys:
            marks=','.join('?' for _ in state_keys)
            conn.execute(f"UPDATE notifications SET active=0 WHERE source='state' AND notification_key NOT IN ({marks})", state_keys)
        else:
            conn.execute("UPDATE notifications SET active=0 WHERE source='state'")

        _resolve_missing_diagnostic_states_conn(conn,diagnostic_keys)

        event_cutoff=(now-timedelta(hours=24)).isoformat()
        events=conn.execute("SELECT id,ts,charge_point_id,event_type,payload,transaction_id FROM events WHERE ts>=? ORDER BY id DESC LIMIT 250",(event_cutoff,)).fetchall()
        for ev in events:
            payload=str(ev["payload"] or "")
            severity=title=message=link=None
            if ev["event_type"] == "Authorize" and "accepted=False" in payload:
                severity="warning"; title="RFID-Autorisierung abgelehnt"; message=f"{ev['charge_point_id']} · {payload}"
            elif ev["event_type"] == "StartTransaction" and "rejected=true" in payload:
                severity="warning"; title="Ladevorgang abgelehnt"; message=f"{ev['charge_point_id']} · {payload}"
            elif ev["event_type"] == "BudgetLimitReached":
                severity="critical"; title="Monatslimit während Ladevorgang erreicht"; message=f"{ev['charge_point_id']} · RemoteStop wird ausgelöst"
            elif ev["event_type"] == "RemoteStopTransaction" and "error=" in payload:
                severity="critical"; title="RemoteStop fehlgeschlagen"; message=f"{ev['charge_point_id']} · {payload}"
            if severity:
                link=f"/transactions/{ev['transaction_id']}" if ev["transaction_id"] else f"/charge-points/{ev['charge_point_id']}"
                _upsert_notification_conn(conn,f"event:{ev['id']}","event",severity,title,message,link,True)
        cleanup_cutoff=(now-timedelta(days=7)).isoformat()
        conn.execute("UPDATE notifications SET active=0 WHERE source='event' AND created_at<?",(cleanup_cutoff,))
        conn.commit()


def _notification_role_conn(conn, user_id):
    row=conn.execute("SELECT role FROM system_users WHERE id=?",(int(user_id),)).fetchone()
    return str(row[0] if row else "viewer")


def notifications_for_user(user_id, limit=12):
    sync_notifications()
    limit=max(1,min(int(limit or 12),100))
    with _connect() as conn:
        role=_notification_role_conn(conn,user_id)
        audience=("all",role)
        visible="""n.active=1 AND n.audience IN (?,?)
            AND NOT EXISTS (
                SELECT 1 FROM notification_dismissals d
                WHERE d.notification_id=n.id AND d.user_id=? AND d.revision>=n.revision
            )"""
        unread=int(conn.execute(f"""SELECT COUNT(*) FROM notifications n
                                  LEFT JOIN notification_reads r ON r.notification_id=n.id AND r.user_id=?
                                  WHERE {visible} AND r.notification_id IS NULL""",(user_id,*audience,user_id)).fetchone()[0])
        rows=conn.execute(f"""SELECT n.*, CASE WHEN r.notification_id IS NULL THEN 0 ELSE 1 END AS is_read
                            FROM notifications n LEFT JOIN notification_reads r ON r.notification_id=n.id AND r.user_id=?
                            WHERE {visible} ORDER BY CASE WHEN r.notification_id IS NULL THEN 0 ELSE 1 END ASC,
                                     CASE n.severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END DESC,
                                     n.last_seen_at DESC LIMIT ?""",(user_id,*audience,user_id,limit)).fetchall()
        return {"unread":unread,"items":[dict(r) for r in rows]}


def mark_notification_read(notification_id, user_id):
    with _lock, _connect() as conn:
        role=_notification_role_conn(conn,user_id)
        if not conn.execute("SELECT 1 FROM notifications WHERE id=? AND active=1 AND audience IN ('all',?)",(notification_id,role)).fetchone():
            return False
        conn.execute("INSERT OR REPLACE INTO notification_reads(notification_id,user_id,read_at) VALUES(?,?,?)",
                     (notification_id,user_id,utc_now()))
        conn.commit(); return True


def dismiss_notification(notification_id, user_id):
    """Hide one notification only for this system user and current notification revision."""
    with _lock,_connect() as conn:
        role=_notification_role_conn(conn,user_id)
        row=conn.execute("SELECT id,revision FROM notifications WHERE id=? AND active=1 AND audience IN ('all',?)",(int(notification_id),role)).fetchone()
        if not row:
            return False
        conn.execute("""INSERT INTO notification_dismissals(notification_id,user_id,revision,dismissed_at)
            VALUES(?,?,?,?) ON CONFLICT(notification_id,user_id) DO UPDATE SET revision=excluded.revision,dismissed_at=excluded.dismissed_at""",
            (int(notification_id),int(user_id),int(row["revision"] or 1),utc_now()))
        conn.commit(); return True


def dismiss_read_notifications(user_id):
    """Hide all currently visible/read notifications for this user without deleting shared/system data."""
    with _lock,_connect() as conn:
        role=_notification_role_conn(conn,user_id)
        now=utc_now()
        rows=conn.execute("""SELECT n.id,n.revision FROM notifications n
            JOIN notification_reads r ON r.notification_id=n.id AND r.user_id=?
            WHERE n.active=1 AND n.audience IN ('all',?)
              AND NOT EXISTS (
                SELECT 1 FROM notification_dismissals d
                WHERE d.notification_id=n.id AND d.user_id=? AND d.revision>=n.revision
              )""",(int(user_id),role,int(user_id))).fetchall()
        conn.executemany("""INSERT INTO notification_dismissals(notification_id,user_id,revision,dismissed_at)
            VALUES(?,?,?,?) ON CONFLICT(notification_id,user_id) DO UPDATE SET revision=excluded.revision,dismissed_at=excluded.dismissed_at""",
            [(int(r["id"]),int(user_id),int(r["revision"] or 1),now) for r in rows])
        conn.commit(); return len(rows)


def _normalize_push_severity(value):
    value=str(value or "warning").strip().lower()
    return value if value in {"info","warning","critical"} else "warning"


def save_web_push_subscription(user_id, endpoint, p256dh, auth, min_severity="warning", user_agent=""):
    endpoint=str(endpoint or "").strip()
    p256dh=str(p256dh or "").strip()
    auth=str(auth or "").strip()
    if not endpoint or not p256dh or not auth:
        raise ValueError("Unvollständige Push-Subscription")
    severity=_normalize_push_severity(min_severity)
    now=utc_now()
    with _lock,_connect() as conn:
        existing=conn.execute("SELECT id FROM web_push_subscriptions WHERE endpoint=?",(endpoint,)).fetchone()
        if existing:
            sid=int(existing["id"])
            conn.execute("""UPDATE web_push_subscriptions SET user_id=?,p256dh=?,auth=?,min_severity=?,
                user_agent=?,updated_at=?,last_error=NULL WHERE id=?""",
                (int(user_id),p256dh,auth,severity,str(user_agent or "")[:500],now,sid))
        else:
            cur=conn.execute("""INSERT INTO web_push_subscriptions(
                user_id,endpoint,p256dh,auth,min_severity,user_agent,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
                (int(user_id),endpoint,p256dh,auth,severity,str(user_agent or "")[:500],now,now))
            sid=int(cur.lastrowid)
        role=_notification_role_conn(conn,user_id)
        rows=conn.execute("""SELECT n.id,n.revision FROM notifications n
            WHERE n.active=1 AND n.audience IN ('all',?)""",(role,)).fetchall()
        conn.executemany("""INSERT OR IGNORE INTO web_push_deliveries(
            subscription_id,notification_id,revision,delivered_at
        ) VALUES(?,?,?,?)""",[(sid,int(x["id"]),int(x["revision"] or 1),now) for x in rows])
        conn.commit()
        return {"id":sid,"min_severity":severity}


def update_web_push_preference(user_id, endpoint, min_severity):
    severity=_normalize_push_severity(min_severity)
    with _lock,_connect() as conn:
        cur=conn.execute("""UPDATE web_push_subscriptions SET min_severity=?,updated_at=?,last_error=NULL
            WHERE user_id=? AND endpoint=?""",(severity,utc_now(),int(user_id),str(endpoint or "").strip()))
        conn.commit()
        return int(cur.rowcount or 0)>0


def remove_web_push_subscription(user_id, endpoint):
    endpoint=str(endpoint or "").strip()
    with _lock,_connect() as conn:
        row=conn.execute("SELECT id FROM web_push_subscriptions WHERE user_id=? AND endpoint=?",(int(user_id),endpoint)).fetchone()
        if not row:
            return False
        sid=int(row["id"])
        conn.execute("DELETE FROM web_push_deliveries WHERE subscription_id=?",(sid,))
        conn.execute("DELETE FROM web_push_subscriptions WHERE id=?",(sid,))
        conn.commit()
        return True


def remove_web_push_subscription_by_id(subscription_id):
    with _lock,_connect() as conn:
        sid=int(subscription_id)
        conn.execute("DELETE FROM web_push_deliveries WHERE subscription_id=?",(sid,))
        cur=conn.execute("DELETE FROM web_push_subscriptions WHERE id=?",(sid,))
        conn.commit()
        return int(cur.rowcount or 0)>0


def web_push_subscriptions_for_user(user_id):
    with _connect() as conn:
        return [dict(r) for r in conn.execute("""SELECT id,endpoint,min_severity,created_at,updated_at,
            last_success_at,last_error FROM web_push_subscriptions WHERE user_id=? ORDER BY updated_at DESC""",
            (int(user_id),)).fetchall()]


def web_push_subscription_for_user_endpoint(user_id, endpoint):
    with _connect() as conn:
        row=conn.execute("""SELECT * FROM web_push_subscriptions WHERE user_id=? AND endpoint=?""",
            (int(user_id),str(endpoint or "").strip())).fetchone()
        return dict(row) if row else None


def pending_web_push_deliveries(limit=50):
    try: limit=max(1,min(250,int(limit or 50)))
    except (TypeError,ValueError): limit=50
    try:
        sync_notifications()
    except Exception:
        pass
    with _connect() as conn:
        rows=conn.execute("""SELECT s.id AS subscription_id,s.user_id,s.endpoint,s.p256dh,s.auth,
                   s.min_severity,s.user_agent,u.role,
                   n.id AS notification_id,n.revision,n.severity,n.title,n.message,n.link,n.last_seen_at
            FROM web_push_subscriptions s
            JOIN system_users u ON u.id=s.user_id AND u.active=1
            JOIN notifications n ON n.active=1 AND n.audience IN ('all',u.role)
            LEFT JOIN notification_reads r ON r.notification_id=n.id AND r.user_id=s.user_id
            LEFT JOIN web_push_deliveries d ON d.subscription_id=s.id AND d.notification_id=n.id AND d.revision=n.revision
            WHERE r.notification_id IS NULL AND d.subscription_id IS NULL
              AND CASE n.severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END >=
                  CASE s.min_severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END
            ORDER BY CASE n.severity WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END DESC,
                     n.last_seen_at ASC LIMIT ?""",(limit,)).fetchall()
        return [dict(r) for r in rows]


def mark_web_push_delivery(subscription_id, notification_id, revision):
    now=utc_now()
    with _lock,_connect() as conn:
        conn.execute("""INSERT OR REPLACE INTO web_push_deliveries(
            subscription_id,notification_id,revision,delivered_at
        ) VALUES(?,?,?,?)""",(int(subscription_id),int(notification_id),int(revision or 1),now))
        conn.execute("""UPDATE web_push_subscriptions SET last_success_at=?,last_error=NULL,updated_at=?
            WHERE id=?""",(now,now,int(subscription_id)))
        conn.commit()


def mark_web_push_success(subscription_id):
    now=utc_now()
    with _lock,_connect() as conn:
        conn.execute("""UPDATE web_push_subscriptions SET last_success_at=?,last_error=NULL,updated_at=?
            WHERE id=?""",(now,now,int(subscription_id)))
        conn.commit()


def mark_web_push_error(subscription_id, error):
    with _lock,_connect() as conn:
        conn.execute("UPDATE web_push_subscriptions SET last_error=?,updated_at=? WHERE id=?",
            (str(error or "")[:600],utc_now(),int(subscription_id)))
        conn.commit()


def mark_all_notifications_read(user_id):
    with _lock, _connect() as conn:
        role=_notification_role_conn(conn,user_id)
        rows=conn.execute("SELECT id FROM notifications WHERE active=1 AND audience IN ('all',?)",(role,)).fetchall()
        now=utc_now()
        conn.executemany("INSERT OR REPLACE INTO notification_reads(notification_id,user_id,read_at) VALUES(?,?,?)",
                         [(r[0],user_id,now) for r in rows])
        conn.commit(); return len(rows)


# V0.8.9.2 tariff and billing helpers. Prices are integer euro cents per kWh.
def _iso_dt(value):
    from datetime import datetime
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Ungültiger Gültigkeitszeitpunkt") from exc

def list_tariff_users():
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id,name,status,department,rfid FROM users ORDER BY name COLLATE NOCASE").fetchall()]

def list_tariff_charge_points():
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id,vendor,model,location,status FROM charge_points WHERE COALESCE(archived,0)=0 ORDER BY id COLLATE NOCASE").fetchall()]

def _tariff_target_label_conn(conn, scope, target_id):
    if scope=="global":
        return "Globaler Standard"
    if target_id in (None,""):
        return "—"
    if scope=="user":
        row=conn.execute("SELECT name FROM users WHERE id=?",(target_id,)).fetchone()
        return row[0] if row else f"Benutzer #{target_id}"
    if scope=="charge_point":
        row=conn.execute("SELECT id,location FROM charge_points WHERE id=?",(str(target_id),)).fetchone()
        return (f"{row['id']} · {row['location']}" if row and row['location'] else row['id']) if row else str(target_id)
    return str(target_id)

def list_tariffs():
    with _lock, _connect() as conn:
        rows=[dict(r) for r in conn.execute("SELECT * FROM tariffs ORDER BY scope,valid_from DESC,id DESC").fetchall()]
        for row in rows:
            row["target_label"]=_tariff_target_label_conn(conn,row["scope"],row.get("target_id"))
        return rows

def get_tariff(tariff_id):
    with _lock, _connect() as conn:
        row=conn.execute("SELECT * FROM tariffs WHERE id=?",(tariff_id,)).fetchone()
        if not row:
            return None
        item=dict(row)
        item["target_label"]=_tariff_target_label_conn(conn,item["scope"],item.get("target_id"))
        return item

def _validate_tariff_target_conn(conn, scope, target_id):
    if scope=="global":
        return None
    if target_id in (None,""):
        raise ValueError("Bitte ein konkretes Tarifziel auswählen")
    if scope=="user" and not conn.execute("SELECT id FROM users WHERE id=?",(target_id,)).fetchone():
        raise ValueError("Ladebenutzer nicht gefunden")
    if scope=="charge_point" and not conn.execute("SELECT id FROM charge_points WHERE id=?",(str(target_id),)).fetchone():
        raise ValueError("Ladepunkt nicht gefunden")
    return str(target_id)

def _validate_tariff_dates(valid_from, valid_until):
    start=_iso_dt(valid_from)
    end=_iso_dt(valid_until) if valid_until else None
    if end and end<=start:
        raise ValueError("Gültig bis muss nach Gültig ab liegen")

def create_tariff(name, scope, target_id, price_cents_per_kwh, valid_from, valid_until=None):
    if scope not in {"global","charge_point","user"}:
        raise ValueError("Ungültige Tarifebene")
    name=str(name or "").strip()
    if not name:
        raise ValueError("Tarifname ist erforderlich")
    cents=int(price_cents_per_kwh)
    if cents < 0:
        raise ValueError("Preis darf nicht negativ sein")
    _validate_tariff_dates(valid_from,valid_until)
    with _lock, _connect() as conn:
        target_id=_validate_tariff_target_conn(conn,scope,target_id)
        cur=conn.execute(
            "INSERT INTO tariffs(name,scope,target_id,price_cents_per_kwh,valid_from,valid_until,created_at) VALUES(?,?,?,?,?,?,?)",
            (name,scope,target_id,cents,valid_from,valid_until or None,utc_now()),
        )
        conn.commit()
        return cur.lastrowid

def create_tariff_version(tariff_id, name, price_cents_per_kwh, valid_from, valid_until=None):
    name=str(name or "").strip()
    if not name:
        raise ValueError("Tarifname ist erforderlich")
    cents=int(price_cents_per_kwh)
    if cents < 0:
        raise ValueError("Preis darf nicht negativ sein")
    _validate_tariff_dates(valid_from,valid_until)
    with _lock, _connect() as conn:
        old=conn.execute("SELECT * FROM tariffs WHERE id=?",(tariff_id,)).fetchone()
        if not old:
            raise ValueError("Tarif nicht gefunden")
        if _iso_dt(valid_from)<=_iso_dt(old["valid_from"]):
            raise ValueError("Die neue Tarifversion muss nach dem Start der bisherigen Version beginnen")
        old_until=old["valid_until"]
        if not old_until or _iso_dt(old_until)>_iso_dt(valid_from):
            conn.execute("UPDATE tariffs SET valid_until=? WHERE id=?",(valid_from,tariff_id))
        cur=conn.execute(
            "INSERT INTO tariffs(name,scope,target_id,price_cents_per_kwh,valid_from,valid_until,active,created_at) VALUES(?,?,?,?,?,?,1,?)",
            (name,old["scope"],old["target_id"],cents,valid_from,valid_until or None,utc_now()),
        )
        conn.commit()
        return cur.lastrowid

def delete_tariff(tariff_id):
    with _lock, _connect() as conn:
        cur=conn.execute("UPDATE tariffs SET active=0 WHERE id=?",(tariff_id,))
        conn.commit()
        return cur.rowcount > 0

def _select_tariff_conn(conn, user_id, cp_id, at):
    rows=conn.execute(
        "SELECT * FROM tariffs WHERE active=1 AND valid_from<=? AND (valid_until IS NULL OR valid_until>?) ORDER BY valid_from DESC,id DESC",
        (at,at),
    ).fetchall()
    for scope,target in (("user",str(user_id) if user_id else None),("charge_point",str(cp_id)),("global",None)):
        for row in rows:
            if row["scope"]!=scope:
                continue
            if scope!="global" and str(row["target_id"] or "")!=str(target):
                continue
            result=dict(row)
            result["source"]={"user":"Benutzer","charge_point":"Ladepunkt","global":"Global"}[scope]
            return result
    return None

def resolve_tariff(user_id=None, id_tag=None, cp_id=None, at=None):
    at=at or utc_now()
    with _lock, _connect() as conn:
        if user_id is None and id_tag:
            row=conn.execute("SELECT user_id FROM rfid_cards WHERE uid=?",(id_tag,)).fetchone()
            user_id=row[0] if row else None
        return _select_tariff_conn(conn,user_id,cp_id,at)

def _apply_tariff_to_tx_conn(conn, tx_id, user_id, cp_id, started_at):
    tariff=_select_tariff_conn(conn,user_id,cp_id,started_at)
    if not tariff:
        return
    conn.execute(
        "UPDATE transactions SET tariff_id=?,tariff_name=?,tariff_source=?,price_cents_per_kwh=? WHERE id=?",
        (tariff["id"],tariff["name"],tariff["source"],tariff["price_cents_per_kwh"],tx_id),
    )

def _update_tx_cost_conn(conn, tx_id):
    """Freeze the session cost from the tariff snapshot.

    Community monthly limits are usage/access limits, not implicit free-energy
    allowances. Without an explicit zero-price tariff, charged energy is billed
    at the stored tariff price.
    """
    row=conn.execute("SELECT id,energy_kwh,price_cents_per_kwh FROM transactions WHERE id=?",(tx_id,)).fetchone()
    if not row or row["energy_kwh"] is None:
        return
    energy=max(0.0,float(row["energy_kwh"] or 0))
    if row["price_cents_per_kwh"] is None:
        conn.execute("UPDATE transactions SET cost_cents=NULL WHERE id=?",(tx_id,))
        return
    cents=(Decimal(str(energy))*Decimal(int(row["price_cents_per_kwh"]))).quantize(Decimal("1"),rounding=ROUND_HALF_UP)
    conn.execute("UPDATE transactions SET cost_cents=? WHERE id=?",(int(cents),tx_id))

def diagnostic_summary(cp_id):
    caps=meter_capabilities_for_charge_point(cp_id)
    with _lock, _connect() as conn:
        events=conn.execute("SELECT event_type,COUNT(*) AS n FROM events WHERE charge_point_id=? GROUP BY event_type ORDER BY n DESC",(cp_id,)).fetchall()
        warnings=conn.execute("SELECT * FROM events WHERE charge_point_id=? AND (LOWER(event_type) LIKE '%fault%' OR LOWER(event_type) LIKE '%error%' OR LOWER(event_type) LIKE '%warning%') ORDER BY id DESC LIMIT 20",(cp_id,)).fetchall()
    return {"event_counts":[dict(x) for x in events],"warnings":[dict(x) for x in warnings],"telemetry_seen":sum(1 for x in caps if x.get("seen")),"telemetry_total":len(caps),"hints":[],"health":charge_point_health(cp_id),"history":diagnostic_history(cp_id,limit=60)}


BRANDING_DEFAULTS = {
    "product_name":"VoltCore Community",
    "organization_name":"Ihre Organisation",
    "display_name":"VoltCore Community",
    "product_subtitle":"Community Edition · OCPP Charging Management",
    "voucher_prefix":"VOLT",
    "primary_color":"#2563eb",
    "logo_light_url":"",
    "logo_dark_url":"",
    "favicon_url":"",
    "login_background_url":"",
    "app_background_url":"",
    "header_background_url":"",
}

def branding_settings():
    with _lock,_connect() as conn:
        rows=conn.execute("SELECT key,value FROM app_settings WHERE key LIKE 'branding_%'").fetchall()
    stored={str(r[0])[9:]:r[1] for r in rows}
    result={}
    asset_names={"logo_light_url","logo_dark_url","favicon_url","login_background_url","app_background_url","header_background_url"}
    for name,default in BRANDING_DEFAULTS.items():
        value=stored.get(name,default)
        result[name]=(str(value or "") if name in asset_names else (str(value or "").strip() or default))
    color=str(result.get("primary_color") or "#2563eb").strip()
    if len(color)!=7 or not color.startswith("#") or any(c not in "0123456789abcdefABCDEF" for c in color[1:]):
        color="#2563eb"
    result["primary_color"]=color.lower()
    r,g,b=(int(color[i:i+2],16) for i in (1,3,5))
    result["contrast_color"]="#111827" if (r*299+g*587+b*114)/1000 >= 150 else "#ffffff"
    return result

def get_setting(key, default=None):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT value FROM app_settings WHERE key=?",(str(key),)).fetchone()
        return row[0] if row else default


def set_setting(key, value):
    with _lock,_connect() as conn:
        conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(str(key),str(value),utc_now()))
        conn.commit()
        return True


def setting_bool(key, default=False):
    value=get_setting(key, "1" if default else "0")
    return str(value or "").strip().lower() in {"1","true","yes","on"}


def _setting_int(key, default, minimum=0, maximum=3650):
    try:
        value=int(get_setting(key, str(default)))
    except (TypeError, ValueError):
        value=int(default)
    return max(int(minimum), min(int(maximum), value))


def admin_reconcile_transaction(tx_id, reason="AdminReconciled"):
    tx=get_transaction(int(tx_id))
    if not tx: raise ValueError("Ladevorgang nicht gefunden")
    if tx.get("status") != "Active" or tx.get("ended_at"): raise ValueError("Ladevorgang ist nicht mehr aktiv")
    stop_transaction(int(tx_id),status="Interrupted",stop_reason=reason,ended_at=utc_now())
    add_event(tx.get("charge_point_id"),"Reconciled",f"transaction_id={tx_id}; reason={reason}",transaction_id=int(tx_id))
    return get_transaction(int(tx_id))

# V0.9.7.5 - Billing & reporting helpers

def _report_row_amounts(row):
    energy=float(row.get("energy_kwh") or 0.0)
    cost=row.get("cost_cents")
    return energy, (int(cost) if cost is not None else None)


def reporting_bundle(start_at=None, end_at=None, user_id=None, vehicle_id=None, charge_point_id=None):
    """Return immutable Community reporting data from completed transactions."""
    with _lock, _connect() as conn:
        clauses=["t.ended_at IS NOT NULL", "COALESCE(t.status,'') <> 'Active'"]; params=[]
        if start_at: clauses.append("t.started_at >= ?"); params.append(start_at)
        if end_at: clauses.append("t.started_at < ?"); params.append(end_at)
        if user_id is not None: clauses.append("t.user_id = ?"); params.append(int(user_id))
        if vehicle_id is not None: clauses.append("t.vehicle_id = ?"); params.append(int(vehicle_id))
        if charge_point_id: clauses.append("t.charge_point_id = ?"); params.append(str(charge_point_id))
        where=" AND ".join(clauses)
        rows=[dict(r) for r in conn.execute(f"""
            SELECT t.id,t.started_at,t.ended_at,t.charge_point_id,t.connector_id,t.energy_kwh,
                   t.charging_seconds,t.stand_seconds,t.connection_seconds,t.user_id,t.vehicle_id,
                   t.tariff_name,t.tariff_source,t.price_cents_per_kwh,t.cost_cents,t.timing_quality,
                   u.name AS user_name,u.department AS user_department,
                   v.name AS vehicle_name,v.plate AS vehicle_plate
              FROM transactions t LEFT JOIN users u ON u.id=t.user_id
              LEFT JOIN vehicles v ON v.id=t.vehicle_id
             WHERE {where} ORDER BY t.started_at DESC,t.id DESC
        """, params).fetchall()]

        def aggregate(items):
            sessions=len(items); energy=round(sum(float(x.get("energy_kwh") or 0) for x in items),3)
            known=[x for x in items if x.get("cost_cents") is not None]
            known_energy=sum(float(x.get("energy_kwh") or 0) for x in known); cost_cents=sum(int(x.get("cost_cents") or 0) for x in known)
            charging=sum(float(x.get("charging_seconds") or 0) for x in items); connection=sum(float(x.get("connection_seconds") or 0) for x in items); stand=sum(float(x.get("stand_seconds") or 0) for x in items)
            return {"sessions":sessions,"energy_kwh":round(energy,2),"cost_cents":cost_cents,"cost_eur":round(cost_cents/100.0,2),
                "cost_known_sessions":len(known),"cost_missing_sessions":sessions-len(known),
                "cost_coverage_pct":round(len(known)/sessions*100.0,1) if sessions else 100.0,
                "known_cost_energy_kwh":round(known_energy,2),"avg_price_cents_per_kwh":round(cost_cents/known_energy,2) if known_energy>0 else None,
                "avg_energy_kwh":round(energy/sessions,2) if sessions else 0.0,
                "charging_hours":round(charging/3600.0,2),"connection_hours":round(connection/3600.0,2),"stand_hours":round(stand/3600.0,2)}

        def group_by(key_fn,label_fn):
            groups={}
            for row in rows:
                key=key_fn(row); groups.setdefault(str(key),{"key":key,"label":label_fn(row),"rows":[]})["rows"].append(row)
            result=[]
            for bucket in groups.values():
                item={"key":bucket["key"],"label":bucket["label"]}; item.update(aggregate(bucket["rows"])); result.append(item)
            result.sort(key=lambda x:(-float(x.get("cost_cents") or 0),-float(x.get("energy_kwh") or 0),str(x.get("label") or "").lower()))
            return result

        berlin=ZoneInfo("Europe/Berlin"); monthly={}
        for row in rows:
            dt=_parse_iso_utc(row.get("started_at"))
            if not dt: continue
            local=dt.astimezone(berlin); key=local.strftime("%Y-%m")
            monthly.setdefault(key,{"key":key,"label":local.strftime("%m/%Y"),"rows":[]})["rows"].append(row)
        by_month=[]
        for key in sorted(monthly):
            bucket=monthly[key]; item={"key":key,"label":bucket["label"]}; item.update(aggregate(bucket["rows"])); by_month.append(item)

        summary=aggregate(rows)
        summary["unassigned_user_sessions"]=sum(1 for x in rows if x.get("user_id") is None)
        summary["unassigned_vehicle_sessions"]=sum(1 for x in rows if x.get("vehicle_id") is None)
        options={"users":[dict(r) for r in conn.execute("SELECT id,name FROM users ORDER BY name COLLATE NOCASE").fetchall()],
                 "vehicles":[dict(r) for r in conn.execute("SELECT id,name,plate FROM vehicles WHERE active=1 ORDER BY name COLLATE NOCASE").fetchall()],
                 "charge_points":[dict(r) for r in conn.execute("SELECT id FROM charge_points WHERE COALESCE(ignored,0)=0 AND COALESCE(retired,0)=0 AND COALESCE(archived,0)=0 ORDER BY id COLLATE NOCASE").fetchall()]}
        return {"summary":summary,
            "by_user":group_by(lambda x:x.get("user_id") if x.get("user_id") is not None else "unassigned",lambda x:x.get("user_name") or "Nicht zugeordnet"),
            "by_vehicle":group_by(lambda x:x.get("vehicle_id") if x.get("vehicle_id") is not None else "unassigned",lambda x:(x.get("vehicle_name") or "Nicht zugeordnet")+((" · "+x.get("vehicle_plate")) if x.get("vehicle_plate") else "")),
            "by_charge_point":group_by(lambda x:x.get("charge_point_id") or "unassigned",lambda x:x.get("charge_point_id") or "Nicht zugeordnet"),
            "by_month":by_month,"transactions":rows,"options":options}

# V0.9.7.9 - production security / OCPP access protection
OCPP_SECRET_ITERATIONS = 260000


def _ocpp_secret_hash(secret):
    secret=str(secret or "")
    if len(secret) < 16 or len(secret) > 128:
        raise ValueError("Das OCPP-Secret muss zwischen 16 und 128 Zeichen lang sein")
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac("sha256",secret.encode("utf-8"),salt,OCPP_SECRET_ITERATIONS)
    return f"pbkdf2_sha256${OCPP_SECRET_ITERATIONS}${salt.hex()}${digest.hex()}"


def _secret_ok(secret, encoded):
    try:
        scheme,iterations,salt_hex,digest_hex=str(encoded or "").split("$",3)
        if scheme != "pbkdf2_sha256": return False
        actual=hashlib.pbkdf2_hmac("sha256",str(secret or "").encode("utf-8"),bytes.fromhex(salt_hex),int(iterations)).hex()
        return hmac.compare_digest(actual,digest_hex)
    except Exception:
        return False


def security_settings():
    mode=str(get_setting("ocpp_auth_mode","off") or "off").strip().lower()
    if mode not in {"off","configured","required"}: mode="off"
    return {"auth_mode":mode,"reject_unknown":setting_bool("ocpp_reject_unknown",False),"require_tls":setting_bool("ocpp_require_tls",False),"require_subprotocol":setting_bool("ocpp_require_subprotocol",False)}


def set_security_settings(auth_mode="off", reject_unknown=False, require_tls=False, require_subprotocol=False):
    mode=str(auth_mode or "off").strip().lower()
    if mode not in {"off","configured","required"}:
        raise ValueError("Unbekannter OCPP-Authentifizierungsmodus")
    if mode == "required":
        with _connect() as conn:
            missing=[x[0] for x in conn.execute("""SELECT id FROM charge_points WHERE COALESCE(onboarded,1)=1 AND COALESCE(ignored,0)=0 AND COALESCE(retired,0)=0 AND COALESCE(archived,0)=0 AND COALESCE(ocpp_secret_hash,'')='' ORDER BY id""").fetchall()]
        if missing:
            preview=", ".join(missing[:5]) + (" …" if len(missing)>5 else "")
            raise ValueError(f"Strikter Modus nicht möglich: Für {len(missing)} Ladepunkt(e) fehlt ein OCPP-Secret ({preview})")
    set_setting("ocpp_auth_mode",mode)
    set_setting("ocpp_reject_unknown","1" if reject_unknown else "0")
    set_setting("ocpp_require_tls","1" if require_tls else "0")
    set_setting("ocpp_require_subprotocol","1" if require_subprotocol else "0")
    return security_settings()


def set_charge_point_secret(cp_id, secret):
    encoded=_ocpp_secret_hash(secret)
    now=utc_now()
    with _lock,_connect() as conn:
        cur=conn.execute("UPDATE charge_points SET ocpp_secret_hash=?,ocpp_secret_set_at=?,ocpp_auth_last_reason=NULL WHERE id=?",(encoded,now,str(cp_id)))
        conn.commit()
        if not cur.rowcount: raise ValueError("Ladepunkt nicht gefunden")
    return True


def clear_charge_point_secret(cp_id):
    with _lock,_connect() as conn:
        cur=conn.execute("UPDATE charge_points SET ocpp_secret_hash=NULL,ocpp_secret_set_at=NULL,ocpp_auth_last_reason=NULL WHERE id=?",(str(cp_id),))
        conn.commit(); return cur.rowcount>0


def verify_charge_point_secret(cp_id, secret):
    with _connect() as conn:
        row=conn.execute("SELECT ocpp_secret_hash FROM charge_points WHERE id=?",(str(cp_id),)).fetchone()
        return bool(row and row[0] and _secret_ok(secret,row[0]))


def charge_point_security(cp_id):
    with _connect() as conn:
        row=conn.execute("""SELECT id,location,onboarded,ignored,retired,archived,ocpp_secret_set_at,ocpp_auth_last_success_at,
                    ocpp_auth_last_failure_at,ocpp_auth_last_reason,ocpp_last_transport,ocpp_last_remote,
                    CASE WHEN COALESCE(ocpp_secret_hash,'')<>'' THEN 1 ELSE 0 END AS secret_configured
                    FROM charge_points WHERE id=?""",(str(cp_id),)).fetchone()
        return dict(row) if row else None


def list_charge_point_security():
    with _connect() as conn:
        rows=conn.execute("""SELECT id,location,onboarded,ignored,retired,archived,status,last_seen,ocpp_secret_set_at,ocpp_auth_last_success_at,
                    ocpp_auth_last_failure_at,ocpp_auth_last_reason,ocpp_last_transport,ocpp_last_remote,
                    CASE WHEN COALESCE(ocpp_secret_hash,'')<>'' THEN 1 ELSE 0 END AS secret_configured
                    FROM charge_points WHERE COALESCE(ignored,0)=0 ORDER BY COALESCE(location,''),id""").fetchall()
        return [dict(r) for r in rows]


def add_security_event(event_type, severity="info", category="security", charge_point_id=None, system_user_id=None, username=None, remote=None, success=None, detail=None):
    with _lock,_connect() as conn:
        conn.execute("""INSERT INTO security_events(ts,severity,category,event_type,charge_point_id,system_user_id,username,remote,success,detail)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",(utc_now(),str(severity or "info"),str(category or "security"),str(event_type),charge_point_id,system_user_id,username,remote,None if success is None else (1 if success else 0),detail))
        conn.commit()


def recent_security_events(limit=100, category=None):
    limit=max(1,min(int(limit or 100),500)); clauses=[]; values=[]
    if category:
        clauses.append("category=?"); values.append(str(category))
    where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows=conn.execute("SELECT * FROM security_events"+where+" ORDER BY id DESC LIMIT ?",values+[limit]).fetchall()
        return [dict(r) for r in rows]


def security_events_page(page=1, page_size=10, category=None):
    page_size=max(1,min(int(page_size or 10),100))
    page=max(1,int(page or 1))
    clauses=[]; values=[]
    if category:
        clauses.append("category=?"); values.append(str(category))
    where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        total=int(conn.execute("SELECT COUNT(*) FROM security_events"+where,values).fetchone()[0] or 0)
        pages=max(1,(total+page_size-1)//page_size)
        page=min(page,pages)
        offset=(page-1)*page_size
        rows=conn.execute("SELECT * FROM security_events"+where+" ORDER BY id DESC LIMIT ? OFFSET ?",values+[page_size,offset]).fetchall()
        return {"items":[dict(r) for r in rows],"page":page,"page_size":page_size,"pages":pages,"total":total}


def record_ocpp_security_result(cp_id, success, reason=None, transport=None, remote=None):
    now=utc_now(); cp_id=str(cp_id or "")
    with _lock,_connect() as conn:
        row=conn.execute("SELECT id FROM charge_points WHERE id=?",(cp_id,)).fetchone()
        if row:
            if success:
                conn.execute("UPDATE charge_points SET ocpp_auth_last_success_at=?,ocpp_auth_last_reason=?,ocpp_last_transport=COALESCE(?,ocpp_last_transport),ocpp_last_remote=COALESCE(?,ocpp_last_remote) WHERE id=?",(now,reason,transport,remote,cp_id))
            else:
                conn.execute("UPDATE charge_points SET ocpp_auth_last_failure_at=?,ocpp_auth_last_reason=?,ocpp_last_transport=COALESCE(?,ocpp_last_transport),ocpp_last_remote=COALESCE(?,ocpp_last_remote) WHERE id=?",(now,reason,transport,remote,cp_id))
        conn.execute("""INSERT INTO security_events(ts,severity,category,event_type,charge_point_id,remote,success,detail)
                        VALUES(?,?,?,?,?,?,?,?)""",(now,"info" if success else "warning","ocpp_auth","OCPP authentication accepted" if success else "OCPP connection rejected",cp_id or None,remote,1 if success else 0,reason))
        conn.commit()


def web_login_failures(ip_hash, username_key, minutes=10):
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=max(1,int(minutes)))).isoformat()
    with _lock,_connect() as conn:
        conn.execute("DELETE FROM web_login_attempts WHERE ts<?",((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),))
        pair=conn.execute("SELECT COUNT(*) FROM web_login_attempts WHERE ip_hash=? AND username_key=? AND success=0 AND ts>=?",(str(ip_hash),str(username_key),cutoff)).fetchone()[0]
        ip_total=conn.execute("SELECT COUNT(*) FROM web_login_attempts WHERE ip_hash=? AND success=0 AND ts>=?",(str(ip_hash),cutoff)).fetchone()[0]
        return {"pair":int(pair or 0),"ip":int(ip_total or 0)}


def record_web_login_attempt(ip_hash, username_key, success):
    with _lock,_connect() as conn:
        if success:
            conn.execute("DELETE FROM web_login_attempts WHERE ip_hash=? AND username_key=?",(str(ip_hash),str(username_key)))
        else:
            conn.execute("INSERT INTO web_login_attempts(ip_hash,username_key,ts,success) VALUES(?,?,?,0)",(str(ip_hash),str(username_key),utc_now()))
        conn.commit()

def ocpp_auth_client_key(cp_id, remote_host):
    raw=f"{str(cp_id or '').strip()}|{str(remote_host or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ocpp_auth_failures(client_key, minutes=None):
    minutes=OCPP_AUTH_FAILURE_WINDOW_MINUTES if minutes is None else max(1,int(minutes))
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=minutes)).isoformat()
    with _lock,_connect() as conn:
        conn.execute("DELETE FROM ocpp_auth_attempts WHERE ts<?",((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),))
        count=conn.execute("SELECT COUNT(*) FROM ocpp_auth_attempts WHERE client_key=? AND success=0 AND ts>=?",(str(client_key),cutoff)).fetchone()[0]
        conn.commit()
        return int(count or 0)


def record_ocpp_auth_attempt(client_key, cp_id, success):
    with _lock,_connect() as conn:
        if success:
            conn.execute("DELETE FROM ocpp_auth_attempts WHERE client_key=?",(str(client_key),))
        else:
            conn.execute("INSERT INTO ocpp_auth_attempts(client_key,charge_point_id,ts,success) VALUES(?,?,?,0)",(str(client_key),str(cp_id or '') or None,utc_now()))
        conn.commit()


def global_search(query,limit_per_group=6):
    q=str(query or "").strip(); limit=max(1,min(int(limit_per_group or 6),10))
    if len(q)<2:return {"query":q,"groups":[],"total":0}
    like=f"%{q}%"; groups=[]
    with _lock,_connect() as conn:
        users=[dict(r) for r in conn.execute("""SELECT u.id,u.name,u.department,u.status FROM users u WHERE u.name LIKE ? COLLATE NOCASE OR COALESCE(u.department,'') LIKE ? COLLATE NOCASE OR COALESCE(u.rfid,'') LIKE ? COLLATE NOCASE OR EXISTS(SELECT 1 FROM rfid_cards r WHERE r.user_id=u.id AND (r.uid LIKE ? COLLATE NOCASE OR COALESCE(r.label,'') LIKE ? COLLATE NOCASE)) ORDER BY u.name LIMIT ?""",(like,like,like,like,like,limit)).fetchall()]
        if users: groups.append({"key":"users","label":"Ladebenutzer / RFID","items":[{"title":r['name'],"subtitle":" · ".join(x for x in [r.get('department'),r.get('status')] if x),"url":f"/users?user={r['id']}"} for r in users]})
        vehicles=[dict(r) for r in conn.execute("SELECT id,name,plate,make,model FROM vehicles WHERE active=1 AND (name LIKE ? COLLATE NOCASE OR COALESCE(plate,'') LIKE ? COLLATE NOCASE OR COALESCE(make,'') LIKE ? COLLATE NOCASE OR COALESCE(model,'') LIKE ? COLLATE NOCASE) ORDER BY name LIMIT ?",(like,like,like,like,limit)).fetchall()]
        if vehicles: groups.append({"key":"vehicles","label":"Fahrzeuge","items":[{"title":r['name'],"subtitle":" · ".join(x for x in [r.get('plate'),r.get('make'),r.get('model')] if x),"url":f"/vehicles?vehicle={r['id']}"} for r in vehicles]})
        cps=[dict(r) for r in conn.execute("SELECT id,vendor,model,location,status FROM charge_points WHERE COALESCE(ignored,0)=0 AND (id LIKE ? COLLATE NOCASE OR COALESCE(vendor,'') LIKE ? COLLATE NOCASE OR COALESCE(model,'') LIKE ? COLLATE NOCASE OR COALESCE(location,'') LIKE ? COLLATE NOCASE OR COALESCE(serial_number,'') LIKE ? COLLATE NOCASE) ORDER BY id LIMIT ?",(like,like,like,like,like,limit)).fetchall()]
        if cps: groups.append({"key":"charge_points","label":"Ladepunkte","items":[{"title":r['id'],"subtitle":" · ".join(x for x in [r.get('vendor'),r.get('model'),r.get('location'),r.get('status')] if x),"url":f"/charge-points/{r['id']}"} for r in cps]})
        txs=[dict(r) for r in conn.execute("""SELECT t.id,t.charge_point_id,t.id_tag,t.status,u.name AS user_name,v.name AS vehicle_name FROM transactions t LEFT JOIN users u ON u.id=t.user_id LEFT JOIN vehicles v ON v.id=t.vehicle_id WHERE CAST(t.id AS TEXT) LIKE ? OR COALESCE(t.transaction_id,'') LIKE ? OR t.charge_point_id LIKE ? COLLATE NOCASE OR COALESCE(t.id_tag,'') LIKE ? COLLATE NOCASE OR COALESCE(u.name,'') LIKE ? COLLATE NOCASE OR COALESCE(v.name,'') LIKE ? COLLATE NOCASE OR COALESCE(v.plate,'') LIKE ? COLLATE NOCASE ORDER BY t.id DESC LIMIT ?""",(like,like,like,like,like,like,like,limit)).fetchall()]
        if txs: groups.append({"key":"transactions","label":"Ladevorgänge","items":[{"title":f"Ladevorgang #{r['id']}","subtitle":" · ".join(x for x in [r.get('user_name'),r.get('vehicle_name'),r.get('charge_point_id'),r.get('status')] if x),"url":f"/transactions/{r['id']}"} for r in txs]})
    return {"query":q,"groups":groups,"total":sum(len(g['items']) for g in groups)}
