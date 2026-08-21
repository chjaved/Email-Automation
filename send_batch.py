"""One-off batch: send outreach emails to the first N companies in a CSV.

Unlike a raw SMTP script, this records each send in the campaign DB (leads +
events tables) so the dashboard's Companies/Follow-ups/Overview views stay
in sync and the normal follow-up scheduler can pick these leads up later.
"""
import argparse
import csv
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import setup_logging
from db import get_conn, insert_returning_id, log_event, init_db
from generator import _ai_personalisation, _assemble_body, _build_subject, _normalise_industry
from sender import SMTPSession, _build_message, set_lead_status, verify_sender

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
    init_db()
    skip_emails = set() if force else _already_contacted_emails()
    targets = load_targets(csv_path, limit, skip_emails)
    if not targets:
        print("No new valid targets found in CSV (all may already be contacted; use --force to re-target).")
        return

    print(f"Loaded {len(targets)} targets from {csv_path}")
    for i, t in enumerate(targets, 1):
        cc_str = f"  cc: {', '.join(t['cc'])}" if t["cc"] else ""
        print(f"  {i:2d}. {t['company']}  to: {t['email']}{cc_str}  [{t['industry']}]")

    if dry_run:
        print("\nDry run - no emails sent, no DB changes.")
        return

    from_email = verify_sender()
    sent, failed = 0, 0
    with SMTPSession() as session:
        for i, t in enumerate(targets, 1):
            cc_str = f" cc: {', '.join(t['cc'])}" if t["cc"] else ""
            print(f"\n[{i}/{len(targets)}] Sending to {t['company']} to: {t['email']}{cc_str} ...")
            lead_id = _upsert_lead(t["company"], t["email"], t["industry"])
            try:
                norm_industry = _normalise_industry(t["industry"])
                subject = _build_subject(t["company"])
                personalisation = _ai_personalisation(t["company"], norm_industry, "", {})
                body = _assemble_body(t["company"], norm_industry, personalisation)
                msg = _build_message(t["email"], from_email, subject, body, cc=t["cc"])
                all_recipients = [t["email"]] + t["cc"]
                session.send(from_addr=from_email, to_addrs=all_recipients, msg=msg)

                now = datetime.now(timezone.utc).isoformat()
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO emails (lead_id, subject, body, generated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(lead_id) DO UPDATE SET subject=excluded.subject, body=excluded.body, generated_at=excluded.generated_at
                    """,
                    (lead_id, subject, body, now),
                )
                conn.commit()
                conn.close()

                set_lead_status(
                    lead_id,
                    "sent",
                    sequence_step=1,
                    last_contact_at=now,
                    sent_at=now,
                    gmail_message_id=msg["Message-ID"],
                    gmail_thread_id=msg["Message-ID"],
                    gmail_message_id_header=msg["Message-ID"],
                    scheduled_at=None,
                )
                log_event(lead_id, "sent", "Sent via send_batch.py")

                print(f"  OK  subject: {subject}")
                sent += 1
            except Exception as e:
                print(f"  FAIL: {e}")
                failed += 1
            if i < len(targets):
                time.sleep(gap_seconds)

    print(f"\nDone. Sent={sent}  Failed={failed}")


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
