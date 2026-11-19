"""Workspaces: who sees which grants, and whose key pays for the scans.

A workspace owns its grants, its sources and its queue outright. Nothing is
shared between two: the same call tracked by two workspaces is two rows, each
checked and refreshed on its own. That costs duplication and buys a rule with
no exceptions, which is what a permission model is for.

Two kinds. A *personal* workspace belongs to one user, is created the first
time they are seen, and has no other members. A *shared* one is created by a
platform administrator, who names its first admin.

Three roles, per workspace: `read` sees, `write` edits grants and decides the
queue, `admin` manages members, sources and settings. Having no access is the
absence of a row, and everything that looks a workspace up answers the same
way to "does not exist" and to "is not yours".

Scans cost money, and the money is the payer's: a workspace names one of its
admins, and the scan runs on that user's own Anthropic key. No key, no scan.
"""
import base64
import hashlib
import json
import os
import re

from cryptography.fernet import Fernet, InvalidToken

from .db import DEFAULT_PROFILE, get_db

ROLES = ("read", "write", "admin")
_RANK = {r: i for i, r in enumerate(ROLES)}


def role_allows(role: str | None, need: str) -> bool:
    return role in _RANK and _RANK[role] >= _RANK[need]


# --- the Anthropic keys users hand over ---------------------------------------
#
# Stored encrypted, with a secret that lives only in the server's environment:
# a copy of the database alone does not give the keys away. They are never sent
# back to a browser, an MCP client or a REST caller; the page shows the last
# four characters and that is all.

def _fernet() -> Fernet:
    secret = os.environ.get("GR_SECRET_KEY") or os.environ.get("JWT_SECRET")
    if not secret:
        # No fallback to a built-in default: a key encrypted with a string that
        # is in the repository is not encrypted.
        raise RuntimeError("GR_SECRET_KEY (or JWT_SECRET) is not set: API keys cannot be stored")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def set_user_api_key(user_id: int, key: str | None) -> None:
    enc = _fernet().encrypt(key.strip().encode()).decode() if key and key.strip() else None
    with get_db() as db:
        db.execute("UPDATE users SET anthropic_key_enc=? WHERE id=?", (enc, user_id))


def user_api_key(user_id: int | None) -> str | None:
    """The user's key in clear, for the one purpose of calling the API with it.
    None when there is none, or when it no longer decrypts (the secret changed)."""
    if not user_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT anthropic_key_enc FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row["anthropic_key_enc"]:
        return None
    try:
        return _fernet().decrypt(row["anthropic_key_enc"].encode()).decode()
    except (InvalidToken, RuntimeError):
        return None


def key_hint(user_id: int) -> str | None:
    key = user_api_key(user_id)
    return f"…{key[-4:]}" if key else None


# --- lookups ------------------------------------------------------------------

def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "workspace"


def _free_slug(db, wanted: str) -> str:
    base = _slugify(wanted)
    slug, n = base, 1
    while db.execute("SELECT 1 FROM workspaces WHERE slug=?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    return slug


def personal_workspace(user_id: int) -> dict:
    """The user's personal workspace, created on first sight."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM workspaces WHERE kind='personal' AND owner_user_id=?", (user_id,)).fetchone()
        if row:
            return dict(row)
        user = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        name = user["username"] if user else f"user-{user_id}"
        cur = db.execute(
            "INSERT INTO workspaces (slug, name, kind, owner_user_id, payer_user_id, profile) "
            "VALUES (?, ?, 'personal', ?, ?, ?)",
            (_free_slug(db, name), name, user_id, user_id, DEFAULT_PROFILE))
        db.execute("INSERT INTO memberships (user_id, workspace_id, role) VALUES (?, ?, 'admin')",
                   (user_id, cur.lastrowid))
        row = db.execute("SELECT * FROM workspaces WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def user_workspaces(user_id: int) -> list[dict]:
    """Every workspace the user belongs to, personal first, each with `role`."""
    personal_workspace(user_id)
    with get_db() as db:
        rows = db.execute(
            "SELECT w.*, m.role FROM workspaces w JOIN memberships m ON m.workspace_id = w.id "
            "WHERE m.user_id=? ORDER BY w.kind != 'personal', w.name", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def workspace_for(user_id: int, slug: str) -> dict | None:
    """The workspace with this slug, with the user's `role` — or None, both
    when it does not exist and when the user is not a member."""
    if not slug:
        ws = personal_workspace(user_id)
        return {**ws, "role": "admin"}
    with get_db() as db:
        row = db.execute(
            "SELECT w.*, m.role FROM workspaces w JOIN memberships m ON m.workspace_id = w.id "
            "WHERE w.slug=? AND m.user_id=?", (slug, user_id)).fetchone()
    return dict(row) if row else None


def workspace_by_id(workspace_id: int) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
    return dict(row) if row else None


def role_in(user_id: int, workspace_id: int | None) -> str | None:
    if not workspace_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT role FROM memberships WHERE user_id=? AND workspace_id=?",
                         (user_id, workspace_id)).fetchone()
    return row["role"] if row else None


def payer_key(workspace: dict) -> str | None:
    """The Anthropic key this workspace's scans run on, or None."""
    return user_api_key(workspace.get("payer_user_id"))


def members(workspace_id: int) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT u.id, u.username, u.email, u.active, m.role, "
            "u.anthropic_key_enc IS NOT NULL AS has_key "
            "FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.workspace_id=? ORDER BY u.username", (workspace_id,)).fetchall()
    return [dict(r) for r in rows]


