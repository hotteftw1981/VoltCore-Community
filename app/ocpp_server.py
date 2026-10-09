import asyncio
import json
import logging
import os
import ssl
from http import HTTPStatus
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import Action, RegistrationStatus
from websockets.legacy.server import serve, WebSocketServerProtocol

from . import db
from . import ocpp_diagnostics
from .security import connection_security_decision, transport_is_secure, normalize_charge_point_id

log = logging.getLogger("voltcore.community.ocpp")

ACTIVE_CONNECTIONS = {}
LOCAL_LIST_LOCKS = {}

def _local_list_lock(cp_id):
    key=str(cp_id)
    lock=LOCAL_LIST_LOCKS.get(key)
    if lock is None:
        lock=asyncio.Lock(); LOCAL_LIST_LOCKS[key]=lock
    return lock

async def _local_list_version_from_station(cp):
    response=await asyncio.wait_for(cp.call(call.GetLocalListVersion()),timeout=15)
    try: return int(getattr(response,"list_version"))
    except (TypeError,ValueError,AttributeError): return None

def _config_true(value):
    return str(value or "").strip().casefold() in {"1","true","yes","on"}


async def _ensure_offline_authorization(cp, cp_id):
    changes={}
    keys=("LocalAuthListEnabled","LocalAuthorizeOffline")
    for key in keys:
        try:
            response=await asyncio.wait_for(cp.call(call.ChangeConfiguration(key=key,value="true")),timeout=12)
            status=_response_status(response); changes[key]=status
            db.add_event(str(cp_id),"ChangeConfiguration",f"key={key}; value=true; response={status}; reason=RFID LocalList",direction="OUT")
        except asyncio.TimeoutError:
            changes[key]="Timeout"
        except Exception as exc:
            changes[key]=type(exc).__name__

    try:
        readback=await read_configuration(str(cp_id),list(keys))
        cfg=readback.get("configuration") or {}
        unknown=set(readback.get("unknown_keys") or [])
        values={key:(cfg.get(key) or {}).get("value") for key in keys}
        readonly=any(bool((cfg.get(key) or {}).get("readonly")) for key in keys if key in cfg)
        known=[key for key in keys if key in cfg]
        if known and all(_config_true(values.get(key)) for key in keys if key in cfg) and len(known)==len(keys):
            status="Aktiv"
        elif any(key in cfg and not _config_true(values.get(key)) for key in keys):
            status="Inaktiv"
        elif all(key in unknown for key in keys):
            status="Nicht verfügbar"
        else:
            status="Teilweise"
        detail=", ".join(
            f"{key}={values.get(key)}"+(" (read-only)" if bool((cfg.get(key) or {}).get("readonly")) else "")
            if key in cfg else f"{key}=unbekannt"
            for key in keys
        )
        db.set_local_list_offline_auth_state(cp_id,status,detail,readonly)
        return {"changes":changes,"status":status,"detail":detail,"readonly":readonly,"configuration":cfg,"unknown_keys":list(unknown)}
    except Exception as exc:
        detail=f"GetConfiguration: {type(exc).__name__}"
        db.set_local_list_offline_auth_state(cp_id,"Unbekannt",detail,None)
        return {"changes":changes,"status":"Unbekannt","detail":detail,"readonly":None,"configuration":{},"unknown_keys":[]}

async def verify_offline_authorization(cp_id):
    cp_id=str(cp_id)
    info=ACTIVE_CONNECTIONS.get(cp_id) or {}
    cp=info.get("charge_point")
    if cp is None:
        return {"ok":False,"offline":True,"status":"Offline","detail":"Ladepunkt ist aktuell nicht verbunden"}
    result=await _ensure_offline_authorization(cp,cp_id)
    return {"ok":result.get("status")!="Unbekannt",**result}


