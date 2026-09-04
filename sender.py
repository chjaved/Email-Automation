"""SMTP sender + bounce/reply detection.

Sends via Gmail SMTP with an authenticated `SMTP_USER` and (optionally) a
different `FROM_ALIAS` address that must be a registered "send-as" alias
on the same Gmail account.
"""
import base64
import email.utils
import html as html_lib
import logging
import mimetypes
import random
import re
import smtplib
import sqlite3
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from config import (
    BOUNCE_PAUSE_THRESHOLD,
    BOUNCE_RATE_DAYS,
    BOUNCE_RATE_WINDOW,
    FOLLOWUP_SCHEDULE,
    MAILBOX_POOL,
    SEND_INTERVAL_SECONDS,
    TIMEZONE,
)
from db import get_conn, log_event
from mailboxes import (
    get_next_mailbox,
    get_total_daily_cap,
    send_message as _send_mailbox_message,
    send_test_email as _send_mailbox_test,
)
from settings import get_daily_send_cap, get_smtp_password, get_smtp_user
from followups import get_followup_body, is_final
from generator import generate_for_lead
from leads import add_do_not_email, is_do_not_email

logger = logging.getLogger(__name__)

# googleapiclient logs a WARNING for every retried 403/429; num_retries handles
# them so silence the noise.
logging.getLogger("googleapiclient.http").setLevel(logging.ERROR)

REMOVE_KEYWORDS = [
    "remove",
    "unsubscribe",
    "stop",
    "opt out",
    "do not email",
    "don't email",
    "remove me",
]


# ---------------------------------------------------------------------------
# Sending co-ordination
# ---------------------------------------------------------------------------
# Per-mailbox OAuth, message construction, and Gmail API dispatch are in
# mailboxes.py. This file now only drives the send loop and state updates.



# ---------------------------------------------------------------------------
# Lead status + state (unchanged behaviour)
# ---------------------------------------------------------------------------
def set_lead_status(lead_id: int, status: str, **fields: Any) -> None:
    conn = get_conn()
    cur = conn.cursor()

    columns = ["status = ?"]
    values: List[Any] = [status]
    for k, v in fields.items():
        columns.append(f"{k} = ?")
        values.append(v)

    values.append(lead_id)
    cur.execute(f"UPDATE leads SET {', '.join(columns)} WHERE id = ?", values)
    conn.commit()
    conn.close()


def _state_key(user_id: int, key: str) -> str:
    return f"user:{user_id}:{key}"


def _get_state(key: str) -> Optional[str]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM state WHERE key = ?", (key,))
        row = cur.fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def _set_state(key: str, value: str) -> None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def is_paused(user_id: int) -> bool:
    return _get_state(_state_key(user_id, "paused")) == "1"


def pause_campaign(user_id: int, reason: str) -> None:
    logger.warning("CAMPAIGN PAUSED for user %s: %s", user_id, reason)
    _set_state(_state_key(user_id, "paused"), "1")


def reset_pause(user_id: Optional[int] = None) -> None:
    if user_id is None:
        for uid in _all_user_ids():
            _set_state(_state_key(uid, "paused"), "0")
        return
    _set_state(_state_key(user_id, "paused"), "0")


