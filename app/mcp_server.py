"""MCP surface for Ono (and any MCP client). Auth is enforced upstream by the
capability-URL middleware in main.py — tools here assume an authenticated caller.

Tools: search_grants, get_grant, upcoming_deadlines, list_proposals,
propose_grant, propose_update. Proposal approval is deliberately NOT exposed."""
import json

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .db import get_db, status_condition, GRANT_FIELDS

mcp = MCPServer(
    "grant-radar",
    instructions=(
        "Funding-opportunity tracker. Search tracked grants, check upcoming deadlines, "
        "and file proposals (new grants or updates) that a human reviews in the web UI."
    ),
)


def build_asgi_app():
    """Streamable HTTP ASGI app, mounted at /mcp by main.py (so path here is '/').
    DNS-rebinding protection off: the app sits behind Caddy on a public hostname,
    and auth is enforced by the capability-URL middleware upstream."""
    return mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


@mcp.tool()
def search_grants(
    q: str = "",
    funder: str = "",
    primary_type: str = "",
    status: str = "open",
) -> str:
    """Search tracked grants. q matches name/scope/notes (substring); funder and
    primary_type (Project|Postdoc|PI|Network|PhD|Other) filter exactly.
    status is deadline-aware: 'open' = status open AND deadline in the future
    or absent; 'expired' = status open but deadline passed; 'closed'/'archived'
    = manual states; '' = any. Returns a JSON list sorted by next deadline."""
    sql = "SELECT * FROM grants WHERE 1=1"
    args: list = []
    if q:
        sql += " AND (name LIKE ? OR scope LIKE ? OR notes LIKE ?)"
        args += [f"%{q}%"] * 3
    for col, val in (("funder", funder), ("primary_type", primary_type)):
        if val:
            sql += f" AND {col} = ?"
            args.append(val)
    cond, cond_args = status_condition(status)
    sql += cond
    args += cond_args
    sql += " ORDER BY deadline_date IS NULL, deadline_date"
    with get_db() as db:
        rows = db.execute(sql, args).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)


@mcp.tool()
def get_grant(grant_id: int) -> str:
    """Full record of one grant by ID, as JSON."""
    with get_db() as db:
        row = db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
    return json.dumps(dict(row) if row else None, ensure_ascii=False, default=str)


@mcp.tool()
def upcoming_deadlines(days: int = 90) -> str:
    """Open grants with a deadline within the next `days` days, soonest first."""
    sql = (
        "SELECT id, name, funder, deadline, deadline_date, primary_type, link "
        "FROM grants WHERE status='open' AND deadline_date IS NOT NULL "
        "AND deadline_date >= date('now') AND deadline_date <= date('now', ?) "
        "ORDER BY deadline_date"
    )
    with get_db() as db:
        rows = db.execute(sql, [f"+{int(days)} days"]).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)


@mcp.tool()
def list_proposals(status: str = "pending") -> str:
    """Discovery/manual proposals in the queue (status: pending|approved|rejected).
    Approval happens only in the web UI."""
    with get_db() as db:
        rows = db.execute(
            "SELECT p.*, g.name AS grant_name FROM proposals p "
            "LEFT JOIN grants g ON g.id = p.grant_id "
            "WHERE p.status=? ORDER BY p.created_at DESC",
            (status,),
        ).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)


def _insert_proposal(kind: str, grant_id: int | None, fields: dict, rationale: str,
                     source_url: str, confidence: str) -> int:
    clean = {k: v for k, v in fields.items() if k in GRANT_FIELDS and v not in (None, "")}
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO proposals (kind, grant_id, payload, rationale, source_url, confidence, method) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ono_mcp')",
            (kind, grant_id, json.dumps(clean, ensure_ascii=False), rationale, source_url, confidence),
        )
        return cur.lastrowid


@mcp.tool()
def propose_grant(fields: dict, rationale: str, source_url: str = "", confidence: str = "medium") -> str:
    """Propose a NEW grant for the queue (needs human approval in the UI).
    fields: subset of {name, funder, scope, max_amount, duration_months, deadline,
    deadline_date (YYYY-MM-DD), deadline_logic, link, notes, grant_start,
    primary_type}. name is required."""
    if not fields.get("name"):
        return json.dumps({"ok": False, "error": "fields.name is required"})
    pid = _insert_proposal("new", None, fields, rationale, source_url, confidence)
    return json.dumps({"ok": True, "proposal_id": pid})


@mcp.tool()
def propose_update(grant_id: int, fields: dict, rationale: str, source_url: str = "",
                   confidence: str = "medium") -> str:
    """Propose an UPDATE to an existing grant (needs human approval in the UI).
    fields: only the fields that should change."""
    with get_db() as db:
        if not db.execute("SELECT id FROM grants WHERE id=?", (grant_id,)).fetchone():
            return json.dumps({"ok": False, "error": f"grant {grant_id} not found"})
    if not fields:
        return json.dumps({"ok": False, "error": "fields is empty"})
    pid = _insert_proposal("update", grant_id, fields, rationale, source_url, confidence)
    return json.dumps({"ok": True, "proposal_id": pid})
