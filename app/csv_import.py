import csv
import hashlib
import io
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db

MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100000

PROVIDERS = {
    "generic": "Neutral / eigenes CSV",
    "reev": "reev",
    "chargecloud": "chargecloud",
    "be_energised": "be.ENERGISED / ChargePoint",
    "monta": "Monta",
    "evbox_everon": "EVBox Everon",
    "other": "Anderes System",
}

FIELD_DEFS = {
    "users": [
        {"key": "name", "label": "Name", "required": True, "aliases": ["name", "benutzer", "user", "nutzer", "fahrer", "driver", "rfid nutzer"]},
        {"key": "email", "label": "E-Mail", "aliases": ["email", "e-mail", "mail", "user email"]},
        {"key": "phone", "label": "Telefon", "aliases": ["telefon", "phone", "mobile", "mobil"]},
        {"key": "status", "label": "Status", "aliases": ["status", "state"]},
    ],
    "vehicles": [
        {"key": "name", "label": "Fahrzeugname", "aliases": ["fahrzeug", "vehicle", "vehicle name", "fahrzeugname", "name"]},
        {"key": "make", "label": "Hersteller", "aliases": ["hersteller", "make", "brand", "marke"]},
        {"key": "model", "label": "Modell", "aliases": ["modell", "model"]},
        {"key": "plate", "label": "Kennzeichen", "aliases": ["kennzeichen", "license plate", "plate", "registration", "number plate"]},
        {"key": "battery_kwh", "label": "Batterie kWh", "aliases": ["battery kwh", "battery", "batterie", "batteriekapazität", "batteriekapazitaet"]},
        {"key": "ac_power_kw", "label": "AC-Leistung kW", "aliases": ["ac power", "ac kw", "ac leistung", "ac power kw"]},
        {"key": "dc_power_kw", "label": "DC-Leistung kW", "aliases": ["dc power", "dc kw", "dc leistung", "dc power kw"]},
        {"key": "range_km", "label": "Reichweite km", "aliases": ["range", "range km", "reichweite", "reichweite km"]},
    ],
    "rfids": [
        {"key": "uid", "label": "RFID UID", "required": True, "aliases": ["rfid", "rfid uid", "rfid tag", "uid", "id tag", "idtag", "token", "card id", "card uid"]},
        {"key": "label", "label": "Bezeichnung", "aliases": ["label", "bezeichnung", "card name", "name"]},
        {"key": "user_name", "label": "Benutzername", "aliases": ["benutzer", "user", "user name", "username", "nutzer", "fahrer", "driver", "rfid nutzer"]},
        {"key": "user_email", "label": "Benutzer E-Mail", "aliases": ["user email", "benutzer email", "email", "e-mail"]},
        {"key": "vehicle_plate", "label": "Kennzeichen", "aliases": ["kennzeichen", "license plate", "plate", "registration"]},
        {"key": "status", "label": "Status", "aliases": ["status", "state"]},
        {"key": "expires_at", "label": "Gültig bis", "aliases": ["expires", "expires at", "expiry", "gültig bis", "gueltig bis", "ablauf"]},
        {"key": "notes", "label": "Notiz", "aliases": ["notes", "note", "notiz", "bemerkung"]},
    ],
    "charge_points": [
        {"key": "charge_point_id", "label": "OCPP-Ladepunkt-ID", "required": True, "aliases": ["charge point id", "chargepoint id", "ocpp id", "station id", "ladepunkt id", "ladepunkt", "charge point", "station"]},
        {"key": "name", "label": "Name", "aliases": ["name", "station name", "ladepunkt name"]},
        {"key": "vendor", "label": "Hersteller", "aliases": ["vendor", "manufacturer", "hersteller", "marke"]},
        {"key": "model", "label": "Modell", "aliases": ["model", "modell"]},
        {"key": "serial_number", "label": "Seriennummer", "aliases": ["serial", "serial number", "seriennummer"]},
        {"key": "firmware", "label": "Firmware", "aliases": ["firmware", "firmware version"]},
        {"key": "location", "label": "Standort", "aliases": ["location", "standort", "site"]},
        {"key": "connector_count", "label": "Connectoren", "aliases": ["connector count", "connectors", "anschlüsse", "anschluesse", "ports"]},
        {"key": "max_power_kw", "label": "Max. Leistung kW", "aliases": ["max power", "max power kw", "max kw", "leistung kw"]},
    ],
    "sessions": [
        {"key": "started_at", "label": "Startzeit", "required": True, "aliases": ["start", "started at", "start time", "startdatum", "startdatum utc", "session start", "ladestart", "charging start"]},
        {"key": "ended_at", "label": "Endzeit", "aliases": ["end", "ended at", "end time", "enddatum", "session end", "ladeende", "charging end"]},
        {"key": "duration_seconds", "label": "Dauer", "aliases": ["dauer", "duration", "duration seconds", "connection duration", "session duration"]},
        {"key": "energy_kwh", "label": "Energie kWh", "required": True, "aliases": ["kwh", "energy", "energy kwh", "verbrauch", "charged energy", "total energy"]},
        {"key": "charge_point_id", "label": "Ladepunkt-ID", "required": True, "aliases": ["charge point id", "chargepoint id", "ocpp id", "station id", "ladepunkt id", "ladepunkt", "charge point", "station"]},
        {"key": "connector_id", "label": "Connector", "aliases": ["connector", "connector id", "anschluss", "port"]},
        {"key": "rfid_uid", "label": "RFID UID", "aliases": ["rfid", "rfid uid", "rfid tag", "uid", "id tag", "idtag", "token", "card id"]},
        {"key": "user_name", "label": "Benutzername", "aliases": ["benutzer", "user", "user name", "username", "nutzer", "fahrer", "driver", "rfid nutzer"]},
        {"key": "user_email", "label": "Benutzer E-Mail", "aliases": ["user email", "benutzer email", "email", "e-mail"]},
        {"key": "vehicle_name", "label": "Fahrzeugname", "aliases": ["fahrzeug", "vehicle", "vehicle name", "fahrzeugname"]},
        {"key": "vehicle_plate", "label": "Kennzeichen", "aliases": ["kennzeichen", "license plate", "plate", "registration"]},
        {"key": "max_power_kw", "label": "Max. Leistung kW", "aliases": ["max power", "max power kw", "peak power", "peak kw", "max kw"]},
        {"key": "cost_eur", "label": "Gesamtkosten EUR", "aliases": ["cost", "cost eur", "kosten", "gesamtpreis", "total price", "amount"]},
        {"key": "price_eur_per_kwh", "label": "Preis EUR/kWh", "aliases": ["price per kwh", "eur/kwh", "€/kwh", "preis/kwh", "preis eur/kwh"]},
        {"key": "external_id", "label": "Externe Session-ID", "aliases": ["transaction id", "session id", "charge session id", "external id"]},
        {"key": "evse_id", "label": "EVSE-ID", "aliases": ["evse", "evse id"]},
    ],
}


