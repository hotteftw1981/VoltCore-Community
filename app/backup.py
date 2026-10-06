"""Backup and restore support for the OCPP backend.

Backups are self-contained ZIP archives with a consistent SQLite snapshot and
persistent DATA_DIR files. The backup directory and external credentials are
explicitly excluded to avoid recursive archives and credential disclosure.
"""
from __future__ import annotations

import ftplib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import ssl
import tempfile
import zipfile

import paramiko
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath

from . import db
from .runtime_utils import (
    bool_value as _bool,
    clear_secret,
    ensure_setting_defaults,
    read_secret,
    secret_is_configured,
    write_secret,
)

BACKUP_DIR = db.DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
CREDENTIAL_FILE = db.DATA_DIR / ".backup_external_password"
SFTP_KNOWN_HOSTS_FILE = db.DATA_DIR / ".backup_sftp_known_hosts"
SMTP_CREDENTIAL_FILE = db.DATA_DIR / ".smtp_password"
BACKUP_PREFIX = "voltcore-community-backup-"
BACKUP_RE = re.compile(r"^ocpp-backup-(\d{8})-(\d{6})(?:-[a-z0-9_-]+)?\.zip$", re.I)
MAX_RESTORE_BYTES = 5 * 1024 * 1024 * 1024

DEFAULTS = {
    "backup_schedule_enabled": "0",
    "backup_frequency": "daily",
    "backup_time": "03:00",
    "backup_weekday": "0",
    "backup_retention_days": "30",
    "backup_internal_enabled": "1",
    "backup_external_enabled": "0",
    "backup_external_protocol": "ftps",
    "backup_external_host": "",
    "backup_external_port": "21",
    "backup_external_username": "",
    "backup_external_path": "/",
    "backup_external_tls_verify": "1",
    "backup_last_scheduled_at": "",
    "backup_last_success_at": "",
    "backup_last_success_filename": "",
    "backup_external_last_success_at": "",
    "backup_external_last_error_at": "",
    "backup_external_last_error": "",
}


def ensure_defaults():
    ensure_setting_defaults(DEFAULTS)


def _clean_remote_path(value):
    value = str(value or "/").strip().replace("\\", "/")
    if not value:
        return "/"
    return "/" + value.strip("/") if value != "/" else "/"


def settings(include_secret_state=True):
    ensure_defaults()
    result = {
        "schedule_enabled": _bool(db.get_setting("backup_schedule_enabled", "0")),
        "frequency": db.get_setting("backup_frequency", "daily") or "daily",
        "time": db.get_setting("backup_time", "03:00") or "03:00",
        "weekday": int(db.get_setting("backup_weekday", "0") or 0),
        "retention_days": int(db.get_setting("backup_retention_days", "30") or 30),
        "internal_enabled": _bool(db.get_setting("backup_internal_enabled", "1")),
        "external_enabled": _bool(db.get_setting("backup_external_enabled", "0")),
        "external_protocol": (db.get_setting("backup_external_protocol", "ftps") or "ftps").lower(),
        "external_host": db.get_setting("backup_external_host", "") or "",
        "external_port": int(db.get_setting("backup_external_port", "21") or 21),
        "external_username": db.get_setting("backup_external_username", "") or "",
        "external_path": _clean_remote_path(db.get_setting("backup_external_path", "/") or "/"),
        "external_tls_verify": _bool(db.get_setting("backup_external_tls_verify", "1")),
        "last_scheduled_at": db.get_setting("backup_last_scheduled_at", "") or "",
        "last_success_at": db.get_setting("backup_last_success_at", "") or "",
        "last_success_filename": db.get_setting("backup_last_success_filename", "") or "",
        "external_last_success_at": db.get_setting("backup_external_last_success_at", "") or "",
        "external_last_error_at": db.get_setting("backup_external_last_error_at", "") or "",
        "external_last_error": db.get_setting("backup_external_last_error", "") or "",
    }
    if include_secret_state:
        result["external_password_configured"] = secret_is_configured(CREDENTIAL_FILE)
    return result


