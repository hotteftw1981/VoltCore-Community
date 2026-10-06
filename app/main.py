import logging
import asyncio
import csv
import io
import os
import json
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
import hashlib
import hmac
import html
from urllib.parse import unquote, urlparse

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
import qrcode

from . import db
from . import backup
from . import mailer
from . import totp
from . import updates
from . import web_push
from .ocpp_server import serve_ocpp, remote_command, is_connected, probe_capabilities, verify_offline_authorization, sync_local_list, sync_pending_local_lists

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
BASE_DIR = Path(__file__).resolve().parent
APP_VERSION = "0.9.7.74"
OCPP_PORT = int(os.getenv("OCPP_PORT", "9000"))
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))
ENABLE_API_DOCS = str(os.getenv("ENABLE_API_DOCS", "0")).strip().lower() in {"1","true","yes","on"}
MEDIA_DIR = Path(os.getenv("DATA_DIR", "/data")) / "vehicle_images"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
BRANDING_DIR = Path(os.getenv("DATA_DIR", "/data")) / "branding"
BRANDING_DIR.mkdir(parents=True, exist_ok=True)
MAX_VEHICLE_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

BERLIN_TZ = ZoneInfo("Europe/Berlin")
APP_STARTED_AT = datetime.now(timezone.utc)
OPERATIONAL_STARTUP_GRACE_SECONDS = 90


def _local_dt(value):
    if not value:
        return None
    try:
        dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BERLIN_TZ)
    except (TypeError,ValueError):
        return None

def _local_text(value, fmt="%d.%m.%Y %H:%M:%S"):
    dt=_local_dt(value)
    return dt.strftime(fmt) if dt else ""

def _schedule_local_list_sync(reason="RFID geändert"):
    try:
        asyncio.get_running_loop().create_task(sync_pending_local_lists(reason=reason))
    except RuntimeError:
        pass


async def _send_admin_mail(template_key, event_key, context=None, cooldown_minutes=0, base_url=None):
    """Best-effort admin mail with persistent cooldown to prevent mail storms."""
    try:
        cfg=mailer.settings(False)
        if not cfg.get("enabled") or not mailer.event_enabled(event_key):
            return {"ok":False,"skipped":"disabled"}
        recipients=mailer.admin_recipients()
        if not recipients:
            return {"ok":False,"skipped":"no-recipients"}
        if cooldown_minutes:
            key=f"mail_last_event_{event_key}"
            last=_local_dt(db.get_setting(key,""))
            if last and (datetime.now(BERLIN_TZ)-last).total_seconds() < int(cooldown_minutes)*60:
                return {"ok":False,"skipped":"cooldown"}
            db.set_setting(key,datetime.now(timezone.utc).isoformat())
        return await asyncio.to_thread(mailer.send_template,template_key,recipients,context or {},base_url)
    except Exception as exc:
        logging.exception("Admin mail failed: %s",template_key)
        return {"ok":False,"error":f"{type(exc).__name__}: {exc}"}



async def _diagnostic_worker():
    while True:
        try:
            db.sync_notifications()
        except Exception:
            logging.exception("Monitoring/diagnostic sync failed")
        await asyncio.sleep(15)


async def _update_worker():
    while True:
        try:
            cfg=updates.settings()
            if cfg.get("check_enabled") and cfg.get("github_token_configured"):
                await asyncio.to_thread(updates.check_latest,APP_VERSION,True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.warning("Automatische Update-Prüfung fehlgeschlagen",exc_info=True)
        await asyncio.sleep(30*60)


async def _backup_worker():
    """Run configured daily/weekly backups without blocking the event loop."""
    while True:
        try:
            now_local=datetime.now(BERLIN_TZ)
            if backup.scheduled_due(now_local):
                result=await asyncio.to_thread(backup.run_backup,"scheduled")
                backup.mark_scheduled_run()
                logging.info("Scheduled backup completed: %s", result.get("backup",{}).get("filename"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("Scheduled backup failed")
            db.create_notification("backup:scheduled:failed","critical","Automatisches Backup fehlgeschlagen",f"{type(exc).__name__}: {exc}","/backups",audience="admin")
            await _send_admin_mail("backup_failed","backup_failures",{"detail":f"{type(exc).__name__}: {exc}"},cooldown_minutes=60)
        await asyncio.sleep(60)


async def _web_push_worker():
    """Deliver unread VoltCore notifications to subscribed PWA/browser clients."""
    while True:
        try:
            await asyncio.to_thread(web_push.dispatch_pending,80)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.warning("Web-Push-Worker fehlgeschlagen",exc_info=True)
        await asyncio.sleep(20)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    backup.ensure_defaults()
    mailer.ensure_defaults()
    updates.ensure_defaults()
    web_push.ensure_vapid_keys()
    updates.reconcile_startup(APP_VERSION)
    server = await serve_ocpp(port=OCPP_PORT)
    app.state.ocpp_server = server
    diagnostic_task=asyncio.create_task(_diagnostic_worker())
    backup_task=asyncio.create_task(_backup_worker())
    update_task=asyncio.create_task(_update_worker())
    push_task=asyncio.create_task(_web_push_worker())
    try:
        yield
    finally:
        diagnostic_task.cancel(); backup_task.cancel(); update_task.cancel(); push_task.cancel()
        for task in (diagnostic_task,backup_task,update_task,push_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        server.close()
        await server.wait_closed()


app = FastAPI(title="VoltCore Community", version=APP_VERSION, lifespan=lifespan, docs_url="/docs" if ENABLE_API_DOCS else None, redoc_url="/redoc" if ENABLE_API_DOCS else None, openapi_url="/openapi.json" if ENABLE_API_DOCS else None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
app.mount("/branding", StaticFiles(directory=BRANDING_DIR), name="branding")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def render(request: Request, template_name: str, status_code: int = 200, **context):
    context.setdefault("app_version", APP_VERSION)
    context.setdefault("branding", db.branding_settings())
    auth_user=getattr(request.state, "auth_user", None)
    context.setdefault("auth_user", auth_user)
    return templates.TemplateResponse(request=request, name=template_name, context=context, status_code=status_code)


SESSION_COOKIE = "voltcore_community_session"
TWO_FACTOR_COOKIE = "voltcore_community_2fa"
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "12"))
TWO_FACTOR_CHALLENGE_MINUTES = max(2, int(os.getenv("TWO_FACTOR_CHALLENGE_MINUTES", "5")))
TWO_FACTOR_MAX_FAILURES = max(3, int(os.getenv("TWO_FACTOR_MAX_FAILURES", "6")))
TWO_FACTOR_RATE_WINDOW_MINUTES = max(1, int(os.getenv("TWO_FACTOR_RATE_WINDOW_MINUTES", "10")))
PBKDF2_ITERATIONS = max(600000, int(os.getenv("PBKDF2_ITERATIONS", "600000")))
PASSWORD_MAX_LENGTH = 256
WEB_MAX_FAILURES = max(3, int(os.getenv("WEB_MAX_FAILURES", "8")))
WEB_FAILURE_WINDOW_MINUTES = max(1, int(os.getenv("WEB_FAILURE_WINDOW_MINUTES", "10")))
PUBLIC_PATHS = {"/login", "/login/2fa", "/setup", "/health", "/liveview", "/api/liveview", "/manifest.webmanifest", "/service-worker.js"}


def _password_hash(password: str) -> str:
    password=str(password or "")
    if len(password) < 12:
        raise ValueError("Das Passwort muss mindestens 12 Zeichen lang sein.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Das Passwort darf höchstens {PASSWORD_MAX_LENGTH} Zeichen lang sein.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _password_ok(password: str, encoded: str) -> bool:
    try:
        if len(str(password or "")) > 1024:
            return False
        scheme, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256": return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)).hex()
        return hmac.compare_digest(actual, digest_hex)
    except Exception:
        return False


def _password_needs_rehash(encoded: str) -> bool:
    try:
        scheme, iterations, _salt_hex, _digest_hex = str(encoded or "").split("$", 3)
        return scheme != "pbkdf2_sha256" or int(iterations) < PBKDF2_ITERATIONS
    except Exception:
        return True


_DUMMY_PASSWORD_SALT = bytes.fromhex("a93e62ad45fb76540fd0b944732b18bc")
_DUMMY_PASSWORD_DIGEST = hashlib.pbkdf2_hmac("sha256", b"invalid-password", _DUMMY_PASSWORD_SALT, PBKDF2_ITERATIONS).hex()
_DUMMY_PASSWORD_HASH = f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_DUMMY_PASSWORD_SALT.hex()}${_DUMMY_PASSWORD_DIGEST}"


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _trust_proxy_headers() -> bool:
    return str(os.getenv("TRUST_PROXY_HEADERS","0")).strip().lower() in {"1","true","yes","on"}


def _request_is_https(request: Request) -> bool:
    if request.url.scheme == "https": return True
    if not _trust_proxy_headers(): return False
    forwarded=str(request.headers.get("x-forwarded-proto") or "").split(",",1)[0].strip().lower()
    return forwarded in {"https","wss"}


def _cookie_secure(request: Request) -> bool:
    explicit=str(os.getenv("COOKIE_SECURE","")).strip().lower()
    if explicit in {"1","true","yes","on"}: return True
    if explicit in {"0","false","no","off"}: return False
    return _request_is_https(request)


def _client_text(request: Request) -> str:
    if _trust_proxy_headers():
        forwarded=str(request.headers.get("x-forwarded-for") or "").split(",",1)[0].strip()
        if forwarded: return forwarded
    return str((request.client.host if request.client else "unknown") or "unknown")


def _client_hash(request: Request) -> str:
    return hashlib.sha256(_client_text(request).encode("utf-8")).hexdigest()


def _same_origin_value(value: str | None, request: Request) -> bool:
    if not value: return True
    try:
        from urllib.parse import urlparse
        parsed=urlparse(value)
        request_host=str(request.headers.get("host") or "").lower()
        return bool(parsed.netloc) and parsed.netloc.lower()==request_host
    except Exception:
        return False


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/invite/") or path.startswith("/static/") or path.startswith("/branding/") or path.startswith("/media/") or path.startswith("/api/liveview/history/")


def _activity_descriptor(method: str, path: str):
    """Create a human-readable audit label without inspecting request bodies."""
    method=(method or "").upper()
    verb={"POST":"ausgeführt","PUT":"geändert","PATCH":"geändert","DELETE":"gelöscht"}.get(method,"ausgeführt")
    if path.startswith("/api/system-users"):
        return f"Systembenutzer {verb}", "Systembenutzer", path
    if path.startswith("/api/users") or path.startswith("/api/rfid"):
        return f"Ladebenutzer/RFID {verb}", "Ladebenutzer", path
    if path.startswith("/api/remote-control/"):
        return f"OCPP-Fernsteuerung {verb}", "Remote-Steuerung", path
    if path.startswith("/api/security"):
        return f"Sicherheitseinstellung {verb}", "Sicherheit", path
    if path.startswith("/api/backups"):
        if path.endswith("/restore") or path.endswith("/restore-upload"): return "Backup wiederhergestellt", "Backup", path
        if path.endswith("/create"): return "Backup erstellt", "Backup", path
        if path.endswith("/settings"): return "Backup-Einstellungen geändert", "Backup", path
        if path.endswith("/test-external"): return "Externes Backup-Ziel getestet", "Backup", path
        if method == "DELETE": return "Backup gelöscht", "Backup", path
        return f"Backup {verb}", "Backup", path
    if path.startswith("/api/updates"):
        if path.endswith("/install"): return "Systemupdate gestartet", "Updates", path
        if path.endswith("/settings"): return "Update-Einstellungen geändert", "Updates", path
        return f"Update-Center {verb}", "Updates", path
    if path.startswith("/api/settings/"):
        return f"Systemeinstellung {verb}", "Einstellungen", path
    if path.startswith("/api/vehicles"):
        return f"Fahrzeug {verb}", "Fahrzeuge", path
    if path.startswith("/api/charge-points"):
        if path.endswith("/archive"): return "Ladepunkt archiviert", "Ladepunkte", path
        if path.endswith("/unarchive"): return "Ladepunkt wiederhergestellt", "Ladepunkte", path
        if path.endswith("/retire"): return "Ladepunkt außer Betrieb gesetzt", "Ladepunkte", path
        if path.endswith("/restore"): return "Ladepunkt reaktiviert", "Ladepunkte", path
        return f"Ladepunkt {verb}", "Ladepunkte", path
    if path.startswith("/api/ocpp-devices"):
        return f"Ladepunkt-Erkennung {verb}", "Ladepunkte", path
    if path.startswith("/api/transactions"):
        return f"Ladevorgang {verb}", "Ladevorgänge", path
    if path == "/api/account/password":
        return "Eigenes Passwort geändert", "Zugriff", "Mein Konto"
    if path.startswith("/api/account/2fa"):
        return f"Zwei-Faktor-Authentifizierung {verb}", "Zugriff", "Mein Konto"
    return f"Änderung {verb}", "System", path


def _login_redirect(request: Request, reason: str | None = None):
    target = request.url.path
    if request.url.query: target += "?" + request.url.query
    from urllib.parse import quote
    url = "/login?next=" + quote(target, safe="")
    if reason:
        url += "&reason=" + quote(reason, safe="")
    return RedirectResponse(url=url, status_code=303)


@app.middleware("http")
async def same_origin_mutation_guard(request: Request, call_next):
    if request.method.upper() not in {"GET","HEAD","OPTIONS"}:
        fetch_site=str(request.headers.get("sec-fetch-site") or "").strip().lower()
        origin=request.headers.get("origin")
        referer=request.headers.get("referer")
        blocked=(fetch_site=="cross-site") or (origin is not None and not _same_origin_value(origin,request)) or (origin is None and referer is not None and not _same_origin_value(referer,request))
        if blocked:
            try:
                db.add_security_event("Cross-site mutation blocked",severity="warning",category="web",remote=_client_text(request),success=False,detail=f"{request.method} {request.url.path}")
                asyncio.create_task(_send_admin_mail("security_warning","security_warnings",{"body":"Eine Anfrage aus fremder Herkunft wurde blockiert.","detail":f"{request.method} {request.url.path}"},cooldown_minutes=60,base_url=str(request.base_url).rstrip("/")))
            except Exception:
                pass
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail":"Anfrage aus fremder Herkunft blockiert"},status_code=403)
            return HTMLResponse("Anfrage aus fremder Herkunft blockiert",status_code=403)
    return await call_next(request)


@app.middleware("http")
async def web_access_control(request: Request, call_next):
    path=request.url.path
    request.state.auth_user=None
    if _is_public_path(path):
        return await call_next(request)

    # First start: only the setup page can create the initial administrator.
    if db.system_user_count() == 0:
        if path.startswith("/api/"):
            return JSONResponse({"detail":"Ersteinrichtung erforderlich","setup_required":True}, status_code=503)
        return RedirectResponse(url="/setup", status_code=303)

    token=request.cookies.get(SESSION_COOKIE)
    auth=db.system_user_for_session(_session_hash(token)) if token else None
    if not auth:
        if path.startswith("/api/"):
            return JSONResponse({"detail":"Anmeldung erforderlich"}, status_code=401)
        return _login_redirect(request, "expired" if token else None)
    request.state.auth_user=auth

    # Settings and system-account administration are admin-only.
    admin_only = path in {"/welcome","/settings","/security","/tariffs","/backups","/updates","/openapi.json"} or path.startswith("/api/updates") or path.startswith("/docs") or path.startswith("/redoc") or path.startswith("/system-users") or path.startswith("/api/system-users") or path.startswith("/api/security") or path.startswith("/api/tariffs") or path.startswith("/api/settings/") or path.startswith("/api/backups") or path.startswith("/api/rfid/local-list") or path.startswith("/api/remote-control/")
    if admin_only and auth.get("role") != "admin":
        if path.startswith("/api/"):
            return JSONResponse({"detail":"Administratorrechte erforderlich"}, status_code=403)
        return render(request, "forbidden.html", status_code=403, page="", required="Administrator")

    # Read-only accounts may inspect the backend but cannot change operational data.
    if auth.get("role") == "viewer" and request.method not in ("GET","HEAD","OPTIONS"):
        allowed_read_state = path == "/logout" or path == "/api/account/password" or path.startswith("/api/account/2fa") or path.startswith("/api/notifications/")
        if not allowed_read_state:
            if path.startswith("/api/"):
                return JSONResponse({"detail":"Dieses Konto besitzt nur Leserechte"}, status_code=403)
            return render(request, "forbidden.html", status_code=403, page="", required="Schreibrechte")

    response = await call_next(request)
    if request.method not in ("GET","HEAD","OPTIONS") and response.status_code < 400 and not path.startswith("/api/notifications/") and path != "/logout" and not path.startswith("/api/remote-control/"):
        try:
            action, category, target = _activity_descriptor(request.method, path)
            db.add_activity(
                system_user_id=auth.get("id"), username=auth.get("username"), display_name=auth.get("display_name"),
                action=action, category=category, target=target, method=request.method, path=path, status_code=response.status_code,
            )
        except Exception:
            logging.exception("Aktivitätsprotokoll konnte nicht geschrieben werden")
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self' data:; object-src 'none'; frame-src 'none'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if _request_is_https(request):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.url.path in {"/login","/setup","/security"} or request.url.path.startswith("/api/") or not _is_public_path(request.url.path):
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("Pragma", "no-cache")
            response.headers.setdefault("Expires", "0")
    return response


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if db.system_user_count() > 0:
        return RedirectResponse(url="/login", status_code=303)
    return render(request, "setup.html", page="auth")


@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(request: Request, username: str = Form(...), display_name: str = Form(...), password: str = Form(...), password_repeat: str = Form(...)):
    if db.system_user_count() > 0:
        return RedirectResponse(url="/login", status_code=303)
    if password != password_repeat:
        return render(request, "setup.html", page="auth", error="Die Passwörter stimmen nicht überein.", username=username, display_name=display_name)
    try:
        user_id=db.create_system_user(username, display_name, _password_hash(password), role="admin", active=True)
    except ValueError as exc:
        return render(request, "setup.html", page="auth", error=str(exc), username=username, display_name=display_name)
    token=secrets.token_urlsafe(32); expires=datetime.now(timezone.utc)+timedelta(hours=SESSION_HOURS)
    db.create_system_session(_session_hash(token), user_id, expires.isoformat()); db.mark_system_login(user_id)
    db.add_activity(system_user_id=user_id, username=username, display_name=display_name, action="Ersteinrichtung abgeschlossen", category="Zugriff", target="Erster Administrator")
    response=RedirectResponse(url="/welcome", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_HOURS*3600, httponly=True, samesite="lax", secure=_cookie_secure(request), path="/")
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", reason: str | None = None):
    if db.system_user_count() == 0:
        return RedirectResponse(url="/setup", status_code=303)
    token=request.cookies.get(SESSION_COOKIE)
    if token and db.system_user_for_session(_session_hash(token)):
        if db.get_setting("community_onboarding_completed","0")!="1":
            return RedirectResponse(url="/welcome", status_code=303)
        return RedirectResponse(url="/", status_code=303)
    return render(request, "login.html", page="auth", next_path=next if next.startswith("/") and not next.startswith("//") else "/", login_reason=reason)


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    username_key=str(username or "").strip().casefold()
    ip_hash=_client_hash(request)
    failures=db.web_login_failures(ip_hash,username_key,WEB_FAILURE_WINDOW_MINUTES)
    if failures["pair"] >= WEB_MAX_FAILURES or failures["ip"] >= WEB_MAX_FAILURES*3:
        db.add_security_event("Web login rate limited",severity="warning",category="web_login",username=str(username or "").strip() or None,remote=_client_text(request),success=False,detail=f"window={WEB_FAILURE_WINDOW_MINUTES}m")
        asyncio.create_task(_send_admin_mail("security_warning","security_warnings",{"body":"Die Web-Anmeldung wurde wegen zu vieler Fehlversuche begrenzt.","detail":f"Benutzer: {str(username or '').strip() or 'unbekannt'} · Quelle: {_client_text(request)}"},cooldown_minutes=60,base_url=str(request.base_url).rstrip("/")))
        return render(request,"login.html",status_code=429,page="auth",error=f"Zu viele Fehlversuche. Bitte in {WEB_FAILURE_WINDOW_MINUTES} Minuten erneut versuchen.",username=username,next_path=next if next.startswith("/") and not next.startswith("//") else "/")
    account=db.get_system_user_auth(username)
    stored_hash=(account or {}).get("password_hash") or _DUMMY_PASSWORD_HASH
    password_valid=_password_ok(password, stored_hash)
    if not account or not int(account.get("active") or 0) or not password_valid:
        db.record_web_login_attempt(ip_hash,username_key,False)
        db.add_security_event("Web login failed",severity="warning",category="web_login",username=str(username or "").strip() or None,remote=_client_text(request),success=False,detail="Ungültige Zugangsdaten oder inaktives Konto")
        return render(request, "login.html", page="auth", error="Benutzername oder Passwort ist nicht korrekt.", username=username, next_path=next if next.startswith("/") and not next.startswith("//") else "/")
    db.record_web_login_attempt(ip_hash,username_key,True)
    if _password_needs_rehash(stored_hash):
        try:
            db.update_system_user(account["id"],account["username"],account["display_name"],account["role"],bool(account["active"]),_password_hash(password))
        except Exception:
            logging.exception("Passwort-Hash konnte nicht automatisch aktualisiert werden")
    target=next if next.startswith("/") and not next.startswith("//") else "/"
    if int(account.get("totp_enabled") or 0):
        factor_failures=db.system_2fa_failures(account["id"],ip_hash,TWO_FACTOR_RATE_WINDOW_MINUTES)
        if factor_failures["pair"] >= TWO_FACTOR_MAX_FAILURES or factor_failures["user"] >= TWO_FACTOR_MAX_FAILURES*3:
            db.add_security_event("Web 2FA rate limited",severity="warning",category="web_login",system_user_id=account.get("id"),username=account.get("username"),remote=_client_text(request),success=False,detail=f"window={TWO_FACTOR_RATE_WINDOW_MINUTES}m")
            return render(request,"login.html",status_code=429,page="auth",error=f"Zu viele 2FA-Fehlversuche. Bitte in {TWO_FACTOR_RATE_WINDOW_MINUTES} Minuten erneut versuchen.",username=username,next_path=target)
        challenge=secrets.token_urlsafe(32)
        challenge_expires=datetime.now(timezone.utc)+timedelta(minutes=TWO_FACTOR_CHALLENGE_MINUTES)
        db.create_system_2fa_challenge(_session_hash(challenge),account["id"],challenge_expires.isoformat(),target)
        db.add_security_event("Web password accepted; 2FA required",severity="info",category="web_login",system_user_id=account.get("id"),username=account.get("username"),remote=_client_text(request),success=True)
        response=RedirectResponse(url="/login/2fa",status_code=303)
        response.set_cookie(TWO_FACTOR_COOKIE,challenge,max_age=TWO_FACTOR_CHALLENGE_MINUTES*60,httponly=True,samesite="strict",secure=_cookie_secure(request),path="/")
        return response
    token=secrets.token_urlsafe(32); expires=datetime.now(timezone.utc)+timedelta(hours=SESSION_HOURS)
    db.create_system_session(_session_hash(token), account["id"], expires.isoformat()); db.mark_system_login(account["id"])
    db.add_activity(system_user_id=account.get("id"), username=account.get("username"), display_name=account.get("display_name"), action="Anmeldung", category="Zugriff", target="Weboberfläche")
    db.add_security_event("Web login successful",severity="info",category="web_login",system_user_id=account.get("id"),username=account.get("username"),remote=_client_text(request),success=True)
    response=RedirectResponse(url=target, status_code=303)
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_HOURS*3600, httponly=True, samesite="lax", secure=_cookie_secure(request), path="/")
    return response


@app.get("/login/2fa", response_class=HTMLResponse)
async def login_two_factor_page(request: Request):
    raw=request.cookies.get(TWO_FACTOR_COOKIE)
    challenge=db.system_2fa_challenge(_session_hash(raw)) if raw else None
    if not challenge:
        response=RedirectResponse(url="/login?reason=expired",status_code=303)
        response.delete_cookie(TWO_FACTOR_COOKIE,path="/")
        return response
    return render(request,"login_2fa.html",page="auth",display_name=challenge.get("display_name") or challenge.get("username"),error=None)


@app.post("/login/2fa", response_class=HTMLResponse)
async def login_two_factor_submit(request: Request, code: str = Form(...)):
    raw=request.cookies.get(TWO_FACTOR_COOKIE)
    challenge=db.system_2fa_challenge(_session_hash(raw)) if raw else None
    if not challenge:
        response=RedirectResponse(url="/login?reason=expired",status_code=303)
        response.delete_cookie(TWO_FACTOR_COOKIE,path="/")
        return response
    ip_hash=_client_hash(request)
    failures=db.system_2fa_failures(challenge["user_id"],ip_hash,TWO_FACTOR_RATE_WINDOW_MINUTES)
    if int(challenge.get("attempts") or 0)>=TWO_FACTOR_MAX_FAILURES or failures["pair"]>=TWO_FACTOR_MAX_FAILURES or failures["user"]>=TWO_FACTOR_MAX_FAILURES*3:
        db.delete_system_2fa_challenge(_session_hash(raw))
        db.add_security_event("Web 2FA rate limited",severity="warning",category="web_login",system_user_id=challenge.get("user_id"),username=challenge.get("username"),remote=_client_text(request),success=False,detail=f"window={TWO_FACTOR_RATE_WINDOW_MINUTES}m")
        response=render(request,"login_2fa.html",status_code=429,page="auth",display_name=challenge.get("display_name") or challenge.get("username"),error=f"Zu viele Fehlversuche. Bitte in {TWO_FACTOR_RATE_WINDOW_MINUTES} Minuten erneut anmelden.")
        response.delete_cookie(TWO_FACTOR_COOKIE,path="/")
        return response
    value=str(code or "").strip()
    method=None
    matched_counter=totp.verify_code(challenge.get("totp_secret") or "",value,window=1)
    if matched_counter is not None and db.accept_system_totp_counter(challenge["user_id"],matched_counter):
        method="totp"
    elif len(totp.normalize_recovery_code(value))>=10 and db.consume_system_recovery_code(challenge["user_id"],totp.recovery_code_hash(value)):
        method="recovery"
    if not method:
        attempts=db.record_system_2fa_challenge_failure(_session_hash(raw))
        db.record_system_2fa_attempt(challenge["user_id"],ip_hash,False)
        db.add_security_event("Web 2FA failed",severity="warning",category="web_login",system_user_id=challenge.get("user_id"),username=challenge.get("username"),remote=_client_text(request),success=False,detail=f"attempt={attempts}")
        response=render(request,"login_2fa.html",status_code=401,page="auth",display_name=challenge.get("display_name") or challenge.get("username"),error="Der Sicherheitscode ist nicht korrekt.")
        if attempts>=TWO_FACTOR_MAX_FAILURES:
            db.delete_system_2fa_challenge(_session_hash(raw))
            response.delete_cookie(TWO_FACTOR_COOKIE,path="/")
        return response
    db.record_system_2fa_attempt(challenge["user_id"],ip_hash,True)
    db.delete_system_2fa_challenge(_session_hash(raw))
    token=secrets.token_urlsafe(32); expires=datetime.now(timezone.utc)+timedelta(hours=SESSION_HOURS)
    db.create_system_session(_session_hash(token),challenge["user_id"],expires.isoformat()); db.mark_system_login(challenge["user_id"])
    db.add_activity(system_user_id=challenge.get("user_id"),username=challenge.get("username"),display_name=challenge.get("display_name"),action="Anmeldung",category="Zugriff",target="Weboberfläche")
    db.add_security_event("Web login successful with 2FA",severity="info",category="web_login",system_user_id=challenge.get("user_id"),username=challenge.get("username"),remote=_client_text(request),success=True,detail=f"factor={method}")
    response=RedirectResponse(url=challenge.get("next_path") or "/",status_code=303)
    response.set_cookie(SESSION_COOKIE,token,max_age=SESSION_HOURS*3600,httponly=True,samesite="lax",secure=_cookie_secure(request),path="/")
    response.delete_cookie(TWO_FACTOR_COOKIE,path="/")
    return response


@app.post("/logout")
async def logout(request: Request):
    token=request.cookies.get(SESSION_COOKIE)
    auth=getattr(request.state, "auth_user", None) or {}
    if auth:
        db.add_activity(system_user_id=auth.get("id"), username=auth.get("username"), display_name=auth.get("display_name"), action="Abmeldung", category="Zugriff", target="Weboberfläche")
    if token: db.delete_system_session(_session_hash(token))
    response=RedirectResponse(url="/login?reason=logout", status_code=303)
    response.delete_cookie(SESSION_COOKIE,path="/")
    return response



def _community_onboarding_state():
    allowance_enabled=str(db.get_setting("community_free_allowance_enabled","0") or "0")=="1"
    try:
        allowance_kwh=max(0.0,float(db.get_setting("community_free_allowance_kwh","0") or 0))
    except (TypeError,ValueError):
        allowance_kwh=0.0
    return {
        "completed":db.get_setting("community_onboarding_completed","0")=="1",
        "branding":db.branding_settings(),
        "mail":mailer.settings(),
        "allowance_enabled":allowance_enabled,
        "allowance_kwh":allowance_kwh,
        "invite_counts":db.community_invite_counts(),
    }


@app.get("/welcome", response_class=HTMLResponse)
async def community_welcome_page(request:Request):
    state=_community_onboarding_state()
    if state["completed"]:
        return RedirectResponse(url="/",status_code=303)
    return render(request,"community_welcome.html",page="welcome",onboarding=state)


@app.post("/welcome", response_class=HTMLResponse)
async def community_welcome_submit(
    request:Request,
    organization_name:str=Form(...),
    display_name:str=Form(...),
    primary_color:str=Form("#146af5"),
    price_eur_kwh:str=Form(""),
    allowance_enabled:str|None=Form(None),
    allowance_kwh:str=Form(""),
    smtp_enabled:str|None=Form(None),
    smtp_host:str=Form(""),
    smtp_port:int=Form(587),
    smtp_security:str=Form("starttls"),
    smtp_username:str=Form(""),
    smtp_password:str=Form(""),
    smtp_from_email:str=Form(""),
    smtp_from_name:str=Form(""),
    invite_emails:str=Form(""),
):
    org=str(organization_name or "").strip()
    display=str(display_name or "").strip()
    color=str(primary_color or "").strip().lower()
    if not org or not display:
        return render(request,"community_welcome.html",status_code=400,page="welcome",onboarding=_community_onboarding_state(),error="Organisation und Anzeigename sind erforderlich.")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}",color):
        return render(request,"community_welcome.html",status_code=400,page="welcome",onboarding=_community_onboarding_state(),error="Bitte eine gültige Akzentfarbe auswählen.")

    emails=[]
    for value in re.split(r"[,;\\n]+",str(invite_emails or "")):
        value=value.strip().lower()
        if value and value not in emails:
            emails.append(value)
    if any(not re.fullmatch(r"[^\\s@]+@[^\\s@]+\\.[^\\s@]+",email) for email in emails):
        return render(request,"community_welcome.html",status_code=400,page="welcome",onboarding=_community_onboarding_state(),error="Mindestens eine Einladungs-E-Mail-Adresse ist ungültig.")

    mail_payload={
        "enabled":bool(smtp_enabled),
        "host":smtp_host,"port":smtp_port,"security":smtp_security,
        "username":smtp_username,"password":smtp_password or None,
        "from_email":smtp_from_email,"from_name":smtp_from_name or display,
        "admin_recipients":smtp_from_email if smtp_from_email else "",
        "public_base_url":str(request.base_url).rstrip("/"),
        "event_backup_failures":True,"event_security_warnings":False,
    }
    if emails and not smtp_enabled:
        return render(request,"community_welcome.html",status_code=400,page="welcome",onboarding=_community_onboarding_state(),error="Für Benutzer-Einladungen muss SMTP aktiviert sein.")

    try:
        mailer.save_settings(mail_payload)
    except (ValueError,RuntimeError) as exc:
        return render(request,"community_welcome.html",status_code=400,page="welcome",onboarding=_community_onboarding_state(),error=str(exc))

    db.set_setting("branding_product_name","VoltCore Community")
    db.set_setting("branding_organization_name",org[:100])
    db.set_setting("branding_display_name",display[:80])
    db.set_setting("branding_product_subtitle","Community Edition · OCPP Charging Management")
    db.set_setting("branding_primary_color",color)

    allowance=bool(allowance_enabled)
    try:
        allowance_value=max(0.0,float(str(allowance_kwh or "0").replace(",",".")))
    except ValueError:
        allowance_value=0.0
    if allowance and allowance_value<=0:
        return render(request,"community_welcome.html",status_code=400,page="welcome",onboarding=_community_onboarding_state(),error="Für ein aktiviertes Freikontingent bitte einen Wert größer 0 kWh angeben.")
    db.set_setting("community_free_allowance_enabled","1" if allowance else "0")
    db.set_setting("community_free_allowance_kwh",str(allowance_value if allowance else 0))

    price_text=str(price_eur_kwh or "").strip().replace(",",".")
    if price_text:
        try:
            price=max(0.0,float(price_text))
        except ValueError:
            return render(request,"community_welcome.html",status_code=400,page="welcome",onboarding=_community_onboarding_state(),error="Der Strompreis ist ungültig.")
        cents=int(round(price*100))
        active_global=[x for x in db.list_tariffs() if x.get("scope")=="global" and int(x.get("active") or 0)]
        if not active_global:
            db.create_tariff("Standardtarif","global",None,cents,datetime.now(timezone.utc).replace(microsecond=0).isoformat(),None,None,None)

    invite_errors=[]
    for email in emails:
        raw=secrets.token_urlsafe(32)
        token_hash=_session_hash(raw)
        expires=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()
        db.create_community_invite(email,token_hash,expires)
        invite_url=str(request.base_url).rstrip("/")+"/invite/"+raw
        try:
            await asyncio.to_thread(mailer.send_template,"user_invite",email,{"invite_url":invite_url},str(request.base_url).rstrip("/"))
        except Exception as exc:
            logging.exception("Community invitation mail failed for %s",email)
            invite_errors.append(email)

    if invite_errors:
        return render(
            request,"community_welcome.html",status_code=502,page="welcome",
            onboarding=_community_onboarding_state(),
            error="Einladungen konnten nicht an alle Adressen gesendet werden: "+", ".join(invite_errors)+". Bitte SMTP prüfen und erneut versuchen."
        )

    db.set_setting("community_onboarding_completed","1")
    auth=getattr(request.state,"auth_user",None) or {}
    db.add_activity(
        system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),
        action="Community-Ersteinrichtung abgeschlossen",category="System",target=org,
        details=f"SMTP={'aktiv' if smtp_enabled else 'aus'} · Standardpreis={'gesetzt' if price_text else 'übersprungen'} · Freikontingent={'aktiv' if allowance else 'aus'} · Einladungen={len(emails)}"
    )
    return RedirectResponse(url="/",status_code=303)