def ensure_schema():
    """Keep neutral migration metadata without reintroducing provider-specific features."""
    with db._lock, db._connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
        for name, statement in {
            "import_source": "ALTER TABLE transactions ADD COLUMN import_source TEXT",
            "import_key": "ALTER TABLE transactions ADD COLUMN import_key TEXT",
            "imported_at": "ALTER TABLE transactions ADD COLUMN imported_at TEXT",
            "import_source_name": "ALTER TABLE transactions ADD COLUMN import_source_name TEXT",
            "import_evse_id": "ALTER TABLE transactions ADD COLUMN import_evse_id TEXT",
        }.items():
            if name not in columns:
                conn.execute(statement)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_import_source_key "
            "ON transactions(import_source,import_key) "
            "WHERE import_source IS NOT NULL AND import_key IS NOT NULL"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS csv_import_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                filename TEXT,
                imported INTEGER NOT NULL DEFAULT 0,
                duplicates INTEGER NOT NULL DEFAULT 0,
                conflicts INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.commit()


def provider_options():
    return [{"id": key, "label": label} for key, label in PROVIDERS.items()]


def field_schema(entity_type):
    entity = str(entity_type or "").strip()
    if entity not in FIELD_DEFS:
        raise ValueError("Unbekannter Importtyp.")
    return [{k: v for k, v in item.items() if k != "aliases"} for item in FIELD_DEFS[entity]]


def _header_key(value):
    text = str(value or "").strip().casefold()
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", "", text)


