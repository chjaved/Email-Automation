"""Follow-up email sequence generation and caching."""
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Optional

from openai import OpenAI

from config import (
    OPENAI_API_KEY,
    OPENAI_MODEL,
    SIGNATURE_ADDRESS,
    SIGNATURE_COMPANY,
    SIGNATURE_CONFIDENTIALITY,
    SIGNATURE_EMAIL,
    SIGNATURE_NAME,
    SIGNATURE_PHONE,
    SIGNATURE_SERVICES,
    SIGNATURE_TITLE,
    SIGNATURE_WEBSITE,
    TIMEZONE,
)
from db import get_conn

logger = logging.getLogger(__name__)

client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)


STEP_NAMES = {
    1: "Follow-up 1 (A Quick Follow-Up)",
    2: "Follow-up 2 (Just a Final Note)",
    3: "Follow-up 3 (extra final note)",
}

# Approved follow-up templates (sent verbatim).
FOLLOWUP_SUBJECTS = {
    1: "A Quick Follow-Up",
    2: "Just a Final Note",
    3: "Just a Final Note",
}

FOLLOWUP_BODIES = {
    1: (
        "Hi,\n\n"
        "I just wanted to follow up on my previous email in case it got lost or overlooked.\n\n"
        "I work with sole traders and small to medium-sized businesses across Ireland, providing "
        "reliable bookkeeping, VAT, PAYE, and payroll support — all delivered remotely and "
        "tailored to transaction volume and employee numbers.\n\n"
        "If you'd like any further information or have any questions, feel free to reply to this "
        "email and I'll be happy to help. There's absolutely no obligation.\n\n"
        "You can also find more details about my services at www.atlasprobookkeeping.ie."
    ),
    2: (
        "Hi,\n\n"
        "I just wanted to send a quick final note in case my previous emails got buried.\n\n"
        "I work with sole traders and small to medium-sized businesses across Ireland, providing "
        "reliable bookkeeping, VAT, PAYE, and payroll support — all delivered remotely, with "
        "packages starting from €95 and including the full range of services.\n\n"
        "If this would be helpful for your business, feel free to reply to this email anytime — "
        "there's absolutely no obligation.\n\n"
        "You can also find more details about my services at www.atlasprobookkeeping.ie.\n\n"
        "Wishing you every success,"
    ),
    3: (
        "Hi,\n\n"
        "I just wanted to send a quick final note in case my previous emails got buried.\n\n"
        "I work with sole traders and small to medium-sized businesses across Ireland, providing "
        "reliable bookkeeping, VAT, PAYE, and payroll support — all delivered remotely, with "
        "packages starting from €95 and including the full range of services.\n\n"
        "If this would be helpful for your business, feel free to reply to this email anytime — "
        "there's absolutely no obligation.\n\n"
        "You can also find more details about my services at www.atlasprobookkeeping.ie.\n\n"
        "Wishing you every success,"
    ),
}


def _get_original_email(lead: sqlite3.Row) -> Dict[str, str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT subject, body FROM emails WHERE lead_id = ?", (lead["id"],))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"subject": row["subject"], "body": row["body"]}
    return {"subject": "", "body": ""}


def _get_cached_followup(lead_id: int, step: int) -> Optional[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT body FROM followup_emails WHERE lead_id = ? AND step = ?", (lead_id, step))
    row = cur.fetchone()
    conn.close()
    return row["body"] if row else None


def _save_followup(lead_id: int, step: int, body: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO followup_emails (lead_id, step, body, generated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(lead_id, step) DO UPDATE SET body=excluded.body, generated_at=excluded.generated_at
        """,
        (lead_id, step, body, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _call_openai(prompt: str, temperature: float = 0.7) -> str:
    if client is None:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a concise, professional email copywriter."},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=400,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.error("OpenAI follow-up generation failed: %s", e)
        raise


def _signature(lead: sqlite3.Row = None) -> str:
    """Build signature from per-user settings, falling back to config defaults."""
    sig = {
        "name": SIGNATURE_NAME,
        "title": SIGNATURE_TITLE,
        "company": SIGNATURE_COMPANY,
        "email": SIGNATURE_EMAIL,
        "phone": SIGNATURE_PHONE,
        "website": SIGNATURE_WEBSITE,
    }
    if lead is not None:
        try:
            user_id = lead["user_id"]
        except (KeyError, IndexError):
            user_id = None
        if user_id:
            try:
                from settings import get_signature
                sig = get_signature(user_id)
            except Exception:
                pass
    from generator import _signature_lines
    return (
        "\n\n"
        + _signature_lines(sig)
        + "\n\n---\n"
        "Reply 'remove' if this isn't relevant and we won't email you again."
    )


def _strip_codeblock(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.DOTALL).strip()


def _lead_ai_context(lead: sqlite3.Row) -> str:
    try:
        user_id = lead["user_id"]
    except (KeyError, IndexError):
        return ""
    if not user_id:
        return ""
    try:
        from settings import get_ai_context
        return get_ai_context(user_id) or ""
    except Exception:
        return ""


def _lead_sample_and_instructions(lead: sqlite3.Row):
    """Return (sample_email, email_instructions) for the lead's account."""
    try:
        user_id = lead["user_id"]
    except (KeyError, IndexError):
        return "", ""
    if not user_id:
        return "", ""
    try:
        from settings import get_sample_email, get_email_instructions
        return get_sample_email(user_id) or "", get_email_instructions(user_id) or ""
    except Exception:
        return "", ""


def _account_brief_block(lead: sqlite3.Row) -> str:
    ctx = _lead_ai_context(lead)
    if not ctx.strip():
        return ""
    return (
        "SENDER ACCOUNT BRIEF (who is sending — let this shape tone and value, "
        "never invent facts beyond it):\n"
        f"{ctx.strip()}\n\n"
    )


def _sample_block(lead: sqlite3.Row) -> str:
    sample, instr = _lead_sample_and_instructions(lead)
    parts = []
    if sample.strip():
        parts.append(
            "SAMPLE EMAIL TEMPLATE (the original email was based on this —\n"
            "keep follow-ups consistent in tone and structure):\n"
            f"---\n{sample.strip()[:1500]}\n---\n"
        )
    if instr.strip():
        parts.append(f"FOLLOW-UP INSTRUCTIONS:\n{instr.strip()}\n")
    return "\n".join(parts) + "\n" if parts else ""


def _generate_step(lead: sqlite3.Row, step: int) -> str:
    """Return the approved follow-up template for `step` plus signature."""
    return FOLLOWUP_BODIES[step] + _signature(lead)


def get_followup_subject(step: int, original_subject: str = "") -> str:
    """Subject line for a follow-up step."""
    return FOLLOWUP_SUBJECTS.get(step, "A Quick Follow-Up")


def get_followup_body(lead: sqlite3.Row, step: int) -> str:
    if step not in STEP_NAMES:
        raise ValueError(f"Invalid follow-up step: {step}")

    cached = _get_cached_followup(lead["id"], step)
    if cached:
        return cached

    body = _generate_step(lead, step)

    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    _save_followup(lead["id"], step, body)
    return body


def is_final(status: str) -> bool:
    return status in ("replied", "bounced", "unsubscribed", "customer")


def step_to_wait_days(step: int) -> int:
    from config import FOLLOWUP_SCHEDULE

    if step < 1 or step > len(FOLLOWUP_SCHEDULE):
        return 0
    return FOLLOWUP_SCHEDULE[step - 1]