def _trailing_bounce_rate(user_id: int) -> float:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cutoff = (
            datetime.now(ZoneInfo(TIMEZONE)) - _timedelta(days=BOUNCE_RATE_DAYS)
        ).isoformat()
        cur.execute(
            "SELECT status FROM leads WHERE sent_at IS NOT NULL AND user_id = ? "
            "AND sent_at > ? ORDER BY sent_at DESC LIMIT ?",
            (user_id, cutoff, BOUNCE_RATE_WINDOW),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return 0.0
    bounces = sum(1 for r in rows if r["status"] in ("bounced",))
    return bounces / len(rows)


def check_bounce_rate_safety(user_id: int) -> bool:
    rate = _trailing_bounce_rate(user_id)
    logger.info("Trailing bounce rate for user %s: %.2f%%", user_id, rate * 100)
    if rate > BOUNCE_PAUSE_THRESHOLD:
        pause_campaign(
            user_id,
            f"Bounce rate over last {BOUNCE_RATE_WINDOW} sends is {rate:.2%}, "
            f"exceeding {BOUNCE_PAUSE_THRESHOLD:.2%}.",
        )
        return False
    return True


# ---------------------------------------------------------------------------
# IMAP bounce/reply detection
# ---------------------------------------------------------------------------
def _fetch_recent_inbox_messages(user_id: int, days: int = 7) -> List[Dict[str, Any]]:
    """Return a list of {"from": str, "subject": str, "body": str} dicts from
    the last `days` days. Uses IMAP with the same SMTP credentials.
    Returns an empty list on any failure (bounce detection is best-effort)."""
    import imaplib
    import email as email_mod

    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        imap.login(get_smtp_user(user_id), get_smtp_password(user_id))
        imap.select("INBOX", readonly=True)
        since_date = (datetime.now() - _timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = imap.search(None, f'(SINCE "{since_date}")')
        if typ != "OK":
            imap.logout()
            return []
        results: List[Dict[str, Any]] = []
        for num in data[0].split()[-200:]:  # cap
            typ, msg_data = imap.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            raw = msg_data[0][1]
            parsed = email_mod.message_from_bytes(raw)
            body = ""
            if parsed.is_multipart():
                for part in parsed.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True) or b""
                        body += payload.decode(
                            part.get_content_charset() or "utf-8", errors="ignore"
                        )
            else:
                payload = parsed.get_payload(decode=True) or b""
                body = payload.decode(
                    parsed.get_content_charset() or "utf-8", errors="ignore"
                )
            results.append(
                {
                    "from": (parsed.get("From") or "").lower(),
                    "subject": parsed.get("Subject") or "",
                    "body": body,
                }
            )
        imap.logout()
        return results
    except Exception as e:
        logger.warning("IMAP fetch failed: %s", e)
        return []


def _timedelta(days: int):
    from datetime import timedelta

    return timedelta(days=days)


def _extract_bounce_reason(body: str) -> str:
    """Pull a short, human-readable reason out of a bounce (DSN) message body.

    Looks for common patterns like an SMTP status line ("552 ... mailbox not
    found") or a diagnostic-code header, falling back to the first
    non-empty line of the message.
    """
    if not body:
        return "Unknown bounce reason"

    import re as _re

    # e.g. "550 5.1.1 The email account that you tried to reach does not exist"
    smtp_match = _re.search(r"\b([45]\d{2}[ -][\d.]+.{0,160})", body)
    if smtp_match:
        return " ".join(smtp_match.group(1).split())[:300]

    # e.g. "Diagnostic-Code: smtp; 550 5.1.1 ... "
    diag_match = _re.search(r"Diagnostic-Code:\s*(.+)", body, _re.IGNORECASE)
    if diag_match:
        return " ".join(diag_match.group(1).split())[:300]

    # e.g. "Action: failed" / "Status: 5.1.1" lines commonly precede the reason
    status_match = _re.search(r"Status:\s*([\d.]+.{0,160})", body, _re.IGNORECASE)
    if status_match:
        return " ".join(status_match.group(1).split())[:300]

    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith(("--", "Content-", "MIME-")):
            return line[:300]

    return "Unknown bounce reason"


def detect_bounces_and_replies(user_id: int, from_email: str) -> None:
    """Very lightweight inbox scan: mark leads as bounced/replied/unsubscribed."""
    messages = _fetch_recent_inbox_messages(user_id, days=7)
    if not messages:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, email, status FROM leads WHERE sent_at IS NOT NULL AND user_id = ? "
        "AND status NOT IN ('replied','unsubscribed','bounced')",
        (user_id,),
    )
    sent_leads = {row["email"].lower(): row["id"] for row in cur.fetchall()}
    conn.close()

    from_email_lower = from_email.lower()
    for m in messages:
        sender = m["from"]
        if "mailer-daemon" in sender or "postmaster" in sender:
            # try to extract the failed recipient from body
            import re as _re

            found = _re.findall(
                r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", m["body"]
            )
            reason = _extract_bounce_reason(m["body"])
            for addr in found:
                if addr.lower() == from_email_lower or addr.lower() == get_smtp_user(user_id).lower():
                    continue
                lead_id = sent_leads.get(addr.lower())
                if lead_id:
                    set_lead_status(lead_id, "bounced", bounce_reason=reason)
                    log_event(lead_id, "bounced", reason)
                    logger.info("Marked lead %s as bounced (%s): %s", lead_id, addr, reason)
            continue

        # Reply from an actual recipient
        for lead_email, lead_id in list(sent_leads.items()):
            if lead_email in sender:
                snippet = (m["body"] or "")[:500].strip()
                if any(k in m["body"].lower() for k in REMOVE_KEYWORDS):
                    add_do_not_email(lead_email)
                    set_lead_status(
                        lead_id,
                        "unsubscribed",
                        reply_snippet=snippet,
                        last_contact_at=datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
                    )
                    log_event(lead_id, "unsubscribed", snippet)
                    logger.info("Lead %s (%s) unsubscribed", lead_id, lead_email)
                else:
                    set_lead_status(
                        lead_id,
                        "replied",
                        reply_snippet=snippet,
                        last_contact_at=datetime.now(ZoneInfo(TIMEZONE)).isoformat(),
                    )
                    log_event(lead_id, "replied", snippet)
                    logger.info("Lead %s (%s) replied", lead_id, lead_email)
                sent_leads.pop(lead_email, None)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def _due_leads(user_id: int) -> List[sqlite3.Row]:
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM leads WHERE status = 'scheduled' AND scheduled_at IS NOT NULL "
        "AND scheduled_at <= ? AND user_id = ? ORDER BY scheduled_at",
        (now, user_id),
    )
    rows = cur.fetchall()
    conn.close()

    # Round-robin by industry so mailboxes don't dump one industry in a row.
    from collections import defaultdict
    groups: Dict[str, List[Any]] = defaultdict(list)
    for r in rows:
        key = (r["industry"] or "other").lower()
        groups[key].append(r)
    industry_keys = list(groups.keys())
    random.shuffle(industry_keys)
    interleaved: List[Any] = []
    while any(groups[k] for k in industry_keys):
        for k in industry_keys:
            if groups[k]:
                interleaved.append(groups[k].pop(0))
    return interleaved


