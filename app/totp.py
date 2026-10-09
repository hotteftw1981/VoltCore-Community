"""TOTP and recovery-code helpers for system-account two-factor authentication."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import os
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

TOTP_PERIOD = 30
TOTP_DIGITS = 6
MASTER_KEY_FILE = Path(os.getenv("DATA_DIR", "./data")) / ".totp_master_key"


def _fernet() -> Fernet:
    MASTER_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not MASTER_KEY_FILE.exists():
        key=Fernet.generate_key()
        MASTER_KEY_FILE.write_bytes(key)
        try:
            os.chmod(MASTER_KEY_FILE,0o600)
        except OSError:
            pass
    key=MASTER_KEY_FILE.read_bytes().strip()
    return Fernet(key)


def protect_secret(secret: str) -> str:
    value=str(secret or "").strip()
    if not value:
        return ""
    return "fernet:" + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def unprotect_secret(value: str) -> str:
    text=str(value or "")
    if not text:
        return ""
    if not text.startswith("fernet:"):
        return text
    try:
        return _fernet().decrypt(text[7:].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, OSError) as exc:
        raise ValueError("TOTP-Secret konnte nicht entschlüsselt werden.") from exc


def generate_secret(byte_length: int = 20) -> str:
    raw = secrets.token_bytes(max(16, int(byte_length)))
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    value = str(secret or "").strip().replace(" ", "").upper()
    if not value:
        raise ValueError("TOTP-Secret fehlt.")
    padding = "=" * ((8 - (len(value) % 8)) % 8)
    return base64.b32decode(value + padding, casefold=True)


def counter_for_time(at_time: float | int | None = None) -> int:
    ts = time.time() if at_time is None else float(at_time)
    return int(ts // TOTP_PERIOD)


def code_for_counter(secret: str, counter: int) -> str:
    key = _decode_secret(secret)
    msg = struct.pack(">Q", int(counter))
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = (
        ((digest[offset] & 0x7F) << 24)
        | (digest[offset + 1] << 16)
        | (digest[offset + 2] << 8)
        | digest[offset + 3]
    )
    return str(binary % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def current_code(secret: str, at_time: float | int | None = None) -> str:
    return code_for_counter(secret, counter_for_time(at_time))


def verify_code(secret: str, code: str, at_time: float | int | None = None, window: int = 1) -> int | None:
    candidate = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(candidate) != TOTP_DIGITS:
        return None
    current = counter_for_time(at_time)
    offsets = [0]
    for step in range(1, max(0, int(window)) + 1):
        offsets.extend((-step, step))
    for offset in offsets:
        counter = current + offset
        if counter < 0:
            continue
        if hmac.compare_digest(code_for_counter(secret, counter), candidate):
            return counter
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    issuer_text = str(issuer or "VoltCore").strip() or "VoltCore"
    account_text = str(account or "Benutzer").strip() or "Benutzer"
    label = quote(f"{issuer_text}:{account_text}", safe="")
    return (
        f"otpauth://totp/{label}"
        f"?secret={quote(str(secret), safe='')}"
        f"&issuer={quote(issuer_text, safe='')}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"
    )


def generate_recovery_codes(count: int = 8) -> list[str]:
    result = []
    for _ in range(max(1, int(count))):
        raw = secrets.token_hex(8).upper()
        result.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}")
    return result


def normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def recovery_code_hash(code: str) -> str:
    normalized = normalize_recovery_code(code)
    return hashlib.sha256(normalized.encode("ascii", "ignore")).hexdigest()
