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
                gamification_enabled INTEGER NOT NULL DEFAULT 0,
                gamification_seen_award_id INTEGER,
                gamification_seen_level INTEGER,
                weekly_hours REAL,
                budget_source TEXT NOT NULL DEFAULT 'manual',
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
            CREATE TABLE IF NOT EXISTS load_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                condition_text TEXT,
                action_text TEXT,
                priority TEXT NOT NULL DEFAULT 'Normal',
                active INTEGER NOT NULL DEFAULT 1
            );
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
            CREATE TABLE IF NOT EXISTS portal_pin_reset_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_hash TEXT NOT NULL,
                ip_hash TEXT NOT NULL,
                user_id INTEGER,
                requested_at TEXT NOT NULL,
                mail_sent INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS portal_pin_reset_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS access_request_verifications (
                token_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                email_hash TEXT NOT NULL,
                ip_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS access_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Neu',
                name TEXT NOT NULL,
                street TEXT NOT NULL,
                postal_code TEXT NOT NULL,
                city TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                vehicle_make_model TEXT,
                vehicle_plate TEXT NOT NULL,
                weekly_hours REAL,
                field_values_json TEXT,
                field_schema_json TEXT,
                approved_budget_kwh REAL,
                budget_source TEXT,
                terms_version TEXT NOT NULL,
                terms_snapshot TEXT NOT NULL,
                signature_path TEXT NOT NULL,
                signed_at TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                ip_hash TEXT NOT NULL,
                admin_note TEXT,
                decision_at TEXT,
                decided_by INTEGER,
                user_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS rfid_enrollment_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                charge_point_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                candidate_uid TEXT,
                detected_at TEXT,
                completed_at TEXT,
                message TEXT
            );
            CREATE TABLE IF NOT EXISTS billing_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                cost_center TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_billing_groups (
                user_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                assigned_at TEXT NOT NULL,
                PRIMARY KEY(user_id, group_id)
            );
            CREATE TABLE IF NOT EXISTS tariffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                scope TEXT NOT NULL,
                target_id TEXT,
                price_cents_per_kwh INTEGER NOT NULL,
                valid_from TEXT NOT NULL,
                valid_until TEXT,
                cost_center TEXT,
                billing_group_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS portal_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS portal_login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_hash TEXT NOT NULL,
                ts TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                icon TEXT,
                metric TEXT NOT NULL DEFAULT 'manual',
                threshold REAL,
                hidden INTEGER NOT NULL DEFAULT 0,
                system_secret INTEGER NOT NULL DEFAULT 0,
                category TEXT NOT NULL DEFAULT 'Allgemein',
                rarity TEXT NOT NULL DEFAULT 'common',
                xp INTEGER NOT NULL DEFAULT 50,
                tier_group TEXT,
                tier_name TEXT,
                tier_rank INTEGER NOT NULL DEFAULT 0,
                seed_key TEXT,
                leaderboard_enabled INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS achievement_awards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                achievement_id INTEGER NOT NULL,
                awarded_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'automatic',
                UNIQUE(user_id, achievement_id)
            );
            CREATE TABLE IF NOT EXISTS gamification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                metric TEXT NOT NULL DEFAULT 'energy_kwh',
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                min_sessions INTEGER NOT NULL DEFAULT 0,
                min_session_kwh REAL NOT NULL DEFAULT 0,
                reward_bonus_kwh REAL NOT NULL DEFAULT 0,
                reward_valid_days INTEGER,
                reward_bonus_enabled INTEGER NOT NULL DEFAULT 0,
                winner_badge_enabled INTEGER NOT NULL DEFAULT 0,
                winner_badge_name TEXT,
                winner_badge_icon TEXT,
                winner_badge_description TEXT,
                winner_achievement_id INTEGER,
                finalized_at TEXT
            );
            CREATE TABLE IF NOT EXISTS gamification_event_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                bonus_grant_id INTEGER NOT NULL,
                amount_kwh REAL NOT NULL,
                granted_at TEXT NOT NULL,
                UNIQUE(event_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS gamification_event_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rank INTEGER,
                metric_value REAL,
                qualified INTEGER NOT NULL DEFAULT 0,
                sessions INTEGER NOT NULL DEFAULT 0,
                energy_kwh REAL NOT NULL DEFAULT 0,
                details_json TEXT,
                captured_at TEXT NOT NULL,
                UNIQUE(event_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS bonus_vouchers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                amount_kwh REAL NOT NULL,
                redeem_until TEXT,
                bonus_valid_days INTEGER NOT NULL DEFAULT 90,
                max_redemptions INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                note TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bonus_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount_kwh REAL NOT NULL,
                remaining_kwh REAL NOT NULL,
                granted_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'admin',
                note TEXT,
                voucher_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS bonus_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grant_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                transaction_id INTEGER NOT NULL,
                amount_kwh REAL NOT NULL,
                used_at TEXT NOT NULL,
                UNIQUE(grant_id, transaction_id)
            );
            CREATE TABLE IF NOT EXISTS bonus_voucher_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                grant_id INTEGER NOT NULL,
                redeemed_at TEXT NOT NULL,
                UNIQUE(voucher_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS bonus_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER NOT NULL,
                to_user_id INTEGER NOT NULL,
                source_grant_id INTEGER NOT NULL,
                target_grant_id INTEGER NOT NULL,
                amount_kwh REAL NOT NULL,
                transferred_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS rfid_replacement_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT 'replacement',
                status TEXT NOT NULL DEFAULT 'Offen',
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_note TEXT
            );
            CREATE TABLE IF NOT EXISTS cost_centers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL,
                description TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
        if "max_concurrent_sessions" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN max_concurrent_sessions INTEGER NOT NULL DEFAULT 1")
        card_columns = {r[1] for r in conn.execute("PRAGMA table_info(rfid_cards)").fetchall()}
        if "max_concurrent_sessions" not in card_columns:
            conn.execute("ALTER TABLE rfid_cards ADD COLUMN max_concurrent_sessions INTEGER NOT NULL DEFAULT 1")
        for col in ("email", "phone"):
            if col not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        if "monthly_kwh_limit" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN monthly_kwh_limit REAL")
        if "monthly_limit_mode" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN monthly_limit_mode TEXT NOT NULL DEFAULT 'warn'")
        if "gamification_enabled" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN gamification_enabled INTEGER NOT NULL DEFAULT 0")
        if "weekly_hours" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN weekly_hours REAL")
        if "budget_source" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN budget_source TEXT NOT NULL DEFAULT 'manual'")
        if "image_path" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN image_path TEXT")
        if "charge_access_mode" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN charge_access_mode TEXT NOT NULL DEFAULT 'all'")
        user_columns = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        for name, statement in {
            "portal_pin_hash": "ALTER TABLE users ADD COLUMN portal_pin_hash TEXT",
            "portal_pin_set_at": "ALTER TABLE users ADD COLUMN portal_pin_set_at TEXT",
            "portal_enabled": "ALTER TABLE users ADD COLUMN portal_enabled INTEGER NOT NULL DEFAULT 0",
            "portal_last_login_at": "ALTER TABLE users ADD COLUMN portal_last_login_at TEXT",
            "gamification_seen_award_id": "ALTER TABLE users ADD COLUMN gamification_seen_award_id INTEGER",
            "gamification_seen_level": "ALTER TABLE users ADD COLUMN gamification_seen_level INTEGER",
        }.items():
            if name not in user_columns:
                conn.execute(statement)
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
        cp_columns = {r[1] for r in conn.execute("PRAGMA table_info(charge_points)").fetchall()}
        if "rfid_self_enroll_mode" not in cp_columns:
            conn.execute("ALTER TABLE charge_points ADD COLUMN rfid_self_enroll_mode TEXT NOT NULL DEFAULT 'auto'")
        notification_columns = {r[1] for r in conn.execute("PRAGMA table_info(notifications)").fetchall()}
        if "audience" not in notification_columns:
            conn.execute("ALTER TABLE notifications ADD COLUMN audience TEXT NOT NULL DEFAULT 'all'")
        if "revision" not in notification_columns:
            conn.execute("ALTER TABLE notifications ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        access_request_columns = {r[1] for r in conn.execute("PRAGMA table_info(access_requests)").fetchall()}
        for name, statement in {
            "weekly_hours": "ALTER TABLE access_requests ADD COLUMN weekly_hours REAL",
            "field_values_json": "ALTER TABLE access_requests ADD COLUMN field_values_json TEXT",
            "field_schema_json": "ALTER TABLE access_requests ADD COLUMN field_schema_json TEXT",
            "approved_budget_kwh": "ALTER TABLE access_requests ADD COLUMN approved_budget_kwh REAL",
            "budget_source": "ALTER TABLE access_requests ADD COLUMN budget_source TEXT",
        }.items():
            if name not in access_request_columns:
                conn.execute(statement)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_portal_pin_reset_email ON portal_pin_reset_requests(email_hash,requested_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_portal_pin_reset_ip ON portal_pin_reset_requests(ip_hash,requested_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_portal_pin_reset_tokens_expiry ON portal_pin_reset_tokens(expires_at,used_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_access_verify_email ON access_request_verifications(email_hash,created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_access_requests_status ON access_requests(status,created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rfid_enrollment_active ON rfid_enrollment_sessions(charge_point_id,status,expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rfid_events_card ON rfid_card_events(card_id,created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rfid_requests_status ON rfid_replacement_requests(status,created_at)")
        conn.execute("UPDATE rfid_cards SET issued_at=COALESCE(issued_at,created_at) WHERE issued_at IS NULL")
        achievement_columns = {r[1] for r in conn.execute("PRAGMA table_info(achievements)").fetchall()}
        leaderboard_enabled_added = "leaderboard_enabled" not in achievement_columns
        for name, statement in {
            "system_secret": "ALTER TABLE achievements ADD COLUMN system_secret INTEGER NOT NULL DEFAULT 0",
            "category": "ALTER TABLE achievements ADD COLUMN category TEXT NOT NULL DEFAULT 'Allgemein'",
            "rarity": "ALTER TABLE achievements ADD COLUMN rarity TEXT NOT NULL DEFAULT 'common'",
            "xp": "ALTER TABLE achievements ADD COLUMN xp INTEGER NOT NULL DEFAULT 50",
            "tier_group": "ALTER TABLE achievements ADD COLUMN tier_group TEXT",
            "tier_name": "ALTER TABLE achievements ADD COLUMN tier_name TEXT",
            "tier_rank": "ALTER TABLE achievements ADD COLUMN tier_rank INTEGER NOT NULL DEFAULT 0",
            "seed_key": "ALTER TABLE achievements ADD COLUMN seed_key TEXT",
            "leaderboard_enabled": "ALTER TABLE achievements ADD COLUMN leaderboard_enabled INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in achievement_columns:
                conn.execute(statement)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_achievements_seed_key ON achievements(seed_key) WHERE seed_key IS NOT NULL")
        if leaderboard_enabled_added:
            conn.execute("""UPDATE achievements SET leaderboard_enabled=1 WHERE seed_key IN (
                'energy-100','sessions-10','early-5','evening-10','night-5','weekend-10',
                'active-days-30','months-3','week-streak-4','cp-2','vehicles-2','single-30',
                'peak-11','max-month-energy-500','max-month-sessions-40','charging-hours-100',
                'long-10','quick-20'
            ) AND COALESCE(system_secret,0)=0""")
        gamification_event_columns = {r[1] for r in conn.execute("PRAGMA table_info(gamification_events)").fetchall()}
        reward_enabled_added = "reward_bonus_enabled" not in gamification_event_columns
        for name, statement in {
            "min_sessions": "ALTER TABLE gamification_events ADD COLUMN min_sessions INTEGER NOT NULL DEFAULT 0",
            "min_session_kwh": "ALTER TABLE gamification_events ADD COLUMN min_session_kwh REAL NOT NULL DEFAULT 0",
            "reward_bonus_kwh": "ALTER TABLE gamification_events ADD COLUMN reward_bonus_kwh REAL NOT NULL DEFAULT 0",
            "reward_valid_days": "ALTER TABLE gamification_events ADD COLUMN reward_valid_days INTEGER",
            "reward_bonus_enabled": "ALTER TABLE gamification_events ADD COLUMN reward_bonus_enabled INTEGER NOT NULL DEFAULT 0",
            "winner_badge_enabled": "ALTER TABLE gamification_events ADD COLUMN winner_badge_enabled INTEGER NOT NULL DEFAULT 0",
            "winner_badge_name": "ALTER TABLE gamification_events ADD COLUMN winner_badge_name TEXT",
            "winner_badge_icon": "ALTER TABLE gamification_events ADD COLUMN winner_badge_icon TEXT",
            "winner_badge_description": "ALTER TABLE gamification_events ADD COLUMN winner_badge_description TEXT",
            "winner_achievement_id": "ALTER TABLE gamification_events ADD COLUMN winner_achievement_id INTEGER",
            "finalized_at": "ALTER TABLE gamification_events ADD COLUMN finalized_at TEXT",
        }.items():
            if name not in gamification_event_columns:
                conn.execute(statement)
        if reward_enabled_added:
            # Preserve all V0.9.5 events that already had a configured kWh reward.
            conn.execute("UPDATE gamification_events SET reward_bonus_enabled=1 WHERE COALESCE(reward_bonus_kwh,0)>0")
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
            "cost_center": "ALTER TABLE transactions ADD COLUMN cost_center TEXT",
            "billing_group_id": "ALTER TABLE transactions ADD COLUMN billing_group_id INTEGER",
            "billing_group_name": "ALTER TABLE transactions ADD COLUMN billing_group_name TEXT",
            "import_source": "ALTER TABLE transactions ADD COLUMN import_source TEXT",
            "import_key": "ALTER TABLE transactions ADD COLUMN import_key TEXT",
            "imported_at": "ALTER TABLE transactions ADD COLUMN imported_at TEXT",
            "import_source_name": "ALTER TABLE transactions ADD COLUMN import_source_name TEXT",
            "import_evse_id": "ALTER TABLE transactions ADD COLUMN import_evse_id TEXT",
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
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_import_source_key ON transactions(import_source,import_key) WHERE import_source IS NOT NULL AND import_key IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bonus_grants_user_expiry ON bonus_grants(user_id,active,expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bonus_usage_transaction ON bonus_usage(transaction_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bonus_voucher_redemptions_voucher ON bonus_voucher_redemptions(voucher_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bonus_transfers_from ON bonus_transfers(from_user_id,transferred_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bonus_transfers_to ON bonus_transfers(to_user_id,transferred_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gamification_event_rewards_event ON gamification_event_rewards(event_id,granted_at)")

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
                          ("rfid_local_list_version","1"),
                          ("registration_enabled","0"),("registration_reference_kwh","0"),
                          ("registration_limit_mode","warn"),("registration_budget_mode","fixed")):
            conn.execute("INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",(key,value,now_setting))
        current_local_version=int((conn.execute("SELECT value FROM app_settings WHERE key='rfid_local_list_version'").fetchone() or [1])[0] or 1)

        # V0.9.7.75: LiveView is not part of Community. Remove settings left
        # behind by the short-lived 0.9.7.74 release candidate.
        conn.execute("DELETE FROM app_settings WHERE key LIKE 'liveview_%'")
        # Legacy Community users must not retain active PIN/XP access.
        conn.execute("UPDATE users SET portal_enabled=0, gamification_enabled=0 WHERE portal_enabled<>0 OR gamification_enabled<>0")

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

        # Import already-used free-text cost centers into the new master-data table.
        # Historical transaction snapshots remain untouched; this only gives existing
        # installations an immediately useful central catalog after the upgrade.
        known_cost_centers=set()
        for table in ("transactions","tariffs","billing_groups"):
            for row in conn.execute(f"SELECT DISTINCT TRIM(cost_center) FROM {table} WHERE COALESCE(TRIM(cost_center),'')<>''").fetchall():
                known_cost_centers.add(str(row[0]).strip())
        now_cc=utc_now()
        for code in sorted(known_cost_centers,key=str.casefold):
            conn.execute("INSERT OR IGNORE INTO cost_centers(code,name,active,created_at,updated_at) VALUES(?,?,1,?,?)",(code,code,now_cc,now_cc))

        # Fresh Community installations start without organization-specific
        # load-management rules. Administrators can define rules for their site.
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
        # BEGIN IMMEDIATE serializes admission across workers and processes, not just Python threads.
        conn.execute("BEGIN IMMEDIATE")
        card=conn.execute("SELECT id,user_id,vehicle_id,status,max_concurrent_sessions FROM rfid_cards WHERE uid=? LIMIT 1",(id_tag,)).fetchone()
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
        # A repeated start with the same credential on an occupied connector
        # cannot be safely identified as a new physical charging session.
        # Preserve the active transaction even if this RFID permits >1 sessions.
        existing_on_connector = conn.execute(
            """SELECT id,id_tag FROM transactions
               WHERE charge_point_id=? AND connector_id=?
                 AND status='Active' AND ended_at IS NULL
               ORDER BY id DESC LIMIT 1""",
            (cp_id, int(connector_id)),
        ).fetchone()
        if existing_on_connector:
            # Do not evict an existing physical session based only on a new
            # StartTransaction request. This applies to different RFIDs too.
            # A StopTransaction or authoritative connector recovery must close it.
            raise ValueError("CONNECTOR_ACTIVE_SESSION")
        if id_tag:
            card_count = conn.execute("SELECT COUNT(*) FROM transactions WHERE status='Active' AND ended_at IS NULL AND (rfid_card_id=? OR (rfid_card_id IS NULL AND id_tag=?))", (rfid_card_id, id_tag)).fetchone()[0] if card else conn.execute("SELECT COUNT(*) FROM transactions WHERE status='Active' AND ended_at IS NULL AND id_tag=?", (id_tag,)).fetchone()[0]
            card_limit = max(1, int(card[4] or 1)) if card else 1
            if card_count >= card_limit:
                raise ValueError("RFID_CONCURRENT_SESSION_LIMIT")
        if user_id is not None:
            user_limit_row = conn.execute("SELECT max_concurrent_sessions FROM users WHERE id=?", (user_id,)).fetchone()
            user_limit = max(1, int(user_limit_row[0] or 1)) if user_limit_row else 1
            user_count = conn.execute("SELECT COUNT(*) FROM transactions WHERE status='Active' AND ended_at IS NULL AND user_id=?", (user_id,)).fetchone()[0]
            if user_count >= user_limit:
                raise ValueError("USER_CONCURRENT_SESSION_LIMIT")
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


def charging_session_limit_snapshot(kind, item_id):
    """Return the configured limit and active usage for one card or driver."""
    if kind not in ("user", "rfid"):
        raise ValueError("INVALID_CHARGING_LIMIT_KIND")
    table, key = ("users", "user_id") if kind == "user" else ("rfid_cards", "rfid_card_id")
    with _lock, _connect() as conn:
        row = conn.execute(f"SELECT max_concurrent_sessions FROM {table} WHERE id=?", (int(item_id),)).fetchone()
        if row is None:
            return None
        active = conn.execute(
            f"SELECT COUNT(*) FROM transactions WHERE {key}=? AND status='Active' AND ended_at IS NULL",
            (int(item_id),),
        ).fetchone()[0]
        return {"limit": int(row[0]), "active": int(active), "available": max(0, int(row[0]) - int(active))}


def set_charging_session_limit(kind, item_id, limit):
    """Validate and persist a per-card or per-driver session cap.

    This is a database primitive, not an authorization endpoint. API callers
    must perform their own administrator permission check.
    """
    if kind not in ("user", "rfid"):
        raise ValueError("INVALID_CHARGING_LIMIT_KIND")
    if isinstance(limit, bool) or not str(limit).isdecimal() or not (1 <= int(limit) <= 100):
        raise ValueError("INVALID_CHARGING_LIMIT_VALUE")
    table = "users" if kind == "user" else "rfid_cards"
    with _lock, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            f"UPDATE {table} SET max_concurrent_sessions=? WHERE id=?",
            (int(limit), int(item_id)),
        )
        if cur.rowcount != 1:
            return False
        conn.commit()
        return True


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
    if result and result.get("user_id"):
        try:
            evaluate_user_achievements(int(result["user_id"]))
        except Exception:
            pass
    return result


def stop_transaction(tx, energy_kwh=None, meter_stop_kwh=None, status="Completed", stop_reason=None, ended_at=None):
    """Finish a transaction defensively.

    If the station omits/invalidates meterStop, the last valid Energy.Active.Import.Register
    reading is used as a fallback. This keeps the final session energy consistent without
    inventing data.
    """
    achievement_user_id=None
    with _lock, _connect() as conn:
        row=conn.execute("SELECT charge_point_id,connector_id,meter_start_kwh,last_meter_kwh,user_id FROM transactions WHERE id=?",(tx,)).fetchone()
        if not row:
            return None
        achievement_user_id=row[4]
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
    # No Community XP or achievement evaluation.
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
                bonus=_bonus_wallet_conn(conn,int(user_id))
                if float(bonus.get("available_kwh") or 0)<=1e-9:
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

def local_list_diagnostics():
    """Read-only, conservative assessment: an accepted send is not proof of an installed list."""
    result = []
    for row in local_list_states():
        item = dict(row)
        backend = int(item.get("backend_version") or 1)
        raw_station = item.get("station_version")
        try:
            station = int(raw_station) if raw_station is not None else None
        except (TypeError, ValueError):
            station = None
        supported = item.get("supported")
        pending = bool(item.get("pending"))
        response = str(item.get("last_response") or "").strip().lower()
        if supported == 0:
            code = "unsupported"
        elif station is None or station < 0:
            code = "unknown"
        elif station > backend:
            code = "station_ahead"
        elif pending or station < backend:
            code = "out_of_sync"
        elif str(item.get("status") or "") == "Synchronisiert":
            code = "version_match"
        else:
            code = "needs_verification"
        item["sync_diagnostic"] = code
        item["sync_verified"] = code == "version_match" and supported == 1
        item["station_version_known"] = station is not None and station >= 0
        result.append(item)
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


def create_user(name,role="Fahrer",department=None,email=None,phone=None,status="Aktiv",monthly_kwh_limit=None,monthly_limit_mode="warn",gamification_enabled=False,weekly_hours=None,budget_source="manual",charge_access_mode="all",allowed_charge_point_ids=None):
    """Create a neutral Community charging user.

    weekly_hours and budget_source remain accepted for backwards compatibility,
    but Community never derives charging credit from employment data.
    """
    limit_value = None if monthly_kwh_limit in (None, "") else max(0.0, float(monthly_kwh_limit))
    with _lock,_connect() as conn:
        mode = "block" if str(monthly_limit_mode).lower() == "block" else "warn"
        gamification = 0  # No gamification in Community.
        access_mode=_normalize_charge_access_mode(charge_access_mode)
        cur=conn.execute(
            "INSERT INTO users(name,role,department,status,email,phone,monthly_kwh_limit,monthly_limit_mode,gamification_enabled,weekly_hours,budget_source,charge_access_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (name,role,department,status,email,phone,limit_value,mode,gamification,None,"manual",access_mode),
        )
        user_id=int(cur.lastrowid)
        _set_user_charge_access_conn(conn,user_id,access_mode,allowed_charge_point_ids)
        conn.commit()
        return user_id

def update_user(user_id,**fields):
    access_mode=fields.pop("charge_access_mode",None)
    access_ids=fields.pop("allowed_charge_point_ids",None)
    fields.pop("weekly_hours",None)
    fields.pop("budget_source",None)
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
    if not updates and access_mode is None and access_ids is None:return False
    with _lock,_connect() as conn:
        if not conn.execute("SELECT id FROM users WHERE id=?",(user_id,)).fetchone():return False
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
        conn.commit(); return True

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

    tx_where=["user_id=?"]
    tx_args=[uid]
    if card_ids:
        marks=",".join("?" for _ in card_ids)
        tx_where.append(f"(user_id IS NULL AND rfid_card_id IN ({marks}))")
        tx_args.extend(card_ids)
    if id_tags:
        marks=",".join("?" for _ in id_tags)
        tx_where.append(f"(user_id IS NULL AND id_tag IN ({marks}))")
        tx_args.extend(id_tags)
    tx_rows=conn.execute(
        "SELECT id,status,ended_at FROM transactions WHERE "+(" OR ".join(tx_where)),
        tx_args,
    ).fetchall()
    transaction_ids=[int(r["id"]) for r in tx_rows]
    transactions=len(transaction_ids)
    active_transactions=sum(1 for r in tx_rows if str(r["status"] or "")=="Active" and not r["ended_at"])

    checks={
        "transactions":transactions,
        "bonus_grants":int(conn.execute("SELECT COUNT(*) FROM bonus_grants WHERE user_id=?",(uid,)).fetchone()[0] or 0),
        "bonus_usage":int(conn.execute("SELECT COUNT(*) FROM bonus_usage WHERE user_id=?",(uid,)).fetchone()[0] or 0),
        "voucher_redemptions":int(conn.execute("SELECT COUNT(*) FROM bonus_voucher_redemptions WHERE user_id=?",(uid,)).fetchone()[0] or 0),
        "bonus_transfers":int(conn.execute("SELECT COUNT(*) FROM bonus_transfers WHERE from_user_id=? OR to_user_id=?",(uid,uid)).fetchone()[0] or 0),
        "event_rewards":int(conn.execute("SELECT COUNT(*) FROM gamification_event_rewards WHERE user_id=?",(uid,)).fetchone()[0] or 0),
        "event_results":int(conn.execute("SELECT COUNT(*) FROM gamification_event_results WHERE user_id=?",(uid,)).fetchone()[0] or 0),
    }
    blockers={k:v for k,v in checks.items() if v>0}
    labels={
        "transactions":"Ladevorgänge",
        "bonus_grants":"Bonusgutschriften",
        "bonus_usage":"Bonusverbrauch",
        "voucher_redemptions":"Gutschein-Einlösungen",
        "bonus_transfers":"Bonusübertragungen",
        "event_rewards":"Event-Prämien",
        "event_results":"historische Event-Ergebnisse",
    }
    reasons=[f"{labels.get(k,k)}: {v}" for k,v in blockers.items()]

    purge_reasons=[]
    if active_transactions:
        purge_reasons.append(f"Aktive Ladevorgänge: {active_transactions}")
    if checks["bonus_transfers"]:
        purge_reasons.append(f"Bonusübertragungen mit anderen Benutzern: {checks['bonus_transfers']}")
    can_purge=(active_transactions==0 and checks["bonus_transfers"]==0)

    meter_samples=transaction_events=diagnostic_events=0
    if transaction_ids:
        marks=",".join("?" for _ in transaction_ids)
        meter_samples=int(conn.execute(f"SELECT COUNT(*) FROM meter_samples WHERE transaction_id IN ({marks})",transaction_ids).fetchone()[0] or 0)
        transaction_events=int(conn.execute(f"SELECT COUNT(*) FROM events WHERE transaction_id IN ({marks})",transaction_ids).fetchone()[0] or 0)
        diagnostic_events=int(conn.execute(f"SELECT COUNT(*) FROM diagnostic_events WHERE transaction_id IN ({marks})",transaction_ids).fetchone()[0] or 0)

    grant_ids=[int(r[0]) for r in conn.execute("SELECT id FROM bonus_grants WHERE user_id=?",(uid,)).fetchall()]
    removable={
        "transactions":transactions,
        "meter_samples":meter_samples,
        "transaction_events":transaction_events,
        "diagnostic_events":diagnostic_events,
        "rfid_cards":len(card_ids),
        "vehicle_links":int(conn.execute("SELECT COUNT(*) FROM user_vehicles WHERE user_id=?",(uid,)).fetchone()[0] or 0),
        "charge_point_links":int(conn.execute("SELECT COUNT(*) FROM user_charge_point_access WHERE user_id=?",(uid,)).fetchone()[0] or 0),
        "billing_group_links":int(conn.execute("SELECT COUNT(*) FROM user_billing_groups WHERE user_id=?",(uid,)).fetchone()[0] or 0),
        "achievements":int(conn.execute("SELECT COUNT(*) FROM achievement_awards WHERE user_id=?",(uid,)).fetchone()[0] or 0),
        "portal_sessions":int(conn.execute("SELECT COUNT(*) FROM portal_sessions WHERE user_id=?",(uid,)).fetchone()[0] or 0),
        "bonus_grants":checks["bonus_grants"],
        "bonus_usage":checks["bonus_usage"],
        "voucher_redemptions":checks["voucher_redemptions"],
        "event_rewards":checks["event_rewards"],
        "event_results":checks["event_results"],
    }
    return {
        "user_id":int(user["id"]),
        "name":user["name"],
        "status":user["status"],
        "can_delete":not bool(blockers),
        "can_purge":can_purge,
        "active_transactions":active_transactions,
        "blockers":blockers,
        "reasons":reasons,
        "purge_reasons":purge_reasons,
        "removable":removable,
        "card_ids":card_ids,
        "card_uids":id_tags,
        "transaction_ids":transaction_ids,
        "grant_ids":grant_ids,
    }


def _public_user_delete_check(check):
    if check is None:
        return None
    hidden={"card_ids","card_uids","transaction_ids","grant_ids"}
    return {k:v for k,v in check.items() if k not in hidden}


def user_delete_check(user_id):
    with _lock,_connect() as conn:
        return _public_user_delete_check(_user_delete_check_conn(conn,user_id))


def delete_user_permanently(user_id):
    with _lock,_connect() as conn:
        check=_user_delete_check_conn(conn,user_id)
        if check is None:
            return False,"not_found",None
        if not check["can_delete"]:
            return False,"history",_public_user_delete_check(check)

        uid=int(user_id)
        card_ids=list(check.get("card_ids") or [])
        card_uids=list(check.get("card_uids") or [])
        try:
            if card_ids:
                marks=",".join("?" for _ in card_ids)
                conn.execute(f"DELETE FROM rfid_card_events WHERE card_id IN ({marks})",card_ids)
                conn.execute(f"DELETE FROM rfid_replacement_requests WHERE card_id IN ({marks}) OR user_id=?",card_ids+[uid])
                conn.execute(f"UPDATE rfid_cards SET replacement_for_id=NULL WHERE replacement_for_id IN ({marks})",card_ids)
            else:
                conn.execute("DELETE FROM rfid_replacement_requests WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM rfid_cards WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM user_vehicles WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM user_charge_point_access WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM user_billing_groups WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM achievement_awards WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM portal_sessions WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM portal_pin_reset_tokens WHERE user_id=?",(uid,))
            conn.execute("UPDATE portal_pin_reset_requests SET user_id=NULL WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM rfid_enrollment_sessions WHERE user_id=?",(uid,))
            conn.execute("UPDATE access_requests SET user_id=NULL WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM users WHERE id=?",(uid,))
            if card_uids:
                _rfid_local_list_bump_conn(conn,card_uids)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return True,"deleted",_public_user_delete_check(check)


def purge_user_with_history(user_id):
    """Destructive admin-only cleanup for explicit test/error data.

    The caller must provide its own authorization and confirmation. Active charging
    sessions and cross-user bonus transfers deliberately block this operation.
    """
    with _lock,_connect() as conn:
        check=_user_delete_check_conn(conn,user_id)
        if check is None:
            return False,"not_found",None
        if not check["can_purge"]:
            return False,"unsafe",_public_user_delete_check(check)

        uid=int(user_id)
        card_ids=list(check.get("card_ids") or [])
        card_uids=list(check.get("card_uids") or [])
        transaction_ids=list(check.get("transaction_ids") or [])
        try:
            if transaction_ids:
                marks=",".join("?" for _ in transaction_ids)
                conn.execute(f"UPDATE charge_points SET transaction_id=NULL,power_kw=0 WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"UPDATE connectors SET transaction_id=NULL,power_kw=0 WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"DELETE FROM bonus_usage WHERE user_id=? OR transaction_id IN ({marks})",[uid]+transaction_ids)
                conn.execute(f"DELETE FROM meter_samples WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"DELETE FROM events WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"DELETE FROM diagnostic_events WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"UPDATE diagnostic_states SET transaction_id=NULL WHERE transaction_id IN ({marks})",transaction_ids)
                conn.execute(f"DELETE FROM transactions WHERE id IN ({marks})",transaction_ids)
            else:
                conn.execute("DELETE FROM bonus_usage WHERE user_id=?",(uid,))

            conn.execute("DELETE FROM gamification_event_rewards WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM gamification_event_results WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM bonus_voucher_redemptions WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM bonus_grants WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM achievement_awards WHERE user_id=?",(uid,))

            if card_ids:
                marks=",".join("?" for _ in card_ids)
                conn.execute(f"DELETE FROM rfid_card_events WHERE card_id IN ({marks})",card_ids)
                conn.execute(f"DELETE FROM rfid_replacement_requests WHERE card_id IN ({marks}) OR user_id=?",card_ids+[uid])
                conn.execute(f"UPDATE rfid_cards SET replacement_for_id=NULL WHERE replacement_for_id IN ({marks})",card_ids)
            else:
                conn.execute("DELETE FROM rfid_replacement_requests WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM rfid_cards WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM user_vehicles WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM user_charge_point_access WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM user_billing_groups WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM portal_sessions WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM portal_pin_reset_tokens WHERE user_id=?",(uid,))
            conn.execute("UPDATE portal_pin_reset_requests SET user_id=NULL WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM rfid_enrollment_sessions WHERE user_id=?",(uid,))
            conn.execute("UPDATE access_requests SET user_id=NULL WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM users WHERE id=?",(uid,))
            if card_uids:
                _rfid_local_list_bump_conn(conn,card_uids)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
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
            (SELECT COUNT(*) FROM rfid_replacement_requests q WHERE q.card_id=r.id AND q.status IN ('Offen','In Bearbeitung')) AS open_request_count,
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
        requests=[dict(r) for r in conn.execute("SELECT * FROM rfid_replacement_requests WHERE card_id=? ORDER BY id DESC",(card_id,)).fetchall()]
        tx=[dict(r) for r in conn.execute("""SELECT id,started_at,ended_at,charge_point_id,connector_id,energy_kwh,status FROM transactions
            WHERE rfid_card_id=? OR (rfid_card_id IS NULL AND id_tag=(SELECT uid FROM rfid_cards WHERE id=?)) ORDER BY id DESC LIMIT 20""",(card_id,card_id)).fetchall()]
        return {"card":dict(card),"history":history,"requests":requests,"transactions":tx}


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
        open_req_ids=[int(r[0]) for r in conn.execute("SELECT id FROM rfid_replacement_requests WHERE card_id=? AND status IN ('Offen','In Bearbeitung')",(card_id,)).fetchall()]
        conn.execute("""UPDATE rfid_replacement_requests SET status='Erledigt',updated_at=?,resolved_at=?,resolution_note=COALESCE(resolution_note,'Ersatzkarte ausgegeben')
            WHERE card_id=? AND status IN ('Offen','In Bearbeitung')""",(now,now,card_id))
        for rid in open_req_ids:
            conn.execute("UPDATE notifications SET active=0 WHERE notification_key=?",(f"rfid-request:{rid}",))
        conn.commit();return new_id


def create_rfid_replacement_request(user_id,card_id,reason="replacement",note=None,block_card=False):
    with _lock,_connect() as conn:
        card=conn.execute("SELECT * FROM rfid_cards WHERE id=? AND user_id=?",(card_id,user_id)).fetchone()
        if not card: raise ValueError("RFID-Karte nicht gefunden oder nicht diesem Benutzer zugeordnet.")
        if card["status"] in {"Ersetzt","Deaktiviert"}: raise ValueError("Für diese RFID-Karte kann keine Ersatzanfrage mehr gestellt werden.")
        if block_card and card["status"] != "Aktiv": raise ValueError("Nur eine aktive RFID-Karte kann als verloren gemeldet werden.")
        existing=conn.execute("SELECT id FROM rfid_replacement_requests WHERE card_id=? AND status IN ('Offen','In Bearbeitung') ORDER BY id DESC LIMIT 1",(card_id,)).fetchone()
        if existing: raise ValueError("Für diese RFID-Karte besteht bereits eine offene Anfrage.")
        now=utc_now()
        cur=conn.execute("INSERT INTO rfid_replacement_requests(card_id,user_id,reason,status,note,created_at,updated_at) VALUES(?,?,?,'Offen',?,?,?)",
                         (card_id,user_id,str(reason or "replacement"),note,now,now))
        req_id=int(cur.lastrowid)
        if block_card and card["status"] == "Aktiv":
            conn.execute("UPDATE rfid_cards SET status='Verloren',blocked_at=?,blocked_reason='Vom Ladebenutzer als verloren gemeldet' WHERE id=?",(now,card_id))
            _rfid_log_event_conn(conn,card_id,"lost",card["status"],"Verloren","Vom Ladebenutzer im Self-Service als verloren gemeldet","self-service")
            _rfid_local_list_bump_conn(conn,[card["uid"]])
        _rfid_log_event_conn(conn,card_id,"replacement_requested",card["status"],"Verloren" if block_card and card["status"]=="Aktiv" else card["status"],"Ersatzkarte angefragt","self-service")
        user=conn.execute("SELECT name FROM users WHERE id=?",(user_id,)).fetchone()
        title="RFID als verloren gemeldet" if block_card else "Neue RFID-Ersatzanfrage"
        severity="warning" if block_card else "info"
        message=f"{(user['name'] if user else 'Ladebenutzer')} · {(card['label'] or 'RFID-Karte')}"
        _upsert_notification_conn(conn,f"rfid-request:{req_id}","self_service",severity,title,message,"/users#rfid-self-service",True,"admin")
        conn.commit();return req_id


def list_rfid_replacement_requests(status=None):
    with _lock,_connect() as conn:
        params=[]; where=""
        if status: where="WHERE q.status=?"; params=[status]
        rows=conn.execute(f"""SELECT q.*,r.uid,r.label,r.status AS card_status,u.name AS user_name,v.name AS vehicle_name
            FROM rfid_replacement_requests q JOIN rfid_cards r ON r.id=q.card_id JOIN users u ON u.id=q.user_id
            LEFT JOIN vehicles v ON v.id=r.vehicle_id {where} ORDER BY CASE q.status WHEN 'Offen' THEN 0 WHEN 'In Bearbeitung' THEN 1 ELSE 2 END,q.id DESC""",params).fetchall()
        return [dict(r) for r in rows]


def update_rfid_replacement_request(request_id,status,resolution_note=None):
    allowed={"Offen","In Bearbeitung","Erledigt","Abgelehnt"}
    if status not in allowed: raise ValueError("Ungültiger Anfragestatus.")
    with _lock,_connect() as conn:
        req=conn.execute("SELECT * FROM rfid_replacement_requests WHERE id=?",(request_id,)).fetchone()
        if not req:return False
        now=utc_now(); resolved=now if status in {"Erledigt","Abgelehnt"} else None
        conn.execute("UPDATE rfid_replacement_requests SET status=?,updated_at=?,resolved_at=?,resolution_note=? WHERE id=?",(status,now,resolved,resolution_note,request_id))
        _rfid_log_event_conn(conn,req["card_id"],"request_status",None,None,f"Ersatzanfrage #{request_id}: {status}" + (f" · {resolution_note}" if resolution_note else ""),"admin")
        if status in {"Erledigt","Abgelehnt"}:
            conn.execute("UPDATE notifications SET active=0 WHERE notification_key=?",(f"rfid-request:{request_id}",))
        conn.commit();return True


def delete_rfid_replacement_request(request_id):
    """Delete only the self-service request; card state and card history stay intact."""
    with _lock,_connect() as conn:
        req=conn.execute("SELECT * FROM rfid_replacement_requests WHERE id=?",(int(request_id),)).fetchone()
        if not req:
            return None
        item=dict(req)
        conn.execute("DELETE FROM rfid_replacement_requests WHERE id=?",(int(request_id),))
        conn.execute("UPDATE notifications SET active=0 WHERE notification_key=?",(f"rfid-request:{int(request_id)}",))
        _rfid_log_event_conn(conn,int(req["card_id"]),"request_deleted",None,None,
            f"Ersatzanfrage #{int(request_id)} gelöscht · letzter Status: {req['status']}","admin")
        conn.commit()
        return item


def portal_rfid_cards(user_id):
    with _lock,_connect() as conn:
        rows=conn.execute("""SELECT r.id,r.uid,r.label,r.status,r.issued_at,r.expires_at,r.last_used_at,r.blocked_at,r.blocked_reason,
            v.name AS vehicle_name,v.plate AS vehicle_plate,
            (SELECT COUNT(*) FROM transactions t WHERE t.rfid_card_id=r.id OR (t.rfid_card_id IS NULL AND t.id_tag=r.uid)) AS session_count,
            (SELECT COALESCE(SUM(t.energy_kwh),0) FROM transactions t WHERE t.rfid_card_id=r.id OR (t.rfid_card_id IS NULL AND t.id_tag=r.uid)) AS energy_kwh,
            (SELECT id FROM rfid_replacement_requests q WHERE q.card_id=r.id AND q.status IN ('Offen','In Bearbeitung') ORDER BY q.id DESC LIMIT 1) AS open_request_id,
            (SELECT status FROM rfid_replacement_requests q WHERE q.card_id=r.id AND q.status IN ('Offen','In Bearbeitung') ORDER BY q.id DESC LIMIT 1) AS request_status
            FROM rfid_cards r LEFT JOIN vehicles v ON v.id=r.vehicle_id WHERE r.user_id=? ORDER BY CASE r.status WHEN 'Aktiv' THEN 0 ELSE 1 END,r.id DESC""",(user_id,)).fetchall()
        result=[]
        for row in rows:
            item=dict(row); uid=str(item.pop("uid") or "")
            item["uid_masked"]=("••••"+uid[-4:]) if len(uid)>4 else ("••"+uid[-2:] if uid else "—")
            result.append(item)
        return result

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
        base_used=used if limit_value is None else min(max(0.0,used),max(0.0,float(limit_value)))
        return {
            "month":month_key,"limit_kwh":None if limit_value is None else float(limit_value),
            "used_kwh":round(used,3),"base_used_kwh":round(base_used,3),"mode":mode,
            "bonus_available_kwh":0.0,"bonus_expiring_next":None,**state
        }

def user_budget_summary(now=None):
    start_utc,end_utc,month_key=_month_bounds_utc(now)
    with _lock,_connect() as conn:
        users=conn.execute("SELECT id,monthly_kwh_limit,monthly_limit_mode FROM users WHERE status='Aktiv'").fetchall()
        summary={"month":month_key,"ok":0,"warning":0,"blocked":0,"unlimited":0,"total":len(users)}
        for user in users:
            used=_user_month_energy_conn(conn,user["id"],start_utc,end_utc)
            state=_budget_status(user["monthly_kwh_limit"],used,user["monthly_limit_mode"] or "warn")
            effective_blocked=bool(state["blocked"])
            if state["status"] == "unlimited": summary["unlimited"] += 1
            elif effective_blocked: summary["blocked"] += 1
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




_PORTAL_MONTH_NAMES = ["Januar","Februar","März","April","Mai","Juni","Juli","August","September","Oktober","November","Dezember"]

def _portal_period_context(rows, requested=None, now=None):
    berlin=ZoneInfo("Europe/Berlin")
    now_utc=now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None: now_utc=now_utc.replace(tzinfo=timezone.utc)
    now_local=now_utc.astimezone(berlin)
    current_key=now_local.strftime("%Y-%m")
    keys={current_key}
    for row in rows:
        started=_parse_iso_utc(row["started_at"])
        if started: keys.add(started.astimezone(berlin).strftime("%Y-%m"))
    month_options=[]
    for key in sorted(keys, reverse=True):
        try:
            year,month=(int(x) for x in key.split("-",1))
            label=f"{_PORTAL_MONTH_NAMES[month-1]} {year}"
        except Exception:
            continue
        month_options.append({"key":key,"label":label})
    requested=str(requested or current_key).strip().lower()
    if requested=="all":
        selected_key="all"; selected_label="Gesamt"; start_utc=end_utc=None
    elif requested in {x["key"] for x in month_options}:
        selected_key=requested
        year,month=(int(x) for x in selected_key.split("-",1))
        start_local=datetime(year,month,1,tzinfo=berlin)
        if month==12: end_local=datetime(year+1,1,1,tzinfo=berlin)
        else: end_local=datetime(year,month+1,1,tzinfo=berlin)
        start_utc=start_local.astimezone(timezone.utc)
        end_utc=min(end_local.astimezone(timezone.utc),now_utc) if selected_key==current_key else end_local.astimezone(timezone.utc)
        selected_label=f"{_PORTAL_MONTH_NAMES[month-1]} {year}"
    else:
        selected_key=current_key
        selected_label=f"{_PORTAL_MONTH_NAMES[now_local.month-1]} {now_local.year}"
        start_local=now_local.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        start_utc=start_local.astimezone(timezone.utc); end_utc=now_utc
    return {
        "selected_key":selected_key,"selected_label":selected_label,"current_key":current_key,
        "start_utc":start_utc,"end_utc":end_utc,
        "options":[{"key":"all","label":"Gesamt"}]+month_options,
    }

def _portal_period_summary_conn(conn, rows, period_ctx, now=None):
    now_utc=now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None: now_utc=now_utc.replace(tzinfo=timezone.utc)
    start_utc=period_ctx.get("start_utc"); end_utc=period_ctx.get("end_utc")
    summary=_summarize_analytics_rows_conn(conn,rows,start_utc,end_utc,capacity_slots=1,now_utc=now_utc)
    selected=[]
    for row in rows:
        started=_parse_iso_utc(row["started_at"])
        if started is None: continue
        if start_utc is not None and started < start_utc: continue
        if end_utc is not None and started >= end_utc: continue
        selected.append(row)
    cost_rows=[r for r in selected if "cost_cents" in r.keys() and r["cost_cents"] is not None]
    summary["cost_known_sessions"]=len(cost_rows)
    summary["cost_cents"]=int(round(sum(float(r["cost_cents"] or 0) for r in cost_rows)))
    summary["cost_eur"]=round(summary["cost_cents"]/100.0,2)
    summary["period_key"]=period_ctx["selected_key"]
    summary["period_label"]=period_ctx["selected_label"]
    return summary,selected

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
            for legacy in ("portal_pin_hash","portal_pin_set_at","portal_enabled",
                           "portal_last_login_at","gamification_enabled",
                           "gamification_seen_award_id","gamification_seen_level"):
                item.pop(legacy,None)
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
                "percent":state["percent"],"remaining_kwh":state["remaining_kwh"],"blocked":state["blocked"],
                "bonus_available_kwh":0.0,"bonus_expiring_next":None
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
        for legacy in ("portal_pin_hash","portal_pin_set_at","portal_enabled",
                       "portal_last_login_at","gamification_enabled",
                       "gamification_seen_award_id","gamification_seen_level"):
            user.pop(legacy,None)
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


def list_load_rules():
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM load_rules ORDER BY id").fetchall()]



def list_charge_points_admin(include_retired=True):
    with _lock, _connect() as conn:
        sql = "SELECT * FROM charge_points" if include_retired else "SELECT * FROM charge_points WHERE COALESCE(retired,0)=0 AND COALESCE(archived,0)=0"
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY id").fetchall()]
        for cp in rows:
            cp["connectors"] = [dict(r) for r in conn.execute("SELECT * FROM connectors WHERE charge_point_id=? ORDER BY connector_id", (cp["id"],)).fetchall()]
        return rows

def create_charge_point(cp_id, name=None, vendor=None, model=None, serial_number=None, firmware=None, ocpp_version="1.6J", location=None, connector_count=1, connector_type="Type 2", max_power_kw=22, notes=None, rfid_self_enroll_mode="auto"):
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
    enroll_mode=str(rfid_self_enroll_mode or "auto").strip().lower()
    if enroll_mode not in {"auto","enabled","disabled"}:
        raise ValueError("RFID Self-Service muss Automatisch, Aktiviert oder Deaktiviert sein")
    with _lock, _connect() as conn:
        if conn.execute("SELECT 1 FROM charge_points WHERE id=?", (cp_id,)).fetchone():
            return False
        conn.execute(
            """INSERT INTO charge_points(
                id,vendor,model,serial_number,firmware,status,last_seen,power_kw,energy_kwh,
                connector_count,max_power_kw,simulated,location,connector_type,ocpp_version,notes,rfid_self_enroll_mode,retired,source_type
            ) VALUES(?,?,?,?,?,'Unknown',NULL,0,0,?,?,0,?,?,?,?,?,0,?)""",
            (cp_id, vendor, model, serial_number, firmware, count, maxkw, location, connector_type or "Type 2", ocpp_version or "1.6J", notes, enroll_mode, 'manual')
        )
        for cid in range(1, count + 1):
            conn.execute(
                "INSERT INTO connectors(charge_point_id,connector_id,connector_type,max_power_kw,status) VALUES(?,?,?,?,?)",
                (cp_id, cid, connector_type or "Type 2", maxkw, "Unknown")
            )
        conn.commit()
        return True

def update_charge_point(cp_id, **fields):
    allowed={"vendor","model","serial_number","firmware","ocpp_version","location","connector_type","max_power_kw","connector_count","notes","rfid_self_enroll_mode"}
    if "rfid_self_enroll_mode" in fields:
        mode=str(fields.get("rfid_self_enroll_mode") or "auto").strip().lower()
        if mode not in {"auto","enabled","disabled"}: raise ValueError("Ungültiger RFID-Self-Service-Modus")
        fields["rfid_self_enroll_mode"]=mode
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


def fleet_integration_sessions(since=None, limit=200):
    """Read-only, versioned export surface for a future fleet-management link."""
    try:
        limit=max(1,min(int(limit or 200),1000))
    except (TypeError,ValueError):
        limit=200
    clauses=["t.ended_at IS NOT NULL"]
    params=[]
    if since:
        try:
            parsed=_parse_iso_utc(str(since))
            if not parsed:
                raise ValueError()
            clauses.append("t.started_at>=?")
            params.append(parsed.isoformat())
        except Exception as exc:
            raise ValueError("Ungültiger since-Zeitpunkt; ISO-8601 erwartet") from exc
    params.append(limit)
    with _connect() as conn:
        rows=conn.execute(f"""SELECT t.id,t.started_at,t.ended_at,t.status,t.energy_kwh,t.cost_cents,
                t.charge_point_id,t.connector_id,t.vehicle_id,t.user_id,t.id_tag,
                v.name AS vehicle_name,v.plate AS vehicle_plate,
                u.name AS user_name
            FROM transactions t
            LEFT JOIN vehicles v ON v.id=t.vehicle_id
            LEFT JOIN users u ON u.id=t.user_id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.started_at DESC,t.id DESC LIMIT ?""",params).fetchall()
        return [dict(r) for r in rows]


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
                bonus=_bonus_wallet_conn(conn,user["id"])
                if state.get("blocked") and bonus["available_kwh"] > 1e-9:
                    sev,title="warning","Monatsbudget verbraucht – Bonus aktiv"
                elif state.get("blocked"):
                    sev,title="critical","Monatslimit erreicht – Laden gesperrt"
                else:
                    sev,title="critical","Monatsbudget vollständig verbraucht"
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

def _validate_cost_center_conn(conn,cost_center):
    code=str(cost_center or "").strip()
    if not code: return None
    row=conn.execute("SELECT code,active FROM cost_centers WHERE code=? COLLATE NOCASE",(code,)).fetchone()
    if row:
        if not int(row[1] or 0): raise ValueError("Kostenstelle ist deaktiviert")
        return row[0]
    # Backward compatibility for older API clients/configurations: an unknown
    # free-text code is promoted into the central master-data catalog once.
    now=utc_now()
    conn.execute("INSERT INTO cost_centers(code,name,active,created_at,updated_at) VALUES(?,?,1,?,?)",(code,code,now,now))
    return code

def list_billing_groups():
    with _lock, _connect() as conn:
        groups=[dict(r) for r in conn.execute("SELECT g.*,COUNT(ubg.user_id) AS user_count FROM billing_groups g LEFT JOIN user_billing_groups ubg ON ubg.group_id=g.id GROUP BY g.id ORDER BY g.name").fetchall()]
        for group in groups:
            group["members"]=[dict(r) for r in conn.execute("SELECT u.id,u.name,u.status,u.department FROM user_billing_groups x JOIN users u ON u.id=x.user_id WHERE x.group_id=? ORDER BY u.name",(group["id"],)).fetchall()]
        return groups

def create_billing_group(name, cost_center=None):
    name=str(name or "").strip()
    if not name: raise ValueError("Gruppenname ist erforderlich")
    with _lock, _connect() as conn:
        try:
            cost_center=_validate_cost_center_conn(conn,cost_center)
            cur=conn.execute("INSERT INTO billing_groups(name,cost_center,created_at) VALUES(?,?,?)",(name,cost_center,utc_now())); conn.commit(); return cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError("Diese Abrechnungsgruppe existiert bereits") from exc

def update_billing_group(group_id, name, cost_center=None, active=True):
    name=str(name or "").strip()
    if not name: raise ValueError("Gruppenname ist erforderlich")
    with _lock,_connect() as conn:
        row=conn.execute("SELECT id FROM billing_groups WHERE id=?",(int(group_id),)).fetchone()
        if not row: return False
        try:
            cost_center=_validate_cost_center_conn(conn,cost_center)
            conn.execute("UPDATE billing_groups SET name=?,cost_center=?,active=? WHERE id=?",
                         (name,cost_center,1 if active else 0,int(group_id)))
            conn.commit(); return True
        except sqlite3.IntegrityError as exc:
            raise ValueError("Diese Abrechnungsgruppe existiert bereits") from exc

def assign_user_billing_group(user_id, group_id):
    with _lock, _connect() as conn:
        if not conn.execute("SELECT id FROM users WHERE id=?",(user_id,)).fetchone(): raise ValueError("Ladebenutzer nicht gefunden")
        if not conn.execute("SELECT id FROM billing_groups WHERE id=? AND active=1",(group_id,)).fetchone(): raise ValueError("Abrechnungsgruppe nicht gefunden")
        conn.execute("INSERT OR REPLACE INTO user_billing_groups(user_id,group_id,assigned_at) VALUES(?,?,?)",(user_id,group_id,utc_now())); conn.commit()

def unassign_user_billing_group(user_id, group_id):
    with _lock, _connect() as conn:
        cur=conn.execute("DELETE FROM user_billing_groups WHERE user_id=? AND group_id=?",(user_id,group_id)); conn.commit(); return cur.rowcount > 0

def list_tariff_users():
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id,name,status,department,rfid FROM users ORDER BY name COLLATE NOCASE").fetchall()]

def list_tariff_charge_points():
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id,vendor,model,location,status FROM charge_points WHERE COALESCE(archived,0)=0 ORDER BY id COLLATE NOCASE").fetchall()]

def _tariff_target_label_conn(conn, scope, target_id):
    if scope=="global": return "Globaler Standard"
    if target_id in (None,""): return "—"
    if scope=="user":
        row=conn.execute("SELECT name FROM users WHERE id=?",(target_id,)).fetchone(); return row[0] if row else f"Benutzer #{target_id}"
    if scope=="user_group":
        row=conn.execute("SELECT name FROM billing_groups WHERE id=?",(target_id,)).fetchone(); return row[0] if row else f"Gruppe #{target_id}"
    if scope=="charge_point":
        row=conn.execute("SELECT id,location FROM charge_points WHERE id=?",(str(target_id),)).fetchone()
        return (f"{row['id']} · {row['location']}" if row and row['location'] else row['id']) if row else str(target_id)
    return str(target_id)

def list_tariffs():
    with _lock, _connect() as conn:
        rows=[dict(r) for r in conn.execute("SELECT t.*,g.name AS billing_group_name FROM tariffs t LEFT JOIN billing_groups g ON g.id=t.billing_group_id ORDER BY t.scope,t.valid_from DESC,t.id DESC").fetchall()]
        for row in rows: row["target_label"]=_tariff_target_label_conn(conn,row["scope"],row.get("target_id"))
        return rows

def get_tariff(tariff_id):
    with _lock, _connect() as conn:
        row=conn.execute("SELECT t.*,g.name AS billing_group_name FROM tariffs t LEFT JOIN billing_groups g ON g.id=t.billing_group_id WHERE t.id=?",(tariff_id,)).fetchone()
        if not row: return None
        item=dict(row); item["target_label"]=_tariff_target_label_conn(conn,item["scope"],item.get("target_id")); return item

def _validate_tariff_target_conn(conn, scope, target_id):
    if scope=="global": return None
    if target_id in (None,""): raise ValueError("Bitte ein konkretes Tarifziel auswählen")
    if scope=="user" and not conn.execute("SELECT id FROM users WHERE id=?",(target_id,)).fetchone(): raise ValueError("Ladebenutzer nicht gefunden")
    if scope=="user_group" and not conn.execute("SELECT id FROM billing_groups WHERE id=? AND active=1",(target_id,)).fetchone(): raise ValueError("Benutzergruppe nicht gefunden")
    if scope=="charge_point" and not conn.execute("SELECT id FROM charge_points WHERE id=?",(str(target_id),)).fetchone(): raise ValueError("Ladepunkt nicht gefunden")
    return str(target_id)

def _validate_tariff_dates(valid_from, valid_until):
    start=_iso_dt(valid_from)
    end=_iso_dt(valid_until) if valid_until else None
    if end and end<=start: raise ValueError("Gültig bis muss nach Gültig ab liegen")

def create_tariff(name, scope, target_id, price_cents_per_kwh, valid_from, valid_until=None, cost_center=None, billing_group_id=None):
    if scope not in {"global","charge_point","user_group","user"}: raise ValueError("Ungültige Tarifebene")
    name=str(name or "").strip()
    if not name: raise ValueError("Tarifname ist erforderlich")
    cents=int(price_cents_per_kwh)
    if cents < 0: raise ValueError("Preis darf nicht negativ sein")
    _validate_tariff_dates(valid_from,valid_until)
    with _lock, _connect() as conn:
        target_id=_validate_tariff_target_conn(conn,scope,target_id)
        cost_center=_validate_cost_center_conn(conn,cost_center)
        if billing_group_id is not None and not conn.execute("SELECT id FROM billing_groups WHERE id=? AND active=1",(billing_group_id,)).fetchone(): raise ValueError("Abrechnungsgruppe nicht gefunden")
        cur=conn.execute("INSERT INTO tariffs(name,scope,target_id,price_cents_per_kwh,valid_from,valid_until,cost_center,billing_group_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(name,scope,target_id,cents,valid_from,valid_until or None,cost_center,billing_group_id,utc_now())); conn.commit(); return cur.lastrowid

def create_tariff_version(tariff_id, name, price_cents_per_kwh, valid_from, valid_until=None, cost_center=None, billing_group_id=None):
    name=str(name or "").strip()
    if not name: raise ValueError("Tarifname ist erforderlich")
    cents=int(price_cents_per_kwh)
    if cents < 0: raise ValueError("Preis darf nicht negativ sein")
    _validate_tariff_dates(valid_from,valid_until)
    with _lock, _connect() as conn:
        old=conn.execute("SELECT * FROM tariffs WHERE id=?",(tariff_id,)).fetchone()
        if not old: raise ValueError("Tarif nicht gefunden")
        if _iso_dt(valid_from)<=_iso_dt(old["valid_from"]): raise ValueError("Die neue Tarifversion muss nach dem Start der bisherigen Version beginnen")
        cost_center=_validate_cost_center_conn(conn,cost_center)
        if billing_group_id is not None and not conn.execute("SELECT id FROM billing_groups WHERE id=? AND active=1",(billing_group_id,)).fetchone(): raise ValueError("Abrechnungsgruppe nicht gefunden")
        # Historical versions remain queryable. We only close the previous validity window; session snapshots are immutable.
        old_until=old["valid_until"]
        if not old_until or _iso_dt(old_until)>_iso_dt(valid_from):
            conn.execute("UPDATE tariffs SET valid_until=? WHERE id=?",(valid_from,tariff_id))
        cur=conn.execute("INSERT INTO tariffs(name,scope,target_id,price_cents_per_kwh,valid_from,valid_until,cost_center,billing_group_id,active,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",(name,old["scope"],old["target_id"],cents,valid_from,valid_until or None,cost_center,billing_group_id,utc_now()))
        conn.commit(); return cur.lastrowid

def delete_tariff(tariff_id):
    with _lock, _connect() as conn:
        cur=conn.execute("UPDATE tariffs SET active=0 WHERE id=?",(tariff_id,)); conn.commit(); return cur.rowcount > 0

def _select_tariff_conn(conn, user_id, cp_id, at):
    rows=conn.execute("SELECT t.*,g.name AS billing_group_name,g.cost_center AS billing_group_cost_center FROM tariffs t LEFT JOIN billing_groups g ON g.id=t.billing_group_id WHERE t.active=1 AND t.valid_from<=? AND (t.valid_until IS NULL OR t.valid_until>?) ORDER BY t.valid_from DESC,t.id DESC",(at,at)).fetchall()
    groups={int(r[0]) for r in conn.execute("SELECT x.group_id FROM user_billing_groups x JOIN billing_groups g ON g.id=x.group_id WHERE x.user_id=? AND g.active=1",(user_id,)).fetchall()} if user_id else set()
    for scope,target in (("user",str(user_id) if user_id else None),("user_group",groups),("charge_point",str(cp_id)),("global",None)):
        for row in rows:
            if row["scope"]!=scope: continue
            if scope=="user_group" and int(row["target_id"] or 0) not in target: continue
            if scope not in {"user_group","global"} and str(row["target_id"] or "")!=str(target): continue
            result=dict(row)
            # A user-group tariff already targets a billing group. If no separate
            # accounting group was selected in the tariff, snapshot the target
            # group itself so reporting does not lose that assignment.
            if scope=="user_group" and not result.get("billing_group_id"):
                group=conn.execute("SELECT id,name,cost_center FROM billing_groups WHERE id=?",(int(row["target_id"]),)).fetchone()
                if group:
                    result["billing_group_id"]=group["id"]
                    result["billing_group_name"]=group["name"]
                    if not result.get("cost_center"): result["billing_group_cost_center"]=group["cost_center"]
            result["source"]={"user":"Benutzer","user_group":"Gruppe","charge_point":"Ladepunkt","global":"Global"}[scope]; return result
    return None

def resolve_tariff(user_id=None, id_tag=None, cp_id=None, at=None):
    at=at or utc_now()
    with _lock, _connect() as conn:
        if user_id is None and id_tag:
            row=conn.execute("SELECT user_id FROM rfid_cards WHERE uid=?",(id_tag,)).fetchone(); user_id=row[0] if row else None
        return _select_tariff_conn(conn,user_id,cp_id,at)

def _apply_tariff_to_tx_conn(conn, tx_id, user_id, cp_id, started_at):
    tariff=_select_tariff_conn(conn,user_id,cp_id,started_at)
    if not tariff: return
    conn.execute("UPDATE transactions SET tariff_id=?,tariff_name=?,tariff_source=?,price_cents_per_kwh=?,cost_center=?,billing_group_id=?,billing_group_name=? WHERE id=?",(tariff["id"],tariff["name"],tariff["source"],tariff["price_cents_per_kwh"],tariff.get("cost_center") or tariff.get("billing_group_cost_center"),tariff.get("billing_group_id"),tariff.get("billing_group_name"),tx_id))

def _update_tx_cost_conn(conn, tx_id):
    """Freeze the chargeable session cost after monthly allowance and bonus kWh."""
    row=conn.execute("SELECT id,energy_kwh,price_cents_per_kwh,user_id,id_tag,started_at FROM transactions WHERE id=?",(tx_id,)).fetchone()
    if not row or row["energy_kwh"] is None:
        return
    energy=max(0.0,float(row["energy_kwh"] or 0))
    user_id=row["user_id"]
    if user_id is None and row["id_tag"]:
        card=conn.execute("SELECT user_id FROM rfid_cards WHERE uid=?",(row["id_tag"],)).fetchone()
        if card and card[0] is not None:
            user_id=int(card[0])
        else:
            legacy=conn.execute("SELECT id FROM users WHERE rfid=?",(row["id_tag"],)).fetchone()
            user_id=int(legacy[0]) if legacy else None

    chargeable_energy=energy
    if user_id is not None:
        user=conn.execute("SELECT monthly_kwh_limit FROM users WHERE id=?",(user_id,)).fetchone()
        if user:
            if user["monthly_kwh_limit"] is None:
                chargeable_energy=0.0
            else:
                started=_parse_iso_utc(row["started_at"]) or datetime.now(timezone.utc)
                start_utc,end_utc,_=_month_bounds_utc(started)
                prior=conn.execute("""SELECT COALESCE(SUM(energy_kwh),0) FROM transactions
                    WHERE id<>? AND (user_id=? OR (user_id IS NULL AND (id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?) OR id_tag=(SELECT rfid FROM users WHERE id=?))))
                      AND started_at>=? AND started_at<? AND (ended_at IS NOT NULL OR status<>'Active')""",
                    (tx_id,user_id,user_id,user_id,start_utc,end_utc)).fetchone()[0]
                base_remaining=max(0.0,max(0.0,float(user["monthly_kwh_limit"]))-float(prior or 0))
                bonus=conn.execute("""SELECT COALESCE(SUM(remaining_kwh),0) FROM bonus_grants
                    WHERE user_id=? AND active=1 AND remaining_kwh>0.0000001 AND granted_at<=? AND expires_at>?""",
                    (user_id,started.isoformat(),started.isoformat())).fetchone()[0]
                chargeable_energy=max(0.0,energy-base_remaining-float(bonus or 0))

    if chargeable_energy<=1e-9:
        conn.execute("UPDATE transactions SET cost_cents=0 WHERE id=?",(tx_id,))
        return
    if row["price_cents_per_kwh"] is None:
        conn.execute("UPDATE transactions SET cost_cents=NULL WHERE id=?",(tx_id,))
        return
    cents=(Decimal(str(chargeable_energy))*Decimal(int(row["price_cents_per_kwh"]))).quantize(Decimal("1"),rounding=ROUND_HALF_UP)
    conn.execute("UPDATE transactions SET cost_cents=? WHERE id=?",(int(cents),tx_id))

def billing_groups_for_user(user_id):
    with _lock, _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT g.* FROM billing_groups g JOIN user_billing_groups x ON x.group_id=g.id WHERE x.user_id=?",(user_id,)).fetchall()]

def diagnostic_summary(cp_id):
    caps=meter_capabilities_for_charge_point(cp_id)
    with _lock, _connect() as conn:
        events=conn.execute("SELECT event_type,COUNT(*) AS n FROM events WHERE charge_point_id=? GROUP BY event_type ORDER BY n DESC",(cp_id,)).fetchall()
        warnings=conn.execute("SELECT * FROM events WHERE charge_point_id=? AND (LOWER(event_type) LIKE '%fault%' OR LOWER(event_type) LIKE '%error%' OR LOWER(event_type) LIKE '%warning%') ORDER BY id DESC LIMIT 20",(cp_id,)).fetchall()
    return {"event_counts":[dict(x) for x in events],"warnings":[dict(x) for x in warnings],"telemetry_seen":sum(1 for x in caps if x.get("seen")),"telemetry_total":len(caps),"hints":[],"health":charge_point_health(cp_id),"history":diagnostic_history(cp_id,limit=60)}


# V0.9.0 - private charging-credit portal, privacy-safe rankings and achievements

def set_user_portal_pin(user_id, encoded_hash, enabled=True):
    with _lock, _connect() as conn:
        if not conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
            return False
        conn.execute(
            "UPDATE users SET portal_pin_hash=?, portal_pin_set_at=?, portal_enabled=? WHERE id=?",
            (encoded_hash, utc_now(), 1 if enabled else 0, user_id),
        )
        conn.execute("DELETE FROM portal_sessions WHERE user_id=?", (user_id,))
        conn.commit()
        return True


def set_user_portal_enabled(user_id, enabled):
    with _lock, _connect() as conn:
        cur=conn.execute("UPDATE users SET portal_enabled=? WHERE id=?", (1 if enabled else 0, user_id))
        if not enabled:
            conn.execute("DELETE FROM portal_sessions WHERE user_id=?", (user_id,))
        conn.commit(); return cur.rowcount > 0


def portal_pin_records(include_disabled=False):
    with _lock, _connect() as conn:
        extra="" if include_disabled else "AND portal_enabled=1"
        return [dict(r) for r in conn.execute(
            f"SELECT id,name,portal_pin_hash,portal_enabled FROM users WHERE status='Aktiv' {extra} AND portal_pin_hash IS NOT NULL ORDER BY id"
        ).fetchall()]


def portal_user_for_reset_email(email):
    key=str(email or "").strip().casefold()
    if not key: return None
    with _connect() as conn:
        rows=conn.execute("""SELECT id,name,email,portal_enabled,portal_pin_set_at FROM users
            WHERE status='Aktiv' AND portal_enabled=1 AND portal_pin_hash IS NOT NULL AND LOWER(TRIM(COALESCE(email,'')))=?""",(key,)).fetchall()
        return dict(rows[0]) if len(rows)==1 else None


def portal_pin_reset_allowed(email_hash, ip_hash, window_minutes=60, ip_limit=5):
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=max(1,int(window_minutes)))).isoformat()
    with _connect() as conn:
        by_email=int(conn.execute("SELECT COUNT(*) FROM portal_pin_reset_requests WHERE email_hash=? AND requested_at>=?",(str(email_hash),cutoff)).fetchone()[0] or 0)
        by_ip=int(conn.execute("SELECT COUNT(*) FROM portal_pin_reset_requests WHERE ip_hash=? AND requested_at>=?",(str(ip_hash),cutoff)).fetchone()[0] or 0)
        return by_email < 1 and by_ip < max(1,int(ip_limit))


def record_portal_pin_reset_request(email_hash, ip_hash, user_id=None, mail_sent=False):
    now=datetime.now(timezone.utc)
    with _lock,_connect() as conn:
        conn.execute("DELETE FROM portal_pin_reset_requests WHERE requested_at<?",((now-timedelta(days=30)).isoformat(),))
        conn.execute("DELETE FROM portal_pin_reset_tokens WHERE expires_at<?",((now-timedelta(days=7)).isoformat(),))
        cur=conn.execute("INSERT INTO portal_pin_reset_requests(email_hash,ip_hash,user_id,requested_at,mail_sent) VALUES(?,?,?,?,?)",(str(email_hash),str(ip_hash),user_id,now.isoformat(),1 if mail_sent else 0))
        conn.commit(); return int(cur.lastrowid)


def mark_portal_pin_reset_mail_sent(request_id):
    with _lock,_connect() as conn:
        conn.execute("UPDATE portal_pin_reset_requests SET mail_sent=1 WHERE id=?",(int(request_id),)); conn.commit()


def create_portal_pin_reset_token(user_id, token_hash, expires_at):
    now=utc_now()
    with _lock,_connect() as conn:
        conn.execute("UPDATE portal_pin_reset_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL",(now,int(user_id)))
        conn.execute("DELETE FROM portal_pin_reset_tokens WHERE expires_at<?",(now,))
        conn.execute("INSERT INTO portal_pin_reset_tokens(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",(str(token_hash),int(user_id),now,str(expires_at)))
        conn.commit(); return True


def portal_pin_reset_token(token_hash):
    now=utc_now()
    with _connect() as conn:
        row=conn.execute("""SELECT t.token_hash,t.user_id,t.created_at,t.expires_at,u.name,u.email
            FROM portal_pin_reset_tokens t JOIN users u ON u.id=t.user_id
            WHERE t.token_hash=? AND t.used_at IS NULL AND t.expires_at>? AND u.status='Aktiv' AND u.portal_enabled=1""",(str(token_hash),now)).fetchone()
        return dict(row) if row else None


def consume_portal_pin_reset_token(token_hash, encoded_hash):
    now=utc_now()
    with _lock,_connect() as conn:
        row=conn.execute("SELECT user_id FROM portal_pin_reset_tokens WHERE token_hash=? AND used_at IS NULL AND expires_at>?",(str(token_hash),now)).fetchone()
        if not row: return None
        user_id=int(row[0])
        conn.execute("UPDATE users SET portal_pin_hash=?,portal_pin_set_at=?,portal_enabled=1 WHERE id=?",(str(encoded_hash),now,user_id))
        conn.execute("DELETE FROM portal_sessions WHERE user_id=?",(user_id,))
        conn.execute("UPDATE portal_pin_reset_tokens SET used_at=? WHERE token_hash=?",(now,str(token_hash)))
        conn.commit(); return user_id


def create_portal_session(token_hash, user_id, expires_at):
    now=utc_now()
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM portal_sessions WHERE expires_at<=?", (now,))
        conn.execute("INSERT OR REPLACE INTO portal_sessions(token_hash,user_id,created_at,expires_at,last_seen_at) VALUES(?,?,?,?,?)", (token_hash,user_id,now,expires_at,now))
        conn.execute("UPDATE users SET portal_last_login_at=? WHERE id=?", (now,user_id))
        conn.commit()


def portal_user_for_session(token_hash):
    if not token_hash: return None
    now=utc_now()
    with _lock, _connect() as conn:
        row=conn.execute("""SELECT u.id,u.name,u.role,u.department,u.portal_enabled,u.portal_pin_set_at,u.portal_last_login_at,s.expires_at
            FROM portal_sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>? AND u.portal_enabled=1 AND u.status='Aktiv'""", (token_hash,now)).fetchone()
        if not row: return None
        conn.execute("UPDATE portal_sessions SET last_seen_at=? WHERE token_hash=?", (now,token_hash)); conn.commit()
        return dict(row)


def delete_portal_session(token_hash):
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM portal_sessions WHERE token_hash=?", (token_hash,)); conn.commit()


def portal_login_failures(ip_hash, minutes=10):
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=minutes)).isoformat()
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM portal_login_attempts WHERE ts<?", ((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),))
        row=conn.execute("SELECT COUNT(*) FROM portal_login_attempts WHERE ip_hash=? AND success=0 AND ts>=?", (ip_hash,cutoff)).fetchone()
        conn.commit(); return int(row[0] or 0)


def record_portal_login_attempt(ip_hash, success):
    with _lock, _connect() as conn:
        if success:
            conn.execute("DELETE FROM portal_login_attempts WHERE ip_hash=?", (ip_hash,))
        else:
            conn.execute("INSERT INTO portal_login_attempts(ip_hash,ts,success) VALUES(?,?,0)", (ip_hash,utc_now()))
        conn.commit()


ACHIEVEMENT_METRICS={
    "manual",
    "energy_total",
    "sessions_total",
    "energy_month",
    "energy_year",
    "sessions_month",
    "sessions_year",
    "stand_minutes_total",
    "charging_hours_total",
    "max_session_energy",
    "max_session_power",
    "distinct_charge_points",
    "distinct_vehicles",
    "days_active",
    "months_active",
    "weekend_sessions",
    "early_sessions",
    "evening_sessions",
    "night_sessions",
    "midnight_sessions",
    "long_sessions",
    "quick_sessions",
    "prompt_unplug_sessions",
    "exact_42_sessions",
    "max_month_energy",
    "max_month_sessions",
    "week_streak",
}
ACHIEVEMENT_RARITIES={"common","uncommon","rare","epic","legendary","secret"}

LEADERBOARD_METRIC_META={
    "energy_kwh":{"label":"Geladene Energie","icon":"🔋","unit":"kWh","decimals":1,"order":10,"description":"Summe der im gewählten Zeitraum geladenen Energie. Mehr kWh bedeuten einen höheren Rang."},
    "sessions":{"label":"Ladevorgänge","icon":"🔌","unit":"Sessions","decimals":0,"order":20,"description":"Anzahl der Ladevorgänge im gewählten Zeitraum. Wer häufiger lädt, steht weiter oben."},
    "xp":{"label":"XP-Rangliste","icon":"⚡","unit":"XP","decimals":0,"order":30,"description":"Vergleicht die gesammelten Erfahrungspunkte aus freigeschalteten Achievements und Erfolgen."},
    "achievement_count":{"label":"Achievement-Sammler","icon":"🏆","unit":"Achievements","decimals":0,"order":40,"description":"Vergleicht die Anzahl freigeschalteter Achievements. Die meisten Auszeichnungen führen die Rangliste an."},
    "early_sessions":{"label":"Frühlader / Early Birds","icon":"🌅","unit":"Sessions","decimals":0,"order":100,"description":"Zählt Ladevorgänge am frühen Morgen zwischen 05:00 und 08:00 Uhr."},
    "evening_sessions":{"label":"Feierabend-Lader","icon":"🌇","unit":"Sessions","decimals":0,"order":110,"description":"Zählt Ladevorgänge am Feierabend zwischen 17:00 und 22:00 Uhr."},
    "night_sessions":{"label":"Night Owls","icon":"🌙","unit":"Sessions","decimals":0,"order":120,"description":"Zählt Nachtladungen ab 22:00 Uhr bis vor 05:00 Uhr."},
    "weekend_sessions":{"label":"Wochenend-Lader","icon":"🏖️","unit":"Sessions","decimals":0,"order":130,"description":"Zählt Ladevorgänge an Samstagen und Sonntagen."},
    "week_streak":{"label":"Streak Champions","icon":"🔥","unit":"Wochen","decimals":0,"order":140,"description":"Misst die längste Serie aufeinanderfolgender Kalenderwochen mit mindestens einem Ladevorgang."},
    "max_session_energy":{"label":"Größte Einzelladung","icon":"🥤","unit":"kWh","decimals":1,"order":150,"description":"Wertet die größte Energiemenge einer einzelnen Ladesession."},
    "max_session_power":{"label":"Power-Peak","icon":"⚡","unit":"kW","decimals":1,"order":160,"description":"Wertet die höchste in einer Session gemessene Ladeleistung."},
    "distinct_charge_points":{"label":"Ladepunkt-Entdecker","icon":"🧭","unit":"Ladepunkte","decimals":0,"order":170,"description":"Zählt, an wie vielen unterschiedlichen Ladepunkten geladen wurde."},
    "distinct_vehicles":{"label":"Flottenhopper","icon":"🚗","unit":"Fahrzeuge","decimals":0,"order":180,"description":"Zählt, wie viele unterschiedliche zugeordnete Fahrzeuge in Sessions verwendet wurden."},
    "days_active":{"label":"Aktive Ladetage","icon":"📆","unit":"Tage","decimals":0,"order":190,"description":"Zählt unterschiedliche Kalendertage mit mindestens einem Ladevorgang."},
    "months_active":{"label":"Aktive Lademonate","icon":"🗓️","unit":"Monate","decimals":0,"order":200,"description":"Zählt unterschiedliche Kalendermonate mit mindestens einem Ladevorgang."},
    "charging_hours_total":{"label":"Ladezeit-Könige","icon":"⏱️","unit":"Std.","decimals":1,"order":210,"description":"Summiert die tatsächliche Ladezeit aller Sessions im Zeitraum."},
    "max_month_energy":{"label":"Stärkster Lademonat","icon":"📊","unit":"kWh","decimals":1,"order":220,"description":"Vergleicht den stärksten einzelnen Lademonat nach geladener Energie."},
    "max_month_sessions":{"label":"Session-Sammler","icon":"🧮","unit":"Sessions","decimals":0,"order":230,"description":"Vergleicht den Monat mit den meisten einzelnen Ladevorgängen."},
    "long_sessions":{"label":"Langzeitparker","icon":"🅿️","unit":"Sessions","decimals":0,"order":240,"description":"Zählt lange angeschlossene Sessions ab vier Stunden."},
    "quick_sessions":{"label":"Power Naps","icon":"😴","unit":"Sessions","decimals":0,"order":250,"description":"Zählt kurze Sessions mit Energiefluss und höchstens 45 Minuten Dauer."},
    "prompt_unplug_sessions":{"label":"Stecker-Sprinter","icon":"🏃","unit":"Sessions","decimals":0,"order":260,"description":"Zählt Sessions, bei denen der Connector nach Sessionende innerhalb von 10 Minuten wieder frei gemeldet wurde."},
}
LEADERBOARD_METRIC_ALIASES={
    "energy_total":"energy_kwh","energy_month":"energy_kwh","energy_year":"energy_kwh",
    "sessions_total":"sessions","sessions_month":"sessions","sessions_year":"sessions",
}
LEADERBOARD_ALWAYS_ENABLED={"energy_kwh","sessions","xp","achievement_count"}


def _leaderboard_metric_key(metric):
    key=LEADERBOARD_METRIC_ALIASES.get(str(metric or ""),str(metric or ""))
    return key if key in LEADERBOARD_METRIC_META else None



def list_achievements(include_inactive=True):
    """Administrative catalogue. Secret achievements stay hidden from players, not admins."""
    with _lock, _connect() as conn:
        where="" if include_inactive else "WHERE a.active=1"
        return [dict(r) for r in conn.execute(f"""SELECT a.*,
            (SELECT COUNT(*) FROM achievement_awards x JOIN users ux ON ux.id=x.user_id WHERE x.achievement_id=a.id AND COALESCE(ux.gamification_enabled,1)=1) AS awarded_count,
            EXISTS(SELECT 1 FROM gamification_events ge WHERE ge.winner_achievement_id=a.id) AS event_badge
            FROM achievements a {where}
            ORDER BY a.active DESC,COALESCE(a.category,'Allgemein') COLLATE NOCASE,
                     COALESCE(a.tier_group,''),COALESCE(a.tier_rank,0),a.name COLLATE NOCASE""").fetchall()]


def _achievement_fields(name, description=None, icon=None, metric="manual", threshold=None, hidden=False,
                        system_secret=False, active=True, category="Allgemein", rarity="common", xp=50,
                        tier_group=None, tier_name=None, tier_rank=0, leaderboard_enabled=False):
    metric=str(metric or "manual")
    if metric not in ACHIEVEMENT_METRICS:
        raise ValueError("Ungültige Achievement-Metrik")
    rarity=str(rarity or "common").strip().lower()
    if rarity not in ACHIEVEMENT_RARITIES:
        raise ValueError("Ungültige Seltenheit")
    clean_name=str(name or "").strip()
    if not clean_name:
        raise ValueError("Name ist erforderlich")
    try:
        xp=max(0,min(100000,int(xp or 0)))
        tier_rank=max(0,min(99,int(tier_rank or 0)))
    except (TypeError,ValueError):
        raise ValueError("XP und Stufenrang müssen ganze Zahlen sein")
    return {
        "name":clean_name,
        "description":str(description).strip() if description not in (None,"") else None,
        "icon":str(icon or "🏅").strip() or "🏅",
        "metric":metric,
        "threshold":None if threshold in (None,"") else float(threshold),
        "hidden":1 if hidden else 0,
        "system_secret":1 if system_secret else 0,
        "active":1 if active else 0,
        "category":str(category or "Allgemein").strip() or "Allgemein",
        "rarity":rarity,
        "xp":xp,
        "tier_group":str(tier_group).strip() if tier_group not in (None,"") else None,
        "tier_name":str(tier_name).strip() if tier_name not in (None,"") else None,
        "tier_rank":tier_rank,
        "leaderboard_enabled":1 if (leaderboard_enabled and not system_secret and _leaderboard_metric_key(metric)) else 0,
    }


def create_achievement(name, description=None, icon=None, metric="manual", threshold=None, hidden=False,
                       system_secret=False, active=True, category="Allgemein", rarity="common", xp=50,
                       tier_group=None, tier_name=None, tier_rank=0, leaderboard_enabled=False):
    fields=_achievement_fields(name,description,icon,metric,threshold,hidden,system_secret,active,category,rarity,xp,tier_group,tier_name,tier_rank,leaderboard_enabled)
    keys=list(fields)
    with _lock,_connect() as conn:
        cur=conn.execute(
            "INSERT INTO achievements("+",".join(keys)+",created_at) VALUES("+",".join("?" for _ in keys)+",?)",
            [fields[k] for k in keys]+[utc_now()],
        )
        conn.commit()
        return int(cur.lastrowid)


def update_achievement(achievement_id, **fields):
    current=get_achievement(achievement_id)
    if not current:
        return False
    merged={k:current.get(k) for k in (
        "name","description","icon","metric","threshold","hidden","system_secret","active",
        "category","rarity","xp","tier_group","tier_name","tier_rank","leaderboard_enabled"
    )}
    merged.update({k:v for k,v in fields.items() if k in merged})
    cleaned=_achievement_fields(**merged)
    with _lock,_connect() as conn:
        keys=list(cleaned)
        cur=conn.execute(
            "UPDATE achievements SET "+", ".join(f"{k}=?" for k in keys)+" WHERE id=?",
            [cleaned[k] for k in keys]+[int(achievement_id)],
        )
        conn.commit()
        changed=cur.rowcount>0
    if changed and any(k in fields for k in {"metric","threshold","active"}):
        reconcile_automatic_achievements()
    return changed


def get_achievement(achievement_id):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT * FROM achievements WHERE id=?",(int(achievement_id),)).fetchone()
        return dict(row) if row else None


def _achievement_tx_rows_conn(conn, user_id):
    return conn.execute("""SELECT t.* FROM transactions t
        WHERE t.user_id=? OR (
          t.user_id IS NULL AND (
            t.id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?)
            OR t.id_tag=(SELECT rfid FROM users WHERE id=?)
          )
        )
        ORDER BY t.started_at""",(user_id,user_id,user_id)).fetchall()


def _tx_local_start(row):
    dt=_parse_iso_utc(row["started_at"] if "started_at" in row.keys() else None)
    return dt.astimezone(ZoneInfo("Europe/Berlin")) if dt else None


def _max_consecutive_iso_weeks(rows):
    weeks=sorted({(dt.isocalendar().year,dt.isocalendar().week) for r in rows if (dt:=_tx_local_start(r))})
    if not weeks:
        return 0
    monday_dates=[]
    for year,week in weeks:
        try:
            monday_dates.append(datetime.fromisocalendar(year,week,1).date())
        except ValueError:
            pass
    monday_dates=sorted(set(monday_dates))
    best=cur=1 if monday_dates else 0
    for prev,nxt in zip(monday_dates,monday_dates[1:]):
        if (nxt-prev).days==7:
            cur+=1
            best=max(best,cur)
        else:
            cur=1
    return best


def _achievement_metric_value_conn(conn, user_id, metric):
    rows=_achievement_tx_rows_conn(conn,user_id)
    now_local=datetime.now(ZoneInfo("Europe/Berlin"))
    if metric=="energy_month":
        start,end,_=_month_bounds_utc()
        return _user_month_energy_conn(conn,user_id,start,end)
    if metric=="energy_year":
        return sum(float(r["energy_kwh"] or 0) for r in rows if (dt:=_tx_local_start(r)) and dt.year==now_local.year)
    if metric=="sessions_month":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and dt.year==now_local.year and dt.month==now_local.month))
    if metric=="sessions_year":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and dt.year==now_local.year))
    if metric=="energy_total":
        return sum(float(r["energy_kwh"] or 0) for r in rows)
    if metric=="sessions_total":
        return float(len(rows))
    if metric=="stand_minutes_total":
        total=0.0
        for r in rows:
            _,s,_=_analytics_row_timing_conn(conn,r)
            total+=s/60.0
        return total
    if metric=="charging_hours_total":
        total=0.0
        for r in rows:
            charging,_,_=_analytics_row_timing_conn(conn,r)
            total+=charging/3600.0
        return total
    if metric=="max_session_energy":
        return max([float(r["energy_kwh"] or 0) for r in rows] or [0.0])
    if metric=="max_session_power":
        return max([float(r["max_power_kw"] or 0) for r in rows] or [0.0])
    if metric=="distinct_charge_points":
        return float(len({str(r["charge_point_id"]) for r in rows if r["charge_point_id"] not in (None,"")}))
    if metric=="distinct_vehicles":
        return float(len({int(r["vehicle_id"]) for r in rows if r["vehicle_id"] not in (None,"")}))
    if metric=="days_active":
        return float(len({dt.date().isoformat() for r in rows if (dt:=_tx_local_start(r))}))
    if metric=="months_active":
        return float(len({(dt.year,dt.month) for r in rows if (dt:=_tx_local_start(r))}))
    if metric=="weekend_sessions":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and dt.weekday()>=5))
    if metric=="early_sessions":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and 5<=dt.hour<8))
    if metric=="evening_sessions":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and 17<=dt.hour<22))
    if metric=="night_sessions":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and (dt.hour>=22 or dt.hour<5)))
    if metric=="midnight_sessions":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and dt.hour==0))
    if metric in {"long_sessions","quick_sessions"}:
        count=0
        for r in rows:
            charging,stand,connection=_analytics_row_timing_conn(conn,r)
            seconds=max(float(connection or 0),float(charging or 0)+float(stand or 0))
            energy=float(r["energy_kwh"] or 0)
            if metric=="long_sessions" and seconds>=4*3600:
                count+=1
            if metric=="quick_sessions" and energy>0 and 0<seconds<=45*60:
                count+=1
        return float(count)
    if metric=="prompt_unplug_sessions":
        count=0
        for r in rows:
            if "post_session_occupied_seconds" not in r.keys() or r["post_session_occupied_seconds"] is None:
                continue
            try: seconds=float(r["post_session_occupied_seconds"])
            except (TypeError,ValueError): continue
            if float(r["energy_kwh"] or 0)>0 and 0<=seconds<=PROMPT_UNPLUG_SECONDS:
                count+=1
        return float(count)
    if metric=="exact_42_sessions":
        return float(sum(1 for r in rows if 41.95<=float(r["energy_kwh"] or 0)<=42.05))
    if metric in {"max_month_energy","max_month_sessions"}:
        months={}
        for r in rows:
            dt=_tx_local_start(r)
            if not dt:
                continue
            key=(dt.year,dt.month)
            bucket=months.setdefault(key,{"energy":0.0,"sessions":0})
            bucket["energy"]+=float(r["energy_kwh"] or 0)
            bucket["sessions"]+=1
        if not months:
            return 0.0
        key="energy" if metric=="max_month_energy" else "sessions"
        return float(max(x[key] for x in months.values()))
    if metric=="week_streak":
        return float(_max_consecutive_iso_weeks(rows))
    return 0.0


