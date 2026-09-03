"""One-off batch: send outreach emails to the first N companies in a CSV.

Unlike a raw SMTP script, this records each send in the campaign DB (leads +
events tables) so the dashboard's Companies/Follow-ups/Overview views stay
in sync and the normal follow-up scheduler can pick these leads up later.
"""
import argparse
import csv
import re
import sys
from pathlib import Path

from config import setup_logging
from db import get_conn, insert_returning_id, init_db

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
NA_VALUES = {"", "not publicly available", "n/a", "na", "none", "-"}


def pick_emails(row: dict) -> list:
    """Return all valid, de-duplicated emails for a company.
    Order: HR Email, Recruitment Email, General Company Email (first = To, rest = Cc).
    """
    found = []
    seen = set()
    for key in ("HR Email", "Recruitment Email", "General Company Email"):
        val = (row.get(key) or "").strip()
        if val.lower() in NA_VALUES:
            continue
        if EMAIL_RE.match(val) and val.lower() not in seen:
            seen.add(val.lower())
            found.append(val)
    return found


def load_targets(csv_path: Path, limit: int, skip_emails: set) -> list:
    targets = []
    seen_emails = set()
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            company = (row.get("Company Name") or "").strip()
            industry = (row.get("Industry") or "").strip() or "other"
            emails = pick_emails(row)
            if not company or not emails:
                continue
            primary = emails[0]
            if primary.lower() in seen_emails or primary.lower() in skip_emails:
                continue
            seen_emails.add(primary.lower())
            targets.append({
                "company": company,
                "industry": industry,
                "email": primary,
                "cc": emails[1:],
            })
            if limit and len(targets) >= limit:
                break
    return targets


def import_only(csv_path: Path, limit: int) -> None:
    """Upsert companies into the DB with status='new' (no email sent).
    Lets them show up on the dashboard so you can trigger sends individually.
    """
    init_db()
    targets = load_targets(csv_path, limit, skip_emails=set())
    if not targets:
        print("No valid rows found in CSV.")
        return

    imported, updated = 0, 0
    for t in targets:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, status FROM leads WHERE email = ? LIMIT 1", (t["email"],))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE leads SET company_name = ?, industry = ? WHERE id = ?",
                (t["company"], t["industry"].lower(), row["id"]),
            )
            updated += 1
        else:
            cur.execute(
                "INSERT INTO leads (company_name, email, industry, status) VALUES (?, ?, ?, 'new')",
                (t["company"], t["email"], t["industry"].lower()),
            )
            imported += 1
        conn.commit()
        conn.close()

    print(f"Imported {imported} new, updated {updated} existing. Total processed: {len(targets)}")
    print("Open the dashboard's Companies tab and use 'Send now' to email them individually.")


def _already_contacted_emails() -> set:
    """Emails already sent/in-progress/final in the DB - skip these by default."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT email FROM leads WHERE status NOT IN ('new', 'enrichment_failed')"
    )
    rows = cur.fetchall()
    conn.close()
    return {r["email"].lower() for r in rows}


def _upsert_lead(company: str, email: str, industry: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM leads WHERE email = ? LIMIT 1", (email,))
    row = cur.fetchone()
    if row:
        lead_id = row["id"]
        cur.execute(
            "UPDATE leads SET company_name = ?, industry = ? WHERE id = ?",
            (company, industry.lower(), lead_id),
        )
    else:
        lead_id = insert_returning_id(
            cur,
            """
            INSERT INTO leads (company_name, email, industry, status)
            VALUES (?, ?, ?, 'new')
            """,
            (company, email, industry.lower()),
        )
    conn.commit()
    conn.close()
    return lead_id


def send_batch(csv_path: Path, limit: int, gap_seconds: int, dry_run: bool, force: bool) -> None:
    raise NotImplementedError(
        "send_batch live sending is disabled in the multi-mailbox version. "
        "Use `python main.py run` or the dashboard's Send now button."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path, help="CSV file with enriched leads")
    parser.add_argument("--limit", type=int, default=10, help="0 = no limit (all rows)")
    parser.add_argument("--gap", type=int, default=15, help="seconds between sends")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-target companies already contacted")
    parser.add_argument(
        "--import-only",
        action="store_true",
        help="Only upsert companies into the DB (status='new'); do not send. "
             "Use this to populate the dashboard's Companies tab so you can send individually.",
    )
    args = parser.parse_args()

    setup_logging()
    if args.import_only:
        import_only(args.csv, args.limit)
    else:
        send_batch(args.csv, args.limit, args.gap, args.dry_run, args.force)


if __name__ == "__main__":
    sys.exit(main())
