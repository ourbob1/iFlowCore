"""
Admin Console accounts: users, roles, sessions (Phase 11).

Roles → permissions
-------------------
    owner      everything, including managing other owners
    admin      users (not owners), licenses, parser publishing, telemetry, health, audit
    developer  Parser Studio (edit + publish), crash reports (triage), health
    support    licenses (issue + check), crash reports (read), health

Passwords are Argon2id hashes. Temporary passwords (bootstrap, create, reset) must be changed at
first sign-in: until then the account can only change its password or sign out. 5 wrong passwords
lock an account for 15 minutes. Sessions: opaque "ias_<id>.<secret>" tokens (the server keeps a
SHA-256 of the secret), 30 minutes idle / 12 hours absolute, revoked on password change or reset.

First owner (run on the server once; the password is read from the terminal or an env var, never
from the command line, and is stored only as a hash):

    python -m iflow_ai.admin_users bootstrap --username jdoe --name "Jane Doe"
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

PERMISSIONS = ("users.manage", "licenses.issue", "licenses.read", "parsers.edit", "parsers.publish",
               "telemetry.read", "telemetry.triage", "health.read", "audit.read")
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(PERMISSIONS),
    "admin": frozenset(PERMISSIONS),
    "developer": frozenset({"parsers.edit", "parsers.publish", "telemetry.read", "telemetry.triage", "health.read"}),
    "support": frozenset({"licenses.issue", "licenses.read", "telemetry.read", "health.read"}),
}
ROLE_LABELS = {"owner": "Owner", "admin": "Administrator", "developer": "Developer", "support": "Support"}
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,30}[a-z0-9]$")
MIN_PASSWORD = 12
MIN_TEMP_PASSWORD = 8
LOCK_AFTER = 5
LOCK_MINUTES = 15
IDLE = timedelta(minutes=30)
ABSOLUTE = timedelta(hours=12)
COMMON = {"password", "welcome", "letmein", "qwerty", "admin", "iflowcore", "changeme"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_users (
    id                    TEXT PRIMARY KEY,
    username              TEXT NOT NULL UNIQUE,
    display_name          TEXT NOT NULL,
    role                  TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'developer', 'support')),
    password_hash         TEXT NOT NULL,
    must_change_password  INTEGER NOT NULL DEFAULT 1,
    active                INTEGER NOT NULL DEFAULT 1,
    failed_logins         INTEGER NOT NULL DEFAULT 0,
    locked_until          TEXT,
    created_at            TEXT NOT NULL,
    created_by            TEXT NOT NULL,
    last_login_at         TEXT,
    password_changed_at   TEXT
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    secret_hash   BLOB NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    revoked_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_admin_sessions_user ON admin_sessions (user_id);
"""


class AccountError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid") -> None:
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


@dataclass(frozen=True)
class Principal:
    actor: str                     # "user:bcompagnoni" | "token-0"
    role: str
    user_id: str | None = None
    session_id: str | None = None
    must_change_password: bool = False

    @property
    def permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS[self.role]

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> datetime | None:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if ts else None


def password_problem(password: str, username: str, *, temporary: bool = False) -> str | None:
    minimum = MIN_TEMP_PASSWORD if temporary else MIN_PASSWORD
    if len(password) < minimum:
        return f"Use at least {minimum} characters"
    if len(password) > 256:
        return "Use at most 256 characters"
    if not temporary:
        lowered = password.lower()
        if username.lower() in lowered or re.sub(r"[^a-z]", "", lowered) in COMMON:
            return "That password is too easy to guess"
        if len(set(password)) < 6:
            return "Use a more varied password"
    return None


def generate_temp_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