def evaluate_user_achievements(user_id):
    """Reconcile automatic achievements against their current definition."""
    with _lock,_connect() as conn:
        user=conn.execute("SELECT gamification_enabled FROM users WHERE id=?",(user_id,)).fetchone()
        if not user or not bool(user["gamification_enabled"]):
            return []
        conn.execute("""DELETE FROM achievement_awards
            WHERE user_id=? AND source='automatic' AND achievement_id IN (
                SELECT id FROM achievements WHERE active=1 AND (metric='manual' OR threshold IS NULL)
            )""",(user_id,))
        defs=conn.execute("SELECT * FROM achievements WHERE active=1 AND metric<>'manual' AND threshold IS NOT NULL ORDER BY id").fetchall()
        awarded=[]
        metric_cache={}
        for a in defs:
            metric=str(a["metric"])
            if metric not in metric_cache:
                metric_cache[metric]=_achievement_metric_value_conn(conn,user_id,metric)
            value=metric_cache[metric]
            qualifies=value+1e-9>=float(a["threshold"])
            if qualifies:
                cur=conn.execute("INSERT OR IGNORE INTO achievement_awards(user_id,achievement_id,awarded_at,source) VALUES(?,?,?,'automatic')",(user_id,a["id"],utc_now()))
                if cur.rowcount:
                    awarded.append(int(a["id"]))
            else:
                conn.execute("DELETE FROM achievement_awards WHERE user_id=? AND achievement_id=? AND source='automatic'",(user_id,a["id"]))
        conn.commit()
        return awarded


