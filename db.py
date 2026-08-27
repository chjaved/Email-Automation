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
    import psycopg2.pool

    # Bounded pool: caps total open Postgres connections regardless of how
    # many requests are in flight or whether any individual call site fails
    # to close its connection. Without this, a bug (or a huge CSV import)
    # that leaks connections can exhaust Postgres' server-side connection
    # limit and take down every service sharing that database.
    POSTGRES_POOL_MAX = int(os.getenv("POSTGRES_POOL_MAX", "20"))
    _pool = psycopg2.pool.ThreadedConnectionPool(1, POSTGRES_POOL_MAX, DATABASE_URL)

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
            self._returned = False

        def cursor(self):
            return _PGCursor(self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

        def execute(self, sql, params=None):
            cur = self.cursor()
            cur.execute(sql, params)
            return cur

        def commit(self):
            self._raw.commit()

        def close(self):
            """Return the connection to the pool instead of actually closing the
            socket, so the app never opens more than POSTGRES_POOL_MAX connections."""
            if self._returned:
                return
            self._returned = True
            try:
                self._raw.rollback()
            except Exception:
                pass
            _pool.putconn(self._raw)

    def get_conn():
        return _PGConnection(_pool.getconn())

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
            ("user_id", "INTEGER", "NULL"),
            ("bounce_reason", "TEXT", "NULL"),
            ("sent_from_mailbox", "TEXT", "NULL"),
        ],
        "events": [
            ("mailbox", "TEXT", "NULL"),
        ],
        "user_settings": [
            ("cc_enabled", "INTEGER", "1"),
            ("ai_context", "TEXT", "NULL"),
            ("attachment_bytes", "BLOB", "NULL"),
            ("attachment_name", "TEXT", "NULL"),
            ("attachment_mime", "TEXT", "NULL"),
            ("sample_email", "TEXT", "NULL"),
            ("email_instructions", "TEXT", "NULL"),
            ("sig_name", "TEXT", "NULL"),
            ("sig_title", "TEXT", "NULL"),
            ("sig_company", "TEXT", "NULL"),
            ("sig_email", "TEXT", "NULL"),
            ("sig_phone", "TEXT", "NULL"),
            ("sig_website", "TEXT", "NULL"),
            ("auto_send_enabled", "INTEGER", "0"),
            ("send_gap_min", "INTEGER", "120"),
            ("send_gap_max", "INTEGER", "300"),
            ("daily_send_cap", "INTEGER", "300"),
        ],
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
            ("user_id", "INTEGER", "NULL"),
            ("bounce_reason", "TEXT", "NULL"),
            ("sent_from_mailbox", "TEXT", "NULL"),
        ],
        "events": [
            ("mailbox", "TEXT", "NULL"),
        ],
        "user_settings": [
            ("cc_enabled", "INTEGER", "1"),
            ("ai_context", "TEXT", "NULL"),
            ("attachment_bytes", "BYTEA", "NULL"),
            ("attachment_name", "TEXT", "NULL"),
            ("attachment_mime", "TEXT", "NULL"),
            ("sample_email", "TEXT", "NULL"),
            ("email_instructions", "TEXT", "NULL"),
            ("sig_name", "TEXT", "NULL"),
            ("sig_title", "TEXT", "NULL"),
            ("sig_company", "TEXT", "NULL"),
            ("sig_email", "TEXT", "NULL"),
            ("sig_phone", "TEXT", "NULL"),
            ("sig_website", "TEXT", "NULL"),
            ("auto_send_enabled", "INTEGER", "0"),
            ("send_gap_min", "INTEGER", "120"),
            ("send_gap_max", "INTEGER", "300"),
            ("daily_send_cap", "INTEGER", "300"),
        ],
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
    email TEXT,
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
    gmail_message_id_header TEXT,
    sent_from_mailbox TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_user ON leads(email, user_id);

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
    mailbox TEXT,
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

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    smtp_user TEXT,
    smtp_password_enc TEXT,
    from_alias TEXT,
    from_display_name TEXT,
    cc_enabled INTEGER DEFAULT 1,
    ai_context TEXT,
    attachment_bytes BLOB,
    attachment_name TEXT,
    attachment_mime TEXT,
    sample_email TEXT,
    email_instructions TEXT,
    sig_name TEXT,
    sig_title TEXT,
    sig_company TEXT,
    sig_email TEXT,
    sig_phone TEXT,
    sig_website TEXT,
    auto_send_enabled INTEGER DEFAULT 0,
    send_gap_min INTEGER DEFAULT 120,
    send_gap_max INTEGER DEFAULT 300,
    daily_send_cap INTEGER DEFAULT 300,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS user_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT,
    data BLOB NOT NULL,
    uploaded_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""

