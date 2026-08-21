"""SMTP sender + bounce/reply detection.

Sends via Gmail SMTP with an authenticated `SMTP_USER` and (optionally) a
different `FROM_ALIAS` address that must be a registered "send-as" alias
on the same Gmail account.
"""
import email.utils
import html as html_lib
import logging
import mimetypes
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
    ATTACHMENT_PATH,
    BOUNCE_PAUSE_THRESHOLD,
    BOUNCE_RATE_WINDOW,
    DEFAULT_CC_EMAILS,
    FOLLOWUP_SCHEDULE,
    SEND_INTERVAL_SECONDS,
    SIGNATURE_COMPANY,
    SIGNATURE_EMAIL,
    SIGNATURE_LOGO_PATH,
    SIGNATURE_NAME,
    SIGNATURE_PHONE,
    SIGNATURE_TITLE,
    SIGNATURE_WEBSITE,
    SMTP_HOST,
    SMTP_PORT,
    TIMEZONE,
)
from db import get_conn, log_event
from settings import get_from_alias, get_from_display_name, get_smtp_password, get_smtp_user
from followups import get_followup_body, is_final
from generator import generate_for_lead
from leads import add_do_not_email, is_do_not_email
from scheduler import build_daily_schedule

logger = logging.getLogger(__name__)

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
# SMTP session
# ---------------------------------------------------------------------------
class SMTPSession:
    """Thin wrapper around smtplib for a single SMTP session, scoped to one user's credentials."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.user = get_smtp_user(user_id)
        self.password = get_smtp_password(user_id)
        if not self.user or not self.password:
            raise RuntimeError(
                "SMTP_USER / SMTP_PASSWORD not configured. Set them in the dashboard's Settings tab."
            )
        self.host = SMTP_HOST
        self.port = SMTP_PORT
        self._smtp: Optional[smtplib.SMTP] = None

    def __enter__(self) -> "SMTPSession":
        self._smtp = smtplib.SMTP(self.host, self.port, timeout=30)
        self._smtp.ehlo()
        self._smtp.starttls()
        self._smtp.ehlo()
        self._smtp.login(self.user, self.password)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except Exception:
                pass
            self._smtp = None

    def send(self, from_addr: str, to_addrs: List[str], msg: EmailMessage) -> None:
        assert self._smtp is not None, "SMTP session not open"
        self._smtp.send_message(msg, from_addr=from_addr, to_addrs=to_addrs)


def get_from_address(user_id: int) -> str:
    """Return the address to use as the visible `From:` header."""
    return (get_from_alias(user_id) or get_smtp_user(user_id)).strip()


def verify_sender(user_id: int) -> str:
    """Best-effort validation that SMTP credentials + alias are usable."""
    from_addr = get_from_address(user_id)
    if not from_addr:
        raise RuntimeError("No FROM_ALIAS or SMTP_USER configured")
    if not get_smtp_user(user_id) or not get_smtp_password(user_id):
        raise RuntimeError(
            "SMTP_USER / SMTP_PASSWORD missing. Set them in the dashboard's Settings tab."
        )
    with SMTPSession(user_id):
        pass
    logger.info("SMTP login OK for %s; sending as %s", get_smtp_user(user_id), from_addr)
    return from_addr


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------
def _attach_pdf(msg: EmailMessage, user_id: Optional[int] = None) -> None:
    """Attach the per-user file if one is uploaded, else fall back to the
    global ATTACHMENT_PATH on disk. Silently sends without attachment if
    neither is available."""
    # 1) Per-user attachment stored in DB (survives redeploys)
    if user_id is not None:
        try:
            from settings import get_attachment
            att = get_attachment(user_id)
        except Exception as e:
            logger.warning("Failed to load per-user attachment: %s", e)
            att = None
        if att is not None:
            data, name, mime = att
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            if not subtype:
                maintype, subtype = "application", "octet-stream"
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
            return

    # 2) Legacy global fallback (env-configured path on disk)
    path: Path = ATTACHMENT_PATH
    if not path.exists():
        logger.info("No per-user attachment and global %s missing; sending without PDF", path)
        return
    ctype, _ = mimetypes.guess_type(str(path))
    maintype, subtype = (ctype or "application/pdf").split("/", 1)
    with open(path, "rb") as f:
        data = f.read()
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)


_SIGNATURE_BLOCK_RE = re.compile(r"\n\n(Kind regards,\n\n.*?)(\n\n-+\n.*)?$", re.DOTALL)


def _split_signature(body: str) -> tuple:
    """Split a plain-text email body into (main_text, signature_text, footer_text)."""
    m = _SIGNATURE_BLOCK_RE.search(body)
    if not m:
        return body, "", ""
    return body[: m.start()], m.group(1) or "", m.group(2) or ""


def _paragraphs_html(text: str) -> str:
    paragraphs = [p for p in text.strip().split("\n\n") if p.strip()]
    return "".join(
        "<p style='margin:0 0 14px;'>" + html_lib.escape(p).replace("\n", "<br>") + "</p>"
        for p in paragraphs
    )


def _signature_html(logo_cid: Optional[str]) -> str:
    logo_html = (
        f"<img src='cid:{logo_cid}' alt='{html_lib.escape(SIGNATURE_COMPANY)}' "
        f"style='max-width:200px;margin-top:10px;display:block;'>"
        if logo_cid
        else ""
    )
    return (
        "<p style='margin:0 0 4px;'>Kind regards,</p>"
        "<p style='margin:0 0 4px;line-height:1.5;'>"
        f"<strong>{html_lib.escape(SIGNATURE_NAME)}</strong><br>"
        f"{html_lib.escape(SIGNATURE_TITLE)}<br>"
        f"{html_lib.escape(SIGNATURE_COMPANY)}<br>"
        f"Email: <a href='mailto:{SIGNATURE_EMAIL}'>{html_lib.escape(SIGNATURE_EMAIL)}</a><br>"
        f"Phone: {html_lib.escape(SIGNATURE_PHONE)}<br>"
        f"Website: <a href='{SIGNATURE_WEBSITE}'>{html_lib.escape(SIGNATURE_WEBSITE)}</a>"
        "</p>"
        f"{logo_html}"
    )


def _build_html_body(body: str) -> str:
    main, signature, footer = _split_signature(body)
    parts = [_paragraphs_html(main)]
    if signature:
        logo_cid = "logo" if SIGNATURE_LOGO_PATH.exists() else None
        parts.append(_signature_html(logo_cid))
    footer_text = re.sub(r"^-+\s*", "", footer.strip()) if footer else ""
    if footer_text:
        parts.append(
            f"<p style='margin:18px 0 0;font-size:0.82em;color:#6b7280;'>{html_lib.escape(footer_text)}</p>"
        )
    return (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;"
        "color:#1f2430;line-height:1.4;\">" + "".join(parts) + "</body></html>"
    )


def _build_message(
    to: str,
    from_addr: str,
    subject: str,
    body: str,
    user_id: int,
    in_reply_to: str = "",
    cc: Optional[List[str]] = None,
) -> EmailMessage:
    msg = EmailMessage()
    display = get_from_display_name(user_id) or "AP Online Jobs"
    msg["From"] = email.utils.formataddr((display, from_addr))
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Reply-To"] = from_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=from_addr.split("@")[-1] or None)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    msg.add_alternative(_build_html_body(body), subtype="html")
    if SIGNATURE_LOGO_PATH.exists():
        html_part = msg.get_payload()[1]
        ctype, _ = mimetypes.guess_type(str(SIGNATURE_LOGO_PATH))
        maintype, subtype = (ctype or "image/png").split("/", 1)
        with open(SIGNATURE_LOGO_PATH, "rb") as f:
            html_part.add_related(f.read(), maintype=maintype, subtype=subtype, cid="logo")
    _attach_pdf(msg, user_id)
    return msg


def get_email_body(lead: sqlite3.Row) -> Dict[str, str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM emails WHERE lead_id = ?", (lead["id"],))
    cached = cur.fetchone()
    conn.close()
    if cached:
        return {"subject": cached["subject"], "body": cached["body"]}
    return generate_for_lead(lead)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def send_message(
    session: SMTPSession,
    lead: sqlite3.Row,
    from_email: str,
    user_id: int,
    step: int = 0,
    in_reply_to: str = "",
    thread_id: str = "",
) -> Optional[Dict[str, str]]:
    to = lead["email"]

    email_data = get_email_body(lead)
    subject = email_data["subject"]
    body = email_data["body"]
    if step > 0:
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        body = get_followup_body(lead, step)

    from settings import get_cc_enabled

    cc_list = DEFAULT_CC_EMAILS if get_cc_enabled(user_id) else []
    msg = _build_message(to, from_email, subject, body, user_id, in_reply_to=in_reply_to, cc=cc_list)

    try:
        session.send(from_addr=from_email, to_addrs=[to] + cc_list, msg=msg)
    except Exception as e:
        logger.error("Failed to send to %s: %s", to, e)
        raise

    message_id = msg["Message-ID"]
    logger.info("Sent to %s (message-id %s, step %s)", to, message_id, step)
    return {
        "message_id": message_id,
        "thread_id": thread_id or message_id,
        "subject": subject,
        "body": body,
        "message_id_header": message_id,
    }


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


def reset_pause(user_id: int) -> None:
    _set_state(_state_key(user_id, "paused"), "0")


def _trailing_bounce_rate(user_id: int) -> float:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status FROM leads WHERE sent_at IS NOT NULL AND user_id = ? ORDER BY sent_at DESC LIMIT ?",
            (user_id, BOUNCE_RATE_WINDOW),
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
            for addr in found:
                if addr.lower() == from_email_lower or addr.lower() == get_smtp_user(user_id).lower():
                    continue
                lead_id = sent_leads.get(addr.lower())
                if lead_id:
                    set_lead_status(lead_id, "bounced")
                    log_event(lead_id, "bounced")
                    logger.info("Marked lead %s as bounced (%s)", lead_id, addr)
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
    return rows


def send_due(session: SMTPSession, from_email: str, user_id: int) -> int:
    due = _due_leads(user_id)
    if not due:
        return 0

    sent_count = 0
    for lead in due:
        if is_paused(user_id):
            logger.warning("Campaign is paused; stopping send loop.")
            return sent_count

        if is_do_not_email(lead["email"]):
            logger.info("Skipping %s: in do_not_email.csv", lead["email"])
            set_lead_status(lead["id"], "unsubscribed")
            continue

        if is_final(lead["status"]):
            continue

        if not check_bounce_rate_safety(user_id):
            return sent_count

        step = lead["sequence_step"] or 0
        in_reply_to = lead["gmail_message_id_header"] or ""
        thread_id = lead["gmail_thread_id"] or ""

        try:
            result = send_message(
                session,
                lead,
                from_email,
                user_id,
                step=step,
                in_reply_to=in_reply_to,
                thread_id=thread_id,
            )
            if result:
                now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
                next_step = step + 1

                if step == 0:
                    log_event(lead["id"], "sent")
                else:
                    log_event(lead["id"], f"followup_{step}", f"Step {step} follow-up")

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
                )
                sent_count += 1
                logger.info(
                    "Marked lead %s as %s (step %d)", lead["id"], new_status, step
                )
        except Exception as e:
            logger.error("Send failed for lead %s: %s", lead["id"], e)

    return sent_count


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
    _set_state(_state_key(user_id, "last_inbox_check"), datetime.now(ZoneInfo(TIMEZONE)).isoformat())


def _all_user_ids() -> List[int]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users ORDER BY id")
        return [row["id"] for row in cur.fetchall()]
    finally:
        conn.close()


def _run_cycle_for_user(user_id: int) -> None:
    """One send + inbox-check pass for a single user's campaign."""
    if is_paused(user_id):
        return

    try:
        from_email = verify_sender(user_id)
    except Exception as e:
        logger.debug("User %s not ready to send (%s); skipping this cycle.", user_id, e)
        return

    build_daily_schedule(user_id)

    due = _due_leads(user_id)
    if due:
        try:
            with SMTPSession(user_id) as session:
                send_due(session, from_email, user_id)
        except Exception as e:
            logger.error("Send cycle failed for user %s: %s", user_id, e)

    if _should_check_inbox(user_id):
        try:
            _check_inbox(user_id, from_email)
        except Exception as e:
            logger.warning("Inbox check failed for user %s: %s", user_id, e)


