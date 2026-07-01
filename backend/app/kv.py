"""Tiny key/value settings store (the Setting table), for values set in the admin UI."""

from __future__ import annotations

from .db import get_session
from .models import Setting


def get_setting(key: str, default: str | None = None) -> str | None:
    with get_session() as s:
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    with get_session() as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            row = Setting(key=key, value=value)
        s.add(row)
        s.commit()


def get_setting_float(key: str, default: float) -> float:
    """Fetch a setting as a float, falling back to default for missing/blank/invalid values."""
    val = get_setting(key)
    try:
        return float(val) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


def get_setting_int(key: str, default: int) -> int:
    """Fetch a setting as an int, falling back to default for missing/blank/invalid values."""
    val = get_setting(key)
    try:
        return int(float(val)) if val not in (None, "") else default
    except (TypeError, ValueError):
        return default


def get_timezone_name() -> str:
    """Effective IANA timezone: the admin-set value, else the env/config default."""
    from .config import get_settings

    return get_setting("timezone") or get_settings().timezone or "America/Chicago"


def get_zoneinfo():
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(get_timezone_name())
    except Exception:  # noqa: BLE001 — bad/unknown tz name -> safe fallback
        return ZoneInfo("America/Chicago")
