"""REST layer: /ono/grants dump + minimal JSON CRUD for scripts.

A key is a person. Every route takes `?workspace=<slug>` (empty: the key
owner's personal workspace) and answers 404 both for a workspace that does not
exist and for one the key's owner is not a member of."""
from fastapi import APIRouter, Header, HTTPException

from ..auth import user_for_api_key
from ..db import clean_proposal_fields, get_db, grants_digest
from ..workspaces import role_allows, role_in, workspace_for

router = APIRouter()


def _caller(x_api_key: str | None) -> int:
    user = user_for_api_key(x_api_key or "")
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return int(user["sub"])


def _workspace(uid: int, slug: str, need: str = "read") -> dict:
    ws = workspace_for(uid, slug)
    if not ws:
        raise HTTPException(status_code=404, detail="workspace_not_found")
    if not role_allows(ws["role"], need):
        raise HTTPException(status_code=403, detail="forbidden_role")
    return ws


def _grant(uid: int, grant_id: int, need: str = "read") -> dict:
    with get_db() as db:
        row = db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
    role = role_in(uid, row["workspace_id"]) if row else None
    if not row or role is None:
        raise HTTPException(status_code=404, detail="grant_not_found")
    if not role_allows(role, need):
        raise HTTPException(status_code=403, detail="forbidden_role")
    return dict(row)


@router.get("/ono/grants")
def ono_grants(workspace: str = "", x_api_key: str | None = Header(None)):
    """Compact JSON dump of one workspace, optimized for LLM consumption."""
    uid = _caller(x_api_key)
    return grants_digest(_workspace(uid, workspace)["id"])


@router.get("/api/grants")
def api_grants(workspace: str = "", x_api_key: str | None = Header(None)):
    uid = _caller(x_api_key)
    ws = _workspace(uid, workspace)
    with get_db() as db:
        rows = db.execute("SELECT * FROM grants WHERE workspace_id=? ORDER BY deadline_date IS NULL, deadline_date",
                          (ws["id"],)).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/grants/{grant_id}")
def api_grant(grant_id: int, x_api_key: str | None = Header(None)):
    return _grant(_caller(x_api_key), grant_id)


@router.post("/api/grants")
def api_grant_create(payload: dict, workspace: str = "", x_api_key: str | None = Header(None)):
    uid = _caller(x_api_key)
    ws = _workspace(uid, workspace, "write")
    fields = clean_proposal_fields(payload)
    if not fields.get("name"):
        raise HTTPException(status_code=422, detail="name_required")
    with get_db() as db:
        cur = db.execute(
            f"INSERT INTO grants ({', '.join(fields)}, workspace_id) VALUES ({', '.join('?' * len(fields))}, ?)",
            list(fields.values()) + [ws["id"]],
        )
        return {"id": cur.lastrowid, "workspace": ws["slug"]}


@router.put("/api/grants/{grant_id}")
def api_grant_update(grant_id: int, payload: dict, x_api_key: str | None = Header(None)):
    grant = _grant(_caller(x_api_key), grant_id, "write")
    fields = clean_proposal_fields(payload)
    if not fields:
        raise HTTPException(status_code=422, detail="no_valid_fields")
    sets = ", ".join(f"{k}=?" for k in fields)
    with get_db() as db:
        db.execute(
            f"UPDATE grants SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            list(fields.values()) + [grant["id"]],
        )
    return {"ok": True}
