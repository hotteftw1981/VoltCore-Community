"""Self-update support: GitHub release checks and optional Portainer redeploy webhook."""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import db
from .runtime_utils import bool_value, clear_secret, ensure_setting_defaults, read_secret, secret_is_configured, write_secret

REPOSITORY = os.getenv("UPDATE_GITHUB_REPOSITORY", "hotteftw1981/VoltCore").strip()
LEGACY_REPOSITORY = os.getenv("UPDATE_GITHUB_REPOSITORY_FALLBACK", "hotteftw1981/drk-ocpp-backend").strip()
GITHUB_TOKEN_FILE = db.DATA_DIR / ".update_github_token"
PORTAINER_WEBHOOK_FILE = db.DATA_DIR / ".update_portainer_webhook"
CHECK_CACHE_SECONDS = 600
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
}
_cache_lock = threading.Lock()
_cache = {"at": 0.0, "payload": None}


def ensure_defaults():
    ensure_setting_defaults(DEFAULTS)


def settings(include_secret_state=True):
    ensure_defaults()
    result = {
        "check_enabled": bool_value(db.get_setting("update_check_enabled", "1")),
        "repository": REPOSITORY,
        "portainer_tls_verify": bool_value(db.get_setting("update_portainer_tls_verify", "1")),
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
        result["portainer_webhook_configured"] = secret_is_configured(PORTAINER_WEBHOOK_FILE)
    return result


def _validate_webhook(value):
    text=str(value or "").strip()
    parsed=urllib.parse.urlparse(text)
    if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Portainer-Webhook muss eine gültige HTTP-/HTTPS-URL ohne eingebettete Zugangsdaten sein.")
    if "/api/" not in parsed.path or "webhook" not in parsed.path:
        raise ValueError("Die URL sieht nicht wie ein Portainer-Webhook aus.")
    return text


def save_settings(payload):
    db.set_setting("update_check_enabled", "1" if payload.get("check_enabled", True) else "0")
    db.set_setting("update_portainer_tls_verify", "1" if payload.get("portainer_tls_verify", True) else "0")
    token=payload.get("github_token")
    if token is not None and str(token).strip():
        write_secret(GITHUB_TOKEN_FILE, str(token).strip())
    if payload.get("clear_github_token"):
        clear_secret(GITHUB_TOKEN_FILE)
    webhook=payload.get("portainer_webhook")
    if webhook is not None and str(webhook).strip():
        write_secret(PORTAINER_WEBHOOK_FILE, _validate_webhook(webhook))
    if payload.get("clear_portainer_webhook"):
        clear_secret(PORTAINER_WEBHOOK_FILE)
    with _cache_lock:
        _cache["at"]=0.0
        _cache["payload"]=None
    return settings()


def _version_tuple(value):
    text=str(value or "").strip().lower()
    if text.startswith("v"):
        text=text[1:]
    if not re.fullmatch(r"\d+(?:\.\d+){1,5}", text):
        raise ValueError(f"Ungültige Versionsnummer: {value}")
    return tuple(int(x) for x in text.split("."))


def is_newer(candidate, current):
    a=list(_version_tuple(candidate)); b=list(_version_tuple(current))
    length=max(len(a),len(b))
    a += [0]*(length-len(a)); b += [0]*(length-len(b))
    return tuple(a)>tuple(b)


def _github_latest_release(token):
    repositories=[]
    for repository in (REPOSITORY, LEGACY_REPOSITORY):
        if repository and repository not in repositories:
            repositories.append(repository)
    last_error=None
    for repository in repositories:
        url=f"https://api.github.com/repos/{repository}/releases/latest"
        req=urllib.request.Request(url,headers={
            "Accept":"application/vnd.github+json",
            "Authorization":f"Bearer {token}",
            "X-GitHub-Api-Version":"2022-11-28",
            "User-Agent":"voltcore-update-center",
        })
        try:
            with urllib.request.urlopen(req,timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error=exc
            if exc.code==404 and repository != repositories[-1]:
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError("Kein GitHub-Repository für die Update-Prüfung konfiguriert.")


def _status_from_release(current_version, release):
    tag=str(release.get("tag_name") or "").strip()
    latest=tag[1:] if tag.lower().startswith("v") else tag
    _version_tuple(latest)
    available=is_newer(latest,current_version)
    ahead=is_newer(current_version,latest)
    return {
        "configured":True,
        "current_version":current_version,
        "latest_version":latest,
        "update_available":available,
        "up_to_date":not available and not ahead,
        "ahead_of_release":ahead,
        "release_name":str(release.get("name") or tag or latest),
        "release_url":str(release.get("html_url") or ""),
        "published_at":str(release.get("published_at") or ""),
        "notes":str(release.get("body") or "")[:16000],
        "install_ready":secret_is_configured(PORTAINER_WEBHOOK_FILE),
    }


def check_latest(current_version, force=False):
    ensure_defaults()
    cfg=settings()
    if not cfg["check_enabled"]:
        return {"configured":cfg.get("github_token_configured",False),"check_enabled":False,"current_version":current_version,"update_available":False,"message":"Update-Prüfung ist deaktiviert.","install_ready":cfg.get("portainer_webhook_configured",False)}
    token=read_secret(GITHUB_TOKEN_FILE).strip()
    if not token:
        return {"configured":False,"check_enabled":True,"current_version":current_version,"update_available":False,"message":"GitHub-Zugriff noch nicht eingerichtet.","install_ready":cfg.get("portainer_webhook_configured",False)}
    now=time.time()
    with _cache_lock:
        cached=_cache.get("payload")
        if not force and cached is not None and now-float(_cache.get("at") or 0)<CHECK_CACHE_SECONDS:
            return dict(cached)
    try:
        release=_github_latest_release(token)
        payload=_status_from_release(current_version,release)
        checked=datetime.now(timezone.utc).isoformat()
        payload["checked_at"]=checked
        db.set_setting("update_last_check_at",checked)
        db.set_setting("update_last_seen_version",payload["latest_version"])
        db.set_setting("update_last_release_name",payload["release_name"])
        db.set_setting("update_last_release_url",payload["release_url"])
        db.set_setting("update_last_release_published_at",payload["published_at"])
        db.set_setting("update_last_error","")
    except urllib.error.HTTPError as exc:
        detail="GitHub-Zugriff fehlgeschlagen."
        if exc.code in (401,403):
            detail="GitHub-Token ist ungültig oder besitzt keinen Lesezugriff auf das private Repository."
        elif exc.code==404:
            detail="Kein freigegebenes Release gefunden oder das Repository ist für den Token nicht sichtbar."
        db.set_setting("update_last_check_at",datetime.now(timezone.utc).isoformat())
        db.set_setting("update_last_error",f"HTTP {exc.code}: {detail}")
        raise RuntimeError(detail) from exc
    except Exception as exc:
        db.set_setting("update_last_check_at",datetime.now(timezone.utc).isoformat())
        db.set_setting("update_last_error",f"{type(exc).__name__}: {exc}"[:1000])
        raise
    with _cache_lock:
        _cache["at"]=now
        _cache["payload"]=dict(payload)
    return payload


def cached_status(current_version):
    cfg=settings()
    latest=cfg.get("last_seen_version") or ""
    error=cfg.get("last_error") or ""
    if not cfg.get("github_token_configured"):
        return {"level":"neutral","label":"Nicht eingerichtet","detail":"GitHub-Release-Prüfung benötigt einen Lese-Token","available":False}
    if error:
        return {"level":"warn","label":"Prüfen","detail":"Letzte Update-Prüfung ist fehlgeschlagen","available":False}
    if latest:
        try:
            if is_newer(latest,current_version):
                return {"level":"neutral","label":f"V{latest} verfügbar","detail":"Freigegebenes Update wartet auf Installation","available":True,"latest_version":latest}
        except ValueError:
            pass
        return {"level":"ok","label":"Aktuell","detail":f"Neuester freigegebener Stand V{latest}","available":False,"latest_version":latest}
    return {"level":"neutral","label":"Noch nicht geprüft","detail":"Update-Center wartet auf die erste Release-Prüfung","available":False}


def _portainer_context(url, verify):
    if urllib.parse.urlparse(url).scheme=="https" and not verify:
        return ssl._create_unverified_context()
    return ssl.create_default_context() if urllib.parse.urlparse(url).scheme=="https" else None


def trigger_portainer(target_version):
    cfg=settings()
    raw=read_secret(PORTAINER_WEBHOOK_FILE).strip()
    if not raw:
        raise RuntimeError("Kein Portainer-Webhook hinterlegt.")
    raw=_validate_webhook(raw)
    parsed=urllib.parse.urlparse(raw)
    query=urllib.parse.parse_qs(parsed.query,keep_blank_values=True)
    query["tag"]=[f"v{target_version}"]
    url=urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query,doseq=True)))
    req=urllib.request.Request(url,data=b"",method="POST",headers={"User-Agent":"ocpp-backend-update-center"})
    ctx=_portainer_context(url,cfg.get("portainer_tls_verify",True))
    with urllib.request.urlopen(req,timeout=20,context=ctx) as response:
        status=int(getattr(response,"status",200) or 200)
        if status<200 or status>=300:
            raise RuntimeError(f"Portainer antwortete mit HTTP {status}.")
        return {"ok":True,"status":status}


