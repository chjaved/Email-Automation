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

from config import DASHBOARD_HOST, DASHBOARD_PORT, DB_PATH, FOLLOWUP_SCHEDULE, TIMEZONE
from db import get_conn, init_db
from send_batch import pick_emails

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

    conn.close()

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
    cur = conn.cursor()
    cur.execute("UPDATE leads SET is_customer = 1 WHERE id = ?", (lead_id,))
    conn.commit()
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

    conn.close()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "rows": rows,
    }


@app.get("/api/followups")
def api_followups():
    """Leads awaiting their next follow-up, with the computed due date."""
    conn = get_conn()
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


@app.post("/api/import-csv")
async def api_import_csv(file: UploadFile = File(...)):
    """Upload a CSV of companies and upsert them into the DB (status='new').

    Recognised columns (best match wins, case-insensitive):
      Company Name, Industry, HR Email, Recruitment Email, General Company Email
      (falls back to a generic 'email'/'Email' column if none of the above exist)
    """
    init_db()
    raw = await file.read()
    text = raw.decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV has no header row")

    imported, updated, skipped = 0, 0, 0
    for row in reader:
        company = (row.get("Company Name") or row.get("company_name") or row.get("Company") or "").strip()
        industry = (row.get("Industry") or row.get("industry") or "").strip() or "other"

        emails = pick_emails(row)
        if not emails:
            fallback = (row.get("Email") or row.get("email") or row.get("E-mail") or "").strip()
            if fallback:
                emails = [fallback]

        if not company or not emails:
            skipped += 1
            continue

        primary = emails[0]
        conn = get_conn()
        cur = conn.cursor()
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
        conn.close()

    return {"ok": True, "imported": imported, "updated": updated, "skipped": skipped}


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
  * { box-sizing: border-box; }
  body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f6fa; color: #333; }
  h1 { margin: 0 0 15px; }
  .filters { margin-bottom: 15px; }
  .filters select, .filters input { padding: 6px; margin-right: 8px; }
  .banner { padding: 12px 16px; border-radius: 6px; margin-bottom: 20px; font-weight: bold; }
  .banner.green { background: #d4edda; color: #155724; }
  .banner.red { background: #f8d7da; color: #721c24; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: #fff; padding: 16px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); text-align: center; }
  .card .value { font-size: 1.8em; font-weight: bold; color: #2c3e50; }
  .card .label { font-size: 0.85em; color: #777; margin-top: 4px; }
  .section { background: #fff; padding: 16px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
  th, td { padding: 8px; text-align: left; border-bottom: 1px solid #eee; }
  th { color: #666; }
  .btn { padding: 6px 10px; background: #28a745; color: #fff; border: none; border-radius: 4px; cursor: pointer; }
  .btn:disabled { background: #aaa; cursor: default; }
  .btn.secondary { background: #3498db; }
  .btn.small { padding: 4px 8px; font-size: 0.85em; }
  .snippet { max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 2px solid #e1e4e8; }
  .tab-btn { padding: 10px 18px; background: none; border: none; cursor: pointer; font-size: 0.95em; color: #666; border-bottom: 3px solid transparent; margin-bottom: -2px; }
  .tab-btn.active { color: #2c3e50; font-weight: bold; border-bottom-color: #3498db; }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.78em; font-weight: bold; }
  .badge.new, .badge.enriched { background: #e3f2fd; color: #1565c0; }
  .badge.scheduled { background: #fff3cd; color: #856404; }
  .badge.sent { background: #d1ecf1; color: #0c5460; }
  .badge.completed { background: #d4edda; color: #155724; }
  .badge.replied { background: #d4edda; color: #155724; }
  .badge.bounced, .badge.unsubscribed { background: #f8d7da; color: #721c24; }
  .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
  .toolbar input, .toolbar select { padding: 6px; }
  .pager { display: flex; gap: 8px; align-items: center; margin-top: 10px; }
  .toast { position: fixed; top: 16px; right: 16px; padding: 12px 18px; border-radius: 6px; color: #fff; font-weight: bold; z-index: 999; opacity: 0; transition: opacity 0.3s; }
  .toast.show { opacity: 1; }
  .toast.ok { background: #28a745; }
  .toast.err { background: #dc3545; }
  @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
  <h1>Campaign Engine Dashboard</h1>

  <div class="tabs">
    <button class="tab-btn active" data-tab="overview" onclick="switchTab('overview')">Overview</button>
    <button class="tab-btn" data-tab="companies" onclick="switchTab('companies')">Companies</button>
    <button class="tab-btn" data-tab="followups" onclick="switchTab('followups')">Follow-ups</button>
    <button class="tab-btn" data-tab="replies" onclick="switchTab('replies')">Replies</button>
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
      <div class="toolbar">
        <input type="text" id="companySearch" placeholder="Search company or email..." style="min-width:260px" />
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
        <input type="file" id="csvFile" accept=".csv" style="display:none" onchange="uploadCsv()" />
        <button class="btn" onclick="document.getElementById('csvFile').click()">Upload CSV</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>Company</th><th>Email</th><th>Industry</th><th>Status</th><th>Sequence</th>
            <th>Last Contact</th><th>Scheduled</th><th></th>
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
      } catch (e) {
        showToast(e.message, false);
      } finally {
        input.value = '';
      }
    }

    async function loadCompanies(page) {
      companyPageNum = page || companyPageNum;
      const params = new URLSearchParams();
      const search = document.getElementById('companySearch').value.trim();
      const status = document.getElementById('companyStatus').value;
      const industry = document.getElementById('companyIndustry').value;
      if (search) params.set('search', search);
      if (status) params.set('status', status);
      if (industry) params.set('industry', industry);
      params.set('page', companyPageNum);
      params.set('page_size', companyPageSize);

      const res = await fetch('/api/companies?' + params.toString());
      const d = await res.json();
      companyTotal = d.total;

      document.getElementById('companiesTable').innerHTML = d.rows.map(r => {
        const canSend = !['replied', 'bounced', 'unsubscribed'].includes(r.status);
        return `<tr>
          <td>${r.company_name || ''}</td>
          <td>${r.email || ''}</td>
          <td>${r.industry || ''}</td>
          <td>${statusBadge(r.status || 'new')}</td>
          <td>${r.step_label}</td>
          <td>${r.last_contact_at ? r.last_contact_at.substring(0,16) : ''}</td>
          <td>${r.scheduled_at ? r.scheduled_at.substring(0,16) : ''}</td>
          <td>${canSend ? `<button class="btn small" onclick="sendNow(${r.id}, this)">Send now</button>` : ''}</td>
        </tr>`;
      }).join('');

      const totalPages = Math.max(1, Math.ceil(companyTotal / companyPageSize));
      document.getElementById('companyPageInfo').textContent =
        `Page ${companyPageNum} of ${totalPages} (${companyTotal} total)`;
    }

    function companyPage(delta) {
      const totalPages = Math.max(1, Math.ceil(companyTotal / companyPageSize));
      const next = Math.min(totalPages, Math.max(1, companyPageNum + delta));
      loadCompanies(next);
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
      const res = await fetch('/api/data');
      const d = await res.json();
      const seen = new Set();
      d.industry_replies.forEach(x => seen.add(x.industry));
      d.activity.forEach(a => a.industry && seen.add(a.industry));
      d.replies.forEach(r => r.industry && seen.add(r.industry));
      const sorted = Array.from(seen).sort();
      ['industry', 'companyIndustry'].forEach(id => {
        const select = document.getElementById(id);
        sorted.forEach(ind => {
          const opt = document.createElement('option');
          opt.value = ind;
          opt.textContent = ind;
          select.appendChild(opt);
        });
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
