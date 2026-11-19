"""MCP surface for Ono (and any MCP client). Auth is enforced upstream by the
capability-URL middleware in main.py — tools here assume an authenticated caller.

Every tool is the function right below its `@mcp.tool()` in this file, and the
list of them is `tool_names()`, read off the registry. It is not written out
here on purpose: the landing page and the README both quote it, and a list kept
by hand goes stale silently — it said six tools while seven were registered,
with `list_sources` missing from the copy on a public page.

Proposal approval is deliberately NOT exposed on this surface."""
import contextvars
import json

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .db import clean_proposal_fields, get_db, status_condition
from .workspaces import role_allows, role_in, user_workspaces, workspace_for

# Who is calling. Set by the middleware in main.py from the API key, which
# belongs to one person; every tool reads it, and none trusts an argument for it.
current_user: contextvars.ContextVar[dict | None] = contextvars.ContextVar("gr_mcp_user", default=None)

mcp = MCPServer(
    "grant-radar",
    instructions=(
        "Funding-opportunity tracker, organised in workspaces: each has its own grants, "
        "sources and proposal queue, and nothing is shared between two. Start with "
        "list_workspaces to see which ones this key reaches and with what role. Every "
        "other tool takes a `workspace` slug; leave it empty for the key owner's personal "
        "workspace. Search tracked grants, check upcoming deadlines, and file proposals "
        "(new grants or updates) that a human reviews in the web UI. search_grants is "
        "lexical: no hit means those words are absent, not that the call is untracked."
    ),
)


def tool_names() -> list[str]:
    """The tools actually registered on this server, in registration order.

    Whatever quotes the tool list — the landing's geek mode today — asks here
    instead of spelling it out, so adding a tool updates the copy and cannot
    leave a public page claiming a number that is one short."""
    # `_tool_manager` is private: there is no public *sync* accessor, and the
    # landing route that needs this is sync. A future version of `mcp` renaming
    # it would otherwise take the public showcase down with it — so a failure
    # here costs the enumeration, not the page.
    try:
        return [t.name for t in mcp._tool_manager.list_tools()]
    except Exception:
        return []


def build_asgi_app():
    """Streamable HTTP ASGI app, mounted at /mcp by main.py (so path here is '/').
    DNS-rebinding protection off: the app sits behind Caddy on a public hostname,
    and auth is enforced by the capability-URL middleware upstream."""
    return mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def _uid() -> int | None:
    user = current_user.get()
    return int(user["sub"]) if user else None


def _err(code: str, detail: str = "") -> str:
    return json.dumps({"ok": False, "error": code, **({"detail": detail} if detail else {})})


def _ws(workspace: str, need: str = "read"):
    """(workspace, None) or (None, error JSON). A workspace the caller is not a
    member of and one that does not exist answer alike."""
    uid = _uid()
    if uid is None:
        return None, _err("not_authenticated")
    ws = workspace_for(uid, (workspace or "").strip())
    if not ws:
        return None, _err("workspace_not_found", "no such workspace for this key; see list_workspaces")
    if not role_allows(ws["role"], need):
        return None, _err("forbidden_role", f"this needs '{need}', the key's owner has '{ws['role']}'")
    return ws, None