def reconcile_automatic_achievements():
    with _lock,_connect() as conn:
        user_ids=[int(r[0]) for r in conn.execute("SELECT id FROM users WHERE COALESCE(gamification_enabled,1)=1").fetchall()]
    for user_id in user_ids:
        evaluate_user_achievements(user_id)
    return len(user_ids)


def award_achievement(user_id, achievement_id, source="manual"):
    with _lock,_connect() as conn:
        user=conn.execute("SELECT id,gamification_enabled FROM users WHERE id=?",(user_id,)).fetchone()
        if not user:
            raise ValueError("Benutzer nicht gefunden")
        if not bool(user["gamification_enabled"]):
            raise ValueError("Benutzer nimmt nicht an Achievements, Events oder Ranglisten teil")
        if not conn.execute("SELECT id FROM achievements WHERE id=? AND active=1",(achievement_id,)).fetchone():
            raise ValueError("Achievement nicht gefunden")
        conn.execute("INSERT OR IGNORE INTO achievement_awards(user_id,achievement_id,awarded_at,source) VALUES(?,?,?,?)",(user_id,achievement_id,utc_now(),source))
        conn.commit()


def revoke_achievement(user_id, achievement_id):
    with _lock,_connect() as conn:
        cur=conn.execute("DELETE FROM achievement_awards WHERE user_id=? AND achievement_id=?",(user_id,achievement_id))
        conn.commit()
        return cur.rowcount>0


