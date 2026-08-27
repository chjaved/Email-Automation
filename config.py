"""Campaign Engine configuration."""
import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# Used to sign login session cookies and to encrypt per-user SMTP app
# passwords at rest. MUST be set to a fixed value in production (Railway
# Variables on both the web and worker services) - if it changes, all
# existing sessions are invalidated and stored SMTP passwords become
# undecryptable. A random one is generated for local/dev convenience only.
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    logging.getLogger(__name__).warning(
        "SECRET_KEY not set - using a random, non-persistent key. "
        "Set SECRET_KEY in your environment for production so sessions and "
        "encrypted SMTP passwords survive restarts/redeploys."
    )

DB_PATH = BASE_DIR / "campaign.db"
LOG_PATH = BASE_DIR / "campaign.log"
DO_NOT_EMAIL_PATH = BASE_DIR / "do_not_email.csv"
GMAIL_CREDENTIALS = BASE_DIR / "credentials.json"
GMAIL_TOKEN = BASE_DIR / "token.json"

TIMEZONE = os.getenv("TIMEZONE", "Asia/Kuala_Lumpur")
DAILY_CAP = int(os.getenv("DAILY_CAP", "20"))
FROM_ALIAS = os.getenv("FROM_ALIAS", "").strip()  # legacy single-alias fallback
FROM_DISPLAY_NAME = os.getenv("FROM_DISPLAY_NAME", "Javed Jabbar").strip()
MIN_GAP_SECONDS = int(os.getenv("MIN_GAP_SECONDS", "90"))

# Gmail OAuth scopes used for multi-mailbox sending and bounce/reply detection
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Multi-mailbox pool: emails are sent round-robin across active mailboxes,
# respecting each mailbox's daily cap and warmup ramp schedule.
MAILBOX_POOL = [
    {
        "name": "info",
        "address": "info@iprosedutech.com.my",
        "alias": "fwiv-ai@iprosedutech.com.my",
        "credentials": BASE_DIR / "credentials_info.json",
        "token": BASE_DIR / "token_info.json",
        "daily_cap": 1000,
        "active": True,
        "warmup_day": 4,
    },
    {
        "name": "contact",
        "address": "contact@iprosedutech.com.my",
        "alias": "fwiv-ai-sys@iprosedutech.com.my",
        "credentials": BASE_DIR / "credentials_contact.json",
        "token": BASE_DIR / "token_contact.json",
        "daily_cap": 1000,
        "active": True,
        "warmup_day": 0,
    },
    {
        "name": "ipros",
        "address": "ipros@iprosedutech.com.my",
        "alias": "fwiv-ai-portal@iprosedutech.com.my",
        "credentials": BASE_DIR / "credentials_ipros.json",
        "token": BASE_DIR / "token_ipros.json",
        "daily_cap": 1000,
        "active": True,
        "warmup_day": 0,
    },
]

# Warmup ramp: days since activation -> maximum allowed daily cap for that day
WARMUP_RAMP = {
    0: 100,
    4: 300,
    8: 1000,
    15: 1000,
}

# SMTP (Gmail) settings
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()

# Company profile PDF to attach to every outbound email (optional; skipped if missing)
ATTACHMENT_PATH = Path(os.getenv("ATTACHMENT_PATH", str(BASE_DIR / "AP_ONLINE_JOBS_COMPANY_PROFILE.pdf")))

# Email signature (single source of truth for both the plain-text and HTML
# versions of every outbound email; the HTML version also embeds the logo
# image below the contact details).
SIGNATURE_NAME = os.getenv("SIGNATURE_NAME", "Javed Jabbar").strip()
SIGNATURE_TITLE = os.getenv("SIGNATURE_TITLE", "Telemarketing Coordinator").strip()
SIGNATURE_COMPANY = os.getenv("SIGNATURE_COMPANY", "AP ONLINE JOBS SDN BHD").strip()
SIGNATURE_EMAIL = os.getenv("SIGNATURE_EMAIL", "javed@onlinejobs.my").strip()
SIGNATURE_PHONE = os.getenv("SIGNATURE_PHONE", "0109086787").strip()
SIGNATURE_WEBSITE = os.getenv("SIGNATURE_WEBSITE", "https://onlinejobs.my/").strip()
# Logo image embedded inline in the HTML signature (optional; skipped if missing).
SIGNATURE_LOGO_PATH = Path(os.getenv("SIGNATURE_LOGO_PATH", str(BASE_DIR / "signature_logo.png")))

# CC'd on every outbound email (initial + follow-ups + manual "Send now").
DEFAULT_CC_EMAILS = [
    e.strip()
    for e in os.getenv("DEFAULT_CC_EMAILS", "vasu@onlinejobs.my,seelan@onlinejobs.my").split(",")
    if e.strip()
]

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
OPENAI_MINI_MODEL = os.getenv("OPENAI_MINI_MODEL", "gpt-4o-mini")

SEND_INTERVAL_SECONDS = int(os.getenv("SEND_INTERVAL_SECONDS", "30"))
SCRAPE_DELAY_SECONDS = float(os.getenv("SCRAPE_DELAY_SECONDS", "3"))
SCRAPE_TIMEOUT_SECONDS = int(os.getenv("SCRAPE_TIMEOUT_SECONDS", "10"))
BOUNCE_RATE_WINDOW = int(os.getenv("BOUNCE_RATE_WINDOW", "100"))
BOUNCE_PAUSE_THRESHOLD = float(os.getenv("BOUNCE_PAUSE_THRESHOLD", "1.0"))
BOUNCE_RATE_DAYS = int(os.getenv("BOUNCE_RATE_DAYS", "1"))

FOLLOWUP_SCHEDULE = [int(x) for x in os.getenv("FOLLOWUP_SCHEDULE", "3,7,14").split(",") if x.strip()]
# Default to 0.0.0.0 so this binds correctly on Railway/containers; override locally if needed.
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
# Railway/Render/Heroku-style platforms inject PORT; prefer that over DASHBOARD_PORT if set.
DASHBOARD_PORT = int(os.getenv("PORT") or os.getenv("DASHBOARD_PORT", "8000"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
