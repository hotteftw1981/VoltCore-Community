"""Small, dependency-free internationalisation helper for VoltCore Community."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

BASE_DIR = Path(__file__).resolve().parent
LOCALES_DIR = BASE_DIR / "locales"

SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "de": "Deutsch",
}
DEFAULT_LANGUAGE = "en"


def normalize_language(value: str | None) -> str | None:
    """Return a supported two-letter language code or None."""
    text = str(value or "").strip().replace("_", "-").lower()
    if not text:
        return None
    code = text.split("-", 1)[0]
    return code if code in SUPPORTED_LANGUAGES else None


def language_from_accept_header(header: str | None) -> str | None:
    """Resolve the best supported language from an Accept-Language header."""
    candidates: list[tuple[float, int, str]] = []
    for index, raw in enumerate(str(header or "").split(",")):
        part = raw.strip()
        if not part:
            continue
        pieces = [x.strip() for x in part.split(";") if x.strip()]
        language = normalize_language(pieces[0])
        if not language:
            continue
        quality = 1.0
        for parameter in pieces[1:]:
            if parameter.lower().startswith("q="):
                try:
                    quality = float(parameter.split("=", 1)[1])
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        candidates.append((quality, -index, language))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def resolve_language(
    explicit: str | None = None,
    accept_language: str | None = None,
) -> str:
    """Manual choice wins, then browser language, then English fallback."""
    return (
        normalize_language(explicit)
        or language_from_accept_header(accept_language)
        or DEFAULT_LANGUAGE
    )


@lru_cache(maxsize=None)
def _load_locale(language: str) -> Mapping[str, Any]:
    code = normalize_language(language) or DEFAULT_LANGUAGE
    path = LOCALES_DIR / f"{code}.json"
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Locale {code} must contain a JSON object.")
    return payload


def _lookup(payload: Mapping[str, Any], key: str) -> Any:
    value: Any = payload
    for segment in str(key or "").split("."):
        if not segment or not isinstance(value, Mapping) or segment not in value:
            return None
        value = value[segment]
    return value


def translate(key: str, language: str | None = None, **values: Any) -> str:
    """Translate a dotted key and safely fall back to English, then the key."""
    code = normalize_language(language) or DEFAULT_LANGUAGE
    value = _lookup(_load_locale(code), key)
    if value is None and code != DEFAULT_LANGUAGE:
        value = _lookup(_load_locale(DEFAULT_LANGUAGE), key)
    text = str(value if value is not None else key)
    if values:
        try:
            text = text.format(**values)
        except (KeyError, ValueError, IndexError):
            pass
    return text


def translator(language: str | None = None):
    """Return a Jinja-friendly translation callable bound to one language."""
    code = normalize_language(language) or DEFAULT_LANGUAGE

    def _t(key: str, **values: Any) -> str:
        return translate(key, code, **values)

    return _t


def clear_locale_cache() -> None:
    """Useful for tests and future runtime locale refreshes."""
    _load_locale.cache_clear()
