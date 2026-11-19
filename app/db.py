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
    recurring       TEXT,              -- yes | no | NULL (not decided yet)
    origin          TEXT DEFAULT 'manual',
    last_checked_at DATETIME,
    last_verified_at DATETIME,
    link_status     TEXT,
    link_fail_count INTEGER DEFAULT 0, -- consecutive failed fetches
    link_last_ok_at DATETIME,          -- last time the link answered
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

# What a proposal may carry: the descriptive fields plus the two that decide
# whether a row is still alive. They are kept out of GRANT_FIELDS because the
# edit form handles them on their own, with a closed set of values.
STATUS_VALUES = ("open", "closed", "archived")
RECURRING_VALUES = ("yes", "no")
PROPOSAL_FIELDS = GRANT_FIELDS + ["status", "recurring"]

# A one-off call stays visible as `expired` for this long after its deadline
# before the nightly job closes it: deadlines do get extended.
AUTO_CLOSE_GRACE_DAYS = 7


def clean_proposal_fields(fields: dict | None) -> dict:
    """Keep only the fields a proposal may carry, drop empties, and drop a
    status or recurring value outside its vocabulary instead of storing it:
    every writer of the queue (scan, check, MCP) goes through here."""
    out = {}
    for k, v in (fields or {}).items():
        if k not in PROPOSAL_FIELDS or v in (None, ""):
            continue
        if k == "status":
            v = str(v).strip().lower()
            if v not in STATUS_VALUES:
                continue
        if k == "recurring":
            v = str(v).strip().lower()
            v = {"true": "yes", "false": "no", "1": "yes", "0": "no"}.get(v, v)
            if v not in RECURRING_VALUES:
                continue
        out[k] = v
    return out


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
        # Migration 2026-11-19: `recurring` says whether a scheme has a next
        # edition (the nightly job closes the ones that do not), and the two
        # link columns let the monitor wait for consecutive failures.
        if "recurring" not in cols:
            db.execute("ALTER TABLE grants ADD COLUMN recurring TEXT")
        if "link_fail_count" not in cols:
            db.execute("ALTER TABLE grants ADD COLUMN link_fail_count INTEGER DEFAULT 0")
        if "link_last_ok_at" not in cols:
            db.execute("ALTER TABLE grants ADD COLUMN link_last_ok_at DATETIME")
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
            "deadline_date, deadline_logic, recurring, link, primary_type, source_id, status "
            "FROM grants ORDER BY deadline_date IS NULL, deadline_date"
        ).fetchall()
    return [dict(r) for r in rows]


def auto_close_expired() -> int:
    """Close the one-off calls whose deadline is behind them.

    Only rows marked recurring='no': a recurring scheme past its deadline is
    waiting for its next date, and closing it would hide it from the scan that
    brings that date. A row where `recurring` is not set is left alone for the
    same reason. Each closure leaves an already-approved proposal behind, so the
    queue's history says what closed, when and by which rule.
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT id, source_id, deadline_date FROM grants "
            "WHERE status='open' AND recurring='no' AND deadline_date IS NOT NULL "
            "AND date(deadline_date) < date('now', ?)",
            (f"-{AUTO_CLOSE_GRACE_DAYS} days",),
        ).fetchall()
        for r in rows:
            db.execute("UPDATE grants SET status='closed', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (r["id"],))
            db.execute(
                "INSERT INTO proposals (kind, grant_id, source_id, payload, rationale, source_url, "
                "confidence, method, status, decided_at) "
                "VALUES ('update', ?, ?, ?, ?, '', 'high', 'auto_close', 'approved', CURRENT_TIMESTAMP)",
                (r["id"], r["source_id"], '{"status": "closed"}',
                 f"Closed automatically: not recurring, and the deadline ({r['deadline_date']}) "
                 f"passed more than {AUTO_CLOSE_GRACE_DAYS} days ago."),
            )
    if rows:
        print(f"[auto-close] closed {len(rows)} grant(s): {[r['id'] for r in rows]}")
    return len(rows)


def failed_scan_sources() -> list[dict]:
    """Enabled sources whose most recent finished scan was an error."""
    with get_db() as db:
        rows = db.execute(
            "SELECT s.id, s.name, l.started_at, l.detail FROM sources s "
            "JOIN scan_log l ON l.id = (SELECT MAX(id) FROM scan_log "
            "                           WHERE source_id = s.id AND outcome IS NOT NULL) "
            "WHERE s.enabled=1 AND l.outcome='error' ORDER BY s.name"
        ).fetchall()
    return [dict(r) for r in rows]
