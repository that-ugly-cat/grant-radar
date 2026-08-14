"""HTMX/Jinja2 web UI: grants table, proposals queue, sources, admin, auth."""
import json
import os
import threading
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import (COOKIE_NAME, get_user_or_none, hash_password, make_token,
                    new_api_key, require_admin, require_user, verify_password)
from ..db import get_db, status_condition, GRANT_FIELDS
from ..discovery.link_monitor import run_link_monitor
from ..discovery.scanner import run_scan
from ..proposals import approve, compute_diff, reject
from ..version import commit_hash

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))
templates.env.globals["commit"] = commit_hash()


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", get_user_or_none(request))
    with get_db() as db:
        ctx.setdefault("pending_count", db.execute(
            "SELECT COUNT(*) AS n FROM proposals WHERE status='pending'").fetchone()["n"])
    return templates.TemplateResponse(request, name, ctx)


# --- landing (pubblica) ---

@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return _render(request, "landing.html")


# --- auth ---

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None, "user": None, "pending_count": 0})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid credentials", "user": None, "pending_count": 0}, status_code=401)
    token = make_token(row["id"], row["username"], row["role"])
    resp = RedirectResponse("/grants", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=3600 * 24 * 30)
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --- grants ---

def _query_grants(q, funder, ptype, status):
    sql = "SELECT * FROM grants WHERE 1=1"
    args: list = []
    if q:
        sql += " AND (name LIKE ? OR scope LIKE ? OR notes LIKE ? OR funder LIKE ?)"
        args += [f"%{q}%"] * 4
    if funder:
        sql += " AND funder = ?"
        args.append(funder)
    if ptype == "__none__":
        sql += " AND (primary_type IS NULL OR primary_type = '')"
    elif ptype:
        sql += " AND primary_type = ?"
        args.append(ptype)
    cond, cond_args = status_condition(status)
    sql += cond
    args += cond_args
    sql += " ORDER BY deadline_date IS NULL, deadline_date"
    with get_db() as db:
        rows = db.execute(sql, args).fetchall()
        funders = [r["funder"] for r in db.execute(
            "SELECT DISTINCT funder FROM grants WHERE funder IS NOT NULL AND funder != '' ORDER BY funder")]
        types = [r["primary_type"] for r in db.execute(
            "SELECT DISTINCT primary_type FROM grants WHERE primary_type IS NOT NULL AND primary_type != '' ORDER BY primary_type")]
    return [dict(r) for r in rows], funders, types


@router.get("/grants", response_class=HTMLResponse)
def grants_page(request: Request, q: str = "", funder: str = "", primary_type: str = "",
                status: str = "open", user=Depends(require_user)):
    grants, funders, types = _query_grants(q, funder, primary_type, status)
    ctx = {"grants": grants, "funders": funders, "types": types, "q": q, "f_funder": funder,
           "f_type": primary_type, "f_status": status,
           "today": date.today().isoformat()}

    if request.headers.get("HX-Request"):
        return _render(request, "_grants_table.html", **ctx)
    return _render(request, "grants.html", **ctx)


def _known_types() -> list[str]:
    with get_db() as db:
        return [r["primary_type"] for r in db.execute(
            "SELECT DISTINCT primary_type FROM grants "
            "WHERE primary_type IS NOT NULL AND primary_type != '' ORDER BY primary_type")]


@router.get("/grants/new", response_class=HTMLResponse)
def grant_new(request: Request, user=Depends(require_admin)):
    return _render(request, "grant_form.html", grant={}, action="/grants/new", types=_known_types())


@router.post("/grants/new")
async def grant_create(request: Request, user=Depends(require_admin)):
    form = await request.form()
    fields = {k: (form.get(k) or None) for k in GRANT_FIELDS}
    with get_db() as db:
        db.execute(
            f"INSERT INTO grants ({', '.join(fields)}, status) VALUES ({', '.join('?' * len(fields))}, ?)",
            list(fields.values()) + [form.get("status") or "open"],
        )
    return RedirectResponse("/grants", status_code=303)


@router.get("/grants/{grant_id}/edit", response_class=HTMLResponse)
def grant_edit(request: Request, grant_id: int, user=Depends(require_admin)):
    with get_db() as db:
        row = db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
    if not row:
        return RedirectResponse("/grants", status_code=303)
    return _render(request, "grant_form.html", grant=dict(row), action=f"/grants/{grant_id}/edit", types=_known_types())


@router.post("/grants/{grant_id}/edit")
async def grant_update(request: Request, grant_id: int, user=Depends(require_admin)):
    form = await request.form()
    fields = {k: (form.get(k) or None) for k in GRANT_FIELDS}
    sets = ", ".join(f"{k}=?" for k in fields)
    with get_db() as db:
        db.execute(
            f"UPDATE grants SET {sets}, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            list(fields.values()) + [form.get("status") or "open", grant_id],
        )
    return RedirectResponse("/grants", status_code=303)


@router.post("/grants/{grant_id}/delete")
def grant_delete(grant_id: int, user=Depends(require_admin)):
    with get_db() as db:
        db.execute("DELETE FROM proposals WHERE grant_id=?", (grant_id,))
        db.execute("DELETE FROM grants WHERE id=?", (grant_id,))
    return RedirectResponse("/grants", status_code=303)