# --- changes ------------------------------------------------------------------

def create_shared(name: str, admin_user_id: int, profile: str | None = None) -> dict:
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO workspaces (slug, name, kind, payer_user_id, profile) VALUES (?, ?, 'shared', ?, ?)",
            (_free_slug(db, name), name.strip(), admin_user_id, profile or DEFAULT_PROFILE))
        db.execute("INSERT INTO memberships (user_id, workspace_id, role) VALUES (?, ?, 'admin')",
                   (admin_user_id, cur.lastrowid))
        row = db.execute("SELECT * FROM workspaces WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def set_member(workspace_id: int, user_id: int, role: str) -> None:
    if role not in ROLES:
        raise ValueError(role)
    with get_db() as db:
        db.execute(
            "INSERT INTO memberships (user_id, workspace_id, role) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, workspace_id) DO UPDATE SET role=excluded.role",
            (user_id, workspace_id, role))
    _keep_a_payer(workspace_id)


def remove_member(workspace_id: int, user_id: int) -> None:
    with get_db() as db:
        db.execute("DELETE FROM memberships WHERE user_id=? AND workspace_id=?", (user_id, workspace_id))
    _keep_a_payer(workspace_id)


def admin_count(workspace_id: int) -> int:
    with get_db() as db:
        return db.execute("SELECT COUNT(*) AS n FROM memberships WHERE workspace_id=? AND role='admin'",
                          (workspace_id,)).fetchone()["n"]


def _keep_a_payer(workspace_id: int) -> None:
    """The payer has to be an admin of the workspace. When the one on record
    stops being one, the bill moves to another admin rather than staying with
    somebody who can no longer see what they are paying for."""
    with get_db() as db:
        ws = db.execute("SELECT payer_user_id FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        admins = [r["user_id"] for r in db.execute(
            "SELECT user_id FROM memberships WHERE workspace_id=? AND role='admin' ORDER BY created_at, user_id",
            (workspace_id,))]
        if ws and ws["payer_user_id"] not in admins:
            db.execute("UPDATE workspaces SET payer_user_id=? WHERE id=?",
                       (admins[0] if admins else None, workspace_id))


def copy_grant(grant_id: int, target_workspace_id: int) -> int | None:
    """Copy one grant into another workspace. The copy is a row of its own: it
    carries no source (sources belong to a workspace) and starts unverified."""
    from .db import GRANT_FIELDS
    cols = GRANT_FIELDS + ["status", "recurring", "fit"]
    with get_db() as db:
        row = db.execute(f"SELECT {', '.join(cols)} FROM grants WHERE id=?", (grant_id,)).fetchone()
        if not row:
            return None
        cur = db.execute(
            f"INSERT INTO grants ({', '.join(cols)}, workspace_id, origin) "
            f"VALUES ({', '.join('?' * len(cols))}, ?, 'manual')",
            [row[c] for c in cols] + [target_workspace_id])
        return cur.lastrowid


def copy_workspace_content(src_id: int, dst_id: int) -> dict:
    """Copy every source and every grant of one workspace into another, keeping
    each grant attached to the copy of its source. Proposals are history and
    stay where the decisions were taken."""
    from .db import GRANT_FIELDS
    cols = GRANT_FIELDS + ["status", "recurring", "fit", "origin", "link_status", "content_hash",
                           "last_checked_at", "last_verified_at", "link_fail_count", "link_last_ok_at"]
    with get_db() as db:
        source_map = {}
        for s in db.execute("SELECT * FROM sources WHERE workspace_id=?", (src_id,)).fetchall():
            cur = db.execute(
                "INSERT INTO sources (name, url, hints, enabled, workspace_id) VALUES (?, ?, ?, ?, ?)",
                (s["name"], s["url"], s["hints"], s["enabled"], dst_id))
            source_map[s["id"]] = cur.lastrowid
        n = 0
        for g in db.execute(f"SELECT source_id, {', '.join(cols)} FROM grants WHERE workspace_id=?",
                            (src_id,)).fetchall():
            db.execute(
                f"INSERT INTO grants ({', '.join(cols)}, source_id, workspace_id) "
                f"VALUES ({', '.join('?' * len(cols))}, ?, ?)",
                [g[c] for c in cols] + [source_map.get(g["source_id"]), dst_id])
            n += 1
    return {"sources": len(source_map), "grants": n}


