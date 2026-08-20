"""Web dashboard for campaign-engine."""
import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from config import DASHBOARD_HOST, DASHBOARD_PORT, DB_PATH, FOLLOWUP_SCHEDULE, TIMEZONE
from db import get_conn, init_db
from send_batch import pick_emails
from settings import get_public_settings, update_settings

app = FastAPI(title="Campaign Engine Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _lead_filters(industry: Optional[str], start: Optional[str], end: Optional[str], date_col: str = "sent_at") -> tuple:
    where: List[str] = []
    params: List[Any] = []
    if industry:
        where.append("industry = ?")
        params.append(industry.lower())
    if start and end:
        where.append(f"{date_col} >= ? AND {date_col} <= ?")
        params.append(f"{start}T00:00:00")
        params.append(f"{end}T23:59:59")
    return where, params


def _where_clause(where: List[str]) -> str:
    return f"WHERE {' AND '.join(where)}" if where else ""


@app.get("/api/data")
def api_data(
    industry: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    conn = get_conn()
    try:
        return _api_data_impl(conn, industry, start, end)
    finally:
        conn.close()


def _api_data_impl(conn, industry: Optional[str], start: Optional[str], end: Optional[str]) -> dict:
    cur = conn.cursor()

    # Stats
    where, params = _lead_filters(industry, start, end)
    ind_where: List[str] = []
    ind_params: List[Any] = []
    if industry:
        ind_where.append("industry = ?")
        ind_params.append(industry.lower())
    total = cur.execute(f"SELECT COUNT(*) FROM leads {_where_clause(ind_where)}", ind_params).fetchone()[0]

    # Total sends (any sent_at)
    where2, params2 = _lead_filters(industry, start, end, "sent_at")
    where2.append("sent_at IS NOT NULL")
    sent = cur.execute(f"SELECT COUNT(*) FROM leads {_where_clause(where2)}", params2).fetchone()[0]

    bounces = cur.execute(
        f"SELECT COUNT(*) FROM leads {_where_clause(ind_where + ['status = \'bounced\''])}",
        list(ind_params),
    ).fetchone()[0]

    replies = cur.execute(
        f"SELECT COUNT(*) FROM leads {_where_clause(ind_where + ['status = \'replied\''])}",
        list(ind_params),
    ).fetchone()[0]

    unsubscribes = cur.execute(
        f"SELECT COUNT(*) FROM leads {_where_clause(ind_where + ['status = \'unsubscribed\''])}",
        list(ind_params),
    ).fetchone()[0]

    delivered = max(sent - bounces, 0)
    reply_rate = (replies / delivered * 100) if delivered > 0 else 0.0

    # Trend: sends per day last 30 days
    since = (datetime.now() - timedelta(days=30)).isoformat()
    trend_where = ["sent_at >= ?"]
    trend_params: List[Any] = [since]
    if industry:
        trend_where.append("industry = ?")
        trend_params.append(industry.lower())
    cur.execute(
        f"SELECT substr(sent_at, 1, 10) AS day, COUNT(*) AS n FROM leads {_where_clause(trend_where)} GROUP BY day ORDER BY day",
        trend_params,
    )
    trend = [{"day": row["day"], "count": row["n"]} for row in cur.fetchall()]

    # Replies by industry
    cur.execute(
        f"SELECT COALESCE(NULLIF(industry, ''), 'other') AS industry, COUNT(*) AS n FROM leads WHERE status = 'replied' {('AND industry = ?' if industry else '')} GROUP BY industry ORDER BY n DESC",
        ([industry.lower()] if industry else []),
    )
    industry_replies = [{"industry": row["industry"], "count": row["n"]} for row in cur.fetchall()]

    # Funnel
    enriched_where = ind_where + [
        "status IN ('enriched', 'scheduled', 'sent', 'completed', 'replied', 'bounced', 'unsubscribed')"
    ]
    enriched = cur.execute(
        f"SELECT COUNT(*) FROM leads {_where_clause(enriched_where)}", list(ind_params)
    ).fetchone()[0]
    funnel = {"leads": total, "enriched": enriched, "sent": sent, "replied": replies}

    # Sequence counts
    seq_where = ind_where + ["status NOT IN ('bounced', 'unsubscribed')"]
    cur.execute(
        f"SELECT sequence_step, COUNT(*) AS n FROM leads {_where_clause(seq_where)} GROUP BY sequence_step ORDER BY sequence_step",
        list(ind_params),
    )
    seq_map = {0: "Initial", 1: "FU1", 2: "FU2", 3: "FU3", 4: "Completed"}
    sequence = []
    for row in cur.fetchall():
        step = row["sequence_step"] or 0
        sequence.append({"step": step, "label": seq_map.get(step, f"Step {step}"), "count": row["n"]})
    for step, label in seq_map.items():
        if not any(s["step"] == step for s in sequence):
            sequence.append({"step": step, "label": label, "count": 0})
    sequence.sort(key=lambda x: x["step"])

    # Recent activity
    activity_where = []
    activity_params: List[Any] = []
    if industry:
        activity_where.append("l.industry = ?")
        activity_params.append(industry.lower())
    if start and end:
        activity_where.append("substr(e.created_at, 1, 10) >= ? AND substr(e.created_at, 1, 10) <= ?")
        activity_params.extend([start, end])
    sql = f"""
        SELECT e.created_at, e.event_type, e.details, l.company_name, l.industry
        FROM events e
        JOIN leads l ON e.lead_id = l.id
        {_where_clause(activity_where)}
        ORDER BY e.created_at DESC
        LIMIT 50
    """
    cur.execute(sql, activity_params)
    activity = [dict(row) for row in cur.fetchall()]

    # Replies inbox
    reply_where = ["(status = 'replied' OR reply_snippet IS NOT NULL)"]
    reply_params: List[Any] = []
    if industry:
        reply_where.append("industry = ?")
        reply_params.append(industry.lower())
    if start and end:
        reply_where.append("substr(last_contact_at, 1, 10) >= ? AND substr(last_contact_at, 1, 10) <= ?")
        reply_params.extend([start, end])
    cur.execute(
        f"""
        SELECT id, company_name, industry, last_contact_at, reply_snippet, is_customer
        FROM leads
        {_where_clause(reply_where)}
        ORDER BY last_contact_at DESC
        LIMIT 50
        """,
        reply_params,
    )
    replies_inbox = [dict(row) for row in cur.fetchall()]

    # Health
    from sender import _trailing_bounce_rate, is_paused

    bounce_rate = _trailing_bounce_rate()
    paused = is_paused()
    health = {
        "ok": not paused and bounce_rate < 0.03,
        "paused": paused,
        "bounce_rate": bounce_rate,
        "message": "Campaign healthy" if (not paused and bounce_rate < 0.03) else ("Paused" if paused else f"Bounce rate {bounce_rate:.2%}"),
    }

    return {
        "stats": {
            "total": total,
            "sent": sent,
            "delivered": delivered,
            "bounced": bounces,
            "replies": replies,
            "reply_rate": round(reply_rate, 2),
            "unsubscribed": unsubscribes,
        },
        "trend": trend,
        "industry_replies": industry_replies,
        "funnel": funnel,
        "sequence": sequence,
        "activity": activity,
        "replies": replies_inbox,
        "health": health,
    }


@app.post("/api/mark-customer/{lead_id}")
def mark_customer(lead_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE leads SET is_customer = 1 WHERE id = ?", (lead_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


STEP_LABELS = {0: "Not sent", 1: "Follow-up 1", 2: "Follow-up 2", 3: "Follow-up 3", 4: "Completed"}


@app.get("/api/companies")
def api_companies(
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    industry: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
):
    conn = get_conn()
    try:
        cur = conn.cursor()

        where: List[str] = []
        params: List[Any] = []
        if search:
            where.append("(company_name LIKE ? OR email LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like])
        if status:
            where.append("status = ?")
            params.append(status)
        if industry:
            where.append("industry = ?")
            params.append(industry.lower())

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        total = cur.execute(f"SELECT COUNT(*) FROM leads {where_sql}", params).fetchone()[0]

        offset = (page - 1) * page_size
        cur.execute(
            f"""
            SELECT id, company_name, email, industry, location, status, sequence_step,
                   last_contact_at, sent_at, scheduled_at, reply_snippet, is_customer
            FROM leads
            {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, offset],
        )
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            step = d.get("sequence_step") or 0
            d["step_label"] = STEP_LABELS.get(step, f"Step {step}")
            rows.append(d)
    finally:
        conn.close()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "rows": rows,
    }


@app.get("/api/industries")
def api_industries():
    """Distinct industries actually present in the leads table (for filter dropdowns)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT industry FROM leads WHERE industry IS NOT NULL AND industry != '' ORDER BY industry"
        )
        rows = [r["industry"] for r in cur.fetchall()]
    finally:
        conn.close()
    return {"industries": rows}


class DeleteIds(BaseModel):
    ids: List[int]


@app.delete("/api/companies/{lead_id}")
def api_delete_company(lead_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM events WHERE lead_id = ?", (lead_id,))
        cur.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"ok": True, "deleted": deleted}


@app.post("/api/companies/delete-bulk")
def api_delete_companies_bulk(body: DeleteIds):
    if not body.ids:
        return {"ok": True, "deleted": 0}
    conn = get_conn()
    try:
        cur = conn.cursor()
        placeholders = ", ".join(["?"] * len(body.ids))
        cur.execute(f"DELETE FROM events WHERE lead_id IN ({placeholders})", body.ids)
        cur.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", body.ids)
        deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": deleted}


@app.post("/api/companies/delete-all")
def api_delete_companies_all(
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    industry: Optional[str] = Query(None),
):
    """Delete ALL companies, optionally scoped to the current search/status/industry filter."""
    conn = get_conn()
    try:
        cur = conn.cursor()

        where: List[str] = []
        params: List[Any] = []
        if search:
            where.append("(company_name LIKE ? OR email LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like])
        if status:
            where.append("status = ?")
            params.append(status)
        if industry:
            where.append("industry = ?")
            params.append(industry.lower())
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        cur.execute(f"SELECT id FROM leads {where_sql}", params)
        ids = [r["id"] for r in cur.fetchall()]
        if ids:
            placeholders = ", ".join(["?"] * len(ids))
            cur.execute(f"DELETE FROM events WHERE lead_id IN ({placeholders})", ids)
            cur.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", ids)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "deleted": len(ids)}


@app.get("/api/followups")
def api_followups():
    """Leads awaiting their next follow-up, with the computed due date."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, company_name, email, industry, status, sequence_step, last_contact_at
            FROM leads
            WHERE status = 'sent' AND sequence_step BETWEEN 1 AND 3 AND last_contact_at IS NOT NULL
            ORDER BY last_contact_at ASC
            """
        )
        rows = cur.fetchall()

        # Also surface anything already queued (status='scheduled') with step > 0
        cur.execute(
            """
            SELECT id, company_name, email, industry, status, sequence_step, scheduled_at
            FROM leads
            WHERE status = 'scheduled' AND sequence_step > 0
            ORDER BY scheduled_at ASC
            """
        )
        queued_rows = cur.fetchall()
    finally:
        conn.close()

    pending = []
    for row in rows:
        step = row["sequence_step"] or 0
        wait_days = FOLLOWUP_SCHEDULE[step - 1] if 1 <= step <= len(FOLLOWUP_SCHEDULE) else 0
        last_contact = datetime.fromisoformat(row["last_contact_at"])
        due_date = last_contact + timedelta(days=wait_days)
        now = datetime.now(due_date.tzinfo) if due_date.tzinfo else datetime.now()
        pending.append(
            {
                "id": row["id"],
                "company_name": row["company_name"],
                "email": row["email"],
                "industry": row["industry"],
                "step": step,
                "step_label": STEP_LABELS.get(step, f"Step {step}"),
                "due_at": due_date.isoformat(),
                "is_due": due_date <= now,
            }
        )

    queued = []
    for row in queued_rows:
        step = row["sequence_step"] or 0
        queued.append(
            {
                "id": row["id"],
                "company_name": row["company_name"],
                "email": row["email"],
                "industry": row["industry"],
                "step": step,
                "step_label": STEP_LABELS.get(step, f"Step {step}"),
                "scheduled_at": row["scheduled_at"],
            }
        )

    pending.sort(key=lambda x: x["due_at"])
    return {"pending": pending, "queued": queued}


@app.post("/api/send-now/{lead_id}")
def api_send_now(lead_id: int):
    from sender import send_lead_now

    try:
        result = send_lead_now(lead_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "result": result}


def _rows_from_excel(raw: bytes) -> List[Dict[str, str]]:
    """Parse the first sheet of an .xlsx/.xls file into a list of dict rows,
    using the first non-empty row as the header (same shape as csv.DictReader)."""
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not read this Excel file. Legacy .xls files aren't supported - "
                "please re-save as .xlsx or .csv. "
                f"({e})"
            ),
        )
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = None
    for r in rows_iter:
        if any(c is not None and str(c).strip() for c in r):
            header = [str(c).strip() if c is not None else "" for c in r]
            break
    if not header:
        raise HTTPException(status_code=400, detail="Excel sheet has no header row")

    out = []
    for r in rows_iter:
        if all(c is None or str(c).strip() == "" for c in r):
            continue
        row = {}
        for i, col_name in enumerate(header):
            if not col_name:
                continue
            val = r[i] if i < len(r) else None
            row[col_name] = "" if val is None else str(val).strip()
        out.append(row)
    return out


def _rows_from_csv_text(text: str) -> List[Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV has no header row")
    return list(reader)


@app.post("/api/import-csv")
async def api_import_csv(file: UploadFile = File(...)):
    """Upload a CSV or Excel (.xlsx) file of companies and upsert them into
    the DB (status='new').

    Recognised columns (best match wins, case-insensitive):
      Company Name, Industry, HR Email, Recruitment Email, General Company Email
      (falls back to a generic 'email'/'Email' column if none of the above exist)
    """
    init_db()
    raw = await file.read()
    filename = (file.filename or "").lower()

    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        rows = _rows_from_excel(raw)
    else:
        text = raw.decode("utf-8-sig", errors="ignore")
        rows = _rows_from_csv_text(text)

    imported, updated, skipped = 0, 0, 0
    conn = get_conn()
    try:
        cur = conn.cursor()
        for row in rows:
            company = (row.get("Company Name") or row.get("company_name") or row.get("Company") or "").strip()
            industry = (
                row.get("Industry") or row.get("industry")
                or row.get("Category") or row.get("category")
                or ""
            ).strip() or "other"

            emails = pick_emails(row)
            if not emails:
                fallback = (row.get("Email") or row.get("email") or row.get("E-mail") or "").strip()
                if fallback:
                    emails = [fallback]

            if not company or not emails:
                skipped += 1
                continue

            primary = emails[0]
            cur.execute("SELECT id FROM leads WHERE email = ?", (primary,))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE leads SET company_name = ?, industry = ? WHERE id = ?",
                    (company, industry.lower(), existing["id"]),
                )
                updated += 1
            else:
                cur.execute(
                    "INSERT INTO leads (company_name, email, industry, status) VALUES (?, ?, ?, 'new')",
                    (company, primary, industry.lower()),
                )
                imported += 1
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "imported": imported, "updated": updated, "skipped": skipped}


class SettingsUpdate(BaseModel):
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    from_alias: Optional[str] = None
    from_display_name: Optional[str] = None


@app.get("/api/settings")
def api_get_settings():
    return get_public_settings()


@app.post("/api/settings")
def api_update_settings(body: SettingsUpdate):
    update_settings(
        smtp_user=body.smtp_user,
        smtp_password=body.smtp_password,
        from_alias=body.from_alias,
        from_display_name=body.from_display_name,
    )
    return {"ok": True, "settings": get_public_settings()}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Campaign Engine Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root {
    --bg: #f4f6fb;
    --surface: #ffffff;
    --border: #e6e9f0;
    --text: #1f2430;
    --muted: #6b7280;
    --primary: #4f46e5;
    --primary-dark: #4338ca;
    --accent: #06b6d4;
    --green: #16a34a;
    --red: #dc2626;
    --amber: #d97706;
    --radius: 12px;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Arial, sans-serif;
    margin: 0; padding: 24px; background: var(--bg); color: var(--text);
  }
  h1 { margin: 0 0 4px; font-size: 1.6em; font-weight: 700; letter-spacing: -0.02em; }
  h1 .sub { display: block; font-size: 0.5em; font-weight: 500; color: var(--muted); margin-top: 4px; letter-spacing: 0; }
  h3 { margin: 0 0 12px; font-size: 1.02em; font-weight: 600; }
  .filters { margin-bottom: 15px; display: flex; gap: 8px; flex-wrap: wrap; }
  .filters select, .filters input { padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface); font-size: 0.9em; }
  .banner { padding: 12px 16px; border-radius: var(--radius); margin-bottom: 20px; font-weight: 600; border: 1px solid transparent; }
  .banner.green { background: #ecfdf5; color: #065f46; border-color: #a7f3d0; }
  .banner.red { background: #fef2f2; color: #991b1b; border-color: #fecaca; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-bottom: 20px; }
  .card { background: var(--surface); padding: 18px; border-radius: var(--radius); box-shadow: 0 1px 2px rgba(16,24,40,0.05); border: 1px solid var(--border); text-align: center; transition: transform 0.15s; }
  .card .value { font-size: 1.9em; font-weight: 700; color: var(--text); }
  .card .label { font-size: 0.82em; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; }
  .section { background: var(--surface); padding: 20px; border-radius: var(--radius); box-shadow: 0 1px 2px rgba(16,24,40,0.05); border: 1px solid var(--border); margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88em; }
  th, td { padding: 10px 8px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 0.82em; text-transform: uppercase; letter-spacing: 0.03em; }
  tr:hover td { background: #fafbff; }
  .btn { padding: 8px 14px; background: var(--primary); color: #fff; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 0.88em; transition: background 0.15s; }
  .btn:hover { background: var(--primary-dark); }
  .btn:disabled { background: #c7c9d1; cursor: default; }
  .btn.secondary { background: var(--surface); color: var(--text); border: 1px solid var(--border); }
  .btn.secondary:hover { background: #f3f4f8; }
  .btn.small { padding: 5px 10px; font-size: 0.82em; }
  .btn.danger { background: var(--red); }
  .snippet { max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tabs { display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 1px solid var(--border); }
  .tab-btn { padding: 10px 18px; background: none; border: none; cursor: pointer; font-size: 0.92em; font-weight: 500; color: var(--muted); border-bottom: 2px solid transparent; margin-bottom: -1px; }
  .tab-btn.active { color: var(--primary); font-weight: 700; border-bottom-color: var(--primary); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 0.76em; font-weight: 700; }
  .badge.new, .badge.enriched { background: #e0e7ff; color: #4338ca; }
  .badge.scheduled { background: #fef3c7; color: #92400e; }
  .badge.sent { background: #cffafe; color: #0e7490; }
  .badge.completed { background: #dcfce7; color: #166534; }
  .badge.replied { background: #dcfce7; color: #166534; }
  .badge.bounced, .badge.unsubscribed, .badge.enrichment_failed { background: #fee2e2; color: #991b1b; }
  .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
  .toolbar input, .toolbar select { padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: #fafbfc; font-size: 0.88em; }
  .pager { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
  .toast { position: fixed; top: 16px; right: 16px; padding: 12px 18px; border-radius: 8px; color: #fff; font-weight: 600; z-index: 999; opacity: 0; transition: opacity 0.3s; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
  .toast.show { opacity: 1; }
  .toast.ok { background: var(--green); }
  .toast.err { background: var(--red); }
  .hint { color: var(--muted); font-size: 0.85em; margin: 4px 0 12px; line-height: 1.5; }
  .hint code { background: #f1f2f7; padding: 1px 5px; border-radius: 4px; font-size: 0.95em; }
  .col-list { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 14px; }
  .col-chip { background: #eef2ff; color: #4338ca; padding: 3px 10px; border-radius: 6px; font-size: 0.8em; font-weight: 600; }
  .form-row { display: flex; flex-direction: column; gap: 6px; margin-bottom: 16px; max-width: 420px; }
  .form-row label { font-size: 0.85em; font-weight: 600; color: var(--text); }
  .form-row input { padding: 9px 12px; border: 1px solid var(--border); border-radius: 8px; font-size: 0.92em; }
  .form-row .desc { font-size: 0.8em; color: var(--muted); }
  @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
  <h1>Campaign Engine<span class="sub">Outreach automation dashboard</span></h1>

  <div class="tabs">
    <button class="tab-btn active" data-tab="overview" onclick="switchTab('overview')">Overview</button>
    <button class="tab-btn" data-tab="companies" onclick="switchTab('companies')">Companies</button>
    <button class="tab-btn" data-tab="followups" onclick="switchTab('followups')">Follow-ups</button>
    <button class="tab-btn" data-tab="replies" onclick="switchTab('replies')">Replies</button>
    <button class="tab-btn" data-tab="settings" onclick="switchTab('settings')">Settings</button>
  </div>

  <div id="toast" class="toast"></div>

  <!-- ===================== OVERVIEW ===================== -->
  <div class="tab-panel active" id="panel-overview">
    <div class="filters">
      <select id="industry"><option value="">All industries</option></select>
      <input type="date" id="start" />
      <input type="date" id="end" />
      <button class="btn secondary" onclick="load()">Filter</button>
    </div>

    <div id="health" class="banner">Loading...</div>

    <div class="cards" id="cards"></div>

    <div class="grid">
      <div class="section">
        <h3>Sent emails per day (last 30 days)</h3>
        <canvas id="trendChart" height="120"></canvas>
      </div>
      <div class="section">
        <h3>Replies by industry</h3>
        <canvas id="industryChart" height="120"></canvas>
      </div>
    </div>

    <div class="grid">
      <div class="section">
        <h3>Funnel</h3>
        <div id="funnel"></div>
      </div>
      <div class="section">
        <h3>Follow-up sequence</h3>
        <table>
          <thead><tr><th>Step</th><th>Count</th></tr></thead>
          <tbody id="sequence"></tbody>
        </table>
      </div>
    </div>

    <div class="section">
      <h3>Recent activity (last 50)</h3>
      <table>
        <thead><tr><th>Time</th><th>Event</th><th>Company</th><th>Industry</th><th>Details</th></tr></thead>
        <tbody id="activity"></tbody>
      </table>
    </div>
  </div>

  <!-- ===================== COMPANIES ===================== -->
  <div class="tab-panel" id="panel-companies">
    <div class="section">
      <h3>Upload companies (CSV or Excel)</h3>
      <p class="hint">
        Accepts <code>.csv</code>, <code>.xlsx</code>, or <code>.xls</code> &mdash; Excel files are converted internally so
        both formats work the same way. Your file needs a company name column and at least one email column. Recognised
        column names (case-insensitive):
      </p>
      <div class="col-list">
        <span class="col-chip">Company Name</span>
        <span class="col-chip">Industry / Category</span>
        <span class="col-chip">HR Email</span>
        <span class="col-chip">Recruitment Email</span>
        <span class="col-chip">General Company Email</span>
        <span class="col-chip">Email</span>
      </div>
      <p class="hint">
        Only <code>Company Name</code> + one valid email are required &mdash; everything else is optional. Rows with an email
        already in the system are updated in place, not duplicated. Uploaded companies appear below with status
        <code>new</code> and a <strong>Send now</strong> button.
      </p>
      <input type="file" id="csvFile" accept=".csv,.xlsx,.xls" style="display:none" onchange="uploadCsv()" />
      <button class="btn" onclick="document.getElementById('csvFile').click()">Choose File &amp; Upload</button>
    </div>
    <div class="section">
      <div class="toolbar">
        <input type="text" id="companySearch" placeholder="Search company or email..." style="min-width:260px" onkeyup="if(event.key==='Enter') loadCompanies(1)" />
        <select id="companyStatus">
          <option value="">All statuses</option>
          <option value="new">new</option>
          <option value="enriched">enriched</option>
          <option value="scheduled">scheduled</option>
          <option value="sent">sent</option>
          <option value="completed">completed</option>
          <option value="replied">replied</option>
          <option value="bounced">bounced</option>
          <option value="unsubscribed">unsubscribed</option>
        </select>
        <select id="companyIndustry"><option value="">All industries</option></select>
        <button class="btn secondary" onclick="loadCompanies(1)">Search</button>
        <span style="flex:1"></span>
        <button class="btn small danger" id="deleteSelectedBtn" onclick="deleteSelectedCompanies()" disabled>Delete selected (<span id="selectedCount">0</span>)</button>
        <button class="btn small danger" onclick="deleteAllCompanies()">Delete ALL matching filter</button>
      </div>
      <table>
        <thead>
          <tr>
            <th><input type="checkbox" id="selectAllCompanies" onchange="toggleSelectAllCompanies(this)" /></th>
            <th>Company</th><th>Email</th><th>Industry</th><th>Status</th><th>Sequence</th>
            <th>Last Contact</th><th>Scheduled</th><th></th><th></th>
          </tr>
        </thead>
        <tbody id="companiesTable"></tbody>
      </table>
      <div class="pager">
        <button class="btn small secondary" onclick="companyPage(-1)">&laquo; Prev</button>
        <span id="companyPageInfo"></span>
        <button class="btn small secondary" onclick="companyPage(1)">Next &raquo;</button>
      </div>
    </div>
  </div>

  <!-- ===================== FOLLOW-UPS ===================== -->
  <div class="tab-panel" id="panel-followups">
    <div class="section">
      <h3>Due / upcoming follow-ups</h3>
      <table>
        <thead><tr><th>Company</th><th>Email</th><th>Industry</th><th>Step</th><th>Due at</th><th>Status</th><th></th></tr></thead>
        <tbody id="followupsPending"></tbody>
      </table>
    </div>
    <div class="section">
      <h3>Queued (already scheduled)</h3>
      <table>
        <thead><tr><th>Company</th><th>Email</th><th>Industry</th><th>Step</th><th>Scheduled at</th></tr></thead>
        <tbody id="followupsQueued"></tbody>
      </table>
    </div>
  </div>

  <!-- ===================== REPLIES ===================== -->
  <div class="tab-panel" id="panel-replies">
    <div class="section">
      <h3>Replies inbox</h3>
      <table>
        <thead><tr><th>Company</th><th>Industry</th><th>Date</th><th>Snippet</th><th></th></tr></thead>
        <tbody id="replies"></tbody>
      </table>
    </div>
  </div>

  <!-- ===================== SETTINGS ===================== -->
  <div class="tab-panel" id="panel-settings">
    <div class="section">
      <h3>Sender identity</h3>
      <p class="hint">
        Controls which Gmail account authenticates the SMTP connection, and which address/name recipients see as the
        sender. Changing these takes effect immediately &mdash; no redeploy needed. Leave the password field blank to
        keep the currently configured password.
      </p>
      <div class="form-row">
        <label for="setSmtpUser">SMTP login email (Gmail account)</label>
        <input type="email" id="setSmtpUser" placeholder="you@gmail.com" />
        <span class="desc">The Gmail account that authenticates with Google's SMTP server.</span>
      </div>
      <div class="form-row">
        <label for="setSmtpPassword">Gmail App Password</label>
        <input type="password" id="setSmtpPassword" placeholder="Leave blank to keep current password" autocomplete="new-password" />
        <span class="desc" id="smtpPasswordStatus">Not set</span>
      </div>
      <div class="form-row">
        <label for="setFromAlias">Send-from address (alias)</label>
        <input type="email" id="setFromAlias" placeholder="e.g. careers@yourcompany.com" />
        <span class="desc">Optional. Must be a registered "Send mail as" alias on the Gmail account above. Leave blank to send from the SMTP login email itself.</span>
      </div>
      <div class="form-row">
        <label for="setFromDisplayName">Display name</label>
        <input type="text" id="setFromDisplayName" placeholder="e.g. AP Online Jobs" />
        <span class="desc">Shown as the sender's name in the recipient's inbox.</span>
      </div>
      <button class="btn" onclick="saveSettings()">Save settings</button>
    </div>
  </div>

  <script>
    let trendChart, industryChart;
    let companyPageNum = 1;
    let companyPageSize = 25;
    let companyTotal = 0;

    function switchTab(tab) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + tab));
      if (tab === 'companies') loadCompanies(companyPageNum);
      if (tab === 'followups') loadFollowups();
      if (tab === 'settings') loadSettings();
    }

    async function loadSettings() {
      const res = await fetch('/api/settings');
      const s = await res.json();
      document.getElementById('setSmtpUser').value = s.smtp_user || '';
      document.getElementById('setFromAlias').value = s.from_alias || '';
      document.getElementById('setFromDisplayName').value = s.from_display_name || '';
      document.getElementById('smtpPasswordStatus').textContent = s.smtp_password_set
        ? 'A password is currently configured. Leave blank to keep it.'
        : 'Not set yet.';
    }

    async function saveSettings() {
      const body = {
        smtp_user: document.getElementById('setSmtpUser').value.trim(),
        smtp_password: document.getElementById('setSmtpPassword').value,
        from_alias: document.getElementById('setFromAlias').value.trim(),
        from_display_name: document.getElementById('setFromDisplayName').value.trim(),
      };
      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Save failed');
        showToast('Settings saved', true);
        document.getElementById('setSmtpPassword').value = '';
        loadSettings();
      } catch (e) {
        showToast(e.message, false);
      }
    }

    function showToast(msg, ok) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.className = 'toast show ' + (ok ? 'ok' : 'err');
      setTimeout(() => { t.className = 'toast'; }, 3500);
    }

    async function sendNow(leadId, btn) {
      btn.disabled = true;
      const original = btn.textContent;
      btn.textContent = 'Sending...';
      try {
        const res = await fetch('/api/send-now/' + leadId, { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Send failed');
        showToast('Sent to ' + data.result.company_name + ' (' + data.result.subject + ')', true);
        loadCompanies(companyPageNum);
        loadFollowups();
      } catch (e) {
        showToast(e.message, false);
        btn.disabled = false;
        btn.textContent = original;
      }
    }

    function statusBadge(status) {
      return `<span class="badge ${status}">${status}</span>`;
    }

    async function uploadCsv() {
      const input = document.getElementById('csvFile');
      const file = input.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append('file', file);
      try {
        const res = await fetch('/api/import-csv', { method: 'POST', body: formData });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Upload failed');
        showToast(`Imported ${data.imported} new, updated ${data.updated}, skipped ${data.skipped}`, true);
        loadCompanies(1);
        loadIndustries();
      } catch (e) {
        showToast(e.message, false);
      } finally {
        input.value = '';
      }
    }

    function companyFilterParams() {
      const params = new URLSearchParams();
      const search = document.getElementById('companySearch').value.trim();
      const status = document.getElementById('companyStatus').value;
      const industry = document.getElementById('companyIndustry').value;
      if (search) params.set('search', search);
      if (status) params.set('status', status);
      if (industry) params.set('industry', industry);
      return params;
    }

    async function loadCompanies(page) {
      companyPageNum = page || companyPageNum;
      const params = companyFilterParams();
      params.set('page', companyPageNum);
      params.set('page_size', companyPageSize);

      const res = await fetch('/api/companies?' + params.toString());
      const d = await res.json();
      companyTotal = d.total;

      document.getElementById('companiesTable').innerHTML = d.rows.map(r => {
        const canSend = !['replied', 'bounced', 'unsubscribed'].includes(r.status);
        return `<tr>
          <td><input type="checkbox" class="companyRowCheck" value="${r.id}" onchange="updateSelectedCount()" /></td>
          <td>${r.company_name || ''}</td>
          <td>${r.email || ''}</td>
          <td>${r.industry || ''}</td>
          <td>${statusBadge(r.status || 'new')}</td>
          <td>${r.step_label}</td>
          <td>${r.last_contact_at ? r.last_contact_at.substring(0,16) : ''}</td>
          <td>${r.scheduled_at ? r.scheduled_at.substring(0,16) : ''}</td>
          <td>${canSend ? `<button class="btn small" onclick="sendNow(${r.id}, this)">Send now</button>` : ''}</td>
          <td><button class="btn small danger" onclick="deleteCompany(${r.id}, this)">Delete</button></td>
        </tr>`;
      }).join('');

      const totalPages = Math.max(1, Math.ceil(companyTotal / companyPageSize));
      document.getElementById('companyPageInfo').textContent =
        `Page ${companyPageNum} of ${totalPages} (${companyTotal} total)`;

      document.getElementById('selectAllCompanies').checked = false;
      updateSelectedCount();
    }

    function companyPage(delta) {
      const totalPages = Math.max(1, Math.ceil(companyTotal / companyPageSize));
      const next = Math.min(totalPages, Math.max(1, companyPageNum + delta));
      loadCompanies(next);
    }

    function toggleSelectAllCompanies(checkbox) {
      document.querySelectorAll('.companyRowCheck').forEach(c => c.checked = checkbox.checked);
      updateSelectedCount();
    }

    function updateSelectedCount() {
      const checked = document.querySelectorAll('.companyRowCheck:checked');
      document.getElementById('selectedCount').textContent = checked.length;
      document.getElementById('deleteSelectedBtn').disabled = checked.length === 0;
    }

    async function deleteCompany(id, btn) {
      if (!confirm('Delete this company? This cannot be undone.')) return;
      btn.disabled = true;
      try {
        const res = await fetch('/api/companies/' + id, { method: 'DELETE' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Delete failed');
        showToast('Company deleted', true);
        loadCompanies(companyPageNum);
      } catch (e) {
        showToast(e.message, false);
        btn.disabled = false;
      }
    }

    async function deleteSelectedCompanies() {
      const ids = Array.from(document.querySelectorAll('.companyRowCheck:checked')).map(c => Number(c.value));
      if (!ids.length) return;
      if (!confirm(`Delete ${ids.length} selected companies? This cannot be undone.`)) return;
      try {
        const res = await fetch('/api/companies/delete-bulk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Delete failed');
        showToast(`Deleted ${data.deleted} companies`, true);
        loadCompanies(1);
        loadIndustries();
      } catch (e) {
        showToast(e.message, false);
      }
    }

    async function deleteAllCompanies() {
      const params = companyFilterParams();
      const hasFilter = params.toString().length > 0;
      const msg = hasFilter
        ? `Delete ALL companies matching the current search/status/industry filter (${companyTotal} total)? This cannot be undone.`
        : `Delete ALL ${companyTotal} companies in the database? This cannot be undone.`;
      if (!confirm(msg)) return;
      try {
        const res = await fetch('/api/companies/delete-all?' + params.toString(), { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Delete failed');
        showToast(`Deleted ${data.deleted} companies`, true);
        loadCompanies(1);
        loadIndustries();
      } catch (e) {
        showToast(e.message, false);
      }
    }

    async function loadFollowups() {
      const res = await fetch('/api/followups');
      const d = await res.json();

      document.getElementById('followupsPending').innerHTML = d.pending.map(r => `
        <tr>
          <td>${r.company_name || ''}</td>
          <td>${r.email || ''}</td>
          <td>${r.industry || ''}</td>
          <td>${r.step_label}</td>
          <td>${r.due_at.substring(0,16)}</td>
          <td>${r.is_due ? '<span class="badge scheduled">Due now</span>' : '<span class="badge new">Upcoming</span>'}</td>
          <td><button class="btn small" onclick="sendNow(${r.id}, this)">Send now</button></td>
        </tr>`).join('') || '<tr><td colspan="7">No pending follow-ups.</td></tr>';

      document.getElementById('followupsQueued').innerHTML = d.queued.map(r => `
        <tr>
          <td>${r.company_name || ''}</td>
          <td>${r.email || ''}</td>
          <td>${r.industry || ''}</td>
          <td>${r.step_label}</td>
          <td>${r.scheduled_at ? r.scheduled_at.substring(0,16) : ''}</td>
        </tr>`).join('') || '<tr><td colspan="5">Nothing queued.</td></tr>';
    }

    function qs() {
      const params = new URLSearchParams();
      const industry = document.getElementById('industry').value;
      const start = document.getElementById('start').value;
      const end = document.getElementById('end').value;
      if (industry) params.set('industry', industry);
      if (start) params.set('start', start);
      if (end) params.set('end', end);
      return params.toString();
    }

    async function load() {
      const q = qs() ? '?' + qs() : '';
      const res = await fetch('/api/data' + q);
      const d = await res.json();

      // Health
      const h = document.getElementById('health');
      h.className = 'banner ' + (d.health.ok ? 'green' : 'red');
      h.textContent = d.health.message + ' — bounce rate ' + (d.health.bounce_rate * 100).toFixed(2) + '%';

      // Cards
      const stats = d.stats;
      const cardLabels = [
        ['Total Leads', stats.total],
        ['Sent', stats.sent],
        ['Delivered', stats.delivered],
        ['Bounced', stats.bounced],
        ['Replies', stats.replies],
        ['Reply Rate %', stats.reply_rate.toFixed(1)],
        ['Unsubscribed', stats.unsubscribed]
      ];
      document.getElementById('cards').innerHTML = cardLabels.map(([label, value]) =>
        `<div class="card"><div class="value">${value}</div><div class="label">${label}</div></div>`
      ).join('');

      // Trend chart
      const trendCtx = document.getElementById('trendChart').getContext('2d');
      if (trendChart) trendChart.destroy();
      trendChart = new Chart(trendCtx, {
        type: 'line',
        data: {
          labels: d.trend.map(x => x.day),
          datasets: [{
            label: 'Sends',
            data: d.trend.map(x => x.count),
            borderColor: '#3498db',
            fill: false
          }]
        },
        options: { scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } } }
      });

      // Industry chart
      const indCtx = document.getElementById('industryChart').getContext('2d');
      if (industryChart) industryChart.destroy();
      industryChart = new Chart(indCtx, {
        type: 'bar',
        data: {
          labels: d.industry_replies.map(x => x.industry),
          datasets: [{
            label: 'Replies',
            data: d.industry_replies.map(x => x.count),
            backgroundColor: '#2ecc71'
          }]
        },
        options: { scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } } }
      });

      // Funnel
      document.getElementById('funnel').innerHTML = `
        <p>Leads &rarr; Enriched &rarr; Sent &rarr; Replied</p>
        <p><strong>${d.funnel.leads}</strong> &rarr; <strong>${d.funnel.enriched}</strong> &rarr; <strong>${d.funnel.sent}</strong> &rarr; <strong>${d.funnel.replied}</strong></p>
      `;

      // Sequence
      document.getElementById('sequence').innerHTML = d.sequence.map(s =>
        `<tr><td>${s.label}</td><td>${s.count}</td></tr>`
      ).join('');

      // Activity
      document.getElementById('activity').innerHTML = d.activity.map(a =>
        `<tr><td>${a.created_at}</td><td>${a.event_type}</td><td>${a.company_name}</td><td>${a.industry}</td><td>${a.details ? a.details.substring(0,80) : ''}</td></tr>`
      ).join('');

      // Replies
      document.getElementById('replies').innerHTML = d.replies.map(r =>
        `<tr><td>${r.company_name}</td><td>${r.industry}</td><td>${r.last_contact_at}</td><td class="snippet" title="${r.reply_snippet}">${r.reply_snippet ? r.reply_snippet.substring(0,60) + '...' : ''}</td><td>${r.is_customer ? '<span>Customer</span>' : `<button class="btn" onclick="markCustomer(${r.id}, this)">Mark customer</button>`}</td></tr>`
      ).join('');
    }

    async function markCustomer(id, btn) {
      await fetch('/api/mark-customer/' + id, { method: 'POST' });
      btn.disabled = true;
      btn.textContent = 'Customer';
    }

    async function loadIndustries() {
      const res = await fetch('/api/industries');
      const d = await res.json();
      const sorted = (d.industries || []).slice().sort();
      ['industry', 'companyIndustry'].forEach(id => {
        const select = document.getElementById(id);
        const current = select.value;
        while (select.options.length > 1) select.remove(1);
        sorted.forEach(ind => {
          const opt = document.createElement('option');
          opt.value = ind;
          opt.textContent = ind;
          select.appendChild(opt);
        });
        select.value = current;
      });
    }

    // Set default date range to last 30 days
    const end = new Date();
    const start = new Date();
    start.setDate(start.getDate() - 30);
    document.getElementById('start').value = start.toISOString().split('T')[0];
    document.getElementById('end').value = end.toISOString().split('T')[0];

    loadIndustries();
    load();
    setInterval(load, 60000);
  </script>
</body>
</html>
"""


def start_dashboard() -> None:
    uvicorn.run(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT, log_level="info")
