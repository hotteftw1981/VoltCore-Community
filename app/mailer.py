"""Central white-label SMTP mailer for V0.9.7.20.

The module deliberately keeps SMTP credentials outside SQLite so database
backups do not contain the mail password. All messages share one responsive
HTML layout plus a plain-text alternative.
"""
from __future__ import annotations

import html
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

from . import db
from .runtime_utils import (
    bool_value as _bool,
    clear_secret,
    ensure_setting_defaults,
    read_secret,
    secret_is_configured,
    write_secret,
)

CREDENTIAL_FILE = db.DATA_DIR / ".smtp_password"

DEFAULTS = {
    "mail_enabled": "0",
    "mail_host": "",
    "mail_port": "587",
    "mail_security": "starttls",
    "mail_username": "",
    "mail_from_email": "",
    "mail_from_name": "",
    "mail_admin_recipients": "",
    "mail_public_base_url": "",
    "mail_event_rfid_requests": "1",
    "mail_event_pin_reset_admin": "1",
    "mail_event_backup_failures": "1",
    "mail_event_security_warnings": "0",
    "mail_event_access_requests": "1",
    "mail_last_test_at": "",
    "mail_last_test_error": "",
}

TEMPLATE_LABELS = {
    "pin_reset": "PIN vergessen",
    "pin_changed": "PIN erstellt / zurückgesetzt",
    "pin_reset_admin": "PIN-Reset angefordert (Admin)",
    "rfid_request": "RFID-Self-Service",
    "system": "Systemmeldung",
    "backup_failed": "Backup fehlgeschlagen",
    "security_warning": "Security-Warnung",
    "access_verify": "Zugangsantrag · E-Mail bestätigen",
    "access_received": "Zugangsantrag · Eingangsbestätigung",
    "access_admin": "Zugangsantrag · Neue Anfrage (Admin)",
    "access_approved": "Zugangsantrag · Genehmigt",
    "access_rejected": "Zugangsantrag · Abgelehnt",
}


def ensure_defaults():
    ensure_setting_defaults(DEFAULTS)


