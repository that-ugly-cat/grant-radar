"""Monthly semantic scan: one Claude call per enabled source, with server-side
web search. The model compares what it finds against the current grants digest
and files `new` / `update` proposals. Nothing touches the grants table directly:
everything lands in the proposals queue for human review."""
import json
import os
from datetime import date

import anthropic

from ..db import get_db, grants_digest, GRANT_FIELDS

MODEL = os.environ.get("GR_MODEL", "claude-opus-5")
MAX_TURNS = 8
MAX_WEB_SEARCHES = 12

# Structured proposals arrive via a strict client tool: web search + strict tool
# use are compatible, and validation happens at the tool-call layer.
_FIELD_PROPS = {f: {"type": ["string", "null"]} for f in GRANT_FIELDS}

SUBMIT_TOOL = {
    "name": "submit_proposals",
    "description": (
        "Submit the final list of proposals for this funding source. Call this exactly once, "
        "when you have finished searching. Pass an empty array if nothing new or changed was found."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["new", "update"]},
                        "grant_id": {
                            "type": ["integer", "null"],
                            "description": "ID of the existing grant for kind=update; null for kind=new",
                        },
                        "fields": {
                            "type": "object",
                            "properties": _FIELD_PROPS,
                            "required": list(_FIELD_PROPS.keys()),
                            "additionalProperties": False,
                        },
                        "rationale": {"type": "string"},
                        "source_url": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["kind", "grant_id", "fields", "rationale", "source_url", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["proposals"],
        "additionalProperties": False,
    },
}

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_WEB_SEARCHES}

SYSTEM = """You are the discovery engine of Grant Radar, a funding-opportunity tracker for an \
academic institute of biomedical ethics (research areas: bioethics, digital health, AI ethics, \
public health, empirical ethics; based in Zurich, Switzerland).

Given one funding source and the digest of grants already tracked, use web search to find:
1. NEW open or upcoming calls from this source that fit the research areas and are not in the digest.
2. UPDATES to tracked grants from this source: a new deadline for a recurring call, changed amounts, \
a closed or discontinued scheme.

Rules:
- Only propose what you actually verified on the web; every proposal needs a source_url you visited.
- For kind=update, set grant_id to the digest ID and fill ONLY the fields that changed (null for the rest).
- For kind=new, fill every field you found (null when unknown). deadline_date is ISO YYYY-MM-DD or null; \
deadline is the human-readable form; deadline_logic describes recurrence (e.g. "annual, usually October").
- Skip calls that are clearly out of scope, closed with no next edition, or reserved to categories the \
institute cannot apply to.
- Be conservative: a wrong proposal costs review time. Use confidence=low when unsure.
- When done, call submit_proposals exactly once with all proposals (empty array if none)."""


def _user_prompt(source: dict) -> str:
    # Ogni source vede i propri grant più gli orfani non ancora mappati.
    digest_rows = [g for g in grants_digest()
                   if g.get("source_id") in (source["id"], None)]
    digest = json.dumps(digest_rows, ensure_ascii=False, default=str)
    return (
        f"Today is {date.today().isoformat()}.\n\n"
        f"FUNDING SOURCE\nname: {source['name']}\nurl: {source['url'] or '(none)'}\n"
        f"hints: {source['hints'] or '(none)'}\n\n"
        f"TRACKED GRANTS DIGEST (JSON):\n{digest}"
    )


def _duplicate_pending(db, kind: str, grant_id, name: str | None) -> bool:
    if kind == "update" and grant_id:
        return db.execute(
            "SELECT id FROM proposals WHERE kind='update' AND grant_id=? AND status='pending'",
            (grant_id,),
        ).fetchone() is not None
    if kind == "new" and name:
        return db.execute(
            "SELECT id FROM proposals WHERE kind='new' AND status='pending' "
            "AND json_extract(payload, '$.name') = ?",
            (name,),
        ).fetchone() is not None
    return False


def _store_proposals(source: dict, proposals: list[dict]) -> int:
    stored = 0
    with get_db() as db:
        for p in proposals:
            fields = {k: v for k, v in (p.get("fields") or {}).items() if v not in (None, "")}
            kind, grant_id = p.get("kind"), p.get("grant_id")
            if kind not in ("new", "update") or (kind == "update" and not grant_id):
                continue
            if _duplicate_pending(db, kind, grant_id, fields.get("name")):
                continue
            db.execute(
                "INSERT INTO proposals (kind, grant_id, source_id, payload, rationale, source_url, confidence, method) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'llm_scan')",
                (kind, grant_id if kind == "update" else None, source["id"],
                 json.dumps(fields, ensure_ascii=False),
                 p.get("rationale", ""), p.get("source_url", ""), p.get("confidence", "medium")),
            )
            stored += 1
    return stored


def scan_source(source: dict) -> dict:
    """One agentic loop over a single source. Returns a summary dict."""
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": _user_prompt(source)}]
    tools = [WEB_SEARCH_TOOL, SUBMIT_TOOL]
    proposals = None

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "refusal":
            return {"source": source["name"], "outcome": "error", "detail": "refusal"}

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        submit = next((b for b in response.content if b.type == "tool_use" and b.name == "submit_proposals"), None)
        if submit is not None:
            proposals = submit.input.get("proposals", [])
            break

        if response.stop_reason == "tool_use":
            # Only server tools ran; nothing for the client to execute. Continue the turn.
            messages.append({"role": "assistant", "content": response.content})
            continue

        # end_turn without submit_proposals: nudge once, then give up on next loop.
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": "Call submit_proposals now with your findings (empty array if none)."})

    if proposals is None:
        return {"source": source["name"], "outcome": "error", "detail": "no submit_proposals call"}

    stored = _store_proposals(source, proposals)
    return {"source": source["name"], "outcome": "ok", "proposed": len(proposals), "stored": stored}


def run_scan(source_id: int | None = None) -> list[dict]:
    """Scan one source or every enabled source. Called by the scheduler and by 'Scan now'."""
    with get_db() as db:
        if source_id:
            rows = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM sources WHERE enabled=1").fetchall()
    sources = [dict(r) for r in rows]

    results = []
    for source in sources:
        with get_db() as db:
            log_id = db.execute(
                "INSERT INTO scan_log (source_id) VALUES (?)", (source["id"],)
            ).lastrowid
        try:
            result = scan_source(source)
        except anthropic.APIError as e:
            result = {"source": source["name"], "outcome": "error", "detail": f"API error: {e}"}
        except Exception as e:  # non far morire lo scheduler per una source rotta
            result = {"source": source["name"], "outcome": "error", "detail": repr(e)}
        with get_db() as db:
            db.execute(
                "UPDATE scan_log SET finished_at=CURRENT_TIMESTAMP, outcome=?, detail=? WHERE id=?",
                (result["outcome"], json.dumps(result, ensure_ascii=False), log_id),
            )
            if result["outcome"] == "ok":
                db.execute("UPDATE sources SET last_scanned_at=CURRENT_TIMESTAMP WHERE id=?", (source["id"],))
        print(f"[scan] {result}")
        results.append(result)
    return results