def suggest_mapping(entity_type, headers):
    fields = FIELD_DEFS.get(entity_type)
    if not fields:
        raise ValueError("Unbekannter Importtyp.")
    normalized = {_header_key(header): header for header in headers if str(header or "").strip()}
    used = set()
    result = {}
    for field in fields:
        candidates = [field["key"], field["label"], *field.get("aliases", [])]
        for candidate in candidates:
            hit = normalized.get(_header_key(candidate))
            if hit and hit not in used:
                result[field["key"]] = hit
                used.add(hit)
                break
    return result


def _decode_csv(data):
    if not data:
        raise ValueError("Die CSV-Datei ist leer.")
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError("Die CSV-Datei ist größer als 20 MB.")
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "iso-8859-1"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in text:
            return text, encoding
    raise ValueError("Die CSV-Datei konnte nicht als Text gelesen werden.")


def parse_csv_bytes(data):
    text, encoding = _decode_csv(data)
    sample = text[:65536]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        raw_headers = next(reader)
    except StopIteration:
        raise ValueError("Die CSV-Datei enthält keine Kopfzeile.")
    headers = [str(x or "").strip() for x in raw_headers]
    if not any(headers):
        raise ValueError("Die CSV-Kopfzeile ist leer.")
    keys = [_header_key(x) for x in headers if x]
    if len(keys) != len(set(keys)):
        raise ValueError("Die CSV-Datei enthält doppelte Spaltennamen.")
    rows = []
    for index, values in enumerate(reader, start=2):
        if index > MAX_ROWS + 1:
            raise ValueError(f"Die CSV-Datei enthält mehr als {MAX_ROWS} Datenzeilen.")
        if not any(str(x or "").strip() for x in values):
            continue
        row = {}
        for pos, header in enumerate(headers):
            if not header:
                continue
            row[header] = str(values[pos] if pos < len(values) else "").strip()
        rows.append(row)
    if not rows:
        raise ValueError("Die CSV-Datei enthält keine Datenzeilen.")
    return {
        "headers": headers,
        "rows": rows,
        "delimiter": "\\t" if delimiter == "\t" else delimiter,
        "encoding": encoding,
    }


def save_upload(data):
    token = secrets.token_hex(16)
    folder = Path(db.DATA_DIR) / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"neutral-{token}.csv").write_bytes(data)
    return token