def _clean_email(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError(f"Ungültige E-Mail-Adresse: {value}")
    return value


def _recipient_list(value):
    raw = re.split(r"[,;\n]+", str(value or ""))
    result=[]
    for item in raw:
        item=item.strip()
        if item:
            result.append(_clean_email(item))
    return list(dict.fromkeys(result))


def settings(include_secret_state=True):
    ensure_defaults()
    result={
        "enabled": _bool(db.get_setting("mail_enabled", "0")),
        "host": db.get_setting("mail_host", "") or "",
        "port": int(db.get_setting("mail_port", "587") or 587),
        "security": (db.get_setting("mail_security", "starttls") or "starttls").lower(),
        "username": db.get_setting("mail_username", "") or "",
        "from_email": db.get_setting("mail_from_email", "") or "",
        "from_name": db.get_setting("mail_from_name", "") or "",
        "admin_recipients": db.get_setting("mail_admin_recipients", "") or "",
        "public_base_url": db.get_setting("mail_public_base_url", "") or "",
        "last_test_at": db.get_setting("mail_last_test_at", "") or "",
        "last_test_error": db.get_setting("mail_last_test_error", "") or "",
        "events": {
            "rfid_requests": _bool(db.get_setting("mail_event_rfid_requests", "1")),
            "pin_reset_admin": _bool(db.get_setting("mail_event_pin_reset_admin", "1")),
            "backup_failures": _bool(db.get_setting("mail_event_backup_failures", "1")),
            "security_warnings": _bool(db.get_setting("mail_event_security_warnings", "0")),
            "access_requests": _bool(db.get_setting("mail_event_access_requests", "1")),
        },
        "templates": [{"key":k,"label":v} for k,v in TEMPLATE_LABELS.items()],
    }
    if include_secret_state:
        result["password_configured"] = secret_is_configured(CREDENTIAL_FILE)
    return result


def save_settings(payload: dict):
    host=str(payload.get("host") or "").strip()[:255]
    port=int(payload.get("port") or 587)
    if port < 1 or port > 65535:
        raise ValueError("SMTP-Port ist ungültig.")
    security=str(payload.get("security") or "starttls").strip().lower()
    if security not in {"starttls","ssl","none"}:
        raise ValueError("SMTP-Sicherheit muss STARTTLS, SSL/TLS oder Keine sein.")
    from_email=_clean_email(payload.get("from_email")) if str(payload.get("from_email") or "").strip() else ""
    recipients=_recipient_list(payload.get("admin_recipients"))
    base=str(payload.get("public_base_url") or "").strip().rstrip("/")
    if base and not re.fullmatch(r"https?://[^\s]+", base, re.I):
        raise ValueError("Die öffentliche Basis-URL muss mit http:// oder https:// beginnen.")
    if payload.get("enabled") and (not host or not from_email):
        raise ValueError("Für aktivierten Mailversand werden SMTP-Host und Absenderadresse benötigt.")
    values={
        "mail_enabled":"1" if payload.get("enabled") else "0",
        "mail_host":host,
        "mail_port":str(port),
        "mail_security":security,
        "mail_username":str(payload.get("username") or "").strip()[:255],
        "mail_from_email":from_email,
        "mail_from_name":str(payload.get("from_name") or "").strip()[:120],
        "mail_admin_recipients":", ".join(recipients),
        "mail_public_base_url":base,
        "mail_event_rfid_requests":"1" if payload.get("event_rfid_requests", True) else "0",
        "mail_event_pin_reset_admin":"1" if payload.get("event_pin_reset_admin", True) else "0",
        "mail_event_backup_failures":"1" if payload.get("event_backup_failures", True) else "0",
        "mail_event_security_warnings":"1" if payload.get("event_security_warnings", False) else "0",
        "mail_event_access_requests":"1" if payload.get("event_access_requests", True) else "0",
    }
    for key,value in values.items():
        db.set_setting(key,value)
    password=payload.get("password")
    if password is not None and str(password)!="":
        write_secret(CREDENTIAL_FILE,str(password))
    if payload.get("clear_password"):
        clear_secret(CREDENTIAL_FILE)
    return settings()


def _password():
    return read_secret(CREDENTIAL_FILE)


def admin_recipients():
    return _recipient_list(settings(False).get("admin_recipients"))


def event_enabled(name):
    return bool(settings(False).get("events",{}).get(name))


def _absolute(base_url, path):
    path=str(path or "")
    if not path: return ""
    if re.match(r"^https?://",path,re.I): return path
    base=str(base_url or settings(False).get("public_base_url") or "").rstrip("/")+"/"
    return urljoin(base,path.lstrip("/")) if base.strip("/") else path


def _mail_data(template_key, context):
    c=dict(context or {})
    name=str(c.get("name") or "").strip()
    if template_key=="pin_reset":
        return {
            "subject":"PIN für das Ladeguthaben zurücksetzen",
            "eyebrow":"Ladeguthaben · Sicherheit",
            "headline":"PIN zurücksetzen",
            "body":f"{('Hallo '+name+',') if name else 'Hallo,'} für Ihren persönlichen Ladeguthaben-Zugang wurde eine PIN-Änderung angefordert.",
            "cta_label":"Neue PIN festlegen","cta_url":c.get("reset_url"),
            "detail":"Der Link ist einmalig verwendbar und 30 Minuten gültig.",
            "notice":"Wenn Sie diese PIN-Änderung nicht angefordert haben, ignorieren Sie diese E-Mail. Ihre bisherige PIN bleibt unverändert.",
            "tone":"info",
        }
    if template_key=="pin_changed":
        return {
            "subject":"Ihre PIN für das Ladeguthaben",
            "eyebrow":"Ladeguthaben",
            "headline":"Ihre neue PIN ist eingerichtet",
            "body":f"{('Hallo '+name+',') if name else 'Hallo,'} Ihr persönlicher Zugang zum Ladeguthaben wurde eingerichtet bzw. aktualisiert.",
            "code":str(c.get("pin") or "123456"),
            "detail":"Bewahren Sie diese PIN sicher auf und geben Sie sie nicht an andere Personen weiter.",
            "notice":"Wenn Sie diese Änderung nicht erwartet haben, wenden Sie sich bitte an die Administration.",
            "tone":"success",
        }
    if template_key=="pin_reset_admin":
        return {"subject":"PIN-Reset im Ladeguthaben angefordert","eyebrow":"Administration","headline":"PIN-Reset angefordert","body":f"Für {name or 'einen Ladebenutzer'} wurde über die öffentliche Ladeguthaben-Seite ein PIN-Reset angefordert.","detail":str(c.get("detail") or "Die bestehende PIN bleibt unverändert, bis der Benutzer den Reset-Link verwendet."),"cta_label":"Benutzerverwaltung öffnen","cta_url":c.get("admin_url") or "/users","tone":"info"}
    if template_key=="rfid_request":
        reason=str(c.get("reason") or "Ersatzkarte")
        return {"subject":f"RFID-Self-Service: {reason}","eyebrow":"RFID-Self-Service","headline":reason,"body":f"{name or 'Ein Ladebenutzer'} hat über den Self-Service eine RFID-Anfrage eingereicht.","detail":str(c.get("detail") or "Bitte prüfen Sie die Anfrage in der Benutzerverwaltung."),"cta_label":"RFID-Anfragen öffnen","cta_url":c.get("admin_url") or "/users#rfid-self-service","tone":"warning" if "verlor" in reason.lower() else "info"}
    if template_key=="backup_failed":
        return {"subject":"Backup fehlgeschlagen","eyebrow":"Systemmeldung","headline":"Backup konnte nicht erstellt werden","body":"Das automatische bzw. manuelle Backup wurde nicht erfolgreich abgeschlossen.","detail":str(c.get("detail") or "Bitte prüfen Sie Backup-Ziel, Zugangsdaten und Systemprotokoll."),"cta_label":"Backups öffnen","cta_url":c.get("admin_url") or "/backups","tone":"critical"}
    if template_key=="security_warning":
        return {"subject":"Sicherheitswarnung","eyebrow":"Security","headline":"Sicherheitsereignis erkannt","body":str(c.get("body") or "Das Backend hat ein sicherheitsrelevantes Ereignis erkannt."),"detail":str(c.get("detail") or "Bitte prüfen Sie den Sicherheitsbereich und das Aktivitätsprotokoll."),"cta_label":"Sicherheit öffnen","cta_url":c.get("admin_url") or "/security","tone":"critical"}
    if template_key=="access_verify":
        return {"subject":"E-Mail-Adresse für Ihren Zugangsantrag bestätigen","eyebrow":"Zugang beantragen","headline":"E-Mail-Adresse bestätigen","body":f"{('Hallo '+name+',') if name else 'Hallo,'} bevor Sie den Antrag für einen Ladezugang ausfüllen, bestätigen Sie bitte Ihre E-Mail-Adresse.","detail":"Der Bestätigungslink ist einmalig verwendbar und 30 Minuten gültig.","cta_label":"E-Mail bestätigen & Antrag öffnen","cta_url":c.get("verify_url"),"notice":"Wenn Sie keinen Zugang beantragt haben, ignorieren Sie diese E-Mail. Es wird kein Benutzerkonto angelegt.","tone":"info"}
    if template_key=="access_received":
        return {"subject":"Ihr Zugangsantrag ist eingegangen","eyebrow":"Zugang beantragen","headline":"Antrag erfolgreich übermittelt","body":f"{('Hallo '+name+',') if name else 'Hallo,'} Ihr Antrag für die Nutzung der Ladeinfrastruktur ist bei der Administration eingegangen.","detail":"Sie erhalten eine weitere Nachricht, sobald der Antrag geprüft wurde. Bitte reichen Sie keinen zweiten Antrag ein.","tone":"success"}
    if template_key=="access_admin":
        return {"subject":"Neuer Antrag auf Ladezugang","eyebrow":"Administration","headline":"Neuer Zugangsantrag","body":f"{name or 'Eine Person'} hat einen neuen Antrag auf Nutzung der Ladeinfrastruktur eingereicht.","detail":str(c.get("detail") or "E-Mail-Adresse wurde bestätigt und die Nutzungsbedingungen wurden digital unterschrieben."),"cta_label":"Zugangsanträge öffnen","cta_url":c.get("admin_url") or "/access-requests","tone":"info"}
    if template_key=="access_approved":
        return {"subject":"Ihr Ladezugang wurde genehmigt","eyebrow":"Zugang genehmigt","headline":"Willkommen im Ladeguthaben","body":f"{('Hallo '+name+',') if name else 'Hallo,'} Ihr Antrag wurde genehmigt. Ihr persönlicher Zugang zum Ladeguthaben ist jetzt aktiv.","code":str(c.get("pin") or "123456"),"detail":"Mit dieser 6-stelligen PIN melden Sie sich im Ladeguthaben an. Dort können Sie anschließend Ihren persönlichen Türchip direkt an einer unterstützten Ladesäule einlernen.","cta_label":"Zum Ladeguthaben","cta_url":c.get("portal_url") or "/public/ladeguthaben","notice":"Bewahren Sie Ihre PIN sicher auf. Falls das automatische Chip-Anlernen an Ihrer Ladesäule nicht unterstützt wird, wenden Sie sich bitte an die Administration.","tone":"success"}
    if template_key=="access_rejected":
        return {"subject":"Rückmeldung zu Ihrem Zugangsantrag","eyebrow":"Zugang beantragen","headline":"Ihr Antrag wurde geprüft","body":f"{('Hallo '+name+',') if name else 'Hallo,'} Ihr Antrag auf Nutzung der Ladeinfrastruktur konnte derzeit nicht genehmigt werden.","detail":str(c.get("rejection_reason") or "Für Rückfragen wenden Sie sich bitte an die Administration."),"tone":"warning"}
    return {"subject":str(c.get("subject") or "Systemmeldung"),"eyebrow":"Systemmeldung","headline":str(c.get("headline") or "Information aus dem Backend"),"body":str(c.get("body") or "Dies ist eine Test- bzw. Systemmeldung."),"detail":str(c.get("detail") or "Der zentrale E-Mail-Versand ist betriebsbereit."),"cta_label":c.get("cta_label"),"cta_url":c.get("cta_url"),"tone":str(c.get("tone") or "info")}


def render_template(template_key, context=None, base_url=None):
    if template_key not in TEMPLATE_LABELS:
        raise ValueError("Unbekannte Mailvorlage.")
    branding=db.branding_settings(); data=_mail_data(template_key,context or {})
    product=branding.get("product_name") or "VoltCore"
    organization=branding.get("organization_name") or ""
    display=branding.get("display_name") or product
    color=branding.get("primary_color") or "#2563eb"
    logo=branding.get("logo_light_url") or branding.get("logo_dark_url") or ""
    logo_abs=_absolute(base_url,logo)
    cta_url=_absolute(base_url,data.get("cta_url"))
    esc=lambda v: html.escape(str(v or ""),quote=True)
    tone_bg={"success":"#ecfdf3","warning":"#fff7ed","critical":"#fef2f2","info":"#eff6ff"}.get(data.get("tone"),"#eff6ff")
    tone_fg={"success":"#166534","warning":"#9a3412","critical":"#991b1b","info":"#1e40af"}.get(data.get("tone"),"#1e40af")
    logo_html=f'<img src="{esc(logo_abs)}" alt="" style="max-height:48px;max-width:180px;display:block;margin-bottom:18px">' if logo_abs else ""
    cta_html=f'<p style="margin:28px 0"><a href="{esc(cta_url)}" style="display:inline-block;background:{esc(color)};color:#fff;text-decoration:none;font-weight:700;padding:13px 20px;border-radius:10px">{esc(data.get("cta_label"))}</a></p>' if cta_url and data.get("cta_label") else ""
    code_html=f'<div class="mail-code" style="font-size:30px;letter-spacing:8px;font-weight:800;text-align:center;background:#f3f4f6;color:#111827;border-radius:12px;padding:18px;margin:24px 0">{esc(data.get("code"))}</div>' if data.get("code") else ""
    notice_html=f'<div style="margin-top:24px;padding:14px 16px;border-radius:10px;background:{tone_bg};color:{tone_fg};font-size:14px;line-height:1.55"><strong>Hinweis</strong><br>{esc(data.get("notice"))}</div>' if data.get("notice") else ""
    dark_style="@media (prefers-color-scheme:dark){.mail-bg{background:#111827!important}.mail-card{background:#1f2937!important;color:#f9fafb!important}.mail-footer{background:#18212f!important;border-color:#374151!important;color:#aeb8c7!important}.mail-footer strong{color:#f3f4f6!important}.mail-detail{color:#c3cad5!important}.mail-code{background:#111827!important;color:#f9fafb!important}}"
    html_body=f'''<!doctype html><html><head><meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark"><style>{dark_style}</style></head><body class="mail-bg" style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827"><div style="display:none;max-height:0;overflow:hidden">{esc(data['subject'])}</div><table class="mail-bg" role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f4f6;padding:28px 12px"><tr><td align="center"><table class="mail-card" role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 8px 30px rgba(15,23,42,.08)"><tr><td style="height:6px;background:{esc(color)}"></td></tr><tr><td style="padding:34px 38px">{logo_html}<div style="font-size:12px;text-transform:uppercase;letter-spacing:.12em;font-weight:700;color:{esc(color)}">{esc(data.get('eyebrow'))}</div><h1 style="font-size:26px;line-height:1.2;margin:8px 0 16px">{esc(data.get('headline'))}</h1><p style="font-size:16px;line-height:1.65;margin:0 0 12px">{esc(data.get('body'))}</p>{code_html}<p class="mail-detail" style="font-size:14px;line-height:1.65;color:#4b5563;margin:14px 0">{esc(data.get('detail'))}</p>{cta_html}{notice_html}</td></tr><tr><td class="mail-footer" style="background:#f9fafb;border-top:1px solid #e5e7eb;padding:20px 38px;font-size:12px;line-height:1.55;color:#6b7280"><strong style="color:#374151">{esc(display)}</strong>{('<br>'+esc(organization)) if organization and organization!=display else ''}<br>Diese Nachricht wurde automatisch erzeugt. Bitte antworten Sie nur, wenn die Absenderadresse dafür vorgesehen ist.</td></tr></table></td></tr></table></body></html>'''
    text=[data.get("headline"),"",data.get("body")]
    if data.get("code"): text += ["",f"PIN: {data['code']}"]
    if data.get("detail"): text += ["",data.get("detail")]
    if cta_url and data.get("cta_label"): text += ["",f"{data['cta_label']}: {cta_url}"]
    if data.get("notice"): text += ["","Hinweis: "+data.get("notice")]
    text += ["",display + ((" · "+organization) if organization and organization!=display else "")]
    return {"subject":data["subject"],"html":"".join(html_body),"text":"\n".join(str(x) for x in text if x is not None)}


def send_template(template_key, recipients, context=None, base_url=None, allow_disabled=False):
    cfg=settings()
    if not cfg["enabled"] and not allow_disabled:
        raise RuntimeError("E-Mail-Versand ist deaktiviert.")
    to=_recipient_list(recipients) if isinstance(recipients,str) else [_clean_email(x) for x in recipients if str(x or "").strip()]
    if not to: raise ValueError("Keine Empfängeradresse angegeben.")
    if not cfg["host"] or not cfg["from_email"]:
        raise RuntimeError("SMTP ist noch nicht vollständig konfiguriert.")
    rendered=render_template(template_key,context,base_url or cfg.get("public_base_url"))
    msg=EmailMessage()
    msg["Subject"]=rendered["subject"]
    sender_name=cfg.get("from_name") or db.branding_settings().get("display_name") or "VoltCore"
    msg["From"]=f'{sender_name} <{cfg["from_email"]}>'
    msg["To"] = ", ".join(to)
    msg.set_content(rendered["text"])
    msg.add_alternative(rendered["html"],subtype="html")
    password=_password(); timeout=15
    if cfg["security"]=="ssl":
        with smtplib.SMTP_SSL(cfg["host"],cfg["port"],timeout=timeout,context=ssl.create_default_context()) as smtp:
            if cfg["username"]: smtp.login(cfg["username"],password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"],cfg["port"],timeout=timeout) as smtp:
            smtp.ehlo()
            if cfg["security"]=="starttls":
                smtp.starttls(context=ssl.create_default_context()); smtp.ehlo()
            if cfg["username"]: smtp.login(cfg["username"],password)
            smtp.send_message(msg)
    return {"ok":True,"recipients":to,"template":template_key,"subject":rendered["subject"]}