@app.get("/invite/{token}", response_class=HTMLResponse)
async def community_invite_page(request:Request,token:str):
    invite=db.community_invite(_session_hash(token))
    return render(request,"community_invite.html",page="public",invite=invite,token=token if invite else "",accepted=False)


@app.post("/invite/{token}", response_class=HTMLResponse)
async def community_invite_accept(request:Request,token:str,name:str=Form(...),phone:str=Form("")):
    invite=db.community_invite(_session_hash(token))
    if not invite:
        return render(request,"community_invite.html",status_code=400,page="public",invite=None,token="",accepted=False,error="Diese Einladung ist ungültig, abgelaufen oder wurde bereits verwendet.")
    clean_name=str(name or "").strip()
    if not clean_name:
        return render(request,"community_invite.html",status_code=400,page="public",invite=invite,token=token,accepted=False,error="Bitte einen Namen angeben.")
    existing=db.get_user_by_email(invite["email"])
    if existing:
        user_id=int(existing["id"])
    else:
        allowance_enabled=str(db.get_setting("community_free_allowance_enabled","0") or "0")=="1"
        allowance=float(db.get_setting("community_free_allowance_kwh","0") or 0) if allowance_enabled else None
        user_id=db.create_user(
            clean_name,role="Fahrer",email=invite["email"],phone=str(phone or "").strip() or None,
            status="Aktiv",monthly_kwh_limit=allowance,monthly_limit_mode="warn",
            gamification_enabled=False,weekly_hours=None,budget_source="manual",
        )
    db.mark_community_invite_accepted(invite["id"],user_id)
    return render(request,"community_invite.html",page="public",invite=invite,token="",accepted=True,user=db.get_user(user_id))


@app.get("/system-users", response_class=HTMLResponse)
async def system_users_page(request: Request):
    return render(request, "system_users.html", page="system-users")


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request):
    return render(request, "activity.html", page="activity")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    auth=getattr(request.state,"auth_user",None) or {}
    if auth.get("role")=="admin" and db.get_setting("community_onboarding_completed","0")!="1":
        return RedirectResponse(url="/welcome",status_code=303)
    return render(request, "dashboard.html", page="dashboard")


@app.get("/charge-points", response_class=HTMLResponse)
async def charge_points_page(request: Request):
    return render(request, "charge_points.html", page="charge-points")


@app.get("/charge-points/{cp_id}", response_class=HTMLResponse)
async def charge_point_page(request: Request, cp_id: str):
    if not db.get_charge_point(cp_id):
        raise HTTPException(404, "Ladepunkt nicht gefunden")
    return render(request, "charge_point.html", page="charge-points", cp_id=cp_id)


@app.get("/ocpp-devices", response_class=HTMLResponse)
async def ocpp_devices_page(request: Request):
    # V0.8.8.4: discovery is part of the Ladepunkte workspace. Keep the old URL
    # as a compatibility redirect for bookmarks and historic audit links.
    return RedirectResponse(url="/charge-points#discovery", status_code=303)

@app.get("/ocpp-monitor", response_class=HTMLResponse)
async def ocpp_monitor_page(request: Request):
    return render(request, "ocpp_monitor.html", page="ocpp-monitor")


@app.get("/liveview", response_class=HTMLResponse)
async def liveview_page(request: Request, preview: str | None = None):
    # Deliberately uses a dedicated, chrome-free template for wall displays.
    preview=str(preview or "").strip().lower()
    if preview not in db.LIVEVIEW_PRESETS:
        preview=""
    return render(request, "liveview.html", page="liveview", liveview_settings=db.liveview_settings(), liveview_preview=preview)

@app.get("/ocpp-devices/{cp_id}/detail", response_class=HTMLResponse)
async def ocpp_device_detail_page(request: Request, cp_id: str):
    cp_id = unquote(cp_id)
    if not db.get_charge_point(cp_id):
        raise HTTPException(404, "Ladepunkt nicht gefunden")
    from urllib.parse import quote
    return RedirectResponse(url=f"/charge-points/{quote(cp_id, safe='')}?tab=diagnostics", status_code=303)

@app.get("/transactions", response_class=HTMLResponse)
async def transactions_page(request: Request):
    return render(request, "transactions.html", page="transactions")


@app.get("/vehicles", response_class=HTMLResponse)
async def vehicles_page(request: Request):
    return render(request, "vehicles.html", page="vehicles")


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    return render(request, "users.html", page="users")


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    return render(request, "reports.html", page="reports")


@app.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request):
    return render(request, "backups.html", page="backups")


class BackupSettingsPayload(BaseModel):
    schedule_enabled: bool = False
    frequency: str = "daily"
    time: str = "03:00"
    weekday: int = 0
    retention_days: int = 30
    internal_enabled: bool = True
    external_enabled: bool = False
    external_protocol: str = "ftps"
    external_host: str = ""
    external_port: int | None = None
    external_username: str = ""
    external_path: str = "/"
    external_tls_verify: bool = True
    external_password: str | None = None
    clear_external_password: bool = False


@app.get("/api/backups")
async def api_backups():
    cfg=backup.settings()
    return {"settings":cfg,"items":await asyncio.to_thread(backup.list_backups)}


@app.put("/api/backups/settings")
async def api_backup_settings(payload: BackupSettingsPayload):
    try:
        return {"ok":True,"settings":backup.save_settings(payload.model_dump())}
    except ValueError as exc:
        raise HTTPException(400,str(exc))


@app.post("/api/backups/create")
async def api_backup_create(request: Request):
    try:
        result=await asyncio.to_thread(backup.run_backup,"manual")
        return result
    except Exception as exc:
        logging.exception("Manual backup failed")
        db.create_notification(f"backup:manual:failed:{int(datetime.now(timezone.utc).timestamp())}","critical","Manuelles Backup fehlgeschlagen",f"{type(exc).__name__}: {exc}","/backups",audience="admin")
        await _send_admin_mail("backup_failed","backup_failures",{"detail":f"{type(exc).__name__}: {exc}"},cooldown_minutes=60,base_url=str(request.base_url).rstrip("/"))
        raise HTTPException(500,f"Backup fehlgeschlagen: {type(exc).__name__}: {exc}")


@app.post("/api/backups/test-external")
async def api_backup_test_external():
    try:
        return await asyncio.to_thread(backup.test_external)
    except Exception as exc:
        raise HTTPException(400,f"Verbindung fehlgeschlagen: {type(exc).__name__}: {exc}")


@app.get("/api/backups/{filename}/download")
async def api_backup_download(filename: str):
    try:
        path=backup.backup_path(filename)
    except (ValueError,FileNotFoundError):
        raise HTTPException(404,"Backup nicht gefunden.")
    return FileResponse(path,media_type="application/zip",filename=path.name,headers={"Cache-Control":"no-store"})


@app.delete("/api/backups/{filename}")
async def api_backup_delete(filename: str):
    try:
        backup.delete_backup(filename)
        return {"ok":True}
    except (ValueError,FileNotFoundError):
        raise HTTPException(404,"Backup nicht gefunden.")


@app.post("/api/backups/{filename}/restore")
async def api_backup_restore(request: Request, filename: str):
    try:
        path=backup.backup_path(filename)
        result=await asyncio.to_thread(backup.restore_backup,path)
        return result
    except (ValueError,FileNotFoundError) as exc:
        raise HTTPException(400,str(exc))
    except Exception as exc:
        logging.exception("Backup restore failed")
        raise HTTPException(500,f"Wiederherstellung fehlgeschlagen: {type(exc).__name__}: {exc}")


@app.post("/api/backups/restore-upload")
async def api_backup_restore_upload(request: Request, file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400,"Bitte eine OCPP-Backup-ZIP auswählen.")
    tmp=backup.BACKUP_DIR/("restore-upload-"+secrets.token_hex(8)+".zip")
    try:
        total=0
        with tmp.open("wb") as fh:
            while chunk:=await file.read(1024*1024):
                total+=len(chunk)
                if total>backup.MAX_RESTORE_BYTES:
                    raise HTTPException(413,"Backup-Datei ist zu groß.")
                fh.write(chunk)
        result=await asyncio.to_thread(backup.restore_backup,tmp)
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    finally:
        tmp.unlink(missing_ok=True)


@app.get("/updates", response_class=HTMLResponse)
async def updates_page(request: Request):
    return render(request, "updates.html", page="updates", update_repository=updates.REPOSITORY)


class UpdateSettingsPayload(BaseModel):
    check_enabled: bool = True
    github_token: str | None = None
    clear_github_token: bool = False
    portainer_webhook: str | None = None
    clear_portainer_webhook: bool = False
    portainer_tls_verify: bool = True


class UpdateInstallPayload(BaseModel):
    target_version: str


@app.get("/api/updates/settings")
async def api_update_settings():
    return updates.settings()


@app.put("/api/updates/settings")
async def api_save_update_settings(payload: UpdateSettingsPayload):
    try:
        return updates.save_settings(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400,str(exc))


@app.get("/api/updates/status")
async def api_update_status(force: bool=False):
    try:
        return await asyncio.to_thread(updates.check_latest,APP_VERSION,bool(force))
    except RuntimeError as exc:
        raise HTTPException(502,str(exc))
    except Exception as exc:
        logging.exception("Update-Prüfung fehlgeschlagen")
        raise HTTPException(502,f"Update-Prüfung fehlgeschlagen: {type(exc).__name__}")


@app.post("/api/updates/install",status_code=202)
async def api_install_update(payload: UpdateInstallPayload, request: Request, background_tasks: BackgroundTasks):
    try:
        status=await asyncio.to_thread(updates.check_latest,APP_VERSION,True)
    except Exception as exc:
        raise HTTPException(502,f"Release-Prüfung fehlgeschlagen: {exc}")
    target=str(payload.target_version or "").strip().removeprefix("v")
    if not status.get("update_available"):
        raise HTTPException(409,"Es ist kein neueres freigegebenes Update verfügbar.")
    if target != str(status.get("latest_version") or ""):
        raise HTTPException(409,"Die angeforderte Version entspricht nicht dem neuesten freigegebenen Release.")
    cfg=updates.settings()
    if not cfg.get("portainer_webhook_configured"):
        raise HTTPException(409,"Für die Ein-Klick-Installation ist noch kein Portainer-Stack-Webhook hinterlegt.")
    try:
        pre=await asyncio.to_thread(backup.create_backup,f"pre-update-v{target}",True)
    except Exception as exc:
        logging.exception("Pre-Update-Backup fehlgeschlagen")
        raise HTTPException(500,f"Update abgebrochen: Pre-Update-Backup fehlgeschlagen ({type(exc).__name__}).")
    updates.mark_pending(target,pre.get("filename"))
    auth=request.state.auth_user or {}
    db.add_security_event("System update requested",severity="info",category="admin",system_user_id=auth.get("id"),username=auth.get("username"),remote=_client_text(request),success=True,detail=f"target={target}; backup={pre.get('filename')}")
    background_tasks.add_task(updates.trigger_and_record,target)
    return {"ok":True,"target_version":target,"backup":pre,"message":"Pre-Update-Backup erstellt; Portainer-Redeploy wird gestartet."}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return render(request, "settings.html", page="settings")


