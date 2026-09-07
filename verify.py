"""Bulk email-address verification.

Runs per-domain MX-record + syntax + known-typo checks against every lead in
`status IN ('new','enriched','scheduled')` and marks the failures with
`status = 'invalid'` so the sender loop cannot pick them up.

We only mark leads INVALID; we never delete them. The Dashboard can still
show them under a filter, and if you fix a domain typo you can flip them back
to `new` in SQL.
"""
from __future__ import annotations

import concurrent.futures
import logging
import re
from typing import Dict, List, Optional, Tuple

from db import get_conn

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Explicit typo / non-existent domain blocklist. These never resolve MX but
# some show up thousands of times in scraped Malaysian business data, and it
# saves a DNS round-trip per lead.
TYPO_DOMAINS = {
    "gmial.com", "gmali.com", "gmaill.com", "gnail.com", "gmailcom.com",
    "pgmail.com", "gmail.co", "gmail.cm",
    "gmail.com.my", "gmail.my",  # gmail has no MY TLD
    "yahooo.com", "yaho.com", "yahoo.co.my", "yahoo.my",
    "hotmial.com", "hotnail.com", "homtail.com",
    "outllook.com", "outlok.com",
}

# Public providers whose MX we do NOT need to look up — they are always up.
_KNOWN_GOOD = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.com.my", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
}

# Real minimum local-part length enforced by each public provider at signup.
# Any address on these providers with a shorter local part cannot exist and
# must not be sent to. Sources: provider signup pages, publicly documented.
_PROVIDER_MIN_LOCAL_LEN = {
    "gmail.com": 6,
    "googlemail.com": 6,
    "yahoo.com": 4,
    "yahoo.com.my": 4,
    "yahoo.co.uk": 4,
    "ymail.com": 4,
    "hotmail.com": 1,
    "outlook.com": 1,
    "live.com": 1,
    "msn.com": 1,
    "icloud.com": 3,
    "me.com": 3,
    "aol.com": 3,
    "proton.me": 1,
    "protonmail.com": 1,
}


def _local_part_is_plausible(local: str, domain: str) -> bool:
    """Reject local parts that violate the domain provider's real signup rules
    OR are structurally implausible (all-digits, no letters, etc.).

    Deliberately conservative: only reject when we're very confident. Common
    legitimate patterns like 'first.last', 'first_last123', 'firstl' pass."""
    if not local:
        return False
    min_len = _PROVIDER_MIN_LOCAL_LEN.get(domain)
    if min_len is not None and len(local) < min_len:
        return False
    # A local part with zero letters (e.g. '12345', '007') is essentially never
    # a real inbox on any consumer provider.
    if not any(c.isalpha() for c in local):
        return False
    return True


def _resolve_mx(domain: str, timeout: float = 4.0) -> bool:
    """Return True iff `domain` has at least one MX record (or A record as a
    fallback, per RFC 5321 §5). Any DNS failure returns False."""
    try:
        import dns.resolver  # type: ignore
    except Exception:
        logger.warning("dnspython not installed; treating all domains as valid")
        return True

    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    resolver.timeout = timeout
    try:
        answers = resolver.resolve(domain, "MX")
        if list(answers):
            return True
    except Exception:
        pass
    # RFC-5321 implicit MX: if no MX, an A/AAAA record still accepts mail.
    try:
        answers = resolver.resolve(domain, "A")
        return bool(list(answers))
    except Exception:
        return False


def _primary_address(raw: str) -> Optional[str]:
    if not raw:
        return None
    parts = re.split(r"[\s,;]+", raw.strip())
    for p in parts:
        p = p.strip().lower()
        if p and _EMAIL_RE.match(p):
            return p
    return None


def verify_user_leads(
    user_id: int,
    workers: int = 20,
    only_status: Tuple[str, ...] = ("new", "enriched", "scheduled"),
) -> Dict[str, int]:
    """Verify every lead for `user_id` whose status is in `only_status`.

    Returns a stats dict: {"checked", "invalid", "domains_ok", "domains_bad"}.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(only_status))
        cur.execute(
            f"SELECT id, email FROM leads WHERE user_id = ? "
            f"AND status IN ({placeholders})",
            (user_id, *only_status),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {"checked": 0, "invalid": 0, "domains_ok": 0, "domains_bad": 0}

    # Group leads by domain so each unique domain gets one DNS query.
    # syntax_bad also captures leads whose local-part violates the target
    # provider's real signup rules (e.g. gmail.com requires >=6 chars).
    by_domain: Dict[str, List[int]] = {}
    syntax_bad: List[int] = []
    for row in rows:
        primary = _primary_address(row["email"] or "")
        if not primary:
            syntax_bad.append(row["id"])
            continue
        local, domain = primary.split("@", 1)
        if not _local_part_is_plausible(local, domain):
            syntax_bad.append(row["id"])
            continue
        by_domain.setdefault(domain, []).append(row["id"])

    # Resolve MX for each unique domain in parallel.
    domain_ok: Dict[str, bool] = {}
    unknown_domains = [d for d in by_domain if d not in _KNOWN_GOOD and d not in TYPO_DOMAINS]
    for d in by_domain:
        if d in TYPO_DOMAINS:
            domain_ok[d] = False
        elif d in _KNOWN_GOOD:
            domain_ok[d] = True

    logger.info(
        "Verifying %d unique domains (%d cached ok, %d cached bad) for user %s",
        len(unknown_domains),
        sum(1 for v in domain_ok.values() if v),
        sum(1 for v in domain_ok.values() if not v),
        user_id,
    )

    if unknown_domains:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_resolve_mx, d): d for d in unknown_domains}
            for fut in concurrent.futures.as_completed(futures):
                d = futures[fut]
                try:
                    domain_ok[d] = fut.result()
                except Exception:
                    domain_ok[d] = False

    domains_bad = [d for d, ok in domain_ok.items() if not ok]
    domains_ok = [d for d, ok in domain_ok.items() if ok]

    bad_lead_ids: List[int] = list(syntax_bad)
    for d in domains_bad:
        bad_lead_ids.extend(by_domain[d])

    if bad_lead_ids:
        conn = get_conn()
        try:
            cur = conn.cursor()
            # Batch to avoid parameter-count limits on Postgres.
            CHUNK = 500
            for i in range(0, len(bad_lead_ids), CHUNK):
                chunk = bad_lead_ids[i : i + CHUNK]
                placeholders = ",".join(["?"] * len(chunk))
                cur.execute(
                    f"UPDATE leads SET status = 'invalid' WHERE id IN ({placeholders})",
                    tuple(chunk),
                )
            conn.commit()
        finally:
            conn.close()

    return {
        "checked": len(rows),
        "invalid": len(bad_lead_ids),
        "domains_ok": len(domains_ok),
        "domains_bad": len(domains_bad),
        "sample_bad_domains": ", ".join(sorted(domains_bad)[:15]),
    }