def send_due(user_id: int) -> int:
    from settings import get_send_gap_min, get_send_gap_max

    due = _due_leads(user_id)
    if not due:
        return 0

    gap_min = get_send_gap_min(user_id)
    gap_max = get_send_gap_max(user_id)

    conn = get_conn()
    cur = conn.cursor()
    sent_count = 0
    for lead in due:
        if is_paused(user_id):
            logger.warning("Campaign is paused; stopping send loop.")
            break

        if is_do_not_email(lead["email"].split()[0] if lead["email"] else ""):
            logger.info("Skipping %s: in do_not_email.csv", lead["email"])
            set_lead_status(lead["id"], "unsubscribed")
            continue

        if is_final(lead["status"]):
            continue

        if not check_bounce_rate_safety(user_id):
            break

        mailbox = get_next_mailbox(conn)
        if mailbox is None:
            logger.info("All mailboxes at daily cap. Stopping.")
            break

        step = lead["sequence_step"] or 0
        in_reply_to = lead["gmail_message_id_header"] or ""
        thread_id = lead["gmail_thread_id"] or ""

        try:
            result = _send_mailbox_message(lead, mailbox)
            if result:
                now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
                next_step = step + 1

                if step == 0:
                    log_event(lead["id"], "sent", mailbox=mailbox["name"])
                else:
                    log_event(
                        lead["id"],
                        f"followup_{step}",
                        f"Step {step} follow-up",
                        mailbox=mailbox["name"],
                    )

                if step == len(FOLLOWUP_SCHEDULE):
                    new_status = "completed"
                    next_step = step + 1
                else:
                    new_status = "sent"

                set_lead_status(
                    lead["id"],
                    new_status,
                    sequence_step=next_step,
                    last_contact_at=now,
                    sent_at=now,
                    gmail_message_id=result["message_id"],
                    gmail_thread_id=result["thread_id"],
                    gmail_message_id_header=result.get("message_id_header", ""),
                    scheduled_at=None,
                    sent_from_mailbox=mailbox["name"],
                )
                sent_count += 1
                logger.info(
                    "Marked lead %s as %s (step %d) via mailbox %s",
                    lead["id"],
                    new_status,
                    step,
                    mailbox["name"],
                )
        except Exception as e:
            reason = _classify_send_failure(e)
            if reason:
                try:
                    set_lead_status(lead["id"], "bounced", bounce_reason=reason)
                    log_event(lead["id"], "bounced", reason, mailbox=mailbox["name"])
                    logger.warning(
                        "Marked lead %s as bounced (invalid recipient): %s",
                        lead["id"], reason,
                    )
                except Exception:
                    logger.exception("Failed to mark lead %s bounced after send error", lead["id"])
            else:
                logger.exception("Send failed for lead %s", lead["id"])

        # Randomized delay between sends to avoid spam detection
        if sent_count < len(due):
            delay = random.randint(gap_min, gap_max)
            logger.info("Waiting %ds before next send (user %s)", delay, user_id)
            time.sleep(delay)

    conn.close()
    return sent_count


