"""CLI entry point for campaign-engine."""
import argparse
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DASHBOARD_HOST, DASHBOARD_PORT, LOG_PATH, setup_logging, TIMEZONE
from db import init_db
from enricher import run_enrichment
from generator import preview_emails
from leads import ingest_csv
from sender import reset_pause, run_sender_loop, send_test_email


logger = logging.getLogger(__name__)


def _init() -> None:
    setup_logging()
    init_db()


def cmd_import(args: argparse.Namespace) -> None:
    path = Path(args.csv)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)
    stats = ingest_csv(path)
    print("Import complete.")
    for k, v in stats.items():
        print(f"  {k}: {v}")


def cmd_enrich(args: argparse.Namespace) -> None:
    count = run_enrichment()
    print(f"Enriched {count} leads.")


def cmd_preview(args: argparse.Namespace) -> None:
    preview_emails(args.n)


def cmd_run(args: argparse.Namespace) -> None:
    print("Starting campaign engine daemon. Press Ctrl+C to stop.")
    import os
    from db import USE_POSTGRES
    if USE_POSTGRES:
        url = os.getenv("DATABASE_URL", "")
        # Mask credentials, show only host/db for verification
        safe = re.sub(r"://[^@]+@", "://***:***@", url)
        print(f"[db] Using Postgres: {safe}")
    else:
        print("[db] WARNING: Using local SQLite (DATABASE_URL not set). "
              "This worker will NOT see data from the web service's Postgres database.")
    run_sender_loop()


def cmd_stats(args: argparse.Namespace) -> None:
    from db import get_conn

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS n FROM leads")
    total = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n FROM leads WHERE status = 'enriched'")
    enriched = cur.fetchone()["n"]

    today = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    cur.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE status = 'sent' AND substr(sent_at, 1, 10) = ?",
        (today,),
    )
    sent_today = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n FROM leads WHERE status = 'sent'")
    sent_total = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n FROM leads WHERE status = 'bounced'")
    bounces = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n FROM leads WHERE status = 'replied'")
    replies = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n FROM leads WHERE status = 'unsubscribed'")
    unsubscribes = cur.fetchone()["n"]

    print("=" * 50)
    print("CAMPAIGN STATS")
    print("=" * 50)
    print(f"  Total leads:        {total}")
    print(f"  Enriched:           {enriched}")
    print(f"  Sent today:         {sent_today}")
    print(f"  Sent total:         {sent_total}")
    print(f"  Bounces:            {bounces}")
    print(f"  Replies:            {replies}")
    print(f"  Unsubscribes:       {unsubscribes}")
    print("-" * 50)
    print("  Per-industry breakdown")
    print("-" * 50)

    cur.execute(
        """
        SELECT COALESCE(NULLIF(industry, ''), 'other') AS industry, status, COUNT(*)
        FROM leads
        GROUP BY industry, status
        """
    )
    breakdown: dict = {}
    for row in cur.fetchall():
        industry = row["industry"]
        status = row["status"]
        count = row[2]
        breakdown.setdefault(industry, {})[status] = count

    for industry, statuses in sorted(breakdown.items()):
        print(f"    {industry}")
        for status, count in sorted(statuses.items()):
            print(f"      {status}: {count}")

    conn.close()


def cmd_reset_pause(args: argparse.Namespace) -> None:
    reset_pause()
    print("Campaign pause reset.")


def cmd_test_send(args: argparse.Namespace) -> None:
    result = send_test_email(
        to_address=args.email,
        company_name=args.company,
        industry=args.industry,
    )
    print("Test email sent.")
    print(f"  From:    {result['from']}")
    print(f"  To:      {result['to']}")
    print(f"  Subject: {result['subject']}")


def cmd_dashboard(args: argparse.Namespace) -> None:
    from dashboard import start_dashboard

    print(f"Starting dashboard at http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    start_dashboard()


def main(argv: list = None) -> int:
    _init()

    parser = argparse.ArgumentParser(prog="campaign-engine", description="Email campaign automation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="Ingest a CSV of leads")
    p_import.add_argument("csv", help="Path to CSV file")
    p_import.set_defaults(func=cmd_import)

    p_enrich = sub.add_parser("enrich", help="Run enrichment on new leads")
    p_enrich.set_defaults(func=cmd_enrich)

    p_preview = sub.add_parser("preview", help="Generate and preview sample emails")
    p_preview.add_argument("--n", type=int, default=10, help="Number of samples")
    p_preview.set_defaults(func=cmd_preview)

    p_run = sub.add_parser("run", help="Start scheduler + sender daemon")
    p_run.set_defaults(func=cmd_run)

    p_stats = sub.add_parser("stats", help="Show campaign statistics")
    p_stats.set_defaults(func=cmd_stats)

    p_reset = sub.add_parser("reset-pause", help="Resume a paused campaign")
    p_reset.set_defaults(func=cmd_reset_pause)

    p_test = sub.add_parser("test-send", help="Send a single test email using the current template")
    p_test.add_argument("email", help="Recipient email address")
    p_test.add_argument("--company", default="Test Company", help="Recipient company name")
    p_test.add_argument("--industry", default="cleaning", help="Industry (e.g. cleaning, construction, hotel)")
    p_test.set_defaults(func=cmd_test_send)

    p_dashboard = sub.add_parser("dashboard", help="Start web dashboard")
    p_dashboard.set_defaults(func=cmd_dashboard)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
