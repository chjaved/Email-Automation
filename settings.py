"""Per-user sender settings, stored in the DB `user_settings` table.

Each logged-in user configures their own SMTP account (login email + Gmail
app password) and sender identity (alias / display name). The app password
is encrypted at rest using `auth.encrypt_secret` (derived from SECRET_KEY).
Falls back to the legacy global .env values only when a user has not yet
configured anything (useful for the first admin account after migration).
"""
from typing import Optional, Tuple

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


def get_ai_context(user_id: int) -> str:
    """Free-form per-account context (business, offering, tone) that the AI
    uses when drafting emails. Empty string when unset (AI falls back to the
    generic recruitment pitch built into `generator.py`)."""
    row = _get_row(user_id)
    return (row["ai_context"] if row and row["ai_context"] else "") or ""


def get_sample_email(user_id: int) -> str:
    """Exact email template the user wants the AI to follow closely, only
    tweaking small things per company. Empty when unset."""
    row = _get_row(user_id)
    return (row["sample_email"] if row and row["sample_email"] else "") or ""


def get_email_instructions(user_id: int) -> str:
    """Per-account instructions: what to change per company, what to leave
    untouched. Empty when unset."""
    row = _get_row(user_id)
    return (row["email_instructions"] if row and row["email_instructions"] else "") or ""


def get_attachment(user_id: int) -> Optional[Tuple[bytes, str, str]]:
    """Return the user's uploaded attachment as (bytes, filename, mime) or None."""
    row = _get_row(user_id)
    if not row:
        return None
    data = row["attachment_bytes"]
    if data is None:
        return None
    # Postgres returns memoryview for BYTEA; normalise to bytes
    if not isinstance(data, (bytes, bytearray)):
        try:
            data = bytes(data)
        except Exception:
            return None
    if not data:
        return None
    name = (row["attachment_name"] or "attachment.bin")
    mime = (row["attachment_mime"] or "application/octet-stream")
    return bytes(data), name, mime