def _classify_send_failure(exc: Exception) -> Optional[str]:
    """Return a short reason string if the exception represents an invalid
    recipient / cc address, else None. Used to mark the lead as bounced
    immediately so we don't retry a permanently broken address."""
    from mailboxes import InvalidRecipientError
    try:
        from googleapiclient.errors import HttpError
    except Exception:
        HttpError = None  # type: ignore

    if isinstance(exc, InvalidRecipientError):
        return f"Invalid email address: {exc}"
    if HttpError is not None and isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        text = str(exc)
        if status == 400 and ("Invalid To header" in text or "Invalid Cc header" in text
                              or "Invalid From header" in text):
            return "Gmail rejected recipient address as invalid"
    return None


def _should_check_inbox(user_id: int) -> bool:
    last = _get_state(_state_key(user_id, "last_inbox_check"))
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now(ZoneInfo(TIMEZONE)) - last_dt).total_seconds() >= 300
    except Exception:
        return True


def _check_inbox(user_id: int, from_email: str) -> None:
    detect_bounces_and_replies(user_id, from_email)
    try:
        detect_bounces_gmail_api(user_id)
    except Exception:
        logger.exception("Gmail bounce scan failed for user %s", user_id)
    _set_state(_state_key(user_id, "last_inbox_check"), datetime.now(ZoneInfo(TIMEZONE)).isoformat())


