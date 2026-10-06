"""Small shared runtime helpers for settings and secret files.

These helpers centralize behavior that used to be duplicated in the backup and
mail modules. Secret values stay outside SQLite and are written with restrictive
permissions where the platform supports it.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import db


def ensure_setting_defaults(defaults: dict[str, str]) -> None:
    for key, value in defaults.items():
        if db.get_setting(key, None) is None:
            db.set_setting(key, value)


def bool_value(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def secret_is_configured(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def read_secret(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear_secret(path: Path) -> None:
    path.unlink(missing_ok=True)