def save_settings(payload: dict):
    frequency = str(payload.get("frequency") or "daily").lower()
    if frequency not in {"daily", "weekly"}:
        raise ValueError("Backup-Intervall muss täglich oder wöchentlich sein.")
    time_text = str(payload.get("time") or "03:00")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_text):
        raise ValueError("Backup-Uhrzeit ist ungültig.")
    weekday = int(payload.get("weekday", 0))
    if weekday < 0 or weekday > 6:
        raise ValueError("Wochentag ist ungültig.")
    retention = int(payload.get("retention_days", 30))
    if retention < 1 or retention > 3650:
        raise ValueError("Aufbewahrungsfrist muss zwischen 1 und 3650 Tagen liegen.")
    protocol = str(payload.get("external_protocol") or "ftps").lower()
    if protocol not in {"ftp", "ftps", "sftp"}:
        raise ValueError("Extern werden FTP, FTPS und SFTP unterstützt.")
    port = int(payload.get("external_port") or (22 if protocol == "sftp" else 21))
    if port < 1 or port > 65535:
        raise ValueError("Port ist ungültig.")
    internal = bool(payload.get("internal_enabled", True))
    external = bool(payload.get("external_enabled", False))
    if not internal and not external:
        raise ValueError("Mindestens ein Backup-Ziel muss aktiviert sein.")
    if external and not str(payload.get("external_host") or "").strip():
        raise ValueError("Für externe Backups fehlt der Servername.")
    values = {
        "backup_schedule_enabled": "1" if payload.get("schedule_enabled") else "0",
        "backup_frequency": frequency,
        "backup_time": time_text,
        "backup_weekday": str(weekday),
        "backup_retention_days": str(retention),
        "backup_internal_enabled": "1" if internal else "0",
        "backup_external_enabled": "1" if external else "0",
        "backup_external_protocol": protocol,
        "backup_external_host": str(payload.get("external_host") or "").strip()[:255],
        "backup_external_port": str(port),
        "backup_external_username": str(payload.get("external_username") or "").strip()[:255],
        "backup_external_path": _clean_remote_path(payload.get("external_path") or "/"),
        "backup_external_tls_verify": "1" if payload.get("external_tls_verify", True) else "0",
    }
    for key, value in values.items():
        db.set_setting(key, value)
    password = payload.get("external_password")
    if password is not None and str(password) != "":
        write_secret(CREDENTIAL_FILE, str(password))
    if payload.get("clear_external_password"):
        clear_secret(CREDENTIAL_FILE)
    return settings()


def _password():
    return read_secret(CREDENTIAL_FILE)


