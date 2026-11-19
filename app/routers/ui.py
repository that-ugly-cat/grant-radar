"""HTMX/Jinja2 web UI: one table, queue and source list per workspace.

Everything a person works on lives under /w/{slug}/…; the slug is checked
against the caller's memberships on every request, and a workspace that is not
theirs answers 404 exactly like one that does not exist. The paths without a
slug (/app, /proposals, /sources) are the old bookmarks: they redirect to the
workspace the reader was last in.
"""
import json
import os
import threading
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth import (BORANT_LOGOUT_URL, COOKIE_NAME, gateway_mode,
                    get_user_or_none, hash_password, make_token, new_api_key,
                    require_admin, require_user, verify_password)
from ..db import (failed_scan_sources, get_db, status_condition, FIT_VALUES, GRANT_FIELDS,
                  RECURRING_VALUES, STATUS_VALUES)
from ..discovery.link_monitor import run_link_monitor
from ..discovery.scanner import run_scan, scan_running
from ..discovery.verifier import verify_grant
from ..mcp_server import tool_names
from ..proposals import approve, compute_diff, reject
from ..version import commit_hash
from .. import workspaces as W

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))
templates.env.globals["commit"] = commit_hash()

WS_COOKIE = "gr_ws"


def _uid(user: dict) -> int:
    return int(user["sub"])


def _ws(slug: str, user: dict, need: str = "read") -> dict:
    """The workspace, or 404 — the same 404 whether it is missing or simply not
    the caller's. A member without the role gets 403: they already know it exists."""
    ws = W.workspace_for(_uid(user), slug)
    if not ws:
        raise HTTPException(status_code=404, detail="No such workspace")
    if not W.role_allows(ws["role"], need):
        raise HTTPException(status_code=403, detail=f"This needs the '{need}' role in {ws['name']}")
    return ws


def _default_ws(request: Request, user: dict) -> dict:
    """Where the old bookmarks land: the workspace last visited, if it is still
    the caller's, otherwise their personal one."""
    slug = request.cookies.get(WS_COOKIE, "")
    return (W.workspace_for(_uid(user), slug) if slug else None) or W.workspace_for(_uid(user), "")


def _render(request: Request, name: str, ws: dict | None = None, **ctx) -> HTMLResponse:
    ctx.setdefault("user", get_user_or_none(request))
    user = ctx["user"]
    ctx["ws"] = ws
    ctx["base"] = f"/w/{ws['slug']}" if ws else ""
    ctx["can_write"] = bool(ws) and W.role_allows(ws["role"], "write")
    ctx["can_admin"] = bool(ws) and W.role_allows(ws["role"], "admin")
    ctx.setdefault("workspaces", W.user_workspaces(_uid(user)) if user else [])
    # Only when the caller has not answered it already: the showcase passes
    # these explicitly, so a public page neither identifies its reader nor runs
    # a query for a number it does not print.
    if "pending_count" not in ctx:
        n = 0
        if ws:
            with get_db() as db:
                n = db.execute("SELECT COUNT(*) AS n FROM proposals WHERE status='pending' AND workspace_id=?",
                               (ws["id"],)).fetchone()["n"]
        ctx["pending_count"] = n
    # A scan that fails writes to a log nobody reads: in the autumn of 2026
    # every source answered 401 for three months and the table simply stopped
    # being refreshed. The workspace's own pages say so.
    if "scan_failures" not in ctx:
        ctx["scan_failures"] = failed_scan_sources(ws["id"]) if ws else []
    if ws and ctx["scan_failures"]:
        with get_db() as db:
            payer = db.execute("SELECT username FROM users WHERE id=?", (ws["payer_user_id"],)).fetchone()
        ctx["payer_name"] = payer["username"] if payer else None
    resp = templates.TemplateResponse(request, name, ctx)
    if ws:
        resp.set_cookie(WS_COOKIE, ws["slug"], httponly=True, samesite="lax", max_age=3600 * 24 * 365)
    return resp