@app.get("/manifest.webmanifest")
async def pwa_manifest():
    branding=db.branding_settings()
    payload={
        "name":f"{branding.get('display_name')} · {branding.get('product_name')}",
        "short_name":str(branding.get('display_name') or branding.get('product_name') or 'OCPP')[:30],
        "description":branding.get('product_subtitle') or 'OCPP Ladeinfrastruktur',
        "id":"/",
        "start_url":"/",
        "scope":"/",
        "display":"standalone",
        "background_color":"#111827",
        "theme_color":branding.get('primary_color') or '#2563eb',
        "icons":[
            {"src":"/static/pwa-icon-192.png","sizes":"192x192","type":"image/png","purpose":"any maskable"},
            {"src":"/static/pwa-icon-512.png","sizes":"512x512","type":"image/png","purpose":"any maskable"},
        ],
    }
    return Response(json.dumps(payload,ensure_ascii=False),media_type="application/manifest+json",headers={"Cache-Control":"no-cache"})


@app.get("/service-worker.js")
async def pwa_service_worker():
    return FileResponse(BASE_DIR/"static"/"service-worker.js",media_type="application/javascript",headers={"Cache-Control":"no-cache","Service-Worker-Allowed":"/"})


class LiveviewSettingsPayload(BaseModel):
    preset: str = "standard"
    sort: str = "auto"
    columns: str = "auto"
    refresh_seconds: int = 2
    show_vehicle: bool = True
    show_user: bool = True
    show_soc: bool = True
    show_energy: bool = True
    show_diagnostics: bool = True
    show_clock: bool = True
    show_technical_id: bool = True


@app.get("/api/settings/liveview")
async def api_liveview_settings():
    return db.liveview_settings()


@app.put("/api/settings/liveview")
async def api_liveview_settings_save(payload: LiveviewSettingsPayload, request: Request):
    try:
        result=db.save_liveview_settings(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    auth=getattr(request.state,"auth_user",None) or {}
    db.add_activity(system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),action="LiveView-Einstellungen gespeichert",category="Einstellungen",target="LiveView / Kiosk",method=request.method,path=request.url.path,details=f"preset={result.get('preset')}; sort={result.get('sort')}; columns={result.get('columns')}; refresh={result.get('refresh_seconds')}s")
    return {"ok":True,**result}


class MailSettingsPayload(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 587
    security: str = "starttls"
    username: str = ""
    password: str | None = None
    clear_password: bool = False
    from_email: str = ""
    from_name: str = ""
    admin_recipients: str = ""
    public_base_url: str = ""
    event_backup_failures: bool = True
    event_security_warnings: bool = False


class MailTestPayload(BaseModel):
    recipient: str
    template: str = "system"


@app.get("/api/settings/mail")
async def api_mail_settings():
    return mailer.settings()


@app.put("/api/settings/mail")
async def api_mail_settings_save(payload:MailSettingsPayload):
    try:
        return {"ok":True,"settings":mailer.save_settings(payload.model_dump())}
    except ValueError as exc:
        raise HTTPException(400,str(exc))


@app.post("/api/settings/mail/test")
async def api_mail_test(payload:MailTestPayload, request:Request):
    if payload.template not in mailer.TEMPLATE_LABELS:
        raise HTTPException(400,"Unbekannte Mailvorlage")
    base=mailer.settings(False).get("public_base_url") or str(request.base_url).rstrip("/")
    sample={
        "name":"Max Mustermann",
        "admin_url":base+"/users",
        "detail":"Dies ist eine Testnachricht aus den E-Mail-Einstellungen. Es wurde keine echte Aktion ausgelöst.",
        "subject":"Test-Systemmeldung",
        "headline":"E-Mail-System erfolgreich getestet",
        "body":"Diese Nachricht zeigt die aktuell konfigurierte White-Label-Mailvorlage.",
        "cta_label":"Backend öffnen",
        "cta_url":base+"/",
    }
    try:
        result=await asyncio.to_thread(mailer.send_template,payload.template,payload.recipient,sample,base,True)
        db.set_setting("mail_last_test_at",datetime.now(timezone.utc).isoformat())
        db.set_setting("mail_last_test_error","")
        return result
    except (ValueError,RuntimeError) as exc:
        db.set_setting("mail_last_test_error",str(exc)[:500])
        raise HTTPException(400,str(exc))
    except Exception as exc:
        logging.exception("SMTP test mail failed")
        db.set_setting("mail_last_test_error",f"{type(exc).__name__}: {exc}"[:500])
        raise HTTPException(502,f"Testmail konnte nicht versendet werden: {type(exc).__name__}: {exc}")


@app.get("/api/settings/branding")
async def api_branding_settings():
    return db.branding_settings()


def _branding_color(value: str) -> str:
    value=str(value or "").strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        raise HTTPException(400, "Die Akzentfarbe muss als Hex-Farbe angegeben werden, z. B. #2563eb.")
    return value.lower()


def _branding_text(value: str, fallback: str, max_length: int=80) -> str:
    value=" ".join(str(value or "").strip().split())
    if not value:
        return fallback
    return value[:max_length]


async def _save_branding_asset(upload: UploadFile | None, kind: str) -> str | None:
    if upload is None or not upload.filename:
        return None
    allowed={"image/png":"png","image/jpeg":"jpg","image/webp":"webp"}
    ext=allowed.get((upload.content_type or "").lower())
    if not ext:
        raise HTTPException(400, f"{kind}: Erlaubt sind PNG, JPG und WebP.")
    content=await upload.read(2*1024*1024+1)
    if len(content)>2*1024*1024:
        raise HTTPException(400, f"{kind}: Die Datei darf maximal 2 MB groß sein.")
    for old in BRANDING_DIR.glob(f"{kind}.*"):
        old.unlink(missing_ok=True)
    target=BRANDING_DIR/f"{kind}.{ext}"
    target.write_bytes(content)
    return f"/branding/{target.name}"


@app.post("/api/settings/branding")
async def api_save_branding(
    request: Request,
    product_name: str = Form(...), organization_name: str = Form(...),
    display_name: str = Form(...), product_subtitle: str = Form(...),
    primary_color: str = Form(...),
    logo_light: UploadFile | None = File(None), logo_dark: UploadFile | None = File(None),
    favicon: UploadFile | None = File(None),
    remove_logo_light: str = Form("0"), remove_logo_dark: str = Form("0"),
    remove_favicon: str = Form("0"),
):
    current=db.branding_settings()
    values={
        "branding_product_name":_branding_text(product_name,current["product_name"],80),
        "branding_organization_name":_branding_text(organization_name,current["organization_name"],100),
        "branding_display_name":_branding_text(display_name,current["display_name"],80),
        "branding_product_subtitle":_branding_text(product_subtitle,current["product_subtitle"],100),
        "branding_primary_color":_branding_color(primary_color),
    }
    asset_fields=(
        ("logo_light",logo_light,remove_logo_light),("logo_dark",logo_dark,remove_logo_dark),
        ("favicon",favicon,remove_favicon),
    )
    for kind,upload,remove in asset_fields:
        key=f"branding_{kind}_url"
        if str(remove).lower() in {"1","true","on","yes"}:
            for old in BRANDING_DIR.glob(f"{kind}.*"): old.unlink(missing_ok=True)
            values[key]=""
        saved=await _save_branding_asset(upload,kind)
        if saved: values[key]=saved
    for key,value in values.items(): db.set_setting(key,value)
    auth=request.state.auth_user or {}
    db.add_activity(system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),action="Branding geändert",category="System",target="Branding & Erscheinungsbild")
    return {"ok":True,**db.branding_settings()}


@app.get("/security", response_class=HTMLResponse)
async def security_page(request: Request):
    return render(request, "security.html", page="security")

@app.get("/tariffs", response_class=HTMLResponse)
async def tariffs_page(request: Request):
    return render(request, "tariffs.html", page="tariffs")


@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


def _latest_backup_status():
    cfg=backup.settings(False)

    def _parse_dt(value):
        if not value:
            return None
        try:
            dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError,ValueError):
            return None

    latest_at=_parse_dt(cfg.get("last_success_at"))
    if latest_at is None:
        latest_path=None
        try:
            candidates=[x for x in backup.BACKUP_DIR.glob(f"{backup.BACKUP_PREFIX}*.zip") if x.is_file()]
            latest_path=max(candidates,key=lambda x:x.stat().st_mtime) if candidates else None
        except OSError:
            latest_path=None
        if latest_path is not None:
            try:
                latest_at=datetime.fromtimestamp(latest_path.stat().st_mtime,timezone.utc)
            except OSError:
                latest_at=None

    age_hours=max(0.0,(datetime.now(timezone.utc)-latest_at).total_seconds()/3600) if latest_at else None
    scheduled=bool(cfg.get("schedule_enabled"))
    external_enabled=bool(cfg.get("external_enabled"))
    protocol=str(cfg.get("external_protocol") or "ftps").upper()
    targets=[]
    if cfg.get("internal_enabled"):
        targets.append("Intern")
    if external_enabled:
        targets.append(protocol)
    target_text=" + ".join(targets) or "Kein Ziel"

    external_success=_parse_dt(cfg.get("external_last_success_at"))
    external_error_at=_parse_dt(cfg.get("external_last_error_at"))
    external_error=bool(
        external_enabled and external_error_at and
        (external_success is None or external_error_at > external_success)
    )

    if scheduled:
        threshold=36.0 if cfg.get("frequency")=="daily" else 24.0*8
        if latest_at is None:
            if external_error:
                level="bad"; label="Backup fehlgeschlagen"; detail=f"{target_text} · letzte externe Übertragung fehlgeschlagen"
            else:
                level="warn"; label="Noch kein Backup"; detail=f"{target_text} · automatischer Zeitplan ist aktiv"
        elif age_hours is not None and age_hours>threshold:
            level="bad"; label="Backup überfällig"; detail=f"{target_text} · letztes Backup vor {int(age_hours)} Std."
        elif external_error:
            level="warn"; label="Extern fehlerhaft"; detail=f"{target_text} · letzte externe Übertragung fehlgeschlagen"
        elif external_enabled and external_success is None:
            level="warn"; label="Extern unbestätigt"; detail=f"{target_text} · noch keine erfolgreiche externe Übertragung"
        else:
            level="ok"; label="Aktuell"; detail=f"{target_text} · letztes Backup "+_local_text(latest_at.isoformat(),"%d.%m. %H:%M")
    elif latest_at is not None:
        if external_error:
            level="warn"; label="Extern fehlerhaft"; detail=f"{target_text} · letztes Backup "+_local_text(latest_at.isoformat(),"%d.%m. %H:%M")
        else:
            level="neutral"; label="Manuell"; detail=f"{target_text} · letztes Backup "+_local_text(latest_at.isoformat(),"%d.%m. %H:%M")
    else:
        level="neutral"; label="Nicht geplant"; detail=f"{target_text} · kein automatischer Zeitplan aktiv"

    return {
        "level":level,
        "label":label,
        "detail":detail,
        "latest_at":latest_at.isoformat() if latest_at else None,
        "scheduled":scheduled,
        "targets":target_text,
        "external_protocol":protocol if external_enabled else None,
        "external_last_success_at":external_success.isoformat() if external_success else None,
        "external_error":external_error,
    }


def _operational_state(total, online, faulted=0, offline_or_unavailable=None, *, now=None):
    total=max(0,int(total or 0)); online=max(0,int(online or 0)); faulted=max(0,int(faulted or 0))
    unavailable=max(0,int(offline_or_unavailable if offline_or_unavailable is not None else max(0,total-online)))
    current=now or datetime.now(timezone.utc)
    uptime=max(0,(current-APP_STARTED_AT).total_seconds())
    startup=bool(total>0 and online<total and uptime<OPERATIONAL_STARTUP_GRACE_SECONDS)
    if total==0:
        return {"level":"idle","label":"Noch keine Ladepunkte","detail":"Noch keine aktiven Ladepunkte eingerichtet","startup":False}
    if startup:
        return {"level":"starting","label":"Startphase","detail":f"Ladepunkte verbinden sich nach Backend-Neustart · {online}/{total} online","startup":True}
    if faulted>0:
        return {"level":"critical","label":"Störung","detail":f"{faulted} Ladepunkt(e) melden einen Fehlerzustand","startup":False}
    if online<=0:
        return {"level":"critical","label":"Störung","detail":f"0/{total} Ladepunkte erreichbar","startup":False}
    if online<total or unavailable>0:
        return {"level":"warning","label":"Teilbetrieb","detail":f"{online}/{total} Ladepunkte erreichbar","startup":False}
    return {"level":"ok","label":"Betriebsbereit","detail":"Alle verwalteten Ladepunkte sind erreichbar","startup":False}


def _ocpp_transport_status():
    cert=str(os.getenv("OCPP_TLS_CERTFILE") or "").strip()
    key=str(os.getenv("OCPP_TLS_KEYFILE") or "").strip()
    if cert and key:
        cert_ok=Path(cert).is_file(); key_ok=Path(key).is_file()
        if cert_ok and key_ok:
            return {"level":"ok","label":"WSS aktiv","detail":"Direktes OCPP-WSS mit TLS >= 1.2 aktiviert","affects_overall":False}
        missing=[]
        if not cert_ok: missing.append("Zertifikat")
        if not key_ok: missing.append("Schlüssel")
        return {"level":"bad","label":"TLS-Dateien fehlen","detail":"Direktes WSS konfiguriert, aber "+", ".join(missing)+" nicht gefunden","affects_overall":True}
    if cert or key:
        return {"level":"bad","label":"TLS unvollständig","detail":"OCPP_TLS_CERTFILE und OCPP_TLS_KEYFILE müssen gemeinsam gesetzt sein","affects_overall":True}
    return {"level":"neutral","label":"WS / Proxy","detail":"Direktes WSS deaktiviert · TLS kann am Reverse Proxy terminiert werden","affects_overall":False}


def _system_status_payload(auth=None):
    cps=db.list_charge_points()
    total=len(cps)
    online=sum(1 for cp in cps if cp.get("status") not in ("Offline","Unknown","Unavailable"))
    faulted=sum(1 for cp in cps if cp.get("status")=="Faulted")
    unavailable=sum(1 for cp in cps if cp.get("status") in ("Offline","Unknown","Unavailable"))
    operational=_operational_state(total,online,faulted,unavailable)
    if total==0:
        ocpp={"level":"neutral","label":"Keine Ladepunkte","detail":"Noch keine aktiven Ladepunkte eingerichtet"}
    elif operational["startup"]:
        ocpp={"level":"neutral","label":f"{online}/{total} · Startphase","detail":operational["detail"]}
    elif operational["level"]=="ok":
        ocpp={"level":"ok","label":f"{online}/{total} online","detail":"Alle aktiven Ladepunkte erreichbar"}
    elif operational["level"]=="warning":
        ocpp={"level":"warn","label":f"{online}/{total} online","detail":operational["detail"]}
    else:
        ocpp={"level":"bad","label":f"{online}/{total} online","detail":operational["detail"]}

    mail=mailer.settings()
    configured=bool(mail.get("host") and mail.get("from_email") and (not mail.get("username") or mail.get("password_configured")))
    last_test=mail.get("last_test_at")
    last_test_error=mail.get("last_test_error")
    if mail.get("enabled") and configured and last_test_error:
        mail_status={"level":"warn","label":"Prüfen","detail":"Letzter SMTP-Test ist fehlgeschlagen"}
    elif mail.get("enabled") and configured:
        mail_status={"level":"ok","label":"Aktiv","detail":"SMTP konfiguriert"+(" · zuletzt getestet "+_local_text(last_test,"%d.%m. %H:%M") if last_test else "")}
    elif mail.get("enabled"):
        mail_status={"level":"bad","label":"Unvollständig","detail":"SMTP-Host oder Absenderadresse fehlt"}
    else:
        mail_status={"level":"neutral","label":"Deaktiviert","detail":"Regulärer E-Mail-Versand ist ausgeschaltet"}

    backup_status=_latest_backup_status()
    services=[
        {"key":"backend","name":"Backend","level":"ok","label":"Online","detail":f"Version {APP_VERSION}","affects_overall":True},
        {"key":"ocpp","name":"OCPP","affects_overall":total>0,**ocpp},
        {"key":"mail","name":"E-Mail / SMTP","affects_overall":False,**mail_status},
        {"key":"backup","name":"Backup","affects_overall":bool(backup_status.get("scheduled")),**backup_status},
        {"key":"pwa","name":"PWA","level":"ok","label":"Bereit","detail":"Installierbare Web-App · Push-Benachrichtigungen verfügbar","affects_overall":False},
        {"key":"ocpp_transport","name":"OCPP-Transport",**_ocpp_transport_status()},
    ]
    if auth and auth.get("role")=="admin":
        sec=_security_dashboard_summary()
        failures=int(sec.get("web_failures_24h") or 0)+int(sec.get("ocpp_rejected_24h") or 0)
        sec_level="warn" if failures else ("ok" if int(sec.get("score") or 0)>=70 else "neutral")
        sec_label="Hinweise" if failures else sec.get("label") or "Status verfügbar"
        sec_detail=(f"{failures} abgewiesene/fehlgeschlagene Zugriffe in 24 h · " if failures else "")+f"Härtungsgrad {int(sec.get('score') or 0)} %"
        services.append({"key":"security","name":"Security","level":sec_level,"label":sec_label,"detail":sec_detail,"affects_overall":failures>0})
        upd=updates.cached_status(APP_VERSION)
        services.append({
            "key":"update","name":"Updates",
            "level":upd.get("level","neutral"),"label":upd.get("label","—"),"detail":upd.get("detail",""),
            "available":bool(upd.get("available")),"latest_version":upd.get("latest_version") or "",
            "affects_overall":False,
        })
    rank={"ok":0,"neutral":0,"warn":1,"bad":2}
    relevant=[x for x in services if x.get("affects_overall")]
    worst=max((rank.get(x.get("level"),0) for x in relevant),default=0)
    overall={0:("ok","System OK"),1:("warn","Teilbetrieb"),2:("bad","Störung")}[worst]
    update_service=next((x for x in services if x.get("key")=="update"),None)
    update_available=bool(update_service and update_service.get("available"))
    update_version=(str(update_service.get("latest_version") or "").strip() if update_available else "")
    return {
        "version":APP_VERSION,
        "overall":{
            "level":overall[0],
            "label":overall[1],
            "update_available":update_available,
            "update_version":update_version,
        },
        "services":services,
        "updated_at":datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/system-status")
async def api_system_status(request: Request):
    return _system_status_payload(getattr(request.state,"auth_user",None))


@app.get("/api/activity")
async def api_activity(limit: int = 100, category: str | None = None, q: str | None = None):
    return {"items": db.recent_activity(limit=limit, category=category or None, query=q or None)}


@app.get("/api/notifications")
async def api_notifications(request: Request, limit: int = 12):
    auth=request.state.auth_user or {}
    return db.notifications_for_user(int(auth["id"]), limit=limit)


@app.post("/api/notifications/read-all")
async def api_notifications_read_all(request: Request):
    auth=request.state.auth_user or {}
    return {"ok": True, "count": db.mark_all_notifications_read(int(auth["id"]))}


@app.delete("/api/notifications/read-all")
async def api_notifications_clear_read(request: Request):
    auth=request.state.auth_user or {}
    return {"ok":True,"count":db.dismiss_read_notifications(int(auth["id"]))}


@app.delete("/api/notifications/{notification_id}")
async def api_notification_dismiss(request: Request, notification_id: int):
    auth=request.state.auth_user or {}
    if not db.dismiss_notification(notification_id,int(auth["id"])):
        raise HTTPException(404,"Benachrichtigung nicht gefunden")
    return {"ok":True}


@app.post("/api/notifications/{notification_id}/read")
async def api_notification_read(request: Request, notification_id: int):
    auth=request.state.auth_user or {}
    if not db.mark_notification_read(notification_id, int(auth["id"])):
        raise HTTPException(404, "Benachrichtigung nicht gefunden")
    return {"ok": True}


@app.get("/api/notifications/push/config")
async def api_push_config(request: Request):
    auth=request.state.auth_user or {}
    config=web_push.public_config()
    config["subscriptions"]=db.web_push_subscriptions_for_user(int(auth["id"]))
    return config


@app.post("/api/notifications/push/subscribe")
async def api_push_subscribe(request: Request, payload: dict):
    auth=request.state.auth_user or {}
    keys=payload.get("keys") or {}
    try:
        endpoint=web_push.validate_endpoint(payload.get("endpoint"))
        saved=db.save_web_push_subscription(
            int(auth["id"]),endpoint,keys.get("p256dh"),keys.get("auth"),
            payload.get("min_severity") or "warning",
            request.headers.get("user-agent") or "",
        )
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    db.add_activity(
        system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),
        action="PWA-Push aktiviert",category="Benachrichtigungen",target="Dieses Gerät",
        details=f"Schwelle: {saved.get('min_severity')}",method=request.method,path=request.url.path,status_code=200,
    )
    return {"ok":True,**saved}


@app.post("/api/notifications/push/preferences")
async def api_push_preferences(request: Request, payload: dict):
    auth=request.state.auth_user or {}
    try:
        endpoint=web_push.validate_endpoint(payload.get("endpoint"))
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    if not db.update_web_push_preference(int(auth["id"]),endpoint,payload.get("min_severity") or "warning"):
        raise HTTPException(404,"Push-Subscription nicht gefunden")
    return {"ok":True,"min_severity":str(payload.get("min_severity") or "warning")}


@app.delete("/api/notifications/push/subscribe")
async def api_push_unsubscribe(request: Request, payload: dict):
    auth=request.state.auth_user or {}
    try:
        endpoint=web_push.validate_endpoint(payload.get("endpoint"))
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    removed=db.remove_web_push_subscription(int(auth["id"]),endpoint)
    if removed:
        db.add_activity(
            system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),
            action="PWA-Push deaktiviert",category="Benachrichtigungen",target="Dieses Gerät",
            method=request.method,path=request.url.path,status_code=200,
        )
    return {"ok":True,"removed":bool(removed)}