def _sha256(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_filename(label=None):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = ""
    if label:
        clean = re.sub(r"[^a-z0-9_-]+", "-", str(label).lower()).strip("-")[:30]
        suffix = f"-{clean}" if clean else ""
    token=os.urandom(3).hex()
    return f"{BACKUP_PREFIX}{stamp}{suffix}-{token}.zip"


def _sqlite_snapshot(target: Path):
    source = sqlite3.connect(db.DB_PATH, timeout=30, check_same_thread=False)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
        check = dest.execute("PRAGMA integrity_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise RuntimeError("SQLite-Integritätsprüfung des Backups ist fehlgeschlagen.")
    finally:
        dest.close()
        source.close()


def _persistent_files():
    excluded_roots = {BACKUP_DIR.resolve()}
    excluded_files = {db.DB_PATH.resolve(), CREDENTIAL_FILE.resolve(), SMTP_CREDENTIAL_FILE.resolve(), (db.DATA_DIR/".update_github_token").resolve()}
    for path in db.DATA_DIR.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in excluded_files or path.name.startswith(db.DB_PATH.name + "-"):
            continue
        if any(root == resolved or root in resolved.parents for root in excluded_roots):
            continue
        yield path


def create_backup(label=None, keep_local=True):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    filename = _backup_filename(label)
    final_path = BACKUP_DIR / filename
    tmp_zip = BACKUP_DIR / (filename + ".partial")
    with tempfile.TemporaryDirectory(prefix="voltcore-community-backup-") as td:
        snapshot = Path(td) / "ocpp.sqlite3"
        _sqlite_snapshot(snapshot)
        manifest = {
            "format": 1,
            "product": db.branding_settings().get("product_name") or "VoltCore",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": "database/ocpp.sqlite3",
            "data_root": "data/",
            "excluded": ["backups/", ".backup_external_password", ".smtp_password", ".update_github_token"],
        }
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(snapshot, "database/ocpp.sqlite3")
            for path in _persistent_files():
                rel = path.relative_to(db.DATA_DIR).as_posix()
                zf.write(path, f"data/{rel}")
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            bad = zf.testzip()
            if bad:
                raise RuntimeError(f"ZIP-Integritätsprüfung fehlgeschlagen: {bad}")
            if "database/ocpp.sqlite3" not in zf.namelist() or "manifest.json" not in zf.namelist():
                raise RuntimeError("Backup ist unvollständig.")
        tmp_zip.replace(final_path)
        try:
            os.chmod(final_path,0o600)
        except OSError:
            pass
    info = backup_info(final_path)
    if not keep_local:
        info["temporary"] = True
    return info


def _manifest_from_zip(path: Path):
    try:
        with zipfile.ZipFile(path, "r") as zf:
            raw = zf.read("manifest.json")
            return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def backup_info(path: Path):
    stat = path.stat()
    manifest = _manifest_from_zip(path)
    return {
        "filename": path.name,
        "size_bytes": stat.st_size,
        "created_at": manifest.get("created_at") or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "sha256": _sha256(path),
        "valid": bool(manifest.get("database")),
        "label": "pre-restore" if "pre-restore" in path.name else ("scheduled" if "scheduled" in path.name else "manual"),
    }


def list_backups():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    items=[]
    for path in sorted(BACKUP_DIR.glob(f"{BACKUP_PREFIX}*.zip"), key=lambda p:p.stat().st_mtime, reverse=True):
        try:
            items.append(backup_info(path))
        except OSError:
            continue
    return items


def backup_path(filename):
    name = Path(str(filename)).name
    if name != str(filename) or not name.startswith(BACKUP_PREFIX) or not name.endswith(".zip"):
        raise ValueError("Ungültiger Backup-Dateiname.")
    path = BACKUP_DIR / name
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(name)
    return path


def delete_backup(filename):
    path = backup_path(filename)
    path.unlink()
    return True


def _ftp_connect(cfg):
    host=cfg["external_host"]; port=int(cfg["external_port"]); user=cfg["external_username"]; password=_password()
    if not host:
        raise ValueError("Kein externer Server konfiguriert.")
    if not password:
        raise ValueError("Kein Passwort für das externe Backup-Ziel hinterlegt.")
    if cfg["external_protocol"] == "ftps":
        context = ssl.create_default_context() if cfg.get("external_tls_verify", True) else ssl._create_unverified_context()
        ftp = ftplib.FTP_TLS(context=context, timeout=20)
        ftp.connect(host, port)
        ftp.login(user, password)
        ftp.prot_p()
    else:
        ftp = ftplib.FTP(timeout=20)
        ftp.connect(host, port)
        ftp.login(user, password)
    return ftp


def _ftp_ensure_dir(ftp, remote_path):
    if not remote_path or remote_path == "/":
        ftp.cwd("/")
        return
    ftp.cwd("/")
    for part in [p for p in remote_path.split("/") if p]:
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            ftp.mkd(part)
            ftp.cwd(part)


def _ftp_close(ftp):
    try:
        ftp.quit()
    except Exception:
        ftp.close()


def _sftp_connect(cfg):
    host=cfg["external_host"]; port=int(cfg["external_port"]); user=cfg["external_username"]; password=_password()
    if not host:
        raise ValueError("Kein externer Server konfiguriert.")
    if not user:
        raise ValueError("Für SFTP fehlt der Benutzername.")
    if not password:
        raise ValueError("Kein Passwort für das externe Backup-Ziel hinterlegt.")
    client=paramiko.SSHClient()
    if SFTP_KNOWN_HOSTS_FILE.exists():
        try:
            client.load_host_keys(str(SFTP_KNOWN_HOSTS_FILE))
        except OSError:
            pass
    # Trust-on-first-use: the first successful connection stores the server key
    # in /data. Every later connection verifies that the server presents the
    # same key and fails on a mismatch.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host, port=port, username=user, password=password,
        timeout=20, banner_timeout=20, auth_timeout=20,
        allow_agent=False, look_for_keys=False,
    )
    try:
        client.save_host_keys(str(SFTP_KNOWN_HOSTS_FILE))
        try:
            os.chmod(SFTP_KNOWN_HOSTS_FILE,0o600)
        except OSError:
            pass
        return client, client.open_sftp()
    except Exception:
        client.close()
        raise


def _sftp_ensure_dir(sftp, remote_path):
    remote_path=_clean_remote_path(remote_path)
    if remote_path == "/":
        sftp.chdir("/")
        return
    sftp.chdir("/")
    for part in [p for p in remote_path.split("/") if p]:
        try:
            sftp.chdir(part)
        except OSError:
            sftp.mkdir(part)
            sftp.chdir(part)


def _sftp_close(client, sftp):
    try:
        sftp.close()
    finally:
        client.close()


def test_external():
    cfg=settings()
    if cfg["external_protocol"] == "sftp":
        client,sftp=_sftp_connect(cfg)
        try:
            _sftp_ensure_dir(sftp,cfg["external_path"])
            sftp.listdir(".")
            return {"ok":True,"protocol":"sftp","host":cfg["external_host"],"path":cfg["external_path"],"host_key_pinned":True}
        finally:
            _sftp_close(client,sftp)
    ftp=_ftp_connect(cfg)
    try:
        _ftp_ensure_dir(ftp,cfg["external_path"])
        ftp.voidcmd("NOOP")
        return {"ok":True,"protocol":cfg["external_protocol"],"host":cfg["external_host"],"path":cfg["external_path"]}
    finally:
        _ftp_close(ftp)


def upload_external(path: Path):
    cfg=settings()
    if cfg["external_protocol"] == "sftp":
        client,sftp=_sftp_connect(cfg)
        try:
            _sftp_ensure_dir(sftp,cfg["external_path"])
            sftp.put(str(path),path.name,confirm=True)
            return {"ok":True,"filename":path.name,"protocol":"sftp","host":cfg["external_host"]}
        finally:
            _sftp_close(client,sftp)
    ftp=_ftp_connect(cfg)
    try:
        _ftp_ensure_dir(ftp,cfg["external_path"])
        with path.open("rb") as fh:
            ftp.storbinary(f"STOR {path.name}", fh, blocksize=1024*256)
        return {"ok":True,"filename":path.name,"protocol":cfg["external_protocol"],"host":cfg["external_host"]}
    finally:
        _ftp_close(ftp)


def _filename_dt(name):
    match=BACKUP_RE.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1)+match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def apply_retention():
    cfg=settings(); days=max(1,int(cfg["retention_days"])); cutoff=datetime.now(timezone.utc)-timedelta(days=days)
    local=list_backups(); removed_local=[]
    # Always retain at least the newest complete local backup.
    for item in local[1:]:
        dt=_filename_dt(item["filename"])
        if dt and dt < cutoff:
            try:
                (BACKUP_DIR/item["filename"]).unlink(); removed_local.append(item["filename"])
            except OSError:
                pass
    removed_external=[]
    if cfg["external_enabled"]:
        try:
            if cfg["external_protocol"] == "sftp":
                client,sftp=_sftp_connect(cfg)
                try:
                    _sftp_ensure_dir(sftp,cfg["external_path"])
                    names=[n for n in sftp.listdir(".") if BACKUP_RE.match(Path(n).name)]
                    names=sorted(names, key=lambda n:_filename_dt(Path(n).name) or datetime.max.replace(tzinfo=timezone.utc), reverse=True)
                    for name in names[1:]:
                        dt=_filename_dt(Path(name).name)
                        if dt and dt < cutoff:
                            sftp.remove(name); removed_external.append(Path(name).name)
                finally:
                    _sftp_close(client,sftp)
            else:
                ftp=_ftp_connect(cfg)
                try:
                    _ftp_ensure_dir(ftp,cfg["external_path"])
                    names=[n for n in ftp.nlst() if BACKUP_RE.match(Path(n).name)]
                    names=sorted(names, key=lambda n:_filename_dt(Path(n).name) or datetime.max.replace(tzinfo=timezone.utc), reverse=True)
                    for name in names[1:]:
                        dt=_filename_dt(Path(name).name)
                        if dt and dt < cutoff:
                            ftp.delete(name); removed_external.append(Path(name).name)
                finally:
                    _ftp_close(ftp)
        except Exception:
            # Retention failure must never turn a successful backup into a failed backup.
            pass
    return {"local":removed_local,"external":removed_external}