# --- sources as a file --------------------------------------------------------

EXPORT_FORMAT = "grantradar.sources/1"


def export_sources(workspace: dict) -> dict:
    with get_db() as db:
        rows = db.execute("SELECT name, url, hints, enabled FROM sources WHERE workspace_id=? ORDER BY name",
                          (workspace["id"],)).fetchall()
    return {
        "format": EXPORT_FORMAT,
        "workspace": workspace["name"],
        "profile": workspace.get("profile") or "",
        "sources": [{"name": r["name"], "url": r["url"] or "", "hints": r["hints"] or "",
                     "enabled": bool(r["enabled"])} for r in rows],
    }


def import_sources(workspace_id: int, raw: str | bytes) -> dict:
    """Read a file written by export_sources. Idempotent by name: importing the
    same file twice updates the sources it names and adds nothing. The profile
    in the file is never applied here: a workspace's interests are its own."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {"ok": False, "error": "not_json"}
    if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT or not isinstance(data.get("sources"), list):
        return {"ok": False, "error": "wrong_format"}
    added = updated = skipped = 0
    with get_db() as db:
        for s in data["sources"]:
            name = str((s or {}).get("name") or "").strip() if isinstance(s, dict) else ""
            if not name:
                skipped += 1
                continue
            url = str(s.get("url") or "").strip() or None
            hints = str(s.get("hints") or "").strip() or None
            enabled = 1 if s.get("enabled", True) else 0
            row = db.execute("SELECT id FROM sources WHERE workspace_id=? AND name=?",
                             (workspace_id, name)).fetchone()
            if row:
                db.execute("UPDATE sources SET url=?, hints=?, enabled=? WHERE id=?",
                           (url, hints, enabled, row["id"]))
                updated += 1
            else:
                db.execute("INSERT INTO sources (name, url, hints, enabled, workspace_id) VALUES (?, ?, ?, ?, ?)",
                           (name, url, hints, enabled, workspace_id))
                added += 1
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped}


# --- the one-off migration ----------------------------------------------------

def migrate_to_workspaces() -> dict | None:
    """Run once, on the first start after workspaces exist as a table.

    Decided by Giovanni on 19 November 2026: everything that was in the single
    table becomes the first administrator's personal workspace; every other
    user gets an empty personal one; and the whole of it is copied into a
    shared workspace, IBME, where the people who could read the table before
    can read it now. The monthly scan stays on for IBME and is switched off on
    the personal copy, so the same sources are not paid for twice.
    """
    with get_db() as db:
        if db.execute("SELECT 1 FROM workspaces LIMIT 1").fetchone():
            return None
        users = db.execute("SELECT id, role, active FROM users ORDER BY id").fetchall()
        if not users:
            return None
        owner = next((u for u in users if u["role"] == "admin" and u["active"]), users[0])
    owner_id = owner["id"]
    for u in users:
        personal_workspace(u["id"])
    mine = personal_workspace(owner_id)
    with get_db() as db:
        for table in ("grants", "sources", "proposals"):
            db.execute(f"UPDATE {table} SET workspace_id=? WHERE workspace_id IS NULL", (mine["id"],))
        db.execute("UPDATE api_keys SET user_id=? WHERE user_id IS NULL", (owner_id,))
        db.execute("UPDATE workspaces SET auto_scan=0 WHERE id=?", (mine["id"],))
    shared = create_shared("IBME", owner_id)
    copied = copy_workspace_content(mine["id"], shared["id"])
    readers = 0
    for u in users:
        if u["id"] != owner_id and u["active"]:
            set_member(shared["id"], u["id"], "read")
            readers += 1
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    adopted = False
    if env_key and not user_api_key(owner_id):
        # The key the server used so far becomes its owner's own, so the scans
        # do not stop on the day of the migration. It can then leave the .env.
        try:
            set_user_api_key(owner_id, env_key)
            adopted = True
        except RuntimeError as e:   # no secret to encrypt with: start anyway, the key is typed in later
            print(f"[workspaces] the server's Anthropic key was not adopted: {e}")
    summary = {"owner_user_id": owner_id, "personal": mine["slug"], "shared": shared["slug"],
               "readers": readers, "env_key_adopted": adopted, **copied}
    print(f"[workspaces] migrated: {summary}")
    return summary
