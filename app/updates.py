"""VoltCore Community update support via GitHub Releases.

The Community edition supports two one-click deployment providers:
- Portainer stack webhook
- Bundled Docker Compose updater sidecar

The application container never receives Docker socket access.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
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
IMAGE_REPOSITORY = os.getenv(
    "UPDATE_IMAGE_REPOSITORY",
    "ghcr.io/hotteftw1981/voltcore-community",
).strip()
GITHUB_TOKEN_FILE = db.DATA_DIR / ".update_github_token"
PORTAINER_WEBHOOK_FILE = db.DATA_DIR / ".update_portainer_webhook"
UPDATE_AGENT_URL = os.getenv("UPDATE_AGENT_URL", "").strip().rstrip("/")
UPDATE_AGENT_TOKEN_FILE = Path(
    os.getenv("UPDATE_AGENT_TOKEN_FILE", "/run/voltcore-updater/token")
)
CHECK_CACHE_SECONDS = int(os.getenv("UPDATE_CHECK_CACHE_SECONDS", "21600") or 21600)

DEFAULTS = {
    "update_check_enabled": "1",
    "update_portainer_tls_verify": "1",
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
    "update_last_provider": "",
}
_cache_lock = threading.Lock()
_cache = {"at": 0.0, "payload": None}


def ensure_defaults():
    ensure_setting_defaults(DEFAULTS)


def _agent_token():
    try:
        return UPDATE_AGENT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _docker_agent_configured():
    return bool(UPDATE_AGENT_URL and _agent_token())


def _install_provider():
    if secret_is_configured(PORTAINER_WEBHOOK_FILE):
        return "portainer"
    if _docker_agent_configured():
        return "docker-compose"
    return ""


def settings(include_secret_state=True):
    ensure_defaults()
    provider = _install_provider()
    result = {
        "edition": EDITION,
        "source": UPDATE_SOURCE,
        "channel": UPDATE_CHANNEL,
        "repository": REPOSITORY,
        "image_repository": IMAGE_REPOSITORY,
        "check_enabled": bool_value(db.get_setting("update_check_enabled", "1")),
        "portainer_tls_verify": bool_value(db.get_setting("update_portainer_tls_verify", "1")),
        "install_provider": provider,
        "install_ready": bool(provider),
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
        "last_provider": db.get_setting("update_last_provider", "") or "",
    }
    if include_secret_state:
        result["github_token_configured"] = secret_is_configured(GITHUB_TOKEN_FILE)
        result["portainer_webhook_configured"] = secret_is_configured(PORTAINER_WEBHOOK_FILE)
        result["docker_agent_configured"] = _docker_agent_configured()
    return result


def _validate_webhook(value):
    text = str(value or "").strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Portainer-Webhook muss eine gültige HTTP-/HTTPS-URL ohne eingebettete Zugangsdaten sein.")
    if "/api/" not in parsed.path or "webhook" not in parsed.path:
        raise ValueError("Die URL sieht nicht wie ein Portainer-Webhook aus.")
    return text


def save_settings(payload):
    db.set_setting(
        "update_check_enabled",
        "1" if payload.get("check_enabled", True) else "0",
    )
    db.set_setting(
        "update_portainer_tls_verify",
        "1" if payload.get("portainer_tls_verify", True) else "0",
    )
    token = payload.get("github_token")
    if token is not None and str(token).strip():
        write_secret(GITHUB_TOKEN_FILE, str(token).strip())
    if payload.get("clear_github_token"):
        clear_secret(GITHUB_TOKEN_FILE)
    webhook = payload.get("portainer_webhook")
    if webhook is not None and str(webhook).strip():
        write_secret(PORTAINER_WEBHOOK_FILE, _validate_webhook(webhook))
    if payload.get("clear_portainer_webhook"):
        clear_secret(PORTAINER_WEBHOOK_FILE)
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
    provider = _install_provider()
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
        "install_provider": provider,
        "install_ready": bool(provider),
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
            "install_provider": cfg.get("install_provider", ""),
            "install_ready": cfg.get("install_ready", False),
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
            cached = dict(cached)
            provider = _install_provider()
            cached["install_provider"] = provider
            cached["install_ready"] = bool(provider)
            return cached

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
            detail = "GitHub-Zugriff wurde abgewiesen. Bitte optionalen Token und Berechtigungen prüfen."
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


def _portainer_context(url, verify):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" and not verify:
        return ssl._create_unverified_context()
    return ssl.create_default_context() if parsed.scheme == "https" else None


def trigger_portainer(target_version):
    cfg = settings()
    raw = read_secret(PORTAINER_WEBHOOK_FILE).strip()
    if not raw:
        raise RuntimeError("Kein Portainer-Stack-Webhook hinterlegt.")
    raw = _validate_webhook(raw)
    parsed = urllib.parse.urlparse(raw)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query["VOLTCORE_COMMUNITY_IMAGE"] = [f"{IMAGE_REPOSITORY}:v{target_version}"]
    url = urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(query, doseq=True))
    )
    req = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={"User-Agent": "voltcore-community-update-center"},
    )
    ctx = _portainer_context(url, cfg.get("portainer_tls_verify", True))
    with urllib.request.urlopen(req, timeout=20, context=ctx) as response:
        status = int(getattr(response, "status", 200) or 200)
        if status < 200 or status >= 300:
            raise RuntimeError(f"Portainer antwortete mit HTTP {status}.")
        return {"ok": True, "status": status, "provider": "portainer"}


def trigger_docker_agent(target_version):
    token = _agent_token()
    if not UPDATE_AGENT_URL or not token:
        raise RuntimeError("Docker-Updater ist für diese Installation nicht verfügbar.")
    body = json.dumps({"target_version": str(target_version)}).encode("utf-8")
    req = urllib.request.Request(
        UPDATE_AGENT_URL + "/update",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "voltcore-community-update-center",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        status = int(getattr(response, "status", 200) or 200)
        payload = json.loads(response.read().decode("utf-8") or "{}")
        if status < 200 or status >= 300:
            raise RuntimeError(f"Docker-Updater antwortete mit HTTP {status}.")
        return payload


def mark_pending(target_version, backup_filename):
    db.set_setting("update_pending_version", str(target_version))
    db.set_setting("update_pending_backup", str(backup_filename or ""))
    db.set_setting("update_triggered_at", datetime.now(timezone.utc).isoformat())
    db.set_setting("update_last_trigger_error", "")


def trigger_and_record(target_version):
    provider = _install_provider()
    try:
        if provider == "portainer":
            trigger_portainer(target_version)
        elif provider == "docker-compose":
            trigger_docker_agent(target_version)
        else:
            raise RuntimeError("Kein 1-Klick-Update-Provider ist für diese Installation verfügbar.")
        db.set_setting("update_last_provider", provider)
        return True
    except Exception as exc:
        db.set_setting("update_last_trigger_error", f"{type(exc).__name__}: {exc}"[:1000])
        db.set_setting("update_pending_version", "")
        try:
            db.create_notification(
                "update:trigger:failed",
                "critical",
                "Update konnte nicht gestartet werden",
                str(exc),
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