@app.post("/api/notifications/push/test")
async def api_push_test(request: Request, payload: dict):
    auth=request.state.auth_user or {}
    try:
        endpoint=web_push.validate_endpoint(payload.get("endpoint"))
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    row=db.web_push_subscription_for_user_endpoint(int(auth["id"]),endpoint)
    if not row:
        raise HTTPException(404,"Push-Subscription nicht gefunden")
    try:
        await asyncio.to_thread(web_push.send_test,row)
        db.mark_web_push_success(row["id"])
        return {"ok":True}
    except Exception as exc:
        logging.warning("Push-Test fehlgeschlagen",exc_info=True)
        raise HTTPException(502,f"Push-Test fehlgeschlagen: {type(exc).__name__}")


def _with_live_power(cps):
    """Decorate charge points with current connector-derived power for all list views."""
    enriched=[]
    for cp in cps:
        item=dict(cp)
        try:
            live=db.live_telemetry_for_charge_point(item["id"])
            item["power_kw"]=live.get("power_kw")
            item["power_source"]=live.get("power_source")
            item["power_calculated_at"]=live.get("last_meter_at")
            item["live_telemetry"]=live
            active=db.active_transactions_for_charge_point(item["id"], 10)
            item["active_transactions"]=active
            item["active_session_count"]=len(active)
        except Exception:
            logging.exception("Live-Leistungsdaten konnten für %s nicht geladen werden", item.get("id"))
        enriched.append(item)
    return enriched


@app.get("/api/status")
async def api_status(request: Request):
    cps = _with_live_power(db.list_charge_points())
    online = sum(1 for cp in cps if cp["status"] not in ("Offline", "Unknown", "Unavailable"))
    active = [tx for cp in cps for tx in (cp.get("active_transactions") or [])]
    now=datetime.now(timezone.utc)
    cp_status={cp.get("id"):cp.get("status") for cp in cps}
    for tx in active:
        tx["ocpp_status"]=cp_status.get(tx.get("charge_point_id"))
        tx["timing"]=db.transaction_time_breakdown(tx.get("id"))
        try:
            started=datetime.fromisoformat(str(tx.get("started_at") or "").replace("Z","+00:00"))
            tx["duration_seconds"]=max(0,int((now-started.astimezone(timezone.utc)).total_seconds()))
        except Exception:
            tx["duration_seconds"]=None
        tx["budget"]=db.user_monthly_budget(int(tx["user_id"])) if tx.get("user_id") is not None else None
    power_values=[float(cp["power_kw"]) for cp in cps if cp.get("power_kw") is not None]
    total_power = round(sum(power_values), 2) if power_values else None
    active_energy = sum(float(tx.get("energy_kwh") or 0) for tx in active) if active else 0.0
    faulted=sum(1 for cp in cps if cp.get("status")=="Faulted")
    unavailable=sum(1 for cp in cps if cp.get("status") in ("Offline","Unknown","Unavailable"))
    online_ids={str(cp.get("id")) for cp in cps if cp.get("status") not in ("Offline","Unknown","Unavailable")}
    connector_rows=[c for cp in cps if str(cp.get("id")) in online_ids for c in ((cp.get("live_telemetry") or {}).get("connectors") or [])]
    telemetry_fresh=sum(1 for c in connector_rows if c.get("freshness")=="live")
    telemetry_delayed=sum(1 for c in connector_rows if c.get("freshness")=="delayed")
    telemetry_stale=sum(1 for c in connector_rows if c.get("freshness") in ("stale","none"))
    operational=_operational_state(len(cps),online,faulted,unavailable)
    dashboard_health={"faulted":faulted,"offline_or_unavailable":unavailable,"telemetry_fresh":telemetry_fresh,"telemetry_delayed":telemetry_delayed,"telemetry_stale":telemetry_stale,"connector_samples":len(connector_rows),"operational":operational}
    if getattr(request.state,"auth_user",None) and request.state.auth_user.get("role")=="admin":
        dashboard_health["security"]=_security_dashboard_summary()
    return {
        "version": APP_VERSION, "charge_points": cps, "count": len(cps), "online": online,
        "charging": sum(1 for cp in cps if cp["status"] == "Charging"),
        "active_transactions_count": len(active),
        "active_session_energy_kwh": round(active_energy, 3),
        "total_power_kw": total_power,
        "total_power_source": "measured" if any(cp.get("power_source") == "measured" for cp in cps) else ("calculated" if total_power is not None else None),
        "max_theoretical_kw": round(sum(float(cp["max_power_kw"] or 0) for cp in cps), 1),
        "events": db.recent_events(40), "transactions": db.recent_transactions(20),
        "analytics": db.analytics(), "active_transactions": active,
        "user_budget_summary": db.user_budget_summary(),
        "recent_activity": db.recent_activity(8),
        "dashboard_health": dashboard_health,
    }


class DeviceOnboardPayload(BaseModel):
    location: str | None = None
    max_power_kw: float | None = None
    notes: str | None = None


def _live_age_seconds(value, now=None):
    if not value:
        return None
    try:
        dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=timezone.utc)
        return max(0, int(((now or datetime.now(timezone.utc))-dt.astimezone(timezone.utc)).total_seconds()))
    except (TypeError,ValueError):
        return None


def _telemetry_freshness(age_seconds):
    if age_seconds is None:
        return "none"
    if age_seconds <= 90:
        return "live"
    if age_seconds <= 300:
        return "delayed"
    return "stale"


def _liveview_snapshot():
    now=datetime.now(timezone.utc)
    cps=db.list_charge_points()
    active=db.active_transactions(100)
    active_by_connector={(str(x.get("charge_point_id")),int(x.get("connector_id") or 0)):x for x in active}
    from .ocpp_server import connection_snapshot
    connection_rows=connection_snapshot().get("connections") or []
    connection_by_id={str(x.get("id")):x for x in connection_rows}
    stations=[]
    total_power=0.0
    has_power=False
    online_count=0
    charging_count=0
    fresh_count=0
    fault_count=0
    warning_count=0
    for cp in cps:
        cp_id=str(cp.get("id"))
        connected=is_connected(cp_id)
        if connected:
            online_count+=1
        live=db.live_telemetry_for_charge_point(cp_id)
        connectors=[]
        for raw in live.get("connectors") or []:
            item=dict(raw)
            cid=int(item.get("connector_id") or 0)
            tx=active_by_connector.get((cp_id,cid))
            age=_live_age_seconds(item.get("last_meter_at"),now)
            freshness=_telemetry_freshness(age)
            if connected and freshness=="live":
                fresh_count+=1
            status=str(item.get("status") or "Unknown")
            active_state=status in {"Charging","SuspendedEV","SuspendedEVSE","Preparing","Finishing"}
            raw_power=item.get("last_power_kw")
            # Values older than five minutes remain available as last-known values,
            # but are never counted as live site power.
            live_power=float(raw_power) if raw_power is not None and connected and freshness in {"live","delayed"} and active_state else None
            if live_power is not None:
                total_power+=live_power; has_power=True
            if connected and status in {"Charging","SuspendedEV","SuspendedEVSE"}:
                charging_count+=1
            connectors.append({
                "connector_id":cid,"connector_type":item.get("connector_type"),"status":status,
                "max_power_kw":item.get("max_power_kw"),"power_kw":live_power,"last_power_kw":raw_power,
                "power_source":item.get("power_source"),"last_power_source":item.get("last_power_source"),"energy_kwh":item.get("energy_kwh"),"meter_register_kwh":item.get("energy_kwh"),
                "voltage_v":item.get("voltage_v"),"voltage_l1":item.get("voltage_l1"),"voltage_l2":item.get("voltage_l2"),"voltage_l3":item.get("voltage_l3"),
                "current_import_a":item.get("current_import_a"),"current_import_l1":item.get("current_import_l1"),"current_import_l2":item.get("current_import_l2"),"current_import_l3":item.get("current_import_l3"),
                "power_offered_kw":item.get("power_offered_kw"),"frequency_hz":item.get("frequency_hz"),
                "temperature_c":item.get("temperature_c"),"soc_percent":item.get("soc_percent"),"last_soc_percent":item.get("last_soc_percent"),"soc_age_seconds":item.get("soc_age_seconds"),
                "last_meter_at":item.get("last_meter_at"),"meter_age_seconds":age,"freshness":freshness,
                "session":({
                    "id":tx.get("id"),"started_at":tx.get("started_at"),"energy_kwh":tx.get("energy_kwh"),
                    "user_id":tx.get("user_id"),"user_name":tx.get("user_name"),"user_role":tx.get("user_role"),
                    "user_department":tx.get("user_department"),"user_image_path":tx.get("user_image_path"),
                    "vehicle_id":tx.get("vehicle_id"),"vehicle_name":tx.get("vehicle_name"),"vehicle_make":tx.get("vehicle_make"),
                    "vehicle_model":tx.get("vehicle_model"),"vehicle_plate":tx.get("vehicle_plate"),"vehicle_image_path":tx.get("vehicle_image_path"),
                    "id_tag":tx.get("id_tag"),"max_power_kw":tx.get("max_power_kw"),
                } if tx else None),
            })
        cp_last=live.get("last_meter_at")
        cp_age=_live_age_seconds(cp_last,now)
        # For an actually disconnected charge point, show how long OCPP
        # communication has been absent. This is intentionally separate from
        # MeterValues age: an online idle connector may legitimately send no
        # MeterValues for many hours.
        offline_age=_live_age_seconds(cp.get("last_message_at") or cp.get("last_seen"),now) if not connected else None
        connection=connection_by_id.get(cp_id) or {}
        diagnostic=connection.get("diagnostic") or {
            "level":"critical" if not connected else ("critical" if (cp.get("status") or "")=="Faulted" else "healthy"),
            "label":"Kritisch" if not connected or (cp.get("status") or "")=="Faulted" else "Gesund",
            "score":0 if not connected else (35 if (cp.get("status") or "")=="Faulted" else 100),
            "issues":[{"code":"offline","severity":"critical","title":"OCPP-Verbindung getrennt","detail":"Der Ladepunkt ist nicht verbunden."}] if not connected else [],
        }
        if diagnostic.get("level")=="critical":
            fault_count+=1
        elif diagnostic.get("level")=="warning":
            warning_count+=1
        stations.append({
            "id":cp_id,"location":cp.get("location"),"vendor":cp.get("vendor"),"model":cp.get("model"),
            "firmware":cp.get("firmware"),"serial_number":cp.get("serial_number"),
            "status":cp.get("status") or "Unknown","connected":connected,"last_seen":cp.get("last_seen"),
            "last_message_at":cp.get("last_message_at"),"last_message_type":cp.get("last_message_type"),
            "max_power_kw":cp.get("max_power_kw"),"last_meter_at":cp_last,"meter_age_seconds":cp_age,
            "offline_age_seconds":offline_age,
            "freshness":_telemetry_freshness(cp_age),"connectors":connectors,
            "diagnostic":diagnostic,
            "heartbeat_age_seconds":diagnostic.get("heartbeat_age_seconds"),
            "subprotocol":connection.get("subprotocol") or "ocpp1.6",
        })
    available_count=sum(1 for station in stations if station.get("connected") and str(station.get("status") or "")=="Available")
    return {
        "version":APP_VERSION,"generated_at":now.isoformat(),"stations":stations,"settings":db.liveview_settings(),
        "summary":{"stations":len(stations),"online":online_count,"offline":max(0,len(stations)-online_count),
                   "available":available_count,"active_sessions":len(active),
                   "charging_connectors":charging_count,"total_power_kw":round(total_power,2) if has_power else None,
                   "fresh_connectors":fresh_count,"critical":fault_count,"warnings":warning_count},
    }


@app.get("/api/ocpp-monitor")
async def api_ocpp_monitor():
    from .ocpp_server import connection_snapshot
    data = connection_snapshot()
    data["active_transactions"] = db.active_transactions(50)
    return data


@app.get("/api/liveview")
async def api_liveview():
    return _liveview_snapshot()


@app.get("/api/liveview/history/{cp_id}")
async def api_liveview_history(cp_id: str, connector_id: int | None = None, hours: int = 6, points: int = 240):
    cp_id=unquote(cp_id)
    if not db.get_charge_point(cp_id):
        raise HTTPException(404,"Ladepunkt nicht gefunden")
    return db.meter_history_for_charge_point(cp_id, connector_id=connector_id, hours=hours, max_points=points)

@app.get("/api/ocpp-devices")
async def api_ocpp_devices():
    devices = db.list_discovered_devices()
    for d in devices:
        d["connectors"] = db.connectors_for_charge_point(d["id"])
        d["events"] = db.events_for_charge_point(d["id"], 20)
        try:
            live=db.live_telemetry_for_charge_point(d["id"])
            d["power_kw"]=live.get("power_kw")
            d["power_source"]=live.get("power_source")
        except Exception:
            d["power_source"]=None
    return {"devices": devices}

@app.get("/api/ocpp-devices/{cp_id}")
async def api_ocpp_device(cp_id: str):
    cp_id = unquote(cp_id)
    cp = db.get_charge_point(cp_id)
    if not cp: raise HTTPException(404, "OCPP-Gerät nicht gefunden")
    cp["connectors"] = db.connectors_for_charge_point(cp_id)
    from .ocpp_server import ACTIVE_CONNECTIONS
    cp["connected"] = cp_id in ACTIVE_CONNECTIONS
    return {"device": cp, "events": db.events_for_charge_point(cp_id, 120), "transactions": db.transactions_for_charge_point(cp_id, 50), "active_transactions": db.active_transactions_for_charge_point(cp_id, 20), "live_telemetry": db.live_telemetry_for_charge_point(cp_id), "load_state": db.load_state_for_charge_point(cp_id)}

@app.post("/api/ocpp-devices/{cp_id}/onboard")
async def onboard_ocpp_device(cp_id: str, payload: DeviceOnboardPayload):
    if not db.onboard_charge_point(cp_id, location=payload.location, max_power_kw=payload.max_power_kw, notes=payload.notes):
        raise HTTPException(404, "OCPP-Gerät nicht gefunden")
    return {"ok": True, "charge_point": db.get_charge_point(cp_id)}

@app.post("/api/ocpp-devices/{cp_id}/ignore")
async def ignore_ocpp_device(cp_id: str):
    if not db.ignore_charge_point(cp_id):
        raise HTTPException(404, "OCPP-Gerät nicht gefunden")
    return {"ok": True}


@app.post("/api/ocpp-devices/{cp_id}/unignore")
async def unignore_ocpp_device(cp_id: str):
    if not db.unignore_charge_point(cp_id):
        raise HTTPException(404, "OCPP-Gerät nicht gefunden")
    return {"ok": True}

@app.delete("/api/ocpp-devices/{cp_id}")
async def delete_ocpp_device(cp_id: str):
    cp_id = unquote(cp_id)
    cp = db.get_charge_point(cp_id)
    if not cp:
        raise HTTPException(404, "OCPP-Gerät nicht gefunden")
    from .ocpp_server import ACTIVE_CONNECTIONS
    if cp_id in ACTIVE_CONNECTIONS:
        raise HTTPException(409, "Das OCPP-Gerät ist noch verbunden und kann erst nach dem Trennen gelöscht werden")
    ok, reason = db.delete_ocpp_device(cp_id)
    if not ok:
        if reason == "not_found":
            raise HTTPException(404, "OCPP-Gerät nicht gefunden")
        if reason == "active":
            raise HTTPException(409, "Das OCPP-Gerät ist noch verbunden oder hat einen aktiven Ladevorgang")
        raise HTTPException(500, "OCPP-Gerät konnte nicht gelöscht werden")
    return {"ok": True, "deleted": cp_id}


@app.get("/api/charge-points")
async def api_charge_points(include_retired: bool = True, include_pending: bool = False):
    cps = db.list_charge_points_admin(include_retired=include_retired)
    if not include_pending:
        cps = [cp for cp in cps if int(cp.get("onboarded", 1)) and not int(cp.get("ignored", 0))]
    cps = _with_live_power(cps)
    return {"charge_points": cps}

class ChargePointPayload(BaseModel):
    id: str
    vendor: str | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware: str | None = None
    ocpp_version: str = "1.6J"
    location: str | None = None
    connector_count: int = 1
    connector_type: str | None = "Type 2"
    max_power_kw: float = 22
    notes: str | None = None
    rfid_self_enroll_mode: str = "auto"

class SessionPolicyPayload(BaseModel):
    stand_grace_seconds: int = 120
    auto_stop_zero_minutes: int = 0

@app.get("/api/charge-points/{cp_id}")
async def api_charge_point(cp_id: str):
    cp = db.get_charge_point(cp_id)
    if not cp:
        raise HTTPException(404, "Ladepunkt nicht gefunden")
    cp["connectors"] = db.connectors_for_charge_point(cp_id)
    # Connection state is intentionally derived from the live WebSocket registry,
    # not from last_seen. A device that talked to us five minutes ago is not
    # magically still connected. The OCPP dog door knows who is actually inside.
    from .ocpp_server import ACTIVE_CONNECTIONS
    cp["connected"] = cp_id in ACTIVE_CONNECTIONS
    live = db.live_telemetry_for_charge_point(cp_id)
    active = db.active_transactions_for_charge_point(cp_id, 20)
    for item in active:
        item["timing"] = db.transaction_time_breakdown(item["id"])
    transactions = db.transactions_for_charge_point(cp_id)
    # The unified charge-point workspace keeps the overview rich without duplicating
    # a second device detail page. Timing data is therefore enriched here once and
    # reused by overview + statistics. One charge point, many drawers.
    for item in transactions:
        item["timing"] = db.transaction_time_breakdown(item["id"])
    events=db.events_for_charge_point(cp_id, 240)
    labels={"Connected":"Ladepunkt verbunden","Disconnected":"Ladepunkt getrennt","BootNotification":"BootNotification empfangen","Authorize":"RFID erkannt / Autorisierung","StartTransaction":"Ladevorgang gestartet","StopTransaction":"Ladevorgang beendet","MeterValues":"Energiefluss / Messwerte","StatusNotification":"Connector-Status","RemoteStopTransaction":"RemoteStop ausgeführt","RemoteStartTransaction":"RemoteStart gesendet","UnlockConnector":"Connector entriegelt","ChangeAvailability":"Verfügbarkeit geändert","Reset":"Reset gesendet","BudgetLimitReached":"Monatslimit erreicht","ZeroFlowAutoStop":"0-kW-Auto-Stop ausgelöst","Reconciled":"Session-Reconciliation","ConnectorAvailable":"Connector wieder verfügbar"}
    for e in events:
        e["human_label"]=labels.get(e.get("event_type"),e.get("event_type") or "OCPP-Ereignis")
        e["human_status"]="Fehler/Warnung" if any(x in str(e.get("event_type","")).lower() for x in ("fault","error","warning")) else ("Pausiert" if "Suspended" in str(e.get("payload","")) else "Information")
    return {"charge_point": cp, "events": events, "transactions": transactions, "active_transactions": active, "meter_samples": db.meter_samples_for_charge_point(cp_id), "meter_diagnostics": db.meter_diagnostics_for_charge_point(cp_id, 12), "meter_capabilities": db.meter_capabilities_for_charge_point(cp_id), "diagnostics": db.diagnostic_summary(cp_id), "live_telemetry": live, "load_state": db.load_state_for_charge_point(cp_id)}

@app.get("/api/tariffs")
async def api_tariffs():
    return {
        "tariffs":db.list_tariffs(),
        "users":db.list_tariff_users(),
        "charge_points":db.list_tariff_charge_points(),
    }


class TariffPayload(BaseModel):
    name: str
    scope: str
    target_id: str | None = None
    price_cents_per_kwh: int
    valid_from: str
    valid_until: str | None = None


class TariffVersionPayload(BaseModel):
    name: str
    price_cents_per_kwh: int
    valid_from: str
    valid_until: str | None = None


@app.post("/api/tariffs")
async def create_tariff(payload: TariffPayload):
    if payload.scope not in {"global","charge_point","user"}:
        raise HTTPException(400,"Community unterstützt Tarife global, pro Ladepunkt oder pro Benutzer.")
    try:
        return {"id":db.create_tariff(
            payload.name,payload.scope,payload.target_id,payload.price_cents_per_kwh,
            payload.valid_from,payload.valid_until,None,None
        )}
    except ValueError as exc:
        raise HTTPException(400,str(exc))


@app.post("/api/tariffs/{tariff_id}/versions")
async def create_tariff_version(tariff_id:int,payload:TariffVersionPayload):
    try:
        return {"id":db.create_tariff_version(
            tariff_id,payload.name,payload.price_cents_per_kwh,payload.valid_from,
            payload.valid_until,None,None
        )}
    except ValueError as exc:
        detail=str(exc)
        raise HTTPException(404 if detail=="Tarif nicht gefunden" else 400,detail)


@app.delete("/api/tariffs/{tariff_id}")
async def remove_tariff(tariff_id:int):
    if not db.delete_tariff(tariff_id):
        raise HTTPException(404,"Tarif nicht gefunden")
    return {"ok":True}


@app.get("/api/charge-points/{cp_id}/diagnostics")
async def api_charge_point_diagnostics(cp_id: str, severity: str | None = None, category: str | None = None, limit: int = 100):
    if not db.get_charge_point(cp_id):
        raise HTTPException(404, "Ladepunkt nicht gefunden")
    return {"health":db.charge_point_health(cp_id),"active":db.active_diagnostic_states(cp_id),"history":db.diagnostic_history(cp_id,severity=severity,category=category,limit=limit)}


@app.get("/api/charge-points/{cp_id}/analytics")
async def api_charge_point_analytics(cp_id: str):
    if not db.get_charge_point(cp_id):
        raise HTTPException(404, "Ladepunkt nicht gefunden")
    return {"analytics": db.charge_point_analytics(cp_id)}

