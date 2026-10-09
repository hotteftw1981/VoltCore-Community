import csv
import hashlib
import io
import json
import secrets
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import db

FORMAT = "voltcore-migration"
SCHEMA_VERSION = 1
NULL_TOKEN = "__VOLTCORE_NULL_V1__"
MAX_PACKAGE_BYTES = 100 * 1024 * 1024

CORE_EMPTY_TABLES = ("charge_points", "transactions", "vehicles", "users", "rfid_cards")

EXPORT_TABLES = [
    "charge_points",
    "connectors",
    "users",
    "vehicles",
    "rfid_cards",
    "user_vehicles",
    "user_charge_point_access",
    "billing_groups",
    "user_billing_groups",
    "tariffs",
    "transactions",
    "meter_samples",
    "events",
    "rfid_card_events",
    "ocpp_capability_snapshots",
    "ocpp_remote_capabilities",
    "ocpp_service_operations",
    "ocpp_reservations",
    "diagnostic_states",
    "diagnostic_events",
    "rfid_local_list_changes",
    "charge_point_local_list_state",
    # Main-edition tables; silently skipped when an edition does not have them.
    "load_rules",
    "cost_centers",
    "smart_charging_connectors",
    "achievements",
    "achievement_awards",
    "gamification_events",
    "gamification_event_rewards",
    "gamification_event_results",
    "bonus_vouchers",
    "bonus_grants",
    "bonus_usage",
    "bonus_voucher_redemptions",
    "bonus_transfers",
]

EXCLUDED_DOMAINS = [
    "system users and sessions",
    "passwords, TOTP and recovery codes",
    "SMTP/update/Portainer credentials",
    "web-push subscriptions",
    "security and login-attempt logs",
    "backup archives",
    "temporary import files",
    "personal portal sessions and reset tokens",
]


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (str(table),),
    ).fetchone()
    return bool(row)


def _table_columns(conn, table):
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _table_pk_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row[1]) for row in sorted(rows, key=lambda r: int(r[5] or 0)) if int(row[5] or 0) > 0]


def _csv_bytes(columns, rows):
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([NULL_TOKEN if row.get(column) is None else row.get(column) for column in columns])
    return out.getvalue().encode("utf-8")


def _read_csv_bytes(data):
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    try:
        columns = [str(x) for x in next(reader)]
    except StopIteration:
        raise ValueError("Leere CSV-Datei im Migrationspaket.")
    if not columns or len(columns) != len(set(columns)):
        raise ValueError("Ungültige oder doppelte CSV-Spalten im Migrationspaket.")
    rows = []
    for values in reader:
        if not values:
            continue
        item = {}
        for index, column in enumerate(columns):
            value = values[index] if index < len(values) else NULL_TOKEN
            item[column] = None if value == NULL_TOKEN else value
        rows.append(item)
    return columns, rows


def _anon_token(prefix, value, fallback_index):
    raw = str(value if value not in (None, "") else fallback_index)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10].upper()
    return f"{prefix}-{digest}"


def _anonymizer():
    return {
        "rfids": {},
        "users": {},
        "vehicles": {},
    }


def _anonymize_row(table, row, state, index):
    item = dict(row)

    if table == "users":
        user_id = item.get("id", index)
        item["name"] = f"User {int(user_id):04d}" if str(user_id).isdigit() else _anon_token("USER", user_id, index)
        for key in ("email", "phone", "department", "image_path"):
            if key in item:
                item[key] = None
        if "rfid" in item and item.get("rfid"):
            original = str(item["rfid"])
            state["rfids"].setdefault(original, _anon_token("ANON-RFID", original, index))
            item["rfid"] = state["rfids"][original]

    elif table == "vehicles":
        vehicle_id = item.get("id", index)
        item["name"] = f"Vehicle {int(vehicle_id):04d}" if str(vehicle_id).isdigit() else _anon_token("VEHICLE", vehicle_id, index)
        for key in ("plate", "driver", "image_path"):
            if key in item:
                item[key] = None

    elif table == "rfid_cards":
        original = str(item.get("uid") or "")
        if original:
            state["rfids"].setdefault(original, _anon_token("ANON-RFID", original, index))
            item["uid"] = state["rfids"][original]
        if "label" in item:
            item["label"] = f"RFID {int(item.get('id') or index):04d}" if str(item.get("id") or index).isdigit() else "RFID"
        for key in ("notes", "blocked_reason"):
            if key in item:
                item[key] = None

    elif table == "transactions":
        original = str(item.get("id_tag") or "")
        if original:
            state["rfids"].setdefault(original, _anon_token("ANON-RFID", original, index))
            item["id_tag"] = state["rfids"][original]
        if "import_source_name" in item:
            item["import_source_name"] = None

    elif table == "rfid_local_list_changes":
        original = str(item.get("uid") or "")
        if original:
            state["rfids"].setdefault(original, _anon_token("ANON-RFID", original, index))
            item["uid"] = state["rfids"][original]

    elif table == "rfid_card_events":
        for key in ("note", "source"):
            if key in item:
                item[key] = None

    elif table == "ocpp_service_operations":
        if "requested_by" in item:
            item["requested_by"] = None
        for key in ("summary", "detail"):
            if key in item:
                item[key] = None

    elif table in {"events", "diagnostic_states", "diagnostic_events"}:
        for key in ("payload", "message", "detail", "title"):
            if key in item:
                item[key] = None

    elif table == "billing_groups":
        if "cost_center" in item:
            item["cost_center"] = None

    elif table == "cost_centers":
        for key in ("name", "description"):
            if key in item:
                item[key] = f"Cost Center {index:04d}" if key == "name" else None

    return item


