import base64
import json
import logging
import ipaddress
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from . import db

log = logging.getLogger("voltcore.push")

_PRIVATE_KEY_SETTING = "web_push_vapid_private_key"
_PUBLIC_KEY_SETTING = "web_push_vapid_public_key"
_SUBJECT_SETTING = "web_push_vapid_subject"
_DEFAULT_SUBJECT = "mailto:p.garbe@drk-schwelm.org"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def ensure_vapid_keys():
    private_key = db.get_setting(_PRIVATE_KEY_SETTING)
    public_key = db.get_setting(_PUBLIC_KEY_SETTING)
    if private_key and public_key:
        if not db.get_setting(_SUBJECT_SETTING):
            db.set_setting(_SUBJECT_SETTING, _DEFAULT_SUBJECT)
        return {"public_key": public_key, "created": False}

    key = ec.generate_private_key(ec.SECP256R1())
    private_der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_key = _b64url(private_der)
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_key = _b64url(public_bytes)
    db.set_setting(_PRIVATE_KEY_SETTING, private_key)
    db.set_setting(_PUBLIC_KEY_SETTING, public_key)
    db.set_setting(_SUBJECT_SETTING, db.get_setting(_SUBJECT_SETTING) or _DEFAULT_SUBJECT)
    return {"public_key": public_key, "created": True}


def public_config():
    keys = ensure_vapid_keys()
    return {"enabled": True, "public_key": keys["public_key"]}


def validate_endpoint(value):
    endpoint=str(value or "").strip()
    if len(endpoint)>2048:
        raise ValueError("Push-Endpunkt ist zu lang")
    parsed=urlparse(endpoint)
    if parsed.scheme.lower()!="https" or not parsed.hostname:
        raise ValueError("Push-Endpunkt muss eine HTTPS-URL sein")
    host=parsed.hostname.strip().lower().rstrip(".")
    if host=="localhost" or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        raise ValueError("Lokale Push-Endpunkte sind nicht erlaubt")
    try:
        ip=ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError("Private oder lokale Push-Endpunkte sind nicht erlaubt")
    except ValueError as exc:
        if "Push-Endpunkte" in str(exc):
            raise
        # Normal DNS hostname; browser push services use HTTPS hostnames.
        pass
    return endpoint


def _subscription_info(row):
    return {
        "endpoint": str(row.get("endpoint") or ""),
        "keys": {
            "p256dh": str(row.get("p256dh") or ""),
            "auth": str(row.get("auth") or ""),
        },
    }


def _payload(row, test=False):
    branding = db.branding_settings()
    title = "Push-Test erfolgreich" if test else str(row.get("title") or "VoltCore")
    body = (
        "Push-Benachrichtigungen sind für dieses Gerät aktiv."
        if test
        else str(row.get("message") or "")
    )
    url = "/" if test else str(row.get("link") or "/")
    icon = str(branding.get("favicon_url") or "/static/pwa-icon-192.png")
    return {
        "title": title,
        "body": body,
        "url": url,
        "icon": icon,
        "badge": "/static/pwa-icon-192.png",
        "tag": (
            "voltcore-push-test"
            if test
            else f"voltcore-notification-{int(row.get('notification_id') or 0)}-{int(row.get('revision') or 1)}"
        ),
        "severity": "info" if test else str(row.get("severity") or "info"),
        "product": str(branding.get("product_name") or "VoltCore"),
    }


def _send(row, payload):
    private_key = db.get_setting(_PRIVATE_KEY_SETTING)
    if not private_key:
        ensure_vapid_keys()
        private_key = db.get_setting(_PRIVATE_KEY_SETTING)
    subject = str(db.get_setting(_SUBJECT_SETTING, _DEFAULT_SUBJECT) or _DEFAULT_SUBJECT)
    return webpush(
        subscription_info=_subscription_info(row),
        data=json.dumps(payload, ensure_ascii=False),
        vapid_private_key=private_key,
        vapid_claims={"sub": subject},
        ttl=300,
    )


def _status_code(exc):
    response = getattr(exc, "response", None)
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def dispatch_pending(limit=80):
    ensure_vapid_keys()
    result = {"sent": 0, "gone": 0, "errors": 0}
    for row in db.pending_web_push_deliveries(limit):
        try:
            _send(row, _payload(row))
            db.mark_web_push_delivery(
                row["subscription_id"], row["notification_id"], row.get("revision") or 1
            )
            result["sent"] += 1
        except WebPushException as exc:
            status = _status_code(exc)
            if status in (404, 410):
                db.remove_web_push_subscription_by_id(row["subscription_id"])
                result["gone"] += 1
            else:
                db.mark_web_push_error(row["subscription_id"], f"{status or 'WebPush'}: {exc}")
                result["errors"] += 1
        except Exception as exc:
            db.mark_web_push_error(row["subscription_id"], f"{type(exc).__name__}: {exc}")
            result["errors"] += 1
            log.warning("Web-Push konnte nicht versendet werden", exc_info=True)
    return result


def send_test(subscription_row):
    ensure_vapid_keys()
    _send(subscription_row, _payload(subscription_row, test=True))
    return True