def earned_achievement_count(user_id):
    with _lock,_connect() as conn:
        row=conn.execute("""SELECT COUNT(*) FROM achievement_awards x
            JOIN achievements a ON a.id=x.achievement_id
            WHERE x.user_id=? AND a.active=1""",(int(user_id),)).fetchone()
        return int(row[0] or 0) if row else 0


def earned_achievements_for_user(user_id, limit=8):
    try:
        limit=max(1,min(20,int(limit or 8)))
    except (TypeError,ValueError):
        limit=8
    with _lock,_connect() as conn:
        rows=conn.execute("""SELECT a.*,x.awarded_at,x.source FROM achievement_awards x
            JOIN achievements a ON a.id=x.achievement_id
            WHERE x.user_id=? AND a.active=1
            ORDER BY x.awarded_at DESC,a.name COLLATE NOCASE LIMIT ?""",(int(user_id),limit)).fetchall()
        result=[]
        for r in rows:
            d=dict(r)
            d["earned"]=True
            d["event_badge"]=str(d.get("source") or "").startswith("event:")
            d["display_name"]=d.get("name")
            d["display_description"]=d.get("description") or ""
            d["display_icon"]=d.get("icon") or "🏅"
            result.append(d)
        return result


def _xp_floor(level):
    level=max(1,int(level))
    return 125*(level-1)*level//2


def _level_title(level):
    bands=[
        (40,"Grid Grandmaster"),(30,"Ladelegende"),(25,"Lord of the kWh"),
        (20,"Voltage Veteran"),(16,"High Voltage"),(12,"Watt-Wizard"),
        (8,"Watt-Sammler"),(5,"Ampere-Akrobat"),(3,"Lade-Fan"),(1,"Stecker-Neuling"),
    ]
    return next(title for minimum,title in bands if level>=minimum)


def _gamification_level_data(total_xp):
    total_xp=max(0,int(total_xp or 0))
    level=1
    while level<99 and total_xp>=_xp_floor(level+1):
        level+=1
    current_floor=_xp_floor(level)
    next_floor=_xp_floor(level+1)
    span=max(1,next_floor-current_floor)
    progress=max(0.0,min(100.0,(total_xp-current_floor)/span*100.0))
    total_progress=max(0.0,min(100.0,total_xp/max(1,next_floor)*100.0))
    return {
        "xp":total_xp,
        "level":level,
        "title":_level_title(level),
        "level_xp":total_xp-current_floor,
        "next_level_xp":span,
        "next_level_total_xp":next_floor,
        "xp_to_next":max(0,next_floor-total_xp),
        "progress_pct":round(progress,1),
        "total_progress_pct":round(total_progress,1),
    }


def user_gamification_profile(user_id):
    evaluate_user_achievements(user_id)
    with _lock,_connect() as conn:
        rows=conn.execute("""SELECT COALESCE(a.xp,0) AS xp,COALESCE(a.rarity,'common') AS rarity,
                                   COALESCE(a.category,'Allgemein') AS category
            FROM achievement_awards x JOIN achievements a ON a.id=x.achievement_id
            WHERE x.user_id=? AND a.active=1""",(int(user_id),)).fetchall()
    total_xp=sum(max(0,int(r["xp"] or 0)) for r in rows)
    rarity_counts={}
    category_counts={}
    for r in rows:
        rarity=str(r["rarity"] or "common")
        category=str(r["category"] or "Allgemein")
        rarity_counts[rarity]=rarity_counts.get(rarity,0)+1
        category_counts[category]=category_counts.get(category,0)+1
    return {
        **_gamification_level_data(total_xp),
        "achievement_count":len(rows),
        "rarity_counts":rarity_counts,
        "category_counts":category_counts,
    }


def portal_gamification_reveals(user_id, limit=6):
    """Return newly earned achievements / level-ups for the real user portal.

    Existing installations are baselined on first access so an upgrade does not
    replay the complete historic achievement catalogue. Afterwards only awards
    earned since the last acknowledgement are revealed. Secret achievements are
    only exposed here after they have actually been awarded.
    """
    uid=int(user_id)
    profile=user_gamification_profile(uid)
    current_level=int(profile.get("level") or 1)
    with _lock,_connect() as conn:
        user=conn.execute("""SELECT gamification_enabled,gamification_seen_award_id,gamification_seen_level
            FROM users WHERE id=?""",(uid,)).fetchone()
        if not user or not bool(user["gamification_enabled"]):
            return {"pending":False,"achievements":[],"level_up":None,"ack_award_id":0,"ack_level":current_level}
        max_row=conn.execute("""SELECT COALESCE(MAX(x.id),0) FROM achievement_awards x
            JOIN achievements a ON a.id=x.achievement_id
            WHERE x.user_id=? AND a.active=1""",(uid,)).fetchone()
        max_award_id=int(max_row[0] or 0)
        seen_award=user["gamification_seen_award_id"]
        seen_level=user["gamification_seen_level"]

        # V0.9.7.50 migration baseline: do not flood existing users with every
        # achievement they earned before the reveal feature existed.
        if seen_award is None or seen_level is None:
            conn.execute("""UPDATE users SET gamification_seen_award_id=?,gamification_seen_level=?
                WHERE id=?""",(max_award_id,current_level,uid))
            conn.commit()
            return {"pending":False,"achievements":[],"level_up":None,
                    "ack_award_id":max_award_id,"ack_level":current_level}

        try: limit=max(1,min(12,int(limit or 6)))
        except (TypeError,ValueError): limit=6
        rows=conn.execute("""SELECT x.id AS award_id,x.awarded_at,x.source,
                    a.id AS achievement_id,a.name,a.description,a.icon,a.category,a.rarity,a.xp,
                    a.tier_group,a.tier_name,a.tier_rank,a.hidden,a.system_secret
            FROM achievement_awards x JOIN achievements a ON a.id=x.achievement_id
            WHERE x.user_id=? AND a.active=1 AND x.id>?
            ORDER BY x.id ASC LIMIT ?""",(uid,int(seen_award or 0),limit)).fetchall()
        achievements=[]
        rarity_labels={"common":"Gewöhnlich","uncommon":"Ungewöhnlich","rare":"Selten",
                       "epic":"Episch","legendary":"Legendär","secret":"Geheim"}
        for row in rows:
            d=dict(row)
            d["display_name"]=d.get("name") or "Achievement"
            d["display_description"]=d.get("description") or ""
            d["display_icon"]=d.get("icon") or "🏅"
            d["rarity_label"]=rarity_labels.get(str(d.get("rarity") or "common"),str(d.get("rarity") or "common"))
            achievements.append(d)

        level_up=None
        if current_level>int(seen_level or 1):
            level_up={
                "from_level":int(seen_level or 1),
                "level":current_level,
                "title":profile.get("title") or _level_title(current_level),
                "xp":int(profile.get("xp") or 0),
                "xp_to_next":int(profile.get("xp_to_next") or 0),
            }
        return {
            "pending":bool(achievements or level_up),
            "achievements":achievements,
            "level_up":level_up,
            "ack_award_id":max_award_id,
            "ack_level":current_level,
        }


def acknowledge_portal_gamification_reveals(user_id, award_id=None, level=None):
    uid=int(user_id)
    profile=user_gamification_profile(uid)
    current_level=int(profile.get("level") or 1)
    with _lock,_connect() as conn:
        row=conn.execute("""SELECT gamification_seen_award_id,gamification_seen_level
            FROM users WHERE id=?""",(uid,)).fetchone()
        if not row:
            return False
        max_row=conn.execute("""SELECT COALESCE(MAX(x.id),0) FROM achievement_awards x
            JOIN achievements a ON a.id=x.achievement_id
            WHERE x.user_id=? AND a.active=1""",(uid,)).fetchone()
        max_award_id=int(max_row[0] or 0)
        old_award=int(row["gamification_seen_award_id"] or 0)
        old_level=int(row["gamification_seen_level"] or 1)
        try: requested_award=int(award_id if award_id is not None else max_award_id)
        except (TypeError,ValueError): requested_award=max_award_id
        try: requested_level=int(level if level is not None else current_level)
        except (TypeError,ValueError): requested_level=current_level
        new_award=max(old_award,min(max_award_id,max(0,requested_award)))
        new_level=max(old_level,min(current_level,max(1,requested_level)))
        conn.execute("""UPDATE users SET gamification_seen_award_id=?,gamification_seen_level=?
            WHERE id=?""",(new_award,new_level,uid))
        conn.commit()
        return True


def gamification_overview():
    reconcile_automatic_achievements()
    with _lock,_connect() as conn:
        rows=conn.execute("""SELECT u.id,u.name,
            COALESCE(SUM(CASE WHEN a.active=1 THEN COALESCE(a.xp,0) ELSE 0 END),0) AS xp,
            COUNT(CASE WHEN a.active=1 THEN a.id END) AS achievement_count
            FROM users u
            LEFT JOIN achievement_awards x ON x.user_id=u.id
            LEFT JOIN achievements a ON a.id=x.achievement_id
            WHERE u.status='Aktiv' AND COALESCE(u.gamification_enabled,1)=1
            GROUP BY u.id,u.name""").fetchall()
    items=[]
    for r in rows:
        progress=_gamification_level_data(int(r["xp"] or 0))
        items.append({
            "user_id":int(r["id"]),"name":r["name"],
            "achievement_count":int(r["achievement_count"] or 0),
            **progress,
        })
    items.sort(key=lambda x:(-x["xp"],-x["achievement_count"],x["name"].casefold()))
    rank=0; last=None
    for i,item in enumerate(items,1):
        if last is None or item["xp"]!=last:
            rank=i; last=item["xp"]
        item["rank"]=rank
    return items



def achievements_for_user(user_id, include_locked=True):
    with _lock,_connect() as conn:
        user=conn.execute("SELECT gamification_enabled FROM users WHERE id=?",(user_id,)).fetchone()
        if not user or not bool(user["gamification_enabled"]):
            return []
    evaluate_user_achievements(user_id)
    with _lock,_connect() as conn:
        rows=conn.execute("""SELECT a.*,x.awarded_at,x.source FROM achievements a
            LEFT JOIN achievement_awards x ON x.achievement_id=a.id AND x.user_id=?
            WHERE a.active=1 AND (COALESCE(a.system_secret,0)=0 OR x.awarded_at IS NOT NULL) ORDER BY x.awarded_at IS NULL,a.hidden,a.name COLLATE NOCASE""",(user_id,)).fetchall()
        result=[]
        for r in rows:
            d=dict(r); d["earned"]=bool(d.get("awarded_at"))
            d["event_badge"]=bool(d.get("earned") and str(d.get("source") or "").startswith("event:"))
            if d["event_badge"]:
                try: d["event_id"]=int(str(d.get("source")).split(":",1)[1])
                except (TypeError,ValueError,IndexError): d["event_id"]=None
            if d["hidden"] and not d["earned"]:
                d["display_name"]="???"; d["display_description"]="Verstecktes Achievement"; d["display_icon"]="❓"
            else:
                d["display_name"]=d["name"]; d["display_description"]=d.get("description") or ""; d["display_icon"]=d.get("icon") or "🏅"
            if include_locked or d["earned"]: result.append(d)
        return result


def _event_state(event, now=None):
    now_dt=_parse_iso_utc(now) if isinstance(now,str) else (now or datetime.now(timezone.utc))
    if not isinstance(now_dt,datetime): now_dt=datetime.now(timezone.utc)
    start=_parse_iso_utc(event["starts_at"])
    end=_parse_iso_utc(event["ends_at"])
    if start and now_dt < start: return "planned"
    if end and now_dt >= end: return "ended"
    return "running"


def list_gamification_events(include_inactive=True):
    # Finalize ended events before reading so the archive always reflects the
    # immutable result snapshot and any configured prizes.
    finalize_ended_gamification_events()
    with _lock,_connect() as conn:
        where="" if include_inactive else "WHERE e.active=1"
        rows=conn.execute(f"""SELECT e.*,
            (SELECT COUNT(*) FROM gamification_event_rewards r WHERE r.event_id=e.id) AS reward_count,
            (SELECT COALESCE(SUM(r.amount_kwh),0) FROM gamification_event_rewards r WHERE r.event_id=e.id) AS reward_total_kwh,
            (SELECT COUNT(*) FROM achievement_awards aa WHERE aa.source=('event:' || e.id)) AS badge_award_count,
            (SELECT COUNT(*) FROM gamification_event_results rr WHERE rr.event_id=e.id AND rr.qualified=1) AS qualified_count,
            (SELECT COUNT(*) FROM gamification_event_results rr WHERE rr.event_id=e.id AND (rr.sessions>0 OR rr.energy_kwh>0)) AS participant_count
            FROM gamification_events e {where} ORDER BY e.starts_at DESC,e.id DESC""").fetchall()
        now=datetime.now(timezone.utc)
        result=[]
        for row in rows:
            d=dict(row)
            d["event_state"]=_event_state(d,now)
            d["finalized"]=bool(d.get("finalized_at"))
            result.append(d)
        return result


EVENT_METRICS={
    "energy_kwh",
    "sessions",
    "avg_stand_seconds",
    "total_stand_seconds",
    "unplug_ratio",
    "avg_energy_kwh",
    "qualified_sessions",
}
EVENT_QUALIFICATION_METRICS={"avg_stand_seconds","total_stand_seconds","unplug_ratio","avg_energy_kwh","qualified_sessions"}
EVENT_TIMING_METRICS={"avg_stand_seconds","total_stand_seconds","unplug_ratio"}
EVENT_ASCENDING_METRICS={"avg_stand_seconds","total_stand_seconds"}


def _validate_event_fields(metric, starts_at, ends_at, min_sessions=0, min_session_kwh=0, reward_bonus_kwh=0, reward_valid_days=None,
                           reward_bonus_enabled=None, winner_badge_enabled=False, winner_badge_name=None, winner_badge_icon=None, winner_badge_description=None):
    if metric not in EVENT_METRICS:
        raise ValueError("Ungültige Ranking-Metrik")
    if _parse_iso_utc(ends_at) <= _parse_iso_utc(starts_at):
        raise ValueError("Ende muss nach Beginn liegen")
    minimum_sessions=max(0,int(min_sessions or 0))
    minimum_kwh=max(0.0,float(min_session_kwh or 0))
    reward=max(0.0,float(reward_bonus_kwh or 0))
    reward_enabled=(reward>0) if reward_bonus_enabled is None else bool(reward_bonus_enabled)
    valid_days=None if reward_valid_days in (None,"") else max(1,min(730,int(reward_valid_days)))
    badge_enabled=bool(winner_badge_enabled)
    badge_name=str(winner_badge_name or "").strip() or None
    badge_icon=str(winner_badge_icon or "").strip() or "🏆"
    badge_description=str(winner_badge_description or "").strip() or None
    if metric in EVENT_QUALIFICATION_METRICS:
        if minimum_sessions < 1: raise ValueError("Für diese Wertung ist mindestens eine qualifizierende Session erforderlich")
        if minimum_kwh <= 0: raise ValueError("Für diese Wertung muss eine Mindestenergie je Session festgelegt werden")
    if reward_enabled and reward <= 0:
        raise ValueError("Für die automatische Bonusauszahlung muss eine Bonusmenge größer 0 festgelegt werden")
    return minimum_sessions,minimum_kwh,reward,valid_days,reward_enabled,badge_enabled,badge_name,badge_icon,badge_description


