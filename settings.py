"""Runtime-configurable sender settings, stored in the DB `state` table.

These override the corresponding .env / Railway variables at runtime,
without requiring a redeploy. If a setting has never been configured via
the dashboard, the .env value is used as a fallback.
"""
from typing import Optional

from config import FROM_ALIAS, FROM_DISPLAY_NAME, SMTP_PASSWORD, SMTP_USER
from db import get_conn

_PREFIX = "setting:"

# Keys exposed to the dashboard's Settings tab.
SMTP_USER_KEY = "smtp_user"
SMTP_PASSWORD_KEY = "smtp_password"
FROM_ALIAS_KEY = "from_alias"
FROM_DISPLAY_NAME_KEY = "from_display_name"


def get_setting(key: str) -> str:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM state WHERE key = ?", (_PREFIX + key,))
    row = cur.fetchone()
    conn.close()
    return (row["value"] or "") if row else ""


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_PREFIX + key, value or ""),
    )
    conn.commit()
    conn.close()


def get_smtp_user() -> str:
    return (get_setting(SMTP_USER_KEY) or SMTP_USER).strip()


def get_smtp_password() -> str:
    return (get_setting(SMTP_PASSWORD_KEY) or SMTP_PASSWORD).strip()


def get_from_alias() -> str:
    return (get_setting(FROM_ALIAS_KEY) or FROM_ALIAS).strip()


def get_from_display_name() -> str:
    return (get_setting(FROM_DISPLAY_NAME_KEY) or FROM_DISPLAY_NAME).strip()


def get_public_settings() -> dict:
    """Settings safe to return to the dashboard (never expose the raw password)."""
    return {
        "smtp_user": get_smtp_user(),
        "smtp_password_set": bool(get_smtp_password()),
        "from_alias": get_from_alias(),
        "from_display_name": get_from_display_name(),
    }


def update_settings(
    smtp_user: Optional[str] = None,
    smtp_password: Optional[str] = None,
    from_alias: Optional[str] = None,
    from_display_name: Optional[str] = None,
) -> None:
    if smtp_user is not None:
        set_setting(SMTP_USER_KEY, smtp_user.strip())
    if smtp_password is not None and smtp_password.strip():
        # Only overwrite if a new, non-empty value was actually provided
        # (lets the UI leave the password field blank to keep it unchanged).
        set_setting(SMTP_PASSWORD_KEY, smtp_password.strip())
    if from_alias is not None:
        set_setting(FROM_ALIAS_KEY, from_alias.strip())
    if from_display_name is not None:
        set_setting(FROM_DISPLAY_NAME_KEY, from_display_name.strip())