def _back(ws: dict, path: str = "/app", **params) -> RedirectResponse:
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v not in (None, ""))
    return RedirectResponse(f"/w/{ws['slug']}{path}" + (f"?{query}" if query else ""), status_code=303)


# --- landing (pubblica) ---

@router.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    """The user guide, rendered from docs/guide.md, and public like the landing.

    It is linked from the card on borant.eu/tools, where the reader has no
    account yet and is deciding whether the tool is worth asking for one. Behind
    the gate it would document the tool to the people who already use it, which
    is the wrong half of the audience. One source of truth: the markdown in the
    repository, so the page cannot drift from what ships.
    """
    import markdown
    md_text = open(os.path.join(os.path.dirname(__file__), "..", "..", "docs", "guide.md"), encoding="utf-8").read()
    return templates.TemplateResponse(request, "guide.html", {
        "guide_html": markdown.markdown(md_text, extensions=["tables", "fenced_code"]),
        "app_name": "Grant Radar",
        "app_url": "/app",
    })


@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    # The tool list comes from the MCP registry, so the page cannot go on
    # claiming a number the code has since outgrown.
    #
    # user=None is the rule and not an omission: on the public branch of the
    # reverse proxy the X-Borant-* headers are stripped by construction, so a
    # branch on the reader is always false behind the gate and sometimes true
    # without one — the same page with two behaviours. By not asking, this page
    # is identical in both modes.
    return _render(request, "landing.html", mcp_tools=tool_names(),
                   user=None, pending_count=None, scan_failures=[], workspaces=[])


# --- auth ---

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    # In gateway mode the app switches its own login off rather than trusting
    # the proxy to hide it: the app knows its own mode better than the reverse
    # proxy does, and two sets of credentials for one table is exactly what the
    # SSO exists to remove.
    #
    # It sends you to a GATED path, not to `/`. That distinction is the whole
    # bug this line exists to fix: `/` is public, so the gate never sees the
    # request and the identity headers are stripped from it — the landing can
    # therefore never recognise you, and its only button points back here. `/`
    # -> `/login` -> `/` is a closed loop with no way in. Pointing at `/grants`
    # hands the request to the gate, which is the one thing able to log you in.
    if gateway_mode():
        return RedirectResponse("/app", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None, "user": None, "pending_count": 0})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if gateway_mode():
        return RedirectResponse("/app", status_code=303)
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid credentials", "user": None, "pending_count": 0}, status_code=401)
    token = make_token(row["id"], row["username"], row["role"])
    resp = RedirectResponse("/app", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=3600 * 24 * 30)
    return resp