def mark_pending(target_version, backup_filename):
    db.set_setting("update_pending_version",str(target_version))
    db.set_setting("update_pending_backup",str(backup_filename or ""))
    db.set_setting("update_triggered_at",datetime.now(timezone.utc).isoformat())
    db.set_setting("update_last_trigger_error","")


def trigger_and_record(target_version):
    try:
        trigger_portainer(target_version)
        return True
    except Exception as exc:
        db.set_setting("update_last_trigger_error",f"{type(exc).__name__}: {exc}"[:1000])
        db.set_setting("update_pending_version","")
        try:
            db.create_notification("update:trigger:failed","critical","Update konnte nicht gestartet werden",str(exc),"/updates",audience="admin")
        except Exception:
            pass
        return False


def reconcile_startup(current_version):
    ensure_defaults()
    pending=db.get_setting("update_pending_version","") or ""
    if pending and str(pending)==str(current_version):
        now=datetime.now(timezone.utc).isoformat()
        db.set_setting("update_last_success_at",now)
        db.set_setting("update_last_success_version",str(current_version))
        db.set_setting("update_pending_version","")
        db.set_setting("update_pending_backup","")
        db.set_setting("update_last_trigger_error","")
        try:
            db.create_notification("update:success:"+str(current_version),"info","Update erfolgreich installiert",f"Version {current_version} läuft.","/updates",audience="admin")
        except Exception:
            pass
        return True
    return False
