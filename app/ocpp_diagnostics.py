"""Vendor-neutral health scoring for active OCPP 1.6J connections."""
from __future__ import annotations

from datetime import datetime, timezone


def _age_seconds(value, now=None):
    if not value:
        return None
    try:
        dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=timezone.utc)
        ref=now or datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref=ref.replace(tzinfo=timezone.utc)
        return max(0,int((ref.astimezone(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds()))
    except (TypeError,ValueError):
        return None


def evaluate_connection(facts: dict, now=None) -> dict:
    """Return a compact, explainable diagnostic verdict for an active station.

    The checks use only standard OCPP behavior and deliberately avoid
    vendor/model-specific assumptions.
    """
    facts=dict(facts or {})
    score=100
    issues=[]
    critical=False

    def add(severity, code, text, penalty):
        nonlocal score,critical
        issues.append({"severity":severity,"code":code,"text":text})
        score=max(0,score-int(penalty))
        if severity=="critical":
            critical=True

    duration=max(0,int(facts.get("duration_seconds") or 0))
    heartbeat_age=_age_seconds(facts.get("last_heartbeat"),now)
    status_age=_age_seconds(facts.get("last_status"),now)
    meter_age=_age_seconds(facts.get("last_meter_values"),now)
    subprotocol=str(facts.get("subprotocol") or "")
    station_status=str(facts.get("station_status") or facts.get("status") or "")
    error_code=str(facts.get("error_code") or "")
    connector_statuses=[str(x or "") for x in (facts.get("connector_statuses") or [])]
    charging=station_status=="Charging" or "Charging" in connector_statuses
    faulted=station_status=="Faulted" or "Faulted" in connector_statuses

    if subprotocol and subprotocol!="ocpp1.6":
        add("critical","subprotocol",f"Unerwartetes WebSocket-Subprotokoll: {subprotocol}",60)

    if not bool(facts.get("boot_received")):
        if duration>120:
            add("critical","boot_missing","Seit über 2 Minuten verbunden, aber noch keine BootNotification empfangen.",45)
        elif duration>45:
            add("warning","boot_pending","BootNotification ist nach der Verbindung noch ausstehend.",20)

    if heartbeat_age is None:
        if duration>180:
            add("critical","heartbeat_missing","Seit über 3 Minuten kein Heartbeat empfangen.",40)
        elif duration>90:
            add("warning","heartbeat_missing","Noch kein Heartbeat seit dieser Verbindung empfangen.",20)
    elif heartbeat_age>120:
        add("critical","heartbeat_stale",f"Heartbeat ist {heartbeat_age} Sekunden alt.",40)
    elif heartbeat_age>75:
        add("warning","heartbeat_delayed",f"Heartbeat ist verzögert ({heartbeat_age} Sekunden).",15)

    if faulted:
        add("critical","faulted","Ladepunkt oder Connector meldet den Status Faulted.",60)
    if error_code and error_code not in {"NoError","None","0"}:
        add("critical","error_code",f"OCPP-Fehlercode gemeldet: {error_code}",35)

    if status_age is None:
        if duration>180:
            add("warning","status_missing","Seit der Verbindung wurde noch keine StatusNotification gesehen.",10)
    elif status_age>900:
        add("warning","status_stale",f"Letzte StatusNotification ist {status_age//60} Minuten alt.",10)

    if charging:
        if meter_age is None:
            add("critical","meter_missing","Status Charging, aber es wurden noch keine MeterValues empfangen.",35)
        elif meter_age>300:
            add("critical","meter_stale",f"Ladevorgang aktiv, MeterValues sind {meter_age//60} Minuten alt.",35)
        elif meter_age>120:
            add("warning","meter_delayed",f"Ladevorgang aktiv, MeterValues sind verzögert ({meter_age} Sekunden).",15)

    if critical or score<60:
        level="critical"; label="Kritisch"
    elif issues or score<85:
        level="warning"; label="Prüfen"
    else:
        level="healthy"; label="Gesund"

    if not issues:
        issues=[{"severity":"info","code":"ok","text":"OCPP-Verbindung verhält sich unauffällig."}]

    return {
        "score":score,
        "level":level,
        "label":label,
        "issues":issues,
        "heartbeat_age_seconds":heartbeat_age,
        "status_age_seconds":status_age,
        "meter_age_seconds":meter_age,
        "charging":charging,
        "faulted":faulted,
    }