@app.post("/api/charge-points")
async def create_charge_point(payload: ChargePointPayload):
    try:
        data = payload.model_dump(exclude={"id"})
        created = db.create_charge_point(payload.id, **data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logging.exception("Fehler beim Anlegen des Ladepunkts %s", payload.id)
        raise HTTPException(500, f"Ladepunkt konnte nicht gespeichert werden: {exc}") from exc
    if not created:
        raise HTTPException(409, "Ein Ladepunkt mit dieser Charge Point ID existiert bereits")
    return {"ok": True, "charge_point": db.get_charge_point(payload.id)}

@app.put("/api/charge-points/{cp_id}")
async def update_charge_point(cp_id: str, payload: ChargePointPayload):
    if payload.id != cp_id:
        raise HTTPException(400, "Ladepunkt-ID kann nicht geändert werden")
    if not db.update_charge_point(cp_id, **payload.model_dump(exclude={"id"})):
        raise HTTPException(404, "Ladepunkt nicht gefunden")
    return {"ok": True, "charge_point": db.get_charge_point(cp_id), "connectors": db.connectors_for_charge_point(cp_id)}

@app.put("/api/charge-points/{cp_id}/session-policy")
async def update_charge_point_session_policy(cp_id: str, payload: SessionPolicyPayload):
    try:
        ok = db.update_charge_point_session_policy(cp_id, payload.stand_grace_seconds, payload.auto_stop_zero_minutes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "Ladepunkt nicht gefunden")
    return {"ok": True, "charge_point": db.get_charge_point(cp_id)}


@app.post("/api/charge-points/{cp_id}/retire")
async def retire_charge_point(cp_id: str):
    if not db.retire_charge_point(cp_id): raise HTTPException(404, "Ladepunkt nicht gefunden")
    return {"ok": True}

@app.post("/api/charge-points/{cp_id}/restore")
async def restore_charge_point(cp_id: str):
    if not db.restore_charge_point(cp_id): raise HTTPException(404, "Ladepunkt nicht gefunden")
    return {"ok": True}

@app.post("/api/charge-points/{cp_id}/archive")
async def archive_charge_point(cp_id: str):
    ok, reason = db.archive_charge_point(cp_id)
    if not ok:
        if reason == "active": raise HTTPException(409, "Ladepunkt ist noch aktiv oder verbunden")
        if reason == "not_retired": raise HTTPException(409, "Ladepunkt muss zuerst außer Betrieb genommen werden")
        raise HTTPException(404, "Ladepunkt nicht gefunden")
    return {"ok": True}

@app.post("/api/charge-points/{cp_id}/unarchive")
async def unarchive_charge_point(cp_id: str):
    if not db.unarchive_charge_point(cp_id): raise HTTPException(404, "Archivierter Ladepunkt nicht gefunden")
    return {"ok": True}

@app.delete("/api/charge-points/{cp_id}")
async def delete_charge_point(cp_id: str):
    ok, reason = db.delete_charge_point(cp_id)
    if not ok:
        messages={
            "active": (409, "Ladepunkt ist noch aktiv oder hat einen aktiven Ladevorgang"),
            "must_retire": (409, "Ladepunkt muss vor dem endgültigen Löschen außer Betrieb genommen werden"),
            "linked_device": (409, "Dieser Ladepunkt ist technisch mit einem OCPP-Gerät verknüpft und kann hier nicht endgültig gelöscht werden"),
            "archived": (409, "Ein außer Betrieb genommener oder archivierter Ladepunkt kann nicht endgültig gelöscht werden. Bitte verwenden Sie den Archiv-Lebenszyklus."),
            "not_manual": (409, "Dieser Ladepunkt kann nicht endgültig gelöscht werden."),
            "history": (409, "Ladepunkt besitzt bereits historische Daten und kann nicht endgültig gelöscht werden"),
            "not_found": (404, "Ladepunkt nicht gefunden"),
        }
        code,msg=messages.get(reason,(500,"Ladepunkt konnte nicht gelöscht werden"))
        raise HTTPException(code,msg)
    return {"ok": True, "deleted": cp_id}

@app.put("/api/charge-points/{cp_id}/connectors/{connector_id}")
async def update_connector(cp_id: str, connector_id: int, payload: dict):
    if not db.update_connector(cp_id, connector_id, **payload): raise HTTPException(404, "Connector nicht gefunden")
    return {"ok": True, "connectors": db.connectors_for_charge_point(cp_id)}


@app.get("/transactions/{transaction_id}", response_class=HTMLResponse)
async def transaction_page(request: Request, transaction_id: int):
    tx = db.get_transaction(transaction_id)
    if not tx:
        raise HTTPException(404, "Ladevorgang nicht gefunden")
    return render(request, "transaction_detail.html", page="transactions", transaction_id=transaction_id)


@app.get("/api/transactions")
async def api_transactions(
    q: str = "", status: str = "", charge_point: str = "", id_tag: str = "",
    from_date: str = "", to_date: str = "", page: int = 1, page_size: int = 20,
    sort_key: str = "id", sort_dir: str = "desc"
):
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    rows, total = db.search_transactions(
        q=q, status=status, charge_point=charge_point, id_tag=id_tag,
        from_date=from_date, to_date=to_date, page=page, page_size=page_size,
        sort_key=sort_key, sort_dir=sort_dir
    )
    for row in rows:
        if row.get("status") == "Active" and not row.get("ended_at"):
            timing = db.transaction_time_breakdown(row.get("id")) or {}
            for key in ("charging_seconds", "stand_seconds", "connection_seconds"):
                if key in timing:
                    row[key] = timing[key]
    return {"transactions": rows, "total": total, "page": page, "page_size": page_size, "pages": max(1, (total + page_size - 1) // page_size)}


@app.get("/api/transactions/export.csv")
async def export_transactions_csv(q: str = "", status: str = "", charge_point: str = "", id_tag: str = ""):
    rows, _ = db.search_transactions(q=q, status=status, charge_point=charge_point, id_tag=id_tag, page=1, page_size=5000)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["ID", "Start", "Ende", "Ladepunkt", "Connector", "RFID", "Fahrzeug", "OCPP Transaction ID", "Dauer (min)", "Ladezeit (min)", "Standzeit (min)", "Anschlussdauer (min)", "Energie (kWh)", "Peak (kW)", "Status"])
    for row in rows:
        duration = ""
        if row.get("started_at"):
            from datetime import datetime
            start = datetime.fromisoformat(row["started_at"])
            end = datetime.fromisoformat(row["ended_at"]) if row.get("ended_at") else datetime.now(start.tzinfo)
            duration = round(max(0, (end - start).total_seconds() / 60), 1)
        timing = db.transaction_time_breakdown(row.get("id")) or row
        timing_unknown=row.get("timing_quality")=="connection_only"
        writer.writerow([
            row.get("id", ""), _local_text(row.get("started_at")), _local_text(row.get("ended_at")),
            row.get("charge_point_id", ""), row.get("connector_id", ""), row.get("id_tag", ""),
            row.get("vehicle_name", ""), row.get("transaction_id", ""), duration,
            "" if timing_unknown else round(float(timing.get("charging_seconds") or 0) / 60, 1),
            "" if timing_unknown else round(float(timing.get("stand_seconds") or 0) / 60, 1),
            round(float(timing.get("connection_seconds") or 0) / 60, 1),
            row.get("energy_kwh", 0), row.get("max_power_kw", 0), row.get("status", "")
        ])
    data = output.getvalue().encode("utf-8-sig")
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=ocpp-ladevorgaenge.csv"})


def _session_export_context(tx):
    started = datetime.fromisoformat(tx["started_at"]) if tx.get("started_at") else None
    ended = datetime.fromisoformat(tx["ended_at"]) if tx.get("ended_at") else None
    duration_minutes = 0
    if started:
        duration_minutes = max(0, (ended - started).total_seconds() / 60) if ended else max(0, (datetime.now(started.tzinfo) - started).total_seconds() / 60)
    energy = float(tx.get("energy_kwh") or 0)
    avg_power = tx.get("avg_power_kw")
    user = db.get_user_by_rfid(tx.get("id_tag"))
    vehicle = db.get_vehicle(tx.get("vehicle_id")) if tx.get("vehicle_id") else None
    timing = db.transaction_time_breakdown(tx.get("id")) or {}
    post_session_occupancy = db.transaction_post_session_occupancy(tx.get("id")) or {}
    price_cents = tx.get("price_cents_per_kwh")
    cost_cents = tx.get("cost_cents")
    tariff = tx.get("tariff_name") if tx.get("tariff_id") is not None else None
    cost = (float(cost_cents) / 100.0) if cost_cents is not None else None
    return {
        "duration_minutes": duration_minutes,
        "timing": timing,
        "post_session_occupancy": post_session_occupancy,
        "avg_power": float(avg_power) if avg_power is not None else None,
        "user": user,
        "vehicle": vehicle,
        "energy": energy,
        "cost": cost,
        "tariff": (f"{tariff} · {float(price_cents)/100:.2f} EUR/kWh" if tariff and price_cents is not None else "Kein angewendeter Tarif gespeichert"),
        "price_cents": price_cents,
    }


@app.get("/api/transactions/{transaction_id}/export.csv")
async def export_single_transaction_csv(transaction_id: int):
    tx = db.get_transaction(transaction_id)
    if not tx:
        raise HTTPException(404, "Ladevorgang nicht gefunden")
    ctx = _session_export_context(tx)
    user = ctx["user"] or {}
    vehicle = ctx["vehicle"] or {}
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Start", "Ende", "Dauer (min)", "Ladezeit (min)", "Standzeit (min)", "Anschlussdauer (min)", "Nachbelegung (min)", "Connector frei um", "Ladepunkt", "Connector", "RFID",
        "Benutzer", "Fahrzeug", "Kennzeichen", "OCPP Transaction ID", "Energie (kWh)",
        "Durchschnittsleistung (kW)", "Peak (kW)", "Tarif", "Kosten (EUR)", "Status"
    ])
    writer.writerow([
        tx.get("id", ""), _local_text(tx.get("started_at")), _local_text(tx.get("ended_at")),
        round(ctx["duration_minutes"], 1), ("" if tx.get("timing_quality")=="connection_only" else round(float(ctx["timing"].get("charging_seconds",0))/60,1)), ("" if tx.get("timing_quality")=="connection_only" else round(float(ctx["timing"].get("stand_seconds",0))/60,1)), round(float(ctx["timing"].get("connection_seconds",0))/60,1),
        (round(float(ctx["post_session_occupancy"].get("seconds") or 0)/60,1) if ctx["post_session_occupancy"].get("tracked") else ""), _local_text(ctx["post_session_occupancy"].get("unplugged_at")),
        tx.get("charge_point_id", ""), tx.get("connector_id", ""),
        tx.get("id_tag", ""), user.get("name", ""), vehicle.get("name", ""), vehicle.get("plate", ""),
        tx.get("transaction_id", ""), round(ctx["energy"], 3), (round(ctx["avg_power"], 3) if ctx["avg_power"] is not None else ""),
        round(float(tx.get("sampled_peak_kw") if tx.get("sampled_peak_kw") is not None else (tx.get("max_power_kw") or 0)), 3), ctx["tariff"], (f"{ctx['cost']:.2f}" if ctx["cost"] is not None else ""), tx.get("status", "")
    ])
    data = output.getvalue().encode("utf-8-sig")
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=ladevorgang-{transaction_id}.csv"
    })