def detect_bounces_gmail_api(user_id: int) -> int:
    """Scan each active mailbox via the Gmail API for delivery-failure notices
    and mark the matching leads as bounced. Returns the number of leads marked.
    """
    from mailboxes import _gmail_service

    marked = 0
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, email FROM leads WHERE sent_at IS NOT NULL AND user_id = ? "
            "AND status NOT IN ('replied','unsubscribed','bounced')",
            (user_id,),
        )
        # A lead's `email` column can contain multiple whitespace- or
        # comma-separated addresses (primary + cc). Index every individual
        # address so DSN lookups (which report one address at a time) hit.
        sent_leads: Dict[str, int] = {}
        for row in cur.fetchall():
            raw = (row["email"] or "").strip()
            if not raw:
                continue
            for part in re.split(r"[\s,;]+", raw):
                addr = part.strip().lower()
                if addr and "@" in addr:
                    sent_leads.setdefault(addr, row["id"])
    finally:
        conn.close()
    if not sent_leads:
        return 0

    query = (
        "("
        "from:mailer-daemon OR from:postmaster OR from:mail-daemon "
        "OR subject:\"Delivery Status Notification\" "
        "OR subject:\"Address not found\" "
        "OR subject:\"Undelivered Mail\" "
        "OR subject:\"Undeliverable\" "
        "OR subject:\"Mail delivery failed\" "
        "OR subject:\"Returned mail\" "
        "OR subject:\"failure notice\" "
        "OR subject:\"DNS Error\" "
        "OR subject:\"could not be delivered\""
        ") newer_than:30d"
    )
    email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    for mailbox in MAILBOX_POOL:
        if not mailbox.get("active"):
            continue
        try:
            service = _gmail_service(mailbox)
            # Paginate to cover large numbers of DSNs.
            messages: List[Dict[str, Any]] = []
            page_token = None
            while True:
                kwargs = {"userId": "me", "q": query, "maxResults": 500}
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = service.users().messages().list(**kwargs).execute(num_retries=5)
                messages.extend(resp.get("messages", []) or [])
                page_token = resp.get("nextPageToken")
                if not page_token or len(messages) >= 5000:
                    break
            logger.info("Bounce scan %s: %d candidate DSN messages", mailbox["name"], len(messages))
            for m in messages:
                try:
                    msg = service.users().messages().get(
                        userId="me", id=m["id"], format="full"
                    ).execute(num_retries=5)
                except Exception:
                    continue
                # Search headers and snippet+body for the failed recipient email.
                headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
                candidates = []
                for hkey in ("x-failed-recipients", "final-recipient", "original-recipient"):
                    if hkey in headers:
                        candidates.extend(email_re.findall(headers[hkey]))
                snippet = msg.get("snippet") or ""
                candidates.extend(email_re.findall(snippet))
                # Deep body scan (best effort)
                def _walk(part):
                    body = part.get("body", {})
                    data = body.get("data")
                    if data:
                        try:
                            text = base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="ignore")
                            candidates.extend(email_re.findall(text))
                        except Exception:
                            pass
                    for p in part.get("parts", []) or []:
                        _walk(p)
                _walk(msg.get("payload", {}))

                seen_addrs = {c.lower() for c in candidates}
                for addr in seen_addrs:
                    lead_id = sent_leads.get(addr)
                    if not lead_id:
                        continue
                    reason = (snippet or "Delivery failed")[:300]
                    try:
                        set_lead_status(lead_id, "bounced", bounce_reason=reason)
                        log_event(lead_id, "bounced", reason, mailbox=mailbox["name"])
                        logger.info(
                            "Marked lead %s (%s) as bounced via %s: %s",
                            lead_id, addr, mailbox["name"], reason[:120],
                        )
                        sent_leads.pop(addr, None)
                        marked += 1
                    except Exception:
                        logger.exception("Failed to mark lead %s bounced", lead_id)
        except Exception as e:
            logger.warning("Gmail bounce scan failed for mailbox %s: %s", mailbox["name"], e)
    return marked


def _all_user_ids() -> List[int]:
    """Return every user that is registered or owns leads."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT user_id FROM (
                SELECT id AS user_id FROM users
                UNION
                SELECT user_id FROM leads WHERE user_id IS NOT NULL
            ) ORDER BY user_id
            """
        )
        return [row["user_id"] for row in cur.fetchall()]
    finally:
        conn.close()


