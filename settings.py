"""Per-user sender settings, stored in the DB `user_settings` table.

Each logged-in user configures their own SMTP account (login email + Gmail
app password) and sender identity (alias / display name). The app password
is encrypted at rest using `auth.encrypt_secret` (derived from SECRET_KEY).
Falls back to the legacy global .env values only when a user has not yet
configured anything (useful for the first admin account after migration).
"""
from typing import Optional

from auth import decrypt_secret, encrypt_secret
from config import FROM_ALIAS, FROM_DISPLAY_NAME, SMTP_PASSWORD, SMTP_USER
from db import get_conn


def _get_row(user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
        return cur.fetchone()
    finally:
        conn.close()


def get_smtp_user(user_id: int) -> str:
    row = _get_row(user_id)
    val = (row["smtp_user"] if row else "") or ""
    return (val or SMTP_USER).strip()


def get_smtp_password(user_id: int) -> str:
    row = _get_row(user_id)
    enc = (row["smtp_password_enc"] if row else "") or ""
    val = decrypt_secret(enc) if enc else ""
    return (val or SMTP_PASSWORD).strip()


def get_from_alias(user_id: int) -> str:
    row = _get_row(user_id)
    val = (row["from_alias"] if row else "") or ""
    return (val or FROM_ALIAS).strip()


def get_from_display_name(user_id: int) -> str:
    row = _get_row(user_id)
    val = (row["from_display_name"] if row else "") or ""
    return (val or FROM_DISPLAY_NAME).strip()


def get_cc_enabled(user_id: int) -> bool:
    """Whether the user wants the default CC recipients added to outgoing mail.
    Defaults to True (matches previous behaviour) if never explicitly set."""
    row = _get_row(user_id)
    if not row:
        return True
    val = row["cc_enabled"]
    if val is None:
        return True
    return bool(int(val))


def get_public_settings(user_id: int) -> dict:
    """Settings safe to return to the dashboard (never expose the raw password)."""
    return {
        "smtp_user": get_smtp_user(user_id),
        "smtp_password_set": bool(get_smtp_password(user_id)),
        "from_alias": get_from_alias(user_id),
        "from_display_name": get_from_display_name(user_id),
        "cc_enabled": get_cc_enabled(user_id),
    }


def update_settings(
    user_id: int,
    smtp_user: Optional[str] = None,
    smtp_password: Optional[str] = None,
    from_alias: Optional[str] = None,
    from_display_name: Optional[str] = None,
    cc_enabled: Optional[bool] = None,
) -> None:
    row = _get_row(user_id)
    new_smtp_user = smtp_user.strip() if smtp_user is not None else (row["smtp_user"] if row else "") or ""
    new_from_alias = from_alias.strip() if from_alias is not None else (row["from_alias"] if row else "") or ""
    new_display_name = (
        from_display_name.strip() if from_display_name is not None else (row["from_display_name"] if row else "") or ""
    )
    if cc_enabled is None:
        current_cc = row["cc_enabled"] if row else None
        new_cc = 1 if current_cc is None else int(current_cc)
    else:
        new_cc = 1 if cc_enabled else 0
    # Only overwrite the password if a new, non-empty value was provided
    # (lets the UI leave the password field blank to keep it unchanged).
    if smtp_password is not None and smtp_password.strip():
        new_smtp_password_enc = encrypt_secret(smtp_password.strip())
    else:
        new_smtp_password_enc = (row["smtp_password_enc"] if row else "") or ""

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_settings (user_id, smtp_user, smtp_password_enc, from_alias, from_display_name, cc_enabled)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                smtp_user = excluded.smtp_user,
                smtp_password_enc = excluded.smtp_password_enc,
                from_alias = excluded.from_alias,
                from_display_name = excluded.from_display_name,
                cc_enabled = excluded.cc_enabled
            """,
            (user_id, new_smtp_user, new_smtp_password_enc, new_from_alias, new_display_name, new_cc),
        )
        conn.commit()
    finally:
        conn.close()