@app.get("/api/transactions/{transaction_id}/export.pdf")
async def export_single_transaction_pdf(transaction_id: int):
    tx = db.get_transaction(transaction_id)
    if not tx:
        raise HTTPException(404, "Ladevorgang nicht gefunden")
    ctx = _session_export_context(tx)
    user = ctx["user"] or {}
    vehicle = ctx["vehicle"] or {}
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=16*mm, leftMargin=16*mm,
        topMargin=18*mm, bottomMargin=16*mm,
        title=f"Ladevorgangsbericht #{transaction_id}",
        author=db.branding_settings()["product_name"],
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="RedTitle", parent=styles["Title"], fontSize=23, leading=27, textColor=colors.HexColor(db.branding_settings()["primary_color"]), spaceAfter=4))
    styles.add(ParagraphStyle(name="Subtle", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#66717d"), leading=13))
    styles.add(ParagraphStyle(name="Section", parent=styles["Heading2"], fontSize=12, leading=15, textColor=colors.HexColor(db.branding_settings()["primary_color"]), spaceBefore=8, spaceAfter=6))
    styles.add(ParagraphStyle(name="Value", parent=styles["Normal"], fontSize=10, leading=14, textColor=colors.HexColor("#18202a")))
    styles.add(ParagraphStyle(name="Label", parent=styles["Normal"], fontSize=8, leading=10, textColor=colors.HexColor("#75808c")))
    styles.add(ParagraphStyle(name="RightSmall", parent=styles["Normal"], fontSize=8, leading=10, textColor=colors.white, alignment=TA_RIGHT))

    story=[]
    brand = Table([[Paragraph(f'<b>{db.branding_settings()["display_name"]}</b>', ParagraphStyle('Brand', parent=styles['Normal'], fontSize=12, textColor=colors.white)),
                    Paragraph(f'<b>{db.branding_settings()["product_name"]}</b><br/><font size="8">{db.branding_settings()["product_subtitle"]}</font>', ParagraphStyle('BrandText', parent=styles['Normal'], fontSize=10, leading=12, textColor=colors.white)),
                    Paragraph(datetime.now(timezone.utc).astimezone(BERLIN_TZ).strftime('%d.%m.%Y %H:%M'), styles['RightSmall'])]], colWidths=[60*mm, 62*mm, 55*mm])
    brand.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),colors.HexColor(db.branding_settings()['primary_color'])),('TEXTCOLOR',(0,0),(-1,-1),colors.white),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),
        ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),('ALIGN',(2,0),(2,0),'RIGHT')]))
    story += [brand, Spacer(1, 10), Paragraph('Ladevorgangsbericht', styles['RedTitle']), Paragraph(f'Session #{transaction_id} · Einzelbericht', styles['Subtle']), Spacer(1, 6)]

    status = str(tx.get('status') or 'Unbekannt')
    cp_name = str(tx.get('charge_point_id') or '')
    overview = [
        [Paragraph('<b>Ladepunkt</b>', styles['Label']), Paragraph('<b>Status</b>', styles['Label']), Paragraph('<b>Energie</b>', styles['Label']), Paragraph('<b>Peak</b>', styles['Label'])],
        [Paragraph(cp_name, styles['Value']), Paragraph(status, styles['Value']), Paragraph(f"{ctx['energy']:.2f} kWh", styles['Value']), Paragraph('—' if (tx.get('sampled_peak_kw') is None and tx.get('max_power_kw') is None) else f"{float(tx.get('sampled_peak_kw') if tx.get('sampled_peak_kw') is not None else tx.get('max_power_kw') or 0):.1f} kW", styles['Value'])],
    ]
    t=Table(overview, colWidths=[45*mm,45*mm,45*mm,42*mm])
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#f4f6f8')),('BOX',(0,0),(-1,-1),.5,colors.HexColor('#dfe4e8')),('INNERGRID',(0,0),(-1,-1),.25,colors.HexColor('#e7eaed')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
    story += [t, Spacer(1, 8)]

    def section(title, pairs, widths=(55*mm,122*mm)):
        data=[]
        for label, value in pairs:
            data.append([Paragraph(label, styles['Label']), Paragraph(str(value), styles['Value'])])
        tab=Table(data, colWidths=list(widths))
        tab.setStyle(TableStyle([('LINEBELOW',(0,0),(-1,-1),.25,colors.HexColor('#e7eaed')),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
        return [Paragraph(title, styles['Section']), tab, Spacer(1, 3)]

    story += section('Zeit & Ladevorgang', [
        ('Start', _local_text(tx.get('started_at')) or '—'), ('Ende', _local_text(tx.get('ended_at')) or 'Ladevorgang aktiv'),
        ('Dauer', f"{ctx['duration_minutes']:.0f} Minuten"), ('Ladezeit', f"{float(ctx['timing'].get('charging_seconds',0))/60:.1f} Minuten"),
        ('Standzeit', f"{float(ctx['timing'].get('stand_seconds',0))/60:.1f} Minuten"), ('Anschlussdauer', f"{float(ctx['timing'].get('connection_seconds',0))/60:.1f} Minuten"),
        ('Nachbelegung', ('—' if not ctx['post_session_occupancy'].get('tracked') else f"{float(ctx['post_session_occupancy'].get('seconds') or 0)/60:.1f} Minuten")),
        ('Connector frei', (_local_text(ctx['post_session_occupancy'].get('unplugged_at')) or ('läuft noch' if ctx['post_session_occupancy'].get('active') else '—'))),
        ('Connector', tx.get('connector_id') or '—'), ('Energie', f"{ctx['energy']:.2f} kWh"), ('Durchschnittsleistung', '—' if ctx['avg_power'] is None else f"{ctx['avg_power']:.1f} kW"),
        ('Maximale Leistung', '—' if (tx.get('sampled_peak_kw') is None and tx.get('max_power_kw') is None) else f"{float(tx.get('sampled_peak_kw') if tx.get('sampled_peak_kw') is not None else tx.get('max_power_kw') or 0):.1f} kW"),
        ('Stop-Grund', tx.get('stop_reason') or '—'),
    ])
    story += section('Zuordnung', [
        ('RFID', tx.get('id_tag') or '—'), ('Benutzer', user.get('name') or 'Nicht zugeordnet'),
        ('Fahrzeug', vehicle.get('name') or 'Nicht zugeordnet'), ('Kennzeichen', vehicle.get('plate') or '—'), ('Ladepunkt', cp_name),
        ('OCPP Transaction ID', tx.get('transaction_id') or '—'),
    ])
    story += section('Kosten & Tarif', [
        ('Tarif', ctx['tariff']), ('Berechnete Kosten', '—' if ctx['cost'] is None else f"{ctx['cost']:.2f} EUR"),
        ('Abrechnungsgrundlage', 'Keine historische Tarifinformation gespeichert' if ctx['price_cents'] is None else f"{ctx['energy']:.2f} kWh × {float(ctx['price_cents'])/100:.2f} EUR/kWh"),
    ])

    samples = db.meter_samples_for_transaction(tx['id'], 240)
    story += [Paragraph('Messwerte', styles['Section']), Paragraph(f'{len(samples)} gespeicherte Messpunkte in dieser Session.', styles['Subtle']), Spacer(1, 5)]
    if samples:
        sample_rows=[[Paragraph('<b>Zeit</b>',styles['Label']),Paragraph('<b>Leistung</b>',styles['Label']),Paragraph('<b>Energie</b>',styles['Label']),Paragraph('<b>SoC</b>',styles['Label'])]]
        for m in reversed(samples[-12:]):
            sample_rows.append([Paragraph(_local_text(m.get('sampled_at') or m.get('ts')) or '—',styles['Subtle']), Paragraph(f"{float(m.get('power_kw') or 0):.1f} kW",styles['Value']), Paragraph(f"{float(m.get('energy_kwh') or 0):.2f} kWh",styles['Value']), Paragraph('—' if m.get('soc_percent') is None else f"{float(m['soc_percent']):.0f} %",styles['Value'])])
        st=Table(sample_rows,colWidths=[70*mm,35*mm,35*mm,32*mm],repeatRows=1)
        st.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#f4f6f8')),('GRID',(0,0),(-1,-1),.25,colors.HexColor('#e7eaed')),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]))
        story += [st]
    else:
        story += [Paragraph('Keine Messwerte für diese Session gespeichert.', styles['Subtle'])]

    story += [Spacer(1, 12), HRFlowable(width='100%', thickness=.7, color=colors.HexColor('#dfe4e8')), Spacer(1, 6), Paragraph(f'Erstellt durch {db.branding_settings()["product_name"]} · Einzelner Ladevorgang', styles['Subtle'])]
    doc.build(story)
    pdf = buffer.getvalue()
    return StreamingResponse(iter([pdf]), media_type='application/pdf', headers={
        'Content-Disposition': f'attachment; filename=ladevorgang-{transaction_id}.pdf'
    })


@app.get("/api/transactions/{transaction_id}")
async def api_transaction(transaction_id: int):
    tx = db.get_transaction(transaction_id)
    if not tx:
        raise HTTPException(404, "Ladevorgang nicht gefunden")
    return {
        "transaction": tx,
        "vehicle": db.get_vehicle(tx.get("vehicle_id")) if tx.get("vehicle_id") else None,
        "vehicles": db.list_vehicles(),
        "meter_samples": db.meter_samples_for_transaction(tx["id"], 240),
        "events": db.events_for_transaction(tx["id"], 240),
        "live": db.transaction_live_state(tx["id"]),
        "timing": db.transaction_time_breakdown(tx["id"]),
        "post_session_occupancy": db.transaction_post_session_occupancy(tx["id"]),
    }



class VehiclePayload(BaseModel):
    name: str
    make: str | None = None
    model: str | None = None
    plate: str | None = None
    battery_kwh: float | None = None
    ac_power_kw: float | None = None
    dc_power_kw: float | None = None
    range_km: float | None = None
    drivetrain: str | None = None
    assigned_charge_point: str | None = None
    driver: str | None = None

@app.put("/api/transactions/{transaction_id}/vehicle")
async def set_transaction_vehicle(transaction_id: int, payload: dict):
    if not db.get_transaction(transaction_id):
        raise HTTPException(404, "Ladevorgang nicht gefunden")
    vehicle_id = payload.get("vehicle_id")
    if vehicle_id in ("", None):
        vehicle_id = None
    else:
        try:
            vehicle_id = int(vehicle_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "Ungültige Fahrzeug-ID")
    if not db.set_transaction_vehicle(transaction_id, vehicle_id):
        raise HTTPException(400, "Fahrzeug konnte nicht zugeordnet werden")
    return {"ok": True, "transaction": db.get_transaction(transaction_id)}


@app.get("/api/vehicles")
async def api_vehicles():
    vehicles = db.list_vehicles()
    cps = {c["id"]: c for c in db.list_charge_points()}
    for v in vehicles:
        cp = cps.get(v.get("assigned_charge_point"))
        v["charge_point_status"] = cp.get("status") if cp else None
        v["charge_point_power_kw"] = cp.get("power_kw") if cp else None
        v["charge_point_power_source"] = cp.get("power_source") if cp else None
        user = next((u for u in db.list_users() if u.get("name") == v.get("driver")), None)
        v["rfid"] = user.get("rfid") if user else None
    return {"vehicles": vehicles}


@app.get("/api/vehicles/{vehicle_id}/users")
async def api_vehicle_users(vehicle_id: int):
    if not db.get_vehicle(vehicle_id):
        raise HTTPException(404, "Fahrzeug nicht gefunden")
    return {"users": db.vehicle_users(vehicle_id)}


@app.post("/api/vehicles/{vehicle_id}/users/{user_id}")
async def assign_vehicle_user(vehicle_id: int, user_id: int, payload: dict | None = None):
    payload = payload or {}
    primary = bool(payload.get("primary", False))
    if not db.assign_vehicle_user(vehicle_id, user_id, primary=primary):
        raise HTTPException(400, "Benutzer konnte dem Fahrzeug nicht zugeordnet werden")
    return {"ok": True, "users": db.vehicle_users(vehicle_id)}


@app.delete("/api/vehicles/{vehicle_id}/users/{user_id}")
async def unassign_vehicle_user(vehicle_id: int, user_id: int):
    if not db.unassign_vehicle_user(vehicle_id, user_id):
        raise HTTPException(404, "Zuordnung nicht gefunden")
    return {"ok": True}


@app.get("/api/vehicles/{vehicle_id}")
async def api_vehicle(vehicle_id: int):
    details = db.vehicle_details(vehicle_id)
    if not details:
        raise HTTPException(404, "Fahrzeug nicht gefunden")
    return details


@app.post("/api/vehicles")
async def create_vehicle(payload: VehiclePayload):
    if not payload.name.strip():
        raise HTTPException(400, "Fahrzeugname ist erforderlich")
    vehicle_id = db.create_vehicle(**payload.model_dump())
    return {"ok": True, "id": vehicle_id, "vehicle": db.get_vehicle(vehicle_id)}


@app.put("/api/vehicles/{vehicle_id}")
async def update_vehicle(vehicle_id: int, payload: VehiclePayload):
    if not payload.name.strip():
        raise HTTPException(400, "Fahrzeugname ist erforderlich")
    if not db.update_vehicle(vehicle_id, **payload.model_dump()):
        raise HTTPException(404, "Fahrzeug nicht gefunden")
    return {"ok": True, "vehicle": db.get_vehicle(vehicle_id)}


@app.delete("/api/vehicles/{vehicle_id}")
async def delete_vehicle(vehicle_id: int):
    if not db.deactivate_vehicle(vehicle_id):
        raise HTTPException(404, "Fahrzeug nicht gefunden")
    return {"ok": True}



def _validate_vehicle_image(upload: UploadFile):
    content_type = (upload.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, "Nur JPG, PNG oder WebP sind erlaubt.")
    return ALLOWED_IMAGE_TYPES[content_type]


async def _store_vehicle_image(vehicle_id: int, upload: UploadFile):
    suffix = _validate_vehicle_image(upload)
    data = await upload.read(MAX_VEHICLE_IMAGE_BYTES + 1)
    if len(data) > MAX_VEHICLE_IMAGE_BYTES:
        raise HTTPException(413, "Das Fahrzeugbild darf maximal 5 MB groß sein.")
    valid_signature = (
        (suffix == ".jpg" and data.startswith(b"\xff\xd8\xff"))
        or (suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (suffix == ".webp" and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )
    if not valid_signature:
        raise HTTPException(400, "Die Bilddatei ist ungültig oder passt nicht zum Dateityp.")
    filename = f"vehicle-{vehicle_id}-{secrets.token_hex(12)}{suffix}"
    target = MEDIA_DIR / filename
    target.write_bytes(data)
    old = db.get_vehicle(vehicle_id)
    old_path = old.get("image_path") if old else None
    db.set_vehicle_image(vehicle_id, f"/media/{filename}")
    if old_path:
        old_file = MEDIA_DIR / Path(old_path).name
        if old_file.exists() and old_file.is_file():
            old_file.unlink()
    return f"/media/{filename}"


async def _store_user_image(user_id: int, upload: UploadFile):
    suffix=_validate_vehicle_image(upload)
    data=await upload.read(MAX_VEHICLE_IMAGE_BYTES+1)
    if len(data)>MAX_VEHICLE_IMAGE_BYTES:
        raise HTTPException(413,"Das Profilbild darf maximal 5 MB groß sein.")
    valid_signature=(
        (suffix==".jpg" and data.startswith(b"\xff\xd8\xff"))
        or (suffix==".png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (suffix==".webp" and len(data)>=12 and data[:4]==b"RIFF" and data[8:12]==b"WEBP")
    )
    if not valid_signature:
        raise HTTPException(400,"Die Bilddatei ist ungültig oder passt nicht zum Dateityp.")
    filename=f"user-{user_id}-{secrets.token_hex(12)}{suffix}"
    target=MEDIA_DIR/filename
    target.write_bytes(data)
    old=db.get_user(user_id)
    old_path=old.get("image_path") if old else None
    db.set_user_image(user_id,f"/media/{filename}")
    if old_path:
        old_file=MEDIA_DIR/Path(old_path).name
        if old_file.exists() and old_file.is_file():
            old_file.unlink()
    return f"/media/{filename}"


@app.post("/api/vehicles/{vehicle_id}/image")
async def upload_vehicle_image(vehicle_id: int, image: UploadFile = File(...)):
    if not db.get_vehicle(vehicle_id):
        raise HTTPException(404, "Fahrzeug nicht gefunden")
    image_path = await _store_vehicle_image(vehicle_id, image)
    return {"ok": True, "image_path": image_path}


@app.delete("/api/vehicles/{vehicle_id}/image")
async def delete_vehicle_image(vehicle_id: int):
    vehicle = db.get_vehicle(vehicle_id)
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden")
    image_path = vehicle.get("image_path")
    db.set_vehicle_image(vehicle_id, None)
    if image_path:
        target = MEDIA_DIR / Path(image_path).name
        if target.exists() and target.is_file():
            target.unlink()
    return {"ok": True}


class UserPayload(BaseModel):
    name: str
    role: str = "Fahrer"
    department: str | None = None
    email: str | None = None
    phone: str | None = None
    status: str = "Aktiv"
    monthly_kwh_limit: float | None = None
    monthly_limit_mode: str = "warn"
    charge_access_mode: str | None = None
    allowed_charge_point_ids: list[str] | None = None

class RFIDPayload(BaseModel):
    uid: str
    label: str | None = None
    user_id: int | None = None
    vehicle_id: int | None = None
    status: str = "Aktiv"
    expires_at: str | None = None
    notes: str | None = None

class RFIDReplacePayload(BaseModel):
    uid: str
    label: str | None = None
    expires_at: str | None = None
    notes: str | None = None

class RFIDRequestStatusPayload(BaseModel):
    status: str
    resolution_note: str | None = None

@app.get("/api/users")
async def api_users():
    return {"users": db.list_users_rich()}

@app.get("/api/users/{user_id}")
async def api_user(user_id: int, tx_page: int = 1, tx_page_size: int = 10):
    details=db.user_details(user_id, transaction_page=tx_page, transaction_page_size=tx_page_size)
    if not details: raise HTTPException(404,"Benutzer nicht gefunden")
    return details

@app.post("/api/users")
async def create_user(payload: UserPayload):
    if not payload.name.strip():
        raise HTTPException(400,"Name ist erforderlich")
    if payload.charge_access_mode not in (None,"all","selected"):
        raise HTTPException(400,"Ungültige Ladeberechtigung")
    values=payload.model_dump()
    values.update({"gamification_enabled":False,"weekly_hours":None,"budget_source":"manual"})
    try:
        uid=db.create_user(**values)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    user=db.get_user(uid) or {}
    return {"ok":True,"user":user}


@app.put("/api/users/{user_id}")
async def update_user(request:Request,user_id: int,payload: UserPayload):
    if not payload.name.strip():
        raise HTTPException(400,"Name ist erforderlich")
    if payload.charge_access_mode not in (None,"all","selected"):
        raise HTTPException(400,"Ungültige Ladeberechtigung")
    before=db.get_user(user_id)
    if not before:
        raise HTTPException(404,"Benutzer nicht gefunden")
    before_access=db.user_charge_access(user_id) or {"mode":"all","charge_point_ids":[]}
    values=payload.model_dump()
    values.update({"gamification_enabled":False,"weekly_hours":None,"budget_source":"manual"})
    try:
        updated=db.update_user(user_id,**values)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    if not updated:
        raise HTTPException(404,"Benutzer nicht gefunden")
    _schedule_local_list_sync("Benutzerdaten / Ladelimit geändert")
    user=db.get_user(user_id) or {}
    old_budget=before.get("monthly_kwh_limit")
    new_budget=user.get("monthly_kwh_limit")
    if old_budget!=new_budget or before.get("monthly_limit_mode")!=user.get("monthly_limit_mode"):
        auth=getattr(request.state,"auth_user",None) or {}
        def n(v):
            return "—" if v is None else f"{float(v):g} kWh"
        db.add_activity(
            system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),
            action="Optionales Ladebudget geändert",category="Ladebenutzer",
            target=f"{user.get('name') or 'Benutzer'} · #{user_id}",
            details=f"Monatsbudget: {n(old_budget)} → {n(new_budget)} · Verhalten: {before.get('monthly_limit_mode') or 'warn'} → {user.get('monthly_limit_mode') or 'warn'}"
        )
    after_access=db.user_charge_access(user_id) or {"mode":"all","charge_point_ids":[]}
    if before_access!=after_access:
        auth=getattr(request.state,"auth_user",None) or {}
        before_text="Alle Ladepunkte" if before_access["mode"]=="all" else (", ".join(before_access["charge_point_ids"]) or "Keine Ladepunkte")
        after_text="Alle Ladepunkte" if after_access["mode"]=="all" else (", ".join(after_access["charge_point_ids"]) or "Keine Ladepunkte")
        db.add_activity(
            system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),
            action="Ladeberechtigung geändert",category="Ladebenutzer",
            target=f"{user.get('name') or 'Benutzer'} · #{user_id}",
            details=f"{before_text} → {after_text}"
        )
    return {"ok":True,"user":user}


@app.post("/api/users/{user_id}/image")
async def upload_user_image(user_id:int, image:UploadFile=File(...)):
    if not db.get_user(user_id):
        raise HTTPException(404,"Benutzer nicht gefunden")
    image_path=await _store_user_image(user_id,image)
    return {"ok":True,"image_path":image_path}


@app.delete("/api/users/{user_id}/image")
async def delete_user_image(user_id:int):
    user=db.get_user(user_id)
    if not user:
        raise HTTPException(404,"Benutzer nicht gefunden")
    image_path=user.get("image_path")
    db.set_user_image(user_id,None)
    if image_path:
        target=MEDIA_DIR/Path(image_path).name
        if target.exists() and target.is_file():
            target.unlink()
    return {"ok":True}


@app.get("/api/users/{user_id}/delete-check")
async def user_delete_check(user_id:int):
    result=db.user_delete_check(user_id)
    if result is None:
        raise HTTPException(404,"Benutzer nicht gefunden")
    return result


@app.delete("/api/users/{user_id}")
async def delete_user(request:Request,user_id: int):
    user=db.get_user(user_id)
    if not user:
        raise HTTPException(404,"Benutzer nicht gefunden")
    if not db.deactivate_user(user_id):
        raise HTTPException(404,"Benutzer nicht gefunden")
    _schedule_local_list_sync("Benutzer deaktiviert")
    auth=getattr(request.state,"auth_user",None) or {}
    db.add_activity(system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),action="Ladebenutzer deaktiviert",category="Ladebenutzer",target=f"{user.get('name') or 'Benutzer'} · #{user_id}",details="Benutzer und zugeordnete RFID-Karten deaktiviert; Historie bleibt erhalten")
    return {"ok":True,"mode":"deactivated"}


@app.delete("/api/users/{user_id}/permanent")
async def delete_user_permanent(request:Request,user_id:int):
    user=db.get_user(user_id)
    if not user:
        raise HTTPException(404,"Benutzer nicht gefunden")
    image_path=user.get("image_path")
    ok,reason,check=db.delete_user_permanently(user_id)
    if not ok:
        if reason=="history":
            detail="Endgültiges Löschen ist nicht möglich, weil schützenswerte Historie vorhanden ist."
            if check and check.get("reasons"):
                detail+=" "+" · ".join(check["reasons"])
            raise HTTPException(409,detail)
        raise HTTPException(404,"Benutzer nicht gefunden")
    if image_path:
        target=MEDIA_DIR/Path(image_path).name
        if target.exists() and target.is_file():
            target.unlink()
    _schedule_local_list_sync("Ladebenutzer endgültig gelöscht")
    auth=getattr(request.state,"auth_user",None) or {}
    db.add_activity(system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),action="Ladebenutzer endgültig gelöscht",category="Ladebenutzer",target=f"{user.get('name') or 'Benutzer'} · #{user_id}",details="Keine schützenswerte Lade-/Bonus-/Eventhistorie vorhanden; Stammdaten und technische Zuordnungen entfernt")
    return {"ok":True,"mode":"deleted","removed":(check or {}).get("removable",{})}

class UserHistoryPurgePayload(BaseModel):
    confirmation: str


@app.delete("/api/users/{user_id}/purge")
async def purge_user_history(request:Request,user_id:int,payload:UserHistoryPurgePayload):
    auth=getattr(request.state,"auth_user",None) or {}
    if auth.get("role")!="admin":
        raise HTTPException(403,"Administratorrechte erforderlich")
    user=db.get_user(user_id)
    if not user:
        raise HTTPException(404,"Benutzer nicht gefunden")
    if str(payload.confirmation or "").strip()!=str(user.get("name") or "").strip():
        raise HTTPException(400,"Zur Sicherheitsbestätigung muss der Benutzername exakt eingegeben werden.")
    image_path=user.get("image_path")
    ok,reason,check=db.purge_user_with_history(user_id)
    if not ok:
        if reason=="unsafe":
            detail="Komplettlöschung ist momentan gesperrt."
            if check and check.get("purge_reasons"):
                detail+=" "+" · ".join(check["purge_reasons"])
            raise HTTPException(409,detail)
        raise HTTPException(404,"Benutzer nicht gefunden")
    if image_path:
        target=MEDIA_DIR/Path(image_path).name
        if target.exists() and target.is_file():
            target.unlink()
    _schedule_local_list_sync("Ladebenutzer inklusive Test-/Fehldaten gelöscht")
    removed=(check or {}).get("removable",{})
    db.add_activity(
        system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),
        action="Ladebenutzer inkl. Historie endgültig gelöscht",category="Ladebenutzer",
        target=f"{user.get('name') or 'Benutzer'} · #{user_id}",
        details=(
            f"Explizite Admin-Sonderlöschung: {int(removed.get('transactions') or 0)} Ladevorgänge, "
            f"{int(removed.get('meter_samples') or 0)} Messwerte und zugehörige Test-/Fehldaten entfernt"
        ),
    )
    return {"ok":True,"mode":"purged","removed":removed}


@app.post("/api/users/{user_id}/vehicles/{vehicle_id}")
async def assign_user_vehicle(user_id: int, vehicle_id: int):
    if not db.assign_user_vehicle(user_id, vehicle_id): raise HTTPException(400,"Fahrzeug konnte nicht zugeordnet werden")
    return {"ok":True}

@app.delete("/api/users/{user_id}/vehicles/{vehicle_id}")
async def unassign_user_vehicle(user_id: int, vehicle_id: int):
    if not db.unassign_user_vehicle(user_id, vehicle_id): raise HTTPException(404,"Zuordnung nicht gefunden")
    return {"ok":True}

@app.get("/api/rfid")
async def api_rfid(): return {"cards":db.list_rfid_cards()}

@app.get("/api/rfid/requests")
async def api_rfid_requests(status: str | None = None):
    return {"requests":db.list_rfid_replacement_requests(status)}

@app.post("/api/rfid/requests/{request_id}/status")
async def api_rfid_request_status(request_id:int,payload:RFIDRequestStatusPayload):
    try: ok=db.update_rfid_replacement_request(request_id,payload.status,payload.resolution_note)
    except ValueError as exc: raise HTTPException(400,str(exc))
    if not ok: raise HTTPException(404,"Ersatzanfrage nicht gefunden")
    return {"ok":True}


@app.delete("/api/rfid/requests/{request_id}")
async def api_delete_rfid_request(request_id:int):
    deleted=db.delete_rfid_replacement_request(request_id)
    if not deleted: raise HTTPException(404,"Ersatzanfrage nicht gefunden")
    return {"ok":True,"deleted_id":request_id,"card_id":deleted.get("card_id"),"status":deleted.get("status")}

@app.post("/api/rfid")
async def create_rfid(payload: RFIDPayload):
    try: cid=db.create_rfid_card(**payload.model_dump())
    except ValueError as exc: raise HTTPException(400,str(exc))
    _schedule_local_list_sync("RFID-Karte angelegt")
    return {"ok":True,"card":db.get_rfid_card(cid)}

@app.get("/api/rfid/{card_id}/detail")
async def api_rfid_detail(card_id:int):
    detail=db.rfid_card_detail(card_id)
    if not detail: raise HTTPException(404,"RFID-Karte nicht gefunden")
    return detail

@app.post("/api/rfid/{card_id}/replace")
async def api_replace_rfid(card_id:int,payload:RFIDReplacePayload):
    try: new_id=db.replace_rfid_card(card_id,payload.uid,payload.label,payload.expires_at,payload.notes)
    except ValueError as exc: raise HTTPException(400,str(exc))
    _schedule_local_list_sync("RFID-Ersatzkarte ausgegeben")
    return {"ok":True,"card":db.get_rfid_card(new_id),"replaced_card_id":card_id}

@app.put("/api/rfid/{card_id}")
async def update_rfid(card_id: int,payload: RFIDPayload):
    try: ok=db.update_rfid_card(card_id,**payload.model_dump())
    except ValueError as exc: raise HTTPException(400,str(exc))
    if not ok: raise HTTPException(404,"RFID-Karte nicht gefunden")
    _schedule_local_list_sync("RFID-Karte geändert")
    return {"ok":True,"card":db.get_rfid_card(card_id)}

@app.delete("/api/rfid/{card_id}")
async def delete_rfid(card_id: int):
    if not db.set_rfid_status(card_id,"Deaktiviert","Administrativ deaktiviert"): raise HTTPException(404,"RFID-Karte nicht gefunden")
    _schedule_local_list_sync("RFID-Karte deaktiviert")
    return {"ok":True}

@app.get("/api/rfid/local-list")
async def api_rfid_local_list():
    states=db.local_list_states(); version=db.rfid_local_list_version(); authorized=len(db.rfid_local_list_full())
    for item in states:
        item["online"]=is_connected(item.get("id"))
        item["authorized_count"]=len(db.rfid_local_list_full(item.get("id")))
        if item.get("station_entry_count") is None and item.get("status")=="Synchronisiert" and item.get("last_update_type")=="Full":
            response=str(item.get("last_response") or "").strip().casefold()
            if response.startswith("accepted"):
                item["station_entry_count"]=item["authorized_count"]
        item["backend_authorized_count"]=authorized
    return {"version":version,"authorized_count":authorized,"pending_count":sum(1 for x in states if x.get("pending")),"states":states}

@app.post("/api/rfid/local-list/sync")
async def api_rfid_local_list_sync(payload: dict, request: Request):
    cp_id=str(payload.get("charge_point_id") or "").strip(); force_full=bool(payload.get("force_full",True))
    if cp_id:
        result=await sync_local_list(cp_id,force_full=force_full,reason="Manuell durch Admin")
        return {"ok":bool(result.get("ok")),"result":result}
    results=await sync_pending_local_lists(reason="Manuell alle Ladepunkte",force_full=force_full)
    return {"ok":True,"results":results}


@app.post("/api/rfid/local-list/offline-auth")
async def api_rfid_local_list_offline_auth(payload: dict):
    cp_id=str(payload.get("charge_point_id") or "").strip()
    if not cp_id:
        raise HTTPException(400,"Ladepunkt fehlt")
    if not db.get_charge_point(cp_id):
        raise HTTPException(404,"Ladepunkt nicht gefunden")
    result=await verify_offline_authorization(cp_id)
    if result.get("offline"):
        raise HTTPException(409,result.get("detail") or "Ladepunkt ist offline")
    return {"ok":bool(result.get("ok")),"result":result}


def _remote_audit(request: Request, cp_id: str, action: str, details: str = "", status_code: int = 200):
    auth=getattr(request.state,"auth_user",None) or {}
    db.add_activity(system_user_id=auth.get("id"),username=auth.get("username"),display_name=auth.get("display_name"),action=action,category="Remote-Steuerung",target=cp_id,method=request.method,path=request.url.path,details=details,status_code=status_code)

def _remote_history(cp_id: str, limit: int = 20):
    wanted={"RemoteStartTransaction","RemoteStopTransaction","UnlockConnector","ChangeAvailability","Reset","CapabilityProbe"}
    rows=[]
    for ev in db.events_for_charge_point(cp_id,120):
        if ev.get("event_type") in wanted:
            rows.append(ev)
            if len(rows)>=limit: break
    return rows

@app.get("/api/remote-control/{cp_id}/state")
async def remote_control_state(cp_id: str):
    cp_id=unquote(cp_id); cp=db.get_charge_point(cp_id)
    if not cp: raise HTTPException(404,"Ladepunkt nicht gefunden")
    active=db.active_transactions_for_charge_point(cp_id,20)
    cards=[c for c in db.list_rfid_cards() if c.get("status")=="Aktiv" and c.get("user_id")]
    return {"online":is_connected(cp_id),"connectors":db.connectors_for_charge_point(cp_id),"active_transactions":active,"rfid_cards":cards,
            "history":_remote_history(cp_id),"capability_snapshot":db.get_ocpp_capability_snapshot(cp_id),
            "remote_capabilities":db.remote_capability_profile(cp_id)}


@app.post("/api/remote-control/{cp_id}/capabilities")
async def remote_capabilities(cp_id: str, request: Request):
    cp_id=unquote(cp_id)
    if not db.get_charge_point(cp_id):
        raise HTTPException(404,"Ladepunkt nicht gefunden")
    try:
        result=await probe_capabilities(cp_id)
        _remote_audit(request,cp_id,"OCPP-Fähigkeiten geprüft",f"profiles={','.join(result.get('profiles') or [])}; response_ms={result.get('response_ms')}")
        return {"ok":True,"capability_snapshot":result}
    except RuntimeError as exc:
        _remote_audit(request,cp_id,"OCPP-Fähigkeitsprüfung fehlgeschlagen",str(exc),409)
        raise HTTPException(504 if "Zeitüberschreitung" in str(exc) else 409,str(exc))
    except Exception as exc:
        _remote_audit(request,cp_id,"OCPP-Fähigkeitsprüfung fehlgeschlagen",type(exc).__name__,502)
        logging.exception("OCPP-Fähigkeitsprüfung fehlgeschlagen")
        raise HTTPException(502,f"Fähigkeitsprüfung fehlgeschlagen: {type(exc).__name__}")


@app.delete("/api/remote-control/{cp_id}/capabilities/learned")
async def reset_learned_remote_capabilities(cp_id: str, request: Request):
    cp_id=unquote(cp_id)
    if not db.get_charge_point(cp_id):
        raise HTTPException(404,"Ladepunkt nicht gefunden")
    removed=db.reset_remote_capability_profile(cp_id)
    _remote_audit(request,cp_id,"Erlernte OCPP-Fähigkeiten zurückgesetzt",f"entries={removed}")
    return {"ok":True,"removed":removed,"remote_capabilities":db.remote_capability_profile(cp_id)}


@app.post("/api/remote-control/{cp_id}/start")
async def remote_start(cp_id: str, payload: dict, request: Request):
    cp_id=unquote(cp_id); tag=str(payload.get("id_tag") or "").strip(); connector=payload.get("connector_id")
    decision=db.authorization_decision(tag)
    if not decision.get("accepted"): raise HTTPException(409,decision.get("reason") or "RFID nicht autorisiert")
    try:
        result=await remote_command(cp_id,"start",id_tag=tag,connector_id=connector); _remote_audit(request,cp_id,"RemoteStart gesendet",f"connector={connector}; idTag={tag}; status={result.get('status')}"); return {"ok":True,**result}
    except RuntimeError as exc:
        _remote_audit(request,cp_id,"Remote-Befehl fehlgeschlagen",str(exc),409); raise HTTPException(504 if "Zeitüberschreitung" in str(exc) else 409,str(exc))
    except ValueError as exc: raise HTTPException(400,str(exc))
    except Exception as exc: _remote_audit(request,cp_id,"RemoteStart fehlgeschlagen",type(exc).__name__,502); logging.exception("RemoteStart fehlgeschlagen"); raise HTTPException(502,f"OCPP-Befehl fehlgeschlagen: {type(exc).__name__}")


@app.post("/api/remote-control/{cp_id}/stop")
async def remote_stop(cp_id: str, payload: dict, request: Request):
    cp_id=unquote(cp_id); tx_id=int(payload.get("transaction_id") or 0); tx=db.get_transaction(tx_id)
    if not tx or tx.get("charge_point_id")!=cp_id or tx.get("status")!="Active": raise HTTPException(404,"Aktiver Ladevorgang nicht gefunden")
    ocpp_tx=tx.get("transaction_id") or tx_id
    try:
        result=await remote_command(cp_id,"stop",transaction_id=ocpp_tx); _remote_audit(request,cp_id,"RemoteStop gesendet",f"session={tx_id}; ocpp_tx={ocpp_tx}; status={result.get('status')}"); return {"ok":True,**result}
    except RuntimeError as exc:
        _remote_audit(request,cp_id,"Remote-Befehl fehlgeschlagen",str(exc),409); raise HTTPException(504 if "Zeitüberschreitung" in str(exc) else 409,str(exc))
    except Exception as exc: _remote_audit(request,cp_id,"RemoteStop fehlgeschlagen",type(exc).__name__,502); logging.exception("RemoteStop fehlgeschlagen"); raise HTTPException(502,f"OCPP-Befehl fehlgeschlagen: {type(exc).__name__}")


@app.post("/api/remote-control/{cp_id}/unlock")
async def remote_unlock(cp_id: str, payload: dict, request: Request):
    cp_id=unquote(cp_id)
    try:
        connector=int(payload.get("connector_id") or 0);
        if connector <= 0: raise ValueError("Zum Entriegeln ist ein konkreter Connector erforderlich")
        result=await remote_command(cp_id,"unlock",connector_id=connector); _remote_audit(request,cp_id,"Connector entriegelt",f"connector={connector}; status={result.get('status')}"); return {"ok":True,**result}
    except RuntimeError as exc:
        _remote_audit(request,cp_id,"Remote-Befehl fehlgeschlagen",str(exc),409); raise HTTPException(504 if "Zeitüberschreitung" in str(exc) else 409,str(exc))
    except ValueError as exc: raise HTTPException(400,str(exc))
    except Exception as exc: _remote_audit(request,cp_id,"UnlockConnector fehlgeschlagen",type(exc).__name__,502); logging.exception("UnlockConnector fehlgeschlagen"); raise HTTPException(502,f"OCPP-Befehl fehlgeschlagen: {type(exc).__name__}")


@app.post("/api/remote-control/{cp_id}/availability")
async def remote_availability(cp_id: str, payload: dict, request: Request):
    cp_id=unquote(cp_id)
    try:
        connector=int(payload.get("connector_id") or 0); availability=payload.get("availability_type"); result=await remote_command(cp_id,"availability",connector_id=connector,availability_type=availability); _remote_audit(request,cp_id,"Verfügbarkeit geändert",f"connector={connector}; type={availability}; status={result.get('status')}"); return {"ok":True,**result}
    except RuntimeError as exc:
        _remote_audit(request,cp_id,"Remote-Befehl fehlgeschlagen",str(exc),409); raise HTTPException(504 if "Zeitüberschreitung" in str(exc) else 409,str(exc))
    except ValueError as exc: raise HTTPException(400,str(exc))
    except Exception as exc: _remote_audit(request,cp_id,"ChangeAvailability fehlgeschlagen",type(exc).__name__,502); logging.exception("ChangeAvailability fehlgeschlagen"); raise HTTPException(502,f"OCPP-Befehl fehlgeschlagen: {type(exc).__name__}")


@app.post("/api/remote-control/{cp_id}/reset")
async def remote_reset(cp_id: str, payload: dict, request: Request):
    cp_id=unquote(cp_id)
    try:
        reset_type=payload.get("reset_type") or "Soft"; result=await remote_command(cp_id,"reset",reset_type=reset_type); _remote_audit(request,cp_id,"Reset gesendet",f"type={reset_type}; status={result.get('status')}"); return {"ok":True,**result}
    except RuntimeError as exc:
        _remote_audit(request,cp_id,"Remote-Befehl fehlgeschlagen",str(exc),409); raise HTTPException(504 if "Zeitüberschreitung" in str(exc) else 409,str(exc))
    except ValueError as exc: raise HTTPException(400,str(exc))
    except Exception as exc: _remote_audit(request,cp_id,"Reset fehlgeschlagen",type(exc).__name__,502); logging.exception("Reset fehlgeschlagen"); raise HTTPException(502,f"OCPP-Befehl fehlgeschlagen: {type(exc).__name__}")


class SystemUserPayload(BaseModel):
    username: str
    display_name: str
    role: str = "viewer"
    active: bool = True
    password: str | None = None


class PasswordChangePayload(BaseModel):
    current_password: str
    new_password: str


@app.get("/api/system-users")
async def api_system_users():
    return {"users": db.list_system_users()}


@app.post("/api/system-users")
async def api_create_system_user(payload: SystemUserPayload):
    if not payload.password:
        raise HTTPException(400,"Für neue Konten ist ein Passwort erforderlich")
    try:
        uid=db.create_system_user(payload.username,payload.display_name,_password_hash(payload.password),payload.role,payload.active)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    return {"ok":True,"user":db.get_system_user(uid)}


@app.put("/api/system-users/{user_id}")
async def api_update_system_user(user_id: int, payload: SystemUserPayload, request: Request):
    current=request.state.auth_user
    if current and current.get("id")==user_id and not payload.active:
        raise HTTPException(400,"Das aktuell angemeldete Konto kann nicht deaktiviert werden")
    if current and current.get("id")==user_id and payload.role!="admin":
        admins=[u for u in db.list_system_users() if u.get("role")=="admin" and int(u.get("active") or 0)==1]
        if len(admins)<=1: raise HTTPException(400,"Der letzte aktive Administrator kann nicht herabgestuft werden")
    try:
        password_hash=_password_hash(payload.password) if payload.password else None
        ok=db.update_system_user(user_id,payload.username,payload.display_name,payload.role,payload.active,password_hash)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    if not ok: raise HTTPException(404,"Systembenutzer nicht gefunden")
    if password_hash:
        token=request.cookies.get(SESSION_COOKIE)
        keep_hash=_session_hash(token) if current and current.get("id")==user_id and token else None
        db.delete_system_sessions_for_user(user_id, keep_hash)
    return {"ok":True,"user":db.get_system_user(user_id)}


@app.delete("/api/system-users/{user_id}")
async def api_delete_system_user(user_id: int, request: Request):
    if request.state.auth_user and request.state.auth_user.get("id")==user_id:
        raise HTTPException(400,"Das aktuell angemeldete Konto kann nicht gelöscht werden")
    ok,reason=db.delete_system_user(user_id)
    if not ok:
        if reason=='not_found': raise HTTPException(404,"Systembenutzer nicht gefunden")
        if reason=='last_admin': raise HTTPException(400,"Der letzte aktive Administrator kann nicht gelöscht werden")
        raise HTTPException(400,"Systembenutzer konnte nicht gelöscht werden")
    return {"ok":True}


@app.post("/api/account/password")
async def api_change_own_password(payload: PasswordChangePayload, request: Request):
    auth=request.state.auth_user
    account=db.get_system_user_auth(auth.get("username")) if auth else None
    if not account or not _password_ok(payload.current_password,account.get("password_hash") or ""):
        raise HTTPException(400,"Das aktuelle Passwort ist nicht korrekt")
    if _password_ok(payload.new_password,account.get("password_hash") or ""):
        raise HTTPException(400,"Das neue Passwort muss sich vom aktuellen Passwort unterscheiden")
    try: password_hash=_password_hash(payload.new_password)
    except ValueError as exc: raise HTTPException(400,str(exc))
    if not db.update_system_user(account['id'],account['username'],account['display_name'],account['role'],bool(account['active']),password_hash):
        raise HTTPException(404,"Konto nicht gefunden")
    token=request.cookies.get(SESSION_COOKIE)
    db.delete_system_sessions_for_user(account['id'], _session_hash(token) if token else None)
    return {"ok":True}


class TwoFactorPasswordPayload(BaseModel):
    current_password: str


class TwoFactorCodePayload(BaseModel):
    code: str


class TwoFactorProtectedPayload(BaseModel):
    current_password: str
    code: str


def _account_from_request(request: Request):
    auth=request.state.auth_user or {}
    account=db.get_system_user_auth(auth.get("username")) if auth else None
    if not account:
        raise HTTPException(401,"Konto nicht gefunden")
    return account


def _verify_account_factor(account, code, allow_recovery=True):
    value=str(code or "").strip()
    counter=totp.verify_code(account.get("totp_secret") or "",value,window=1)
    if counter is not None and db.accept_system_totp_counter(account["id"],counter):
        return "totp"
    if allow_recovery and len(totp.normalize_recovery_code(value))>=10:
        if db.consume_system_recovery_code(account["id"],totp.recovery_code_hash(value)):
            return "recovery"
    return None


@app.get("/account/security", response_class=HTMLResponse)
async def account_security_page(request: Request):
    auth=request.state.auth_user or {}
    return render(request,"account_security.html",page="",two_factor=db.system_user_2fa_state(auth.get("id")))


@app.get("/api/account/2fa/status")
async def api_account_2fa_status(request: Request):
    auth=request.state.auth_user or {}
    state=db.system_user_2fa_state(auth.get("id"))
    if not state: raise HTTPException(404,"Konto nicht gefunden")
    return {"two_factor":state}


@app.post("/api/account/2fa/setup")
async def api_account_2fa_setup(payload: TwoFactorPasswordPayload, request: Request):
    account=_account_from_request(request)
    if not _password_ok(payload.current_password,account.get("password_hash") or ""):
        raise HTTPException(400,"Das aktuelle Passwort ist nicht korrekt")
    if int(account.get("totp_enabled") or 0):
        raise HTTPException(409,"Zwei-Faktor-Authentifizierung ist bereits aktiv")
    secret=totp.generate_secret()
    db.set_system_user_totp_pending(account["id"],secret)
    issuer=(db.branding_settings().get("product_name") or "VoltCore").strip()
    uri=totp.provisioning_uri(secret,account.get("username") or account.get("display_name"),issuer)
    db.add_security_event("2FA setup started",severity="info",category="web_login",system_user_id=account.get("id"),username=account.get("username"),remote=_client_text(request),success=True)
    return {"ok":True,"secret":secret,"provisioning_uri":uri,"qr_url":"/api/account/2fa/setup/qr"}


@app.get("/api/account/2fa/setup/qr")
async def api_account_2fa_qr(request: Request):
    account=_account_from_request(request)
    material=db.system_user_totp_material(account["id"]) or {}
    secret=material.get("totp_pending_secret")
    if not secret:
        raise HTTPException(404,"Keine laufende 2FA-Einrichtung")
    issuer=(db.branding_settings().get("product_name") or "VoltCore").strip()
    uri=totp.provisioning_uri(secret,account.get("username") or account.get("display_name"),issuer)
    image=qrcode.make(uri)
    buffer=io.BytesIO(); image.save(buffer,format="PNG")
    return Response(buffer.getvalue(),media_type="image/png",headers={"Cache-Control":"no-store","Pragma":"no-cache"})


@app.post("/api/account/2fa/setup/confirm")
async def api_account_2fa_confirm(payload: TwoFactorCodePayload, request: Request):
    account=_account_from_request(request)
    material=db.system_user_totp_material(account["id"]) or {}
    secret=material.get("totp_pending_secret")
    if not secret:
        raise HTTPException(409,"Keine laufende 2FA-Einrichtung")
    if totp.verify_code(secret,payload.code,window=1) is None:
        raise HTTPException(400,"Der Sicherheitscode ist nicht korrekt")
    if not db.enable_system_user_totp(account["id"],secret):
        raise HTTPException(404,"Konto nicht gefunden")
    recovery_codes=totp.generate_recovery_codes(8)
    db.replace_system_recovery_codes(account["id"],[totp.recovery_code_hash(x) for x in recovery_codes])
    token=request.cookies.get(SESSION_COOKIE)
    db.delete_system_sessions_for_user(account["id"],_session_hash(token) if token else None)
    db.add_security_event("2FA enabled",severity="info",category="web_login",system_user_id=account.get("id"),username=account.get("username"),remote=_client_text(request),success=True,detail="recovery_codes=8")
    return {"ok":True,"recovery_codes":recovery_codes,"two_factor":db.system_user_2fa_state(account["id"])}


@app.post("/api/account/2fa/setup/cancel")
async def api_account_2fa_cancel(request: Request):
    account=_account_from_request(request)
    db.clear_system_user_totp_pending(account["id"])
    return {"ok":True}


@app.post("/api/account/2fa/recovery-codes")
async def api_account_2fa_recovery_codes(payload: TwoFactorProtectedPayload, request: Request):
    account=_account_from_request(request)
    if not int(account.get("totp_enabled") or 0):
        raise HTTPException(409,"Zwei-Faktor-Authentifizierung ist nicht aktiv")
    if not _password_ok(payload.current_password,account.get("password_hash") or ""):
        raise HTTPException(400,"Das aktuelle Passwort ist nicht korrekt")
    method=_verify_account_factor(account,payload.code,allow_recovery=True)
    if not method:
        raise HTTPException(400,"Der Sicherheitscode ist nicht korrekt")
    codes=totp.generate_recovery_codes(8)
    db.replace_system_recovery_codes(account["id"],[totp.recovery_code_hash(x) for x in codes])
    db.add_security_event("2FA recovery codes regenerated",severity="info",category="web_login",system_user_id=account.get("id"),username=account.get("username"),remote=_client_text(request),success=True,detail=f"factor={method}")
    return {"ok":True,"recovery_codes":codes,"two_factor":db.system_user_2fa_state(account["id"])}


@app.post("/api/account/2fa/disable")
async def api_account_2fa_disable(payload: TwoFactorProtectedPayload, request: Request):
    account=_account_from_request(request)
    if not int(account.get("totp_enabled") or 0):
        raise HTTPException(409,"Zwei-Faktor-Authentifizierung ist nicht aktiv")
    if not _password_ok(payload.current_password,account.get("password_hash") or ""):
        raise HTTPException(400,"Das aktuelle Passwort ist nicht korrekt")
    method=_verify_account_factor(account,payload.code,allow_recovery=True)
    if not method:
        raise HTTPException(400,"Der Sicherheitscode ist nicht korrekt")
    db.disable_system_user_totp(account["id"])
    token=request.cookies.get(SESSION_COOKIE)
    db.delete_system_sessions_for_user(account["id"],_session_hash(token) if token else None)
    db.add_security_event("2FA disabled",severity="warning",category="web_login",system_user_id=account.get("id"),username=account.get("username"),remote=_client_text(request),success=True,detail=f"factor={method}")
    return {"ok":True,"two_factor":db.system_user_2fa_state(account["id"])}


@app.delete("/api/system-users/{user_id}/2fa")
async def api_admin_reset_system_user_2fa(user_id: int, request: Request):
    current=request.state.auth_user or {}
    if int(current.get("id") or 0)==int(user_id):
        raise HTTPException(400,"Das eigene 2FA bitte unter „Mein Konto“ verwalten")
    target=db.get_system_user(user_id)
    if not target:
        raise HTTPException(404,"Systembenutzer nicht gefunden")
    db.disable_system_user_totp(user_id)
    db.delete_system_sessions_for_user(user_id)
    db.add_security_event("2FA admin reset",severity="warning",category="admin",system_user_id=current.get("id"),username=current.get("username"),remote=_client_text(request),success=True,detail=f"target={target.get('username')}")
    return {"ok":True,"user":db.get_system_user(user_id)}



class SecuritySettingsPayload(BaseModel):
    auth_mode: str = "off"
    reject_unknown: bool = False
    require_tls: bool = False
    require_subprotocol: bool = False


class OcppSecretPayload(BaseModel):
    secret: str | None = None


def _security_readiness(settings, active_cps, transport):
    total=len(active_cps)
    with_secret=sum(1 for x in active_cps if int(x.get("secret_configured") or 0))
    wss=sum(1 for x in active_cps if str(x.get("ocpp_last_transport") or "").upper()=="WSS")
    secret_ratio=(with_secret/total) if total else 1.0
    wss_ratio=(wss/total) if total else 1.0
    score=0
    checks=[]
    def add(key,label,ok,points,partial=0,detail=None):
        nonlocal score
        gained=points if ok else partial
        score+=gained
        checks.append({"key":key,"label":label,"ok":bool(ok),"points":points,"gained":gained,"detail":detail})
    mode=settings.get("auth_mode")
    add("auth","OCPP-Authentifizierung strikt",mode=="required",20,10 if mode=="configured" else 0,"Aktuell: "+({"required":"Strikt","configured":"Schrittweise","off":"Kompatibilitätsmodus"}.get(mode,mode or "Aus")))
    add("known","Unbekannte Ladepunkte werden abgelehnt",bool(settings.get("reject_unknown")),15)
    add("tls","WSS / TLS wird erzwungen",bool(settings.get("require_tls")),20)
    add("subprotocol","OCPP-1.6-Subprotocol wird erzwungen",bool(settings.get("require_subprotocol")),10)
    secret_points=round(20*secret_ratio)
    score+=secret_points
    checks.append({"key":"secrets","label":"Alle aktiven Ladepunkte besitzen ein Secret","ok":with_secret==total,"points":20,"gained":secret_points,"detail":f"{with_secret} von {total}"})
    wss_points=round(10*wss_ratio)
    score+=wss_points
    checks.append({"key":"wss","label":"Alle aktiven Ladepunkte wurden zuletzt per WSS gesehen","ok":wss==total,"points":10,"gained":wss_points,"detail":f"{wss} von {total}"})
    transport_ready=bool(transport.get("direct_tls_configured") or transport.get("trust_proxy_headers"))
    add("termination","TLS-Terminierung ist erkennbar konfiguriert",transport_ready,5,detail="Direktes TLS oder vertrauenswürdiger Reverse Proxy")
    score=max(0,min(100,int(score)))
    level="hardened" if score>=90 else "good" if score>=70 else "partial" if score>=45 else "open"
    label={"hardened":"Produktiv gehärtet","good":"Gut abgesichert","partial":"Teilweise gehärtet","open":"Offen / kompatibel"}[level]
    recommendations=[]
    for item in checks:
        if not item["ok"]:
            recommendations.append(item["label"])
    return {"score":score,"level":level,"label":label,"checks":checks,"recommendations":recommendations[:6]}


def _security_dashboard_summary():
    """Lightweight security snapshot for the 3-second dashboard poll."""
    settings=db.security_settings()
    cps=db.list_charge_point_security()
    active=[x for x in cps if int(x.get("onboarded",1) or 0) and not int(x.get("ignored",0) or 0) and not int(x.get("retired",0) or 0) and not int(x.get("archived",0) or 0)]
    transport={"direct_tls_configured":bool(os.getenv("OCPP_TLS_CERTFILE") and os.getenv("OCPP_TLS_KEYFILE")),"trust_proxy_headers":_trust_proxy_headers()}
    readiness=_security_readiness(settings,active,transport)
    events=db.security_event_summary(24)
    return {"score":readiness["score"],"level":readiness["level"],"label":readiness["label"],"recommendations":readiness["recommendations"][:3],"web_failures_24h":events["web_failed"],"ocpp_rejected_24h":events["ocpp_rejected"]}


def _security_overview_payload(request=None, event_page=1):
    settings=db.security_settings()
    cps=db.list_charge_point_security()
    active=[x for x in cps if int(x.get("onboarded",1) or 0) and not int(x.get("ignored",0) or 0) and not int(x.get("retired",0) or 0) and not int(x.get("archived",0) or 0)]
    with_secret=sum(1 for x in active if int(x.get("secret_configured") or 0))
    secure_transport=sum(1 for x in active if str(x.get("ocpp_last_transport") or "").upper()=="WSS")
    transport={"direct_tls_configured":bool(os.getenv("OCPP_TLS_CERTFILE") and os.getenv("OCPP_TLS_KEYFILE")),"tls_cert_env":bool(os.getenv("OCPP_TLS_CERTFILE")),"tls_key_env":bool(os.getenv("OCPP_TLS_KEYFILE")),"trust_proxy_headers":_trust_proxy_headers()}
    auth=getattr(getattr(request,"state",None),"auth_user",None) if request is not None else None
    sessions=db.active_system_session_counts(auth.get("id") if auth else None)
    system_users=db.list_system_users()
    two_factor_enabled=sum(1 for x in system_users if int(x.get("active") or 0) and int(x.get("totp_enabled") or 0))
    active_system_users=sum(1 for x in system_users if int(x.get("active") or 0))
    own_two_factor=db.system_user_2fa_state(auth.get("id")) if auth else None
    readiness=_security_readiness(settings,active,transport)
    event_page_data=db.security_events_page(event_page,10)
    return {
        "settings":settings,
        "summary":{"charge_points":len(active),"with_secret":with_secret,"without_secret":max(0,len(active)-with_secret),"last_wss":secure_transport},
        "readiness":readiness,
        "charge_points":cps,
        "events":event_page_data["items"],
        "event_pagination":{"page":event_page_data["page"],"page_size":event_page_data["page_size"],"pages":event_page_data["pages"],"total":event_page_data["total"]},
        "event_summary":db.security_event_summary(24),
        "sessions":sessions,
        "two_factor":{"enabled_users":two_factor_enabled,"active_users":active_system_users,"own_enabled":bool(own_two_factor and int(own_two_factor.get("totp_enabled") or 0)),"own_recovery_codes":int((own_two_factor or {}).get("recovery_codes_remaining") or 0)},
        "web":{"session_hours":SESSION_HOURS,"login_failure_limit":WEB_MAX_FAILURES,"login_window_minutes":WEB_FAILURE_WINDOW_MINUTES,"same_origin_guard":True,"security_headers":True,"cookie_secure_auto":True,"password_min_length":12,"password_hash_iterations":PBKDF2_ITERATIONS,"api_docs_enabled":ENABLE_API_DOCS,"two_factor_challenge_minutes":TWO_FACTOR_CHALLENGE_MINUTES},
        "ocpp_rate_limit":{"failure_limit":db.OCPP_AUTH_MAX_FAILURES,"window_minutes":db.OCPP_AUTH_FAILURE_WINDOW_MINUTES,"secret_min_length":16},
        "transport":transport,
    }


@app.get("/api/security/overview")
async def api_security_overview(request: Request, event_page: int = 1):
    return _security_overview_payload(request,event_page)


@app.post("/api/security/sessions/revoke-other")
async def api_security_revoke_other_sessions(request: Request):
    auth=request.state.auth_user or {}
    token=request.cookies.get(SESSION_COOKIE)
    if not token or not auth.get("id"):
        raise HTTPException(401,"Aktive Sitzung nicht gefunden")
    removed=db.delete_other_system_sessions(int(auth["id"]),_session_hash(token))
    db.add_security_event("Other web sessions revoked",severity="info",category="admin",system_user_id=auth.get("id"),username=auth.get("username"),remote=_client_text(request),success=True,detail=f"removed={removed}")
    return {"ok":True,"removed":removed,"sessions":db.active_system_session_counts(int(auth["id"]))}


@app.put("/api/security/settings")
async def api_security_settings(payload: SecuritySettingsPayload, request: Request):
    try:
        settings=db.set_security_settings(payload.auth_mode,payload.reject_unknown,payload.require_tls,payload.require_subprotocol)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    auth=request.state.auth_user or {}
    db.add_security_event("Security policy changed",severity="info",category="admin",system_user_id=auth.get("id"),username=auth.get("username"),success=True,detail=f"auth_mode={settings['auth_mode']}; reject_unknown={settings['reject_unknown']}; require_tls={settings['require_tls']}; require_subprotocol={settings['require_subprotocol']}")
    return {"ok":True,"settings":settings}


@app.post("/api/security/charge-points/{cp_id}/secret")
async def api_security_set_secret(cp_id: str, payload: OcppSecretPayload, request: Request):
    if not db.get_charge_point(cp_id):
        raise HTTPException(404,"Ladepunkt nicht gefunden")
    generated=not bool(str(payload.secret or "").strip())
    secret=str(payload.secret or "").strip() or secrets.token_urlsafe(18)
    try:
        db.set_charge_point_secret(cp_id,secret)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    auth=request.state.auth_user or {}
    db.add_security_event("OCPP secret set",severity="info",category="admin",charge_point_id=cp_id,system_user_id=auth.get("id"),username=auth.get("username"),success=True,detail="Secret erzeugt" if generated else "Secret manuell gesetzt")
    return {"ok":True,"charge_point":db.charge_point_security(cp_id),"secret":secret,"generated":generated,"show_once":True}


@app.delete("/api/security/charge-points/{cp_id}/secret")
async def api_security_clear_secret(cp_id: str, request: Request):
    settings=db.security_settings()
    if settings.get("auth_mode")=="required":
        raise HTTPException(409,"Im strikten Modus kann das OCPP-Secret nicht entfernt werden. Zuerst den Authentifizierungsmodus ändern.")
    if not db.clear_charge_point_secret(cp_id):
        raise HTTPException(404,"Ladepunkt nicht gefunden")
    auth=request.state.auth_user or {}
    db.add_security_event("OCPP secret removed",severity="warning",category="admin",charge_point_id=cp_id,system_user_id=auth.get("id"),username=auth.get("username"),success=True)
    return {"ok":True}


@app.get("/api/search")
async def api_global_search(q: str=""):
    return db.global_search(q,6)

def _report_period(period: str = "current_month", date_from: str | None = None, date_to: str | None = None):
    now=datetime.now(timezone.utc).astimezone(BERLIN_TZ)
    key=(period or "current_month").strip().lower()
    def month_start(dt): return dt.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    def add_months(dt, months):
        idx=dt.year*12+(dt.month-1)+months
        return dt.replace(year=idx//12,month=idx%12+1,day=1,hour=0,minute=0,second=0,microsecond=0)
    month_names=["Januar","Februar","März","April","Mai","Juni","Juli","August","September","Oktober","November","Dezember"]
    if key == "all":
        start=end=None; label="Gesamtzeitraum"
    elif key == "previous_month":
        end=month_start(now); start=add_months(end,-1); label=f"{month_names[start.month-1]} {start.year}"
    elif key == "current_year":
        start=now.replace(month=1,day=1,hour=0,minute=0,second=0,microsecond=0); end=start.replace(year=start.year+1); label=f"Jahr {start.year}"
    elif key == "last12":
        end=now; start=add_months(month_start(now),-11); label="Letzte 12 Monate"
    elif key == "custom":
        try:
            start=datetime.strptime(date_from,"%Y-%m-%d").replace(tzinfo=BERLIN_TZ) if date_from else None
            end=(datetime.strptime(date_to,"%Y-%m-%d")+timedelta(days=1)).replace(tzinfo=BERLIN_TZ) if date_to else None
        except (TypeError,ValueError):
            raise HTTPException(400,"Ungültiger benutzerdefinierter Zeitraum")
        if not start and not end: raise HTTPException(400,"Bitte mindestens ein Datum angeben")
        if start and end and start>=end: raise HTTPException(400,"Das Bis-Datum muss nach dem Von-Datum liegen")
        label=(start.strftime("%d.%m.%Y") if start else "Beginn")+" – "+((end-timedelta(days=1)).strftime("%d.%m.%Y") if end else "heute")
    else:
        key="current_month"; start=month_start(now); end=add_months(start,1); label=f"{month_names[start.month-1]} {start.year}"
    return {
        "key":key,"label":label,
        "start_at":start.astimezone(timezone.utc).isoformat() if start else None,
        "end_at":end.astimezone(timezone.utc).isoformat() if end else None,
        "date_from":date_from or "","date_to":date_to or "",
    }


def _report_data(period="current_month", date_from=None, date_to=None, user_id=None, vehicle_id=None, charge_point_id=None, cost_center=None, billing_group_id=None):
    ctx=_report_period(period,date_from,date_to)
    data=db.reporting_bundle(ctx["start_at"],ctx["end_at"],user_id,vehicle_id,charge_point_id,cost_center,billing_group_id)
    data["period"]=ctx
    data["filters"]={"user_id":user_id,"vehicle_id":vehicle_id,"charge_point_id":charge_point_id or "","cost_center":cost_center or "","billing_group_id":billing_group_id}
    data["generated_at"]=datetime.now(timezone.utc).isoformat()
    return data


@app.get("/api/reports/export.csv")
async def export_reports_csv(period: str="current_month", date_from: str|None=None, date_to: str|None=None, user_id: int|None=None, vehicle_id: int|None=None, charge_point_id: str|None=None, cost_center: str|None=None, billing_group_id: str|None=None):
    data=_report_data(period,date_from,date_to,user_id,vehicle_id,charge_point_id,cost_center,billing_group_id)
    output=io.StringIO(newline="")
    writer=csv.writer(output,delimiter=";")
    writer.writerow([f"{db.branding_settings()['product_name']} Abrechnungsnachweis",data["period"]["label"]])
    writer.writerow(["Erstellt",datetime.now(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")])
    writer.writerow([])
    writer.writerow(["Start","Ende","Benutzer","Fahrzeug","Kennzeichen","Ladepunkt","Connector","Energie kWh","Tarif","Preis ct/kWh","Kosten EUR","Kostenstelle","Abrechnungsgruppe","Quelle"])
    for row in data["transactions"]:
        writer.writerow([
            _local_text(row.get("started_at"),"%d.%m.%Y %H:%M"),_local_text(row.get("ended_at"),"%d.%m.%Y %H:%M"),
            row.get("user_name") or "Nicht zugeordnet",row.get("vehicle_name") or "Nicht zugeordnet",row.get("vehicle_plate") or "",
            row.get("charge_point_id") or "",row.get("connector_id") or "",f"{float(row.get('energy_kwh') or 0):.3f}".replace('.',','),
            row.get("tariff_name") or "",("" if row.get("price_cents_per_kwh") is None else str(row.get("price_cents_per_kwh")).replace('.',',')),
            ("" if row.get("cost_cents") is None else f"{int(row['cost_cents'])/100:.2f}".replace('.',',')),row.get("cost_center") or "",row.get("billing_group_name") or "",row.get("import_source") or "OCPP",
        ])
    payload=output.getvalue().encode("utf-8-sig")
    return StreamingResponse(iter([payload]),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=ocpp-abrechnungsnachweis.csv"})


def _pdf_money(cents):
    if cents is None:
        return "—"
    return f"{int(cents)/100:.2f}".replace(".", ",")+" EUR"


def _pdf_safe(value):
    return html.escape(str(value if value not in (None, "") else "—"))


def _pdf_report_filter_labels(data):
    filters=data.get("filters") or {}
    options=data.get("options") or {}
    labels=[]
    user_id=filters.get("user_id")
    if user_id is not None:
        item=next((x for x in options.get("users",[]) if int(x.get("id") or 0)==int(user_id)),None)
        labels.append(("Benutzer",(item or {}).get("name") or f"ID {user_id}"))
    vehicle_id=filters.get("vehicle_id")
    if vehicle_id is not None:
        item=next((x for x in options.get("vehicles",[]) if int(x.get("id") or 0)==int(vehicle_id)),None)
        if item:
            vehicle=(item.get("name") or f"ID {vehicle_id}")+((" · "+str(item.get("plate"))) if item.get("plate") else "")
        else:
            vehicle=f"ID {vehicle_id}"
        labels.append(("Fahrzeug",vehicle))
    if filters.get("charge_point_id"):
        labels.append(("Ladepunkt",filters["charge_point_id"]))
    if filters.get("cost_center"):
        labels.append(("Kostenstelle","Ohne Kostenstelle" if filters["cost_center"]=="__none__" else filters["cost_center"]))
    billing_id=filters.get("billing_group_id")
    if billing_id is not None:
        if str(billing_id)=="__none__":
            labels.append(("Abrechnungsgruppe","Ohne Abrechnungsgruppe"))
        else:
            item=next((x for x in options.get("billing_groups",[]) if str(x.get("id"))==str(billing_id)),None)
            labels.append(("Abrechnungsgruppe",(item or {}).get("name") or f"ID {billing_id}"))
    return labels


def _pdf_report_page(brand, title, period_label):
    primary=colors.HexColor(brand["primary_color"])
    muted=colors.HexColor("#6b7785")
    def draw(canvas, doc):
        canvas.saveState()
        width,height=A4
        canvas.setStrokeColor(colors.HexColor("#dfe4e8"))
        canvas.setLineWidth(0.45)
        canvas.line(doc.leftMargin,11.5*mm,width-doc.rightMargin,11.5*mm)
        canvas.setFont("Helvetica",7.2)
        canvas.setFillColor(muted)
        canvas.drawString(doc.leftMargin,7.2*mm,f"{brand['organization_name']} · {brand['product_name']}")
        canvas.drawCentredString(width/2,7.2*mm,period_label)
        canvas.setFillColor(primary)
        canvas.drawRightString(width-doc.rightMargin,7.2*mm,f"Seite {doc.page}")
        canvas.restoreState()
    return draw


def _pdf_breakdown_table(title, rows, styles, story, primary, limit=None):
    if not rows:
        return
    selected=rows if limit is None else rows[:limit]
    story.extend([Spacer(1,5*mm),Paragraph(title,styles["ReportSection"])])
    data=[["Zuordnung","Sessions","Energie","Kosten","Ø Preis"]]
    for row in selected:
        avg="—" if row.get("avg_price_cents_per_kwh") is None else f"{float(row['avg_price_cents_per_kwh']):.2f}".replace(".",",")+" ct/kWh"
        data.append([
            Paragraph(_pdf_safe(row.get("label") or "—"),styles["ReportCell"]),
            str(row.get("sessions") or 0),
            f"{float(row.get('energy_kwh') or 0):.2f}".replace(".",",")+" kWh",
            _pdf_money(row.get("cost_cents")),
            avg,
        ])
    table=Table(data,colWidths=[69*mm,20*mm,29*mm,28*mm,28*mm],repeatRows=1,hAlign="LEFT")
    commands=[
        ("BACKGROUND",(0,0),(-1,0),primary),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,0),7.6),
        ("FONTSIZE",(1,1),(-1,-1),7.5),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("ALIGN",(1,1),(-1,-1),"RIGHT"),("LINEBELOW",(0,0),(-1,-1),0.35,colors.HexColor("#dfe4e8")),
        ("TOPPADDING",(0,0),(-1,-1),5.5),("BOTTOMPADDING",(0,0),(-1,-1),5.5),
        ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
    ]
    for idx in range(2,len(data),2):
        commands.append(("BACKGROUND",(0,idx),(-1,idx),colors.HexColor("#f6f8fa")))
    table.setStyle(TableStyle(commands))
    story.append(table)


def _pdf_session_table(rows, styles, story, primary):
    if not rows:
        return
    story.extend([
        PageBreak(),
        Paragraph("Einzelladungsnachweis",styles["ReportSection"]),
        Paragraph(f"{len(rows)} abgeschlossene Ladevorgänge · chronologisch absteigend",styles["ReportSubtle"]),
        Spacer(1,3*mm),
    ])
    data=[["Datum & Zeit","Benutzer / Fahrzeug","Ladepunkt","Ladezeit","Energie","Kosten"]]
    for row in rows:
        seconds=float(row.get("charging_seconds") or row.get("connection_seconds") or 0)
        minutes=int(round(seconds/60.0)) if seconds>0 else 0
        duration=(f"{minutes//60} h {minutes%60} min" if minutes>=60 else (f"{minutes} min" if minutes else "—"))
        start=_local_text(row.get("started_at"),"%d.%m.%Y %H:%M")
        end=_local_text(row.get("ended_at"),"%H:%M")
        date_cell=Paragraph(f"<b>{_pdf_safe(start)}</b><br/><font color='#6b7785'>Ende {_pdf_safe(end)} Uhr</font>",styles["ReportCell"])
        user=_pdf_safe(row.get("user_name") or "Nicht zugeordnet")
        vehicle=(row.get("vehicle_name") or "Nicht zugeordnet")+((" · "+str(row.get("vehicle_plate"))) if row.get("vehicle_plate") else "")
        person_cell=Paragraph(f"<b>{user}</b><br/><font color='#6b7785'>{_pdf_safe(vehicle)}</font>",styles["ReportCell"])
        cp=str(row.get("charge_point_id") or "—")
        cp_sub=(f"Connector {row.get('connector_id')}" if row.get("connector_id") else "Connector —")
        cp_cell=Paragraph(f"<b>{_pdf_safe(cp)}</b><br/><font color='#6b7785'>{_pdf_safe(cp_sub)}</font>",styles["ReportCell"])
        data.append([
            date_cell,person_cell,cp_cell,duration,
            f"{float(row.get('energy_kwh') or 0):.2f}".replace(".",",")+" kWh",
            _pdf_money(row.get("cost_cents")),
        ])
    table=Table(data,colWidths=[31*mm,49*mm,36*mm,20*mm,20*mm,22*mm],repeatRows=1,hAlign="LEFT")
    commands=[
        ("BACKGROUND",(0,0),(-1,0),primary),("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,0),7.4),
        ("FONTSIZE",(3,1),(-1,-1),7.3),("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("ALIGN",(3,1),(-1,-1),"RIGHT"),("LINEBELOW",(0,0),(-1,-1),0.35,colors.HexColor("#dfe4e8")),
        ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
        ("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),
    ]
    for idx in range(2,len(data),2):
        commands.append(("BACKGROUND",(0,idx),(-1,idx),colors.HexColor("#f6f8fa")))
    table.setStyle(TableStyle(commands))
    story.append(table)


@app.get("/api/reports/export.pdf")
async def export_reports_pdf(period: str="current_month", date_from: str|None=None, date_to: str|None=None, user_id: int|None=None, vehicle_id: int|None=None, charge_point_id: str|None=None, cost_center: str|None=None, billing_group_id: str|None=None):
    data=_report_data(period,date_from,date_to,user_id,vehicle_id,charge_point_id,cost_center,billing_group_id)
    summary=data["summary"]; brand=db.branding_settings(); primary=colors.HexColor(brand["primary_color"])
    buffer=io.BytesIO(); title="Abrechnungs- & Energiebericht"
    doc=SimpleDocTemplate(
        buffer,pagesize=A4,rightMargin=16*mm,leftMargin=16*mm,topMargin=16*mm,bottomMargin=18*mm,
        title=title,author=brand["product_name"],subject=data["period"]["label"],
    )
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportBrand",parent=styles["Normal"],fontSize=8.5,leading=11,textColor=colors.HexColor("#6b7785")))
    styles.add(ParagraphStyle(name="ReportTitle",parent=styles["Title"],fontSize=22,leading=25,textColor=colors.HexColor("#17222d"),spaceAfter=2,alignment=0))
    styles.add(ParagraphStyle(name="ReportSubtle",parent=styles["Normal"],fontSize=8.5,textColor=colors.HexColor("#66717d"),leading=12))
    styles.add(ParagraphStyle(name="ReportSection",parent=styles["Heading2"],fontSize=12.5,leading=15,textColor=colors.HexColor("#17222d"),spaceAfter=5,spaceBefore=2))
    styles.add(ParagraphStyle(name="ReportCell",parent=styles["Normal"],fontSize=7.4,leading=9.5,textColor=colors.HexColor("#1f2d39")))
    styles.add(ParagraphStyle(name="MetricCell",parent=styles["Normal"],fontSize=8,leading=11,textColor=colors.HexColor("#66717d")))

    header=Table([
        [
            Paragraph(f"<b>{_pdf_safe(brand['display_name'])}</b><br/><font color='#6b7785'>{_pdf_safe(brand['organization_name'])}</font>",styles["ReportBrand"]),
            Paragraph("<b>REPORTING</b><br/><font color='#6b7785'>OCPP · Energie · Abrechnung</font>",ParagraphStyle("ReportHeaderRight",parent=styles["ReportBrand"],alignment=TA_RIGHT,textColor=primary)),
        ]
    ],colWidths=[100*mm,70*mm])
    header.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("BOTTOMPADDING",(0,0),(-1,-1),7),
        ("LINEBELOW",(0,0),(-1,-1),1.6,primary),
    ]))
    story=[header,Spacer(1,6*mm),Paragraph(title,styles["ReportTitle"]),
           Paragraph(f"{_pdf_safe(data['period']['label'])} · erstellt am {datetime.now(BERLIN_TZ).strftime('%d.%m.%Y um %H:%M Uhr')}",styles["ReportSubtle"])]

    filter_labels=_pdf_report_filter_labels(data)
    if filter_labels:
        story.extend([Spacer(1,4*mm),Paragraph("Berichtskontext",styles["ReportSection"])])
        filter_data=[]
        for i in range(0,len(filter_labels),2):
            row=[]
            for label,value in filter_labels[i:i+2]:
                row.append(Paragraph(f"<font color='#6b7785'>{_pdf_safe(label)}</font><br/><b>{_pdf_safe(value)}</b>",styles["ReportCell"]))
            while len(row)<2:
                row.append("")
            filter_data.append(row)
        ft=Table(filter_data,colWidths=[85*mm,85*mm])
        ft.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#f6f8fa")),
            ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#dfe4e8")),
            ("INNERGRID",(0,0),(-1,-1),0.35,colors.HexColor("#e7ebee")),
            ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
            ("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),
        ]))
        story.append(ft)

    story.extend([Spacer(1,5*mm),Paragraph("Zusammenfassung",styles["ReportSection"])])
    metrics=[
        ("Sessions",str(summary["sessions"])),
        ("Energie",f"{summary['energy_kwh']:.2f}".replace(".",",")+" kWh"),
        ("Kosten",_pdf_money(summary["cost_cents"])),
        ("Ladezeit",f"{summary['charging_hours']:.2f}".replace(".",",")+" h"),
        ("Ø Session",f"{summary['avg_energy_kwh']:.2f}".replace(".",",")+" kWh"),
        ("Ø Preis","—" if summary["avg_price_cents_per_kwh"] is None else f"{summary['avg_price_cents_per_kwh']:.2f}".replace(".",",")+" ct/kWh"),
        ("Kostenabdeckung",f"{summary['cost_coverage_pct']:.1f}".replace(".",",")+" %"),
        ("Standzeit",f"{summary['stand_hours']:.2f}".replace(".",",")+" h"),
    ]
    metric_rows=[]
    for i in range(0,len(metrics),4):
        metric_rows.append([
            Paragraph(f"<font color='#6b7785'>{_pdf_safe(label)}</font><br/><font size='14'><b>{_pdf_safe(value)}</b></font>",styles["MetricCell"])
            for label,value in metrics[i:i+4]
        ])
    mt=Table(metric_rows,colWidths=[42.5*mm]*4)
    mt.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#f8fafb")),
        ("BOX",(0,0),(-1,-1),0.55,colors.HexColor("#dfe4e8")),
        ("INNERGRID",(0,0),(-1,-1),0.35,colors.HexColor("#e7ebee")),
        ("TOPPADDING",(0,0),(-1,-1),9),("BOTTOMPADDING",(0,0),(-1,-1),9),
        ("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),
    ]))
    story.append(mt)

    if summary["cost_missing_sessions"]:
        story.extend([
            Spacer(1,3*mm),
            Paragraph(
                f"<b>Hinweis:</b> Für {summary['cost_missing_sessions']} Session(s) liegt kein eingefrorener Tarif/Kostenwert vor. "
                "Diese Sessions sind in Energie und Anzahl enthalten, aber nicht in der Kostensumme.",
                styles["ReportSubtle"],
            ),
        ])

    _pdf_breakdown_table("Kostenstellen",data["by_cost_center"],styles,story,primary)
    _pdf_breakdown_table("Benutzer",data["by_user"],styles,story,primary)
    _pdf_breakdown_table("Fahrzeuge",data["by_vehicle"],styles,story,primary)
    _pdf_breakdown_table("Ladepunkte",data["by_charge_point"],styles,story,primary)
    _pdf_session_table(data["transactions"],styles,story,primary)

    page_cb=_pdf_report_page(brand,title,data["period"]["label"])
    doc.build(story,onFirstPage=page_cb,onLaterPages=page_cb)
    buffer.seek(0)
    filename=f"ocpp-abrechnungsbericht-{data['period']['key']}.pdf"
    return StreamingResponse(buffer,media_type="application/pdf",headers={"Content-Disposition":f"attachment; filename={filename}"})


@app.get("/api/reports")
async def api_reports(period: str="current_month", date_from: str|None=None, date_to: str|None=None, user_id: int|None=None, vehicle_id: int|None=None, charge_point_id: str|None=None, cost_center: str|None=None, billing_group_id: str|None=None):
    return _report_data(period,date_from,date_to,user_id,vehicle_id,charge_point_id,cost_center,billing_group_id)



if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=WEB_PORT, reload=False, server_header=False)
