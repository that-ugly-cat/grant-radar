"""SQLite layer: connection helper, schema, idempotent init."""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("GR_DB", os.path.join(os.path.dirname(__file__), "..", "data", "grantradar.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    funder          TEXT,
    scope           TEXT,
    max_amount      TEXT,
    duration_months TEXT,
    deadline        TEXT,
    deadline_date   DATE,
    deadline_logic  TEXT,
    link            TEXT,
    notes           TEXT,
    grant_start     TEXT,
    primary_type    TEXT,
    source_id       INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    status          TEXT DEFAULT 'open',
    origin          TEXT DEFAULT 'manual',
    last_checked_at DATETIME,
    last_verified_at DATETIME,
    link_status     TEXT,
    content_hash    TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    grant_id    INTEGER REFERENCES grants(id),
    source_id   INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    payload     TEXT,
    rationale   TEXT,
    source_url  TEXT,
    confidence  TEXT,
    method      TEXT,
    status      TEXT DEFAULT 'pending',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    decided_at  DATETIME
);

CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    url             TEXT,
    hints           TEXT,
    enabled         BOOLEAN DEFAULT 1,
    last_scanned_at DATETIME
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'reader',
    active        BOOLEAN DEFAULT 1,
    borant_sub    TEXT UNIQUE,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL,
    key          TEXT UNIQUE NOT NULL,
    active       BOOLEAN DEFAULT 1,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_at DATETIME
);

CREATE TABLE IF NOT EXISTS scan_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER REFERENCES sources(id),
    started_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME,
    outcome     TEXT,        -- ok | error
    detail      TEXT
);
"""

GRANT_FIELDS = [
    "name", "funder", "scope", "max_amount", "duration_months", "deadline",
    "deadline_date", "deadline_logic", "link", "notes", "grant_start",
    "primary_type",
]


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
        # Migrazione 2026-08-14: rimossa la colonna actionable.
        cols = [r["name"] for r in db.execute("PRAGMA table_info(grants)")]
        if "actionable" in cols:
            db.execute("ALTER TABLE grants DROP COLUMN actionable")
        # Migrazione 2026-08-14 (2): FK grants/proposals → sources.
        if "source_id" not in cols:
            db.execute("ALTER TABLE grants ADD COLUMN source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL")
        pcols = [r["name"] for r in db.execute("PRAGMA table_info(proposals)")]
        if "source_id" not in pcols:
            db.execute("ALTER TABLE proposals ADD COLUMN source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL")
        # Migrazione 2026-08-14 (3): timestamp dell'ultima verifica LLM per-grant.
        if "last_verified_at" not in cols:
            db.execute("ALTER TABLE grants ADD COLUMN last_verified_at DATETIME")
        # Migrazione 2026-08-24: il subject con cui Borant ID conosce l'utente,
        # scritto una volta sola da scripts/map_borant.py. NULL finche' non lo
        # si lega, e in AUTH_MODE=local non lo guarda nessuno. UNIQUE lo mette
        # un indice a parte perche' ALTER TABLE ADD COLUMN non accetta UNIQUE.
        ucols = [r["name"] for r in db.execute("PRAGMA table_info(users)")]
        if "borant_sub" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN borant_sub TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_borant_sub "
                   "ON users(borant_sub) WHERE borant_sub IS NOT NULL")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def status_condition(status: str, prefix: str = "") -> tuple[str, list]:
    """SQL condition for the status filter. 'open' and 'expired' are
    deadline-aware: open = status open AND (no deadline OR deadline in the
    future); expired = status open but deadline passed. `prefix` qualifies
    the columns when the query joins other tables (e.g. 'g.')."""
    s, d = f"{prefix}status", f"{prefix}deadline_date"
    if status == "open":
        return f" AND {s}='open' AND ({d} IS NULL OR date({d}) >= date('now'))", []
    if status == "expired":
        return f" AND {s}='open' AND {d} IS NOT NULL AND date({d}) < date('now')", []
    if status:
        return f" AND {s}=?", [status]
    return "", []


def grants_digest() -> list[dict]:
    """Compact dump of current grants, used by the scanner prompt and /ono/grants."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, funder, scope, max_amount, duration_months, deadline, "
            "deadline_date, deadline_logic, link, primary_type, source_id, status "
            "FROM grants ORDER BY deadline_date IS NULL, deadline_date"
        ).fetchall()
    return [dict(r) for r in rows]
