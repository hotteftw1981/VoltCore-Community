import base64
import os
from http import HTTPStatus
from . import db


MAX_CHARGE_POINT_ID_LENGTH = 128
MAX_BASIC_AUTH_HEADER_LENGTH = 4096


def normalize_charge_point_id(value):
    cp_id=str(value or "")
    if not cp_id or len(cp_id)>MAX_CHARGE_POINT_ID_LENGTH or cp_id!=cp_id.strip():
        return None
    if any(ord(ch)<32 or ord(ch)==127 for ch in cp_id) or any(ch in cp_id for ch in ("/","\\","?","#")):
        return None
    return cp_id


def transport_is_secure(protocol=None, headers=None):
    try:
        if protocol is not None and getattr(protocol, "transport", None) is not None:
            if protocol.transport.get_extra_info("ssl_object") is not None:
                return True
    except Exception:
        pass
    headers=headers or {}
    trust_proxy=str(os.getenv("TRUST_PROXY_HEADERS","0")).strip().lower() in {"1","true","yes","on"}
    if not trust_proxy:
        return False
    proto=str(headers.get("X-Forwarded-Proto") or headers.get("X-Forwarded-Scheme") or "").split(",",1)[0].strip().lower()
    return proto in {"https","wss"}


def parse_basic_auth(value):
    value=str(value or "").strip()
    if len(value)>MAX_BASIC_AUTH_HEADER_LENGTH or not value.lower().startswith("basic "):
        return None,None
    try:
        raw=base64.b64decode(value.split(None,1)[1],validate=True).decode("utf-8")
        username,password=raw.split(":",1)
        return username,password
    except Exception:
        return None,None


def connection_security_decision(cp_id, request_headers=None, secure=False):
    """Side-effect free security decision for an incoming OCPP WebSocket handshake."""
    cp_id=normalize_charge_point_id(cp_id)
    headers=request_headers or {}
    if not cp_id:
        return {"allowed":False,"status":HTTPStatus.BAD_REQUEST,"reason":"Ungültige Ladepunkt-ID","challenge":False,"authenticated":False,"known":False}
    settings=db.security_settings()
    cp=db.get_charge_point(cp_id)
    known=bool(cp and int(cp.get("onboarded",1) or 0) and not int(cp.get("ignored",0) or 0) and not int(cp.get("retired",0) or 0) and not int(cp.get("archived",0) or 0))
    secret_configured=bool(cp and cp.get("ocpp_secret_hash"))
    offered_protocols=str(headers.get("Sec-WebSocket-Protocol") or headers.get("sec-websocket-protocol") or "")
    protocols={x.strip().lower() for x in offered_protocols.split(",") if x.strip()}
    if settings.get("require_subprotocol") and "ocpp1.6" not in protocols:
        return {"allowed":False,"status":HTTPStatus.BAD_REQUEST,"reason":"OCPP-Subprotocol ocpp1.6 erforderlich","challenge":False,"authenticated":False,"known":known}
    if settings.get("require_tls") and not secure:
        return {"allowed":False,"status":HTTPStatus.FORBIDDEN,"reason":"TLS/WSS erforderlich","challenge":False,"authenticated":False,"known":known}
    if (settings.get("reject_unknown") or settings.get("auth_mode")=="required") and not known:
        return {"allowed":False,"status":HTTPStatus.FORBIDDEN,"reason":"Unbekannter oder nicht freigegebener Ladepunkt","challenge":False,"authenticated":False,"known":known}
    auth_needed=settings.get("auth_mode")=="required" or (settings.get("auth_mode")=="configured" and secret_configured)
    if auth_needed:
        if not secret_configured:
            return {"allowed":False,"status":HTTPStatus.FORBIDDEN,"reason":"Kein OCPP-Secret für diesen Ladepunkt konfiguriert","challenge":False,"authenticated":False,"known":known}
        username,password=parse_basic_auth(headers.get("Authorization"))
        if username is None:
            return {"allowed":False,"status":HTTPStatus.UNAUTHORIZED,"reason":"OCPP Basic Auth fehlt","challenge":True,"authenticated":False,"known":known}
        if username != cp_id or not db.verify_charge_point_secret(cp_id,password):
            return {"allowed":False,"status":HTTPStatus.UNAUTHORIZED,"reason":"OCPP-Zugangsdaten ungültig","challenge":True,"authenticated":False,"known":known}
        return {"allowed":True,"status":None,"reason":"OCPP Basic Auth bestätigt","challenge":False,"authenticated":True,"known":known}
    return {"allowed":True,"status":None,"reason":"OCPP-Verbindung gemäß aktueller Sicherheitsrichtlinie zugelassen","challenge":False,"authenticated":False,"known":known}