async def sync_local_list(cp_id, force_full=False, reason="Automatisch"):
    cp_id=str(cp_id)
    async with _local_list_lock(cp_id):
        info=ACTIVE_CONNECTIONS.get(cp_id) or {}; cp=info.get("charge_point")
        if cp is None:
            db.ensure_local_list_state(cp_id)
            db.set_local_list_state(cp_id,pending=True,status="Ausstehend",response="Ladepunkt offline")
            return {"ok":False,"offline":True,"status":"Ausstehend"}
        backend_version=db.rfid_local_list_version(); db.ensure_local_list_state(cp_id)
        try:
            station_version=await _local_list_version_from_station(cp)
        except asyncio.TimeoutError:
            db.set_local_list_state(cp_id,pending=True,status="Timeout",response="GetLocalListVersion: Timeout")
            db.add_event(cp_id,"GetLocalListVersion",f"reason={reason}; timeout=15s",direction="OUT")
            return {"ok":False,"status":"Timeout"}
        except Exception as exc:
            name=type(exc).__name__
            detail=str(exc or "").strip()
            response_text=f"GetLocalListVersion: {name}"+(f" · {detail}" if detail else "")
            db.set_local_list_state(cp_id,pending=True,status="Fehler",response=response_text)
            db.add_event(cp_id,"GetLocalListVersion",f"reason={reason}; error={name}"+(f"; detail={detail[:180]}" if detail else ""),direction="OUT")
            return {"ok":False,"status":"Fehler","error":name,"detail":detail or None}
        db.add_event(cp_id,"GetLocalListVersion",f"reason={reason}; station_version={station_version}; backend_version={backend_version}",direction="OUT")
        if station_version == -1:
            learned_state=db.ensure_local_list_state(cp_id) or {}
            accepted_before=bool(learned_state.get("supported")==1) or db.local_list_send_was_accepted(cp_id)
            if not accepted_before and not force_full:
                diagnostic="GetLocalListVersion=-1 · OCPP 1.6: Säule meldet Local Authorization List ausdrücklich als nicht unterstützt"
                db.set_local_list_state(cp_id,station_version=-1,pending=False,supported=False,status="Nicht unterstützt",response=diagnostic)
                return {"ok":False,"supported":False,"status":"Nicht unterstützt","station_version":-1,"diagnostic":diagnostic}
            if accepted_before:
                diagnostic="GetLocalListVersion=-1 · bereits gelernte SendLocalList-Unterstützung vorhanden; automatischer Vollabgleich wird fortgesetzt"
            else:
                diagnostic="GetLocalListVersion=-1 · manueller Admin-Test erzwingt einmalig einen SendLocalList-Vollabgleich"
            db.set_local_list_state(cp_id,station_version=-1,pending=True,supported=True,status="Widersprüchlich",response=diagnostic)
        if station_version == backend_version and not force_full:
            offline_cfg=await _ensure_offline_authorization(cp,cp_id)
            cfg_text="OfflineAuth="+str(offline_cfg.get("status") or "Unbekannt")+(" (read-only)" if offline_cfg.get("readonly") else "")
            db.set_local_list_state(cp_id,station_version=station_version,pending=False,supported=True,status="Synchronisiert",response="Versionsstand identisch; "+cfg_text,synced=True)
            return {"ok":True,"status":"Synchronisiert","version":backend_version,"update_type":"None","offline_configuration":offline_cfg}
        update_type="Full"
        entries=db.rfid_local_list_full(cp_id)
        if not force_full and station_version is not None and station_version == backend_version-1:
            changes=db.rfid_local_list_changes_for_version(backend_version,cp_id)
            if changes:
                update_type="Differential"; entries=changes
        async def _send(kind,items):
            request=call.SendLocalList(list_version=backend_version,update_type=kind,local_authorization_list=items)
            response=await asyncio.wait_for(cp.call(request),timeout=20)
            status=_response_status(response)
            db.add_event(cp_id,"SendLocalList",f"reason={reason}; update_type={kind}; list_version={backend_version}; entries={len(items)}; response={status}",direction="OUT")
            return status
        try:
            status=await _send(update_type,entries)
            if status.casefold() in {"versionmismatch","failed"} and update_type=="Differential":
                update_type="Full"; entries=db.rfid_local_list_full(cp_id); status=await _send(update_type,entries)
        except asyncio.TimeoutError:
            db.set_local_list_state(cp_id,station_version=station_version,pending=True,supported=True,status="Timeout",update_type=update_type,response="SendLocalList: Timeout")
            return {"ok":False,"status":"Timeout","update_type":update_type}
        except Exception as exc:
            name=type(exc).__name__
            detail=str(exc or "").strip()
            response_text=f"SendLocalList: {name}"+(f" · {detail}" if detail else "")
            db.set_local_list_state(cp_id,station_version=station_version,pending=True,status="Fehler",update_type=update_type,response=response_text)
            return {"ok":False,"status":"Fehler","error":name,"detail":detail or None,"update_type":update_type}
        low=status.casefold()
        if low=="accepted":
            offline_cfg=await _ensure_offline_authorization(cp,cp_id)
            cfg_text="OfflineAuth="+str(offline_cfg.get("status") or "Unbekannt")+(" (read-only)" if offline_cfg.get("readonly") else "")
            # Accepted confirms request acceptance, not the station's installed
            # version or number of stored entries. Always read back the version.
            verified_version=None
            try:
                verified_version=await _local_list_version_from_station(cp)
            except (asyncio.TimeoutError, Exception) as exc:
                db.add_event(cp_id,"GetLocalListVersion",f"readback_after_send={type(exc).__name__}",direction="OUT")
            confirmed=verified_version==backend_version
            response_text=status+("; "+cfg_text if cfg_text else "")
            if not confirmed:
                response_text+="; Versionsabfrage nach SendLocalList nicht bestätigt"
            db.set_local_list_state(cp_id,station_version=verified_version if verified_version is not None else station_version,
                pending=not confirmed,supported=True,status="Synchronisiert" if confirmed else "Prüfung erforderlich",
                update_type=update_type,response=response_text,synced=confirmed)
            return {"ok":confirmed,"accepted":True,"status":"Synchronisiert" if confirmed else "Prüfung erforderlich",
                "version":backend_version,"station_version":verified_version,"update_type":update_type,
                "entries_sent":len(entries),"offline_configuration":offline_cfg}
        if low=="notsupported":
            db.set_local_list_state(cp_id,station_version=station_version,pending=False,supported=False,status="Nicht unterstützt",update_type=update_type,response=status)
            return {"ok":False,"supported":False,"status":"Nicht unterstützt","update_type":update_type}
        db.set_local_list_state(cp_id,station_version=station_version,pending=True,supported=True,status=status or "Fehlgeschlagen",update_type=update_type,response=status)
        return {"ok":False,"status":status,"update_type":update_type}

async def sync_pending_local_lists(reason="RFID geändert", force_full=False):
    results=[]
    for cp_id in list(ACTIVE_CONNECTIONS.keys()):
        state=db.ensure_local_list_state(cp_id) or {}
        if force_full or int(state.get("pending",1) or 0) or state.get("station_version")!=db.rfid_local_list_version():
            results.append(await sync_local_list(cp_id,force_full=force_full,reason=reason))
    return results



class SecureOcppProtocol(WebSocketServerProtocol):
    async def process_request(self, path, request_headers):
        cp_id=normalize_charge_point_id(unquote(urlparse(path).path.strip("/")))
        if not cp_id:
            reason="Ungültige Ladepunkt-ID"
            return HTTPStatus.BAD_REQUEST,[("Content-Type","text/plain; charset=utf-8")],(reason+"\n").encode("utf-8")
        secure=transport_is_secure(self,request_headers)
        remote=_remote_text(getattr(self,"remote_address",None))
        remote_host=_remote_host(getattr(self,"remote_address",None))
        transport="WSS" if secure else "WS"
        client_key=db.ocpp_auth_client_key(cp_id,remote_host)
        failures=db.ocpp_auth_failures(client_key)
        if failures >= db.OCPP_AUTH_MAX_FAILURES:
            reason=f"Zu viele fehlgeschlagene OCPP-Anmeldeversuche; Sperrfenster {db.OCPP_AUTH_FAILURE_WINDOW_MINUTES} min"
            db.record_ocpp_security_result(cp_id,False,reason,transport,remote)
            return HTTPStatus.TOO_MANY_REQUESTS,[("Content-Type","text/plain; charset=utf-8"),("Retry-After",str(db.OCPP_AUTH_FAILURE_WINDOW_MINUTES*60))],(reason+"\n").encode("utf-8")
        decision=connection_security_decision(cp_id,request_headers,secure)
        db.record_ocpp_security_result(cp_id,decision["allowed"],decision["reason"],transport,remote)
        if decision["allowed"]:
            if decision.get("authenticated"):
                db.record_ocpp_auth_attempt(client_key,cp_id,True)
            return None
        if decision.get("challenge"):
            db.record_ocpp_auth_attempt(client_key,cp_id,False)
        headers=[("Content-Type","text/plain; charset=utf-8")]
        if decision.get("challenge"):
            headers.append(("WWW-Authenticate",'Basic realm="OCPP", charset="UTF-8"'))
        body=(decision["reason"]+"\n").encode("utf-8")
        return decision["status"],headers,body


def _ocpp_ssl_context():
    cert=os.getenv("OCPP_TLS_CERTFILE")
    key=os.getenv("OCPP_TLS_KEYFILE")
    if not cert and not key:
        return None
    if not cert or not key:
        raise RuntimeError("Für direktes WSS müssen OCPP_TLS_CERTFILE und OCPP_TLS_KEYFILE gemeinsam gesetzt sein")
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert,keyfile=key)
    return context


def is_connected(cp_id):
    info=ACTIVE_CONNECTIONS.get(str(cp_id)) or {}
    return info.get("charge_point") is not None


def _response_status(response):
    status=getattr(response,"status",None)
    return getattr(status,"value",None) or str(status or "Unknown")


def _status_token(value):
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _exception_is_not_supported(exc):
    values=[
        type(exc).__name__,
        str(exc),
        getattr(exc,"code",""),
        getattr(exc,"error_code",""),
        getattr(exc,"description",""),
    ]
    return any(_status_token(v) in {"notsupported","notimplemented"} or "notsupported" in _status_token(v) for v in values if v)