POSTGRES_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS leads (
        id SERIAL PRIMARY KEY,
        company_name TEXT,
        email TEXT,
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
        gmail_message_id_header TEXT,
        user_id INTEGER
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_user ON leads(email, user_id)
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
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY REFERENCES users(id),
        smtp_user TEXT,
        smtp_password_enc TEXT,
        from_alias TEXT,
        from_display_name TEXT,
        cc_enabled INTEGER DEFAULT 1,
        ai_context TEXT,
        attachment_bytes BYTEA,
        attachment_name TEXT,
        attachment_mime TEXT,
        sample_email TEXT,
        email_instructions TEXT,
        sig_name TEXT,
        sig_title TEXT,
        sig_company TEXT,
        sig_email TEXT,
        sig_phone TEXT,
        sig_website TEXT,
        auto_send_enabled INTEGER DEFAULT 0,
        send_gap_min INTEGER DEFAULT 120,
        send_gap_max INTEGER DEFAULT 300,
        daily_send_cap INTEGER DEFAULT 300
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_attachments (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        filename TEXT NOT NULL,
        mime TEXT,
        data BYTEA NOT NULL,
        uploaded_at TEXT
    )
    """,
]


def _migrate_leads_unique_to_composite_sqlite(conn) -> None:
    """Migrate leads table from email-UNIQUE to (email, user_id) composite unique.

    SQLite cannot DROP a column constraint, so we recreate the table."""
    cur = conn.execute("PRAGMA table_info(leads)")
    cols = cur.fetchall()
    if not cols:
        return  # table doesn't exist yet; schema will create it correctly

    # Check if email has a UNIQUE constraint by looking at the schema SQL
    schema_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='leads'").fetchone()
    if not schema_sql:
        return
    schema_text = schema_sql["sql"] or ""
    # If the schema doesn't have "email TEXT UNIQUE", nothing to migrate
    if "email TEXT UNIQUE" not in schema_text and "email\" TEXT UNIQUE" not in schema_text:
        return

    # Recreate the table without the global UNIQUE on email
    col_names = [c["name"] for c in cols]
    col_list = ", ".join(col_names)
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS leads_new (
            id INTEGER PRIMARY KEY,
            company_name TEXT,
            email TEXT,
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
            gmail_message_id_header TEXT,
            user_id INTEGER
        );
        INSERT INTO leads_new ({col_list}) SELECT {col_list} FROM leads;
        DROP TABLE leads;
        ALTER TABLE leads_new RENAME TO leads;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_user ON leads(email, user_id);
    """)


def _migrate_leads_unique_to_composite_postgres(conn) -> None:
    """Drop the old email-UNIQUE constraint on leads and add (email, user_id) composite."""
    cur = conn.cursor()
    # Find and drop the email unique constraint if it exists
    cur.execute("""
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'leads'::regclass AND contype = 'u'
        AND conname LIKE '%email%'
    """)
    for row in cur.fetchall():
        cur.execute(f"ALTER TABLE leads DROP CONSTRAINT IF EXISTS {row['conname']}")
    # Create composite unique index if not exists
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_user ON leads(email, user_id)")


def init_db() -> None:
    if USE_POSTGRES:
        conn = get_conn()
        cur = conn.cursor()
        for stmt in POSTGRES_SCHEMA_STATEMENTS:
            cur.execute(stmt)
        conn.commit()
        _add_missing_columns_postgres(conn)
        conn.commit()
        _migrate_leads_unique_to_composite_postgres(conn)
        conn.commit()
        conn.close()
        return

    if not DB_PATH.parent.exists():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = get_conn()
    conn.executescript(SQLITE_SCHEMA)
    _add_missing_columns_sqlite(conn)
    _migrate_leads_unique_to_composite_sqlite(conn)
    conn.commit()
    conn.close()


def log_event(lead_id: int, event_type: str, details: str = "", mailbox: str = "") -> None:
    from datetime import datetime, timezone

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO events (lead_id, event_type, details, mailbox, created_at) VALUES (?, ?, ?, ?, ?)",
        (lead_id, event_type, details, mailbox, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
