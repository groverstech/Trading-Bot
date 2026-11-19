"""Security primitives: secret encryption, operator authentication, sessions.

* Exchange API keys are encrypted at rest with Fernet (AES-128-CBC + HMAC).
  The master key comes from ``GROK_MASTER_KEY`` or, failing that, a 0600 key
  file generated in the data directory on first run. Keeping the key in the
  environment / a secrets manager is recommended for production.
* Secrets are never returned by the API, only a short hint (last 4 chars).
* Operator password is hashed with scrypt; sessions are random tokens of
  which only the SHA-256 hash is stored.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .db import Database, dumps, loads
from .util import now


class Vault:
    def __init__(self, db: Database, data_dir: Path, master_key: str | None):
        self.db = db
        if master_key:
            key = master_key.encode()
        else:
            path = data_dir / "master.key"
            if not path.exists():
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            key = path.read_bytes().strip()
        self._f = Fernet(key)
        # HMAC key for trade approval tokens, derived from the master key.
        self.signing_key = hashlib.sha256(b"grok-approval:" + key).digest()

    def encrypt(self, s: str) -> bytes:
        return self._f.encrypt(s.encode())

    def decrypt(self, b: bytes) -> str:
        try:
            return self._f.decrypt(b).decode()
        except InvalidToken as e:  # wrong master key
            raise RuntimeError("cannot decrypt stored credential (master key changed?)") from e

    # -- credentials ---------------------------------------------------------
    @staticmethod
    def cred_id(venue: str, env: str) -> str:
        assert venue in ("spot", "futures") and env in ("testnet", "live")
        return f"{venue}:{env}"

    def store_credentials(self, venue: str, env: str, api_key: str, api_secret: str) -> dict:
        api_key, api_secret = api_key.strip(), api_secret.strip()
        if not (16 <= len(api_key) <= 256 and 16 <= len(api_secret) <= 256):
            raise ValueError("API key/secret length looks invalid")
        if not all(c.isalnum() for c in api_key + api_secret):
            raise ValueError("API key/secret must be alphanumeric (HMAC keys)")
        other = self.get_credentials(venue, "live" if env == "testnet" else "testnet")
        if other and other[0] == api_key:
            raise ValueError(f"this API key is already stored as a {'live' if env == 'testnet' else 'testnet'} key; "
                             "testnet and production keys must be different")
        cid = self.cred_id(venue, env)
        self.db.execute(
            "INSERT INTO credentials(id,venue,env,api_key_enc,api_secret_enc,key_hint,created_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET api_key_enc=excluded.api_key_enc, "
            "api_secret_enc=excluded.api_secret_enc, key_hint=excluded.key_hint, "
            "created_at=excluded.created_at, verified_at=NULL, verification=NULL",
            (cid, venue, env, self.encrypt(api_key), self.encrypt(api_secret), "…" + api_key[-4:], now()),
        )
        return {"id": cid, "key_hint": "…" + api_key[-4:]}

    def get_credentials(self, venue: str, env: str) -> tuple[str, str] | None:
        row = self.db.one("SELECT * FROM credentials WHERE id=?", (self.cred_id(venue, env),))
        if not row:
            return None
        return self.decrypt(row["api_key_enc"]), self.decrypt(row["api_secret_enc"])

    def fingerprint(self, venue: str, env: str) -> str | None:
        creds = self.get_credentials(venue, env)
        if not creds:
            return None
        return hashlib.sha256(("|".join(creds)).encode()).hexdigest()[:16]

    def delete_credentials(self, venue: str, env: str) -> None:
        self.db.execute("DELETE FROM credentials WHERE id=?", (self.cred_id(venue, env),))

    def set_verification(self, venue: str, env: str, result: dict) -> None:
        self.db.execute(
            "UPDATE credentials SET verified_at=?, verification=? WHERE id=?",
            (now() if result.get("ok") else None, dumps(result), self.cred_id(venue, env)),
        )

    def list_credentials(self) -> list[dict]:
        rows = self.db.all("SELECT id,venue,env,key_hint,created_at,verified_at,verification FROM credentials")
        for r in rows:
            r["verification"] = loads(r["verification"])
        return rows

    # -- approval tokens -------------------------------------------------------
    def sign(self, payload: str) -> str:
        return hmac.new(self.signing_key, payload.encode(), hashlib.sha256).hexdigest()

    def verify(self, payload: str, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(self.sign(payload), token)


# -- operator authentication ---------------------------------------------------

SCRYPT_N = 2**17  # OWASP-recommended minimum for scrypt (r=8, p=1): ~128 MiB


def hash_password(pw: str, salt: bytes | None = None, n: int = SCRYPT_N) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=8, p=1, dklen=32, maxmem=256 * 1024 * 1024)
    return f"scrypt${n}$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def check_password(pw: str, stored: str) -> bool:
    parts = stored.split("$")
    try:
        if len(parts) == 4:          # scrypt$N$salt$hash
            n, salt = int(parts[1]), base64.b64decode(parts[2])
            return hmac.compare_digest(hash_password(pw, salt, n), stored)
        if len(parts) == 3:          # legacy scrypt$salt$hash (N=2^14)
            salt = base64.b64decode(parts[1])
            h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
            return hmac.compare_digest(base64.b64encode(h).decode(), parts[2])
    except ValueError:
        return False
    return False


def needs_rehash(stored: str) -> bool:
    parts = stored.split("$")
    return len(parts) != 4 or int(parts[1]) < SCRYPT_N


class Auth:
    MIN_PASSWORD = 10

    def __init__(self, db: Database, session_hours: int, env_password: str | None, idle_minutes: int = 120):
        self.db = db
        self.session_seconds = session_hours * 3600
        self.idle_seconds = idle_minutes * 60
        if env_password and not self.is_configured():
            self.set_password(env_password)

    def is_configured(self) -> bool:
        return self.db.meta_get("admin_password") is not None

    def set_password(self, pw: str) -> None:
        if len(pw) < self.MIN_PASSWORD:
            raise ValueError(f"password must be at least {self.MIN_PASSWORD} characters")
        self.db.meta_set("admin_password", hash_password(pw))
        self.db.execute("DELETE FROM sessions")  # invalidate all sessions

    def verify_password(self, pw: str) -> bool:
        stored = self.db.meta_get("admin_password")
        ok = bool(stored) and check_password(pw, stored)
        if ok and needs_rehash(stored):  # transparent upgrade of legacy hashes
            self.db.meta_set("admin_password", hash_password(pw))
        return ok

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        t = now()
        self.db.execute(
            "INSERT INTO sessions(token_hash,created_at,expires_at,last_seen) VALUES(?,?,?,?)",
            (self._h(token), t, t + self.session_seconds, t),
        )
        return token

    def validate(self, token: str | None) -> bool:
        if not token:
            return False
        row = self.db.one("SELECT * FROM sessions WHERE token_hash=?", (self._h(token),))
        if not row or row["expires_at"] < now() or now() - row["last_seen"] > self.idle_seconds:
            if row:
                self.db.execute("DELETE FROM sessions WHERE token_hash=?", (self._h(token),))
            return False
        self.db.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now(), self._h(token)))
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (self._h(token),))

    def list_sessions(self) -> list[dict]:
        return self.db.all(
            "SELECT substr(token_hash,1,8) AS id, created_at, expires_at, last_seen FROM sessions "
            "WHERE expires_at > ? ORDER BY last_seen DESC", (now(),))

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()


class RateLimiter:
    """Sliding-window limiter keyed by client (used for login and mutations)."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        t = time.monotonic()
        q = self.hits[key]
        while q and t - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(t)
        return True