@mcp.tool()
def list_workspaces() -> str:
    """The workspaces this key reaches: slug, name, kind (personal | shared),
    the caller's role (read | write | admin), the research profile the scan
    judges fit against, and how many grants each holds. Pass a slug as
    `workspace` to the other tools; read can search, write can propose."""
    uid = _uid()
    if uid is None:
        return _err("not_authenticated")
    out = []
    with get_db() as db:
        for ws in user_workspaces(uid):
            n = db.execute("SELECT COUNT(*) AS n FROM grants WHERE workspace_id=?", (ws["id"],)).fetchone()["n"]
            out.append({"slug": ws["slug"], "name": ws["name"], "kind": ws["kind"], "role": ws["role"],
                        "profile": ws["profile"] or "", "grants": n})
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def search_grants(
    q: str = "",
    funder: str = "",
    primary_type: str = "",
    status: str = "open",
    workspace: str = "",
    fit: str = "",
) -> str:
    """Search one workspace's grants. q is a substring matched against name,
    scope, notes and funder — the same four fields as the search box in the
    web UI; funder additionally filters exactly; primary_type filters exactly
    (e.g. Project, Postdoc, PI, Network, PhD, Other) or pass 'none' to match
    grants with no type set; fit filters exactly (high | medium | low: how well
    the call matches the workspace's research profile) or 'none' for grants not
    rated yet. status is deadline-aware: 'open' = status open AND deadline in
    the future or absent; 'expired' = status open but deadline passed;
    'closed' = set by hand, or automatically for a non-recurring call past its
    deadline; 'archived' = set by hand; '' = any. Returns a JSON list sorted by
    next deadline."""
    ws, err = _ws(workspace)
    if err:
        return err
    # The four fields are the web UI's, not a shorter set of their own: one
    # filter with two behaviours means a search someone learned in the table
    # quietly loses results when the same words are typed in a chat. The web
    # behaviour wins because it is the one already learned.
    sql = "SELECT * FROM grants WHERE workspace_id=?"
    args: list = [ws["id"]]
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
    if fit == "none":
        sql += " AND fit IS NULL"
    elif fit:
        sql += " AND fit = ?"
        args.append(fit)
    cond, cond_args = status_condition(status)
    sql += cond
    args += cond_args
    sql += " ORDER BY deadline_date IS NULL, deadline_date"
    with get_db() as db:
        rows = db.execute(sql, args).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)


def _grant_for_caller(grant_id: int, need: str = "read"):
    """(grant row, None) or (None, error JSON); the workspace is the grant's own."""
    uid = _uid()
    if uid is None:
        return None, _err("not_authenticated")
    with get_db() as db:
        row = db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
    role = role_in(uid, row["workspace_id"]) if row else None
    if not row or role is None:
        return None, _err("grant_not_found", f"no grant {grant_id} in a workspace this key reaches")
    if not role_allows(role, need):
        return None, _err("forbidden_role", f"this needs '{need}', the key's owner has '{role}'")
    return dict(row), None


@mcp.tool()
def get_grant(grant_id: int) -> str:
    """Full record of one grant by ID, as JSON. The grant has to be in a
    workspace this key reaches."""
    grant, err = _grant_for_caller(grant_id)
    return err or json.dumps(grant, ensure_ascii=False, default=str)


@mcp.tool()
def upcoming_deadlines(days: int = 90, workspace: str = "") -> str:
    """One workspace's open grants with a deadline within the next `days` days,
    soonest first."""
    ws, err = _ws(workspace)
    if err:
        return err
    sql = (
        "SELECT id, name, funder, deadline, deadline_date, primary_type, fit, link "
        "FROM grants WHERE workspace_id=? AND status='open' AND deadline_date IS NOT NULL "
        "AND deadline_date >= date('now') AND deadline_date <= date('now', ?) "
        "ORDER BY deadline_date"
    )
    with get_db() as db:
        rows = db.execute(sql, [ws["id"], f"+{int(days)} days"]).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)


@mcp.tool()
def list_proposals(status: str = "pending", workspace: str = "") -> str:
    """One workspace's proposal queue (status: pending|approved|rejected).
    Approval happens only in the web UI."""
    ws, err = _ws(workspace)
    if err:
        return err
    with get_db() as db:
        rows = db.execute(
            "SELECT p.*, g.name AS grant_name FROM proposals p "
            "LEFT JOIN grants g ON g.id = p.grant_id "
            "WHERE p.status=? AND p.workspace_id=? ORDER BY p.created_at DESC",
            (status, ws["id"]),
        ).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)


