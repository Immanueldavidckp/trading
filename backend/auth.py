"""Fixed-account authentication for the trading dashboard.

Exactly two accounts, no signup, no user database:
  - super admin: immanueldavidckp@gmail.com
  - user:        user

Passwords are stored as PBKDF2-SHA256 hashes (salt:hash), never in plain
text. Sessions are HMAC-signed cookies; the signing secret is persisted in
local_data/auth_secret.key (or taken from AUTH_SECRET in .env) so logins
survive server restarts.
"""

import os
import hmac
import time
import hashlib
import secrets

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_SECRET_FILE = os.path.join(_BACKEND_DIR, "local_data", "auth_secret.key")

SESSION_COOKIE = "trad_session"
SESSION_TTL = 7 * 24 * 3600  # 7 days

# id -> {salt:hash (PBKDF2-SHA256, 200k iters), role}
USERS = {
    "immanueldavidckp@gmail.com": {
        "hash": "0a53d48b2b25a7742277a90eb9d8e7dc:4beba71931f3dc834ef042d6a58ad24ab4e52afc4d28414d19764254a4b91c78",
        "role": "super_admin",
    },
    "user": {
        "hash": "82d7149fc0c56e5938fc618e98963d58:672fadefd572cb96f5ea3eff775e8b1bd67e330d5e89e66529d9ab265d29a33c",
        "role": "user",
    },
}


def _secret() -> bytes:
    env = os.getenv("AUTH_SECRET")
    if env:
        return env.encode()
    try:
        with open(_SECRET_FILE, "r") as f:
            return f.read().strip().encode()
    except FileNotFoundError:
        os.makedirs(os.path.dirname(_SECRET_FILE), exist_ok=True)
        key = secrets.token_hex(32)
        with open(_SECRET_FILE, "w") as f:
            f.write(key)
        return key.encode()


def verify_password(user_id: str, password: str) -> bool:
    rec = USERS.get(user_id.strip())
    if not rec:
        # burn the same time as a real check so ids can't be probed
        hashlib.pbkdf2_hmac("sha256", password.encode(), b"x" * 16, 200_000)
        return False
    salt, stored = rec["hash"].split(":")
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return hmac.compare_digest(dk.hex(), stored)


def make_token(user_id: str) -> str:
    expires = int(time.time()) + SESSION_TTL
    payload = f"{user_id}|{expires}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def check_token(token: str):
    """Return {'id', 'role'} if the token is valid and unexpired, else None."""
    if not token:
        return None
    try:
        user_id, expires, sig = token.rsplit("|", 2)
    except ValueError:
        return None
    payload = f"{user_id}|{expires}"
    expect = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    if int(expires) < time.time():
        return None
    rec = USERS.get(user_id)
    if not rec:
        return None
    return {"id": user_id, "role": rec["role"]}
