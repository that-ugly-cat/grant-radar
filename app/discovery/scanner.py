"""Monthly semantic scan: one Claude call per enabled source, with server-side
web search. The model compares what it finds against the current grants digest
and files `new` / `update` proposals. Nothing touches the grants table directly:
everything lands in the proposals queue for human review."""
import json
import os
import threading
from datetime import date

import anthropic

from ..db import DEFAULT_PROFILE, clean_proposal_fields, get_db, grants_digest, GRANT_FIELDS
from ..workspaces import payer_key, workspace_by_id

MODEL = os.environ.get("GR_MODEL", "claude-opus-5")
MAX_TURNS = 8
MAX_WEB_SEARCHES = 12

# Structured proposals arrive via a strict client tool: web search + strict tool
# use are compatible, and validation happens at the tool-call layer.
# Strict schemas cap the number of union-typed parameters at 16: twelve
# descriptive fields plus these three and grant_id make sixteen. There is no
# room for one more nullable field without making an existing one required.
_FIELD_PROPS = {f: {"type": ["string", "null"]} for f in GRANT_FIELDS}
# No `enum` on these two: the API refuses an enum on a nullable string
# ("Enum value 'open' does not match declared type"). The vocabulary is held by
# the description, the prompt and clean_proposal_fields, which drops the rest.
_FIELD_PROPS["status"] = {
    "type": ["string", "null"],
    "description": "Exactly 'open' or 'closed', or null. closed = the scheme ended or this was "
                   "its last call; open = a closed row has a new call",
}
_FIELD_PROPS["fit"] = {
    "type": ["string", "null"],
    "description": "Exactly 'high', 'medium' or 'low', or null: how well the call matches the "
                   "research interests in the profile. Not how sure you are of the facts.",
}
_FIELD_PROPS["recurring"] = {
    "type": ["string", "null"],
    "description": "Exactly 'yes' or 'no', or null. yes = further editions are expected; "
                   "no = one-off or final call",
}

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

SYSTEM = """You are the discovery engine of Grant Radar, a funding-opportunity tracker. You work \
for one research group at a time; who they are and what they work on is the RESEARCH PROFILE in \
the user message, and it is the only definition of relevance you have.

Given one funding source and the digest of grants already tracked, use web search to find:
1. NEW open or upcoming calls from this source that fit the research profile and are not in the digest.
2. UPDATES to tracked grants from this source: a new deadline for a recurring call, changed amounts, \
a closed or discontinued scheme.

Rules:
- Only propose what you actually verified on the web; every proposal needs a source_url you visited.
- For kind=update, set grant_id to the digest ID and fill ONLY the fields that changed (null for the rest).
- For kind=new, fill every field you found (null when unknown). deadline_date is ISO YYYY-MM-DD or null; \
deadline is the human-readable form; deadline_logic describes recurrence (e.g. "annual, usually October").
- recurring says whether the scheme has a next edition: "yes" for annual, biannual or rolling schemes, \
"no" for a one-off or final call. Set it on every kind=new, and propose it as an update for any digest \
row where it is null and you could establish it. A row with recurring="no" is closed automatically once \
its deadline has passed, so say "no" only when the funder says so or no further edition is announced.
- status: when a tracked scheme was discontinued, or its last call has closed with no next edition, \
propose status="closed" (and recurring="no") instead of writing it into the deadline text. When a row \
with status="closed" has a new call, propose status="open" together with the new deadline. Otherwise \
leave rows with status closed or archived alone.
- Skip calls that are clearly out of scope, closed with no next edition, or reserved to categories the \
institute cannot apply to.
- Two separate judgements, never merged. confidence is about the FACTS: high when you read the \
dates and conditions on the funder's own page, low when you inferred them or could not confirm them. \
fit is about RELEVANCE to the research profile: "high" when the call's topic and eligibility match \
what the group does, "medium" when it is adjacent or only part of it applies, "low" when it is a \
stretch. A well-verified call that barely concerns the group is confidence=high, fit=low. Set fit on \
every kind=new, say in the rationale what in the profile it matches, and propose it as an update for \
digest rows where it is null.
- Be conservative: a wrong proposal costs review time. Do not propose a new call you would rate \
fit=low unless the source hints ask for that kind of call.
- When done, call submit_proposals exactly once with all proposals (empty array if none)."""


def _user_prompt(source: dict, workspace: dict) -> str:
    # Each source sees its own grants plus the workspace's orphans, never
    # another workspace's rows.
    digest_rows = [g for g in grants_digest(workspace["id"])
                   if g.get("source_id") in (source["id"], None)]
    digest = json.dumps(digest_rows, ensure_ascii=False, default=str)
    return (
        f"Today is {date.today().isoformat()}.\n\n"
        f"RESEARCH PROFILE\n{(workspace.get('profile') or '').strip() or DEFAULT_PROFILE}\n\n"
        f"FUNDING SOURCE\nname: {source['name']}\nurl: {source['url'] or '(none)'}\n"
        f"hints: {source['hints'] or '(none)'}\n\n"
        f"TRACKED GRANTS DIGEST (JSON):\n{digest}"
    )