def _insert_proposal(kind: str, grant_id: int | None, fields: dict, rationale: str,
                     source_url: str, confidence: str, source_id: int | None, workspace_id: int) -> int:
    clean = clean_proposal_fields(fields)
    with get_db() as db:
        # A source from another workspace is dropped, not attached.
        if source_id and not db.execute("SELECT 1 FROM sources WHERE id=? AND workspace_id=?",
                                        (source_id, workspace_id)).fetchone():
            source_id = None
        cur = db.execute(
            "INSERT INTO proposals (kind, grant_id, source_id, workspace_id, payload, rationale, source_url, "
            "confidence, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ono_mcp')",
            (kind, grant_id, source_id, workspace_id, json.dumps(clean, ensure_ascii=False), rationale,
             source_url, confidence),
        )
        return cur.lastrowid


@mcp.tool()
def list_sources(workspace: str = "") -> str:
    """One workspace's discovery sources (id, name, url, hints, enabled,
    last_scanned_at, last_scan_outcome). Use the id as source_id when filing
    proposals that belong to one of these sources. last_scan_outcome='error'
    means the monthly scan is not refreshing that source's grants."""
    ws, err = _ws(workspace)
    if err:
        return err
    with get_db() as db:
        rows = db.execute(
            "SELECT s.id, s.name, s.url, s.hints, s.enabled, s.last_scanned_at, "
            "(SELECT outcome FROM scan_log l WHERE l.source_id = s.id AND l.outcome IS NOT NULL "
            " ORDER BY l.id DESC LIMIT 1) AS last_scan_outcome "
            "FROM sources s WHERE s.workspace_id=? ORDER BY s.name", (ws["id"],)).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
def propose_grant(fields: dict, rationale: str, source_url: str = "", confidence: str = "medium",
                  source_id: int | None = None, workspace: str = "") -> str:
    """Propose a NEW grant for a workspace's queue (needs human approval in the
    UI; the caller needs the write role there). fields: subset of {name,
    funder, scope, max_amount, duration_months, deadline, deadline_date
    (YYYY-MM-DD), deadline_logic, link, notes, grant_start, primary_type,
    recurring ('yes' | 'no': whether further editions are expected; a 'no' row
    is closed automatically after its deadline), fit ('high' | 'medium' | 'low':
    how well the call matches the workspace's research profile, see
    list_workspaces)}. name is required. confidence is about how well the facts
    were verified, fit about relevance: keep them apart. source_id (see
    list_sources) links the proposal to the discovery source it belongs to; on
    approval the grant inherits it."""
    ws, err = _ws(workspace, "write")
    if err:
        return err
    if not fields.get("name"):
        return _err("name_required", "fields.name is required")
    pid = _insert_proposal("new", None, fields, rationale, source_url, confidence, source_id, ws["id"])
    return json.dumps({"ok": True, "proposal_id": pid, "workspace": ws["slug"]})


@mcp.tool()
def propose_update(grant_id: int, fields: dict, rationale: str, source_url: str = "",
                   confidence: str = "medium", source_id: int | None = None) -> str:
    """Propose an UPDATE to an existing grant (needs human approval in the UI;
    the caller needs the write role in the grant's workspace). fields: only the
    fields that should change, among those of propose_grant plus status
    ('open' | 'closed' | 'archived') — propose status='closed' for a
    discontinued scheme or a one-off call that is over. source_id (see
    list_sources) is inherited by the grant on approval if the grant has none
    yet."""
    grant, err = _grant_for_caller(grant_id, "write")
    if err:
        return err
    if not clean_proposal_fields(fields):
        return _err("no_valid_fields", "fields is empty, or holds only unknown fields or values")
    pid = _insert_proposal("update", grant_id, fields, rationale, source_url, confidence, source_id,
                           grant["workspace_id"])
    return json.dumps({"ok": True, "proposal_id": pid})
