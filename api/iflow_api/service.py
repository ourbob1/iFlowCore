"""
Account rules (framework-free; the FastAPI layer only adds HTTP, rate limits and IPs).

Anti-enumeration  sign-up, resend and "forgot password" answer the same way whether or not the
                  email has an account; the owner of an existing account gets a notice instead.
Pre-hijacking     an *unverified* account can't block its real owner: signing up again replaces its
                  password, and a Google sign-in for that email takes it over and clears the password.
Lockout           5 wrong passwords lock the account for 30 s, doubling per further failure (max 1 h).
Codes             6 digits, 15 minutes, 5 attempts, newest only; issuing a code voids older ones.
Refresh reuse     a refresh token is single-use; presenting a used one revokes that device's session.
Revocation        every authenticated request checks the device row, so sign-out takes effect at once.
Deletion          needs a sign-in in the last 10 minutes; deletes the user and everything that hangs off it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import sessionmaker

from . import mailer as mail
from .db import Device, EmailCode, Identity, RefreshToken, User, new_id, utcnow
from .google import GoogleTokenError, GoogleVerifier
from .security import (
    AccessClaims,
    Passwords,
    TokenError,
    code_hash,
    codes_match,
    issue_access_token,
    new_code,
    new_refresh_secret,
    password_problem,
    split_refresh_token,
    verify_access_token,
)
from .settings import Settings

log = logging.getLogger("iflow_api.accounts")

PLATFORMS = frozenset({"linux", "macos", "windows", "android", "ios", "web"})
LOCK_AFTER = 5
LOCK_BASE_S = 30
LOCK_MAX_S = 3600
CODE_ATTEMPTS = 5


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    platform: str

    def validated(self) -> DeviceInfo:
        name = " ".join(self.name.split())[:64]
        if not name or not name.isprintable():
            raise AuthError("invalid_device", "Give this device a name")
        if self.platform not in PLATFORMS:
            raise AuthError("invalid_device", "Unknown platform")
        return DeviceInfo(name, self.platform)


@dataclass(frozen=True)
class SessionTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    device_id: str
    user: dict[str, Any]


def normalize_email(email: str) -> str:
    try:
        return validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise AuthError("invalid_email", "Enter a valid email address") from exc


def _day(ts: datetime) -> datetime:
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


class AccountService:
    def __init__(self, settings: Settings, sessions: sessionmaker, mailer: mail.Mailer, google: GoogleVerifier,
                 clock: Callable[[], datetime] = utcnow, passwords: Passwords | None = None) -> None:
        self.settings, self._sessions, self.mailer, self.google, self.clock = settings, sessions, mailer, google, clock
        self.passwords = passwords or Passwords(settings)

    # --- sign-up & verification -----------------------------------------------------------------

    def signup(self, email: str, password: str) -> None:
        email = normalize_email(email)
        problem = password_problem(password, email)
        if problem:
            raise AuthError("weak_password", problem)
        now = self.clock()
        message: mail.Message | None = None
        with self._sessions.begin() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None and user.email_verified:
                message = mail.existing_account_message(email)
            else:
                if user is None:
                    user = User(id=new_id(), email=email, created_at=now)
                    db.add(user)
                user.password_hash = self.passwords.hash(password)      # unverified: the newest sign-up wins
                user.password_changed_at = now
                db.flush()
                message = self._issue_code(db, user, "verify", now)
        self.mailer.send(message)

    def resend_verification(self, email: str) -> None:
        email = normalize_email(email)
        now = self.clock()
        message = None
        with self._sessions.begin() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None and not user.email_verified:
                message = self._issue_code(db, user, "verify", now)
        if message:
            self.mailer.send(message)

    def verify_email(self, email: str, code: str, device: DeviceInfo) -> SessionTokens:
        email, device = normalize_email(email), device.validated()
        now = self.clock()
        with self._sessions.begin() as db:
            user = db.scalar(select(User).where(User.email == email))
            ok = user is not None and not user.email_verified and self._consume_code(db, user, "verify", code, now)
            if ok:
                user.email_verified = True
                tokens = self._start_session(db, user, device, now)
        if not ok:
            raise AuthError("invalid_code", "That code is wrong or has expired", 400)
        log.info("account verified")
        return tokens

    # --- sign-in ----------------------------------------------------------------------------------

    def login(self, email: str, password: str, device: DeviceInfo) -> SessionTokens:
        email, device = normalize_email(email), device.validated()
        now = self.clock()
        error: AuthError | None = None
        with self._sessions.begin() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None and user.locked_until and user.locked_until > now:
                self.passwords.verify(None, password)                 # keep timing uniform
                wait = (user.locked_until - now).total_seconds()
                error = AuthError("locked", "Too many attempts. Try again later.", 429, retry_after=wait)
            elif not self.passwords.verify(user.password_hash if user else None, password):
                if user is not None:
                    user.failed_logins += 1
                    if user.failed_logins >= LOCK_AFTER:
                        seconds = min(LOCK_MAX_S, LOCK_BASE_S * 2 ** (user.failed_logins - LOCK_AFTER))
                        user.locked_until = now + timedelta(seconds=seconds)
                error = AuthError("invalid_credentials", "Email or password is incorrect", 401)
            else:
                user.failed_logins, user.locked_until = 0, None
                if not user.email_verified:
                    error = AuthError("email_not_verified", "Verify your email first", 403)
                else:
                    if self.passwords.needs_rehash(user.password_hash):
                        user.password_hash = self.passwords.hash(password)
                    tokens = self._start_session(db, user, device, now)
        if error:
            raise error
        return tokens

    def google_login(self, id_token: str, device: DeviceInfo) -> SessionTokens:
        device = device.validated()
        try:
            ident = self.google.verify(id_token)
        except GoogleTokenError as exc:
            raise AuthError("invalid_google_token", str(exc), 401) from exc
        now = self.clock()
        with self._sessions.begin() as db:
            link = db.scalar(select(Identity).where(Identity.provider == "google", Identity.subject == ident.subject))
            if link is not None:
                user = db.get(User, link.user_id)
            else:
                user = db.scalar(select(User).where(User.email == ident.email))
                if user is None:
                    user = User(id=new_id(), email=ident.email, email_verified=True, created_at=now)
                    db.add(user)
                elif not user.email_verified:
                    # Someone registered this email without proving it; Google proves the real owner.
                    user.password_hash, user.email_verified = None, True
                    user.failed_logins, user.locked_until = 0, None
                    self._void_codes(db, user.id, now)
                db.flush()
                db.add(Identity(id=new_id(), user_id=user.id, provider="google", subject=ident.subject,
                                created_at=now))
            tokens = self._start_session(db, user, device, now)
        return tokens

    # --- sessions ---------------------------------------------------------------------------------

    def refresh(self, refresh_token: str) -> SessionTokens:
        try:
            token_id, secret_hash = split_refresh_token(refresh_token)
        except TokenError as exc:
            raise AuthError("invalid_token", "Sign in again", 401) from exc
        now = self.clock()
        error = None
        with self._sessions.begin() as db:
            rt = db.get(RefreshToken, token_id)
            device = db.get(Device, rt.device_id) if rt is not None else None
            if rt is None or device is None or not codes_match(rt.secret_hash, secret_hash):
                error = AuthError("invalid_token", "Sign in again", 401)
            elif rt.used_at is not None:
                device.revoked_at = device.revoked_at or now        # replayed token: assume theft
                log.warning("refresh token reuse; device session revoked")
                error = AuthError("token_reused", "This session was ended for safety. Sign in again.", 401)
            elif device.revoked_at is not None or rt.expires_at <= now:
                error = AuthError("invalid_token", "Sign in again", 401)
            else:
                rt.used_at = now
                device.last_seen_on = _day(now)
                user = db.get(User, device.user_id)
                tokens = self._issue(db, user, device, now)
        if error:
            raise error
        return tokens

    def authenticate(self, access_token: str) -> AccessClaims:
        try:
            claims = verify_access_token(self.settings, access_token, now=self.clock())
        except TokenError as exc:
            raise AuthError("invalid_token", "Sign in again", 401) from exc
        with self._sessions() as db:
            device = db.get(Device, claims.device_id)
            if device is None or device.revoked_at is not None or device.user_id != claims.user_id:
                raise AuthError("invalid_token", "Sign in again", 401)
        return claims

    def logout(self, claims: AccessClaims) -> None:
        with self._sessions.begin() as db:
            device = db.get(Device, claims.device_id)
            if device is not None and device.revoked_at is None:
                device.revoked_at = self.clock()

    def logout_all(self, claims: AccessClaims) -> int:
        now = self.clock()
        with self._sessions.begin() as db:
            result = db.execute(update(Device).where(Device.user_id == claims.user_id, Device.revoked_at.is_(None))
                                .values(revoked_at=now))
            return result.rowcount or 0

    # --- passwords --------------------------------------------------------------------------------

    def forgot_password(self, email: str) -> None:
        email = normalize_email(email)
        now = self.clock()
        message = None
        with self._sessions.begin() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                message = self._issue_code(db, user, "reset", now)
        if message:
            self.mailer.send(message)

    def reset_password(self, email: str, code: str, new_password: str) -> None:
        email = normalize_email(email)
        problem = password_problem(new_password, email)
        if problem:
            raise AuthError("weak_password", problem)
        now = self.clock()
        with self._sessions.begin() as db:
            user = db.scalar(select(User).where(User.email == email))
            ok = user is not None and self._consume_code(db, user, "reset", code, now)
            if ok:
                user.password_hash = self.passwords.hash(new_password)
                user.password_changed_at = now
                user.email_verified = True                        # they just proved they read this inbox
                user.failed_logins, user.locked_until = 0, None
                db.execute(update(Device).where(Device.user_id == user.id, Device.revoked_at.is_(None))
                           .values(revoked_at=now))
        if not ok:
            raise AuthError("invalid_code", "That code is wrong or has expired", 400)

    def change_password(self, claims: AccessClaims, current: str, new_password: str) -> None:
        now = self.clock()
        with self._sessions.begin() as db:
            user = db.get(User, claims.user_id)
            problem = password_problem(new_password, user.email)
            if problem:
                raise AuthError("weak_password", problem)
            if user.password_hash and not self.passwords.verify(user.password_hash, current):
                raise AuthError("invalid_credentials", "Current password is incorrect", 401)
            user.password_hash = self.passwords.hash(new_password)
            user.password_changed_at = now
            db.execute(update(Device).where(Device.user_id == user.id, Device.id != claims.device_id,
                                            Device.revoked_at.is_(None)).values(revoked_at=now))

    # --- account ----------------------------------------------------------------------------------

    def account(self, claims: AccessClaims) -> dict[str, Any]:
        with self._sessions() as db:
            return self._user_dict(db, db.get(User, claims.user_id))

    def devices(self, claims: AccessClaims) -> list[dict[str, Any]]:
        with self._sessions() as db:
            rows = db.scalars(select(Device).where(Device.user_id == claims.user_id, Device.revoked_at.is_(None))
                              .order_by(Device.created_at)).all()
            return [{"id": d.id, "name": d.name, "platform": d.platform, "created_at": d.created_at.isoformat(),
                     "last_seen_on": d.last_seen_on.date().isoformat(), "current": d.id == claims.device_id}
                    for d in rows]

    def revoke_device(self, claims: AccessClaims, device_id: str) -> None:
        with self._sessions.begin() as db:
            device = db.get(Device, device_id)
            if device is None or device.user_id != claims.user_id:
                raise AuthError("not_found", "No such device", 404)
            device.revoked_at = device.revoked_at or self.clock()

    def delete_account(self, claims: AccessClaims) -> None:
        now = self.clock()
        if (now - claims.auth_time).total_seconds() > self.settings.fresh_auth_s:
            raise AuthError("reauth_required", "Sign in again to confirm deleting your account", 403)
        with self._sessions.begin() as db:
            user = db.get(User, claims.user_id)
            if user is not None:
                db.delete(user)
        log.info("account deleted")

    # --- helpers ----------------------------------------------------------------------------------

    def _user_dict(self, db: DBSession, user: User) -> dict[str, Any]:
        providers = sorted({i.provider for i in user.identities} | ({"password"} if user.password_hash else set()))
        return {"id": user.id, "email": user.email, "email_verified": user.email_verified, "providers": providers,
                "created_at": user.created_at.isoformat()}

    def _start_session(self, db: DBSession, user: User, device: DeviceInfo, now: datetime) -> SessionTokens:
        row = Device(id=new_id(), user_id=user.id, name=device.name, platform=device.platform, created_at=now,
                     last_seen_on=_day(now), auth_time=now)
        db.add(row)
        db.flush()
        return self._issue(db, user, row, now)

    def _issue(self, db: DBSession, user: User, device: Device, now: datetime) -> SessionTokens:
        secret, digest = new_refresh_secret()
        rt = RefreshToken(id=new_id(), device_id=device.id, secret_hash=digest, created_at=now,
                          expires_at=now + timedelta(seconds=self.settings.refresh_ttl_s))
        db.add(rt)
        access = issue_access_token(self.settings, user_id=user.id, device_id=device.id, auth_time=device.auth_time,
                                    now=now)
        return SessionTokens(access, f"{rt.id}.{secret}", self.settings.access_ttl_s, self.settings.refresh_ttl_s,
                             device.id, self._user_dict(db, user))

    def _void_codes(self, db: DBSession, user_id: str, now: datetime, purpose: str | None = None) -> None:
        q = update(EmailCode).where(EmailCode.user_id == user_id, EmailCode.consumed_at.is_(None))
        if purpose:
            q = q.where(EmailCode.purpose == purpose)
        db.execute(q.values(consumed_at=now))

    def _issue_code(self, db: DBSession, user: User, purpose: str, now: datetime) -> mail.Message:
        self._void_codes(db, user.id, now, purpose)
        code = new_code()
        db.add(EmailCode(id=new_id(), user_id=user.id, purpose=purpose, created_at=now,
                         code_hash=code_hash(self.settings.code_pepper, user.id, purpose, code),
                         expires_at=now + timedelta(seconds=self.settings.code_ttl_s)))
        ttl = self.settings.code_ttl_s // 60
        return (mail.verification_message if purpose == "verify" else mail.reset_message)(user.email, code, ttl)

    def _consume_code(self, db: DBSession, user: User, purpose: str, code: str, now: datetime) -> bool:
        row = db.scalar(select(EmailCode).where(EmailCode.user_id == user.id, EmailCode.purpose == purpose,
                                                EmailCode.consumed_at.is_(None))
                        .order_by(EmailCode.created_at.desc()).limit(1))
        if row is None or row.expires_at <= now or row.attempts >= CODE_ATTEMPTS:
            return False
        code = "".join(ch for ch in (code or "") if ch.isdigit())[:6]
        if not codes_match(row.code_hash, code_hash(self.settings.code_pepper, user.id, purpose, code)):
            row.attempts += 1
            return False
        row.consumed_at = now
        return True