def _auto_promote_due_leads(user_id: int) -> int:
    """When auto-send is enabled, promote new/enriched leads and due follow-ups
    to 'scheduled' (scheduled_at=now) so the send loop picks them up.

    Simple FIFO, respects the per-user daily send cap. No time-of-day windows.
    """
    from settings import get_auto_send_enabled, get_daily_send_cap
    from followups import step_to_wait_days

    if not get_auto_send_enabled(user_id):
        logger.info("Auto-promote: auto_send_enabled is OFF for user %s", user_id)
        return 0

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    today_iso = now.date().isoformat()

    conn = get_conn()
    cur = conn.cursor()

    # Bulk-enrich new leads that already have an industry (no AI needed)
    cur.execute(
        "UPDATE leads SET status = 'enriched' WHERE status = 'new' AND user_id = ? AND COALESCE(industry, '') != ''",
        (user_id,),
    )
    bulk_enriched = cur.rowcount
    conn.commit()

    daily_cap = min(get_daily_send_cap(user_id), get_total_daily_cap())

    cur.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE user_id = ? AND sent_at IS NOT NULL AND substr(sent_at, 1, 10) = ?",
        (user_id, today_iso),
    )
    sent_today = cur.fetchone()["n"]

    cur.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE user_id = ? AND status = 'scheduled'",
        (user_id,),
    )
    already_scheduled = cur.fetchone()["n"]

    cur.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE user_id = ? AND status = 'new'",
        (user_id,),
    )
    new_count = cur.fetchone()["n"]

    cur.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE user_id = ? AND status = 'enriched'",
        (user_id,),
    )
    enriched_count = cur.fetchone()["n"]

    remaining = max(0, daily_cap - sent_today - already_scheduled)
    logger.info(
        "Auto-promote user %s: daily_cap=%d sent_today=%d already_scheduled=%d remaining=%d "
        "bulk_enriched_now=%d new_count=%d enriched_count=%d",
        user_id, daily_cap, sent_today, already_scheduled, remaining,
        bulk_enriched, new_count, enriched_count,
    )
    if remaining <= 0:
        conn.close()
        return 0

    cur.execute(
        "SELECT id FROM leads WHERE status = 'enriched' AND user_id = ? ORDER BY id LIMIT ?",
        (user_id, remaining),
    )
    ids = [r["id"] for r in cur.fetchall()]

    if len(ids) < remaining:
        cur.execute(
            "SELECT * FROM leads WHERE status = 'sent' AND sequence_step BETWEEN 1 AND 3 "
            "AND last_contact_at IS NOT NULL AND user_id = ? ORDER BY last_contact_at",
            (user_id,),
        )
        for lead in cur.fetchall():
            if len(ids) >= remaining:
                break
            try:
                wait_days = step_to_wait_days(lead["sequence_step"])
                last_contact = datetime.fromisoformat(lead["last_contact_at"])
                if (now - last_contact).days >= wait_days:
                    ids.append(lead["id"])
            except Exception:
                continue

    if not ids:
        conn.close()
        return 0

    for lead_id in ids:
        cur.execute(
            "UPDATE leads SET status = 'scheduled', scheduled_at = ? WHERE id = ?",
            (now.isoformat(), lead_id),
        )
    conn.commit()
    conn.close()
    logger.info("Auto-promoted %d leads to scheduled for user %s", len(ids), user_id)
    return len(ids)


def _run_cycle_for_user(user_id: int) -> bool:
    """One send + inbox-check pass for a single user's campaign.
    Returns True if the cycle completed (even with 0 sends), False if auth/setup failed."""
    paused = is_paused(user_id)
    if paused:
        return True

    # Check for work before touching any mailboxes — avoids OAuth checks for
    # idle accounts that have nothing to send and no inbox check due.
    due = _due_leads(user_id)
    if not due:
        _auto_promote_due_leads(user_id)
        due = _due_leads(user_id)

    inbox_due = _should_check_inbox(user_id)

    if not due and not inbox_due:
        return True

    if due:
        logger.info("Found %d due leads for user %s", len(due), user_id)
        try:
            send_due(user_id)
        except Exception:
            logger.exception("Send cycle failed for user %s", user_id)

    if inbox_due:
        try:
            from_email = get_smtp_user(user_id)
            if not from_email:
                active = [m for m in MAILBOX_POOL if m["active"]]
                from_email = active[0]["address"] if active else None
            if not from_email:
                raise RuntimeError("No active mailbox or SMTP user available for inbox check")
            _check_inbox(user_id, from_email)
        except Exception as e:
            logger.warning("Inbox check failed for user %s: %s", user_id, e)

    return True


