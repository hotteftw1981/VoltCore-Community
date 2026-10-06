"""Invitation storage for VoltCore Community.

Invitations are intentionally token-hash based: only the one-time raw token that
is sent to the recipient can open the invitation. The database stores the
SHA-256 hash, never the usable token itself.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone, timedelta

from . import db

INVITE_TTL_HOURS = 72


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_schema() -> None:
    with db._lock, db._connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS community_user_invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                display_name TEXT,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                accepted_at TEXT,
                created_by INTEGER
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_community_user_invites_email "
            "ON community_user_invites(LOWER(email), created_at DESC)"
        )
        conn.commit()


def create_invite(email: str, display_name: str | None = None, created_by: int | None = None,
                  ttl_hours: int = INVITE_TTL_HOURS) -> dict:
    ensure_schema()
    address=str(email or "").strip().lower()
    if not address or "@" not in address:
        raise ValueError("Eine gültige E-Mail-Adresse ist erforderlich.")

    now=_now()
    expires=now + timedelta(hours=max(1, int(ttl_hours)))
    token=secrets.token_urlsafe(32)
    token_hash=_hash_token(token)

    with db._lock, db._connect() as conn:
        # A new invitation supersedes older unused invitations for the same address.
        conn.execute(
            "UPDATE community_user_invites SET expires_at=? "
            "WHERE LOWER(email)=? AND accepted_at IS NULL AND expires_at>?",
            (now.isoformat(), address, now.isoformat()),
        )
        cur=conn.execute(
            """INSERT INTO community_user_invites
               (email,display_name,token_hash,created_at,expires_at,created_by)
               VALUES(?,?,?,?,?,?)""",
            (
                address,
                str(display_name or "").strip()[:120] or None,
                token_hash,
                now.isoformat(),
                expires.isoformat(),
                int(created_by) if created_by is not None else None,
            ),
        )
        conn.commit()
        invite_id=int(cur.lastrowid)

    return {
        "id": invite_id,
        "email": address,
        "display_name": str(display_name or "").strip()[:120] or None,
        "token": token,
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }


def get_invite(token: str) -> dict | None:
    ensure_schema()
    token_hash=_hash_token(token)
    now=_now().isoformat()
    with db._connect() as conn:
        row=conn.execute(
            """SELECT id,email,display_name,created_at,expires_at,accepted_at,created_by
               FROM community_user_invites
               WHERE token_hash=? AND accepted_at IS NULL AND expires_at>? LIMIT 1""",
            (token_hash, now),
        ).fetchone()
        return dict(row) if row else None


def accept_invite(token: str) -> dict | None:
    """Atomically consume an invitation and return it once."""
    ensure_schema()
    token_hash=_hash_token(token)
    now=_now().isoformat()
    with db._lock, db._connect() as conn:
        row=conn.execute(
            """SELECT id,email,display_name,created_at,expires_at,accepted_at,created_by
               FROM community_user_invites
               WHERE token_hash=? AND accepted_at IS NULL AND expires_at>? LIMIT 1""",
            (token_hash, now),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE community_user_invites SET accepted_at=? "
            "WHERE id=? AND accepted_at IS NULL",
            (now, int(row["id"])),
        )
        conn.commit()
        result=dict(row)
        result["accepted_at"]=now
        return result


def list_invites(limit: int = 100) -> list[dict]:
    ensure_schema()
    with db._connect() as conn:
        rows=conn.execute(
            """SELECT id,email,display_name,created_at,expires_at,accepted_at,created_by
               FROM community_user_invites
               ORDER BY created_at DESC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(row) for row in rows]
