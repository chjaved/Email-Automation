"""Multi-mailbox Gmail API sender with rotation and warmup scheduling."""
import base64
import email.utils
import html as html_lib
import json
import logging
import mimetypes
import os
import re
import sqlite3
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (
    ATTACHMENT_PATH,
    DEFAULT_CC_EMAILS,
    FROM_DISPLAY_NAME,
    GMAIL_SCOPES,
    MAILBOX_POOL,
    SIGNATURE_COMPANY,
    SIGNATURE_EMAIL,
    SIGNATURE_LOGO_PATH,
    SIGNATURE_NAME,
    SIGNATURE_PHONE,
    SIGNATURE_TITLE,
    SIGNATURE_WEBSITE,
    WARMUP_RAMP,
)
from db import get_conn
from followups import get_followup_body
from generator import generate_for_lead

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OAuth credentials
# ---------------------------------------------------------------------------
def get_credentials(mailbox: Dict[str, Any]):
    """Load OAuth credentials for a specific mailbox, refreshing the token
    if needed. Accepts token/credentials from a local file or from env vars
    (MAILBOX_<NAME>_TOKEN and MAILBOX_<NAME>_CREDENTIALS) so production can
    use them without committing secrets to git."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    name = mailbox["name"]
    token_path = Path(mailbox["token"])
    creds_path = Path(mailbox["credentials"])

    env_token = os.getenv(f"MAILBOX_{name.upper()}_TOKEN")
    env_creds = os.getenv(f"MAILBOX_{name.upper()}_CREDENTIALS")

    creds = None
    if env_token:
        try:
            token_info = json.loads(env_token)
            creds = Credentials.from_authorized_user_info(token_info, GMAIL_SCOPES)
        except Exception as e:
            logger.warning("Could not load %s token from env: %s", name, e)

    if not creds and token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_secrets: Any = None
            if env_creds:
                try:
                    client_secrets = json.loads(env_creds)
                except Exception as e:
                    logger.warning("Could not load %s credentials from env: %s", name, e)
            if not client_secrets:
                if creds_path.exists():
                    client_secrets = str(creds_path)
                else:
                    raise RuntimeError(
                        f"No credentials or token for mailbox '{name}'. "
                        f"Set MAILBOX_{name.upper()}_TOKEN (or run auth-mailboxes locally and copy the token file)."
                    )
            if isinstance(client_secrets, dict):
                flow = InstalledAppFlow.from_client_config(client_secrets, GMAIL_SCOPES)
            else:
                flow = InstalledAppFlow.from_client_secrets_file(client_secrets, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())
    return creds


# ---------------------------------------------------------------------------
# Warmup / cap logic
# ---------------------------------------------------------------------------
def get_current_cap(mailbox: Dict[str, Any]) -> int:
    """Calculate current daily cap based on warmup ramp.

    warmup_day is the number of days the mailbox has been active (0 = fully warmed).
    """
    if mailbox["warmup_day"] == 0:
        return mailbox["daily_cap"]
    days_active = int(mailbox["warmup_day"])
    cap = 50  # default for the first days
    for day_threshold, cap_value in sorted(WARMUP_RAMP.items()):
        if days_active >= day_threshold:
            cap = cap_value
    return min(cap, mailbox["daily_cap"])


def get_total_daily_cap() -> int:
    """Total allowed sends today across all active mailboxes."""
    return sum(get_current_cap(m) for m in MAILBOX_POOL if m["active"])


def _sends_today(conn, mailbox_name: str) -> int:
    today_iso = date.today().isoformat()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS count FROM events WHERE mailbox=? AND event_type='sent' AND substr(created_at, 1, 10) = ?",
        (mailbox_name, today_iso),
    )
    row = cur.fetchone()
    return row["count"] if row else 0


def get_next_mailbox(conn=None) -> Optional[Dict[str, Any]]:
    """Return the active mailbox with the fewest sends today that is still
    under its warmup-adjusted daily cap. Returns None when all are at cap."""
    close_conn = conn is None
    if conn is None:
        conn = get_conn()

    try:
        candidates = []
        for mailbox in MAILBOX_POOL:
            if not mailbox["active"]:
                continue
            sends_today = _sends_today(conn, mailbox["name"])
            cap = get_current_cap(mailbox)
            if sends_today < cap:
                candidates.append((sends_today, mailbox))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
    finally:
        if close_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Message building (mirrors sender.py helpers but lives here to avoid cycles)
# ---------------------------------------------------------------------------
_SIGNATURE_BLOCK_RE = re.compile(r"\n\n(Kind regards,\n\n.*?)(\n\n-+\n.*)?$", re.DOTALL)


def _split_signature(body: str) -> tuple:
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


def _get_signature(user_id: int) -> Dict[str, str]:
    try:
        from settings import get_signature
        return get_signature(user_id)
    except Exception:
        return {
            "name": SIGNATURE_NAME,
            "title": SIGNATURE_TITLE,
            "company": SIGNATURE_COMPANY,
            "email": SIGNATURE_EMAIL,
            "phone": SIGNATURE_PHONE,
            "website": SIGNATURE_WEBSITE,
        }


def _signature_html(logo_cid: Optional[str], user_id: int = 0) -> str:
    sig = _get_signature(user_id)
    logo_html = (
        f"<img src='cid:{logo_cid}' alt='{html_lib.escape(sig['company'])}' "
        f"style='max-width:200px;margin-top:10px;display:block;'>"
        if logo_cid
        else ""
    )
    return (
        "<p style='margin:0 0 4px;'>Kind regards,</p>"
        "<p style='margin:0 0 4px;line-height:1.5;'>"
        f"<strong>{html_lib.escape(sig['name'])}</strong><br>"
        f"{html_lib.escape(sig['title'])}<br>"
        f"{html_lib.escape(sig['company'])}<br>"
        f"Email: <a href='mailto:{sig['email']}'>{html_lib.escape(sig['email'])}</a><br>"
        f"Phone: {html_lib.escape(sig['phone'])}<br>"
        f"Website: <a href='{sig['website']}'>{html_lib.escape(sig['website'])}</a>"
        "</p>"
        f"{logo_html}"
    )


def _build_html_body(body: str, user_id: int = 0) -> str:
    main, signature, footer = _split_signature(body)
    parts = [_paragraphs_html(main)]
    if signature:
        logo_cid = "logo" if SIGNATURE_LOGO_PATH.exists() else None
        parts.append(_signature_html(logo_cid, user_id))
    footer_text = re.sub(r"^-+\s*", "", footer.strip()) if footer else ""
    if footer_text:
        parts.append(
            f"<p style='margin:18px 0 0;font-size:0.82em;color:#6b7280;'>{html_lib.escape(footer_text)}</p>"
        )
    return (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;"
        "color:#1f2430;line-height:1.4;\">" + "".join(parts) + "</body></html>"
    )


def _get_email_body(lead: sqlite3.Row) -> Dict[str, str]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM emails WHERE lead_id = ?", (lead["id"],))
        cached = cur.fetchone()
        if cached:
            return {"subject": cached["subject"], "body": cached["body"]}
    finally:
        conn.close()
    return generate_for_lead(lead)


def _attach_pdf(msg: EmailMessage, user_id: int = 0) -> None:
    try:
        from settings import get_all_attachments
        attachments = get_all_attachments(user_id)
    except Exception:
        attachments = []

    for data, name, mime in attachments:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    if attachments:
        return

    path = ATTACHMENT_PATH
    if not path.exists():
        logger.info("No attachments configured; sending without PDF")
        return
    ctype, _ = mimetypes.guess_type(str(path))
    maintype, subtype = (ctype or "application/pdf").split("/", 1)
    with open(path, "rb") as f:
        data = f.read()
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)


def _build_message(
    to: str,
    from_addr: str,
    subject: str,
    body: str,
    user_id: int,
    in_reply_to: str = "",
    cc: Optional[List[str]] = None,
) -> EmailMessage:
    from settings import get_from_display_name, get_cc_enabled

    display = get_from_display_name(user_id) or FROM_DISPLAY_NAME
    msg = EmailMessage()
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
    msg.add_alternative(_build_html_body(body, user_id), subtype="html")
    if SIGNATURE_LOGO_PATH.exists():
        html_part = msg.get_payload()[1]
        ctype, _ = mimetypes.guess_type(str(SIGNATURE_LOGO_PATH))
        maintype, subtype = (ctype or "image/png").split("/", 1)
        with open(SIGNATURE_LOGO_PATH, "rb") as f:
            html_part.add_related(f.read(), maintype=maintype, subtype=subtype, cid="logo")
    _attach_pdf(msg, user_id)
    return msg


# ---------------------------------------------------------------------------
# Sending via Gmail API
# ---------------------------------------------------------------------------
def _gmail_service(mailbox: Dict[str, Any]):
    from googleapiclient.discovery import build
    creds = get_credentials(mailbox)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_message(lead: sqlite3.Row, mailbox: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Build and send one email via the chosen mailbox's Gmail API credentials."""
    from settings import get_cc_enabled

    all_emails = (lead["email"] or "").split()
    to = all_emails[0] if all_emails else lead["email"]
    extra_cc = all_emails[1:] if len(all_emails) > 1 else []
    from_addr = mailbox["alias"]
    user_id = lead["user_id"] or 0

    email_data = _get_email_body(lead)
    subject = email_data["subject"]
    body = email_data["body"]
    step = lead["sequence_step"] or 0
    if step > 0:
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        body = get_followup_body(lead, step)

    cc_list = (DEFAULT_CC_EMAILS if get_cc_enabled(user_id) else []) + extra_cc
    in_reply_to = lead["gmail_message_id_header"] or ""
    msg = _build_message(to, from_addr, subject, body, user_id, in_reply_to=in_reply_to, cc=cc_list)

    service = _gmail_service(mailbox)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()

    message_id = result.get("id", "")
    thread_id = result.get("threadId", "") or message_id
    logger.info(
        "Sent to %s from %s (message-id %s, step %s)",
        to,
        from_addr,
        message_id,
        step,
    )
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "subject": subject,
        "body": body,
        "message_id_header": msg["Message-ID"],
    }


def send_test_email(
    mailbox: Dict[str, Any],
    to_address: str,
    company_name: str = "Test Company",
    industry: str = "cleaning",
    user_id: int = 0,
) -> Dict[str, str]:
    """Send a one-off test email from a mailbox without touching the DB."""
    from generator import _ai_personalisation, _assemble_body, _build_subject, _normalise_industry
    from settings import get_cc_enabled

    norm_industry = _normalise_industry(industry)
    subject = _build_subject(company_name)
    personalisation = _ai_personalisation(company_name, norm_industry, "Malaysia", {})
    body = _assemble_body(company_name, norm_industry, personalisation)

    from_addr = mailbox["alias"]
    cc_list = DEFAULT_CC_EMAILS if get_cc_enabled(user_id) else []
    msg = _build_message(to_address, from_addr, subject, body, user_id, cc=cc_list)

    service = _gmail_service(mailbox)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info("Test email sent from %s to %s", from_addr, to_address)
    return {"subject": subject, "body": body, "from": from_addr, "to": to_address}