def run_sender_loop() -> None:
    """Multi-tenant daemon: each cycle, loops over every registered user and
    sends/checks their own due leads using their own SMTP credentials."""
    logger.info("Starting SMTP sender daemon (multi-tenant)")

    try:
        while True:
            for user_id in _all_user_ids():
                _run_cycle_for_user(user_id)

            time.sleep(SEND_INTERVAL_SECONDS)
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

    if is_do_not_email(lead["email"]):
        set_lead_status(lead_id, "unsubscribed")
        raise ValueError(f"{lead['email']} is on the do-not-email list")

    if is_final(lead["status"]):
        raise ValueError(f"Lead {lead_id} is already in a final state: {lead['status']}")

    from_email = verify_sender(user_id)
    step = lead["sequence_step"] or 0
    in_reply_to = lead["gmail_message_id_header"] or ""
    thread_id = lead["gmail_thread_id"] or ""

    with SMTPSession(user_id) as session:
        result = send_message(
            session,
            lead,
            from_email,
            user_id,
            step=step,
            in_reply_to=in_reply_to,
            thread_id=thread_id,
        )

    if not result:
        raise RuntimeError("Send failed for unknown reason")

    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    next_step = step + 1

    if step == 0:
        log_event(lead_id, "sent")
    else:
        log_event(lead_id, f"followup_{step}", f"Step {step} follow-up")

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
def send_test_email(to_address: str, user_id: int, company_name: str = "Test Company",
                    industry: str = "cleaning") -> Dict[str, str]:
    """Generate a preview email using the current template and send it directly."""
    from_email = verify_sender(user_id)

    # Build a synthetic lead-like row without touching the DB
    class _Row(dict):
        def __getitem__(self, k):
            return super().__getitem__(k) if k in self else None

    fake_lead = _Row(
        id=-1,
        company_name=company_name,
        email=to_address,
        industry=industry,
        location="Malaysia",
        enriched_data=None,
    )

    # Generator caches by lead_id in DB; use direct helpers to avoid persistence.
    from generator import _ai_personalisation, _assemble_body, _build_subject, _normalise_industry

    norm_industry = _normalise_industry(industry)
    subject = _build_subject(company_name)
    personalisation = _ai_personalisation(company_name, norm_industry, "Malaysia", {})
    body = _assemble_body(company_name, norm_industry, personalisation)

    msg = _build_message(to_address, from_email, subject, body, user_id)
    with SMTPSession(user_id) as session:
        session.send(from_addr=from_email, to_addrs=[to_address], msg=msg)
    logger.info("Test email sent from %s to %s", from_email, to_address)
    return {"subject": subject, "body": body, "from": from_email, "to": to_address}