def run_sender_loop() -> None:
    """Multi-tenant daemon: each cycle, loops over every registered user and
    sends/checks their own due leads using their own SMTP credentials."""
    logger.info("Starting SMTP sender daemon (multi-tenant)")

    consecutive_failures = 0
    try:
        while True:
            cycle_ok = True
            for user_id in _all_user_ids():
                try:
                    ok = _run_cycle_for_user(user_id)
                    if not ok:
                        cycle_ok = False
                except Exception as e:
                    logger.error("Cycle failed for user %s: %s", user_id, e)
                    cycle_ok = False

            if cycle_ok:
                consecutive_failures = 0
                time.sleep(SEND_INTERVAL_SECONDS)
            else:
                consecutive_failures += 1
                backoff = min(300, SEND_INTERVAL_SECONDS * (2 ** min(consecutive_failures, 5)))
                logger.warning("Backing off %ds due to %d consecutive auth failures", backoff, consecutive_failures)
                time.sleep(backoff)
    except KeyboardInterrupt:
        logger.info("Sender daemon stopped by user.")


# ---------------------------------------------------------------------------
# Single-lead send (used by the dashboard "Send now" button)
# ---------------------------------------------------------------------------
def send_lead_now(lead_id: int, user_id: int) -> Dict[str, Any]:
    """Immediately send the next due email (initial or follow-up) for one lead.

    Bypasses the schedule/window but still respects do-not-email, final
    statuses, and updates lead state the same way the normal send loop does.
    Also enforces ownership: raises ValueError if the lead doesn't belong to `user_id`.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads WHERE id = ? AND user_id = ?", (lead_id, user_id))
    lead = cur.fetchone()
    conn.close()

    if lead is None:
        raise ValueError(f"Lead {lead_id} not found")

    if is_do_not_email(lead["email"].split()[0] if lead["email"] else ""):
        set_lead_status(lead_id, "unsubscribed")
        raise ValueError(f"{lead['email']} is on the do-not-email list")

    if is_final(lead["status"]):
        raise ValueError(f"Lead {lead_id} is already in a final state: {lead['status']}")

    conn = get_conn()
    try:
        mailbox = get_next_mailbox(conn)
    finally:
        conn.close()
    if mailbox is None:
        raise RuntimeError("All mailboxes at daily cap")

    step = lead["sequence_step"] or 0
    in_reply_to = lead["gmail_message_id_header"] or ""
    thread_id = lead["gmail_thread_id"] or ""

    result = _send_mailbox_message(lead, mailbox)
    if not result:
        raise RuntimeError("Send failed for unknown reason")

    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    next_step = step + 1

    if step == 0:
        log_event(lead_id, "sent", mailbox=mailbox["name"])
    else:
        log_event(lead_id, f"followup_{step}", f"Step {step} follow-up", mailbox=mailbox["name"])

    if step == len(FOLLOWUP_SCHEDULE):
        new_status = "completed"
        next_step = step + 1
    else:
        new_status = "sent"

    set_lead_status(
        lead_id,
        new_status,
        sequence_step=next_step,
        last_contact_at=now,
        sent_at=now,
        gmail_message_id=result["message_id"],
        gmail_thread_id=result["thread_id"],
        gmail_message_id_header=result.get("message_id_header", ""),
        scheduled_at=None,
        sent_from_mailbox=mailbox["name"],
    )

    return {
        "lead_id": lead_id,
        "company_name": lead["company_name"],
        "email": lead["email"],
        "step": step,
        "new_status": new_status,
        "subject": result["subject"],
    }


# ---------------------------------------------------------------------------
# One-off test-send helper (used by CLI: `python main.py test-send <email>`)
# ---------------------------------------------------------------------------
def send_test_email(
    to_address: str,
    user_id: int = 0,
    company_name: str = "Test Company",
    industry: str = "cleaning",
) -> Dict[str, str]:
    """Generate a preview email using the current template and send it directly
    from the first active mailbox."""
    mailbox = next((m for m in MAILBOX_POOL if m["active"]), None)
    if mailbox is None:
        raise RuntimeError("No active mailboxes configured")
    return _send_mailbox_test(mailbox, to_address, company_name, industry, user_id)
