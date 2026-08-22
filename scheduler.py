"""Human-like daily send scheduling."""
import logging
import random
from collections import defaultdict
from datetime import date, datetime, timedelta, time
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from config import DAILY_CAP, FOLLOWUP_SCHEDULE, MIN_GAP_SECONDS, TIMEZONE
from db import get_conn
from followups import get_followup_body, is_final, step_to_wait_days
from generator import generate_for_lead
from industry_profiles import get_profile
from settings import get_auto_send_enabled, get_daily_send_cap

logger = logging.getLogger(__name__)


def _random_time_in_window(
    day: date,
    profile,
    peak_bias: float = 0.6,
    min_time: Optional[datetime] = None,
) -> Optional[datetime]:
    """Generate a random local datetime inside the industry window for the given day."""
    if not profile.days:
        return None

    start = datetime.combine(day, time(profile.start_hour, 0, 0), tzinfo=ZoneInfo(TIMEZONE))
    if profile.end_hour == 24:
        end = datetime.combine(day, time(23, 59, 59), tzinfo=ZoneInfo(TIMEZONE))
    else:
        end = datetime.combine(day, time(profile.end_hour, 0, 0), tzinfo=ZoneInfo(TIMEZONE))

    # If min_time is set (e.g. "now" when building mid-day), don't schedule in the past
    if min_time is not None and min_time > start:
        start = min_time

    if start >= end:
        return None

    use_peak = random.random() < peak_bias
    if use_peak and profile.peak_hours:
        peak_hour = random.choice(profile.peak_hours)
        if profile.start_hour <= peak_hour < profile.end_hour or profile.end_hour == 24:
            peak_start = datetime.combine(day, time(peak_hour, 0, 0), tzinfo=ZoneInfo(TIMEZONE))
            peak_end = peak_start + timedelta(hours=1) - timedelta(seconds=1)
            if peak_end > end:
                peak_end = end
            if peak_start < end:
                span = (peak_end - peak_start).total_seconds()
                if span > 0:
                    offset = random.uniform(0, span)
                    return peak_start + timedelta(seconds=offset)

    span = (end - start).total_seconds()
    if span <= 0:
        return None
    offset = random.uniform(0, span)
    return start + timedelta(seconds=offset)


def _select_diverse(leads: List, cap: int) -> List:
    """Pick `cap` leads while trying to maintain industry diversity."""
    if not leads:
        return []

    groups = defaultdict(list)
    for lead in leads:
        industry = (lead["industry"] or "other").lower()
        groups[industry].append(lead)

    for g in groups.values():
        random.shuffle(g)

    selected = []
    industries = list(groups.keys())
    random.shuffle(industries)
    idx = {ind: 0 for ind in industries}

    while len(selected) < cap:
        made_progress = False
        for industry in industries:
            if len(selected) >= cap:
                break
            group = groups[industry]
            i = idx[industry]
            if i < len(group):
                selected.append(group[i])
                idx[industry] = i + 1
                made_progress = True
        if not made_progress:
            break

    return selected


def _resolve_collisions(
    schedule: List[Tuple[int, datetime, datetime]],
) -> List[Tuple[int, datetime, datetime]]:
    """Enforce at least MIN_GAP between sends by shifting later sends randomly.

    Each tuple is (lead_id, scheduled_at, window_end).
    Sends that cannot fit inside the window are dropped.
    """
    if not schedule:
        return []

    min_gap = timedelta(seconds=MIN_GAP_SECONDS)
    sorted_schedule = sorted(schedule, key=lambda x: x[1])
    resolved: List[Tuple[int, datetime, datetime]] = []
    last_dt: Optional[datetime] = None

    for lead_id, dt, window_end in sorted_schedule:
        if last_dt is None:
            resolved.append((lead_id, dt, window_end))
            last_dt = dt
            continue

        if dt - last_dt >= min_gap:
            resolved.append((lead_id, dt, window_end))
            last_dt = dt
            continue

        # Shift by at least the min gap plus a random 0–90s jitter
        jitter = timedelta(seconds=random.uniform(0, 90))
        candidate = last_dt + min_gap + jitter

        if candidate > window_end:
            logger.info(
                "Lead %s dropped from today's schedule (would exceed window after collision resolution)",
                lead_id,
            )
            continue

        resolved.append((lead_id, candidate, window_end))
        last_dt = candidate

    return resolved