def _duplicate_pending(db, kind: str, grant_id, name: str | None, workspace_id: int) -> bool:
    if kind == "update" and grant_id:
        return db.execute(
            "SELECT id FROM proposals WHERE kind='update' AND grant_id=? AND status='pending'",
            (grant_id,),
        ).fetchone() is not None
    if kind == "new" and name:
        return db.execute(
            "SELECT id FROM proposals WHERE kind='new' AND status='pending' AND workspace_id=? "
            "AND json_extract(payload, '$.name') = ?",
            (workspace_id, name),
        ).fetchone() is not None
    return False


def _store_proposals(source: dict, proposals: list[dict]) -> int:
    stored = 0
    with get_db() as db:
        for p in proposals:
            fields = clean_proposal_fields(p.get("fields"))
            kind, grant_id = p.get("kind"), p.get("grant_id")
            if kind not in ("new", "update") or (kind == "update" and not grant_id):
                continue
            ws_id = source["workspace_id"]
            # The model only saw this workspace's rows, but an id it invents
            # must not reach somebody else's grant.
            if kind == "update" and not db.execute(
                    "SELECT 1 FROM grants WHERE id=? AND workspace_id=?", (grant_id, ws_id)).fetchone():
                continue
            if _duplicate_pending(db, kind, grant_id, fields.get("name"), ws_id):
                continue
            db.execute(
                "INSERT INTO proposals (kind, grant_id, source_id, workspace_id, payload, rationale, "
                "source_url, confidence, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'llm_scan')",
                (kind, grant_id if kind == "update" else None, source["id"], ws_id,
                 json.dumps(fields, ensure_ascii=False),
                 p.get("rationale", ""), p.get("source_url", ""), p.get("confidence", "medium")),
            )
            stored += 1
    return stored


def scan_source(source: dict, workspace: dict, api_key: str) -> dict:
    """One agentic loop over a single source, on the payer's key. Returns a summary dict."""
    client = anthropic.Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": _user_prompt(source, workspace)}]
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


# One scan at a time. On 19 November 2026 "Scan now" was submitted twice and
# two full runs walked the sources side by side: twice the cost, and the second
# run's proposals were mostly dropped as duplicates of the first.
_scan_lock = threading.Lock()


def scan_running() -> bool:
    return _scan_lock.locked()


def run_scan(workspace_id: int, source_id: int | None = None) -> list[dict]:
    """Scan one source of a workspace, or every enabled one. Called by 'Scan
    now' and, through run_all_scans, by the scheduler. Returns at once, having
    done nothing, if a scan is running anywhere in the app."""
    if not _scan_lock.acquire(blocking=False):
        print("[scan] skipped: another scan is already running")
        return []
    try:
        return _run_scan(workspace_id, source_id)
    finally:
        _scan_lock.release()


def run_all_scans() -> None:
    """The monthly job: every workspace that takes part, one after another."""
    with get_db() as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM workspaces WHERE auto_scan=1 ORDER BY id")]
    for ws_id in ids:
        run_scan(ws_id)


def _run_scan(workspace_id: int, source_id: int | None = None) -> list[dict]:
    workspace = workspace_by_id(workspace_id)
    if not workspace:
        return []
    with get_db() as db:
        if source_id:
            rows = db.execute("SELECT * FROM sources WHERE id=? AND workspace_id=?",
                              (source_id, workspace_id)).fetchall()
        else:
            rows = db.execute("SELECT * FROM sources WHERE enabled=1 AND workspace_id=?",
                              (workspace_id,)).fetchall()
    sources = [dict(r) for r in rows]
    if not sources:
        return []

    api_key = payer_key(workspace)
    if not api_key:
        # Nobody to bill: say so once, in the place the banner reads, and stop.
        result = {"source": sources[0]["name"], "outcome": "error",
                  "detail": "no_api_key: the workspace's payer has no Anthropic key, scan not run"}
        with get_db() as db:
            db.execute("INSERT INTO scan_log (source_id, finished_at, outcome, detail) "
                       "VALUES (?, CURRENT_TIMESTAMP, 'error', ?)",
                       (sources[0]["id"], json.dumps(result, ensure_ascii=False)))
        print(f"[scan] {workspace['slug']}: {result}")
        return [result]

    results = []
    auth_failed = False
    for source in sources:
        if auth_failed:
            break
        with get_db() as db:
            log_id = db.execute(
                "INSERT INTO scan_log (source_id) VALUES (?)", (source["id"],)
            ).lastrowid
        try:
            result = scan_source(source, workspace, api_key)
        except anthropic.AuthenticationError as e:
            # A rejected key fails every source the same way: record it once
            # and stop, instead of logging the same 401 for each of them.
            auth_failed = True
            result = {"source": source["name"], "outcome": "error",
                      "detail": f"API key rejected, scan aborted: {e}"}
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
        print(f"[scan] {workspace['slug']}: {result}")
        results.append(result)
    return results
