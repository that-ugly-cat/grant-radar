"""MCP surface for Ono (and any MCP client). Auth is enforced upstream by the
capability-URL middleware in main.py — tools here assume an authenticated caller.

Every tool is the function right below its `@mcp.tool()` in this file, and the
list of them is `tool_names()`, read off the registry. It is not written out
here on purpose: the landing page and the README both quote it, and a list kept
by hand goes stale silently — it said six tools while seven were registered,
with `list_sources` missing from the copy on a public page.

Proposal approval is deliberately NOT exposed on this surface."""
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


def tool_names() -> list[str]:
    """The tools actually registered on this server, in registration order.

    Whatever quotes the tool list — the landing's geek mode today — asks here
    instead of spelling it out, so adding a tool updates the copy and cannot
    leave a public page claiming a number that is one short."""
    return [t.name for t in mcp._tool_manager.list_tools()]


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
    """Search tracked grants. q is a substring matched against name, scope,
    notes and funder — the same four fields as the search box in the web UI;
    funder additionally filters exactly; primary_type filters exactly (e.g.
    Project, Postdoc, PI, Network, PhD, Other) or pass 'none' to match grants
    with no type set. status is deadline-aware: 'open' = status open AND
    deadline in the future or absent; 'expired' = status open but deadline
    passed; 'closed'/'archived' = manual states; '' = any. Returns a JSON list
    sorted by next deadline."""
    # The four fields are the web UI's, not a shorter set of their own: one
    # filter with two behaviours means a search someone learned in the table
    # quietly loses results when the same words are typed in a chat. The web
    # behaviour wins because it is the one already learned.
    sql = "SELECT * FROM grants WHERE 1=1"
    args: list = []
    if q:
        sql += " AND (name LIKE ? OR scope LIKE ? OR notes LIKE ? OR funder LIKE ?)"
        args += [f"%{q}%"] * 4
    if funder:
        sql += " AND funder = ?"
        args.append(funder)
    if primary_type == "none":
        sql += " AND (primary_type IS NULL OR primary_type = '')"
    elif primary_type:
        sql += " AND primary_type = ?"
        args.append(primary_type)
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
                     source_url: str, confidence: str, source_id: int | None) -> int:
    clean = {k: v for k, v in fields.items() if k in GRANT_FIELDS and v not in (None, "")}
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO proposals (kind, grant_id, source_id, payload, rationale, source_url, confidence, method) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'ono_mcp')",
            (kind, grant_id, source_id, json.dumps(clean, ensure_ascii=False), rationale, source_url, confidence),
        )
        return cur.lastrowid


@mcp.tool()
def list_sources() -> str:
    """Configured discovery sources (id, name, url, hints, enabled). Use the id
    as source_id when filing proposals that belong to one of these sources."""
    with get_db() as db:
        rows = db.execute("SELECT id, name, url, hints, enabled FROM sources ORDER BY name").fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
def propose_grant(fields: dict, rationale: str, source_url: str = "", confidence: str = "medium",
                  source_id: int | None = None) -> str:
    """Propose a NEW grant for the queue (needs human approval in the UI).
    fields: subset of {name, funder, scope, max_amount, duration_months, deadline,
    deadline_date (YYYY-MM-DD), deadline_logic, link, notes, grant_start,
    primary_type}. name is required. source_id (see list_sources) links the
    proposal to the discovery source it belongs to; on approval the grant
    inherits it."""
    if not fields.get("name"):
        return json.dumps({"ok": False, "error": "fields.name is required"})
    pid = _insert_proposal("new", None, fields, rationale, source_url, confidence, source_id)
    return json.dumps({"ok": True, "proposal_id": pid})


@mcp.tool()
def propose_update(grant_id: int, fields: dict, rationale: str, source_url: str = "",
                   confidence: str = "medium", source_id: int | None = None) -> str:
    """Propose an UPDATE to an existing grant (needs human approval in the UI).
    fields: only the fields that should change. source_id (see list_sources)
    is inherited by the grant on approval if the grant has none yet."""
    with get_db() as db:
        if not db.execute("SELECT id FROM grants WHERE id=?", (grant_id,)).fetchone():
            return json.dumps({"ok": False, "error": f"grant {grant_id} not found"})
    if not fields:
        return json.dumps({"ok": False, "error": "fields is empty"})
    pid = _insert_proposal("update", grant_id, fields, rationale, source_url, confidence, source_id)
    return json.dumps({"ok": True, "proposal_id": pid})