def run_backup(label="manual"):
    cfg=settings()
    # External-only mode still needs a temporary local archive for transport.
    info=create_backup(label=label, keep_local=cfg["internal_enabled"])
    path=BACKUP_DIR/info["filename"]
    external=None; external_error=None
    try:
        if cfg["external_enabled"]:
            try:
                external=upload_external(path)
                now=datetime.now(timezone.utc).isoformat()
                db.set_setting("backup_external_last_success_at",now)
                db.set_setting("backup_external_last_error_at","")
                db.set_setting("backup_external_last_error","")
            except Exception as exc:
                external_error=f"{type(exc).__name__}: {exc}"
                db.set_setting("backup_external_last_error_at",datetime.now(timezone.utc).isoformat())
                db.set_setting("backup_external_last_error",external_error[:1000])
                if not cfg["internal_enabled"]:
                    raise
        retention=apply_retention()
        now=datetime.now(timezone.utc).isoformat()
        db.set_setting("backup_last_success_at",now)
        db.set_setting("backup_last_success_filename",info["filename"])
        return {"ok":True,"backup":info,"internal":cfg["internal_enabled"],"external":external,"external_error":external_error,"retention":retention}
    finally:
        if not cfg["internal_enabled"]:
            path.unlink(missing_ok=True)