# --- proposals ---

@router.get("/proposals", response_class=HTMLResponse)
def proposals_page(request: Request, status: str = "pending", user=Depends(require_user)):
    with get_db() as db:
        rows = db.execute(
            "SELECT p.*, g.name AS grant_name FROM proposals p "
            "LEFT JOIN grants g ON g.id = p.grant_id "
            "WHERE p.status=? ORDER BY p.created_at DESC", (status,)).fetchall()
    items = []
    for r in rows:
        p = dict(r)
        p["diff"] = compute_diff(p)
        items.append(p)
    return _render(request, "proposals.html", proposals=items, f_status=status)


@router.post("/proposals/{proposal_id}/approve")
def proposal_approve(proposal_id: int, user=Depends(require_admin)):
    approve(proposal_id)
    return RedirectResponse("/proposals", status_code=303)


@router.post("/proposals/{proposal_id}/reject")
def proposal_reject(proposal_id: int, user=Depends(require_admin)):
    reject(proposal_id)
    return RedirectResponse("/proposals", status_code=303)


# --- sources & discovery ---

@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, user=Depends(require_user)):
    with get_db() as db:
        sources = [dict(r) for r in db.execute("SELECT * FROM sources ORDER BY name").fetchall()]
        log = [dict(r) for r in db.execute(
            "SELECT l.*, s.name AS source_name FROM scan_log l "
            "LEFT JOIN sources s ON s.id = l.source_id ORDER BY l.started_at DESC LIMIT 20").fetchall()]
    return _render(request, "sources.html", sources=sources, scan_log=log)


@router.post("/sources/new")
def source_create(name: str = Form(...), url: str = Form(""), hints: str = Form(""),
                  user=Depends(require_admin)):
    with get_db() as db:
        db.execute("INSERT INTO sources (name, url, hints) VALUES (?, ?, ?)", (name, url or None, hints or None))
    return RedirectResponse("/sources", status_code=303)


@router.get("/sources/{source_id}/edit", response_class=HTMLResponse)
def source_edit(request: Request, source_id: int, user=Depends(require_admin)):
    with get_db() as db:
        row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if not row:
        return RedirectResponse("/sources", status_code=303)
    return _render(request, "source_form.html", source=dict(row))


@router.post("/sources/{source_id}/edit")
def source_update(source_id: int, name: str = Form(...), url: str = Form(""),
                  hints: str = Form(""), user=Depends(require_admin)):
    with get_db() as db:
        db.execute("UPDATE sources SET name=?, url=?, hints=? WHERE id=?",
                   (name, url or None, hints or None, source_id))
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/{source_id}/toggle")
def source_toggle(source_id: int, user=Depends(require_admin)):
    with get_db() as db:
        db.execute("UPDATE sources SET enabled = 1 - enabled WHERE id=?", (source_id,))
    return RedirectResponse("/sources", status_code=303)


@router.post("/sources/{source_id}/delete")
def source_delete(source_id: int, user=Depends(require_admin)):
    with get_db() as db:
        db.execute("DELETE FROM sources WHERE id=?", (source_id,))
    return RedirectResponse("/sources", status_code=303)


@router.post("/scan-now")
def scan_now(source_id: int | None = Form(None), user=Depends(require_admin)):
    threading.Thread(target=run_scan, args=(source_id,), daemon=True).start()
    return RedirectResponse("/sources", status_code=303)


@router.post("/check-links-now")
def check_links_now(user=Depends(require_admin)):
    threading.Thread(target=run_link_monitor, daemon=True).start()
    return RedirectResponse("/sources", status_code=303)


# --- admin ---

@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user=Depends(require_admin)):
    with get_db() as db:
        users = [dict(r) for r in db.execute("SELECT id, username, email, role, active FROM users ORDER BY username")]
        keys = [dict(r) for r in db.execute("SELECT * FROM api_keys ORDER BY created_at DESC")]
    return _render(request, "admin.html", users=users, api_keys=keys, new_key=request.query_params.get("new_key"))


@router.post("/admin/users/new")
def user_create(username: str = Form(...), email: str = Form(...), password: str = Form(...),
                role: str = Form("reader"), user=Depends(require_admin)):
    with get_db() as db:
        db.execute("INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, ?)",
                   (username, email, hash_password(password), role if role in ("reader", "admin") else "reader"))
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/users/{user_id}/toggle")
def user_toggle(user_id: int, user=Depends(require_admin)):
    with get_db() as db:
        db.execute("UPDATE users SET active = 1 - active WHERE id=?", (user_id,))
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/keys/new")
def key_create(label: str = Form(...), user=Depends(require_admin)):
    key = new_api_key()
    with get_db() as db:
        db.execute("INSERT INTO api_keys (label, key) VALUES (?, ?)", (label, key))
    # Mostrata una volta sola, in query string sulla pagina admin.
    return RedirectResponse(f"/admin?new_key={key}", status_code=303)


@router.post("/admin/keys/{key_id}/revoke")
def key_revoke(key_id: int, user=Depends(require_admin)):
    with get_db() as db:
        db.execute("UPDATE api_keys SET active=0 WHERE id=?", (key_id,))
    return RedirectResponse("/admin", status_code=303)
