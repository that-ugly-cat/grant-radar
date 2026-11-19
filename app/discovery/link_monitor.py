"""Nightly link monitor: no LLM. Fetches every grant link, updates link_status
and content_hash, files a `flag` proposal when a link dies or its page changes.

A link is not dead because one fetch failed: a server that is down at 03:00 is
back by morning, and on 26 September 2026 ten links on one domain were flagged
together and were all fine the next day. A link is `dead` after
DEAD_AFTER_FAILURES consecutive failed nights, and it is flagged once per
outage — approving the flag does not make it come back the night after."""
import hashlib
import json
import re

import httpx

from ..db import get_db

TIMEOUT = 20.0
DEAD_AFTER_FAILURES = 3
UNREACHABLE = "Link unreachable (HTTP or network error)."
CHANGED = "The call's page has changed since the last check."
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


def _already_flagged_dead(db, grant_id: int, link: str, last_ok_at) -> bool:
    """True when this outage of this link has been flagged already, whatever
    was decided about the flag. An outage starts after the last successful
    fetch; a link that never answered has one outage, as long as the record."""
    row = db.execute(
        "SELECT id FROM proposals WHERE kind='flag' AND grant_id=? AND source_url=? "
        "AND rationale=? AND created_at > COALESCE(?, '')",
        (grant_id, link, UNREACHABLE, last_ok_at),
    ).fetchone()
    return row is not None


def _file_flag(db, grant_id: int, rationale: str, source_url: str) -> bool:
    if _pending_flag_exists(db, grant_id):
        return False
    src = db.execute("SELECT source_id, workspace_id FROM grants WHERE id=?", (grant_id,)).fetchone()
    db.execute(
        "INSERT INTO proposals (kind, grant_id, source_id, workspace_id, payload, rationale, source_url, "
        "confidence, method) VALUES ('flag', ?, ?, ?, ?, ?, ?, 'high', 'link_monitor')",
        (grant_id, src["source_id"] if src else None, src["workspace_id"] if src else None,
         json.dumps({}), rationale, source_url),
    )
    return True


def _fetch(client, link: str) -> str | None:
    """Readable-text hash of the page, or None when the fetch failed."""
    try:
        resp = client.get(link)
    except httpx.HTTPError:
        return None
    if resp.status_code >= 400:
        return None
    return hashlib.sha256(_normalize(resp.text).encode()).hexdigest()


def _record(db, g, new_hash: str | None) -> str:
    """Write one night's outcome for one grant. Returns ok | changed | failing | dead.
    Separated from the fetch so it can be tested without a network."""
    if new_hash is not None:
        status = "changed" if g["content_hash"] and new_hash != g["content_hash"] else "ok"
        db.execute(
            "UPDATE grants SET link_status=?, content_hash=?, link_fail_count=0, "
            "link_last_ok_at=CURRENT_TIMESTAMP, last_checked_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, new_hash, g["id"]),
        )
        if status == "changed":
            _file_flag(db, g["id"], CHANGED, g["link"])
        return status

    fails = (g["link_fail_count"] or 0) + 1
    if fails < DEAD_AFTER_FAILURES:
        # Not dead yet: keep whatever link_status the row had.
        db.execute(
            "UPDATE grants SET link_fail_count=?, last_checked_at=CURRENT_TIMESTAMP WHERE id=?",
            (fails, g["id"]),
        )
        return "failing"
    db.execute(
        "UPDATE grants SET link_status='dead', link_fail_count=?, last_checked_at=CURRENT_TIMESTAMP "
        "WHERE id=?",
        (fails, g["id"]),
    )
    if not _already_flagged_dead(db, g["id"], g["link"], g["link_last_ok_at"]):
        _file_flag(db, g["id"], UNREACHABLE, g["link"])
    return "dead"


def run_link_monitor(workspace_id: int | None = None) -> dict:
    """Check every workspace's links (the nightly job), or one workspace's.
    Returns a small summary dict; safe to call from scheduler or UI."""
    counts = {"ok": 0, "changed": 0, "failing": 0, "dead": 0}
    with get_db() as db:
        # Closed and archived rows are skipped: nobody acts on their page.
        sql = ("SELECT id, link, content_hash, link_fail_count, link_last_ok_at FROM grants "
               "WHERE link IS NOT NULL AND link != '' AND status = 'open'")
        args = []
        if workspace_id:
            sql += " AND workspace_id = ?"
            args.append(workspace_id)
        grants = db.execute(sql, args).fetchall()

    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        for g in grants:
            new_hash = _fetch(client, g["link"])
            with get_db() as db:
                counts[_record(db, g, new_hash)] += 1

    summary = {"checked": len(grants), "dead": counts["dead"], "failing": counts["failing"],
               "changed": counts["changed"]}
    print(f"[link-monitor] {summary}")
    return summary