def build_daily_schedule(user_id: int, schedule_date: Optional[date] = None) -> int:
    """Build and write today's send schedule for one user. Returns number scheduled."""
    tz = ZoneInfo(TIMEZONE)
    if schedule_date is None:
        schedule_date = datetime.now(tz).date()

    today_iso = schedule_date.isoformat()
    state_key = f"user:{user_id}:last_schedule_date"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM state WHERE key = ?", (state_key,))
    row = cur.fetchone()
    if row and row["value"] == today_iso:
        logger.info("Daily schedule for user %s on %s already built.", user_id, today_iso)
        conn.close()
        return 0
    conn.close()

    # Auto-enrich new leads if auto_send is enabled
    auto_send = get_auto_send_enabled(user_id)
    if auto_send:
        try:
            import json as _json
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT * FROM leads WHERE status = 'new' AND user_id = ? ORDER BY id", (user_id,))
            new_leads = cur.fetchall()
            conn.close()

            # Batch-mark leads that already have an industry as enriched (no AI needed)
            leads_with_industry = [l for l in new_leads if (l["industry"] or "").strip()]
            if leads_with_industry:
                conn = get_conn()
                cur = conn.cursor()
                for lead in leads_with_industry:
                    cur.execute(
                        "UPDATE leads SET status = 'enriched' WHERE id = ?",
                        (lead["id"],),
                    )
                conn.commit()
                conn.close()
                logger.info("Auto-enriched (no AI) %d leads with existing industry for user %s", len(leads_with_industry), user_id)

            # AI-enrich leads without industry (limit per cycle to avoid overload)
            leads_without_industry = [l for l in new_leads if not (l["industry"] or "").strip()]
            if leads_without_industry:
                try:
                    from enricher import enrich_lead
                except Exception:
                    enrich_lead = None
                if enrich_lead:
                    for lead in leads_without_industry[:50]:
                        try:
                            data = enrich_lead(lead)
                            conn = get_conn()
                            cur = conn.cursor()
                            cur.execute(
                                """
                                UPDATE leads
                                SET industry = COALESCE(NULLIF(?, ''), industry),
                                    enriched_data = ?,
                                    status = 'enriched'
                                WHERE id = ?
                                """,
                                (data.get("industry", lead["industry"] or "other"), _json.dumps(data, ensure_ascii=False), lead["id"]),
                            )
                            conn.commit()
                            conn.close()
                        except Exception as e:
                            logger.warning("Auto-enrich failed for lead %s: %s", lead["id"], e)
                            # Mark as enriched with default industry so it's not stuck
                            conn = get_conn()
                            cur = conn.cursor()
                            cur.execute("UPDATE leads SET status = 'enriched', industry = 'other' WHERE id = ?", (lead["id"],))
                            conn.commit()
                            conn.close()
        except Exception as e:
            logger.warning("Auto-enrich setup error: %s", e)

    logger.info("Building schedule for user %s on %s", user_id, today_iso)

    # Use per-user daily cap, fall back to global DAILY_CAP
    daily_cap = get_daily_send_cap(user_id) if auto_send else DAILY_CAP

    # Count how many emails already sent today for this user
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE user_id = ? AND sent_at IS NOT NULL AND substr(sent_at, 1, 10) = ?",
        (user_id, today_iso),
    )
    sent_today = cur.fetchone()["n"]
    remaining_cap = max(0, daily_cap - sent_today)
    conn.close()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads WHERE status = 'enriched' AND user_id = ? ORDER BY id", (user_id,))
    initial_leads = cur.fetchall()
    cur.execute(
        "SELECT * FROM leads WHERE status = 'sent' AND sequence_step BETWEEN 1 AND 3 "
        "AND last_contact_at IS NOT NULL AND user_id = ? ORDER BY id",
        (user_id,),
    )
    followup_leads = cur.fetchall()
    conn.close()

    due: List = []

    for lead in initial_leads:
        industry = (lead["industry"] or "other").lower()
        profile = get_profile(industry)
        weekday = schedule_date.weekday()
        if weekday not in profile.days:
            continue
        due.append(lead)

    for lead in followup_leads:
        if is_final(lead["status"]):
            continue
        industry = (lead["industry"] or "other").lower()
        profile = get_profile(industry)
        weekday = schedule_date.weekday()
        if weekday not in profile.days:
            continue

        wait_days = step_to_wait_days(lead["sequence_step"])
        last_contact = datetime.fromisoformat(lead["last_contact_at"])
        due_date = (last_contact + timedelta(days=wait_days)).date()
        if due_date <= schedule_date:
            due.append(lead)

    if not due:
        logger.info("No leads are due on %s.", today_iso)
        _set_last_schedule_date(user_id, today_iso)
        return 0

    if remaining_cap <= 0:
        logger.info("Daily cap of %d already reached for user %s on %s", daily_cap, user_id, today_iso)
        _set_last_schedule_date(user_id, today_iso)
        return 0

    selected = _select_diverse(due, min(remaining_cap, len(due)))

    now_dt = datetime.now(tz)
    schedule: List[Tuple[int, datetime, datetime]] = []
    for lead in selected:
        industry = (lead["industry"] or "other").lower()
        profile = get_profile(industry)
        dt = _random_time_in_window(schedule_date, profile, min_time=now_dt)
        if not dt:
            continue

        # Skip pre-generation to avoid OpenAI bottleneck with large batches.
        # Emails will be generated on-demand at send time.

        window_end = datetime.combine(
            schedule_date,
            time(23, 59, 59) if profile.end_hour == 24 else time(profile.end_hour, 0, 0),
            tzinfo=tz,
        )
        schedule.append((lead["id"], dt, window_end))

    resolved = _resolve_collisions(schedule)
    scheduled_ids = [item[0] for item in resolved]

    if not scheduled_ids:
        _set_last_schedule_date(user_id, today_iso)
        return 0

    conn = get_conn()
    cur = conn.cursor()
    for lead_id, dt, _ in resolved:
        cur.execute(
            "UPDATE leads SET status = 'scheduled', scheduled_at = ? WHERE id = ?",
            (dt.isoformat(), lead_id),
        )
    conn.commit()
    conn.close()

    _set_last_schedule_date(user_id, today_iso)
    logger.info("Scheduled %d emails for user %s on %s", len(resolved), user_id, today_iso)
    return len(resolved)


def _set_last_schedule_date(user_id: int, today_iso: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"user:{user_id}:last_schedule_date", today_iso),
    )
    conn.commit()
    conn.close()