CAPABILITY_CONFIG_KEYS = (
    "SupportedFeatureProfiles",
    "SupportedFeatureProfilesMaxLength",
    "LocalAuthListEnabled",
    "LocalAuthListMaxLength",
    "LocalAuthorizeOffline",
    "AuthorizationCacheEnabled",
    "HeartbeatInterval",
    "MeterValueSampleInterval",
    "ClockAlignedDataInterval",
    "MeterValuesSampledData",
    "MeterValuesAlignedData",
)


def _configuration_item(value):
    if isinstance(value,dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    return {k:getattr(value,k) for k in ("key","readonly","value") if hasattr(value,k)}


def _capability_state(supported, detail, evidence=None):
    return {
        "state":"supported" if supported is True else "not_advertised" if supported is False else "unknown",
        "label":"Unterstützt" if supported is True else "Nicht angekündigt" if supported is False else "Unbekannt",
        "detail":detail,
        "evidence":evidence or "",
    }


async def probe_capabilities(cp_id):
    """Read OCPP configuration and build a manufacturer-neutral capability profile."""
    cp_id=str(cp_id)
    info=ACTIVE_CONNECTIONS.get(cp_id) or {}
    cp=info.get("charge_point")
    if cp is None:
        raise RuntimeError("Ladepunkt ist offline")
    started=datetime.now(timezone.utc)
    configuration={}
    unknown=[]
    for offset in range(0,len(CAPABILITY_CONFIG_KEYS),6):
        keys=list(CAPABILITY_CONFIG_KEYS[offset:offset+6])
        try:
            response=await asyncio.wait_for(cp.call(call.GetConfiguration(key=keys)),timeout=15)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Zeitüberschreitung bei GetConfiguration") from exc
        values=getattr(response,"configuration_key",None) or []
        for item in values:
            data=_configuration_item(item)
            key=str(data.get("key") or "").strip()
            if key:
                configuration[key]={
                    "value":data.get("value"),
                    "readonly":bool(data.get("readonly",False)),
                }
        for key in (getattr(response,"unknown_key",None) or []):
            text=str(key or "").strip()
            if text and text not in unknown:
                unknown.append(text)

    profile_text=str((configuration.get("SupportedFeatureProfiles") or {}).get("value") or "")
    profiles=[x.strip() for x in profile_text.replace("; ",",").split(",") if x.strip()]
    profile_set={x.casefold() for x in profiles}
    recognized=set(configuration)

    local_list_version=None
    local_list_probe="unbekannt"
    try:
        response=await asyncio.wait_for(cp.call(call.GetLocalListVersion()),timeout=12)
        local_list_version=int(getattr(response,"list_version"))
        local_list_probe="unterstützt" if local_list_version!=-1 else "nicht unterstützt"
    except Exception:
        local_list_probe="keine Antwort"

    def advertised(name):
        return name.casefold() in profile_set

    local_keys={"LocalAuthListEnabled","LocalAuthListMaxLength","LocalAuthorizeOffline"}
    local_auth=False if local_list_version==-1 else (True if advertised("LocalAuthListManagement") or local_list_version is not None or bool(recognized & local_keys) else (False if profile_text else None))

    capabilities={
        "core_remote":{
            "state":"available","label":"Verfügbar",
            "detail":"Remote Start/Stop, Reset, Unlock und Availability werden über OCPP Core gesendet; die endgültige Unterstützung bestätigt erst die Geräteantwort.",
            "evidence":"Aktive OCPP-1.6J-Verbindung",
        },
        "local_auth_list":_capability_state(local_auth,"Lokale RFID-Liste für Offline-Autorisierung",f"GetLocalListVersion: {local_list_probe}"),
    }
    elapsed_ms=max(0,int((datetime.now(timezone.utc)-started).total_seconds()*1000))
    snapshot=db.save_ocpp_capability_snapshot(
        cp_id,status="ok",response_ms=elapsed_ms,profiles=profiles,capabilities=capabilities,
        configuration=configuration,unknown_keys=unknown,
    )
    db.add_event(cp_id,"CapabilityProbe",f"profiles={','.join(profiles) or 'none'}; keys={len(configuration)}; unknown={len(unknown)}; response_ms={elapsed_ms}",direction="OUT")
    return snapshot


async def read_configuration(cp_id, keys=None):
    """Read arbitrary OCPP 1.6 configuration keys from an online station."""
    cp_id=str(cp_id)
    info=ACTIVE_CONNECTIONS.get(cp_id) or {}
    cp=info.get("charge_point")
    if cp is None:
        raise RuntimeError("Ladepunkt ist offline")
    clean=None
    if keys is not None:
        clean=[str(x or "").strip() for x in keys if str(x or "").strip()]
        if len(clean)>50:
            raise ValueError("Maximal 50 Konfigurationsschlüssel pro Abfrage")
    started=datetime.now(timezone.utc)
    try:
        response=await asyncio.wait_for(cp.call(call.GetConfiguration(key=clean or None)),timeout=20)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Zeitüberschreitung bei GetConfiguration") from exc
    configuration={}
    for item in (getattr(response,"configuration_key",None) or []):
        data=_configuration_item(item)
        key=str(data.get("key") or "").strip()
        if key:
            configuration[key]={"value":data.get("value"),"readonly":bool(data.get("readonly",False))}
    unknown=[str(x or "").strip() for x in (getattr(response,"unknown_key",None) or []) if str(x or "").strip()]
    elapsed_ms=max(0,int((datetime.now(timezone.utc)-started).total_seconds()*1000))
    db.add_event(cp_id,"GetConfiguration",f"keys={','.join(clean or []) or '*'}; returned={len(configuration)}; unknown={len(unknown)}; response_ms={elapsed_ms}",direction="OUT")
    return {"configuration":configuration,"unknown_keys":unknown,"elapsed_ms":elapsed_ms}


async def remote_command(cp_id, command, **kwargs):
    """Send a manufacturer-neutral OCPP 1.6J remote command to an online station."""
    info=ACTIVE_CONNECTIONS.get(str(cp_id)) or {}
    cp=info.get("charge_point")
    if cp is None:
        raise RuntimeError("Ladepunkt ist offline")
    command=str(command or "").strip().lower()
    learned=(db.remote_capability_profile(str(cp_id)).get(command) or {})
    if learned.get("state")=="unsupported":
        raise RuntimeError("Dieser OCPP-Befehl wurde von diesem Ladepunkt bereits ausdrücklich als NotSupported gemeldet. Lernstatus zurücksetzen, um nach Firmware-/Geräteänderungen erneut zu testen.")
    if command == "start":
        id_tag=str(kwargs.get("id_tag") or "").strip()
        if not id_tag: raise ValueError("RFID/idTag fehlt")
        decision=db.authorization_decision(id_tag,cp_id)
        if not decision.get("accepted"):
            raise ValueError(decision.get("reason") or "RFID ist an diesem Ladepunkt nicht freigegeben")
        connector=kwargs.get("connector_id")
        request=call.RemoteStartTransaction(id_tag=id_tag,connector_id=int(connector) if connector not in (None,"") else None)
        event_type="RemoteStartTransaction"
    elif command == "stop":
        transaction_id=kwargs.get("transaction_id")
        if transaction_id in (None,""): raise ValueError("Transaction-ID fehlt")
        request=call.RemoteStopTransaction(transaction_id=int(transaction_id))
        event_type="RemoteStopTransaction"
    elif command == "unlock":
        connector=kwargs.get("connector_id")
        if connector in (None,""): raise ValueError("Connector fehlt")
        request=call.UnlockConnector(connector_id=int(connector))
        event_type="UnlockConnector"
    elif command == "availability":
        connector=kwargs.get("connector_id",0)
        availability=str(kwargs.get("availability_type") or "").strip()
        if availability not in {"Operative","Inoperative"}: raise ValueError("Ungültiger Availability-Status")
        request=call.ChangeAvailability(connector_id=int(connector or 0),type=availability)
        event_type="ChangeAvailability"
    elif command == "reset":
        reset_type=str(kwargs.get("reset_type") or "Soft").strip()
        if reset_type not in {"Soft","Hard"}: raise ValueError("Ungültiger Reset-Typ")
        request=call.Reset(type=reset_type)
        event_type="Reset"
    else:
        raise ValueError("Unbekannter Remote-Befehl")
    started_at=datetime.now(timezone.utc)
    detail="; ".join(f"{k}={v}" for k,v in kwargs.items() if v not in (None,""))
    try:
        response=await asyncio.wait_for(cp.call(request), timeout=20)
    except asyncio.TimeoutError as exc:
        elapsed_ms=max(0,int((datetime.now(timezone.utc)-started_at).total_seconds()*1000))
        db.add_event(str(cp_id),event_type,"timeout=20s; command="+command,direction="OUT")
        db.record_remote_capability_result(str(cp_id),command,outcome="error",status="Timeout",detail="20s",response_ms=elapsed_ms)
        raise RuntimeError("Zeitüberschreitung: Ladepunkt hat innerhalb von 20 Sekunden nicht geantwortet") from exc
    except Exception as exc:
        elapsed_ms=max(0,int((datetime.now(timezone.utc)-started_at).total_seconds()*1000))
        if _exception_is_not_supported(exc):
            status="NotSupported"
            db.add_event(str(cp_id),event_type,(detail+"; " if detail else "")+"response=NotSupported; source=CallError",direction="OUT")
            learned=db.record_remote_capability_result(str(cp_id),command,outcome="unsupported",status=status,detail=type(exc).__name__,response_ms=elapsed_ms)
            return {"command":command,"status":status,"accepted":False,"elapsed_ms":elapsed_ms,"learned_capability":learned}
        db.record_remote_capability_result(str(cp_id),command,outcome="error",status=type(exc).__name__,detail=str(exc),response_ms=elapsed_ms)
        raise
    status=_response_status(response)
    token=_status_token(status)
    accepted=token in {"accepted","unlocked","scheduled","rebootrequired"}
    db.add_event(str(cp_id),event_type,(detail+"; " if detail else "")+f"response={status}",direction="OUT")
    elapsed_ms=max(0,int((datetime.now(timezone.utc)-started_at).total_seconds()*1000))
    token=_status_token(status)
    if token in {"notsupported","notimplemented"}:
        outcome="unsupported"
    elif accepted:
        outcome="supported"
    else:
        outcome="rejected"
    learned=db.record_remote_capability_result(str(cp_id),command,outcome=outcome,status=status,detail=detail,response_ms=elapsed_ms)
    return {"command":command,"status":status,"accepted":accepted,"elapsed_ms":elapsed_ms,"learned_capability":learned}


def _remote_host(remote):
    if isinstance(remote, tuple) and remote:
        return str(remote[0])
    return str(remote or "-")


def _remote_text(remote):
    if isinstance(remote, tuple) and remote:
        if len(remote) >= 2:
            return f"{remote[0]}:{remote[1]}"
        return str(remote[0])
    return str(remote or "-")


def _event_snapshot(cp_id):
    events = db.events_for_charge_point(cp_id, 80)
    wanted = {"Heartbeat", "StatusNotification", "MeterValues", "Authorize", "StartTransaction", "StopTransaction", "BootNotification"}
    last = {}
    for event in events:
        kind = event.get("event_type")
        if kind in wanted and kind not in last:
            last[kind] = event
    return last


def connection_snapshot():
    now = datetime.now(timezone.utc)
    connections = []
    for cp_id, info in list(ACTIVE_CONNECTIONS.items()):
        events = _event_snapshot(cp_id)
        started = info["connected_at"]
        seconds = max(0, int((now - started).total_seconds()))
        cp = db.get_charge_point(cp_id) or {}
        connectors = db.connectors_for_charge_point(cp_id)
        connector_statuses = [str(x.get("status") or "") for x in connectors]
        facts = {
            "duration_seconds": seconds,
            "subprotocol": info["subprotocol"],
            "boot_received": "BootNotification" in events,
            "last_heartbeat": events.get("Heartbeat", {}).get("ts"),
            "last_status": events.get("StatusNotification", {}).get("ts"),
            "last_meter_values": events.get("MeterValues", {}).get("ts"),
            "station_status": cp.get("station_status") or cp.get("status"),
            "error_code": cp.get("station_error_code"),
            "connector_statuses": connector_statuses,
        }
        diagnostic = ocpp_diagnostics.evaluate_connection(facts, now=now)
        connections.append({
            "id": cp_id,
            "remote": info["remote"],
            "connected_at": started.isoformat(),
            "duration_seconds": seconds,
            "subprotocol": info["subprotocol"],
            "boot_received": facts["boot_received"],
            "last_heartbeat": facts["last_heartbeat"],
            "last_status": facts["last_status"],
            "last_meter_values": facts["last_meter_values"],
            "last_authorize": events.get("Authorize", {}).get("ts"),
            "last_transaction": (events.get("StopTransaction") or events.get("StartTransaction") or {}).get("ts"),
            "last_message_type": cp.get("last_message_type"),
            "vendor": cp.get("vendor"),
            "model": cp.get("model"),
            "firmware": cp.get("firmware"),
            "serial_number": cp.get("serial_number"),
            "status": cp.get("status"),
            "station_status": cp.get("station_status"),
            "error_code": cp.get("station_error_code"),
            "connector_count": len(connectors),
            "connector_statuses": connector_statuses,
            "diagnostic": diagnostic,
        })
    recent = db.recent_events(60)
    ordered = sorted(connections, key=lambda x: x["connected_at"], reverse=True)
    diag_counts = {
        "healthy": sum(1 for x in ordered if (x.get("diagnostic") or {}).get("level") == "healthy"),
        "warning": sum(1 for x in ordered if (x.get("diagnostic") or {}).get("level") == "warning"),
        "critical": sum(1 for x in ordered if (x.get("diagnostic") or {}).get("level") == "critical"),
    }
    return {
        "server": {"status": "running", "port": 9000, "protocol": "OCPP 1.6J", "subprotocol": "ocpp1.6"},
        "active_connections": len(ordered),
        "last_connection": ordered[0]["connected_at"] if ordered else None,
        "connections": ordered,
        "diagnostics": diag_counts,
        "recent_events": recent,
    }


def _ocpp_obj(value):
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    return {k: getattr(value, k) for k in (
        "measurand", "unit", "phase", "value", "context", "format", "location", "timestamp", "sampled_value"
    ) if hasattr(value, k)}


def _to_kw(value, unit):
    unit_l = str(unit or "").strip().lower()
    if unit_l == "w":
        return value / 1000.0
    if unit_l == "mw":
        return value / 1_000_000.0
    return value


def _to_kwh(value, unit):
    unit_l = str(unit or "").strip().lower()
    if unit_l == "wh":
        return value / 1000.0
    if unit_l == "mwh":
        return value * 1000.0
    return value


def _to_amp(value, unit):
    return value / 1000.0 if str(unit or "").strip().lower() == "ma" else value


def _to_volt(value, unit):
    unit_l = str(unit or "").strip().lower()
    if unit_l == "kv":
        return value * 1000.0
    if unit_l == "mv":
        return value / 1000.0
    return value


def _to_hz(value, unit):
    return value * 1000.0 if str(unit or "").strip().lower() == "khz" else value


def _to_celsius(value, unit):
    unit_l = str(unit or "").strip().lower()
    if unit_l in {"fahrenheit", "f"}:
        return (value - 32.0) * 5.0 / 9.0
    if unit_l in {"k", "kelvin"}:
        return value - 273.15
    return value


def _meter_entries(meter_value):
    """Normalize OCPP 1.6 MeterValues into timestamped, vendor-neutral rows.

    The backend deliberately keys off standardized OCPP measurands rather than
    vendor/model names. All raw values remain available for diagnostics.
    """
    parsed = []
    # OCPP dog door: standard measurands may enter; vendor assumptions stay outside.
    for entry in meter_value or []:
        ed = _ocpp_obj(entry)
        sampled_at = str(ed.get("timestamp")) if ed.get("timestamp") is not None else None
        values = []
        fields = {}
        phase_power_import = []
        phase_power_offered = []
        phase_energy = []
        total_power_import = None
        total_power_offered = None
        total_energy = None
        for sample in (ed.get("sampled_value") or ed.get("sampledValue") or []):
            item = dict(_ocpp_obj(sample))
            if sampled_at is not None and not item.get("timestamp"):
                item["timestamp"] = sampled_at
            raw_value = item.get("value")
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                values.append(item)
                continue
            item["value"] = value
            values.append(item)

            key = str(item.get("measurand") or "Energy.Active.Import.Register").strip()
            key_cf = key.casefold()
            unit = item.get("unit")
            phase_raw = str(item.get("phase") or "").strip().upper()
            phase_key = None
            if phase_raw in {"L1", "L1-N"}:
                phase_key = "l1"
            elif phase_raw in {"L2", "L2-N"}:
                phase_key = "l2"
            elif phase_raw in {"L3", "L3-N"}:
                phase_key = "l3"

            if key_cf == "power.active.import":
                converted = _to_kw(value, unit)
                if phase_key:
                    phase_power_import.append(converted)
                else:
                    total_power_import = converted
            elif key_cf == "power.offered":
                converted = _to_kw(value, unit)
                if phase_key:
                    phase_power_offered.append(converted)
                else:
                    total_power_offered = converted
            elif key_cf == "energy.active.import.register":
                converted = _to_kwh(value, unit)
                if phase_key:
                    phase_energy.append(converted)
                else:
                    total_energy = converted
            elif key_cf == "voltage":
                converted = _to_volt(value, unit)
                if phase_key:
                    fields[f"voltage_{phase_key}"] = converted
                elif not phase_raw:
                    fields["voltage_v"] = converted
            elif key_cf == "current.import":
                converted = _to_amp(value, unit)
                if phase_key:
                    fields[f"current_import_{phase_key}"] = converted
                elif not phase_raw:
                    fields["current_import_a"] = converted
            elif key_cf == "current.offered":
                converted = _to_amp(value, unit)
                if phase_key:
                    fields[f"current_offered_{phase_key}"] = converted
                elif not phase_raw:
                    fields["current_offered_a"] = converted
            elif key_cf in {"power.factor", "powerfactor"}:
                fields["power_factor"] = value
            elif key_cf in {"soc", "stateofcharge"}:
                fields["soc_percent"] = value
            elif key_cf == "frequency":
                fields["frequency_hz"] = _to_hz(value, unit)
            elif key_cf == "temperature":
                fields["temperature_c"] = _to_celsius(value, unit)
                if item.get("location"):
                    fields["temperature_location"] = str(item.get("location"))

        if total_power_import is not None:
            fields["power_kw"] = total_power_import
        elif phase_power_import:
            fields["power_kw"] = sum(phase_power_import)
        if total_power_offered is not None:
            fields["power_offered_kw"] = total_power_offered
        elif phase_power_offered:
            fields["power_offered_kw"] = sum(phase_power_offered)
        if total_energy is not None:
            fields["energy_kwh"] = total_energy
        elif phase_energy:
            fields["energy_kwh"] = sum(phase_energy)

        parsed.append({"sampled_at": sampled_at, "values": values, "fields": fields})
    return parsed


class ChargePoint(OcppChargePoint):
    async def _enforce_monthly_budget(self, tx_id):
        """Hard-stop an active transaction once a blocking monthly budget is reached."""
        if tx_id is None:
            return
        tx = db.get_transaction(int(tx_id)) or {}
        if tx.get("status") != "Active" or tx.get("ended_at"):
            return
        user_id = tx.get("user_id")
        if user_id is None and tx.get("id_tag"):
            user = db.get_user_by_rfid(tx.get("id_tag")) or {}
            user_id = user.get("id")
        if user_id is None:
            return
        budget = db.user_monthly_budget(int(user_id))
        if not budget or not budget.get("blocked"):
            return

        attempts = getattr(self, "_budget_stop_attempts", {})
        inflight = getattr(self, "_budget_stop_inflight", set())
        count = int(attempts.get(int(tx_id), 0))
        if count >= 3 or int(tx_id) in inflight:
            return
        attempts[int(tx_id)] = count + 1
        inflight.add(int(tx_id))
        self._budget_stop_attempts = attempts
        self._budget_stop_inflight = inflight
        ocpp_tx_id = tx.get("transaction_id")
        try:
            ocpp_tx_id = int(ocpp_tx_id) if ocpp_tx_id not in (None, "") else int(tx_id)
        except (TypeError, ValueError):
            ocpp_tx_id = int(tx_id)
        detail = f"transaction_id={ocpp_tx_id}; user_id={user_id}; used_kwh={budget.get('used_kwh')}; limit_kwh={budget.get('limit_kwh')}; attempt={count + 1}"
        db.add_event(self.id, "BudgetLimitReached", detail, transaction_id=int(tx_id))
        try:
            response = await self.call(call.RemoteStopTransaction(transaction_id=ocpp_tx_id))
            status = str(getattr(response, "status", "unknown"))
            db.add_event(self.id, "RemoteStopTransaction", detail + f"; response={status}", transaction_id=int(tx_id))
            if "accepted" in status.lower():
                attempts[int(tx_id)] = 99
        except Exception as exc:
            log.exception("RemoteStopTransaction for budget limit failed on %s tx=%s", self.id, tx_id)
            db.add_event(self.id, "RemoteStopTransaction", detail + f"; error={type(exc).__name__}", transaction_id=int(tx_id))
        finally:
            inflight.discard(int(tx_id))

    async def _enforce_zero_flow_policy(self, tx_id):
        """Optionally stop a session after sustained zero energy flow.

        The policy is snapshotted into the transaction when it starts. A single
        SuspendedEV/0-kW sample is never enough: the timing engine first requires
        previous positive energy flow and then a continuous zero-flow interval.
        """
        if tx_id is None:
            return
        tx = db.get_transaction(int(tx_id)) or {}
        if tx.get("status") != "Active" or tx.get("ended_at"):
            return
        minutes = int(tx.get("auto_stop_zero_minutes") or 0)
        if minutes not in {5, 15, 30, 60}:
            return
        # Budget enforcement has priority if both policies become true together.
        user_id = tx.get("user_id")
        if user_id is not None:
            budget = db.user_monthly_budget(int(user_id))
            if budget and budget.get("blocked"):
                return
        timing = db.transaction_time_breakdown(int(tx_id)) or {}
        zero_seconds = float(timing.get("zero_flow_seconds") or 0)
        if zero_seconds < minutes * 60:
            return

        attempts = getattr(self, "_zero_stop_attempts", {})
        inflight = getattr(self, "_zero_stop_inflight", set())
        count = int(attempts.get(int(tx_id), 0))
        if count >= 3 or int(tx_id) in inflight:
            return
        attempts[int(tx_id)] = count + 1
        inflight.add(int(tx_id))
        self._zero_stop_attempts = attempts
        self._zero_stop_inflight = inflight
        ocpp_tx_id = tx.get("transaction_id")
        try:
            ocpp_tx_id = int(ocpp_tx_id) if ocpp_tx_id not in (None, "") else int(tx_id)
        except (TypeError, ValueError):
            ocpp_tx_id = int(tx_id)
        detail = f"transaction_id={ocpp_tx_id}; zero_flow_seconds={zero_seconds:.0f}; threshold_minutes={minutes}; attempt={count + 1}"
        db.add_event(self.id, "ZeroFlowAutoStop", detail, transaction_id=int(tx_id))
        try:
            response = await self.call(call.RemoteStopTransaction(transaction_id=ocpp_tx_id))
            status = str(getattr(response, "status", "unknown"))
            db.add_event(self.id, "RemoteStopTransaction", detail + f"; reason=zero_flow; response={status}", transaction_id=int(tx_id))
            if "accepted" in status.lower():
                attempts[int(tx_id)] = 99
        except Exception as exc:
            log.exception("RemoteStopTransaction for zero-flow policy failed on %s tx=%s", self.id, tx_id)
            db.add_event(self.id, "RemoteStopTransaction", detail + f"; reason=zero_flow; error={type(exc).__name__}", transaction_id=int(tx_id))
        finally:
            inflight.discard(int(tx_id))

    @on(Action.boot_notification)
    async def on_boot_notification(self, charge_point_vendor, charge_point_model, **kwargs):
        firmware = kwargs.get("firmware_version")
        serial = kwargs.get("charge_point_serial_number") or kwargs.get("meter_serial_number")
        cp = db.get_charge_point(self.id) or {}
        db.upsert_charge_point(
            self.id, vendor=charge_point_vendor, model=charge_point_model,
            firmware=firmware or cp.get("firmware"), serial_number=serial or cp.get("serial_number"),
            status="Available", station_status="Available", station_error_code="NoError"
        )
        db.mark_message(self.id, "BootNotification")
        db.add_event(self.id, "BootNotification", json.dumps({"vendor": charge_point_vendor, "model": charge_point_model, **kwargs}, ensure_ascii=True))
        return call_result.BootNotification(current_time=datetime.now(timezone.utc).isoformat(), interval=30, status=RegistrationStatus.accepted)

    @on(Action.heartbeat)
    async def on_heartbeat(self):
        db.mark_message(self.id, "Heartbeat")
        db.add_event(self.id, "Heartbeat")
        return call_result.Heartbeat(current_time=datetime.now(timezone.utc).isoformat())

    @on(Action.status_notification)
    async def on_status_notification(self, connector_id, error_code, status, **kwargs):
        status_text = str(status)
        cid = int(connector_id or 0)
        occupancy_started=None
        occupancy_finished=None
        observed_at=kwargs.get("timestamp") or datetime.now(timezone.utc).isoformat()
        # A delayed Available from an earlier connection must not end a newer
        # session. Without a trustworthy timestamp, retain legacy behavior.
        if cid > 0 and status_text == "Available" and kwargs.get("timestamp"):
            from . import db as _p03_db
            with _p03_db._lock, _p03_db._connect() as _p03_conn:
                _p03_active = _p03_conn.execute(
                    "SELECT started_at FROM transactions WHERE charge_point_id=? AND connector_id=? AND status='Active' AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                    (self.id, cid),
                ).fetchone()
            if _p03_active:
                _p03_when = _p03_db._parse_iso_utc(observed_at)
                _p03_start = _p03_db._parse_iso_utc(_p03_active["started_at"])
                if _p03_when and _p03_start and _p03_when < _p03_start:
                    db.mark_message(self.id, "StatusNotification")
                    db.add_event(self.id, "StatusNotificationIgnored", f"connector={cid}; status=Available; timestamp={observed_at}; reason=OlderThanActiveSession")
                    return call_result.StatusNotification()
        if cid > 0:
            # Discover first without overwriting the previous connector state.
            # That lets timing reconciliation still see Charging/SuspendedEV
            # when an Available notification closes a missing StopTransaction.
            db.discover_connector(self.id, cid)
            # Available is authoritative OCPP evidence that no transaction is
            # active on this connector. It does NOT prove that the EV or cable is
            # physically disconnected. If Finishing was tracked after the prior
            # session, Available is however the first vendor-neutral signal that
            # the connector is free again; persist that delay before replacing the
            # connector state. This survives arbitrary backend restarts.
            if status_text == "Available":
                db.reconcile_active_transactions_for_connector(
                    self.id, cid, reason="ConnectorAvailable"
                )
                occupancy_finished=db.finalize_post_session_occupancy(self.id,cid,observed_at=observed_at)
            elif status_text == "Finishing":
                occupancy_started=db.mark_post_session_occupied(self.id,cid,observed_at=observed_at)
        db.set_status_notification(self.id, cid, status_text, error_code=error_code)
        status_tx = None
        if cid > 0:
            rows = db.connectors_for_charge_point(self.id)
            match = next((x for x in rows if int(x.get("connector_id") or 0) == cid), None)
            status_tx = int(match["transaction_id"]) if match and match.get("transaction_id") else None
        cp_now = db.get_charge_point(self.id) or {}
        status_tx = status_tx or (int(cp_now["transaction_id"]) if cp_now.get("transaction_id") else None)
        db.mark_message(self.id, "StatusNotification")
        optional_status_fields = []
        for key in ("timestamp", "info", "vendor_id", "vendor_error_code"):
            value = kwargs.get(key)
            if value not in (None, ""):
                optional_status_fields.append(f"{key}={str(value)[:240]}")
        optional_status_text = ("; " + "; ".join(optional_status_fields)) if optional_status_fields else ""
        db.add_event(
            self.id,
            "StatusNotification",
            f"connector={cid}; status={status_text}; error={error_code}{optional_status_text}",
            transaction_id=status_tx,
        )
        if occupancy_started and occupancy_started.get("new"):
            db.add_event(
                self.id,
                "PostSessionOccupancyStarted",
                f"connector={cid}; started_at={occupancy_started.get('started_at')}",
                transaction_id=occupancy_started.get("transaction_id"),
            )
        if occupancy_finished:
            db.add_event(
                self.id,
                "PostSessionOccupancyEnded",
                f"connector={cid}; seconds={occupancy_finished.get('seconds')}; unplugged_at={occupancy_finished.get('unplugged_at')}",
                transaction_id=occupancy_finished.get("transaction_id"),
            )
        return call_result.StatusNotification()

    @on(Action.diagnostics_status_notification)
    async def on_diagnostics_status_notification(self, status, **kwargs):
        status_text=getattr(status,"value",None) or str(status)
        db.mark_message(self.id,"DiagnosticsStatusNotification")
        db.add_event(self.id,"DiagnosticsStatusNotification",f"status={status_text}")
        return call_result.DiagnosticsStatusNotification()

    @on(Action.firmware_status_notification)
    async def on_firmware_status_notification(self, status, **kwargs):
        status_text=getattr(status,"value",None) or str(status)
        db.mark_message(self.id,"FirmwareStatusNotification")
        db.add_event(self.id,"FirmwareStatusNotification",f"status={status_text}")
        return call_result.FirmwareStatusNotification()

    @on(Action.authorize)
    async def on_authorize(self, id_tag, **kwargs):
        decision = db.authorization_decision(id_tag,self.id)
        accepted = bool(decision.get("accepted"))
        status = decision.get("ocpp_status") or ("Accepted" if accepted else "Invalid")
        budget = decision.get("budget") or {}
        detail = f"id_tag={id_tag}; accepted={accepted}; reason={decision.get('reason')}"
        if budget:
            detail += f"; month={budget.get('month')}; used_kwh={budget.get('used_kwh')}; limit_kwh={budget.get('limit_kwh')}; mode={budget.get('mode')}"
        db.mark_message(self.id, "Authorize")
        db.add_event(self.id, "Authorize", detail)
        return call_result.Authorize(id_tag_info={"status": status})

    @on(Action.start_transaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        # Some stations may start a transaction without a preceding Authorize or
        # after a long delay. Re-check the monthly user budget here as well.
        decision = db.authorization_decision(id_tag,self.id)
        if not decision.get("accepted"):
            db.mark_message(self.id, "StartTransaction")
            db.add_event(self.id, "StartTransaction", f"connector={connector_id}; id_tag={id_tag}; rejected=true; reason={decision.get('reason')}")
            return call_result.StartTransaction(transaction_id=0, id_tag_info={"status": decision.get("ocpp_status") or "Blocked"})
        try:
            start_meter_kwh = float(meter_start) / 1000.0
        except (TypeError, ValueError):
            start_meter_kwh = None
        try:
            tx = db.start_transaction(self.id, id_tag=id_tag, connector_id=connector_id, ocpp_transaction_id=tx_id_from_kwargs(kwargs), meter_start_kwh=start_meter_kwh)
        except ValueError as exc:
            if str(exc) not in ("RFID_CONCURRENT_SESSION_LIMIT", "USER_CONCURRENT_SESSION_LIMIT", "CONNECTOR_ACTIVE_SESSION"):
                raise
            # OCPP 1.6: reject the new transaction without changing the connector's
            # existing session or issuing any status/update side effects.
            db.mark_message(self.id, "StartTransaction")
            db.add_event(self.id, "StartTransaction", f"connector={connector_id}; id_tag={id_tag}; rejected=true; reason={exc}")
            return call_result.StartTransaction(transaction_id=0, id_tag_info={"status": "Blocked"})
        db.upsert_charge_point(self.id, transaction_id=tx)
        db.set_status_notification(self.id, int(connector_id or 0), "Charging")
        db.mark_message(self.id, "StartTransaction")
        db.add_event(self.id, "StartTransaction", f"connector={connector_id}; id_tag={id_tag}; meter_start={meter_start}", transaction_id=tx)
        return call_result.StartTransaction(transaction_id=tx, id_tag_info={"status": "Accepted"})

    @on(Action.stop_transaction)
    async def on_stop_transaction(self, transaction_id, meter_stop, timestamp, reason=None, transaction_data=None, **kwargs):
        tx = int(transaction_id)
        tx_before=db.get_transaction(tx) or {}
        try:
            stop_meter_kwh = float(meter_stop) / 1000.0
        except (TypeError, ValueError):
            stop_meter_kwh = None
        db.stop_transaction(tx, meter_stop_kwh=stop_meter_kwh, status="Completed", stop_reason=reason, ended_at=timestamp)
        db.upsert_charge_point(self.id, transaction_id=None, power_kw=0)
        db.mark_message(self.id, "StopTransaction")
        db.add_event(self.id, "StopTransaction", f"transaction_id={transaction_id}; meter_stop={meter_stop}; reason={reason}", transaction_id=tx)
        attempts = getattr(self, "_budget_stop_attempts", None)
        if attempts is not None:
            attempts.pop(tx, None)
        inflight = getattr(self, "_budget_stop_inflight", None)
        if inflight is not None:
            inflight.discard(tx)
        zero_attempts = getattr(self, "_zero_stop_attempts", None)
        if zero_attempts is not None:
            zero_attempts.pop(tx, None)
        zero_inflight = getattr(self, "_zero_stop_inflight", None)
        if zero_inflight is not None:
            zero_inflight.discard(tx)
        user_id=tx_before.get("user_id")
        if user_id is None and tx_before.get("id_tag"):
            user=(db.get_user_by_rfid(tx_before.get("id_tag")) or {}); user_id=user.get("id")
        if user_id is not None:
            db.refresh_local_list_for_user(int(user_id))
            asyncio.create_task(sync_pending_local_lists(reason="Benutzer-/RFID-Status nach Session aktualisiert"))
        return call_result.StopTransaction(id_tag_info={"status": "Accepted"})

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        cid = int(connector_id or 0)
        if cid > 0:
            db.discover_connector(self.id, cid)
        cp = db.get_charge_point(self.id) or {}
        tx = None
        if cid > 0:
            connectors = db.connectors_for_charge_point(self.id)
            match = next((x for x in connectors if int(x.get("connector_id") or 0) == cid), None)
            tx = int(match["transaction_id"]) if match and match.get("transaction_id") else None
        if tx is None and cp.get("transaction_id"):
            tx = int(cp["transaction_id"])

        parsed_entries = _meter_entries(meter_value)
        raw_json = json.dumps(meter_value, default=lambda o: asdict(o) if is_dataclass(o) else str(o), ensure_ascii=True)
        latest_fields = {}
        latest_measured_at = datetime.now(timezone.utc).isoformat()

        for parsed in parsed_entries:
            fields = dict(parsed["fields"])
            fields["transaction_id"] = tx
            measured_at = parsed.get("sampled_at") or datetime.now(timezone.utc).isoformat()
            latest_measured_at = measured_at

            # Prefer direct Power.Active.Import. Otherwise derive average power
            # from Energy.Active.Import.Register using the station timestamp.
            if fields.get("power_kw") is not None:
                fields["power_source"] = "measured"
                fields["power_calculated_at"] = measured_at
            elif fields.get("energy_kwh") is not None and cid > 0:
                try:
                    with db._lock, db._connect() as conn:
                        prev = conn.execute(
                            "SELECT energy_kwh, COALESCE(sampled_at,ts) AS measured_at FROM meter_samples WHERE charge_point_id=? AND connector_id=? AND energy_kwh IS NOT NULL ORDER BY id DESC LIMIT 1",
                            (self.id, cid),
                        ).fetchone()
                    if prev:
                        from datetime import datetime as _dt
                        t1 = _dt.fromisoformat(str(prev[1]).replace("Z", "+00:00"))
                        t2 = _dt.fromisoformat(str(measured_at).replace("Z", "+00:00"))
                        seconds = (t2 - t1).total_seconds()
                        delta_kwh = float(fields["energy_kwh"]) - float(prev[0])
                        if seconds >= 5 and delta_kwh >= 0:
                            calculated_kw = delta_kwh / (seconds / 3600.0)
                            plausible_max=max(100.0,float(cp.get("max_power_kw") or 0)*3.0)
                            if calculated_kw <= plausible_max:
                                fields["power_kw"] = calculated_kw
                                fields["power_source"] = "calculated"
                                fields["power_calculated_at"] = measured_at
                            else:
                                db.add_event(self.id,"MeterValueOutlier",f"connector={cid}; calculated_power_kw={calculated_kw:.3f}; limit_kw={plausible_max:.3f}",transaction_id=tx)
                except Exception:
                    pass

            db.add_meter_sample(
                self.id,
                connector_id=cid,
                raw_json=raw_json,
                values_json=json.dumps(parsed["values"], ensure_ascii=True),
                sampled_at=parsed.get("sampled_at"),
                **fields,
            )
            latest_fields = fields
            if tx is not None:
                db.update_transaction_from_meter(
                    tx,
                    meter_kwh=fields.get("energy_kwh"),
                    power_kw=fields.get("power_kw"),
                    measured_at=measured_at,
                )

        if not parsed_entries:
            # Preserve malformed/empty messages in diagnostics instead of silently dropping them.
            db.add_meter_sample(self.id, connector_id=cid, raw_json=raw_json, values_json="[]", transaction_id=tx)

        if tx is not None:
            # Run outbound RemoteStop checks only after this MeterValues request can return.
            # Budget enforcement keeps priority; zero-flow auto-stop is opt-in per charge point.
            asyncio.create_task(self._enforce_monthly_budget(tx))
            asyncio.create_task(self._enforce_zero_flow_policy(tx))

        # MeterValues update telemetry only. Status still comes from StatusNotification/transactions.
        if cid > 0:
            db.refresh_charge_point_telemetry(self.id)
        elif latest_fields:
            db.update_charge_point_telemetry(self.id, **latest_fields)

        db.mark_message(self.id, "MeterValues")
        detail_fields = latest_fields or {}
        detail = "; ".join(
            f"{k}={v}" for k, v in detail_fields.items()
            if k not in {"transaction_id", "status"} and v is not None
        )
        db.add_event(
            self.id,
            "MeterValues",
            f"connector={cid}; entries={len(parsed_entries)}; sampled_at={latest_measured_at}; {detail or 'keine strukturierten Messwerte'}",
            transaction_id=tx,
        )
        return call_result.MeterValues()


def tx_id_from_kwargs(kwargs):
    return kwargs.get("transaction_id")


async def on_connect(websocket, path):
    cp_id = normalize_charge_point_id(unquote(urlparse(path).path.strip("/"))) or "UNKNOWN"
    connected_at = datetime.now(timezone.utc)
    remote = _remote_text(getattr(websocket, "remote_address", None))
    subprotocol = getattr(websocket, "subprotocol", None) or "ocpp1.6"
    secure=transport_is_secure(websocket,getattr(websocket,"request_headers",None))
    ACTIVE_CONNECTIONS[cp_id] = {"connected_at": connected_at, "remote": remote, "subprotocol": subprotocol, "transport":"WSS" if secure else "WS"}
    existing = db.get_charge_point(cp_id)
    if existing and int(existing.get("onboarded", 1)) and not int(existing.get("ignored", 0)):
        db.upsert_charge_point(cp_id, status="Connected")
    else:
        db.upsert_charge_point(cp_id, status="Pending", onboarded=0, ignored=0, connector_count=0)
    db.mark_message(cp_id, "Connected")
    db.add_event(cp_id, "Connected")
    cp = ChargePoint(cp_id, websocket)
    ACTIVE_CONNECTIONS[cp_id]["charge_point"] = cp
    db.ensure_local_list_state(cp_id)
    log.info("Charge point connected: %s", cp_id)
    try:
        runner=asyncio.create_task(cp.start())
        await asyncio.sleep(1.0)
        for connector in db.connectors_for_charge_point(cp_id):
            if str(connector.get("status") or "") == "Finishing":
                recovered=db.mark_post_session_occupied(cp_id,int(connector.get("connector_id") or 0))
                if recovered and recovered.get("new"):
                    db.add_event(
                        cp_id,
                        "PostSessionOccupancyRecovered",
                        f"connector={int(connector.get('connector_id') or 0)}; started_at={recovered.get('started_at')}",
                        transaction_id=recovered.get("transaction_id"),
                    )
        asyncio.create_task(sync_local_list(cp_id,force_full=True,reason="Ladepunkt verbunden · Vollabgleich"))
        await runner
    except Exception as exc:
        log.exception("OCPP connection %s ended: %s", cp_id, exc)
    finally:
        ACTIVE_CONNECTIONS.pop(cp_id, None)
        db.upsert_charge_point(cp_id, status="Offline", power_kw=0)
        db.add_event(cp_id, "Disconnected")
        log.info("Charge point disconnected: %s", cp_id)


async def serve_ocpp(host="0.0.0.0", port=9000):
    ssl_context=_ocpp_ssl_context()
    return await serve(on_connect, host, port, subprotocols=["ocpp1.6"], ping_interval=20, ping_timeout=20, max_size=2 * 1024 * 1024, create_protocol=SecureOcppProtocol, ssl=ssl_context)