@router.get("/logout")
def logout():
    # Dropping the local cookie is not signing out when the gate holds the
    # session: the next click would walk straight back in. The target is the
    # gate's GET /logout, which asks — POST /logout is what revokes, and it has
    # to stay a POST or an <img src> on any site would sign people out.
    target = BORANT_LOGOUT_URL if gateway_mode() else "/login"
    resp = RedirectResponse(target, status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


EXPORT_COLS = [
    ("Name", "name"), ("Funder", "funder"), ("Scope", "scope"), ("Max amount", "max_amount"),
    ("Duration (months)", "duration_months"), ("Deadline", "deadline"), ("Deadline date", "deadline_date"),
    ("Recurrence", "deadline_logic"), ("Recurring", "recurring"), ("Fit", "fit"), ("Link", "link"), ("Notes", "notes"), ("Grant start", "grant_start"),
    ("Type", "primary_type"), ("Source", "source_name"), ("Status", "status"), ("Origin", "origin"),
]


def _xl_safe(value):
    """Formula-injection guard: neutralize leading =, +, -, @ in text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value



# --- old bookmarks ---

def _legacy(request: Request, user: dict, path: str) -> RedirectResponse:
    """`/app?status=expired` and the like are filtered views somebody has
    bookmarked: the redirect carries the query string, or every one of them
    would turn into the same page without saying so."""
    ws = _default_ws(request, user)
    tail = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/w/{ws['slug']}{path}{tail}", status_code=303)


@router.get("/app")
def app_legacy(request: Request, user=Depends(require_user)):
    return _legacy(request, user, "/app")


@router.get("/grants")
def grants_legacy(request: Request, user=Depends(require_user)):
    return _legacy(request, user, "/app")


@router.get("/proposals")
def proposals_legacy(request: Request, user=Depends(require_user)):
    return _legacy(request, user, "/proposals")


@router.get("/sources")
def sources_legacy(request: Request, user=Depends(require_user)):
    return _legacy(request, user, "/sources")


# --- grants ---

def _query_grants(ws_id, q, funder, ptype, source, status, fit):
    sql = ("SELECT g.*, s.name AS source_name FROM grants g LEFT JOIN sources s ON s.id = g.source_id "
           "WHERE g.workspace_id=?")
    args: list = [ws_id]
    if q:
        sql += " AND (g.name LIKE ? OR g.scope LIKE ? OR g.notes LIKE ? OR g.funder LIKE ?)"
        args += [f"%{q}%"] * 4
    if funder:
        sql += " AND g.funder = ?"
        args.append(funder)
    if ptype == "__none__":
        sql += " AND (g.primary_type IS NULL OR g.primary_type = '')"
    elif ptype:
        sql += " AND g.primary_type = ?"
        args.append(ptype)
    if source == "__none__":
        sql += " AND g.source_id IS NULL"
    elif source and source.isdigit():
        sql += " AND g.source_id = ?"
        args.append(int(source))
    if fit == "__none__":
        sql += " AND g.fit IS NULL"
    elif fit in FIT_VALUES:
        sql += " AND g.fit = ?"
        args.append(fit)
    cond, cond_args = status_condition(status, prefix="g.")
    sql += cond
    args += cond_args
    sql += " ORDER BY g.deadline_date IS NULL, g.deadline_date"
    with get_db() as db:
        rows = db.execute(sql, args).fetchall()
        funders = [r["funder"] for r in db.execute(
            "SELECT DISTINCT funder FROM grants WHERE workspace_id=? AND funder IS NOT NULL AND funder != '' "
            "ORDER BY funder", (ws_id,))]
        types = _known_types(ws_id, db)
        sources = [dict(r) for r in db.execute(
            "SELECT id, name FROM sources WHERE workspace_id=? ORDER BY name", (ws_id,))]
    return [dict(r) for r in rows], funders, types, sources


def _known_types(ws_id: int, db=None) -> list[str]:
    sql = ("SELECT DISTINCT primary_type FROM grants WHERE workspace_id=? "
           "AND primary_type IS NOT NULL AND primary_type != '' ORDER BY primary_type")
    if db is not None:
        return [r["primary_type"] for r in db.execute(sql, (ws_id,))]
    with get_db() as conn:
        return [r["primary_type"] for r in conn.execute(sql, (ws_id,))]


def _all_sources(ws_id: int) -> list[dict]:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, name FROM sources WHERE workspace_id=? ORDER BY name", (ws_id,))]


def _grant_in(ws: dict, grant_id: int) -> dict:
    """The grant, if it is this workspace's; 404 otherwise."""
    with get_db() as db:
        row = db.execute(
            "SELECT g.*, s.name AS source_name FROM grants g LEFT JOIN sources s ON s.id = g.source_id "
            "WHERE g.id=? AND g.workspace_id=?", (grant_id, ws["id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No such grant in this workspace")
    return dict(row)


def _form_status(form) -> str:
    raw = form.get("status") or "open"
    return raw if raw in STATUS_VALUES else "open"


def _form_choice(form, name: str, allowed) -> str | None:
    raw = form.get(name) or ""
    return raw if raw in allowed else None


def _form_source_id(form, ws_id: int) -> int | None:
    """The source picked in the form, only if it is this workspace's."""
    raw = form.get("source_id") or ""
    if not raw.isdigit():
        return None
    with get_db() as db:
        ok = db.execute("SELECT 1 FROM sources WHERE id=? AND workspace_id=?", (int(raw), ws_id)).fetchone()
    return int(raw) if ok else None


@router.get("/w/{slug}/app", response_class=HTMLResponse)
def grants_page(request: Request, slug: str, q: str = "", funder: str = "", primary_type: str = "",
                source: str = "", status: str = "open", fit: str = "", user=Depends(require_user)):
    ws = _ws(slug, user)
    grants, funders, types, sources = _query_grants(ws["id"], q, funder, primary_type, source, status, fit)
    ctx = {"grants": grants, "funders": funders, "types": types, "sources": sources,
           "q": q, "f_funder": funder, "f_type": primary_type, "f_source": source,
           "f_status": status, "f_fit": fit, "today": date.today().isoformat()}
    if request.headers.get("HX-Request"):
        return _render(request, "_grants_table.html", ws, **ctx)
    return _render(request, "grants.html", ws, **ctx)


@router.get("/w/{slug}/grants/export.xlsx")
def grants_export(slug: str, q: str = "", funder: str = "", primary_type: str = "",
                  source: str = "", status: str = "open", fit: str = "", user=Depends(require_user)):
    import io
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    ws = _ws(slug, user)
    grants, _, _, _ = _query_grants(ws["id"], q, funder, primary_type, source, status, fit)
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Grants"
    sheet.append([h for h, _ in EXPORT_COLS])
    for cell in sheet[1]:
        cell.font = cell.font.copy(bold=True)
    for g in grants:
        sheet.append([_xl_safe(g.get(k)) for _, k in EXPORT_COLS])
    for i, (header, key) in enumerate(EXPORT_COLS, start=1):
        width = max([len(header)] + [len(str(g.get(key) or "")) for g in grants] or [10])
        sheet.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 60)
    sheet.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    filename = f"grantradar-{ws['slug']}-{date.today().isoformat()}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        # no-store: a spreadsheet behind a login must not be kept by a cache in front of it
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/w/{slug}/grants/new", response_class=HTMLResponse)
def grant_new(request: Request, slug: str, user=Depends(require_user)):
    ws = _ws(slug, user, "write")
    return _render(request, "grant_form.html", ws, grant={}, action=f"/w/{slug}/grants/new",
                   types=_known_types(ws["id"]), sources=_all_sources(ws["id"]))


@router.post("/w/{slug}/grants/new")
async def grant_create(request: Request, slug: str, user=Depends(require_user)):
    ws = _ws(slug, user, "write")
    form = await request.form()
    fields = {k: (form.get(k) or None) for k in GRANT_FIELDS}
    with get_db() as db:
        db.execute(
            f"INSERT INTO grants ({', '.join(fields)}, source_id, status, recurring, fit, workspace_id) "
            f"VALUES ({', '.join('?' * len(fields))}, ?, ?, ?, ?, ?)",
            list(fields.values()) + [_form_source_id(form, ws["id"]), _form_status(form),
                                     _form_choice(form, "recurring", RECURRING_VALUES),
                                     _form_choice(form, "fit", FIT_VALUES), ws["id"]],
        )
    return _back(ws)


@router.get("/w/{slug}/grants/{grant_id}/detail", response_class=HTMLResponse)
def grant_detail(request: Request, slug: str, grant_id: int, user=Depends(require_user)):
    ws = _ws(slug, user)
    try:
        grant = _grant_in(ws, grant_id)
    except HTTPException:
        return HTMLResponse("")
    targets = [w for w in W.user_workspaces(_uid(user))
               if w["id"] != ws["id"] and W.role_allows(w["role"], "write")]
    return templates.TemplateResponse(request, "_grant_modal.html", {
        "g": grant, "user": user, "today": date.today().isoformat(), "ws": ws, "base": f"/w/{slug}",
        "can_write": W.role_allows(ws["role"], "write"), "copy_targets": targets})


@router.get("/w/{slug}/grants/{grant_id}/edit", response_class=HTMLResponse)
def grant_edit(request: Request, slug: str, grant_id: int, user=Depends(require_user)):
    ws = _ws(slug, user, "write")
    return _render(request, "grant_form.html", ws, grant=_grant_in(ws, grant_id),
                   action=f"/w/{slug}/grants/{grant_id}/edit",
                   types=_known_types(ws["id"]), sources=_all_sources(ws["id"]))


@router.post("/w/{slug}/grants/{grant_id}/edit")
async def grant_update(request: Request, slug: str, grant_id: int, user=Depends(require_user)):
    ws = _ws(slug, user, "write")
    _grant_in(ws, grant_id)
    form = await request.form()
    fields = {k: (form.get(k) or None) for k in GRANT_FIELDS}
    sets = ", ".join(f"{k}=?" for k in fields)
    with get_db() as db:
        db.execute(
            f"UPDATE grants SET {sets}, source_id=?, status=?, recurring=?, fit=?, "
            f"updated_at=CURRENT_TIMESTAMP WHERE id=? AND workspace_id=?",
            list(fields.values()) + [_form_source_id(form, ws["id"]), _form_status(form),
                                     _form_choice(form, "recurring", RECURRING_VALUES),
                                     _form_choice(form, "fit", FIT_VALUES), grant_id, ws["id"]],
        )
    return _back(ws)


@router.post("/w/{slug}/grants/{grant_id}/check")
def grant_check(slug: str, grant_id: int, user=Depends(require_user)):
    ws = _ws(slug, user, "write")
    _grant_in(ws, grant_id)
    if not W.payer_key(ws):
        return _back(ws, notice="nokey")
    threading.Thread(target=verify_grant, args=(grant_id,), daemon=True).start()
    return _back(ws, notice="checked")


@router.post("/w/{slug}/grants/{grant_id}/copy")
def grant_copy(slug: str, grant_id: int, target: str = Form(...), user=Depends(require_user)):
    ws = _ws(slug, user)
    _grant_in(ws, grant_id)
    dest = _ws(target, user, "write")
    W.copy_grant(grant_id, dest["id"])
    return _back(ws, notice="copied", to=dest["name"])


@router.post("/w/{slug}/grants/{grant_id}/delete")
def grant_delete(slug: str, grant_id: int, user=Depends(require_user)):
    ws = _ws(slug, user, "write")
    _grant_in(ws, grant_id)
    with get_db() as db:
        db.execute("DELETE FROM proposals WHERE grant_id=? AND workspace_id=?", (grant_id, ws["id"]))
        db.execute("DELETE FROM grants WHERE id=? AND workspace_id=?", (grant_id, ws["id"]))
    return _back(ws)


# --- proposals ---

@router.get("/w/{slug}/proposals", response_class=HTMLResponse)
def proposals_page(request: Request, slug: str, status: str = "pending", user=Depends(require_user)):
    ws = _ws(slug, user)
    with get_db() as db:
        rows = db.execute(
            "SELECT p.*, g.name AS grant_name FROM proposals p "
            "LEFT JOIN grants g ON g.id = p.grant_id "
            "WHERE p.status=? AND p.workspace_id=? ORDER BY p.created_at DESC", (status, ws["id"])).fetchall()
    items = []
    for r in rows:
        p = dict(r)
        p["diff"] = compute_diff(p)
        items.append(p)
    return _render(request, "proposals.html", ws, proposals=items, f_status=status)


@router.post("/w/{slug}/proposals/{proposal_id}/approve")
def proposal_approve(slug: str, proposal_id: int, user=Depends(require_user)):
    ws = _ws(slug, user, "write")
    approve(proposal_id, ws["id"])
    return _back(ws, "/proposals")


@router.post("/w/{slug}/proposals/{proposal_id}/reject")
def proposal_reject(slug: str, proposal_id: int, user=Depends(require_user)):
    ws = _ws(slug, user, "write")
    reject(proposal_id, ws["id"])
    return _back(ws, "/proposals")


# --- sources & discovery ---

def _scan_summary(detail: str | None) -> str:
    """One readable line from a scan_log detail, which is stored as the JSON of
    the whole result. Anything that is not that JSON is shown as it is."""
    try:
        d = json.loads(detail or "")
    except ValueError:
        return detail or ""
    if not isinstance(d, dict):
        return detail or ""
    if d.get("outcome") == "ok":
        return f"{d.get('proposed', 0)} proposed, {d.get('stored', 0)} stored"
    return str(d.get("detail") or detail)


def _source_in(ws: dict, source_id: int) -> dict:
    with get_db() as db:
        row = db.execute("SELECT * FROM sources WHERE id=? AND workspace_id=?", (source_id, ws["id"])).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No such source in this workspace")
    return dict(row)


@router.get("/w/{slug}/sources", response_class=HTMLResponse)
def sources_page(request: Request, slug: str, user=Depends(require_user)):
    ws = _ws(slug, user)
    with get_db() as db:
        sources = [dict(r) for r in db.execute(
            "SELECT * FROM sources WHERE workspace_id=? ORDER BY name", (ws["id"],)).fetchall()]
        log = [dict(r) for r in db.execute(
            "SELECT l.*, s.name AS source_name FROM scan_log l "
            "JOIN sources s ON s.id = l.source_id WHERE s.workspace_id=? "
            "ORDER BY l.started_at DESC, l.id DESC LIMIT 30", (ws["id"],)).fetchall()]
    for entry in log:
        entry["summary"] = _scan_summary(entry["detail"])
    return _render(request, "sources.html", ws, sources=sources, scan_log=log,
                   scan_running=scan_running(), has_key=bool(W.payer_key(ws)))


@router.post("/w/{slug}/sources/new")
def source_create(slug: str, name: str = Form(...), url: str = Form(""), hints: str = Form(""),
                  user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    with get_db() as db:
        db.execute("INSERT INTO sources (name, url, hints, workspace_id) VALUES (?, ?, ?, ?)",
                   (name, url or None, hints or None, ws["id"]))
    return _back(ws, "/sources")


@router.get("/w/{slug}/sources/export.json")
def sources_export(slug: str, user=Depends(require_user)):
    ws = _ws(slug, user)
    body = json.dumps(W.export_sources(ws), ensure_ascii=False, indent=2)
    return Response(content=body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="grantradar-sources-{ws["slug"]}.json"',
        "Cache-Control": "no-store"})


@router.post("/w/{slug}/sources/import")
async def sources_import(slug: str, file: UploadFile, user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    raw = await file.read(2_000_000)
    result = W.import_sources(ws["id"], raw)
    if not result["ok"]:
        return _back(ws, "/sources", notice="import_failed", why=result["error"])
    return _back(ws, "/sources", notice="imported", added=result["added"], updated=result["updated"])


@router.get("/w/{slug}/sources/{source_id}/edit", response_class=HTMLResponse)
def source_edit(request: Request, slug: str, source_id: int, user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    return _render(request, "source_form.html", ws, source=_source_in(ws, source_id))


@router.post("/w/{slug}/sources/{source_id}/edit")
def source_update(slug: str, source_id: int, name: str = Form(...), url: str = Form(""),
                  hints: str = Form(""), user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    with get_db() as db:
        db.execute("UPDATE sources SET name=?, url=?, hints=? WHERE id=? AND workspace_id=?",
                   (name, url or None, hints or None, source_id, ws["id"]))
    return _back(ws, "/sources")


@router.post("/w/{slug}/sources/{source_id}/toggle")
def source_toggle(slug: str, source_id: int, user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    with get_db() as db:
        db.execute("UPDATE sources SET enabled = 1 - enabled WHERE id=? AND workspace_id=?", (source_id, ws["id"]))
    return _back(ws, "/sources")


@router.post("/w/{slug}/sources/{source_id}/delete")
def source_delete(slug: str, source_id: int, user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    with get_db() as db:
        db.execute("DELETE FROM scan_log WHERE source_id IN (SELECT id FROM sources WHERE id=? AND workspace_id=?)",
                   (source_id, ws["id"]))
        db.execute("DELETE FROM sources WHERE id=? AND workspace_id=?", (source_id, ws["id"]))
    return _back(ws, "/sources")


@router.post("/w/{slug}/scan-now")
def scan_now(slug: str, source_id: int | None = Form(None), user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    if source_id:
        _source_in(ws, source_id)
    if not W.payer_key(ws):
        return _back(ws, "/sources", notice="nokey")
    threading.Thread(target=run_scan, args=(ws["id"], source_id), daemon=True).start()
    return _back(ws, "/sources")


@router.post("/w/{slug}/check-links-now")
def check_links_now(slug: str, user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    threading.Thread(target=run_link_monitor, args=(ws["id"],), daemon=True).start()
    return _back(ws, "/sources")


# --- workspace settings & members ---

@router.get("/w/{slug}/settings", response_class=HTMLResponse)
def settings_page(request: Request, slug: str, user=Depends(require_user)):
    ws = _ws(slug, user)
    members = W.members(ws["id"])
    with get_db() as db:
        others = [dict(r) for r in db.execute(
            "SELECT id, username, email FROM users WHERE active=1 AND id NOT IN "
            "(SELECT user_id FROM memberships WHERE workspace_id=?) ORDER BY username", (ws["id"],))]
    return _render(request, "settings.html", ws, members=members, candidates=others,
                   payer=next((m for m in members if m["id"] == ws["payer_user_id"]), None))


@router.post("/w/{slug}/settings")
def settings_save(slug: str, name: str = Form(""), profile: str = Form(""), auto_scan: str = Form(""),
                  payer_user_id: str = Form(""), user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    new_name = name.strip() if ws["kind"] == "shared" and name.strip() else ws["name"]
    payer = ws["payer_user_id"]
    if payer_user_id.isdigit() and W.role_in(int(payer_user_id), ws["id"]) == "admin":
        payer = int(payer_user_id)   # the bill only ever goes to an admin of this workspace
    with get_db() as db:
        db.execute("UPDATE workspaces SET name=?, profile=?, auto_scan=?, payer_user_id=? WHERE id=?",
                   (new_name, profile.strip() or None, 1 if auto_scan else 0, payer, ws["id"]))
    return _back(ws, "/settings", notice="saved")


@router.post("/w/{slug}/members")
def member_set(slug: str, user_id: int = Form(...), role: str = Form("read"), user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    if ws["kind"] != "shared":
        raise HTTPException(status_code=400, detail="A personal workspace has one member")
    if role not in W.ROLES:
        raise HTTPException(status_code=422, detail="Unknown role")
    # Demoting the last admin would leave a workspace nobody can manage.
    if role != "admin" and W.role_in(user_id, ws["id"]) == "admin" and W.admin_count(ws["id"]) <= 1:
        return _back(ws, "/settings", notice="last_admin")
    with get_db() as db:
        if not db.execute("SELECT 1 FROM users WHERE id=? AND active=1", (user_id,)).fetchone():
            raise HTTPException(status_code=404, detail="No such user")
    W.set_member(ws["id"], user_id, role)
    return _back(ws, "/settings", notice="saved")


@router.post("/w/{slug}/members/{user_id}/remove")
def member_remove(slug: str, user_id: int, user=Depends(require_user)):
    ws = _ws(slug, user, "admin")
    if ws["kind"] != "shared":
        raise HTTPException(status_code=400, detail="A personal workspace has one member")
    if W.role_in(user_id, ws["id"]) == "admin" and W.admin_count(ws["id"]) <= 1:
        return _back(ws, "/settings", notice="last_admin")
    W.remove_member(ws["id"], user_id)
    return _back(ws, "/settings", notice="saved")


# --- my account: my Anthropic key, my API keys ---

@router.get("/me", response_class=HTMLResponse)
def me_page(request: Request, user=Depends(require_user)):
    uid = _uid(user)
    with get_db() as db:
        keys = [dict(r) for r in db.execute(
            "SELECT * FROM api_keys WHERE user_id=? ORDER BY created_at DESC", (uid,))]
        paying = [dict(r) for r in db.execute(
            "SELECT slug, name FROM workspaces WHERE payer_user_id=? ORDER BY name", (uid,))]
    return _render(request, "me.html", _default_ws(request, user), api_keys=keys, paying=paying,
                   key_hint=W.key_hint(uid), new_key=request.query_params.get("new_key"),
                   notice=request.query_params.get("notice"))


@router.post("/me/anthropic-key")
def me_set_key(key: str = Form(""), user=Depends(require_user)):
    key = key.strip()
    if key and not key.startswith("sk-ant-"):
        return RedirectResponse("/me?notice=bad_key", status_code=303)
    try:
        W.set_user_api_key(_uid(user), key or None)
    except RuntimeError:   # the server has no secret to encrypt with
        return RedirectResponse("/me?notice=no_secret", status_code=303)
    return RedirectResponse("/me?notice=" + ("key_saved" if key else "key_removed"), status_code=303)


@router.post("/me/keys/new")
def me_key_create(label: str = Form(...), user=Depends(require_user)):
    key = new_api_key()
    with get_db() as db:
        db.execute("INSERT INTO api_keys (label, key, user_id) VALUES (?, ?, ?)", (label, key, _uid(user)))
    # Shown once, in the query string of the page that follows.
    return RedirectResponse(f"/me?new_key={key}", status_code=303)


@router.post("/me/keys/{key_id}/revoke")
def me_key_revoke(key_id: int, user=Depends(require_user)):
    with get_db() as db:
        db.execute("UPDATE api_keys SET active=0 WHERE id=? AND user_id=?", (key_id, _uid(user)))
    return RedirectResponse("/me", status_code=303)


# --- platform admin: accounts and shared workspaces ---

@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user=Depends(require_admin)):
    with get_db() as db:
        users = [dict(r) for r in db.execute(
            "SELECT id, username, email, role, active, anthropic_key_enc IS NOT NULL AS has_key "
            "FROM users ORDER BY username")]
        spaces = [dict(r) for r in db.execute(
            "SELECT w.*, p.username AS payer_name, "
            "(SELECT COUNT(*) FROM memberships m WHERE m.workspace_id = w.id) AS n_members, "
            "(SELECT COUNT(*) FROM grants g WHERE g.workspace_id = w.id) AS n_grants "
            "FROM workspaces w LEFT JOIN users p ON p.id = w.payer_user_id "
            "ORDER BY w.kind != 'shared', w.name")]
    return _render(request, "admin.html", _default_ws(request, user), users=users, spaces=spaces)


@router.post("/admin/workspaces/new")
def workspace_create(name: str = Form(...), admin_user_id: int = Form(...), user=Depends(require_admin)):
    if not name.strip():
        raise HTTPException(status_code=422, detail="A workspace needs a name")
    with get_db() as db:
        if not db.execute("SELECT 1 FROM users WHERE id=? AND active=1", (admin_user_id,)).fetchone():
            raise HTTPException(status_code=404, detail="No such user")
    ws = W.create_shared(name, admin_user_id)
    if W.role_in(_uid(user), ws["id"]):
        return RedirectResponse(f"/w/{ws['slug']}/settings", status_code=303)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/users/new")
def user_create(username: str = Form(...), email: str = Form(...), password: str = Form(...),
                role: str = Form("reader"), user=Depends(require_admin)):
    with get_db() as db:
        db.execute("INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)",
                   (username, email, hash_password(password), role if role in ("reader", "admin") else "reader"))
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/users/{user_id}/password")
def user_set_password(user_id: int, password: str = Form(...), user=Depends(require_admin)):
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="Password too short (min 8 chars)")
    with get_db() as db:
        cur = db.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (hash_password(password), user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/users/{user_id}/toggle")
def user_toggle(user_id: int, user=Depends(require_admin)):
    with get_db() as db:
        db.execute("UPDATE users SET active = 1 - active WHERE id=?", (user_id,))
    return RedirectResponse("/admin", status_code=303)