def set_attachment(user_id: int, data: bytes, name: str, mime: str) -> None:
    _ensure_row(user_id)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_settings SET attachment_bytes = ?, attachment_name = ?, attachment_mime = ? WHERE user_id = ?",
            (data, name, mime, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def clear_attachment(user_id: int) -> None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_settings SET attachment_bytes = NULL, attachment_name = NULL, attachment_mime = NULL WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Multi-attachment support (user_attachments table)
# ---------------------------------------------------------------------------
def list_attachments(user_id: int) -> list:
    """Return list of {id, filename, mime, size} for all attachments."""
    from datetime import datetime, timezone
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, filename, mime, data FROM user_attachments WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            data = r["data"]
            size = 0
            if data is not None:
                if not isinstance(data, (bytes, bytearray)):
                    try:
                        data = bytes(data)
                    except Exception:
                        data = b""
                size = len(data)
            result.append({
                "id": r["id"],
                "filename": r["filename"],
                "mime": r["mime"] or "application/octet-stream",
                "size": size,
            })
        return result
    finally:
        conn.close()


def add_attachment(user_id: int, data: bytes, name: str, mime: str) -> int:
    """Insert a new attachment row and return its id."""
    from datetime import datetime, timezone
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO user_attachments (user_id, filename, mime, data, uploaded_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, mime, data, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_attachment_by_id(attach_id: int, user_id: int) -> Optional[Tuple[bytes, str, str]]:
    """Return (bytes, filename, mime) for a specific attachment, or None."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT filename, mime, data FROM user_attachments WHERE id = ? AND user_id = ?",
            (attach_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        data = row["data"]
        if not isinstance(data, (bytes, bytearray)):
            try:
                data = bytes(data)
            except Exception:
                return None
        return bytes(data), row["filename"], row["mime"] or "application/octet-stream"
    finally:
        conn.close()


def delete_attachment(attach_id: int, user_id: int) -> bool:
    """Delete a specific attachment. Returns True if a row was deleted."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_attachments WHERE id = ? AND user_id = ?",
            (attach_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_all_attachments(user_id: int) -> list:
    """Return list of (bytes, filename, mime) for all user attachments.
    Used by sender.py to attach all files to outgoing emails."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT filename, mime, data FROM user_attachments WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            data = r["data"]
            if not isinstance(data, (bytes, bytearray)):
                try:
                    data = bytes(data)
                except Exception:
                    continue
            if data:
                result.append((bytes(data), r["filename"], r["mime"] or "application/octet-stream"))
        return result
    finally:
        conn.close()


def migrate_legacy_attachment(user_id: int) -> None:
    """If the user has an old single attachment in user_settings, move it
    to the user_attachments table and clear the legacy columns."""
    legacy = get_attachment(user_id)
    if legacy is None:
        return
    data, name, mime = legacy
    add_attachment(user_id, data, name, mime)
    clear_attachment(user_id)


def _ensure_row(user_id: int) -> None:
    """Guarantee a user_settings row exists so UPDATEs land somewhere."""
    if _get_row(user_id):
        return
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO user_settings (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()


def invalidate_generated_emails(user_id: int) -> None:
    """Delete cached AI-generated emails + follow-ups for all leads owned by
    this user. Called when the user changes their AI writing context so the
    next send picks up the new brief."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM emails WHERE lead_id IN (SELECT id FROM leads WHERE user_id = ?)",
            (user_id,),
        )
        cur.execute(
            "DELETE FROM followup_emails WHERE lead_id IN (SELECT id FROM leads WHERE user_id = ?)",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()


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
    row = _get_row(user_id)
    # Migrate legacy single attachment to multi-attachment table if needed
    if row and row["attachment_bytes"] is not None:
        try:
            migrate_legacy_attachment(user_id)
        except Exception:
            pass
    attachments = list_attachments(user_id)
    return {
        "smtp_user": get_smtp_user(user_id),
        "smtp_password_set": bool(get_smtp_password(user_id)),
        "from_alias": get_from_alias(user_id),
        "from_display_name": get_from_display_name(user_id),
        "cc_enabled": get_cc_enabled(user_id),
        "ai_context": get_ai_context(user_id),
        "sample_email": get_sample_email(user_id),
        "email_instructions": get_email_instructions(user_id),
        "attachments": attachments,
        "has_attachment": len(attachments) > 0,
    }


def update_settings(
    user_id: int,
    smtp_user: Optional[str] = None,
    smtp_password: Optional[str] = None,
    from_alias: Optional[str] = None,
    from_display_name: Optional[str] = None,
    cc_enabled: Optional[bool] = None,
    ai_context: Optional[str] = None,
    sample_email: Optional[str] = None,
    email_instructions: Optional[str] = None,
) -> bool:
    """Upsert per-user settings. Returns True if any AI-relevant field
    (ai_context, sample_email, email_instructions) was actually changed
    (caller should then invalidate cached AI emails)."""
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

    current_ai = (row["ai_context"] if row and row["ai_context"] else "") or ""
    if ai_context is None:
        new_ai = current_ai
    else:
        new_ai = ai_context.strip()
    ai_changed = new_ai != current_ai

    current_sample = (row["sample_email"] if row and row["sample_email"] else "") or ""
    if sample_email is None:
        new_sample = current_sample
    else:
        new_sample = sample_email.strip()
    sample_changed = new_sample != current_sample

    current_instr = (row["email_instructions"] if row and row["email_instructions"] else "") or ""
    if email_instructions is None:
        new_instr = current_instr
    else:
        new_instr = email_instructions.strip()
    instr_changed = new_instr != current_instr

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
            INSERT INTO user_settings (user_id, smtp_user, smtp_password_enc, from_alias, from_display_name, cc_enabled, ai_context, sample_email, email_instructions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                smtp_user = excluded.smtp_user,
                smtp_password_enc = excluded.smtp_password_enc,
                from_alias = excluded.from_alias,
                from_display_name = excluded.from_display_name,
                cc_enabled = excluded.cc_enabled,
                ai_context = excluded.ai_context,
                sample_email = excluded.sample_email,
                email_instructions = excluded.email_instructions
            """,
            (user_id, new_smtp_user, new_smtp_password_enc, new_from_alias, new_display_name, new_cc, new_ai, new_sample, new_instr),
        )
        conn.commit()
    finally:
        conn.close()
    return ai_changed or sample_changed or instr_changed