def create_gamification_event(name, description, metric, starts_at, ends_at, active=True, min_sessions=0, min_session_kwh=0, reward_bonus_kwh=0, reward_valid_days=None,
                               reward_bonus_enabled=None, winner_badge_enabled=False, winner_badge_name=None, winner_badge_icon=None, winner_badge_description=None):
    values=_validate_event_fields(metric,starts_at,ends_at,min_sessions,min_session_kwh,reward_bonus_kwh,reward_valid_days,reward_bonus_enabled,winner_badge_enabled,winner_badge_name,winner_badge_icon,winner_badge_description)
    min_sessions,min_session_kwh,reward_bonus_kwh,reward_valid_days,reward_bonus_enabled,winner_badge_enabled,winner_badge_name,winner_badge_icon,winner_badge_description=values
    if not str(name or "").strip(): raise ValueError("Eventname fehlt")
    if winner_badge_enabled and not winner_badge_name:
        winner_badge_name=f"Sieger: {str(name).strip()}"
    if winner_badge_enabled and not winner_badge_description:
        winner_badge_description=f"Gewinner des Events „{str(name).strip()}“."
    with _lock,_connect() as conn:
        cur=conn.execute("""INSERT INTO gamification_events(name,description,metric,starts_at,ends_at,active,created_at,min_sessions,min_session_kwh,reward_bonus_kwh,reward_valid_days,reward_bonus_enabled,winner_badge_enabled,winner_badge_name,winner_badge_icon,winner_badge_description)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(str(name).strip(),description or None,metric,starts_at,ends_at,1 if active else 0,utc_now(),min_sessions,min_session_kwh,reward_bonus_kwh,reward_valid_days,1 if reward_bonus_enabled else 0,1 if winner_badge_enabled else 0,winner_badge_name,winner_badge_icon,winner_badge_description))
        conn.commit(); return int(cur.lastrowid)


def update_gamification_event(event_id, name, description, metric, starts_at, ends_at, active=True, min_sessions=0, min_session_kwh=0, reward_bonus_kwh=0, reward_valid_days=None,
                               reward_bonus_enabled=None, winner_badge_enabled=False, winner_badge_name=None, winner_badge_icon=None, winner_badge_description=None):
    values=_validate_event_fields(metric,starts_at,ends_at,min_sessions,min_session_kwh,reward_bonus_kwh,reward_valid_days,reward_bonus_enabled,winner_badge_enabled,winner_badge_name,winner_badge_icon,winner_badge_description)
    min_sessions,min_session_kwh,reward_bonus_kwh,reward_valid_days,reward_bonus_enabled,winner_badge_enabled,winner_badge_name,winner_badge_icon,winner_badge_description=values
    if not str(name or "").strip(): raise ValueError("Eventname fehlt")
    if winner_badge_enabled and not winner_badge_name:
        winner_badge_name=f"Sieger: {str(name).strip()}"
    if winner_badge_enabled and not winner_badge_description:
        winner_badge_description=f"Gewinner des Events „{str(name).strip()}“."
    with _lock,_connect() as conn:
        event=conn.execute("SELECT finalized_at,winner_achievement_id FROM gamification_events WHERE id=?",(event_id,)).fetchone()
        if not event: return False
        if event["finalized_at"]:
            raise ValueError("Ein bereits abgeschlossenes Event ist archiviert und kann nicht mehr verändert werden")
        paid=conn.execute("SELECT 1 FROM gamification_event_rewards WHERE event_id=? LIMIT 1",(event_id,)).fetchone()
        badge_awarded=conn.execute("SELECT 1 FROM achievement_awards WHERE source=? LIMIT 1",(f"event:{event_id}",)).fetchone()
        if paid or badge_awarded:
            raise ValueError("Ein bereits automatisch prämiertes oder ausgezeichnetes Event kann nicht mehr verändert werden")
        cur=conn.execute("""UPDATE gamification_events SET name=?,description=?,metric=?,starts_at=?,ends_at=?,active=?,min_sessions=?,min_session_kwh=?,reward_bonus_kwh=?,reward_valid_days=?,reward_bonus_enabled=?,winner_badge_enabled=?,winner_badge_name=?,winner_badge_icon=?,winner_badge_description=? WHERE id=?""",
            (str(name).strip(),description or None,metric,starts_at,ends_at,1 if active else 0,min_sessions,min_session_kwh,reward_bonus_kwh,reward_valid_days,1 if reward_bonus_enabled else 0,1 if winner_badge_enabled else 0,winner_badge_name,winner_badge_icon,winner_badge_description,event_id))
        conn.commit(); return cur.rowcount>0


def set_gamification_event_active(event_id, active):
    with _lock,_connect() as conn:
        event=conn.execute("SELECT finalized_at FROM gamification_events WHERE id=?",(event_id,)).fetchone()
        if not event: return False
        if event["finalized_at"] and bool(active):
            raise ValueError("Ein archiviertes Event kann nicht wieder aktiviert werden")
        cur=conn.execute("UPDATE gamification_events SET active=? WHERE id=?",(1 if active else 0,event_id)); conn.commit(); return cur.rowcount>0


def _event_user_join_sql():
    return "(t.user_id=u.id OR (t.user_id IS NULL AND (t.id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=u.id) OR t.id_tag=u.rfid)))"


def _rank_event_items(items, ascending=False):
    if ascending:
        items.sort(key=lambda x:(not x.get("qualified",False), x.get("value") if x.get("qualified") else float("inf"), -int(x.get("sessions") or 0), x.get("name","").casefold()))
    else:
        items.sort(key=lambda x:(not x.get("qualified",False), -(x.get("value") if x.get("qualified") and x.get("value") is not None else -1), -int(x.get("sessions") or 0), x.get("name","").casefold()))
    rank=0; last=None; position=0
    for item in items:
        if not item.get("qualified") or item.get("value") is None:
            item["rank"]=None
            continue
        position+=1
        value=float(item["value"])
        if last is None or abs(value-last)>1e-9:
            rank=position; last=value
        item["rank"]=rank
    return items


def _event_leaderboard_conn(conn, event):
    metric=event["metric"]
    if metric in EVENT_QUALIFICATION_METRICS:
        min_sessions=max(1,int(event["min_sessions"] or 0))
        min_kwh=max(0.0,float(event["min_session_kwh"] or 0))
        timing_sql=""
        if metric=="unplug_ratio":
            timing_sql=""" AND t.ended_at IS NOT NULL
              AND t.post_session_occupied_seconds IS NOT NULL"""
        elif metric in EVENT_TIMING_METRICS:
            timing_sql=""" AND t.ended_at IS NOT NULL
              AND COALESCE(t.timing_quality,'')<>'connection_only'
              AND EXISTS(SELECT 1 FROM meter_samples ms WHERE ms.transaction_id=t.id AND ms.power_kw IS NOT NULL)"""
        rows=conn.execute(f"""SELECT u.id,u.name,
            COUNT(t.id) AS sessions, COALESCE(SUM(t.energy_kwh),0) AS energy_kwh,
            COALESCE(AVG(t.energy_kwh),0) AS avg_energy_kwh,
            COALESCE(AVG(t.stand_seconds),0) AS avg_stand_seconds,
            COALESCE(SUM(t.stand_seconds),0) AS total_stand_seconds,
            COALESCE(SUM(CASE WHEN t.id IS NOT NULL AND t.post_session_occupied_seconds IS NOT NULL AND t.post_session_occupied_seconds<=? THEN 1 ELSE 0 END),0) AS quick_unplug_sessions
            FROM users u LEFT JOIN transactions t ON {_event_user_join_sql()}
              AND t.started_at>=? AND t.started_at<? AND COALESCE(t.energy_kwh,0)>=? {timing_sql}
            WHERE u.status='Aktiv' AND COALESCE(u.gamification_enabled,1)=1 GROUP BY u.id,u.name""",
            (PROMPT_UNPLUG_SECONDS,event["starts_at"],event["ends_at"],min_kwh)).fetchall()
        items=[]
        for r in rows:
            sessions=int(r["sessions"] or 0); qualified=sessions>=min_sessions
            energy=round(float(r["energy_kwh"] or 0),3)
            avg_energy=float(r["avg_energy_kwh"] or 0)
            avg_stand=float(r["avg_stand_seconds"] or 0)
            total_stand=float(r["total_stand_seconds"] or 0)
            quick=int(r["quick_unplug_sessions"] or 0)
            unplug_ratio=(quick/sessions*100.0) if sessions else 0.0
            values={
                "avg_stand_seconds":avg_stand,
                "total_stand_seconds":total_stand,
                "unplug_ratio":unplug_ratio,
                "avg_energy_kwh":avg_energy,
                "qualified_sessions":float(sessions),
            }
            value=values[metric] if qualified else None
            items.append({"user_id":int(r["id"]),"name":r["name"],"sessions":sessions,"energy_kwh":energy,
                "avg_energy_kwh":round(avg_energy,3),"avg_stand_seconds":round(avg_stand,3),"total_stand_seconds":round(total_stand,3),
                "stand_seconds":round(total_stand,3),"quick_unplug_sessions":quick,"unplug_ratio":round(unplug_ratio,3),
                "value":value,"qualified":qualified,"qualification_sessions":sessions,"min_sessions":min_sessions,"min_session_kwh":min_kwh,
                "timing_required":metric in EVENT_TIMING_METRICS})
        return _rank_event_items(items,metric in EVENT_ASCENDING_METRICS)
    rows=conn.execute(f"""SELECT u.id,u.name,
        COUNT(t.id) AS sessions, COALESCE(SUM(t.energy_kwh),0) AS energy_kwh
        FROM users u LEFT JOIN transactions t ON {_event_user_join_sql()}
          AND t.started_at>=? AND t.started_at<?
        WHERE u.status='Aktiv' AND COALESCE(u.gamification_enabled,1)=1 GROUP BY u.id,u.name""",(event["starts_at"],event["ends_at"])).fetchall()
    items=[]
    for r in rows:
        value=float(r["energy_kwh"] or 0) if metric=="energy_kwh" else float(r["sessions"] or 0)
        items.append({"user_id":int(r["id"]),"name":r["name"],"sessions":int(r["sessions"] or 0),"energy_kwh":round(float(r["energy_kwh"] or 0),3),"value":value,"qualified":value>0})
    return _rank_event_items(items,False)


def preview_gamification_event(name, description, metric, starts_at, ends_at, active=True, min_sessions=0, min_session_kwh=0, reward_bonus_kwh=0, reward_valid_days=None,
                               reward_bonus_enabled=None, winner_badge_enabled=False, winner_badge_name=None, winner_badge_icon=None, winner_badge_description=None):
    values=_validate_event_fields(metric,starts_at,ends_at,min_sessions,min_session_kwh,reward_bonus_kwh,reward_valid_days,reward_bonus_enabled,winner_badge_enabled,winner_badge_name,winner_badge_icon,winner_badge_description)
    min_sessions,min_session_kwh,reward_bonus_kwh,reward_valid_days,reward_bonus_enabled,winner_badge_enabled,winner_badge_name,winner_badge_icon,winner_badge_description=values
    event={"id":None,"name":str(name or "Vorschau").strip() or "Vorschau","description":description,"metric":metric,"starts_at":starts_at,"ends_at":ends_at,
        "active":1 if active else 0,"min_sessions":min_sessions,"min_session_kwh":min_session_kwh,"reward_bonus_kwh":reward_bonus_kwh,
        "reward_valid_days":reward_valid_days,"reward_bonus_enabled":1 if reward_bonus_enabled else 0,"winner_badge_enabled":1 if winner_badge_enabled else 0,
        "winner_badge_name":winner_badge_name,"winner_badge_icon":winner_badge_icon,"winner_badge_description":winner_badge_description}
    with _lock,_connect() as conn:
        board=_event_leaderboard_conn(conn,event)
    participants=sum(1 for x in board if int(x.get("sessions") or 0)>0 or float(x.get("energy_kwh") or 0)>0)
    qualified=sum(1 for x in board if x.get("qualified") and x.get("value") is not None)
    return {**event,"leaderboard":board,"participant_count":participants,"qualified_count":qualified,"preview":True}


def _event_rewards_conn(conn,event_id):
    return [dict(r) for r in conn.execute("""SELECT r.*,u.name AS user_name FROM gamification_event_rewards r
        JOIN users u ON u.id=r.user_id WHERE r.event_id=? ORDER BY r.id""",(event_id,)).fetchall()]


def _event_badge_awards_conn(conn,event):
    achievement_id=event["winner_achievement_id"] if "winner_achievement_id" in event.keys() else None
    if not achievement_id:
        return []
    return [dict(r) for r in conn.execute("""SELECT aa.id,aa.user_id,aa.achievement_id,aa.awarded_at,aa.source,u.name AS user_name,a.name AS achievement_name,a.icon AS achievement_icon
        FROM achievement_awards aa JOIN users u ON u.id=aa.user_id JOIN achievements a ON a.id=aa.achievement_id
        WHERE aa.achievement_id=? AND aa.source=? ORDER BY aa.id""",(achievement_id,f"event:{event['id']}")).fetchall()]


def _ensure_event_badge_conn(conn,event):
    achievement_id=event["winner_achievement_id"] if "winner_achievement_id" in event.keys() else None
    if achievement_id:
        exists=conn.execute("SELECT id FROM achievements WHERE id=?",(achievement_id,)).fetchone()
        if exists:
            return int(achievement_id)
    name=str(event["winner_badge_name"] or "").strip() or f"Sieger: {event['name']}"
    icon=str(event["winner_badge_icon"] or "").strip() or "🏆"
    description=str(event["winner_badge_description"] or "").strip() or f"Gewinner des Events „{event['name']}“."
    cur=conn.execute("""INSERT INTO achievements(name,description,icon,metric,threshold,hidden,system_secret,active,created_at)
        VALUES(?,?,?,'manual',NULL,0,0,1,?)""",(name,description,icon,utc_now()))
    achievement_id=int(cur.lastrowid)
    conn.execute("UPDATE gamification_events SET winner_achievement_id=? WHERE id=?",(achievement_id,event["id"]))
    return achievement_id


def _snapshot_event_results_conn(conn,event,board,captured_at):
    if conn.execute("SELECT 1 FROM gamification_event_results WHERE event_id=? LIMIT 1",(event["id"],)).fetchone():
        return
    for item in board:
        details={k:v for k,v in item.items() if k not in {"user_id","name","rank","value","qualified","sessions","energy_kwh"}}
        conn.execute("""INSERT OR IGNORE INTO gamification_event_results(event_id,user_id,rank,metric_value,qualified,sessions,energy_kwh,details_json,captured_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",(event["id"],item["user_id"],item.get("rank"),item.get("value"),1 if item.get("qualified") else 0,int(item.get("sessions") or 0),float(item.get("energy_kwh") or 0),json.dumps(details,ensure_ascii=False,separators=(",",":")),captured_at))


def _event_snapshot_board_conn(conn,event):
    rows=conn.execute("""SELECT rr.*,u.name FROM gamification_event_results rr JOIN users u ON u.id=rr.user_id
        WHERE rr.event_id=? ORDER BY CASE WHEN rr.rank IS NULL THEN 1 ELSE 0 END,rr.rank,u.name COLLATE NOCASE""",(event["id"],)).fetchall()
    board=[]
    for row in rows:
        d={"user_id":int(row["user_id"]),"name":row["name"],"rank":row["rank"],"value":row["metric_value"],"qualified":bool(row["qualified"]),
           "sessions":int(row["sessions"] or 0),"energy_kwh":round(float(row["energy_kwh"] or 0),3)}
        try: d.update(json.loads(row["details_json"] or "{}"))
        except (TypeError,ValueError): pass
        board.append(d)
    return board


def finalize_ended_gamification_events(now=None):
    now_dt=_parse_iso_utc(now) if isinstance(now,str) else (now or datetime.now(timezone.utc))
    if not isinstance(now_dt,datetime): now_dt=datetime.now(timezone.utc)
    now_iso=now_dt.isoformat()
    finalized=[]
    with _lock,_connect() as conn:
        events=conn.execute("""SELECT * FROM gamification_events e WHERE e.active=1 AND e.ends_at<=? AND e.finalized_at IS NULL
            ORDER BY e.ends_at,e.id""",(now_iso,)).fetchall()
        for event in events:
            board=_event_leaderboard_conn(conn,event)
            _snapshot_event_results_conn(conn,event,board,now_iso)
            winners=[x for x in board if x.get("rank")==1 and x.get("qualified",True) and x.get("value") is not None and float(x.get("value") or 0)>=0]
            if event["metric"] in {"energy_kwh","sessions"}:
                winners=[x for x in winners if float(x.get("value") or 0)>0]
            achievement_id=None
            if winners and bool(event["winner_badge_enabled"]):
                achievement_id=_ensure_event_badge_conn(conn,event)
            reward_enabled=bool(event["reward_bonus_enabled"]) and float(event["reward_bonus_kwh"] or 0)>0
            days=int(event["reward_valid_days"] or _bonus_policy_settings_conn(conn)["default_valid_days"]) if reward_enabled else None
            expiry=now_dt+timedelta(days=max(1,min(730,days))) if reward_enabled else None
            for winner in winners:
                result={"event_id":int(event["id"]),"user_id":winner["user_id"]}
                if achievement_id:
                    cur=conn.execute("INSERT OR IGNORE INTO achievement_awards(user_id,achievement_id,awarded_at,source) VALUES(?,?,?,?)",
                        (winner["user_id"],achievement_id,now_iso,f"event:{event['id']}"))
                    if cur.rowcount:
                        result["achievement_id"]=achievement_id
                if reward_enabled and not conn.execute("SELECT 1 FROM gamification_event_rewards WHERE event_id=? AND user_id=?",(event["id"],winner["user_id"])).fetchone():
                    amount=float(event["reward_bonus_kwh"] or 0)
                    cur=conn.execute("""INSERT INTO bonus_grants(user_id,amount_kwh,remaining_kwh,granted_at,expires_at,source,note,active)
                        VALUES(?,?,?,?,?,?,?,1)""",(winner["user_id"],amount,amount,now_iso,expiry.isoformat(),f"event:{event['id']}",f"Eventgewinn: {event['name']}"))
                    grant_id=int(cur.lastrowid)
                    conn.execute("INSERT INTO gamification_event_rewards(event_id,user_id,bonus_grant_id,amount_kwh,granted_at) VALUES(?,?,?,?,?)",
                        (event["id"],winner["user_id"],grant_id,amount,now_iso))
                    result.update({"grant_id":grant_id,"amount_kwh":amount})
                if len(result)>2:
                    finalized.append(result)
            conn.execute("UPDATE gamification_events SET finalized_at=? WHERE id=?",(now_iso,event["id"]))
        conn.commit()
    return finalized


def _event_detail_summary(event,board):
    participants=sum(1 for x in board if int(x.get("sessions") or 0)>0 or float(x.get("energy_kwh") or 0)>0)
    qualified=sum(1 for x in board if x.get("qualified") and x.get("value") is not None)
    top3=[x for x in board if x.get("rank") is not None and int(x.get("rank") or 0)<=3]
    return {"participant_count":participants,"qualified_count":qualified,"top3":top3}


def gamification_event_detail(event_id):
    finalize_ended_gamification_events()
    with _lock,_connect() as conn:
        event=conn.execute("SELECT * FROM gamification_events WHERE id=?",(event_id,)).fetchone()
        if not event: return None
        d=dict(event)
        d["event_state"]=_event_state(d)
        d["finalized"]=bool(d.get("finalized_at"))
        board=_event_snapshot_board_conn(conn,event) if d["finalized"] else _event_leaderboard_conn(conn,event)
        d["leaderboard"]=board
        d.update(_event_detail_summary(event,board))
        d["rewards"]=_event_rewards_conn(conn,event_id)
        d["badge_awards"]=_event_badge_awards_conn(conn,event)
        return d

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


def _bonus_policy_settings_conn(conn):
    def intval(key,default,minimum,maximum):
        row=conn.execute("SELECT value FROM app_settings WHERE key=?",(key,)).fetchone()
        try: value=int(row[0]) if row else int(default)
        except (TypeError,ValueError): value=int(default)
        return max(minimum,min(maximum,value))
    def boolval(key,default):
        row=conn.execute("SELECT value FROM app_settings WHERE key=?",(key,)).fetchone()
        value=row[0] if row else ("1" if default else "0")
        return str(value or "").strip().lower() in {"1","true","yes","on"}
    validity=intval("bonus_default_valid_days",90,1,730)
    transfer_after=intval("bonus_transfer_after_days",30,0,729)
    if transfer_after>=validity: transfer_after=max(0,validity-1)
    return {"default_valid_days":validity,"transfer_after_days":transfer_after,"transfer_enabled":boolval("bonus_transfer_enabled",True)}


def bonus_policy_settings():
    with _lock,_connect() as conn:
        return _bonus_policy_settings_conn(conn)


def set_bonus_policy_settings(default_valid_days, transfer_after_days, transfer_enabled=True):
    validity=max(1,min(730,int(default_valid_days)))
    transfer_after=max(0,min(729,int(transfer_after_days)))
    if transfer_after>=validity:
        raise ValueError("Weitergabe muss vor Ablauf der Bonusgültigkeit möglich werden")
    set_setting("bonus_default_valid_days",validity)
    set_setting("bonus_transfer_after_days",transfer_after)
    set_setting("bonus_transfer_enabled","1" if transfer_enabled else "0")
    return bonus_policy_settings()


def active_event_leaderboards_for_portal(user_id):
    now=utc_now()
    show_names=setting_bool("portal_leaderboard_show_names", False)
    with _lock,_connect() as conn:
        user=conn.execute("SELECT gamification_enabled FROM users WHERE id=?",(user_id,)).fetchone()
        if not user or not bool(user["gamification_enabled"]):
            return []
        events=conn.execute("SELECT * FROM gamification_events WHERE active=1 AND starts_at<=? AND ends_at>? ORDER BY ends_at",(now,now)).fetchall()
        result=[]
        for event in events:
            board=_event_leaderboard_conn(conn,event)
            safe=[]
            for item in board:
                mine=item["user_id"]==user_id
                safe.append({
                    "rank":item["rank"],
                    "name":item["name"] if (mine or show_names) else "********",
                    "sessions":item["sessions"],
                    "energy_kwh":item["energy_kwh"],
                    "value":item["value"],
                    "qualified":item.get("qualified",True),
                    "qualification_sessions":item.get("qualification_sessions"),
                    "min_sessions":item.get("min_sessions"),
                    "min_session_kwh":item.get("min_session_kwh"),
                    "avg_stand_seconds":item.get("avg_stand_seconds"),
                    "total_stand_seconds":item.get("total_stand_seconds"),
                    "unplug_ratio":item.get("unplug_ratio"),
                    "avg_energy_kwh":item.get("avg_energy_kwh"),
                    "quick_unplug_sessions":item.get("quick_unplug_sessions"),
                    "is_me":mine,
                })
            e=dict(event); e["leaderboard"]=safe; e["names_visible"]=show_names; result.append(e)
        return result


def _leaderboard_bounds(period, now=None):
    berlin=ZoneInfo("Europe/Berlin")
    current=(now or datetime.now(timezone.utc)).astimezone(berlin)
    if period=="month":
        start=current.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        end=(start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1))
        label=start.strftime("%m/%Y")
    elif period=="year":
        start=current.replace(month=1,day=1,hour=0,minute=0,second=0,microsecond=0)
        end=start.replace(year=start.year+1)
        label=str(start.year)
    elif period=="all":
        return None,None,"Gesamt"
    else:
        raise ValueError("Ungültiger Ranglisten-Zeitraum")
    return start.astimezone(timezone.utc).isoformat(),end.astimezone(timezone.utc).isoformat(),label


def leaderboard_metric_catalog():
    keys=set(LEADERBOARD_ALWAYS_ENABLED)
    with _lock,_connect() as conn:
        rows=conn.execute("""SELECT DISTINCT metric FROM achievements
            WHERE active=1 AND COALESCE(leaderboard_enabled,0)=1 AND COALESCE(system_secret,0)=0""").fetchall()
    for r in rows:
        key=_leaderboard_metric_key(r["metric"])
        if key:
            keys.add(key)
    result=[]
    for key in keys:
        meta=LEADERBOARD_METRIC_META.get(key)
        if not meta:
            continue
        result.append({"key":key,**meta})
    result.sort(key=lambda x:(int(x.get("order") or 999),x["label"].casefold()))
    return result


def _leaderboard_tx_rows_conn(conn,user_id,start=None,end=None):
    date_sql=""
    params=[int(user_id),int(user_id),int(user_id)]
    if start is not None:
        date_sql=" AND t.started_at>=? AND t.started_at<?"
        params.extend([start,end])
    return conn.execute("""SELECT t.* FROM transactions t
        WHERE (t.user_id=? OR (
          t.user_id IS NULL AND (
            t.id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?)
            OR t.id_tag=(SELECT rfid FROM users WHERE id=?)
          )
        ))"""+date_sql+" ORDER BY t.started_at",params).fetchall()


def _leaderboard_metric_value_conn(conn,user_id,metric,start=None,end=None):
    if metric in {"xp","achievement_count"}:
        date_sql=""; params=[int(user_id)]
        if start is not None:
            date_sql=" AND x.awarded_at>=? AND x.awarded_at<?"
            params.extend([start,end])
        row=conn.execute("""SELECT COALESCE(SUM(CASE WHEN a.active=1 THEN COALESCE(a.xp,0) ELSE 0 END),0) AS xp,
                                  COUNT(CASE WHEN a.active=1 THEN a.id END) AS achievements
            FROM achievement_awards x JOIN achievements a ON a.id=x.achievement_id
            WHERE x.user_id=?"""+date_sql,params).fetchone()
        return float(row["xp"] or 0) if metric=="xp" else float(row["achievements"] or 0)

    rows=_leaderboard_tx_rows_conn(conn,user_id,start,end)
    if metric=="energy_kwh":
        return sum(float(r["energy_kwh"] or 0) for r in rows)
    if metric=="sessions":
        return float(len(rows))
    if metric=="charging_hours_total":
        total=0.0
        for r in rows:
            charging,_,_=_analytics_row_timing_conn(conn,r)
            total+=charging/3600.0
        return total
    if metric=="max_session_energy":
        return max([float(r["energy_kwh"] or 0) for r in rows] or [0.0])
    if metric=="max_session_power":
        return max([float(r["max_power_kw"] or 0) for r in rows] or [0.0])
    if metric=="distinct_charge_points":
        return float(len({str(r["charge_point_id"]) for r in rows if r["charge_point_id"] not in (None,"")}))
    if metric=="distinct_vehicles":
        return float(len({int(r["vehicle_id"]) for r in rows if r["vehicle_id"] not in (None,"")}))
    if metric=="days_active":
        return float(len({dt.date().isoformat() for r in rows if (dt:=_tx_local_start(r))}))
    if metric=="months_active":
        return float(len({(dt.year,dt.month) for r in rows if (dt:=_tx_local_start(r))}))
    if metric=="weekend_sessions":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and dt.weekday()>=5))
    if metric=="early_sessions":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and 5<=dt.hour<8))
    if metric=="evening_sessions":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and 17<=dt.hour<22))
    if metric=="night_sessions":
        return float(sum(1 for r in rows if (dt:=_tx_local_start(r)) and (dt.hour>=22 or dt.hour<5)))
    if metric in {"long_sessions","quick_sessions"}:
        count=0
        for r in rows:
            charging,stand,connection=_analytics_row_timing_conn(conn,r)
            seconds=max(float(connection or 0),float(charging or 0)+float(stand or 0))
            energy=float(r["energy_kwh"] or 0)
            if metric=="long_sessions" and seconds>=4*3600:
                count+=1
            if metric=="quick_sessions" and energy>0 and 0<seconds<=45*60:
                count+=1
        return float(count)
    if metric=="prompt_unplug_sessions":
        count=0
        for r in rows:
            if "post_session_occupied_seconds" not in r.keys() or r["post_session_occupied_seconds"] is None:
                continue
            try: seconds=float(r["post_session_occupied_seconds"])
            except (TypeError,ValueError): continue
            if float(r["energy_kwh"] or 0)>0 and 0<=seconds<=PROMPT_UNPLUG_SECONDS:
                count+=1
        return float(count)
    if metric in {"max_month_energy","max_month_sessions"}:
        months={}
        for r in rows:
            dt=_tx_local_start(r)
            if not dt:
                continue
            key=(dt.year,dt.month)
            bucket=months.setdefault(key,{"energy":0.0,"sessions":0})
            bucket["energy"]+=float(r["energy_kwh"] or 0)
            bucket["sessions"]+=1
        if not months:
            return 0.0
        key="energy" if metric=="max_month_energy" else "sessions"
        return float(max(x[key] for x in months.values()))
    if metric=="week_streak":
        return float(_max_consecutive_iso_weeks(rows))
    return 0.0


def general_leaderboard(period="month", metric="energy_kwh", now=None):
    available={x["key"] for x in leaderboard_metric_catalog()}
    if metric not in available:
        raise ValueError("Ungültige oder deaktivierte Ranking-Metrik")
    start,end,label=_leaderboard_bounds(period,now)
    if metric in {"xp","achievement_count"}:
        reconcile_automatic_achievements()
    with _lock,_connect() as conn:
        users=conn.execute("""SELECT id,name FROM users
            WHERE status='Aktiv' AND COALESCE(gamification_enabled,1)=1
            ORDER BY name COLLATE NOCASE""").fetchall()
        items=[]
        for user in users:
            value=float(_leaderboard_metric_value_conn(conn,int(user["id"]),metric,start,end) or 0)
            items.append({
                "user_id":int(user["id"]),"name":user["name"],"value":round(value,3),
            })
        items.sort(key=lambda x:(-x["value"],x["name"].casefold()))
        rank=0; last=None
        for i,item in enumerate(items,1):
            if last is None or item["value"]!=last:
                rank=i; last=item["value"]
            item["rank"]=rank
        meta=LEADERBOARD_METRIC_META[metric]
        return {
            "period":period,"period_label":label,"metric":metric,
            "metric_label":meta["label"],"metric_icon":meta["icon"],"metric_description":meta.get("description",""),"unit":meta["unit"],
            "decimals":meta["decimals"],"leaderboard":items,
        }



PORTAL_LEADERBOARD_PERIODS=[
    {"key":"month","label":"Dieser Monat"},
    {"key":"year","label":"Dieses Jahr"},
    {"key":"all","label":"Gesamt"},
]


def portal_general_leaderboards(user_id, period="all", metric="xp"):
    """Return one privacy-safe permanent leaderboard plus the selectable catalogue."""
    with _lock,_connect() as conn:
        user=conn.execute("SELECT gamification_enabled FROM users WHERE id=?",(user_id,)).fetchone()
        if not user or not bool(user["gamification_enabled"]):
            return {
                "board":None,
                "metrics":leaderboard_metric_catalog(),
                "periods":PORTAL_LEADERBOARD_PERIODS,
                "selected_metric":None,
                "selected_period":"all",
                "names_visible":setting_bool("portal_leaderboard_show_names",False),
            }

    catalog=leaderboard_metric_catalog()
    available={x["key"] for x in catalog}
    selected_metric=str(metric or "xp")
    if selected_metric not in available:
        selected_metric="xp" if "xp" in available else ("energy_kwh" if "energy_kwh" in available else (catalog[0]["key"] if catalog else None))

    selected_period=str(period or "all")
    if selected_period not in {"month","year","all"}:
        selected_period="all"

    show_names=setting_bool("portal_leaderboard_show_names",False)
    board=general_leaderboard(selected_period,selected_metric) if selected_metric else None
    if board:
        safe=[]
        for item in board["leaderboard"]:
            mine=int(item["user_id"])==int(user_id)
            safe.append({
                **item,
                "name":item["name"] if (mine or show_names) else "********",
                "is_me":mine,
            })
        board["leaderboard"]=safe
        board["title"]=f"{board.get('metric_icon') or '🏁'} {board.get('metric_label') or selected_metric}"
        board["my_entry"]=next((x for x in safe if x["is_me"]),None)

    return {
        "board":board,
        "metrics":catalog,
        "periods":PORTAL_LEADERBOARD_PERIODS,
        "selected_metric":selected_metric,
        "selected_period":selected_period,
        "names_visible":show_names,
    }



def _bonus_wallet_conn(conn,user_id,now=None):
    now_dt=_parse_iso_utc(now) if isinstance(now,str) else (now or datetime.now(timezone.utc))
    if not isinstance(now_dt,datetime): now_dt=datetime.now(timezone.utc)
    now_iso=now_dt.isoformat()
    policy=_bonus_policy_settings_conn(conn)
    rows=conn.execute("""SELECT g.*,
        CASE WHEN g.active=0 THEN 'inactive' WHEN g.expires_at<=? THEN 'expired' WHEN g.remaining_kwh<=0.0000001 THEN 'used' ELSE 'available' END AS state
        FROM bonus_grants g WHERE g.user_id=? ORDER BY g.expires_at,g.granted_at,g.id""",(now_iso,user_id)).fetchall()
    items=[]
    transferable=0.0; next_transfer_at=None
    for row in rows:
        item=dict(row)
        granted=_parse_iso_utc(item.get("granted_at")) or now_dt
        eligible_at=granted+timedelta(days=policy["transfer_after_days"])
        item["transfer_eligible_at"]=eligible_at.isoformat()
        expiry_dt=_parse_iso_utc(item.get("expires_at")) or eligible_at
        item["transfer_unlocks_before_expiry"]=bool(eligible_at<expiry_dt)
        item["transferable"]=bool(policy["transfer_enabled"] and item["state"]=="available" and eligible_at<=now_dt and item["transfer_unlocks_before_expiry"])
        if item["transferable"]:
            transferable+=float(item.get("remaining_kwh") or 0)
        elif policy["transfer_enabled"] and item["state"]=="available" and item["transfer_unlocks_before_expiry"]:
            if next_transfer_at is None or eligible_at.isoformat()<next_transfer_at: next_transfer_at=eligible_at.isoformat()
        items.append(item)
    available=sum(float(x["remaining_kwh"] or 0) for x in items if x["state"]=="available")
    next_expiry=next((x["expires_at"] for x in items if x["state"]=="available"),None)
    return {"available_kwh":round(available,3),"transferable_kwh":round(transferable,3),"next_transfer_at":next_transfer_at,"expiring_next":next_expiry,"grants":items,"policy":policy}


def bonus_wallet(user_id,now=None):
    with _lock,_connect() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?",(user_id,)).fetchone(): return None
        return _bonus_wallet_conn(conn,user_id,now)


def grant_bonus_kwh(user_id,amount_kwh,expires_at,source="admin",note=None,voucher_id=None):
    amount=float(amount_kwh or 0)
    if amount<=0: raise ValueError("Bonus-kWh müssen größer als 0 sein")
    expiry=_parse_iso_utc(expires_at)
    if not expiry or expiry<=datetime.now(timezone.utc): raise ValueError("Ablaufdatum muss in der Zukunft liegen")
    with _lock,_connect() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?",(user_id,)).fetchone(): raise ValueError("Benutzer nicht gefunden")
        cur=conn.execute("INSERT INTO bonus_grants(user_id,amount_kwh,remaining_kwh,granted_at,expires_at,source,note,voucher_id,active) VALUES(?,?,?,?,?,?,?,?,1)",(user_id,amount,amount,utc_now(),expiry.isoformat(),source,note or None,voucher_id))
        conn.commit(); return int(cur.lastrowid)


