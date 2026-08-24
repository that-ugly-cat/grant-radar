"""Auth: pbkdf2 password hashing, JWT session cookie, API keys.

Two modes, chosen at runtime, and `local` is the default on purpose:

    AUTH_MODE=local     (default)   this app authenticates on its own
    AUTH_MODE=gateway               an upstream SSO gate vouches via X-Borant-*

The gateway path is dead code until someone turns it on deliberately: an app
that believes an identity header with no gate in front of it lets anyone be
anyone. What does NOT change in either mode is the machine surface — /mcp,
/api and /ono keep their own revocable API keys, because a model client has no
browser and no cookie.
"""
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request

from .db import get_db

log = logging.getLogger("grantradar.auth")

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me-dev-secret-change-me")
JWT_TTL_HOURS = int(os.environ.get("GR_JWT_TTL_HOURS", "720"))  # 30 giorni
COOKIE_NAME = "gr_session"

# --- password ---

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# --- gateway mode (Borant ID) ---
#
# In gateway mode identity headers are believed only from the reverse proxy,
# never from the internet. Under Docker that is a bridge gateway and NOT
# 127.0.0.1: read the real value off the running container's log after a real
# request, do not deduce it from the network layout.
AUTH_MODE = os.environ.get("AUTH_MODE", "local").strip().lower()
TRUSTED_PROXY = os.environ.get("BORANT_TRUSTED_PROXY", "127.0.0.1")
BORANT_LOGOUT_URL = os.environ.get("BORANT_LOGOUT_URL", "https://id.borant.eu/logout")

# The gate's X-Borant-Hint is a suggestion, honoured only at profile creation
# and never again. Both of this app's roles are accepted, which is a deliberate
# departure from the house rule that an app must not provision a role that can
# spend — `admin` unlocks POST /scan-now and POST /grants/{id}/check, both of
# which draw on the server's Anthropic key with no per-user ceiling in the path.
#
# The reason it is safe here and was not elsewhere: the rule was written for an
# app with open registration, where the hint carries what the *applicant* asked
# for. Nothing self-serves into this one. Registration on the gate is closed,
# and even a request for access has an administrator choosing the role at
# approval time — so `admin` in this header can only be there because a human
# typed it. What the code still owes is noise: a role that spends must never be
# provisioned quietly.
KNOWN_HINTS = {"reader", "admin"}
SPENDING_ROLES = {"admin"}
DEFAULT_ROLE = "reader"


def _parse_trusted(raw: str) -> list:
    nets = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            log.warning("BORANT_TRUSTED_PROXY: ignoring %r, not an address or CIDR", chunk)
    return nets


TRUSTED_PROXIES = _parse_trusted(TRUSTED_PROXY)


def gateway_mode() -> bool:
    return AUTH_MODE == "gateway"


def _from_trusted_proxy(request: Request) -> bool:
    peer = request.client.host if request.client else None
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


def _role_from_hint(hint: str, email: str, sub: str) -> str:
    """The starting role for a profile the gate just introduced.

    An unrecognised hint is a typo, not a role: it falls back rather than
    creating something the app cannot check. A hint that *is* recognised and
    spends gets said out loud, because that is the whole safeguard.
    """
    hint = (hint or "").strip().lower()
    if hint not in KNOWN_HINTS:
        if hint:
            log.warning("gateway: hint %r not in %s, falling back to %r",
                        hint, sorted(KNOWN_HINTS), DEFAULT_ROLE)
        return DEFAULT_ROLE
    if hint in SPENDING_ROLES:
        log.warning("gateway: provisioning %s (%s) as %r on the gate's hint. "
                    "That role can start a scan and a verify, both of which "
                    "spend on ANTHROPIC_API_KEY with no per-user ceiling. "
                    "Revoke from /admin if this was not deliberate.",
                    email, sub, hint)
    return hint


def _free_username(db, wanted: str) -> str:
    """A username nobody else has. The gate speaks emails; this table speaks
    usernames, and the local column is UNIQUE."""
    base = "".join(c for c in wanted.lower() if c.isalnum() or c in "._-") or "user"
    candidate, n = base, 1
    while db.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone():
        n += 1
        candidate = f"{base}{n}"
    return candidate