class AdminUsers:
    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None,
                 hasher: PasswordHasher | None = None) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ph = hasher or PasswordHasher()
        self._dummy = self._ph.hash(secrets.token_urlsafe(12))
        with self._lock:
            self._conn.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;" + SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _now(self) -> datetime:
        return self._clock().replace(microsecond=0)

    # --- accounts -------------------------------------------------------------------------------

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT count(*) FROM admin_users").fetchone()[0]

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM admin_users ORDER BY username").fetchall()
        return [self._public(r) for r in rows]

    def get(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        return self._public(row) if row else None

    def _public(self, r: sqlite3.Row) -> dict[str, Any]:
        locked = _parse(r["locked_until"])
        return {"id": r["id"], "username": r["username"], "display_name": r["display_name"], "role": r["role"],
                "role_label": ROLE_LABELS[r["role"]], "active": bool(r["active"]),
                "must_change_password": bool(r["must_change_password"]),
                "locked": bool(locked and locked > self._now()), "created_at": r["created_at"],
                "created_by": r["created_by"], "last_login_at": r["last_login_at"],
                "permissions": sorted(ROLE_PERMISSIONS[r["role"]])}

    def create(self, username: str, display_name: str, role: str, *, created_by: str,
               temp_password: str | None = None, must_change: bool = True) -> tuple[dict[str, Any], str]:
        """must_change=False (first-owner setup on the operator's own computer): the password is the
        owner's real one, so it gets the full strength rules instead of the temporary-password ones."""
        username = (username or "").strip().lower()
        if not USERNAME.match(username):
            raise AccountError("Username: 3–32 lowercase letters, digits, dots, dashes or underscores")
        if role not in ROLE_PERMISSIONS:
            raise AccountError("Unknown role")
        display_name = " ".join((display_name or username).split())[:80]
        temp = temp_password or generate_temp_password()
        problem = password_problem(temp, username, temporary=must_change)
        if problem:
            raise AccountError(f"Temporary password: {problem}" if must_change else problem)
        now = _iso(self._now())
        uid = str(uuid.uuid4())
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO admin_users (id, username, display_name, role, password_hash, must_change_password, "
                    "created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (uid, username, display_name, role, self._ph.hash(temp), int(must_change), now, created_by))
            except sqlite3.IntegrityError:
                raise AccountError("That username is taken", 409, "conflict") from None
        return self.get(uid), temp                                   # type: ignore[return-value]

    def update(self, actor: Principal, user_id: str, *, display_name: str | None = None, role: str | None = None,
               active: bool | None = None) -> dict[str, Any]:
        target = self.get(user_id)
        if target is None:
            raise AccountError("No such user", 404, "not_found")
        self._check_can_manage(actor, target, new_role=role)
        if user_id == actor.user_id and (role not in (None, target["role"]) or active is False):
            raise AccountError("You can't change your own role or disable yourself", 403, "forbidden")
        demoting_owner = target["role"] == "owner" and ((role and role != "owner") or active is False)
        if demoting_owner and self._active_owners() <= 1:
            raise AccountError("There must always be at least one active owner", 409, "last_owner")
        sets, args = [], []
        if display_name is not None:
            sets.append("display_name = ?")
            args.append(" ".join(display_name.split())[:80] or target["display_name"])
        if role is not None:
            if role not in ROLE_PERMISSIONS:
                raise AccountError("Unknown role")
            sets.append("role = ?")
            args.append(role)
        if active is not None:
            sets.append("active = ?")
            args.append(int(active))
        if sets:
            with self._lock, self._conn:
                self._conn.execute(f"UPDATE admin_users SET {', '.join(sets)} WHERE id = ?", (*args, user_id))
                if active is False or (role is not None and role != target["role"]):
                    self._revoke_sessions(user_id)
        return self.get(user_id)                                     # type: ignore[return-value]

    def reset_password(self, actor: Principal, user_id: str) -> str:
        target = self.get(user_id)
        if target is None:
            raise AccountError("No such user", 404, "not_found")
        self._check_can_manage(actor, target)
        temp = generate_temp_password()
        with self._lock, self._conn:
            self._conn.execute("UPDATE admin_users SET password_hash = ?, must_change_password = 1, failed_logins = 0, "
                               "locked_until = NULL WHERE id = ?", (self._ph.hash(temp), user_id))
            self._revoke_sessions(user_id)
        return temp

    def unlock(self, actor: Principal, user_id: str) -> None:
        target = self.get(user_id)
        if target is None:
            raise AccountError("No such user", 404, "not_found")
        self._check_can_manage(actor, target)
        with self._lock, self._conn:
            self._conn.execute("UPDATE admin_users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (user_id,))

    def _check_can_manage(self, actor: Principal, target: dict[str, Any], *, new_role: str | None = None) -> None:
        if not actor.can("users.manage"):
            raise AccountError("You don't have permission to manage users", 403, "forbidden")
        if actor.role != "owner" and ("owner" in (target["role"], new_role)):
            raise AccountError("Only owners can manage owner accounts", 403, "forbidden")

    def _active_owners(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT count(*) FROM admin_users WHERE role = 'owner' AND active = 1").fetchone()[0]

    # --- sign-in & sessions ---------------------------------------------------------------------

    def login(self, username: str, password: str) -> tuple[str, Principal, dict[str, Any]]:
        username = (username or "").strip().lower()
        now = self._now()
        with self._lock:
            row = self._conn.execute("SELECT * FROM admin_users WHERE username = ?", (username,)).fetchone()
        locked = _parse(row["locked_until"]) if row else None
        if row is not None and locked and locked > now:
            self._verify(None, password)
            raise AccountError("Too many attempts. This account is locked for a few minutes.", 429, "locked")
        if row is None or not row["active"] or not self._verify(row["password_hash"], password):
            if row is not None and row["active"]:
                failures = row["failed_logins"] + 1
                lock = _iso(now + timedelta(minutes=LOCK_MINUTES)) if failures >= LOCK_AFTER else None
                with self._lock, self._conn:
                    self._conn.execute("UPDATE admin_users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                                       (0 if lock else failures, lock, row["id"]))
            raise AccountError("Username or password is incorrect", 401, "invalid_credentials")
        if self._ph.check_needs_rehash(row["password_hash"]):
            with self._lock, self._conn:
                self._conn.execute("UPDATE admin_users SET password_hash = ? WHERE id = ?",
                                   (self._ph.hash(password), row["id"]))
        sid, secret = str(uuid.uuid4()), secrets.token_bytes(32)
        with self._lock, self._conn:
            self._conn.execute("UPDATE admin_users SET failed_logins = 0, locked_until = NULL, last_login_at = ? "
                               "WHERE id = ?", (_iso(now), row["id"]))
            self._conn.execute("INSERT INTO admin_sessions (id, user_id, secret_hash, created_at, last_seen_at) "
                               "VALUES (?, ?, ?, ?, ?)", (sid, row["id"], hashlib.sha256(secret).digest(), _iso(now),
                                                          _iso(now)))
        token = f"ias_{sid}.{base64.urlsafe_b64encode(secret).rstrip(b'=').decode()}"
        principal = Principal(f"user:{row['username']}", row["role"], row["id"], sid, bool(row["must_change_password"]))
        return token, principal, self.get(row["id"])                                  # type: ignore[return-value]

    def _verify(self, stored: str | None, password: str) -> bool:
        try:
            return self._ph.verify(stored or self._dummy, password) and stored is not None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def session(self, token: str) -> Principal | None:
        """Resolve a session token; None if unknown, expired, idle too long, revoked or the user is disabled."""
        if not token.startswith("ias_") or "." not in token:
            return None
        sid, _, secret_b64 = token[4:].partition(".")
        try:
            secret = base64.urlsafe_b64decode(secret_b64 + "=" * (-len(secret_b64) % 4))
        except (ValueError, TypeError):
            return None
        now = self._now()
        with self._lock:
            row = self._conn.execute(
                "SELECT s.*, u.username, u.role, u.active, u.must_change_password FROM admin_sessions s "
                "JOIN admin_users u ON u.id = s.user_id WHERE s.id = ?", (sid,)).fetchone()
        if row is None or not hmac.compare_digest(row["secret_hash"], hashlib.sha256(secret).digest()):
            return None
        created, seen = _parse(row["created_at"]), _parse(row["last_seen_at"])
        if row["revoked_at"] or not row["active"] or now - created > ABSOLUTE or now - seen > IDLE:
            return None
        with self._lock, self._conn:
            self._conn.execute("UPDATE admin_sessions SET last_seen_at = ? WHERE id = ?", (_iso(now), sid))
        return Principal(f"user:{row['username']}", row["role"], row["user_id"], sid, bool(row["must_change_password"]))

    def logout(self, principal: Principal) -> None:
        if principal.session_id:
            with self._lock, self._conn:
                self._conn.execute("UPDATE admin_sessions SET revoked_at = ? WHERE id = ?",
                                   (_iso(self._now()), principal.session_id))

    def change_password(self, principal: Principal, current: str, new: str) -> None:
        if principal.user_id is None:
            raise AccountError("Service tokens don't have passwords", 400)
        with self._lock:
            row = self._conn.execute("SELECT * FROM admin_users WHERE id = ?", (principal.user_id,)).fetchone()
        if row is None or not self._verify(row["password_hash"], current):
            # 400, not 401: the session is fine, the form isn't (clients treat 401 as "signed out")
            raise AccountError("Current password is incorrect", 400, "invalid_credentials")
        problem = password_problem(new, row["username"])
        if problem:
            raise AccountError(problem, 400, "weak_password")
        if self._verify(row["password_hash"], new):
            raise AccountError("Choose a password different from the current one", 400, "weak_password")
        with self._lock, self._conn:
            self._conn.execute("UPDATE admin_users SET password_hash = ?, must_change_password = 0, "
                               "password_changed_at = ? WHERE id = ?", (self._ph.hash(new), _iso(self._now()), row["id"]))
            self._conn.execute("UPDATE admin_sessions SET revoked_at = ? WHERE user_id = ? AND id <> ? AND revoked_at IS NULL",
                               (_iso(self._now()), row["id"], principal.session_id or ""))

    def _revoke_sessions(self, user_id: str) -> None:
        self._conn.execute("UPDATE admin_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                           (_iso(self._now()), user_id))


# --- CLI: bootstrap the first owner ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m iflow_ai.admin_users")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bootstrap", help="create an owner account (temporary password, must change at sign-in)")
    b.add_argument("--username", required=True)
    b.add_argument("--name", default="")
    b.add_argument("--role", default="owner", choices=sorted(ROLE_PERMISSIONS))
    b.add_argument("--data-dir", default=os.environ.get("RS_AI_DATA_DIR", "/data"))
    ns = ap.parse_args(argv)
    password = os.environ.get("RS_AI_BOOTSTRAP_PASSWORD") or getpass.getpass("Temporary password: ")
    users = AdminUsers(Path(ns.data_dir) / "admin.db")
    try:
        user, _ = users.create(ns.username, ns.name or ns.username, ns.role, created_by="bootstrap",
                               temp_password=password)
    except AccountError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    finally:
        users.close()
    print(f"created {user['role_label']} '{user['username']}' — they must choose a new password at first sign-in")
    return 0


if __name__ == "__main__":
    sys.exit(main())