def scheduled_due(now_local):
    cfg=settings()
    if not cfg["schedule_enabled"]:
        return False
    hh,mm=[int(x) for x in cfg["time"].split(":",1)]
    if (now_local.hour,now_local.minute) < (hh,mm):
        return False
    if cfg["frequency"] == "weekly" and now_local.weekday() != cfg["weekday"]:
        return False
    last=cfg.get("last_scheduled_at")
    if last:
        try:
            last_dt=datetime.fromisoformat(last.replace("Z","+00:00"))
            if last_dt.tzinfo is None: last_dt=last_dt.replace(tzinfo=timezone.utc)
            if last_dt.astimezone(now_local.tzinfo).date() == now_local.date():
                return False
        except ValueError:
            pass
    return True


def mark_scheduled_run():
    db.set_setting("backup_last_scheduled_at", datetime.now(timezone.utc).isoformat())


def _safe_member(name):
    p=PurePosixPath(name)
    return not p.is_absolute() and ".." not in p.parts


def validate_restore(path: Path):
    if path.stat().st_size > MAX_RESTORE_BYTES:
        raise ValueError("Backup ist für die Wiederherstellung zu groß.")
    with zipfile.ZipFile(path,"r") as zf:
        names=zf.namelist()
        if not all(_safe_member(n) for n in names):
            raise ValueError("Backup enthält unsichere Dateipfade.")
        if "manifest.json" not in names or "database/ocpp.sqlite3" not in names:
            raise ValueError("Kein gültiges OCPP-Backup.")
        if zf.testzip():
            raise ValueError("ZIP-Integritätsprüfung fehlgeschlagen.")
        manifest=json.loads(zf.read("manifest.json").decode("utf-8"))
        if int(manifest.get("format",0)) != 1:
            raise ValueError("Backup-Format wird nicht unterstützt.")
        total=sum(i.file_size for i in zf.infolist())
        if total > MAX_RESTORE_BYTES:
            raise ValueError("Entpackte Backup-Daten sind zu groß.")
    return manifest


def restore_backup(path: Path):
    validate_restore(path)
    # Safety copy is mandatory and remains local regardless of the configured target.
    safety=create_backup(label="pre-restore", keep_local=True)
    with tempfile.TemporaryDirectory(prefix="ocpp-restore-") as td:
        stage=Path(td)
        with zipfile.ZipFile(path,"r") as zf:
            zf.extractall(stage)
        source_db=stage/"database"/"ocpp.sqlite3"
        src=sqlite3.connect(source_db)
        try:
            check=src.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise ValueError("Datenbank im Backup ist beschädigt.")
            with db._lock:
                dest=sqlite3.connect(db.DB_PATH,timeout=30,check_same_thread=False)
                try:
                    src.backup(dest)
                finally:
                    dest.close()
        finally:
            src.close()
        # A backup may originate from an older application version. Re-run the
        # idempotent schema initializer immediately so current code never operates
        # on a restored legacy schema.
        db.init_db()
        data_stage=stage/"data"
        if data_stage.exists():
            # Only overwrite files present in the backup. Backup archives and the
            # external target password are intentionally never restored.
            for item in data_stage.rglob("*"):
                if not item.is_file(): continue
                rel=item.relative_to(data_stage)
                target=db.DATA_DIR/rel
                if BACKUP_DIR.resolve() in target.resolve().parents or target.resolve() in {CREDENTIAL_FILE.resolve(), SMTP_CREDENTIAL_FILE.resolve(), (db.DATA_DIR/".update_github_token").resolve(), (db.DATA_DIR/".update_portainer_webhook").resolve()}:
                    continue
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(item,target)
    return {"ok":True,"safety_backup":safety}