def user_from_gateway(request: Request) -> dict | None:
    """The user the gate vouched for, in the same shape a decoded JWT has.

    Lookup is by `borant_sub` and never by email: a typo in the gate's admin
    panel must not hand one person another person's account. An unknown subject
    gets a fresh profile at DEFAULT_ROLE, which can read the table and spend
    nothing. `scripts/map_borant.py` does the linking once, by hand, and prints
    what it did.
    """
    if not gateway_mode():
        return None
    sub = request.headers.get("x-borant-sub")
    if not sub:
        return None
    if not _from_trusted_proxy(request):
        log.warning("X-Borant-Sub from %s, outside BORANT_TRUSTED_PROXY (%s): ignored",
                    request.client.host if request.client else "?", TRUSTED_PROXY)
        return None

    with get_db() as db:
        row = db.execute(
            "SELECT id, username, role, active FROM users WHERE borant_sub = ?",
            (sub,)).fetchone()
        if row is not None:
            if not row["active"]:
                return None
            return {"sub": str(row["id"]), "username": row["username"], "role": row["role"]}

        email = (request.headers.get("x-borant-email", "") or f"{sub}@borant.invalid").strip().lower()
        # Someone with this address already exists here, unlinked. Do NOT adopt
        # the row: linking by email is exactly the thing map_borant.py exists to
        # keep manual, and adopting it silently would hand this subject an
        # account that might be an admin. Fail closed and say what to run.
        taken = db.execute("SELECT username FROM users WHERE lower(email) = ?", (email,)).fetchone()
        if taken is not None:
            log.error("gateway: %s arrives as %s, but %r already holds that address "
                      "and has no borant_sub. Run scripts/map_borant.py --map %s=%s "
                      "instead of letting the gate guess.",
                      email, sub, taken["username"], email, sub)
            return None
        name = request.headers.get("x-borant-name", "").strip()
        role = _role_from_hint(request.headers.get("x-borant-hint", ""), email, sub)
        username = _free_username(db, name or email.split("@")[0])
        # A local password nobody knows, rather than none: AUTH_MODE=local has
        # to stay a working way back, and a row with no usable password is not
        # a way back. The admin can reset it from /admin when it is needed.
        cur = db.execute(
            "INSERT INTO users (username, email, password_hash, role, borant_sub) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, email, hash_password(secrets.token_urlsafe(32)), role, sub))
        uid = cur.lastrowid
    log.info("gateway: new profile %s (%s) as %s", username, email, role)
    return {"sub": str(uid), "username": username, "role": role}


# --- JWT session ---

def make_token(user_id: int, username: str, role: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_TTL_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def get_user_or_none(request: Request) -> dict | None:
    if gateway_mode():
        # The header wins over the local cookie, always, and there is no
        # fallback to it: a leftover gr_session must not outlive a session the
        # gate has revoked.
        return user_from_gateway(request)
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return decode_token(token)


def require_user(request: Request) -> dict:
    user = get_user_or_none(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


# --- API keys (REST /ono + MCP capability URL) ---

def new_api_key() -> str:
    return secrets.token_urlsafe(24)


def check_api_key(key: str) -> bool:
    if not key:
        return False
    with get_db() as db:
        row = db.execute("SELECT id FROM api_keys WHERE key = ? AND active = 1", (key,)).fetchone()
        if not row:
            return False
        db.execute("UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
    return True


def bootstrap_admin() -> None:
    """Create the first admin from env if the users table is empty."""
    username = os.environ.get("ADMIN_USERNAME", "spit")
    password = os.environ.get("ADMIN_PASSWORD")
    email = os.environ.get("ADMIN_EMAIL", f"{username}@localhost")
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if count > 0:
            return
        if not password:
            print("[grant-radar] No users and no ADMIN_PASSWORD set: login impossible until you set it and restart.")
            return
        db.execute(
            "INSERT INTO users (username, email, password_hash, role) VALUES (?, ?, ?, 'admin')",
            (username, email, hash_password(password)),
        )
        print(f"[grant-radar] Admin user '{username}' created.")
