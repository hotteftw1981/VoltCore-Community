"""VoltCore Community update support via GitHub Releases.

GitHub Releases are the canonical update source for the Community edition.
The actual installation mechanism is intentionally provider-neutral and will
be selected by the deployment type later (Docker, Portainer, etc.).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import db
from .runtime_utils import (
    bool_value,
    clear_secret,
    ensure_setting_defaults,
    read_secret,
    secret_is_configured,
    write_secret,
)

EDITION = "community"
UPDATE_SOURCE = "github"
UPDATE_CHANNEL = os.getenv("UPDATE_CHANNEL", "stable").strip().lower() or "stable"
REPOSITORY = os.getenv(
    "UPDATE_GITHUB_REPOSITORY",
    "hotteftw1981/VoltCore-Community",
).strip()
GITHUB_TOKEN_FILE = db.DATA_DIR / ".update_github_token"
CHECK_CACHE_SECONDS = int(os.getenv("UPDATE_CHECK_CACHE_SECONDS", "21600") or 21600)

DEFAULTS = {
    "update_check_enabled": "1",
    "update_last_check_at": "",
    "update_last_seen_version": "",
    "update_last_release_name": "",
    "update_last_release_url": "",
    "update_last_release_published_at": "",
    "update_last_error": "",
    "update_pending_version": "",
    "update_pending_backup": "",
    "update_triggered_at": "",
    "update_last_success_at": "",
    "update_last_success_version": "",
    "update_last_trigger_error": "",
}
_cache_lock = threading.Lock()
_cache = {"at": 0.0, "payload": None}


def ensure_defaults():
    ensure_setting_defaults(DEFAULTS)


def settings(include_secret_state=True):
    ensure_defaults()
    result = {
        "edition": EDITION,
        "source": UPDATE_SOURCE,
        "channel": UPDATE_CHANNEL,
        "repository": REPOSITORY,
        "check_enabled": bool_value(db.get_setting("update_check_enabled", "1")),
        "install_provider": "",
        "install_ready": False,
        "last_check_at": db.get_setting("update_last_check_at", "") or "",
        "last_seen_version": db.get_setting("update_last_seen_version", "") or "",
        "last_release_name": db.get_setting("update_last_release_name", "") or "",
        "last_release_url": db.get_setting("update_last_release_url", "") or "",
        "last_release_published_at": db.get_setting("update_last_release_published_at", "") or "",
        "last_error": db.get_setting("update_last_error", "") or "",
        "pending_version": db.get_setting("update_pending_version", "") or "",
        "pending_backup": db.get_setting("update_pending_backup", "") or "",
        "triggered_at": db.get_setting("update_triggered_at", "") or "",
        "last_success_at": db.get_setting("update_last_success_at", "") or "",
        "last_success_version": db.get_setting("update_last_success_version", "") or "",
        "last_trigger_error": db.get_setting("update_last_trigger_error", "") or "",
    }
    if include_secret_state:
        result["github_token_configured"] = secret_is_configured(GITHUB_TOKEN_FILE)
    return result


def save_settings(payload):
    db.set_setting(
        "update_check_enabled",
        "1" if payload.get("check_enabled", True) else "0",
    )
    token = payload.get("github_token")
    if token is not None and str(token).strip():
        write_secret(GITHUB_TOKEN_FILE, str(token).strip())
    if payload.get("clear_github_token"):
        clear_secret(GITHUB_TOKEN_FILE)
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["payload"] = None
    return settings()


def _version_tuple(value):
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    if not re.fullmatch(r"\d+(?:\.\d+){1,5}", text):
        raise ValueError(f"Ungültige Versionsnummer: {value}")
    return tuple(int(x) for x in text.split("."))


def is_newer(candidate, current):
    a = list(_version_tuple(candidate))
    b = list(_version_tuple(current))
    length = max(len(a), len(b))
    a += [0] * (length - len(a))
    b += [0] * (length - len(b))
    return tuple(a) > tuple(b)


def _github_latest_release(token):
    if not REPOSITORY:
        raise RuntimeError("Kein GitHub-Repository für die Update-Prüfung konfiguriert.")

    url = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "voltcore-community-update-center",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _status_from_release(current_version, release):
    tag = str(release.get("tag_name") or "").strip()
    latest = tag[1:] if tag.lower().startswith("v") else tag
    _version_tuple(latest)
    available = is_newer(latest, current_version)
    ahead = is_newer(current_version, latest)
    return {
        "configured": bool(REPOSITORY),
        "edition": EDITION,
        "source": UPDATE_SOURCE,
        "channel": UPDATE_CHANNEL,
        "repository": REPOSITORY,
        "current_version": current_version,
        "latest_version": latest,
        "update_available": available,
        "up_to_date": not available and not ahead,
        "ahead_of_release": ahead,
        "release_name": str(release.get("name") or tag or latest),
        "release_url": str(release.get("html_url") or ""),
        "published_at": str(release.get("published_at") or ""),
        "notes": str(release.get("body") or "")[:16000],
        "install_provider": "",
        "install_ready": False,
    }


def check_latest(current_version, force=False):
    ensure_defaults()
    cfg = settings()
    if not cfg["check_enabled"]:
        return {
            "configured": bool(REPOSITORY),
            "check_enabled": False,
            "edition": EDITION,
            "source": UPDATE_SOURCE,
            "channel": UPDATE_CHANNEL,
            "repository": REPOSITORY,
            "current_version": current_version,
            "update_available": False,
            "install_provider": "",
            "install_ready": False,
            "message": "Update-Prüfung ist deaktiviert.",
        }

    token = read_secret(GITHUB_TOKEN_FILE).strip()
    now = time.time()
    with _cache_lock:
        cached = _cache.get("payload")
        if (
            not force
            and cached is not None
            and now - float(_cache.get("at") or 0) < CHECK_CACHE_SECONDS
        ):
            return dict(cached)

    try:
        release = _github_latest_release(token)
        payload = _status_from_release(current_version, release)
        checked = datetime.now(timezone.utc).isoformat()
        payload["checked_at"] = checked
        db.set_setting("update_last_check_at", checked)
        db.set_setting("update_last_seen_version", payload["latest_version"])
        db.set_setting("update_last_release_name", payload["release_name"])
        db.set_setting("update_last_release_url", payload["release_url"])
        db.set_setting("update_last_release_published_at", payload["published_at"])
        db.set_setting("update_last_error", "")
    except urllib.error.HTTPError as exc:
        detail = "GitHub-Zugriff fehlgeschlagen."
        if exc.code in (401, 403):
            detail = (
                "GitHub-Zugriff wurde abgewiesen. Falls für die private "
                "Entwicklungsphase ein optionaler Token hinterlegt ist, "
                "bitte dessen Berechtigung prüfen."
            )
        elif exc.code == 404:
            detail = "Noch kein freigegebenes VoltCore-Community-Release gefunden."
        db.set_setting("update_last_check_at", datetime.now(timezone.utc).isoformat())
        db.set_setting("update_last_error", f"HTTP {exc.code}: {detail}")
        raise RuntimeError(detail) from exc
    except Exception as exc:
        db.set_setting("update_last_check_at", datetime.now(timezone.utc).isoformat())
        db.set_setting("update_last_error", f"{type(exc).__name__}: {exc}"[:1000])
        raise

    with _cache_lock:
        _cache["at"] = now
        _cache["payload"] = dict(payload)
    return payload


def cached_status(current_version):
    cfg = settings()
    latest = cfg.get("last_seen_version") or ""
    error = cfg.get("last_error") or ""
    if error:
        return {
            "level": "warn",
            "label": "Prüfen",
            "detail": "Letzte GitHub-Release-Prüfung ist fehlgeschlagen",
            "available": False,
        }
    if latest:
        try:
            if is_newer(latest, current_version):
                return {
                    "level": "neutral",
                    "label": f"V{latest} verfügbar",
                    "detail": "Neues VoltCore-Community-Release auf GitHub verfügbar",
                    "available": True,
                    "latest_version": latest,
                }
        except ValueError:
            pass
        return {
            "level": "ok",
            "label": "Aktuell",
            "detail": f"Neuester Community-Release-Stand V{latest}",
            "available": False,
            "latest_version": latest,
        }
    return {
        "level": "neutral",
        "label": "Noch nicht geprüft",
        "detail": "Update-Center wartet auf die erste GitHub-Release-Prüfung",
        "available": False,
    }


def mark_pending(target_version, backup_filename):
    """Record an update started by a deployment-specific installer provider."""
    db.set_setting("update_pending_version", str(target_version))
    db.set_setting("update_pending_backup", str(backup_filename or ""))
    db.set_setting("update_triggered_at", datetime.now(timezone.utc).isoformat())
    db.set_setting("update_last_trigger_error", "")


def trigger_and_record(target_version):
    """Compatibility hook until a deployment-specific installer is selected."""
    message = (
        "Für diese VoltCore-Community-Installation ist noch kein automatischer "
        "Update-Installer konfiguriert. Die Updatequelle ist bereits GitHub Releases."
    )
    db.set_setting("update_last_trigger_error", message)
    try:
        db.create_notification(
            "update:installer:not-configured",
            "info",
            "Update erkannt – Installer noch nicht konfiguriert",
            message,
            "/updates",
            audience="admin",
        )
    except Exception:
        pass
    return False


def reconcile_startup(current_version):
    ensure_defaults()
    pending = db.get_setting("update_pending_version", "") or ""
    if pending and str(pending) == str(current_version):
        now = datetime.now(timezone.utc).isoformat()
        db.set_setting("update_last_success_at", now)
        db.set_setting("update_last_success_version", str(current_version))
        db.set_setting("update_pending_version", "")
        db.set_setting("update_pending_backup", "")
        db.set_setting("update_last_trigger_error", "")
        try:
            db.create_notification(
                "update:success:" + str(current_version),
                "info",
                "Update erfolgreich installiert",
                f"VoltCore Community {current_version} läuft.",
                "/updates",
                audience="admin",
            )
        except Exception:
            pass
        return True
    return False