def revoke_bonus_grant(grant_id):
    with _lock,_connect() as conn:
        used=conn.execute("SELECT COALESCE(SUM(amount_kwh),0) FROM bonus_usage WHERE grant_id=?",(grant_id,)).fetchone()[0]
        if float(used or 0)>1e-9: raise ValueError("Bereits verwendetes Bonusguthaben kann nicht widerrufen werden")
        cur=conn.execute("UPDATE bonus_grants SET active=0,remaining_kwh=0 WHERE id=? AND active=1",(grant_id,)); conn.commit(); return cur.rowcount>0


def _allocate_bonus_for_transaction_conn(conn,tx_id,user_id,session_energy,used_at=None):
    # Idempotent: a completed transaction is allocated at most once.
    if conn.execute("SELECT 1 FROM bonus_usage WHERE transaction_id=? LIMIT 1",(tx_id,)).fetchone(): return 0.0
    tx=conn.execute("SELECT started_at FROM transactions WHERE id=?",(tx_id,)).fetchone()
    if not tx: return 0.0
    started=_parse_iso_utc(tx["started_at"])
    if not started: return 0.0
    start_utc,end_utc,_=_month_bounds_utc(started)
    user=conn.execute("SELECT monthly_kwh_limit FROM users WHERE id=?",(user_id,)).fetchone()
    if not user or user["monthly_kwh_limit"] is None: return 0.0
    limit=max(0.0,float(user["monthly_kwh_limit"]))
    prior=conn.execute("""SELECT COALESCE(SUM(energy_kwh),0) FROM transactions
        WHERE id<>? AND (user_id=? OR (user_id IS NULL AND (id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?) OR id_tag=(SELECT rfid FROM users WHERE id=?))))
          AND started_at>=? AND started_at<? AND (ended_at IS NOT NULL OR status<>'Active')""",(tx_id,user_id,user_id,user_id,start_utc,end_utc)).fetchone()[0]
    base_remaining=max(0.0,limit-float(prior or 0))
    need=max(0.0,float(session_energy or 0)-base_remaining)
    if need<=1e-9: return 0.0
    when=_parse_iso_utc(used_at) or datetime.now(timezone.utc)
    # Eligibility is frozen at session start: a package that was valid when the
    # user started charging may finish that session even if it expires meanwhile.
    rows=conn.execute("""SELECT * FROM bonus_grants WHERE user_id=? AND active=1 AND remaining_kwh>0.0000001 AND granted_at<=? AND expires_at>? ORDER BY expires_at,granted_at,id""",(user_id,started.isoformat(),started.isoformat())).fetchall()
    allocated=0.0
    for grant in rows:
        if need<=1e-9: break
        take=min(need,float(grant["remaining_kwh"] or 0))
        if take<=1e-9: continue
        conn.execute("INSERT OR IGNORE INTO bonus_usage(grant_id,user_id,transaction_id,amount_kwh,used_at) VALUES(?,?,?,?,?)",(grant["id"],user_id,tx_id,take,when.isoformat()))
        conn.execute("UPDATE bonus_grants SET remaining_kwh=MAX(0,remaining_kwh-?) WHERE id=?",(take,grant["id"]))
        allocated+=take; need-=take
    return round(allocated,6)


def list_bonus_grants(limit=250):
    with _lock,_connect() as conn:
        now=utc_now()
        return [dict(r) for r in conn.execute("""SELECT g.*,u.name AS user_name,
            CASE WHEN g.active=0 THEN 'inactive' WHEN g.expires_at<=? THEN 'expired' WHEN g.remaining_kwh<=0.0000001 THEN 'used' ELSE 'available' END AS state
            FROM bonus_grants g JOIN users u ON u.id=g.user_id ORDER BY g.id DESC LIMIT ?""",(now,limit)).fetchall()]


def create_bonus_voucher(code,amount_kwh,redeem_until=None,bonus_valid_days=None,max_redemptions=1,note=None,active=True):
    code=str(code or "").strip().upper()
    if len(code)<6: raise ValueError("Gutscheincode muss mindestens 6 Zeichen haben")
    amount=float(amount_kwh or 0)
    if amount<=0: raise ValueError("Bonus-kWh müssen größer als 0 sein")
    days=max(1,int(bonus_valid_days or bonus_policy_settings()["default_valid_days"])); max_redemptions=max(1,int(max_redemptions or 1))
    if redeem_until and not _parse_iso_utc(redeem_until): raise ValueError("Ungültiges Einlöse-Ende")
    with _lock,_connect() as conn:
        try:
            cur=conn.execute("INSERT INTO bonus_vouchers(code,amount_kwh,redeem_until,bonus_valid_days,max_redemptions,active,note,created_at) VALUES(?,?,?,?,?,?,?,?)",(code,amount,redeem_until,days,max_redemptions,1 if active else 0,note or None,utc_now()))
        except sqlite3.IntegrityError: raise ValueError("Dieser Gutscheincode existiert bereits")
        conn.commit(); return int(cur.lastrowid)


def list_bonus_vouchers():
    with _lock,_connect() as conn:
        return [dict(r) for r in conn.execute("""SELECT v.*,(SELECT COUNT(*) FROM bonus_voucher_redemptions r WHERE r.voucher_id=v.id) AS redemptions
            FROM bonus_vouchers v ORDER BY v.id DESC""").fetchall()]


def set_bonus_voucher_active(voucher_id,active):
    with _lock,_connect() as conn:
        cur=conn.execute("UPDATE bonus_vouchers SET active=? WHERE id=?",(1 if active else 0,voucher_id)); conn.commit(); return cur.rowcount>0

def update_bonus_voucher(voucher_id, code=None, amount_kwh=None, redeem_until=None, bonus_valid_days=None, max_redemptions=None, note=None, active=True):
    with _lock,_connect() as conn:
        row=conn.execute("""SELECT v.*,(SELECT COUNT(*) FROM bonus_voucher_redemptions r WHERE r.voucher_id=v.id) AS redemptions
            FROM bonus_vouchers v WHERE v.id=?""",(int(voucher_id),)).fetchone()
        if not row: return False
        if int(row["redemptions"] or 0)>0:
            raise ValueError("Dieser Gutschein wurde bereits eingelöst und kann nicht mehr inhaltlich bearbeitet werden. Er kann nur deaktiviert werden.")
        clean_code=str(code or row["code"] or "").strip().upper()
        if len(clean_code)<6: raise ValueError("Gutscheincode muss mindestens 6 Zeichen haben")
        amount=float(row["amount_kwh"] if amount_kwh is None else amount_kwh)
        if amount<=0: raise ValueError("Bonus-kWh müssen größer als 0 sein")
        days=max(1,int(row["bonus_valid_days"] if bonus_valid_days is None else bonus_valid_days))
        maximum=max(1,int(row["max_redemptions"] if max_redemptions is None else max_redemptions))
        if redeem_until and not _parse_iso_utc(redeem_until): raise ValueError("Ungültiges Einlöse-Ende")
        try:
            conn.execute("""UPDATE bonus_vouchers SET code=?,amount_kwh=?,redeem_until=?,bonus_valid_days=?,max_redemptions=?,active=?,note=?
                WHERE id=?""",(clean_code,amount,redeem_until,days,maximum,1 if active else 0,note or None,int(voucher_id)))
            conn.commit(); return True
        except sqlite3.IntegrityError as exc:
            raise ValueError("Dieser Gutscheincode existiert bereits") from exc


def delete_bonus_voucher(voucher_id):
    with _lock,_connect() as conn:
        row=conn.execute("""SELECT v.id,(SELECT COUNT(*) FROM bonus_voucher_redemptions r WHERE r.voucher_id=v.id) AS redemptions
            FROM bonus_vouchers v WHERE v.id=?""",(int(voucher_id),)).fetchone()
        if not row: return False
        if int(row["redemptions"] or 0)>0:
            raise ValueError("Dieser Gutschein wurde bereits eingelöst und kann aus Nachvollziehbarkeitsgründen nicht gelöscht werden. Bitte deaktivieren.")
        conn.execute("DELETE FROM bonus_vouchers WHERE id=?",(int(voucher_id),))
        conn.commit(); return True



def redeem_bonus_voucher(user_id,code):
    code=str(code or "").strip().upper(); now=datetime.now(timezone.utc); now_iso=now.isoformat()
    with _lock,_connect() as conn:
        user=conn.execute("SELECT id FROM users WHERE id=? AND status='Aktiv'",(user_id,)).fetchone()
        if not user: raise ValueError("Benutzer nicht gefunden")
        v=conn.execute("SELECT * FROM bonus_vouchers WHERE code=? COLLATE NOCASE",(code,)).fetchone()
        if not v or not bool(v["active"]): raise ValueError("Gutschein ist ungültig oder nicht aktiv")
        if v["redeem_until"] and (_parse_iso_utc(v["redeem_until"]) or now)<=now: raise ValueError("Gutschein ist abgelaufen")
        if conn.execute("SELECT 1 FROM bonus_voucher_redemptions WHERE voucher_id=? AND user_id=?",(v["id"],user_id)).fetchone(): raise ValueError("Dieser Gutschein wurde bereits eingelöst")
        count=conn.execute("SELECT COUNT(*) FROM bonus_voucher_redemptions WHERE voucher_id=?",(v["id"],)).fetchone()[0]
        if int(count)>=int(v["max_redemptions"]): raise ValueError("Gutschein ist vollständig eingelöst")
        expires=(now+timedelta(days=max(1,int(v["bonus_valid_days"] or _bonus_policy_settings_conn(conn)["default_valid_days"])))).isoformat()
        cur=conn.execute("INSERT INTO bonus_grants(user_id,amount_kwh,remaining_kwh,granted_at,expires_at,source,note,voucher_id,active) VALUES(?,?,?,?,?,'voucher',?,?,1)",(user_id,float(v["amount_kwh"]),float(v["amount_kwh"]),now_iso,expires,v["note"],v["id"]))
        grant_id=int(cur.lastrowid)
        conn.execute("INSERT INTO bonus_voucher_redemptions(voucher_id,user_id,grant_id,redeemed_at) VALUES(?,?,?,?)",(v["id"],user_id,grant_id,now_iso))
        conn.commit(); return {"grant_id":grant_id,"amount_kwh":float(v["amount_kwh"]),"expires_at":expires}


def bonus_transfer_recipients(user_id):
    with _lock,_connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id,name FROM users WHERE status='Aktiv' AND id<>? ORDER BY name COLLATE NOCASE",(user_id,)).fetchall()]


def bonus_transfer_history(user_id,limit=20):
    with _lock,_connect() as conn:
        rows=conn.execute("""SELECT bt.*,fu.name AS from_name,tu.name AS to_name
            FROM bonus_transfers bt JOIN users fu ON fu.id=bt.from_user_id JOIN users tu ON tu.id=bt.to_user_id
            WHERE bt.from_user_id=? OR bt.to_user_id=? ORDER BY bt.id DESC LIMIT ?""",(user_id,user_id,max(1,min(int(limit or 20),100)))).fetchall()
        return [dict(r) for r in rows]


def transfer_bonus_kwh(from_user_id,to_user_id,amount_kwh,now=None):
    policy=bonus_policy_settings()
    if not policy["transfer_enabled"]: raise ValueError("Die Weitergabe von Bonus-kWh ist derzeit deaktiviert")
    if int(from_user_id)==int(to_user_id): raise ValueError("Bonus-kWh können nicht an sich selbst übertragen werden")
    amount=round(float(amount_kwh or 0),6)
    if amount<=0: raise ValueError("Bitte eine Bonusmenge größer als 0 kWh angeben")
    now_dt=_parse_iso_utc(now) if isinstance(now,str) else (now or datetime.now(timezone.utc))
    if not isinstance(now_dt,datetime): now_dt=datetime.now(timezone.utc)
    now_iso=now_dt.isoformat(); cutoff=(now_dt-timedelta(days=policy["transfer_after_days"])).isoformat()
    with _lock,_connect() as conn:
        sender=conn.execute("SELECT id,name FROM users WHERE id=? AND status='Aktiv'",(from_user_id,)).fetchone()
        recipient=conn.execute("SELECT id,name FROM users WHERE id=? AND status='Aktiv'",(to_user_id,)).fetchone()
        if not sender: raise ValueError("Absender nicht gefunden oder nicht aktiv")
        if not recipient: raise ValueError("Empfänger nicht gefunden oder nicht aktiv")
        rows=conn.execute("""SELECT * FROM bonus_grants WHERE user_id=? AND active=1 AND remaining_kwh>0.0000001
            AND expires_at>? AND granted_at<=? ORDER BY expires_at,granted_at,id""",(from_user_id,now_iso,cutoff)).fetchall()
        eligible=round(sum(float(r["remaining_kwh"] or 0) for r in rows),6)
        if eligible+1e-9<amount: raise ValueError(f"Aktuell sind maximal {eligible:.1f} Bonus-kWh übertragbar")
        remaining=amount; parts=[]
        try:
            for source in rows:
                if remaining<=1e-9: break
                take=min(remaining,float(source["remaining_kwh"] or 0))
                if take<=1e-9: continue
                note=f"Übertragen von {sender['name']}"
                cur=conn.execute("INSERT INTO bonus_grants(user_id,amount_kwh,remaining_kwh,granted_at,expires_at,source,note,voucher_id,active) VALUES(?,?,?,?,?,'transfer',?,NULL,1)",(to_user_id,take,take,now_iso,source["expires_at"],note))
                target_id=int(cur.lastrowid)
                conn.execute("UPDATE bonus_grants SET remaining_kwh=MAX(0,remaining_kwh-?) WHERE id=?",(take,source["id"]))
                conn.execute("INSERT INTO bonus_transfers(from_user_id,to_user_id,source_grant_id,target_grant_id,amount_kwh,transferred_at,expires_at) VALUES(?,?,?,?,?,?,?)",(from_user_id,to_user_id,source["id"],target_id,take,now_iso,source["expires_at"]))
                parts.append({"source_grant_id":source["id"],"target_grant_id":target_id,"amount_kwh":round(take,6),"expires_at":source["expires_at"]})
                remaining-=take
            conn.commit()
        except Exception:
            conn.rollback(); raise
        return {"amount_kwh":round(amount,3),"to_user_id":int(to_user_id),"to_name":recipient["name"],"parts":parts}


def _portal_driver_profile(user_id, tx_rows, analytics, gamification, achievements):
    berlin=ZoneInfo("Europe/Berlin")
    parsed=[]
    for row in tx_rows:
        started=_parse_iso_utc(row["started_at"])
        if not started:
            continue
        parsed.append((row,started.astimezone(berlin)))

    favorite_cp=None
    cp_stats={}
    for row,started in parsed:
        cp=str(row["charge_point_id"] or "").strip()
        if not cp:
            continue
        bucket=cp_stats.setdefault(cp,{"sessions":0,"energy_kwh":0.0})
        bucket["sessions"]+=1
        bucket["energy_kwh"]+=float(row["energy_kwh"] or 0)
    if cp_stats:
        favorite_id,favorite_data=max(cp_stats.items(),key=lambda x:(x[1]["sessions"],x[1]["energy_kwh"],x[0]))
        with _lock,_connect() as conn:
            cp_row=conn.execute("SELECT id,location FROM charge_points WHERE id=?",(favorite_id,)).fetchone()
        favorite_cp={
            "id":favorite_id,
            "label":(cp_row["location"] if cp_row and cp_row["location"] else favorite_id),
            "sessions":int(favorite_data["sessions"]),
            "energy_kwh":round(float(favorite_data["energy_kwh"]),2),
        }

    dayparts={
        "morning":{"label":"Frühlader","icon":"🌅","sessions":0},
        "day":{"label":"Tagsüber","icon":"☀️","sessions":0},
        "evening":{"label":"Feierabend-Lader","icon":"🌇","sessions":0},
        "night":{"label":"Nachtlader","icon":"🌙","sessions":0},
    }
    weekdays=[{"label":x,"sessions":0} for x in ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]]
    active_days=set()
    for _,started in parsed:
        active_days.add(started.date().isoformat())
        weekdays[started.weekday()]["sessions"]+=1
        hour=started.hour
        if 5<=hour<9:
            dayparts["morning"]["sessions"]+=1
        elif 9<=hour<17:
            dayparts["day"]["sessions"]+=1
        elif 17<=hour<22:
            dayparts["evening"]["sessions"]+=1
        else:
            dayparts["night"]["sessions"]+=1
    preferred_daypart=max(dayparts.values(),key=lambda x:x["sessions"]) if parsed else None
    preferred_weekday=max(weekdays,key=lambda x:x["sessions"]) if parsed else None

    record_energy=None
    record_power=None
    if tx_rows:
        energy_row=max(tx_rows,key=lambda r:float(r["energy_kwh"] or 0))
        power_row=max(tx_rows,key=lambda r:float(r["max_power_kw"] or 0))
        if float(energy_row["energy_kwh"] or 0)>0:
            record_energy={"value":round(float(energy_row["energy_kwh"] or 0),2),"started_at":energy_row["started_at"]}
        if float(power_row["max_power_kw"] or 0)>0:
            record_power={"value":round(float(power_row["max_power_kw"] or 0),2),"started_at":power_row["started_at"]}

    current=(analytics or {}).get("current_month") or {}
    previous=(analytics or {}).get("previous_month") or {}
    def delta(current_value,previous_value):
        current_value=float(current_value or 0)
        previous_value=float(previous_value or 0)
        if previous_value<=0:
            return None if current_value>0 else 0.0
        return round((current_value-previous_value)/previous_value*100.0,1)
    month_compare={
        "current_energy_kwh":round(float(current.get("energy_kwh") or 0),2),
        "previous_energy_kwh":round(float(previous.get("energy_kwh") or 0),2),
        "energy_delta_pct":delta(current.get("energy_kwh"),previous.get("energy_kwh")),
        "current_sessions":int(current.get("sessions") or 0),
        "previous_sessions":int(previous.get("sessions") or 0),
        "sessions_delta_pct":delta(current.get("sessions"),previous.get("sessions")),
    }

    month_buckets={}
    for row,started in parsed:
        key=started.strftime("%Y-%m")
        bucket=month_buckets.setdefault(key,{"month":key,"label":f"{started.month:02d}/{started.year}","sessions":0,"energy_kwh":0.0})
        bucket["sessions"]+=1
        bucket["energy_kwh"]+=float(row["energy_kwh"] or 0)
    best_month=max(month_buckets.values(),key=lambda x:(float(x["energy_kwh"]),int(x["sessions"]))) if month_buckets else None
    if best_month:
        best_month={**best_month,"energy_kwh":round(float(best_month["energy_kwh"]),2)}

    earned=[x for x in (achievements or []) if x.get("earned")]
    recent=sorted(earned,key=lambda x:str(x.get("awarded_at") or ""),reverse=True)[:5]
    rarity_order={"common":1,"uncommon":2,"rare":3,"epic":4,"legendary":5,"secret":6}
    rarity_labels={"common":"Gewöhnlich","uncommon":"Ungewöhnlich","rare":"Selten","epic":"Episch","legendary":"Legendär","secret":"Geheim"}
    rarest=max(earned,key=lambda x:(rarity_order.get(str(x.get("rarity") or "common"),0),int(x.get("xp") or 0))) if earned else None
    for item in recent:
        item["rarity_label"]=rarity_labels.get(str(item.get("rarity") or "common"),"Gewöhnlich")
    if rarest:
        rarest=dict(rarest)
        rarest["rarity_label"]=rarity_labels.get(str(rarest.get("rarity") or "common"),"Gewöhnlich")

    xp_rank=None
    participants=0
    if gamification:
        overview=gamification_overview()
        participants=len(overview)
        xp_rank=next((int(x["rank"]) for x in overview if int(x["user_id"])==int(user_id)),None)

    primary_vehicle=primary_vehicle_for_user(user_id)
    first_started=min((dt for _,dt in parsed),default=None)

    return {
        "xp_rank":xp_rank,
        "participants":participants,
        "favorite_charge_point":favorite_cp,
        "preferred_daypart":preferred_daypart if preferred_daypart and preferred_daypart["sessions"] else None,
        "preferred_weekday":preferred_weekday if preferred_weekday and preferred_weekday["sessions"] else None,
        "record_energy":record_energy,
        "record_power":record_power,
        "active_days":len(active_days),
        "charging_since":first_started.date().isoformat() if first_started else None,
        "month_compare":month_compare,
        "best_month":best_month,
        "recent_achievements":recent,
        "rarest_achievement":rarest,
        "primary_vehicle":primary_vehicle,
        "all_time_energy_kwh":round(float(((analytics or {}).get("all_time") or {}).get("energy_kwh") or 0),2),
        "all_time_sessions":int(((analytics or {}).get("all_time") or {}).get("sessions") or 0),
    }


def portal_dashboard(user_id, period=None, include_inactive=False, ranking_metric=None, ranking_period=None):
    evaluate_user_achievements(user_id)
    start,end,month=_month_bounds_utc()
    with _lock,_connect() as conn:
        status_clause="" if include_inactive else " AND status='Aktiv'"
        row=conn.execute("SELECT id,name,role,department,status,monthly_kwh_limit,monthly_limit_mode,portal_last_login_at,gamification_enabled,image_path FROM users WHERE id=?"+status_clause,(user_id,)).fetchone()
        if not row: return None
        user=dict(row)
        tx_rows=conn.execute("""SELECT t.* FROM transactions t
            WHERE t.user_id=? OR (t.user_id IS NULL AND (t.id_tag IN (SELECT uid FROM rfid_cards WHERE user_id=?) OR t.id_tag=(SELECT rfid FROM users WHERE id=?)))
            ORDER BY t.started_at""",(user_id,user_id,user_id)).fetchall()
        period_ctx=_portal_period_context(tx_rows,period)
        period_summary,selected_rows=_portal_period_summary_conn(conn,tx_rows,period_ctx)
        period_budget=None
        if period_ctx.get("selected_key")!="all":
            period_used=max(0.0,float(period_summary.get("energy_kwh") or 0))
            period_limit=user.get("monthly_kwh_limit")
            period_mode=user.get("monthly_limit_mode") or "warn"
            period_state=_budget_status(period_limit,period_used,period_mode)
            period_budget={
                "month":period_ctx.get("selected_key"),
                "label":period_ctx.get("selected_label"),
                "limit_kwh":None if period_limit is None else float(period_limit),
                "used_kwh":round(period_used,3),
                "mode":period_mode,
                **period_state,
            }
        selected_ids={int(r["id"]) for r in selected_rows}
        recent=[]
        for r in reversed(tx_rows):
            if int(r["id"]) not in selected_ids: continue
            recent.append({k:r[k] for k in ("id","started_at","ended_at","energy_kwh","status","charge_point_id","cost_cents","tariff_name")})
            if len(recent)>=12: break
    budget=user_monthly_budget(user_id)
    analytics=user_analytics(user_id)
    achievements=achievements_for_user(user_id,include_locked=True)
    events=active_event_leaderboards_for_portal(user_id)
    ranking=portal_general_leaderboards(user_id,period=ranking_period or "all",metric=ranking_metric or "xp")
    general_rankings=[ranking["board"]] if ranking.get("board") else []
    gamification=user_gamification_profile(user_id) if bool(user.get("gamification_enabled",1)) else None
    gamification_reveals=(portal_gamification_reveals(user_id) if gamification and not include_inactive else {"pending":False,"achievements":[],"level_up":None,"ack_award_id":0,"ack_level":int((gamification or {}).get("level") or 1)})
    profile=_portal_driver_profile(user_id,tx_rows,analytics,gamification,achievements)
    bonus=bonus_wallet(user_id) or {"available_kwh":0.0,"transferable_kwh":0.0,"expiring_next":None,"grants":[],"policy":bonus_policy_settings()}
    bonus["recipients"]=bonus_transfer_recipients(user_id) if bonus.get("policy",{}).get("transfer_enabled") else []
    bonus["transfers"]=bonus_transfer_history(user_id,12)
    rfid_cards=portal_rfid_cards(user_id)
    return {
        "user":user,"budget":budget,"period_budget":period_budget,"analytics":analytics,"period":period_ctx,"period_stats":period_summary,
        "achievements":achievements,"events":events,"general_rankings":general_rankings,"ranking":ranking,"gamification":gamification,"gamification_reveals":gamification_reveals,"profile":profile,"bonus":bonus,
        "rfid_cards":rfid_cards,"recent_transactions":recent,"month":month,"leaderboard_names_visible":setting_bool("portal_leaderboard_show_names",False)
    }




# V0.9.7.21 - Registration & Onboarding rules
_REGISTRATION_BUILTIN_FIELDS = [
    {"id":"name","type":"text","label":"Name","enabled":True,"required":True,"system":True,"order":10,"help":"Vor- und Nachname"},
    {"id":"phone","type":"tel","label":"Telefonnummer","enabled":True,"required":False,"system":False,"order":30,"help":"Optional für Rückfragen"},
    {"id":"street","type":"text","label":"Straße und Hausnummer","enabled":True,"required":False,"system":False,"order":40,"help":"optional"},
    {"id":"postal_code","type":"text","label":"PLZ","enabled":True,"required":False,"system":False,"order":50,"help":"optional"},
    {"id":"city","type":"text","label":"Ort","enabled":True,"required":False,"system":False,"order":60,"help":"optional"},
    {"id":"vehicle_make_model","type":"text","label":"Hersteller / Modell","enabled":True,"required":False,"system":False,"order":70,"help":"Optional, z. B. VW ID.4"},
    {"id":"vehicle_plate","type":"text","label":"Kennzeichen","enabled":True,"required":False,"system":False,"order":80,"help":"optional, z. B. B-AB 123"},
]

def _registration_default_fields():
    return json.loads(json.dumps(_REGISTRATION_BUILTIN_FIELDS,ensure_ascii=False))

def registration_settings():
    """Return Community registration settings without employment-based rules."""
    def fnum(key,default,minimum,maximum):
        try: value=float(get_setting(key,str(default)))
        except (TypeError,ValueError): value=float(default)
        return max(float(minimum),min(float(maximum),value))
    enabled=setting_bool("registration_enabled",False)
    ref_kwh=fnum("registration_reference_kwh",0,0,10000)
    limit_mode="block" if str(get_setting("registration_limit_mode","warn")).lower()=="block" else "warn"
    raw=get_setting("registration_form_fields",None)
    try:
        fields=json.loads(raw) if raw else _registration_default_fields()
        if not isinstance(fields,list): raise ValueError()
    except Exception:
        fields=_registration_default_fields()
    defaults={x["id"]:x for x in _registration_default_fields()}
    clean=[]; seen=set()
    for item in fields:
        if not isinstance(item,dict): continue
        fid=str(item.get("id") or "").strip()
        if not fid or fid in seen or fid=="weekly_hours": continue
        base=defaults.get(fid,{})
        typ=str(item.get("type") or base.get("type") or "text").lower()
        if typ not in {"text","tel","number","date","textarea","select","checkbox"}: typ="text"
        system=bool(base.get("system"))
        entry={"id":fid,"type":typ,"label":str(item.get("label") or base.get("label") or fid)[:80],"enabled":bool(item.get("enabled",base.get("enabled",True))),"required":bool(item.get("required",base.get("required",False))),"system":system,"order":int(item.get("order",base.get("order",100)) or 100),"help":str(item.get("help") or base.get("help") or "")[:240]}
        if typ=="select": entry["options"]=[str(x)[:80] for x in (item.get("options") or []) if str(x).strip()][:30]
        if system: entry["enabled"]=True; entry["required"]=True
        clean.append(entry); seen.add(fid)
    for fid,base in defaults.items():
        if fid not in seen:
            clean.append(dict(base))
    clean.sort(key=lambda x:(int(x.get("order",100)),str(x.get("label","")).casefold()))
    return {"enabled":enabled,"budget_mode":"fixed","reference_hours":None,"reference_kwh":ref_kwh,"limit_mode":limit_mode,"fields":clean,"self_service_rfid_limit":2}

def calculate_registration_budget(weekly_hours=None, settings=None):
    """Legacy-compatible helper; Community always uses the fixed configured kWh value."""
    cfg=settings or registration_settings()
    ref_kwh=float(cfg.get("reference_kwh") or 0)
    return float(Decimal(str(ref_kwh)).quantize(Decimal("1"),rounding=ROUND_HALF_UP))

