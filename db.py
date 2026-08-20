"""Database helpers.

Two backends are supported, selected automatically:
  - SQLite (default): local file at config.DB_PATH. Good for local dev.
  - Postgres: used whenever the DATABASE_URL env var is set (Railway's
    managed Postgres add-on sets this automatically). Required for any
    deployment where the dashboard and sender run as separate services,
    or where you want the data reachable from multiple devices/services.

All existing call sites use `?` placeholders and `row["col"]` access
(sqlite3.Row style); the Postgres path below transparently translates
placeholders and returns dict-like rows so no other file needs changes.
"""
import os
import re
import sqlite3
from pathlib import Path

from config import DB_PATH

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

_PLACEHOLDER_RE = re.compile(r"\?")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

    class _PGCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=None):
            sql = _PLACEHOLDER_RE.sub("%s", sql)
            if params is None:
                self._cur.execute(sql)
            else:
                self._cur.execute(sql, params)
            return self

        def fetchone(self):
            return self._cur.fetchone()

        def fetchall(self):
            return self._cur.fetchall()

        @property
        def rowcount(self):
            return self._cur.rowcount

        @property
        def lastrowid(self):
            raise NotImplementedError(
                "Postgres has no lastrowid; use db.insert_returning_id() instead."
            )

    class _PGConnection:
        def __init__(self, raw):
            self._raw = raw

        def cursor(self):
            return _PGCursor(self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

        def execute(self, sql, params=None):
            cur = self.cursor()
            cur.execute(sql, params)
            return cur

        def commit(self):
            self._raw.commit()

        def close(self):
            self._raw.close()

    def get_conn():
        return _PGConnection(psycopg2.connect(DATABASE_URL))

else:
    def get_conn() -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def insert_returning_id(cur, sql: str, params) -> int:
    """Run an INSERT and return the new row's id, portably across backends."""
    if USE_POSTGRES:
        cur.execute(sql + " RETURNING id", params)
        return cur.fetchone()["id"]
    cur.execute(sql, params)
    return cur.lastrowid


def _add_missing_columns_sqlite(conn) -> None:
    """Add any columns added since the original schema without destroying data."""
    columns = {
        "leads": [
            ("sequence_step", "INTEGER", "0"),
            ("last_contact_at", "TEXT", "NULL"),
            ("reply_snippet", "TEXT", "NULL"),
            ("is_customer", "INTEGER", "0"),
            ("gmail_message_id_header", "TEXT", "NULL"),
        ]
    }
    for table, defs in columns.items():
        cur = conn.execute(f"PRAGMA table_info({table})")
        existing = {row["name"] for row in cur.fetchall()}
        for col, dtype, default in defs:
            if col not in existing:
                if default == "NULL":
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
                else:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype} DEFAULT {default}")


def _add_missing_columns_postgres(conn) -> None:
    columns = {
        "leads": [
            ("sequence_step", "INTEGER", "0"),
            ("last_contact_at", "TEXT", "NULL"),
            ("reply_snippet", "TEXT", "NULL"),
            ("is_customer", "INTEGER", "0"),
            ("gmail_message_id_header", "TEXT", "NULL"),
        ]
    }
    cur = conn.cursor()
    for table, defs in columns.items():
        for col, dtype, default in defs:
            default_sql = "" if default == "NULL" else f" DEFAULT {default}"
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {dtype}{default_sql}")


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    company_name TEXT,
    email TEXT UNIQUE,
    website TEXT,
    socials_json TEXT,
    industry TEXT,
    location TEXT,
    status TEXT DEFAULT 'new',
    enriched_data TEXT,
    sequence_step INTEGER DEFAULT 0,
    last_contact_at TEXT,
    scheduled_at TEXT,
    sent_at TEXT,
    reply_snippet TEXT,
    is_customer INTEGER DEFAULT 0,
    gmail_message_id TEXT,
    gmail_thread_id TEXT,
    gmail_message_id_header TEXT
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER UNIQUE,
    subject TEXT,
    body TEXT,
    generated_at TEXT,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS followup_emails (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER,
    step INTEGER,
    body TEXT,
    generated_at TEXT,
    UNIQUE(lead_id, step),
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER,
    event_type TEXT,
    details TEXT,
    created_at TEXT,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS subject_usage (
    industry TEXT,
    angle_index INTEGER,
    last_used_at TEXT,
    PRIMARY KEY (industry, angle_index)
);
"""

POSTGRES_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS leads (
        id SERIAL PRIMARY KEY,
        company_name TEXT,
        email TEXT UNIQUE,
        website TEXT,
        socials_json TEXT,
        industry TEXT,
        location TEXT,
        status TEXT DEFAULT 'new',
        enriched_data TEXT,
        sequence_step INTEGER DEFAULT 0,
        last_contact_at TEXT,
        scheduled_at TEXT,
        sent_at TEXT,
        reply_snippet TEXT,
        is_customer INTEGER DEFAULT 0,
        gmail_message_id TEXT,
        gmail_thread_id TEXT,
        gmail_message_id_header TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS emails (
        id SERIAL PRIMARY KEY,
        lead_id INTEGER UNIQUE REFERENCES leads(id),
        subject TEXT,
        body TEXT,
        generated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS followup_emails (
        id SERIAL PRIMARY KEY,
        lead_id INTEGER REFERENCES leads(id),
        step INTEGER,
        body TEXT,
        generated_at TEXT,
        UNIQUE(lead_id, step)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id SERIAL PRIMARY KEY,
        lead_id INTEGER REFERENCES leads(id),
        event_type TEXT,
        details TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS state (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subject_usage (
        industry TEXT,
        angle_index INTEGER,
        last_used_at TEXT,
        PRIMARY KEY (industry, angle_index)
    )
    """,
]


def init_db() -> None:
    if USE_POSTGRES:
        conn = get_conn()
        cur = conn.cursor()
        for stmt in POSTGRES_SCHEMA_STATEMENTS:
            cur.execute(stmt)
        conn.commit()
        _add_missing_columns_postgres(conn)
        conn.commit()
        conn.close()
        return

    if not DB_PATH.parent.exists():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = get_conn()
    conn.executescript(SQLITE_SCHEMA)
    _add_missing_columns_sqlite(conn)
    conn.commit()
    conn.close()


def log_event(lead_id: int, event_type: str, details: str = "") -> None:
    from datetime import datetime, timezone

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO events (lead_id, event_type, details, created_at) VALUES (?, ?, ?, ?)",
        (lead_id, event_type, details, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