def _cleanup_packages(max_age_seconds=86400):
    folder = Path(db.DATA_DIR) / "imports"
    if not folder.exists():
        return
    cutoff = time.time() - max(60, int(max_age_seconds))
    for path in folder.glob("voltcore-migration-*.zip"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def save_package_upload(data):
    if not data:
        raise ValueError("Das Migrationspaket ist leer.")
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError("Das Migrationspaket ist größer als 100 MB.")
    _cleanup_packages()
    token = secrets.token_hex(16)
    folder = Path(db.DATA_DIR) / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"voltcore-migration-{token}.zip"
    path.write_bytes(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def _package_path(token):
    raw = str(token or "").strip().lower()
    if len(raw) != 32 or any(ch not in "0123456789abcdef" for ch in raw):
        raise ValueError("Ungültiger Paket-Token.")
    return Path(db.DATA_DIR) / "imports" / f"voltcore-migration-{raw}.zip"


def load_package_upload(token):
    _cleanup_packages()
    path = _package_path(token)
    if not path.exists():
        raise ValueError("Das Migrationspaket ist nicht mehr verfügbar. Bitte erneut hochladen.")
    return path.read_bytes()


def delete_package_upload(token):
    try:
        _package_path(token).unlink(missing_ok=True)
    except OSError:
        pass


def build_package(app_version, edition, anonymize=False):
    state = _anonymizer()
    generated = datetime.now(timezone.utc).isoformat()
    files = {}
    table_meta = []

    with db._lock, db._connect() as conn:
        for table in EXPORT_TABLES:
            if not _table_exists(conn, table):
                continue
            columns = _table_columns(conn, table)
            if not columns:
                continue
            order = ""
            pk = _table_pk_columns(conn, table)
            if pk:
                order = " ORDER BY " + ",".join(pk)
            raw_rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}{order}").fetchall()]
            rows = [
                _anonymize_row(table, row, state, index)
                if anonymize else row
                for index, row in enumerate(raw_rows, start=1)
            ]
            payload = _csv_bytes(columns, rows)
            name = f"data/{table}.csv"
            files[name] = payload
            table_meta.append({
                "table": table,
                "file": name,
                "rows": len(rows),
                "columns": columns,
                "primary_key": pk,
                "sha256": hashlib.sha256(payload).hexdigest(),
            })

    manifest = {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "source_version": str(app_version),
        "source_edition": str(edition),
        "created_at": generated,
        "anonymized": bool(anonymize),
        "anonymization_scope": "personal_data" if anonymize else "none",
        "null_token": NULL_TOKEN,
        "tables": table_meta,
        "excluded": EXCLUDED_DOMAINS,
        "import_mode": "fresh_operational_installation",
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, payload in files.items():
            zf.writestr(name, payload)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    payload = buffer.getvalue()
    suffix = "-anonymized" if anonymize else ""
    version_slug = str(app_version).replace(".", "_")
    filename = f"VoltCore_Migration_V{version_slug}{suffix}.zip"
    return payload, filename, manifest


def _load_package(data):
    if not data:
        raise ValueError("Das Migrationspaket ist leer.")
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError("Das Migrationspaket ist größer als 100 MB.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data), "r")
    except zipfile.BadZipFile as exc:
        raise ValueError("Die Datei ist kein gültiges ZIP-Migrationspaket.") from exc

    with zf:
        names = zf.namelist()
        if "manifest.json" not in names:
            raise ValueError("manifest.json fehlt im Migrationspaket.")
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise ValueError("Unsicherer Dateipfad im Migrationspaket.")
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("manifest.json ist ungültig.") from exc
        if manifest.get("format") != FORMAT:
            raise ValueError("Unbekanntes Migrationsformat.")
        if int(manifest.get("schema_version") or 0) != SCHEMA_VERSION:
            raise ValueError("Diese Migrationsschema-Version wird nicht unterstützt.")
        if manifest.get("null_token") not in (None, NULL_TOKEN):
            raise ValueError("Unbekannte NULL-Kodierung im Migrationspaket.")

        tables = {}
        for meta in manifest.get("tables") or []:
            table = str(meta.get("table") or "").strip()
            filename = str(meta.get("file") or "").strip()
            if not table or table not in EXPORT_TABLES or filename != f"data/{table}.csv":
                raise ValueError("Ungültiger Tabelleneintrag im Migrationsmanifest.")
            if filename not in names:
                raise ValueError(f"{filename} fehlt im Migrationspaket.")
            payload = zf.read(filename)
            expected = str(meta.get("sha256") or "")
            if expected and hashlib.sha256(payload).hexdigest() != expected:
                raise ValueError(f"Prüfsumme für {filename} stimmt nicht.")
            columns, rows = _read_csv_bytes(payload)
            if list(meta.get("columns") or []) != columns:
                raise ValueError(f"Spaltenstruktur von {filename} stimmt nicht mit dem Manifest überein.")
            if int(meta.get("rows") or 0) != len(rows):
                raise ValueError(f"Zeilenanzahl von {filename} stimmt nicht mit dem Manifest überein.")
            tables[table] = {"columns": columns, "rows": rows, "meta": meta}
        return manifest, tables


def target_state():
    with db._lock, db._connect() as conn:
        counts = {}
        for table in CORE_EMPTY_TABLES:
            if _table_exists(conn, table):
                counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
    return counts


def preview_package(data, target_edition):
    manifest, tables = _load_package(data)
    counts = target_state()
    blockers = {table: count for table, count in counts.items() if count > 0}
    supported = {}
    dropped_tables = []
    dropped_columns = {}
    with db._lock, db._connect() as conn:
        for table, item in tables.items():
            if not _table_exists(conn, table):
                dropped_tables.append(table)
                continue
            target_columns = set(_table_columns(conn, table))
            supported[table] = len(item["rows"])
            missing = [col for col in item["columns"] if col not in target_columns]
            if missing:
                dropped_columns[table] = missing

    warnings = []
    source_edition = str(manifest.get("source_edition") or "")
    if source_edition and source_edition != str(target_edition):
        warnings.append(
            f"Edition-Wechsel {source_edition} → {target_edition}: nicht vorhandene Zieltabellen/-spalten werden bewusst ausgelassen."
        )
    if manifest.get("anonymized"):
        warnings.append("Dieses Paket ist anonymisiert; ursprüngliche persönliche Daten können nicht wiederhergestellt werden.")
    if dropped_tables:
        warnings.append("Einige quellseitige Module existieren in der Ziel-Edition nicht und werden übersprungen.")

    return {
        "manifest": manifest,
        "tables": supported,
        "total_rows": sum(len(item["rows"]) for item in tables.values()),
        "target_counts": counts,
        "blockers": blockers,
        "can_import": not blockers,
        "dropped_tables": dropped_tables,
        "dropped_columns": dropped_columns,
        "warnings": warnings,
    }


def _insert_rows(conn, table, source_columns, rows):
    target_columns = set(_table_columns(conn, table))
    columns = [column for column in source_columns if column in target_columns]
    if not columns:
        return 0
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})"
    count = 0
    for row in rows:
        conn.execute(sql, [row.get(column) for column in columns])
        count += 1
    return count


def import_package(data, target_edition):
    manifest, tables = _load_package(data)
    preview = preview_package(data, target_edition)
    if not preview["can_import"]:
        details = ", ".join(f"{table}: {count}" for table, count in preview["blockers"].items())
        raise ValueError(
            "Direktimport nur in eine frische operative Installation möglich. "
            f"Bereits vorhandene Daten: {details}."
        )

    imported = {}
    skipped_tables = []
    dropped_columns = {}

    with db._lock, db._connect() as conn:
        existing = {table for table in EXPORT_TABLES if _table_exists(conn, table)}

        # Fresh installations may contain first-run tariff defaults. Only tables
        # included in the package are cleared; authentication/settings stay intact.
        for table in reversed(EXPORT_TABLES):
            if table in tables and table in existing:
                conn.execute(f"DELETE FROM {table}")

        for table in EXPORT_TABLES:
            if table not in tables:
                continue
            if table not in existing:
                skipped_tables.append(table)
                continue
            source_columns = tables[table]["columns"]
            target_columns = set(_table_columns(conn, table))
            missing = [column for column in source_columns if column not in target_columns]
            if missing:
                dropped_columns[table] = missing
            imported[table] = _insert_rows(conn, table, source_columns, tables[table]["rows"])

        conn.commit()

    # Re-run idempotent schema/index initialization after raw data transfer.
    db.init_db()
    return {
        "ok": True,
        "source_version": manifest.get("source_version"),
        "source_edition": manifest.get("source_edition"),
        "target_edition": target_edition,
        "anonymized": bool(manifest.get("anonymized")),
        "imported": imported,
        "skipped_tables": skipped_tables,
        "dropped_columns": dropped_columns,
        "warnings": preview["warnings"],
    }