def save_registration_settings(payload):
    enabled=bool(payload.get("enabled",True))
    try: ref_kwh=float(payload.get("reference_kwh",0))
    except (TypeError,ValueError): raise ValueError("Das Standardbudget muss eine Zahl sein.")
    if not 0<=ref_kwh<=10000: raise ValueError("Das Standardbudget muss zwischen 0 und 10.000 kWh liegen.")
    limit_mode="block" if str(payload.get("limit_mode") or "warn").lower()=="block" else "warn"
    fields=payload.get("fields")
    if not isinstance(fields,list): fields=registration_settings()["fields"]
    defaults={x["id"]:x for x in _registration_default_fields()}; clean=[]; seen=set()
    for idx,item in enumerate(fields[:40]):
        if not isinstance(item,dict): continue
        fid=str(item.get("id") or "").strip()
        if not fid:
            fid="custom_"+hashlib.sha256((str(item.get("label") or "field")+str(idx)+utc_now()).encode()).hexdigest()[:10]
        if fid=="weekly_hours" or fid in seen: continue
        is_builtin=fid in defaults
        if not is_builtin and not fid.startswith("custom_"): fid="custom_"+re.sub(r"[^a-z0-9]+","_",fid.lower()).strip("_")[:30]
        typ=str(item.get("type") or defaults.get(fid,{}).get("type") or "text").lower()
        if typ not in {"text","tel","number","date","textarea","select","checkbox"}: typ="text"
        system=bool(defaults.get(fid,{}).get("system"))
        entry={"id":fid,"type":typ,"label":str(item.get("label") or defaults.get(fid,{}).get("label") or "Feld")[:80],"enabled":bool(item.get("enabled",True)),"required":bool(item.get("required",False)),"system":system,"order":int(item.get("order",(idx+1)*10) or (idx+1)*10),"help":str(item.get("help") or "")[:240]}
        if typ=="select": entry["options"]=[str(x).strip()[:80] for x in (item.get("options") or []) if str(x).strip()][:30]
        if system: entry["enabled"]=True; entry["required"]=True
        clean.append(entry); seen.add(fid)
    for fid,base in defaults.items():
        if fid not in seen: clean.append(dict(base))
    clean.sort(key=lambda x:(int(x.get("order",100)),str(x.get("label","")).casefold()))
    with _lock,_connect() as conn:
        now=utc_now()
        vals={
            "registration_enabled":"1" if enabled else "0",
            "registration_budget_mode":"fixed",
            "registration_reference_hours":"",
            "registration_reference_kwh":str(ref_kwh),
            "registration_limit_mode":limit_mode,
            "registration_form_fields":json.dumps(clean,ensure_ascii=False,separators=(",",":")),
        }
        for key,value in vals.items():
            conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,value,now))
        conn.execute("UPDATE users SET weekly_hours=NULL,budget_source='manual' WHERE weekly_hours IS NOT NULL OR budget_source<>'manual'")
        conn.commit()
    result=registration_settings()
    result["recalculated_users"]=0
    result["recalculated_user_ids"]=[]
    result["skipped_auto_users"]=0
    result["skipped_auto_user_ids"]=[]
    return result

# V0.9.7.20 - public access requests and self-service RFID enrollment

def access_request_start_allowed(email_hash, ip_hash, window_minutes=60, ip_limit=5):
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=max(1,int(window_minutes)))).isoformat()
    with _connect() as conn:
        by_email=int(conn.execute("SELECT COUNT(*) FROM access_request_verifications WHERE email_hash=? AND created_at>=?",(str(email_hash),cutoff)).fetchone()[0] or 0)
        by_ip=int(conn.execute("SELECT COUNT(*) FROM access_request_verifications WHERE ip_hash=? AND created_at>=?",(str(ip_hash),cutoff)).fetchone()[0] or 0)
        return by_email < 1 and by_ip < max(1,int(ip_limit))


def create_access_request_verification(email, email_hash, ip_hash, token_hash, expires_at):
    now=utc_now()
    with _lock,_connect() as conn:
        conn.execute("DELETE FROM access_request_verifications WHERE expires_at<?",((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),))
        conn.execute("UPDATE access_request_verifications SET used_at=? WHERE email_hash=? AND used_at IS NULL",(now,str(email_hash)))
        conn.execute("INSERT INTO access_request_verifications(token_hash,email,email_hash,ip_hash,created_at,expires_at) VALUES(?,?,?,?,?,?)",(str(token_hash),str(email).strip(),str(email_hash),str(ip_hash),now,str(expires_at)))
        conn.commit(); return True


def access_request_verification(token_hash):
    now=utc_now()
    with _connect() as conn:
        row=conn.execute("SELECT * FROM access_request_verifications WHERE token_hash=? AND used_at IS NULL AND expires_at>?",(str(token_hash),now)).fetchone()
        return dict(row) if row else None


def create_access_request(token_hash, *, name, street="", postal_code="", city="", phone="", vehicle_make_model="", vehicle_plate="", weekly_hours=None, field_values_json=None, field_schema_json=None, terms_version, terms_snapshot, signature_path, ip_hash):
    now=utc_now()
    hours=None  # Community does not collect or evaluate employment hours.
    normalized_plate=normalize_vehicle_plate(vehicle_plate)
    try:
        values=json.loads(str(field_values_json or '{}'))
        if isinstance(values,dict) and 'vehicle_plate' in values:
            values['vehicle_plate']=normalized_plate
            field_values_json=json.dumps(values,ensure_ascii=False)
    except Exception:
        pass
    with _lock,_connect() as conn:
        verify=conn.execute("SELECT * FROM access_request_verifications WHERE token_hash=? AND used_at IS NULL AND expires_at>?",(str(token_hash),now)).fetchone()
        if not verify: raise ValueError("Der Bestätigungslink ist ungültig oder abgelaufen.")
        email=str(verify['email']).strip()
        existing=conn.execute("SELECT id FROM access_requests WHERE LOWER(email)=LOWER(?) AND status IN ('Neu','In Prüfung')",(email,)).fetchone()
        if existing: raise ValueError("Für diese E-Mail-Adresse besteht bereits ein offener Antrag.")
        cur=conn.execute("""INSERT INTO access_requests(created_at,updated_at,status,name,street,postal_code,city,email,phone,vehicle_make_model,vehicle_plate,weekly_hours,field_values_json,field_schema_json,terms_version,terms_snapshot,signature_path,signed_at,verified_at,ip_hash)
            VALUES(?,?,'Neu',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(now,now,str(name).strip(),str(street or '').strip(),str(postal_code or '').strip(),str(city or '').strip(),email,str(phone or '').strip(),str(vehicle_make_model or '').strip() or None,normalized_plate,hours,str(field_values_json or '{}'),str(field_schema_json or '[]'),str(terms_version),str(terms_snapshot),str(signature_path),now,now,str(ip_hash)))
        request_id=int(cur.lastrowid)
        conn.execute("UPDATE access_request_verifications SET used_at=? WHERE token_hash=?",(now,str(token_hash)))
        conn.commit(); return request_id


def list_access_requests(status=None, limit=200):
    with _connect() as conn:
        params=[]; where=""
        if status and str(status) != 'Alle': where=" WHERE a.status=?"; params.append(str(status))
        params.append(max(1,min(int(limit or 200),500)))
        rows=conn.execute("""SELECT a.*,su.display_name AS decided_by_name,u.name AS created_user_name
            FROM access_requests a LEFT JOIN system_users su ON su.id=a.decided_by LEFT JOIN users u ON u.id=a.user_id"""+where+" ORDER BY CASE a.status WHEN 'Neu' THEN 0 WHEN 'In Prüfung' THEN 1 ELSE 2 END,a.created_at DESC LIMIT ?",params).fetchall()
        return [dict(r) for r in rows]


def get_access_request(request_id):
    with _connect() as conn:
        row=conn.execute("""SELECT a.*,su.display_name AS decided_by_name,u.name AS created_user_name
            FROM access_requests a LEFT JOIN system_users su ON su.id=a.decided_by LEFT JOIN users u ON u.id=a.user_id WHERE a.id=?""",(int(request_id),)).fetchone()
        return dict(row) if row else None


def access_request_view(request_id):
    item=get_access_request(request_id)
    if not item:
        return None
    try:
        item["terms"]=json.loads(item.get("terms_snapshot") or "[]")
    except Exception:
        item["terms"]=[]
    try:
        item["form_fields"]=json.loads(item.get("field_schema_json") or "[]")
    except Exception:
        item["form_fields"]=[]
    try:
        item["form_values"]=json.loads(item.get("field_values_json") or "{}")
    except Exception:
        item["form_values"]={}
    item["signature_url"]=""
    return item


def delete_access_request(request_id):
    """Delete one access-request record while leaving any approved user intact."""
    with _lock,_connect() as conn:
        row=conn.execute("SELECT id,name,status,signature_path,user_id FROM access_requests WHERE id=?",(int(request_id),)).fetchone()
        if not row:
            return None
        item=dict(row)
        conn.execute("DELETE FROM access_requests WHERE id=?",(int(request_id),))
        conn.execute("UPDATE notifications SET active=0 WHERE notification_key IN (?,?)",
                     (f"access-request:{int(request_id)}",f"access-approved:{int(request_id)}"))
        conn.commit()
        return item


def set_access_request_in_review(request_id):
    with _lock,_connect() as conn:
        cur=conn.execute("UPDATE access_requests SET status='In Prüfung',updated_at=? WHERE id=? AND status='Neu'",(utc_now(),int(request_id)))
        conn.commit(); return cur.rowcount>0


def approve_access_request(request_id, system_user_id, portal_pin_hash, monthly_kwh_limit=None, monthly_limit_mode="warn", admin_note=None, budget_source="manual", return_details=False):
    now=utc_now(); mode="block" if str(monthly_limit_mode).lower()=="block" else "warn"
    source="manual"
    with _lock,_connect() as conn:
        req=conn.execute("SELECT * FROM access_requests WHERE id=?",(int(request_id),)).fetchone()
        if not req: raise ValueError("Zugangsantrag nicht gefunden.")
        if req['status'] in ('Genehmigt','Abgelehnt'): raise ValueError("Dieser Antrag wurde bereits abgeschlossen.")
        if conn.execute("SELECT 1 FROM users WHERE LOWER(TRIM(COALESCE(email,'')))=LOWER(TRIM(?)) LIMIT 1",(req['email'],)).fetchone():
            raise ValueError("Für diese E-Mail-Adresse existiert bereits ein Ladebenutzer.")
        limit_value=None if monthly_kwh_limit in (None,"") else max(0.0,float(monthly_kwh_limit))
        normalized_plate=normalize_vehicle_plate(req['vehicle_plate'])
        vehicle_text=" · ".join(x for x in [str(req['vehicle_make_model'] or '').strip(),normalized_plate] if x) or None
        # Approvals create only charging users, never portal accounts or XP.
        cur=conn.execute("""INSERT INTO users(name,role,department,rfid,status,vehicle,monthly_kwh_limit,monthly_limit_mode,gamification_enabled,email,phone,portal_enabled,weekly_hours,budget_source)
            VALUES(?,'Fahrer',NULL,NULL,'Aktiv',?,?,?,?,?,?,0,?,?)""",(req['name'],vehicle_text,limit_value,mode,0,req['email'],req['phone'],None,source))
        user_id=int(cur.lastrowid)
        vehicle_id=None; vehicle_created=False
        if normalized_plate:
            plate_key=vehicle_plate_key(normalized_plate)
            existing=None
            for row in conn.execute("SELECT id,plate FROM vehicles WHERE active=1 AND TRIM(COALESCE(plate,''))<>'' ORDER BY id").fetchall():
                if vehicle_plate_key(row['plate'])==plate_key:
                    existing=row; break
            if existing:
                vehicle_id=int(existing['id'])
            else:
                vehicle_name=str(req['vehicle_make_model'] or '').strip() or normalized_plate
                vcur=conn.execute("""INSERT INTO vehicles(name,make,model,plate,battery_kwh,ac_power_kw,dc_power_kw,range_km,drivetrain,assigned_charge_point,driver,active)
                    VALUES(?,NULL,NULL,?,NULL,NULL,NULL,NULL,NULL,NULL,?,1)""",(vehicle_name,normalized_plate,str(req['name'] or '').strip() or None))
                vehicle_id=int(vcur.lastrowid); vehicle_created=True
            conn.execute("UPDATE user_vehicles SET primary_vehicle=0 WHERE user_id=?",(user_id,))
            conn.execute("INSERT OR IGNORE INTO user_vehicles(user_id,vehicle_id,primary_vehicle,assigned_at) VALUES(?,?,1,?)",(user_id,vehicle_id,now))
        conn.execute("UPDATE access_requests SET status='Genehmigt',updated_at=?,decision_at=?,decided_by=?,admin_note=?,user_id=?,approved_budget_kwh=?,budget_source=?,vehicle_plate=? WHERE id=?",(now,now,int(system_user_id) if system_user_id else None,str(admin_note or '').strip() or None,user_id,limit_value,source,normalized_plate,int(request_id)))
        conn.commit()
        result={"user_id":user_id,"vehicle_id":vehicle_id,"vehicle_created":vehicle_created,"vehicle_plate":normalized_plate}
        return result if return_details else user_id


def decide_access_request(request_id, status, system_user_id, admin_note=None, user_id=None):
    if status not in {'Genehmigt','Abgelehnt'}: raise ValueError('Ungültiger Antragsstatus.')
    now=utc_now()
    with _lock,_connect() as conn:
        row=conn.execute("SELECT status FROM access_requests WHERE id=?",(int(request_id),)).fetchone()
        if not row: return False
        if row[0] in ('Genehmigt','Abgelehnt'): raise ValueError('Dieser Antrag wurde bereits abgeschlossen.')
        conn.execute("UPDATE access_requests SET status=?,updated_at=?,decision_at=?,decided_by=?,admin_note=?,user_id=? WHERE id=?",(status,now,now,int(system_user_id) if system_user_id else None,str(admin_note or '').strip() or None,user_id,int(request_id)))
        conn.commit(); return True


def access_request_counts():
    with _connect() as conn:
        rows=conn.execute("SELECT status,COUNT(*) n FROM access_requests GROUP BY status").fetchall()
        d={r['status']:int(r['n']) for r in rows}
        return {'new':d.get('Neu',0),'review':d.get('In Prüfung',0),'approved':d.get('Genehmigt',0),'rejected':d.get('Abgelehnt',0),'total':sum(d.values())}


def rfid_enrollment_charge_points():
    now=(datetime.now(timezone.utc)-timedelta(days=3650)).isoformat()
    with _connect() as conn:
        rows=conn.execute("""SELECT cp.id,cp.location,cp.vendor,cp.model,cp.status,cp.last_seen,COALESCE(cp.rfid_self_enroll_mode,'auto') AS rfid_self_enroll_mode,
            EXISTS(SELECT 1 FROM events e WHERE e.charge_point_id=cp.id AND e.event_type IN ('Authorize','StartTransaction') AND e.ts>=? AND (e.event_type='Authorize' OR e.payload LIKE '%id_tag=%')) AS authorize_seen
            FROM charge_points cp WHERE COALESCE(cp.onboarded,1)=1 AND COALESCE(cp.ignored,0)=0 AND COALESCE(cp.retired,0)=0 AND COALESCE(cp.archived,0)=0
            ORDER BY COALESCE(NULLIF(TRIM(cp.location),''),cp.id),cp.id""",(now,)).fetchall()
        return [dict(r) for r in rows]


def active_rfid_count(user_id):
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM rfid_cards WHERE user_id=? AND status='Aktiv'",(int(user_id),)).fetchone()[0] or 0)

def user_has_active_rfid(user_id):
    return active_rfid_count(user_id)>0


def start_rfid_enrollment(user_id, charge_point_id, seconds=60):
    now=datetime.now(timezone.utc); expires=now+timedelta(seconds=max(30,min(int(seconds),120)))
    with _lock,_connect() as conn:
        user=conn.execute("SELECT id,status,portal_enabled FROM users WHERE id=?",(int(user_id),)).fetchone()
        if not user or user['status']!='Aktiv' or not int(user['portal_enabled'] or 0): raise ValueError('Der Ladebenutzer ist nicht für den Self-Service freigeschaltet.')
        if int(conn.execute("SELECT COUNT(*) FROM rfid_cards WHERE user_id=? AND status='Aktiv'",(int(user_id),)).fetchone()[0] or 0) >= 2: raise ValueError('Im Self-Service können maximal zwei aktive Chips genutzt werden. Für weitere RFIDs wenden Sie sich bitte an die Administration.')
        cp=conn.execute("SELECT id,COALESCE(rfid_self_enroll_mode,'auto') mode FROM charge_points WHERE id=? AND COALESCE(onboarded,1)=1 AND COALESCE(ignored,0)=0 AND COALESCE(retired,0)=0 AND COALESCE(archived,0)=0",(str(charge_point_id),)).fetchone()
        if not cp: raise ValueError('Ladesäule nicht gefunden oder nicht verfügbar.')
        if cp['mode']=='disabled': raise ValueError('Automatisches Chip-Anlernen ist an dieser Ladesäule deaktiviert. Bitte wenden Sie sich an die Administration.')
        conn.execute("UPDATE rfid_enrollment_sessions SET status='expired',completed_at=?,message='Zeitfenster abgelaufen' WHERE status='active' AND expires_at<=?",(now.isoformat(),now.isoformat()))
        busy=conn.execute("SELECT id FROM rfid_enrollment_sessions WHERE charge_point_id=? AND status='active' AND expires_at>?",(str(charge_point_id),now.isoformat())).fetchone()
        if busy: raise ValueError('An dieser Ladesäule läuft bereits ein Einlernvorgang. Bitte versuchen Sie es in einer Minute erneut.')
        conn.execute("UPDATE rfid_enrollment_sessions SET status='cancelled',completed_at=?,message='Neuer Einlernvorgang gestartet' WHERE user_id=? AND status IN ('active','candidate','conflict')",(now.isoformat(),int(user_id)))
        cur=conn.execute("INSERT INTO rfid_enrollment_sessions(user_id,charge_point_id,created_at,expires_at,status) VALUES(?,?,?,?, 'active')",(int(user_id),str(charge_point_id),now.isoformat(),expires.isoformat()))
        conn.commit(); return int(cur.lastrowid)


def capture_rfid_enrollment_candidate(charge_point_id, uid):
    now=datetime.now(timezone.utc); uid=str(uid or '').strip()
    if not uid: return None
    with _lock,_connect() as conn:
        conn.execute("UPDATE rfid_enrollment_sessions SET status='expired',completed_at=?,message='Zeitfenster abgelaufen' WHERE status='active' AND expires_at<=?",(now.isoformat(),now.isoformat()))
        row=conn.execute("SELECT * FROM rfid_enrollment_sessions WHERE charge_point_id=? AND status='active' AND expires_at>? ORDER BY id DESC LIMIT 1",(str(charge_point_id),now.isoformat())).fetchone()
        if not row:
            conn.commit(); return None
        existing=conn.execute("SELECT r.id,r.user_id,u.name FROM rfid_cards r LEFT JOIN users u ON u.id=r.user_id WHERE r.uid=?",(uid,)).fetchone()
        if existing:
            conn.execute("UPDATE rfid_enrollment_sessions SET status='conflict',candidate_uid=NULL,detected_at=?,completed_at=?,message='Der erkannte Chip ist bereits einem Benutzer zugeordnet.' WHERE id=?",(now.isoformat(),now.isoformat(),int(row['id'])))
            conn.commit(); return {'captured':True,'session_id':int(row['id']),'status':'conflict'}
        conn.execute("UPDATE rfid_enrollment_sessions SET status='candidate',candidate_uid=?,detected_at=?,message='Chip erkannt - Bestätigung im Portal erforderlich' WHERE id=?",(uid,now.isoformat(),int(row['id'])))
        conn.commit(); return {'captured':True,'session_id':int(row['id']),'status':'candidate'}


def rfid_enrollment_status(user_id, session_id=None):
    now=datetime.now(timezone.utc)
    with _lock,_connect() as conn:
        conn.execute("UPDATE rfid_enrollment_sessions SET status='expired',completed_at=?,message='Innerhalb von 60 Sekunden wurde kein Chip erkannt.' WHERE user_id=? AND status='active' AND expires_at<=?",(now.isoformat(),int(user_id),now.isoformat()))
        if session_id:
            row=conn.execute("SELECT * FROM rfid_enrollment_sessions WHERE id=? AND user_id=?",(int(session_id),int(user_id))).fetchone()
        else:
            row=conn.execute("SELECT * FROM rfid_enrollment_sessions WHERE user_id=? ORDER BY id DESC LIMIT 1",(int(user_id),)).fetchone()
        conn.commit()
        if not row: return None
        d=dict(row); d['candidate_uid_masked']=('••••'+str(d.get('candidate_uid') or '')[-4:]) if d.get('candidate_uid') else None
        return d


def confirm_rfid_enrollment(user_id, session_id, accepted):
    now=utc_now()
    with _lock,_connect() as conn:
        row=conn.execute("SELECT * FROM rfid_enrollment_sessions WHERE id=? AND user_id=?",(int(session_id),int(user_id))).fetchone()
        if not row: raise ValueError('Einlernvorgang nicht gefunden.')
        if row['status']!='candidate' or not row['candidate_uid']: raise ValueError('Es liegt kein bestätigbarer Chip vor.')
        if not accepted:
            conn.execute("UPDATE rfid_enrollment_sessions SET status='cancelled',completed_at=?,message='Benutzer hat den erkannten Chip verworfen.' WHERE id=?",(now,int(session_id)))
            conn.commit(); return {'ok':True,'accepted':False}
        if conn.execute("SELECT 1 FROM rfid_cards WHERE uid=?",(row['candidate_uid'],)).fetchone():
            conn.execute("UPDATE rfid_enrollment_sessions SET status='conflict',completed_at=?,message='Chip wurde zwischenzeitlich bereits zugeordnet.' WHERE id=?",(now,int(session_id)))
            conn.commit(); raise ValueError('Dieser Chip wurde inzwischen bereits zugeordnet. Bitte wiederholen Sie den Vorgang.')
        existing_active=int(conn.execute("SELECT COUNT(*) FROM rfid_cards WHERE user_id=? AND status='Aktiv'",(int(user_id),)).fetchone()[0] or 0)
        if existing_active >= 2: raise ValueError('Im Self-Service können maximal zwei aktive Chips genutzt werden. Für weitere RFIDs wenden Sie sich bitte an die Administration.')
        uid=str(row['candidate_uid']); created=utc_now(); label='Persönlicher Chip' if existing_active==0 else 'Persönlicher Chip 2'
        cur=conn.execute("""INSERT INTO rfid_cards(uid,label,user_id,vehicle_id,status,created_at,issued_at,expires_at,blocked_at,blocked_reason,replacement_for_id,notes)
            VALUES(?,?,?,NULL,'Aktiv',?,?,NULL,NULL,NULL,NULL,?)""",(uid,label,int(user_id),created,created,'Per Self-Service an Ladesäule angelernt'))
        card_id=int(cur.lastrowid)
        _rfid_log_event_conn(conn,card_id,'created',None,'Aktiv','RFID-Chip per Self-Service angelernt','portal')
        _rfid_local_list_bump_conn(conn,[uid])
        conn.execute("UPDATE rfid_enrollment_sessions SET status='confirmed',completed_at=?,message='Chip erfolgreich zugeordnet.' WHERE id=?",(now,int(session_id)))
        conn.commit(); return {'ok':True,'accepted':True,'card_id':card_id,'uid':uid,'charge_point_id':row['charge_point_id']}


def cancel_rfid_enrollment(user_id, session_id):
    with _lock,_connect() as conn:
        cur=conn.execute("UPDATE rfid_enrollment_sessions SET status='cancelled',completed_at=?,message='Einlernvorgang abgebrochen.' WHERE id=? AND user_id=? AND status IN ('active','candidate','conflict')",(utc_now(),int(session_id),int(user_id)))
        conn.commit(); return cur.rowcount>0

def seed_default_achievements():
    """Create the editable built-in achievement library exactly once per seed key."""
    leaderboard_seed_keys={
        "energy-100","sessions-10","early-5","evening-10","night-5","weekend-10",
        "active-days-30","months-3","week-streak-4","cp-2","vehicles-2","single-30",
        "peak-11","max-month-energy-500","max-month-sessions-40","charging-hours-100",
        "long-10","quick-20","unplug-fast-1","unplug-fast-10","unplug-fast-50",
    }
    defaults=[
        # Einstieg
        {"key":"first-kwh","name":"Erste Kilowattstunde","description":"Die erste Kilowattstunde wurde geladen.","icon":"⚡","metric":"energy_total","threshold":1,"category":"Einstieg","rarity":"common","xp":25},
        {"key":"first-session","name":"Angesteckt!","description":"Den ersten Ladevorgang gestartet.","icon":"🔌","metric":"sessions_total","threshold":1,"category":"Einstieg","rarity":"common","xp":25},

        # Energie · mehrstufig
        {"key":"energy-100","name":"Kilowattjäger","description":"100 kWh Gesamtenergie erreicht.","icon":"🏆","metric":"energy_total","threshold":100,"category":"Energie","rarity":"common","xp":75,"tier_group":"energy-total","tier_name":"Bronze","tier_rank":1},
        {"key":"energy-500","name":"Stromsammler","description":"500 kWh Gesamtenergie erreicht.","icon":"⚡","metric":"energy_total","threshold":500,"category":"Energie","rarity":"uncommon","xp":150,"tier_group":"energy-total","tier_name":"Silber","tier_rank":2},
        {"key":"energy-1000","name":"Megawatt-Club","description":"1.000 kWh Gesamtenergie erreicht.","icon":"💎","metric":"energy_total","threshold":1000,"category":"Energie","rarity":"rare","xp":300,"tier_group":"energy-total","tier_name":"Gold","tier_rank":3},
        {"key":"energy-5000","name":"High Voltage","description":"5.000 kWh Gesamtenergie erreicht.","icon":"⚡","metric":"energy_total","threshold":5000,"category":"Energie","rarity":"epic","xp":650,"tier_group":"energy-total","tier_name":"Platin","tier_rank":4},
        {"key":"energy-10000","name":"Lord of the kWh","description":"10.000 kWh Gesamtenergie erreicht.","icon":"👑","metric":"energy_total","threshold":10000,"category":"Energie","rarity":"legendary","xp":1200,"tier_group":"energy-total","tier_name":"Diamant","tier_rank":5},

        # Sessions · mehrstufig
        {"key":"sessions-10","name":"Stammgast","description":"10 Ladevorgänge erreicht.","icon":"🔌","metric":"sessions_total","threshold":10,"category":"Sessions","rarity":"common","xp":60,"tier_group":"sessions-total","tier_name":"Bronze","tier_rank":1},
        {"key":"sessions-25","name":"Gewohnheitstier","description":"25 Ladevorgänge erreicht.","icon":"🔁","metric":"sessions_total","threshold":25,"category":"Sessions","rarity":"uncommon","xp":100,"tier_group":"sessions-total","tier_name":"Silber","tier_rank":2},
        {"key":"sessions-50","name":"Dauerstecker","description":"50 Ladevorgänge erreicht.","icon":"🔁","metric":"sessions_total","threshold":50,"category":"Sessions","rarity":"rare","xp":200,"tier_group":"sessions-total","tier_name":"Gold","tier_rank":3},
        {"key":"sessions-100","name":"Ladeprofi","description":"100 Ladevorgänge erreicht.","icon":"🏅","metric":"sessions_total","threshold":100,"category":"Sessions","rarity":"epic","xp":400,"tier_group":"sessions-total","tier_name":"Platin","tier_rank":4},
        {"key":"sessions-250","name":"Voltage Veteran","description":"250 Ladevorgänge erreicht.","icon":"🧙","metric":"sessions_total","threshold":250,"category":"Sessions","rarity":"legendary","xp":800,"tier_group":"sessions-total","tier_name":"Diamant","tier_rank":5},
        {"key":"sessions-500","name":"Ladelegende","description":"500 Ladevorgänge erreicht.","icon":"👑","metric":"sessions_total","threshold":500,"category":"Sessions","rarity":"legendary","xp":1400,"tier_group":"sessions-total","tier_name":"Mythisch","tier_rank":6},

        # Monat / Jahr
        {"key":"month-energy-50","name":"Monatsstromer","description":"50 kWh in einem laufenden Kalendermonat erreicht.","icon":"📅","metric":"energy_month","threshold":50,"category":"Monat & Jahr","rarity":"common","xp":50},
        {"key":"month-energy-150","name":"Monatsmaschine","description":"150 kWh in einem laufenden Kalendermonat erreicht.","icon":"📈","metric":"energy_month","threshold":150,"category":"Monat & Jahr","rarity":"uncommon","xp":100},
        {"key":"month-energy-300","name":"Monatsmonster","description":"300 kWh in einem laufenden Kalendermonat erreicht.","icon":"🔥","metric":"energy_month","threshold":300,"category":"Monat & Jahr","rarity":"rare","xp":200},
        {"key":"month-sessions-10","name":"Zehnerkarte","description":"10 Ladevorgänge im laufenden Monat.","icon":"🎟️","metric":"sessions_month","threshold":10,"category":"Monat & Jahr","rarity":"common","xp":70},
        {"key":"month-sessions-25","name":"Monatsabo","description":"25 Ladevorgänge im laufenden Monat.","icon":"🗓️","metric":"sessions_month","threshold":25,"category":"Monat & Jahr","rarity":"rare","xp":180},
        {"key":"year-energy-1000","name":"Jahreskilowatt","description":"1.000 kWh im laufenden Kalenderjahr.","icon":"🎆","metric":"energy_year","threshold":1000,"category":"Monat & Jahr","rarity":"rare","xp":250},
        {"key":"year-sessions-100","name":"Hundert im Jahr","description":"100 Ladevorgänge im laufenden Kalenderjahr.","icon":"💯","metric":"sessions_year","threshold":100,"category":"Monat & Jahr","rarity":"epic","xp":450},

        # Zeit & Gewohnheiten
        {"key":"early-5","name":"Early Bird","description":"5 Ladevorgänge zwischen 05:00 und 07:59 Uhr.","icon":"🌅","metric":"early_sessions","threshold":5,"category":"Ladezeiten","rarity":"common","xp":75},
        {"key":"early-25","name":"Sonnenaufgangs-Profi","description":"25 frühe Ladevorgänge.","icon":"☀️","metric":"early_sessions","threshold":25,"category":"Ladezeiten","rarity":"rare","xp":220},
        {"key":"evening-10","name":"Feierabend-Lader","description":"10 Ladevorgänge zwischen 17:00 und 21:59 Uhr.","icon":"🌇","metric":"evening_sessions","threshold":10,"category":"Ladezeiten","rarity":"common","xp":80},
        {"key":"evening-50","name":"After-Work-Ampere","description":"50 Ladevorgänge am Abend.","icon":"🌆","metric":"evening_sessions","threshold":50,"category":"Ladezeiten","rarity":"rare","xp":250},
        {"key":"night-5","name":"Night Owl","description":"5 Ladevorgänge zwischen 22:00 und 04:59 Uhr.","icon":"🌙","metric":"night_sessions","threshold":5,"category":"Ladezeiten","rarity":"uncommon","xp":120},
        {"key":"night-25","name":"Mitternachtsstromer","description":"25 nächtliche Ladevorgänge.","icon":"🦉","metric":"night_sessions","threshold":25,"category":"Ladezeiten","rarity":"epic","xp":350},
        {"key":"weekend-10","name":"Wochenend-Lader","description":"10 Ladevorgänge an Samstagen oder Sonntagen.","icon":"🏖️","metric":"weekend_sessions","threshold":10,"category":"Ladezeiten","rarity":"common","xp":80},
        {"key":"weekend-50","name":"Weekend Warrior","description":"50 Wochenend-Ladevorgänge.","icon":"🛡️","metric":"weekend_sessions","threshold":50,"category":"Ladezeiten","rarity":"rare","xp":260},

        # Treue / Serien
        {"key":"active-days-30","name":"30 Tage unter Strom","description":"An 30 unterschiedlichen Tagen geladen.","icon":"📆","metric":"days_active","threshold":30,"category":"Treue & Serien","rarity":"uncommon","xp":150,"tier_group":"active-days","tier_name":"Bronze","tier_rank":1},
        {"key":"active-days-100","name":"Hundert Tage Hochspannung","description":"An 100 unterschiedlichen Tagen geladen.","icon":"⚡","metric":"days_active","threshold":100,"category":"Treue & Serien","rarity":"rare","xp":350,"tier_group":"active-days","tier_name":"Silber","tier_rank":2},
        {"key":"active-days-365","name":"365 Days of Charge","description":"An 365 unterschiedlichen Tagen geladen.","icon":"🗓️","metric":"days_active","threshold":365,"category":"Treue & Serien","rarity":"legendary","xp":1200,"tier_group":"active-days","tier_name":"Gold","tier_rank":3},
        {"key":"months-3","name":"Dreimonats-Abo","description":"In 3 unterschiedlichen Kalendermonaten geladen.","icon":"📅","metric":"months_active","threshold":3,"category":"Treue & Serien","rarity":"common","xp":80},
        {"key":"months-12","name":"Ganzjahresfahrer","description":"In 12 unterschiedlichen Kalendermonaten geladen.","icon":"🗓️","metric":"months_active","threshold":12,"category":"Treue & Serien","rarity":"epic","xp":450},
        {"key":"week-streak-4","name":"Vier-Wochen-Streak","description":"In 4 aufeinanderfolgenden ISO-Wochen geladen.","icon":"🔥","metric":"week_streak","threshold":4,"category":"Treue & Serien","rarity":"uncommon","xp":150,"tier_group":"week-streak","tier_name":"Bronze","tier_rank":1},
        {"key":"week-streak-12","name":"Streak Machine","description":"In 12 aufeinanderfolgenden ISO-Wochen geladen.","icon":"🔥","metric":"week_streak","threshold":12,"category":"Treue & Serien","rarity":"rare","xp":350,"tier_group":"week-streak","tier_name":"Silber","tier_rank":2},
        {"key":"week-streak-26","name":"Halbes Jahr unter Strom","description":"In 26 aufeinanderfolgenden ISO-Wochen geladen.","icon":"⚡","metric":"week_streak","threshold":26,"category":"Treue & Serien","rarity":"epic","xp":700,"tier_group":"week-streak","tier_name":"Gold","tier_rank":3},
        {"key":"week-streak-52","name":"52-Wochen-Legende","description":"In 52 aufeinanderfolgenden ISO-Wochen geladen.","icon":"👑","metric":"week_streak","threshold":52,"category":"Treue & Serien","rarity":"legendary","xp":1600,"tier_group":"week-streak","tier_name":"Platin","tier_rank":4},

        # Entdecker
        {"key":"cp-2","name":"Stecker-Hopper","description":"An 2 unterschiedlichen Ladepunkten geladen.","icon":"🧭","metric":"distinct_charge_points","threshold":2,"category":"Entdecker","rarity":"common","xp":70},
        {"key":"cp-5","name":"Ladepunkt-Sammler","description":"An 5 unterschiedlichen Ladepunkten geladen.","icon":"🗺️","metric":"distinct_charge_points","threshold":5,"category":"Entdecker","rarity":"rare","xp":220},
        {"key":"vehicles-2","name":"Flottenhopper","description":"Mit 2 unterschiedlichen Fahrzeugen geladen.","icon":"🚗","metric":"distinct_vehicles","threshold":2,"category":"Entdecker","rarity":"uncommon","xp":110},
        {"key":"vehicles-5","name":"Garage voller Volt","description":"Mit 5 unterschiedlichen Fahrzeugen geladen.","icon":"🚙","metric":"distinct_vehicles","threshold":5,"category":"Entdecker","rarity":"epic","xp":400},

        # Rekorde / Statistik
        {"key":"single-30","name":"Big Gulp","description":"Mindestens 30 kWh in einer einzelnen Session geladen.","icon":"🥤","metric":"max_session_energy","threshold":30,"category":"Rekorde","rarity":"common","xp":90},
        {"key":"single-50","name":"50-kWh-Keule","description":"Mindestens 50 kWh in einer einzelnen Session geladen.","icon":"🔋","metric":"max_session_energy","threshold":50,"category":"Rekorde","rarity":"rare","xp":250},
        {"key":"single-75","name":"Akku-Monster","description":"Mindestens 75 kWh in einer einzelnen Session geladen.","icon":"🦖","metric":"max_session_energy","threshold":75,"category":"Rekorde","rarity":"epic","xp":500},
        {"key":"peak-11","name":"Elf Freunde müsst ihr sein","description":"Mindestens 11 kW maximale Sessionleistung erreicht.","icon":"⚡","metric":"max_session_power","threshold":11,"category":"Rekorde","rarity":"common","xp":70},
        {"key":"peak-22","name":"Doppelte Dosis","description":"Mindestens 22 kW maximale Sessionleistung erreicht.","icon":"⚡","metric":"max_session_power","threshold":22,"category":"Rekorde","rarity":"rare","xp":200},
        {"key":"max-month-energy-500","name":"Monatsgigant","description":"In einem Kalendermonat mindestens 500 kWh geladen.","icon":"📊","metric":"max_month_energy","threshold":500,"category":"Rekorde","rarity":"epic","xp":550},
        {"key":"max-month-sessions-40","name":"Session-Sammler","description":"In einem Kalendermonat mindestens 40 Sessions erreicht.","icon":"🧮","metric":"max_month_sessions","threshold":40,"category":"Rekorde","rarity":"epic","xp":500},
        {"key":"charging-hours-100","name":"100 Stunden Ampere","description":"100 Stunden kumulierte Ladezeit erreicht.","icon":"⏱️","metric":"charging_hours_total","threshold":100,"category":"Rekorde","rarity":"rare","xp":250},
        {"key":"long-10","name":"Langzeitparker","description":"10 Sessions mit mindestens vier Stunden Verbindung.","icon":"🅿️","metric":"long_sessions","threshold":10,"category":"Ladeverhalten","rarity":"uncommon","xp":150},
        {"key":"quick-20","name":"Power Nap","description":"20 Sessions mit maximal 45 Minuten Verbindung.","icon":"😴","metric":"quick_sessions","threshold":20,"category":"Ladeverhalten","rarity":"rare","xp":240},
        {"key":"unplug-fast-1","name":"Stecker-Sprinter","description":"Nach einer Session den Connector innerhalb von 10 Minuten wieder freigegeben.","icon":"🏃","metric":"prompt_unplug_sessions","threshold":1,"category":"Ladeverhalten","rarity":"common","xp":50,"tier_group":"prompt-unplug","tier_name":"Bronze","tier_rank":1,"leaderboard_enabled":True},
        {"key":"unplug-fast-10","name":"Stecker-Sprinter Pro","description":"10 Sessions mit Freigabe des Connectors innerhalb von 10 Minuten.","icon":"🏃","metric":"prompt_unplug_sessions","threshold":10,"category":"Ladeverhalten","rarity":"uncommon","xp":150,"tier_group":"prompt-unplug","tier_name":"Silber","tier_rank":2,"leaderboard_enabled":True},
        {"key":"unplug-fast-50","name":"Stecker-Sprinter Elite","description":"50 Sessions mit Freigabe des Connectors innerhalb von 10 Minuten.","icon":"🏁","metric":"prompt_unplug_sessions","threshold":50,"category":"Ladeverhalten","rarity":"rare","xp":350,"tier_group":"prompt-unplug","tier_name":"Gold","tier_rank":3,"leaderboard_enabled":True},

        # Manuelle Community-/Jury-Badges
        {"key":"manual-community-hero","name":"Community Hero","description":"Für besondere Hilfsbereitschaft rund um die Ladeinfrastruktur.","icon":"🤝","metric":"manual","threshold":None,"category":"Community","rarity":"epic","xp":400},
        {"key":"manual-kwh-king","name":"Kilowatt-König","description":"Manuelle Auszeichnung für eine besondere Ladeleistung.","icon":"👑","metric":"manual","threshold":None,"category":"Community","rarity":"epic","xp":400},
        {"key":"manual-comeback","name":"Comeback des Monats","description":"Manuelle Auszeichnung für das stärkste Lade-Comeback.","icon":"🚀","metric":"manual","threshold":None,"category":"Community","rarity":"rare","xp":250},

        # Echte Geheimnisse: vor Freischaltung nirgends im Fahrerprofil sichtbar.
        {"key":"secret-42","name":"Die Antwort auf alles","description":"Eine Session mit ziemlich genau 42,0 kWh beendet.","icon":"🌌","metric":"exact_42_sessions","threshold":1,"category":"Geheim","rarity":"secret","xp":420,"hidden":True,"secret":True},
        {"key":"secret-midnight","name":"Geisterstunde","description":"Mitternacht. Stecker rein. Niemand stellt Fragen.","icon":"👻","metric":"midnight_sessions","threshold":1,"category":"Geheim","rarity":"secret","xp":250,"hidden":True,"secret":True},
        {"key":"secret-1337","name":"LEET Charge","description":"1.337 kWh Gesamtenergie erreicht. 1337 bestätigt.","icon":"🕹️","metric":"energy_total","threshold":1337,"category":"Geheim","rarity":"secret","xp":337,"hidden":True,"secret":True},
        {"key":"secret-goose","name":"Die elektrifizierte Gans","description":"Ein kleiner Gruß aus der Werkstatt.","icon":"🪿","metric":"energy_total","threshold":42,"category":"Geheim","rarity":"secret","xp":142,"hidden":True,"secret":True},
    ]
    legacy_names={
        "first-kwh":"Erste Kilowattstunde",
        "sessions-10":"Stammgast",
        "energy-100":"Kilowattjäger",
        "sessions-50":"Dauerstecker",
        "energy-1000":"Megawatt-Club",
        "month-energy-50":"Monatsstromer",
        "secret-goose":"Die elektrifizierte Gans",
    }
    with _lock,_connect() as conn:
        for item in defaults:
            key=item["key"]
            if conn.execute("SELECT 1 FROM achievements WHERE seed_key=?",(key,)).fetchone():
                continue
            legacy=conn.execute("SELECT id FROM achievements WHERE seed_key IS NULL AND name=?",(legacy_names.get(key,item["name"]),)).fetchone()
            if legacy:
                conn.execute("""UPDATE achievements SET seed_key=?,category=?,rarity=?,xp=?,tier_group=?,tier_name=?,tier_rank=?,
                    system_secret=CASE WHEN ? THEN 1 ELSE system_secret END,
                    leaderboard_enabled=CASE WHEN ? THEN 1 ELSE leaderboard_enabled END
                    WHERE id=?""",(key,item.get("category","Allgemein"),item.get("rarity","common"),int(item.get("xp",50)),
                                  item.get("tier_group"),item.get("tier_name"),int(item.get("tier_rank",0)),
                                  1 if item.get("secret") else 0,1 if key in leaderboard_seed_keys else 0,int(legacy["id"])))
                continue
            conn.execute("""INSERT INTO achievements(
                name,description,icon,metric,threshold,hidden,system_secret,category,rarity,xp,tier_group,tier_name,tier_rank,seed_key,leaderboard_enabled,active,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",(
                item["name"],item.get("description"),item.get("icon","🏅"),item.get("metric","manual"),item.get("threshold"),
                1 if item.get("hidden") else 0,1 if item.get("secret") else 0,item.get("category","Allgemein"),
                item.get("rarity","common"),int(item.get("xp",50)),item.get("tier_group"),item.get("tier_name"),
                int(item.get("tier_rank",0)),key,1 if key in leaderboard_seed_keys and not item.get("secret") else 0,utc_now(),
            ))
        conn.commit()