def _upload_path(token):
    raw = str(token or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", raw):
        raise ValueError("Ungültiger Import-Token.")
    return Path(db.DATA_DIR) / "imports" / f"neutral-{raw}.csv"


def load_upload(token):
    path = _upload_path(token)
    if not path.exists():
        raise ValueError("Die Importdatei ist nicht mehr verfügbar. Bitte erneut hochladen.")
    return path.read_bytes()


def delete_upload(token):
    try:
        _upload_path(token).unlink(missing_ok=True)
    except Exception:
        pass


def _number(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("\u00a0", "").replace(" ", "")
    raw = re.sub(r"[^0-9,\.\-+]", "", raw)
    if not raw or raw in {"-", "+", ".", ","}:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _integer(value, default=None, minimum=None):
    number = _number(value)
    if number is None:
        return default
    result = int(round(number))
    if minimum is not None:
        result = max(minimum, result)
    return result


def _duration_seconds(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if re.fullmatch(r"[+-]?\d+(?:[\.,]\d+)?", raw):
        number = _number(raw)
        return None if number is None else max(0, int(round(number)))
    parts = raw.split(":")
    if len(parts) in (2, 3) and all(re.fullmatch(r"\d+", p.strip()) for p in parts):
        nums = [int(p) for p in parts]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    h = re.search(r"(\d+)\s*h", raw)
    m = re.search(r"(\d+)\s*m", raw)
    s = re.search(r"(\d+)\s*s", raw)
    if any((h, m, s)):
        return (int(h.group(1)) if h else 0) * 3600 + (int(m.group(1)) if m else 0) * 60 + (int(s.group(1)) if s else 0)
    return None


def _parse_datetime(value, timezone_name="Europe/Berlin"):
    raw = str(value or "").strip()
    if not raw:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    if dt is None:
        for fmt in (
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _normalize_status(value):
    raw = str(value or "").strip()
    key = raw.casefold()
    if key in {"", "active", "aktiv", "enabled", "valid", "ok"}:
        return "Aktiv"
    if key in {"inactive", "inaktiv", "disabled", "deactivated", "deaktiviert"}:
        return "Deaktiviert"
    if key in {"blocked", "gesperrt", "lost", "verloren"}:
        return "Gesperrt" if key in {"blocked", "gesperrt"} else "Verloren"
    return raw[:64] or "Aktiv"


def _mapped(raw_row, mapping, key):
    source = str(mapping.get(key) or "").strip()
    return str(raw_row.get(source) or "").strip() if source else ""


def normalize_rows(entity_type, raw_rows, mapping, timezone_name="Europe/Berlin"):
    if entity_type not in FIELD_DEFS:
        raise ValueError("Unbekannter Importtyp.")
    allowed = {item["key"] for item in FIELD_DEFS[entity_type]}
    mapping = {str(k): str(v) for k, v in dict(mapping or {}).items() if k in allowed and str(v or "").strip()}
    missing_fields = [item["label"] for item in FIELD_DEFS[entity_type] if item.get("required") and not mapping.get(item["key"])]
    if missing_fields:
        raise ValueError("Pflichtspalten nicht zugeordnet: " + ", ".join(missing_fields))
    result = []
    errors = []
    for offset, raw in enumerate(raw_rows, start=2):
        try:
            if entity_type == "users":
                name = _mapped(raw, mapping, "name")
                if not name:
                    raise ValueError("Name fehlt")
                item = {
                    "row_no": offset,
                    "name": name,
                    "email": _mapped(raw, mapping, "email"),
                    "phone": _mapped(raw, mapping, "phone"),
                    "status": _normalize_status(_mapped(raw, mapping, "status")),
                }
            elif entity_type == "vehicles":
                name = _mapped(raw, mapping, "name")
                plate = _mapped(raw, mapping, "plate")
                if not name and not plate:
                    raise ValueError("Fahrzeugname oder Kennzeichen fehlt")
                item = {
                    "row_no": offset,
                    "name": name or plate,
                    "make": _mapped(raw, mapping, "make"),
                    "model": _mapped(raw, mapping, "model"),
                    "plate": plate,
                    "battery_kwh": _number(_mapped(raw, mapping, "battery_kwh")),
                    "ac_power_kw": _number(_mapped(raw, mapping, "ac_power_kw")),
                    "dc_power_kw": _number(_mapped(raw, mapping, "dc_power_kw")),
                    "range_km": _number(_mapped(raw, mapping, "range_km")),
                }
            elif entity_type == "rfids":
                uid = _mapped(raw, mapping, "uid")
                if not uid:
                    raise ValueError("RFID UID fehlt")
                item = {
                    "row_no": offset,
                    "uid": uid,
                    "label": _mapped(raw, mapping, "label"),
                    "user_name": _mapped(raw, mapping, "user_name"),
                    "user_email": _mapped(raw, mapping, "user_email"),
                    "vehicle_plate": _mapped(raw, mapping, "vehicle_plate"),
                    "status": _normalize_status(_mapped(raw, mapping, "status")),
                    "expires_at": _parse_datetime(_mapped(raw, mapping, "expires_at"), timezone_name) if _mapped(raw, mapping, "expires_at") else None,
                    "notes": _mapped(raw, mapping, "notes"),
                }
            elif entity_type == "charge_points":
                cp_id = _mapped(raw, mapping, "charge_point_id")
                if not cp_id:
                    raise ValueError("Ladepunkt-ID fehlt")
                item = {
                    "row_no": offset,
                    "charge_point_id": cp_id,
                    "name": _mapped(raw, mapping, "name"),
                    "vendor": _mapped(raw, mapping, "vendor"),
                    "model": _mapped(raw, mapping, "model"),
                    "serial_number": _mapped(raw, mapping, "serial_number"),
                    "firmware": _mapped(raw, mapping, "firmware"),
                    "location": _mapped(raw, mapping, "location"),
                    "connector_count": _integer(_mapped(raw, mapping, "connector_count"), 1, 1),
                    "max_power_kw": _number(_mapped(raw, mapping, "max_power_kw")) or 22.0,
                }
            else:
                started = _parse_datetime(_mapped(raw, mapping, "started_at"), timezone_name)
                cp_id = _mapped(raw, mapping, "charge_point_id")
                energy = _number(_mapped(raw, mapping, "energy_kwh"))
                if not started:
                    raise ValueError("Startzeit fehlt oder ist ungültig")
                if not cp_id:
                    raise ValueError("Ladepunkt-ID fehlt")
                if energy is None or energy < 0:
                    raise ValueError("Energie fehlt oder ist ungültig")
                ended_raw = _mapped(raw, mapping, "ended_at")
                duration_raw = _mapped(raw, mapping, "duration_seconds")
                ended = _parse_datetime(ended_raw, timezone_name) if ended_raw else None
                duration = _duration_seconds(duration_raw) if duration_raw else None
                start_dt = datetime.fromisoformat(started)
                if ended:
                    end_dt = datetime.fromisoformat(ended)
                    if end_dt < start_dt:
                        raise ValueError("Endzeit liegt vor der Startzeit")
                    if duration is None:
                        duration = int(round((end_dt - start_dt).total_seconds()))
                elif duration is not None:
                    ended = (start_dt + timedelta(seconds=duration)).isoformat()
                else:
                    raise ValueError("Endzeit oder Dauer muss vorhanden sein")
                item = {
                    "row_no": offset,
                    "started_at": started,
                    "ended_at": ended,
                    "duration_seconds": max(0, int(duration or 0)),
                    "energy_kwh": round(float(energy), 6),
                    "charge_point_id": cp_id,
                    "connector_id": _integer(_mapped(raw, mapping, "connector_id"), 1, 1),
                    "rfid_uid": _mapped(raw, mapping, "rfid_uid"),
                    "user_name": _mapped(raw, mapping, "user_name"),
                    "user_email": _mapped(raw, mapping, "user_email"),
                    "vehicle_name": _mapped(raw, mapping, "vehicle_name"),
                    "vehicle_plate": _mapped(raw, mapping, "vehicle_plate"),
                    "max_power_kw": max(0.0, _number(_mapped(raw, mapping, "max_power_kw")) or 0.0),
                    "cost_eur": _number(_mapped(raw, mapping, "cost_eur")),
                    "price_eur_per_kwh": _number(_mapped(raw, mapping, "price_eur_per_kwh")),
                    "external_id": _mapped(raw, mapping, "external_id"),
                    "evse_id": _mapped(raw, mapping, "evse_id"),
                }
            result.append(item)
        except ValueError as exc:
            if len(errors) < 100:
                errors.append({"row_no": offset, "message": str(exc)})
    return result, errors


def _source(provider):
    key = str(provider or "generic").strip().lower()
    if key not in PROVIDERS:
        key = "other"
    return f"csv:{key}"


def _session_key(item):
    raw = "|".join([
        str(item.get("started_at") or ""),
        str(item.get("ended_at") or ""),
        str(item.get("charge_point_id") or ""),
        str(item.get("connector_id") or 1),
        str(item.get("rfid_uid") or ""),
        str(item.get("external_id") or ""),
        f"{float(item.get('energy_kwh') or 0):.6f}",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _insert_dynamic(conn, table, values):
    columns = _table_columns(conn, table)
    filtered = {key: value for key, value in values.items() if key in columns}
    keys = list(filtered)
    placeholders = ",".join("?" for _ in keys)
    sql = f"INSERT INTO {table}({','.join(keys)}) VALUES({placeholders})"
    cur = conn.execute(sql, [filtered[key] for key in keys])
    return int(cur.lastrowid)


def _find_user(conn, item):
    columns = _table_columns(conn, "users")
    email = str(item.get("user_email") or item.get("email") or "").strip()
    name = str(item.get("user_name") or item.get("name") or "").strip()
    if email and "email" in columns:
        rows = conn.execute("SELECT id FROM users WHERE LOWER(COALESCE(email,''))=LOWER(?) LIMIT 2", (email,)).fetchall()
        if len(rows) == 1:
            return int(rows[0][0]), False
        if len(rows) > 1:
            return None, True
    if name:
        rows = conn.execute("SELECT id FROM users WHERE LOWER(name)=LOWER(?) LIMIT 2", (name,)).fetchall()
        if len(rows) == 1:
            return int(rows[0][0]), False
        if len(rows) > 1:
            return None, True
    return None, False


def _find_vehicle(conn, item):
    columns = _table_columns(conn, "vehicles")
    plate = str(item.get("vehicle_plate") or item.get("plate") or "").strip()
    name = str(item.get("vehicle_name") or item.get("name") or "").strip()
    if plate and "plate" in columns:
        rows = conn.execute("SELECT id FROM vehicles WHERE LOWER(COALESCE(plate,''))=LOWER(?) AND active=1 LIMIT 2", (plate,)).fetchall()
        if len(rows) == 1:
            return int(rows[0][0]), False
        if len(rows) > 1:
            return None, True
    if name:
        rows = conn.execute("SELECT id FROM vehicles WHERE LOWER(name)=LOWER(?) AND active=1 LIMIT 2", (name,)).fetchall()
        if len(rows) == 1:
            return int(rows[0][0]), False
        if len(rows) > 1:
            return None, True
    return None, False


def _create_user(conn, item):
    name = str(item.get("user_name") or item.get("name") or item.get("user_email") or "").strip()
    if not name:
        return None
    return _insert_dynamic(conn, "users", {
        "name": name,
        "email": str(item.get("user_email") or item.get("email") or "").strip() or None,
        "phone": str(item.get("phone") or "").strip() or None,
        "status": _normalize_status(item.get("status")),
    })


def _create_vehicle(conn, item):
    plate = str(item.get("vehicle_plate") or item.get("plate") or "").strip()
    name = str(item.get("vehicle_name") or item.get("name") or plate).strip()
    if not name:
        return None
    return _insert_dynamic(conn, "vehicles", {
        "name": name,
        "make": str(item.get("make") or "").strip() or None,
        "model": str(item.get("model") or "").strip() or None,
        "plate": plate or None,
        "battery_kwh": item.get("battery_kwh"),
        "ac_power_kw": item.get("ac_power_kw"),
        "dc_power_kw": item.get("dc_power_kw"),
        "range_km": item.get("range_km"),
        "active": 1,
    })


def _create_charge_point(conn, item):
    cp_id = str(item.get("charge_point_id") or "").strip()
    connector_count = max(1, int(item.get("connector_count") or 1))
    max_power = max(0.0, float(item.get("max_power_kw") or 22.0))
    _insert_dynamic(conn, "charge_points", {
        "id": cp_id,
        "name": str(item.get("name") or "").strip() or None,
        "vendor": str(item.get("vendor") or "").strip() or None,
        "model": str(item.get("model") or "").strip() or None,
        "serial_number": str(item.get("serial_number") or "").strip() or None,
        "firmware": str(item.get("firmware") or "").strip() or None,
        "location": str(item.get("location") or "").strip() or None,
        "status": "Unknown",
        "connector_count": connector_count,
        "max_power_kw": max_power,
        "onboarded": 1,
        "ignored": 0,
        "simulated": 0,
    })
    connector_columns = _table_columns(conn, "connectors")
    for connector_id in range(1, connector_count + 1):
        values = {
            "charge_point_id": cp_id,
            "connector_id": connector_id,
            "connector_type": "Type 2",
            "max_power_kw": max_power,
            "status": "Unknown",
        }
        filtered = {k: v for k, v in values.items() if k in connector_columns}
        keys = list(filtered)
        conn.execute(
            f"INSERT OR IGNORE INTO connectors({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
            [filtered[key] for key in keys],
        )
    return cp_id


def _create_rfid(conn, item, user_id=None, vehicle_id=None, source_label="CSV Import"):
    uid = str(item.get("uid") or item.get("rfid_uid") or "").strip()
    if not uid:
        return None
    now = db.utc_now()
    return _insert_dynamic(conn, "rfid_cards", {
        "uid": uid,
        "label": str(item.get("label") or source_label).strip() or source_label,
        "user_id": user_id,
        "vehicle_id": vehicle_id,
        "status": _normalize_status(item.get("status")),
        "created_at": now,
        "issued_at": now,
        "expires_at": item.get("expires_at"),
        "notes": str(item.get("notes") or "").strip() or None,
    })


def analyze(entity_type, rows, provider="generic", create_missing=False):
    source = _source(provider)
    duplicates = 0
    conflicts = 0
    missing_references = 0
    with db._lock, db._connect() as conn:
        for item in rows:
            if entity_type == "users":
                user_id, ambiguous = _find_user(conn, item)
                conflicts += 1 if ambiguous else 0
                duplicates += 1 if user_id is not None else 0
            elif entity_type == "vehicles":
                vehicle_id, ambiguous = _find_vehicle(conn, item)
                conflicts += 1 if ambiguous else 0
                duplicates += 1 if vehicle_id is not None else 0
            elif entity_type == "rfids":
                existing = conn.execute("SELECT user_id,vehicle_id FROM rfid_cards WHERE uid=?", (item["uid"],)).fetchone()
                if existing:
                    duplicates += 1
                user_id, user_ambiguous = _find_user(conn, item)
                vehicle_id, vehicle_ambiguous = _find_vehicle(conn, item)
                conflicts += int(user_ambiguous or vehicle_ambiguous)
                if (item.get("user_name") or item.get("user_email")) and user_id is None and not create_missing:
                    missing_references += 1
                if item.get("vehicle_plate") and vehicle_id is None and not create_missing:
                    missing_references += 1
            elif entity_type == "charge_points":
                if conn.execute("SELECT 1 FROM charge_points WHERE id=?", (item["charge_point_id"],)).fetchone():
                    duplicates += 1
            else:
                key = _session_key(item)
                if conn.execute("SELECT 1 FROM transactions WHERE import_source=? AND import_key=?", (source, key)).fetchone():
                    duplicates += 1
                    continue
                if not conn.execute("SELECT 1 FROM charge_points WHERE id=?", (item["charge_point_id"],)).fetchone():
                    missing_references += 0 if create_missing else 1
                _, user_ambiguous = _find_user(conn, item)
                _, vehicle_ambiguous = _find_vehicle(conn, item)
                conflicts += int(user_ambiguous or vehicle_ambiguous)
    return {
        "rows": len(rows),
        "duplicates": duplicates,
        "conflicts": conflicts,
        "missing_references": missing_references,
        "ready": max(0, len(rows) - duplicates - conflicts - (missing_references if entity_type == "sessions" and not create_missing else 0)),
    }


def execute(entity_type, rows, provider="generic", create_missing=False, filename=None):
    source = _source(provider)
    imported = 0
    duplicates = 0
    conflicts = 0
    created = {"users": 0, "vehicles": 0, "rfids": 0, "charge_points": 0}
    imported_energy = 0.0
    new_rfid_uids = []
    with db._lock, db._connect() as conn:
        for item in rows:
            if entity_type == "users":
                user_id, ambiguous = _find_user(conn, item)
                if ambiguous:
                    conflicts += 1
                    continue
                if user_id is not None:
                    duplicates += 1
                    continue
                _create_user(conn, item)
                imported += 1
                created["users"] += 1
                continue

            if entity_type == "vehicles":
                vehicle_id, ambiguous = _find_vehicle(conn, item)
                if ambiguous:
                    conflicts += 1
                    continue
                if vehicle_id is not None:
                    duplicates += 1
                    continue
                _create_vehicle(conn, item)
                imported += 1
                created["vehicles"] += 1
                continue

            if entity_type == "rfids":
                existing = conn.execute("SELECT * FROM rfid_cards WHERE uid=?", (item["uid"],)).fetchone()
                if existing:
                    duplicates += 1
                    continue
                user_id, user_ambiguous = _find_user(conn, item)
                vehicle_id, vehicle_ambiguous = _find_vehicle(conn, item)
                if user_ambiguous or vehicle_ambiguous:
                    conflicts += 1
                    continue
                if user_id is None and create_missing and (item.get("user_name") or item.get("user_email")):
                    user_id = _create_user(conn, item)
                    created["users"] += 1 if user_id else 0
                if vehicle_id is None and create_missing and item.get("vehicle_plate"):
                    vehicle_id = _create_vehicle(conn, item)
                    created["vehicles"] += 1 if vehicle_id else 0
                _create_rfid(conn, item, user_id=user_id, vehicle_id=vehicle_id, source_label="CSV Import")
                new_rfid_uids.append(item["uid"])
                imported += 1
                created["rfids"] += 1
                continue

            if entity_type == "charge_points":
                if conn.execute("SELECT 1 FROM charge_points WHERE id=?", (item["charge_point_id"],)).fetchone():
                    duplicates += 1
                    continue
                _create_charge_point(conn, item)
                imported += 1
                created["charge_points"] += 1
                continue

            key = _session_key(item)
            if conn.execute("SELECT 1 FROM transactions WHERE import_source=? AND import_key=?", (source, key)).fetchone():
                duplicates += 1
                continue
            if not conn.execute("SELECT 1 FROM charge_points WHERE id=?", (item["charge_point_id"],)).fetchone():
                if not create_missing:
                    conflicts += 1
                    continue
                _create_charge_point(conn, {
                    "charge_point_id": item["charge_point_id"],
                    "name": item["charge_point_id"],
                    "connector_count": max(1, int(item.get("connector_id") or 1)),
                    "max_power_kw": item.get("max_power_kw") or 22.0,
                })
                created["charge_points"] += 1

            user_id = None
            vehicle_id = None
            rfid_card_id = None
            uid = str(item.get("rfid_uid") or "").strip()
            if uid:
                card = conn.execute("SELECT id,user_id,vehicle_id FROM rfid_cards WHERE uid=?", (uid,)).fetchone()
                if card:
                    rfid_card_id = int(card["id"])
                    user_id = int(card["user_id"]) if card["user_id"] is not None else None
                    vehicle_id = int(card["vehicle_id"]) if card["vehicle_id"] is not None else None

            resolved_user, user_ambiguous = _find_user(conn, item)
            resolved_vehicle, vehicle_ambiguous = _find_vehicle(conn, item)
            if user_ambiguous or vehicle_ambiguous:
                conflicts += 1
                continue
            if resolved_user is not None:
                if user_id is not None and user_id != resolved_user:
                    conflicts += 1
                    continue
                user_id = resolved_user
            elif user_id is None and create_missing and (item.get("user_name") or item.get("user_email")):
                user_id = _create_user(conn, item)
                created["users"] += 1 if user_id else 0

            if resolved_vehicle is not None:
                if vehicle_id is not None and vehicle_id != resolved_vehicle:
                    conflicts += 1
                    continue
                vehicle_id = resolved_vehicle
            elif vehicle_id is None and create_missing and (item.get("vehicle_plate") or item.get("vehicle_name")):
                vehicle_id = _create_vehicle(conn, item)
                created["vehicles"] += 1 if vehicle_id else 0

            if uid and rfid_card_id is None and create_missing:
                rfid_card_id = _create_rfid(conn, {"rfid_uid": uid, "status": "Aktiv"}, user_id=user_id, vehicle_id=vehicle_id, source_label=f"{PROVIDERS.get(str(provider), 'CSV')} Import")
                created["rfids"] += 1 if rfid_card_id else 0
                new_rfid_uids.append(uid)

            price = item.get("price_eur_per_kwh")
            cost = item.get("cost_eur")
            values = {
                "charge_point_id": item["charge_point_id"],
                "connector_id": int(item.get("connector_id") or 1),
                "transaction_id": item.get("external_id") or None,
                "id_tag": uid or None,
                "user_id": user_id,
                "rfid_card_id": rfid_card_id,
                "vehicle_id": vehicle_id,
                "started_at": item["started_at"],
                "ended_at": item["ended_at"],
                "energy_kwh": float(item.get("energy_kwh") or 0),
                "max_power_kw": float(item.get("max_power_kw") or 0),
                "status": "Completed",
                "stop_reason": "Imported",
                "charging_seconds": 0,
                "stand_seconds": 0,
                "connection_seconds": int(item.get("duration_seconds") or 0),
                "price_cents_per_kwh": None if price is None else int(round(float(price) * 100)),
                "cost_cents": None if cost is None else int(round(float(cost) * 100)),
                "tariff_source": source if price is not None else None,
                "import_source": source,
                "import_key": key,
                "imported_at": db.utc_now(),
                "import_source_name": item.get("user_name") or item.get("user_email") or None,
                "import_evse_id": item.get("evse_id") or None,
                "timing_quality": "connection_only",
            }
            _insert_dynamic(conn, "transactions", values)
            imported += 1
            imported_energy += float(item.get("energy_kwh") or 0)

        if new_rfid_uids and hasattr(db, "_rfid_local_list_bump_conn"):
            db._rfid_local_list_bump_conn(conn, sorted(set(new_rfid_uids)))

        conn.execute(
            "INSERT INTO csv_import_runs(created_at,source,entity_type,filename,imported,duplicates,conflicts) VALUES(?,?,?,?,?,?,?)",
            (db.utc_now(), source, entity_type, str(filename or "")[:255], imported, duplicates, conflicts),
        )
        conn.commit()

    return {
        "ok": True,
        "imported": imported,
        "duplicates": duplicates,
        "conflicts": conflicts,
        "energy_kwh": round(imported_energy, 3),
        "created": created,
        "source": source,
    }
