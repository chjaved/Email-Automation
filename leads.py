"""CSV ingestion, cleaning, and lead persistence."""
import csv
import difflib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config import DO_NOT_EMAIL_PATH
from db import get_conn

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

CANONICAL_HEADERS = {
    "company_name": [
        "company",
        "company name",
        "company_name",
        "name of company",
        "organisation",
        "organization",
        "business name",
    ],
    "email": [
        "email",
        "e-mail",
        "email address",
        "e-mail address",
        "email_id",
        "emailid",
    ],
    "website": [
        "website",
        "site",
        "web",
        "web_url",
        "url",
        "homepage",
    ],
    "facebook": ["facebook", "fb", "facebook url", "fb url"],
    "instagram": ["instagram", "ig", "instagram url", "ig url"],
    "linkedin": ["linkedin", "linkedin url", "li url"],
    "phone": [
        "phone",
        "phone number",
        "telephone",
        "mobile",
        "contact number",
    ],
    "industry": ["industry", "sector", "business type", "type"],
    "location": [
        "location",
        "city",
        "address",
        "state",
        "country",
        "region",
        "area",
    ],
}


def _header_score(header: str, candidates: List[str]) -> float:
    header_clean = re.sub(r"[^a-z0-9]", " ", header.lower()).strip()
    best = difflib.get_close_matches(header_clean, candidates, n=1, cutoff=0.6)
    if best:
        return difflib.SequenceMatcher(None, header_clean, best[0]).ratio()
    # Substring heuristic
    for cand in candidates:
        if cand in header_clean or header_clean in cand:
            return 0.65
    return 0.0


def fuzzy_map_columns(headers: List[str]) -> Dict[str, str]:
    """Map raw CSV headers to canonical column names."""
    mapping: Dict[str, str] = {}
    used: set = set()

    scores: Dict[str, List[tuple]] = {}
    for canonical, candidates in CANONICAL_HEADERS.items():
        best_header: Optional[str] = None
        best_score = 0.0
        for h in headers:
            if h in used:
                continue
            score = _header_score(h, candidates)
            if score > best_score:
                best_score = score
                best_header = h
        if best_header and best_score >= 0.6:
            mapping[canonical] = best_header
            used.add(best_header)

    # Fallback: raw header name equals canonical exactly
    for h in headers:
        if h in used:
            continue
        if h in CANONICAL_HEADERS:
            mapping[h] = h
            used.add(h)

    return mapping


def normalize_website(value: str) -> str:
    value = value.strip()
    if not value or value.lower() in ("n/a", "na", "-", "none"):
        return ""
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "http://" + value
    return value


def parse_socials(row: Dict[str, Any], mapping: Dict[str, str]) -> str:
    socials: Dict[str, str] = {}
    for key in ("facebook", "instagram", "linkedin", "phone"):
        if key in mapping:
            val = str(row.get(mapping[key], "")).strip()
            if val and val.lower() not in ("n/a", "na", "-", "none"):
                socials[key] = val
    return json.dumps(socials, ensure_ascii=False)


def validate_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


def load_csv(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV has no headers")
        raw_headers = [h.strip() for h in reader.fieldnames]
        mapping = fuzzy_map_columns(raw_headers)

        for raw_row in reader:
            row = {k: raw_row[v] for k, v in mapping.items() if v in raw_row}
            rows.append(row)

    logger.info(
        "Loaded %d rows from %s; mapped columns: %s",
        len(rows),
        path,
        list(mapping.values()),
    )
    return rows


def ensure_do_not_email_file() -> None:
    if not DO_NOT_EMAIL_PATH.exists():
        DO_NOT_EMAIL_PATH.write_text("email\n", encoding="utf-8")


def is_do_not_email(email: str) -> bool:
    if not DO_NOT_EMAIL_PATH.exists():
        return False
    with open(DO_NOT_EMAIL_PATH, encoding="utf-8", errors="ignore") as f:
        blocked = set()
        for i, line in enumerate(f):
            addr = line.strip().lower()
            if not addr:
                continue
            if i == 0 and addr == "email":
                continue
            blocked.add(addr)
    return email.strip().lower() in blocked


def add_do_not_email(email: str) -> None:
    ensure_do_not_email_file()
    with open(DO_NOT_EMAIL_PATH, "a", encoding="utf-8") as f:
        f.write(f"{email.strip().lower()}\n")


def ingest_csv(csv_path: Path) -> Dict[str, int]:
    """Import a CSV and upsert leads."""
    ensure_do_not_email_file()
    rows = load_csv(csv_path)
    conn = get_conn()
    cur = conn.cursor()

    stats = {
        "total": len(rows),
        "valid": 0,
        "invalid": 0,
        "duplicate": 0,
        "blocked": 0,
    }

    seen: set = set()
    for row in rows:
        raw_email = str(row.get("email", "")).strip()
        if not raw_email:
            continue

        if raw_email.lower() in seen:
            stats["duplicate"] += 1
            continue
        seen.add(raw_email.lower())

        if not validate_email(raw_email):
            stats["invalid"] += 1
            continue

        if is_do_not_email(raw_email):
            stats["blocked"] += 1
            continue

        company_name = str(row.get("company_name", "")).strip()
        website = normalize_website(str(row.get("website", "")))
        socials = parse_socials(row, {})
        industry = str(row.get("industry", "")).strip().lower() or None
        location = str(row.get("location", "")).strip() or None

        # Rebuild socials with whatever column mapping we have from the row
        social_mapping = {}
        for key in ("facebook", "instagram", "linkedin", "phone"):
            if key in row:
                social_mapping[key] = key
        socials = parse_socials(row, social_mapping)

        try:
            cur.execute(
                """
                INSERT INTO leads
                (company_name, email, website, socials_json, industry, location, status)
                VALUES (?, ?, ?, ?, ?, ?, 'new')
                ON CONFLICT(email, user_id) DO NOTHING
                """,
                (company_name, raw_email, website, socials, industry, location),
            )
            if cur.rowcount:
                stats["valid"] += 1
            else:
                stats["duplicate"] += 1
        except Exception as e:
            logger.error("Insert error for %s: %s", raw_email, e)
            stats["duplicate"] += 1

    conn.commit()
    conn.close()
    logger.info("Ingestion stats: %s", stats)
    return stats
