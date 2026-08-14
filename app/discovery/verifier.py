"""Per-grant verification: the LLM fetches the grant's page (web fetch, with web
search as fallback when the page moved or died), compares it against the stored
record, and either stamps the grant as verified or files an `update` proposal
with the discrepancies. Same rules as every discovery path: nothing touches the
grants table directly — differences go through the human approval queue."""
import json
from datetime import date

import anthropic

from ..db import get_db, GRANT_FIELDS
from .scanner import MODEL

MAX_TURNS = 6

_FIELD_PROPS = {f: {"type": ["string", "null"]} for f in GRANT_FIELDS}

SUBMIT_TOOL = {
    "name": "submit_verification",
    "description": (
        "Submit the verification outcome. Call this exactly once, when you have "
        "checked the grant's page. matches=true means every stored field is still "
        "accurate; matches=false means at least one field should change — put ONLY "
        "the fields that should change in `fields` (null for the rest)."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "matches": {"type": "boolean"},
            "fields": {
                "type": "object",
                "properties": _FIELD_PROPS,
                "required": list(_FIELD_PROPS.keys()),
                "additionalProperties": False,
            },
            "rationale": {"type": "string"},
            "source_url": {"type": "string", "description": "The page you actually verified against"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["matches", "fields", "rationale", "source_url", "confidence"],
        "additionalProperties": False,
    },
}

TOOLS = [
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    SUBMIT_TOOL,
]

SYSTEM = """You verify one record of a funding-opportunity tracker against reality.

Fetch the grant's link and check every stored field against the page: deadline (has a new \
call opened? did the deadline pass and a next edition is announced?), amounts, duration, \
eligibility-relevant scope, whether the scheme still exists. If the link is dead or the page \
moved, search the web for the scheme's current official page.

Rules:
- Compare against what you actually read; do not guess. deadline_date is ISO YYYY-MM-DD.
- Report ONLY fields whose stored value is wrong or outdated; leave everything else null.
- A deadline in the past with a known next edition -> propose the new deadline/deadline_date.
- A scheme that was discontinued -> propose notes saying so (the human decides on status).
- If everything checks out, matches=true with empty changes.
- Call submit_verification exactly once when done."""


def _user_prompt(grant: dict) -> str:
    record = {k: grant.get(k) for k in ["id", "name", "funder", "scope", "max_amount",
                                        "duration_months", "deadline", "deadline_date",
                                        "deadline_logic", "link", "notes", "grant_start",
                                        "primary_type", "status"]}
    return (
        f"Today is {date.today().isoformat()}.\n\n"
        f"STORED RECORD (JSON):\n{json.dumps(record, ensure_ascii=False, default=str)}"
    )


def _apply_result(grant: dict, result: dict) -> dict:
    """Stamp the verification; file an update proposal if there are differences.
    Separated from the LLM loop so it can be unit-tested."""
    fields = {k: v for k, v in (result.get("fields") or {}).items()
              if k in GRANT_FIELDS and v not in (None, "")}
    # Tieni solo i campi davvero diversi dal record corrente.
    fields = {k: v for k, v in fields.items() if str(grant.get(k) or "") != str(v or "")}
    matches = bool(result.get("matches")) or not fields

    with get_db() as db:
        db.execute("UPDATE grants SET last_verified_at=CURRENT_TIMESTAMP WHERE id=?", (grant["id"],))
        if matches:
            return {"grant": grant["name"], "outcome": "verified", "changes": 0}
        if db.execute("SELECT id FROM proposals WHERE kind='update' AND grant_id=? AND status='pending'",
                      (grant["id"],)).fetchone():
            return {"grant": grant["name"], "outcome": "pending_exists", "changes": len(fields)}
        db.execute(
            "INSERT INTO proposals (kind, grant_id, source_id, payload, rationale, source_url, confidence, method) "
            "VALUES ('update', ?, ?, ?, ?, ?, ?, 'llm_check')",
            (grant["id"], grant.get("source_id"), json.dumps(fields, ensure_ascii=False),
             result.get("rationale", ""), result.get("source_url", ""),
             result.get("confidence", "medium")),
        )
        return {"grant": grant["name"], "outcome": "diff_proposed", "changes": len(fields)}


def verify_grant(grant_id: int) -> dict:
    with get_db() as db:
        row = db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
    if not row:
        return {"outcome": "error", "detail": f"grant {grant_id} not found"}
    grant = dict(row)

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": _user_prompt(grant)}]
    result = None
    try:
        for _ in range(MAX_TURNS):
            response = client.messages.create(
                model=MODEL, max_tokens=6000, system=SYSTEM, tools=TOOLS, messages=messages,
            )
            if response.stop_reason == "refusal":
                return {"grant": grant["name"], "outcome": "error", "detail": "refusal"}
            submit = next((b for b in response.content
                           if b.type == "tool_use" and b.name == "submit_verification"), None)
            if submit is not None:
                result = submit.input
                break
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason not in ("pause_turn", "tool_use"):
                messages.append({"role": "user",
                                 "content": "Call submit_verification now with your outcome."})
    except anthropic.APIError as e:
        return {"grant": grant["name"], "outcome": "error", "detail": f"API error: {e}"}

    if result is None:
        return {"grant": grant["name"], "outcome": "error", "detail": "no submit_verification call"}

    summary = _apply_result(grant, result)
    print(f"[verify] {summary}")
    return summary
