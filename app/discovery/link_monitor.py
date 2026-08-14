"""Nightly link monitor: no LLM. Fetches every grant link, updates link_status
and content_hash, files a `flag` proposal when a link dies or its page changes."""
import hashlib
import json
import re

import httpx

from ..db import get_db

TIMEOUT = 20.0
HEADERS = {"User-Agent": "GrantRadar/1.0 (+https://grantradar.borant.eu)"}


def _normalize(html: str) -> str:
    """Strip tags and collapse whitespace so cosmetic changes don't trip the hash."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _pending_flag_exists(db, grant_id: int) -> bool:
    row = db.execute(
        "SELECT id FROM proposals WHERE kind='flag' AND grant_id=? AND status='pending'",
        (grant_id,),
    ).fetchone()
    return row is not None


def _file_flag(db, grant_id: int, rationale: str, source_url: str) -> None:
    if _pending_flag_exists(db, grant_id):
        return
    src = db.execute("SELECT source_id FROM grants WHERE id=?", (grant_id,)).fetchone()
    db.execute(
        "INSERT INTO proposals (kind, grant_id, source_id, payload, rationale, source_url, confidence, method) "
        "VALUES ('flag', ?, ?, ?, ?, ?, 'high', 'link_monitor')",
        (grant_id, src["source_id"] if src else None, json.dumps({}), rationale, source_url),
    )


def run_link_monitor() -> dict:
    """Returns a small summary dict; safe to call from scheduler or UI."""
    checked = dead = changed = 0
    with get_db() as db:
        grants = db.execute(
            "SELECT id, link, content_hash FROM grants "
            "WHERE link IS NOT NULL AND link != '' AND status != 'archived'"
        ).fetchall()

    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        for g in grants:
            checked += 1
            status, new_hash = "ok", None
            try:
                resp = client.get(g["link"])
                if resp.status_code >= 400:
                    status = "dead"
                else:
                    new_hash = hashlib.sha256(_normalize(resp.text).encode()).hexdigest()
                    if g["content_hash"] and new_hash != g["content_hash"]:
                        status = "changed"
            except httpx.HTTPError:
                status = "dead"

            with get_db() as db:
                db.execute(
                    "UPDATE grants SET link_status=?, content_hash=COALESCE(?, content_hash), "
                    "last_checked_at=CURRENT_TIMESTAMP WHERE id=?",
                    (status, new_hash, g["id"]),
                )
                if status == "dead":
                    dead += 1
                    _file_flag(db, g["id"], "Link unreachable (HTTP or network error).", g["link"])
                elif status == "changed":
                    changed += 1
                    _file_flag(db, g["id"], "The call's page has changed since the last check.", g["link"])

    summary = {"checked": checked, "dead": dead, "changed": changed}
    print(f"[link-monitor] {summary}")
    return summary
