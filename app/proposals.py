"""Proposals queue: diff computation and human decision (approve/reject).
Approving is the ONLY path from discovery output to the grants table."""
import json

from .db import get_db, GRANT_FIELDS


def compute_diff(proposal: dict) -> list[dict]:
    """[{field, old, new}] for the UI. For kind=new, old is always None."""
    payload = json.loads(proposal["payload"] or "{}")
    current = {}
    if proposal["grant_id"]:
        with get_db() as db:
            row = db.execute("SELECT * FROM grants WHERE id=?", (proposal["grant_id"],)).fetchone()
            current = dict(row) if row else {}
    diff = []
    for field in GRANT_FIELDS:
        if field in payload:
            old = current.get(field)
            if str(old or "") != str(payload[field] or ""):
                diff.append({"field": field, "old": old, "new": payload[field]})
    return diff


def approve(proposal_id: int) -> bool:
    with get_db() as db:
        p = db.execute("SELECT * FROM proposals WHERE id=? AND status='pending'", (proposal_id,)).fetchone()
        if not p:
            return False
        payload = json.loads(p["payload"] or "{}")
        fields = {k: v for k, v in payload.items() if k in GRANT_FIELDS}

        if p["kind"] == "new":
            if not fields.get("name"):
                return False
            cols = list(fields.keys()) + ["origin", "source_id"]
            vals = list(fields.values()) + ["discovery", p["source_id"]]
            db.execute(
                f"INSERT INTO grants ({', '.join(cols)}) VALUES ({', '.join('?' * len(vals))})",
                vals,
            )
        elif p["kind"] == "update" and p["grant_id"] and fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            db.execute(
                f"UPDATE grants SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                list(fields.values()) + [p["grant_id"]],
            )
            # Se il grant non è ancora mappato a una source e la proposal sì, eredita.
            if p["source_id"]:
                db.execute(
                    "UPDATE grants SET source_id=? WHERE id=? AND source_id IS NULL",
                    (p["source_id"], p["grant_id"]),
                )
        # kind=flag: approving just acknowledges it; nothing to write on grants.

        db.execute(
            "UPDATE proposals SET status='approved', decided_at=CURRENT_TIMESTAMP WHERE id=?",
            (proposal_id,),
        )
    return True


def reject(proposal_id: int) -> bool:
    with get_db() as db:
        cur = db.execute(
            "UPDATE proposals SET status='rejected', decided_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='pending'",
            (proposal_id,),
        )
        return cur.rowcount > 0
