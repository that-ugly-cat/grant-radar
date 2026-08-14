"""Auth: pbkdf2 password hashing, JWT session cookie, API keys."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request

from .db import get_db

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