# V0.9.1 - lade.cloud history import helpers.
def import_history_stats(source="lade.cloud"):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT COUNT(*) AS sessions,COALESCE(SUM(energy_kwh),0) AS energy,MIN(started_at) AS first_at,MAX(started_at) AS last_at FROM transactions WHERE import_source=?",(source,)).fetchone()
        return dict(row) if row else {"sessions":0,"energy":0,"first_at":None,"last_at":None}


def import_ladecloud_rows(rows, user_mapping, charge_point_mapping):
    """Import normalized lade.cloud rows without inventing missing telemetry.

    user_mapping maps source RFID tags to existing backend user IDs.
    charge_point_mapping maps source charge point names to existing OCPP charge point IDs.
    """
    imported=duplicates=0
    imported_energy=0.0
    touched_users=set()
    rfid_created=[]
    with _lock,_connect() as conn:
        # Validate mappings before the first write.
        user_cache={}
        for tag,user_id in user_mapping.items():
            user=conn.execute("SELECT id,name,status FROM users WHERE id=?",(int(user_id),)).fetchone()
            if not user: raise ValueError(f"Zielbenutzer für RFID {tag} wurde nicht gefunden.")
            user_cache[str(tag)]=dict(user)
        for source_cp,target_cp in charge_point_mapping.items():
            if not conn.execute("SELECT id FROM charge_points WHERE id=?",(str(target_cp),)).fetchone():
                raise ValueError(f"Ziel-Ladepunkt für {source_cp} wurde nicht gefunden.")

        # RFID cards are created only when unambiguous. Existing conflicting cards stop the import.
        card_cache={}
        for tag,user in user_cache.items():
            existing=conn.execute("SELECT * FROM rfid_cards WHERE uid=?",(tag,)).fetchone()
            if existing and existing["user_id"] not in (None,int(user["id"])):
                other=conn.execute("SELECT name FROM users WHERE id=?",(existing["user_id"],)).fetchone()
                raise ValueError(f"RFID {tag} ist bereits {other[0] if other else 'einem anderen Benutzer'} zugeordnet.")
            if existing:
                card_id=int(existing["id"])
                if existing["user_id"] is None:
                    conn.execute("UPDATE rfid_cards SET user_id=?,status='Aktiv' WHERE id=?",(int(user["id"]),card_id))
            else:
                cur=conn.execute("INSERT INTO rfid_cards(uid,label,user_id,status,created_at) VALUES(?,?,?,'Aktiv',?)",(tag,"lade.cloud Import",int(user["id"]),utc_now()))
                card_id=int(cur.lastrowid); rfid_created.append(tag)
            card_cache[tag]=card_id

        for item in rows:
            tag=str(item.get("rfid_tag") or "").strip()
            source_cp=str(item.get("source_charge_point") or "").strip()
            if tag not in user_cache: raise ValueError(f"Keine Benutzerzuordnung für RFID {tag}.")
            if source_cp not in charge_point_mapping: raise ValueError(f"Keine Ladepunktzuordnung für {source_cp}.")
            if conn.execute("SELECT id FROM transactions WHERE import_source='lade.cloud' AND import_key=?",(item["import_key"],)).fetchone():
                duplicates += 1; continue
            user=user_cache[tag]
            price=item.get("price_cents_per_kwh")
            # Imported lade.cloud history belongs to the free monthly allowance.
            cost=0
            cur=conn.execute("""INSERT INTO transactions(
                charge_point_id,connector_id,transaction_id,id_tag,user_id,rfid_card_id,started_at,ended_at,
                energy_kwh,max_power_kw,status,stop_reason,charging_seconds,stand_seconds,connection_seconds,
                price_cents_per_kwh,cost_cents,tariff_source,import_source,import_key,imported_at,import_source_name,import_evse_id,timing_quality
            ) VALUES(?,?,?,?,?,?,?,?,?,0,'Completed','Imported',0,0,?,?,?,?,?,?,?,?,?,?)""",
                (str(charge_point_mapping[source_cp]),int(item.get("connector_id") or 1),None,tag,int(user["id"]),card_cache[tag],item["started_at"],item["ended_at"],float(item.get("energy_kwh") or 0),float(item.get("duration_seconds") or 0),price,cost,"lade.cloud" if price is not None else None,"lade.cloud",item["import_key"],utc_now(),item.get("source_user_name"),item.get("evse_id"),"connection_only"))
            imported += 1; imported_energy += float(item.get("energy_kwh") or 0); touched_users.add(int(user["id"]))
        conn.commit()
    for user_id in touched_users:
        evaluate_user_achievements(user_id)
    return {"imported":imported,"duplicates":duplicates,"energy_kwh":round(imported_energy,3),"rfid_created":rfid_created,"users_updated":len(touched_users)}


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


def reporting_bundle(start_at=None, end_at=None, user_id=None, vehicle_id=None, charge_point_id=None, cost_center=None, billing_group_id=None):
    """Return immutable accounting/reporting data from completed transactions.

    Tariff, billing group and cost-center values are intentionally read from the
    transaction snapshot. Later tariff changes therefore do not rewrite old
    accounting periods.
    """
    with _lock, _connect() as conn:
        clauses=["t.ended_at IS NOT NULL", "COALESCE(t.status,'') <> 'Active'"]
        params=[]
        if start_at:
            clauses.append("t.started_at >= ?"); params.append(start_at)
        if end_at:
            clauses.append("t.started_at < ?"); params.append(end_at)
        if user_id is not None:
            clauses.append("t.user_id = ?"); params.append(int(user_id))
        if vehicle_id is not None:
            clauses.append("t.vehicle_id = ?"); params.append(int(vehicle_id))
        if charge_point_id:
            clauses.append("t.charge_point_id = ?"); params.append(str(charge_point_id))
        if cost_center == "__none__":
            clauses.append("COALESCE(TRIM(t.cost_center),'') = ''")
        elif cost_center:
            clauses.append("t.cost_center = ?"); params.append(str(cost_center))
        if billing_group_id == "__none__":
            clauses.append("t.billing_group_id IS NULL")
        elif billing_group_id is not None:
            clauses.append("t.billing_group_id = ?"); params.append(int(billing_group_id))
        where=" AND ".join(clauses)
        rows=[dict(r) for r in conn.execute(f"""
            SELECT t.id,t.started_at,t.ended_at,t.charge_point_id,t.connector_id,t.energy_kwh,
                   t.charging_seconds,t.stand_seconds,t.connection_seconds,t.user_id,t.vehicle_id,
                   t.tariff_name,t.tariff_source,t.price_cents_per_kwh,t.cost_cents,t.cost_center,
                   t.billing_group_id,t.billing_group_name,t.import_source,t.timing_quality,
                   u.name AS user_name,u.department AS user_department,
                   v.name AS vehicle_name,v.plate AS vehicle_plate
              FROM transactions t
              LEFT JOIN users u ON u.id=t.user_id
              LEFT JOIN vehicles v ON v.id=t.vehicle_id
             WHERE {where}
             ORDER BY t.started_at DESC,t.id DESC
        """, params).fetchall()]

        def aggregate(items):
            sessions=len(items)
            energy=round(sum(float(x.get("energy_kwh") or 0) for x in items),3)
            known=[x for x in items if x.get("cost_cents") is not None]
            known_energy=sum(float(x.get("energy_kwh") or 0) for x in known)
            cost_cents=sum(int(x.get("cost_cents") or 0) for x in known)
            charging=sum(float(x.get("charging_seconds") or 0) for x in items)
            connection=sum(float(x.get("connection_seconds") or 0) for x in items)
            stand=sum(float(x.get("stand_seconds") or 0) for x in items)
            return {
                "sessions":sessions,"energy_kwh":round(energy,2),"cost_cents":cost_cents,"cost_eur":round(cost_cents/100.0,2),
                "cost_known_sessions":len(known),"cost_missing_sessions":sessions-len(known),
                "cost_coverage_pct":round(len(known)/sessions*100.0,1) if sessions else 100.0,
                "known_cost_energy_kwh":round(known_energy,2),
                "avg_price_cents_per_kwh":round(cost_cents/known_energy,2) if known_energy>0 else None,
                "avg_energy_kwh":round(energy/sessions,2) if sessions else 0.0,
                "charging_hours":round(charging/3600.0,2),"connection_hours":round(connection/3600.0,2),"stand_hours":round(stand/3600.0,2),
            }

        def group_by(key_fn, label_fn=None):
            groups={}
            for row in rows:
                key=key_fn(row)
                label=label_fn(row) if label_fn else key
                bucket=groups.setdefault(str(key),{"key":key,"label":label,"rows":[]})
                bucket["rows"].append(row)
            result=[]
            for bucket in groups.values():
                item={"key":bucket["key"],"label":bucket["label"]}
                item.update(aggregate(bucket["rows"]))
                result.append(item)
            result.sort(key=lambda x:(-float(x.get("cost_cents") or 0),-float(x.get("energy_kwh") or 0),str(x.get("label") or "").lower()))
            return result

        berlin=ZoneInfo("Europe/Berlin")
        monthly={}
        for row in rows:
            dt=_parse_iso_utc(row.get("started_at"))
            if not dt: continue
            local=dt.astimezone(berlin)
            key=local.strftime("%Y-%m")
            bucket=monthly.setdefault(key,{"key":key,"label":local.strftime("%m/%Y"),"rows":[]})
            bucket["rows"].append(row)
        by_month=[]
        for key in sorted(monthly):
            bucket=monthly[key]; item={"key":key,"label":bucket["label"]}; item.update(aggregate(bucket["rows"])); by_month.append(item)

        summary=aggregate(rows)
        summary["unassigned_user_sessions"]=sum(1 for x in rows if x.get("user_id") is None)
        summary["unassigned_vehicle_sessions"]=sum(1 for x in rows if x.get("vehicle_id") is None)
        summary["unassigned_cost_center_sessions"]=sum(1 for x in rows if not str(x.get("cost_center") or "").strip())

        options={
            "users":[dict(r) for r in conn.execute("SELECT id,name FROM users ORDER BY name COLLATE NOCASE").fetchall()],
            "vehicles":[dict(r) for r in conn.execute("SELECT id,name,plate FROM vehicles WHERE active=1 ORDER BY name COLLATE NOCASE").fetchall()],
            "charge_points":[dict(r) for r in conn.execute("SELECT id FROM charge_points WHERE COALESCE(ignored,0)=0 AND COALESCE(retired,0)=0 AND COALESCE(archived,0)=0 ORDER BY id COLLATE NOCASE").fetchall()],
            "cost_centers":[r[0] for r in conn.execute("SELECT DISTINCT cost_center FROM transactions WHERE COALESCE(TRIM(cost_center),'')<>'' ORDER BY cost_center COLLATE NOCASE").fetchall()],
            "billing_groups":[dict(r) for r in conn.execute("SELECT id,name,cost_center FROM billing_groups WHERE active=1 ORDER BY name COLLATE NOCASE").fetchall()],
        }
        return {
            "summary":summary,
            "by_user":group_by(lambda x:x.get("user_id") if x.get("user_id") is not None else "unassigned", lambda x:x.get("user_name") or "Nicht zugeordnet"),
            "by_vehicle":group_by(lambda x:x.get("vehicle_id") if x.get("vehicle_id") is not None else "unassigned", lambda x:(x.get("vehicle_name") or "Nicht zugeordnet") + ((" · "+x.get("vehicle_plate")) if x.get("vehicle_plate") else "")),
            "by_charge_point":group_by(lambda x:x.get("charge_point_id") or "unassigned", lambda x:x.get("charge_point_id") or "Nicht zugeordnet"),
            "by_cost_center":group_by(lambda x:x.get("cost_center") or "__none__", lambda x:x.get("cost_center") or "Ohne Kostenstelle"),
            "by_billing_group":group_by(lambda x:x.get("billing_group_id") if x.get("billing_group_id") is not None else "unassigned", lambda x:x.get("billing_group_name") or "Ohne Abrechnungsgruppe"),
            "by_month":by_month,
            "transactions":rows,
            "options":options,
        }

# V0.9.7.6 - cost-center master data, global search and Smart Charging state.
def list_cost_centers(include_inactive=True):
    with _lock,_connect() as conn:
        where="" if include_inactive else " WHERE cc.active=1"
        rows=conn.execute(f"""SELECT cc.*,
            (SELECT COUNT(*) FROM tariffs t WHERE t.active=1 AND t.cost_center=cc.code) AS active_tariff_count,
            (SELECT COUNT(*) FROM billing_groups g WHERE g.active=1 AND g.cost_center=cc.code) AS active_group_count,
            (SELECT COUNT(*) FROM transactions tx WHERE tx.cost_center=cc.code) AS historical_session_count
            FROM cost_centers cc{where} ORDER BY cc.active DESC,cc.code COLLATE NOCASE""").fetchall()
        return [dict(r) for r in rows]

def get_cost_center(cost_center_id):
    with _lock,_connect() as conn:
        row=conn.execute("SELECT * FROM cost_centers WHERE id=?",(int(cost_center_id),)).fetchone()
        return dict(row) if row else None

def create_cost_center(code,name,description=None):
    code=str(code or "").strip(); name=str(name or "").strip()
    if not code: raise ValueError("Kostenstellen-Code ist erforderlich")
    if not name: raise ValueError("Bezeichnung ist erforderlich")
    if len(code)>40: raise ValueError("Kostenstellen-Code ist zu lang")
    now=utc_now()
    with _lock,_connect() as conn:
        try:
            cur=conn.execute("INSERT INTO cost_centers(code,name,description,active,created_at,updated_at) VALUES(?,?,?,1,?,?)",(code,name,str(description or "").strip() or None,now,now)); conn.commit(); return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc: raise ValueError("Diese Kostenstelle existiert bereits") from exc

def update_cost_center(cost_center_id,code,name,description=None,active=True):
    code=str(code or "").strip(); name=str(name or "").strip()
    if not code or not name: raise ValueError("Code und Bezeichnung sind erforderlich")
    with _lock,_connect() as conn:
        old=conn.execute("SELECT * FROM cost_centers WHERE id=?",(int(cost_center_id),)).fetchone()
        if not old: return False
        try:
            conn.execute("UPDATE cost_centers SET code=?,name=?,description=?,active=?,updated_at=? WHERE id=?",(code,name,str(description or "").strip() or None,1 if active else 0,utc_now(),int(cost_center_id)))
            # Current configuration follows a renamed master code; historical transaction snapshots stay immutable.
            if str(old['code']).casefold()!=code.casefold():
                conn.execute("UPDATE tariffs SET cost_center=? WHERE cost_center=? AND active=1",(code,old['code']))
                conn.execute("UPDATE billing_groups SET cost_center=? WHERE cost_center=? AND active=1",(code,old['code']))
            conn.commit(); return True
        except sqlite3.IntegrityError as exc: raise ValueError("Diese Kostenstelle existiert bereits") from exc


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
