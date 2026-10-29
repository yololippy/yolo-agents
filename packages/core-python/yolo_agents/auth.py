"""Local pairing and revocable device credentials; no model dependencies."""

from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from uuid import uuid4


class AuthError(Exception):
    def __init__(self, code, message, status=401):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class DeviceAuth:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.codes = {}
        self.attempts = defaultdict(deque)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL,
                    permissions TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, revoked_at TEXT
                );
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('server_id', ?)", (str(uuid4()),))
            self.server_id = db.execute("SELECT value FROM settings WHERE key='server_id'").fetchone()[0]

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def public(row):
        return {"id": row["id"], "name": row["name"], "platform": row["platform"],
                "permissions": row["permissions"].split(","), "created_at": row["created_at"],
                "revoked_at": row["revoked_at"]}

    def issue_pairing_code(self, platform, ttl=600):
        if platform not in ("ios", "macos"):
            raise ValueError("unsupported platform")
        code = secrets.token_urlsafe(16)
        with self.lock:
            current = time.monotonic()
            self.codes = {key: value for key, value in self.codes.items() if value[1] > current}
            self.codes[digest(code)] = (platform, current + ttl)
        return code

    def pair(self, code, name, platform, peer):
        with self.lock:
            current = time.monotonic()
            attempts = self.attempts[peer]
            while attempts and attempts[0] < current - 60:
                attempts.popleft()
            if len(attempts) >= 5:
                raise AuthError("pairing_rate_limited", "配对尝试过多，请稍后重试。", 429)
            attempts.append(current)
            saved = self.codes.get(digest(code))
            if not saved or saved[1] <= current:
                raise AuthError("invalid_pairing_code", "配对码无效或已过期。", 401)
            if saved[0] != platform:
                raise AuthError("platform_not_allowed", "此配对码不适用于该设备平台。", 403)
            permissions = ["chat:read", "chat:write"]
            if platform == "macos":
                permissions.append("devices:manage")
            token = secrets.token_urlsafe(32)
            device_id = str(uuid4())
            with self.connect() as db:
                db.execute("INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, NULL)",
                           (device_id, name, platform, ",".join(permissions), digest(token), now()))
                row = db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            del self.codes[digest(code)]
            return {"device": self.public(row), "token": token, "server_id": self.server_id, "api_version": 1}

    def authenticate(self, authorization):
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError("unauthorized", "需要设备认证。")
        token = authorization[7:]
        if not token or len(token) > 1024:
            raise AuthError("unauthorized", "设备凭据无效。")
        with self.connect() as db:
            row = db.execute("SELECT * FROM devices WHERE token_hash=?", (digest(token),)).fetchone()
        if row is None or row["revoked_at"]:
            raise AuthError("unauthorized", "设备凭据无效或已撤销。")
        return self.public(row)

    def devices(self, caller):
        self.require(caller, "devices:manage")
        with self.connect() as db:
            return [self.public(row) for row in db.execute("SELECT * FROM devices ORDER BY created_at")]

    def reauthorize(self, caller):
        """Refresh a previously authenticated public device identity by ID."""
        device_id = caller.get("id") if isinstance(caller, dict) else None
        if not isinstance(device_id, str):
            raise AuthError("unauthorized", "设备凭据无效。")
        with self.connect() as db:
            row = db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None or row["revoked_at"]:
            raise AuthError("unauthorized", "设备凭据无效或已撤销。")
        return self.public(row)

    @staticmethod
    def require(caller, permission):
        if permission not in caller["permissions"]:
            raise AuthError("forbidden", "此设备没有相应操作权限。", 403)

    def revoke(self, caller, target):
        if caller["id"] != target:
            self.require(caller, "devices:manage")
        with self.connect() as db:
            row = db.execute("SELECT * FROM devices WHERE id=?", (target,)).fetchone()
            if row is None:
                raise AuthError("not_found", "设备不存在。", 404)
            db.execute("UPDATE devices SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?", (now(), target))
            return self.public(db.execute("SELECT * FROM devices WHERE id=?", (target,)).fetchone())
