"""REST layer: /ono/grants dump (API key) + minimal JSON CRUD for scripts."""
from fastapi import APIRouter, Header, HTTPException

from ..auth import check_api_key
from ..db import get_db, grants_digest, GRANT_FIELDS

router = APIRouter()


def _require_key(x_api_key: str | None):
    if not check_api_key(x_api_key or ""):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


@router.get("/ono/grants")
def ono_grants(x_api_key: str | None = Header(None)):
    """Compact JSON dump optimized for LLM consumption."""
    _require_key(x_api_key)
    return grants_digest()


@router.get("/api/grants")
def api_grants(x_api_key: str | None = Header(None)):
    _require_key(x_api_key)
    with get_db() as db:
        rows = db.execute("SELECT * FROM grants ORDER BY deadline_date IS NULL, deadline_date").fetchall()
    return [dict(r) for r in rows]


@router.get("/api/grants/{grant_id}")
def api_grant(grant_id: int, x_api_key: str | None = Header(None)):
    _require_key(x_api_key)
    with get_db() as db:
        row = db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)


@router.post("/api/grants")
def api_grant_create(payload: dict, x_api_key: str | None = Header(None)):
    _require_key(x_api_key)
    fields = {k: v for k, v in payload.items() if k in GRANT_FIELDS and v not in (None, "")}
    if not fields.get("name"):
        raise HTTPException(status_code=422, detail="name is required")
    with get_db() as db:
        cur = db.execute(
            f"INSERT INTO grants ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
            list(fields.values()),
        )
        return {"id": cur.lastrowid}


@router.put("/api/grants/{grant_id}")
def api_grant_update(grant_id: int, payload: dict, x_api_key: str | None = Header(None)):
    _require_key(x_api_key)
    fields = {k: v for k, v in payload.items() if k in GRANT_FIELDS + ["status"]}
    if not fields:
        raise HTTPException(status_code=422, detail="No valid fields")
    sets = ", ".join(f"{k}=?" for k in fields)
    with get_db() as db:
        cur = db.execute(
            f"UPDATE grants SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            list(fields.values()) + [grant_id],
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}
