"""
"Sign in to iFlow" (Phase 10.5): the account and end-to-end encrypted sync, as one guided flow.

    signed out ─▶ sign in ─────────────────────────┐
               └▶ create account ─▶ verify email ──┤
               └▶ forgot password ─▶ reset ─▶ sign in
                                                   ▼
                              choose sync passphrase (first device: recovery key, shown once)
                              or unlock with it (other devices) / use the recovery key
                                                   ▼
                              syncing: status, Sync now, devices, turn off, sign out, delete account

The server is iFlow-hosted by default (core.config.DEFAULT_ACCOUNT_API_URL; IFLOW_API_URL or the
Advanced field point it elsewhere). Records are encrypted on this device before upload, with a key
derived from the passphrase that never leaves it: the server stores ciphertext only. Nothing here
logs emails, passwords, passphrases, codes or recovery keys.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from core.account.client import AccountClient, AccountError, SettingsSessionStore, validate_api_url
from core.config import DEFAULT_ACCOUNT_API_URL
from core.sync.crypto import SyncCryptoError, WrongPassphrase
from core.sync.engine import SyncError
from core.sync.manager import SyncManager
from core.timeutil import utc_now

log = logging.getLogger(__name__)
UI_KEY = "account_ui"
MIN_PASSPHRASE = 10


@dataclass
class AccountUi:
    step: str = "signin"            # signin | signup | verify | forgot | reset | recover
    email: str = ""
    error: str | None = None
    notice: str | None = None
    recovery_key: str | None = None  # shown once, right after sync is set up on the first device
    last_sync: str | None = None     # "2026-09-28 10:15" (this session)
    last_report: str | None = None
    sync_error: str | None = None
    devices: list | None = None      # cached; refreshed after each sync (never fetched while drawing)

    def clear(self) -> None:
        self.error = self.notice = None


def ui(services: dict[str, Any]) -> AccountUi:
    return services.setdefault(UI_KEY, AccountUi())


def server_url(state: Any, services: dict[str, Any]) -> str:
    return (services.get("account_server") or state.config.api_base_url or DEFAULT_ACCOUNT_API_URL).rstrip("/")


def client(state: Any, services: dict[str, Any]) -> AccountClient:
    """One client per server address; the refresh token lives in (sealed) device settings."""
    url = server_url(state, services)
    cached = services.get("account_client")
    if cached is not None and cached.api_url == url:
        return cached
    made = AccountClient(url, store=SettingsSessionStore(state.store), transport=services.get("account_transport"),
                         platform=state.config.platform)
    services["account_client"] = made
    return made


def manager(state: Any, services: dict[str, Any]) -> SyncManager:
    return SyncManager(state.db, state.store, client(state, services))


def _reload(state: Any) -> None:
    state.settings = state.store.load()          # the manager wrote through the store


def _fail(u: AccountUi, exc: Exception) -> None:
    if isinstance(exc, WrongPassphrase):
        u.error = "That isn't this account's sync passphrase."
    elif isinstance(exc, AccountError):
        u.error = exc.message
    elif isinstance(exc, (SyncError, SyncCryptoError, ValueError)):
        u.error = str(exc)
    else:
        log.warning("Account action failed: %s", type(exc).__name__)
        u.error = "Something went wrong. Check your connection and try again."


# --- status -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    signed_in: bool
    email: str | None
    syncing: bool
    server: str


def status(state: Any, services: dict[str, Any]) -> Status:
    c = client(state, services)
    session = c.session
    enabled = bool(state.settings.sync_enabled and state.settings.sync_keys) and session is not None
    return Status(session is not None, session.email if session else None, enabled, c.api_url)


def chip(state: Any, services: dict[str, Any]) -> tuple[str, str]:
    """(label, tooltip) for the shell's sync chip."""
    try:
        st = status(state, services)
    except Exception:
        return "Sync off", "Sign in to iFlow in Settings to sync across your devices"
    if not st.syncing:
        return ("Sync off", "Sign in to iFlow in Settings to sync across your devices") if not st.signed_in else \
               ("Sync not set up", "Finish setting up sync in Settings > iFlow account")
    u = ui(services)
    if u.sync_error:
        return "Sync paused", u.sync_error
    pending = state.pending_sync_count()
    return (f"{pending} to sync" if pending else "Synced",
            "End-to-end encrypted: the server only ever sees ciphertext")


# --- actions: account -------------------------------------------------------------------------------------


def set_server(state: Any, services: dict[str, Any], url: str) -> None:
    u = ui(services)
    u.clear()
    url = (url or "").strip()
    try:
        services["account_server"] = validate_api_url(url) if url else None
    except AccountError as exc:
        u.error = exc.message
        return
    services.pop("account_client", None)
    u.notice = f"Using {server_url(state, services)}"


def sign_in(state: Any, services: dict[str, Any], email: str, password: str) -> bool:
    u = ui(services)
    u.clear()
    u.email = (email or "").strip()
    try:
        client(state, services).login(u.email, password or "")
    except AccountError as exc:
        if exc.code == "email_not_verified":
            u.step = "verify"
            u.notice = "Enter the code we emailed you to finish creating your account."
            return False
        _fail(u, exc)
        return False
    _reload(state)
    u.step = "signin"
    u.notice = "Signed in. Now turn on sync with your passphrase."
    return True


def sign_up(state: Any, services: dict[str, Any], email: str, password: str, confirm: str) -> bool:
    u = ui(services)
    u.clear()
    u.email = (email or "").strip()
    if password != confirm:
        u.error = "The passwords don't match."
        return False
    try:
        client(state, services).signup(u.email, password or "")
    except Exception as exc:
        _fail(u, exc)
        return False
    u.step = "verify"
    u.notice = f"We sent a 6-digit code to {u.email}. It expires in 15 minutes."
    return True


def verify(state: Any, services: dict[str, Any], code: str) -> bool:
    u = ui(services)
    u.clear()
    try:
        client(state, services).verify_email(u.email, (code or "").strip())
    except Exception as exc:
        _fail(u, exc)
        return False
    _reload(state)
    u.step = "signin"
    u.notice = "Your account is ready. Now turn on sync with a passphrase."
    return True


def resend(state: Any, services: dict[str, Any]) -> None:
    u = ui(services)
    u.clear()
    try:
        client(state, services).resend_verification(u.email)
        u.notice = f"Sent a new code to {u.email}."
    except Exception as exc:
        _fail(u, exc)


def forgot(state: Any, services: dict[str, Any], email: str) -> bool:
    u = ui(services)
    u.clear()
    u.email = (email or "").strip()
    try:
        client(state, services).forgot_password(u.email)
    except Exception as exc:
        _fail(u, exc)
        return False
    u.step = "reset"
    u.notice = f"If {u.email} has an account, we sent it a code."
    return True


def reset(state: Any, services: dict[str, Any], code: str, password: str, confirm: str) -> bool:
    u = ui(services)
    u.clear()
    if password != confirm:
        u.error = "The passwords don't match."
        return False
    try:
        client(state, services).reset_password(u.email, (code or "").strip(), password or "")
    except Exception as exc:
        _fail(u, exc)
        return False
    u.step = "signin"
    u.notice = "Password changed. Sign in with the new one."
    return True


def go(services: dict[str, Any], step: str) -> None:
    u = ui(services)
    u.clear()
    u.step = step


# --- actions: sync ------------------------------------------------------------------------------------------


def setup_sync(state: Any, services: dict[str, Any], passphrase: str, confirm: str | None = None) -> bool:
    """First device: creates the key and shows the recovery key once. Other devices: unlocks it."""
    u = ui(services)
    u.clear()
    if confirm is not None and passphrase != confirm:
        u.error = "The passphrases don't match."
        return False
    if len(passphrase or "") < MIN_PASSPHRASE:
        u.error = f"Use at least {MIN_PASSPHRASE} characters. A few unrelated words work well."
        return False
    try:
        recovery = manager(state, services).setup(passphrase)
    except Exception as exc:
        _fail(u, exc)
        return False
    _reload(state)
    u.recovery_key = recovery
    u.notice = ("Sync is on. Save your recovery key now: it's the only way back in if you forget the passphrase."
                if recovery else "Sync is on for this device.")
    sync_now(state, services, quiet=True)
    return True


def recover(state: Any, services: dict[str, Any], recovery_key: str, passphrase: str, confirm: str) -> bool:
    u = ui(services)
    u.clear()
    if passphrase != confirm:
        u.error = "The passphrases don't match."
        return False
    if len(passphrase or "") < MIN_PASSPHRASE:
        u.error = f"Use at least {MIN_PASSPHRASE} characters."
        return False
    try:
        manager(state, services).recover(recovery_key, passphrase)
    except Exception as exc:
        _fail(u, exc)
        return False
    _reload(state)
    u.step = "signin"
    u.notice = "Sync is on, with your new passphrase. Your recovery key still works."
    sync_now(state, services, quiet=True)
    return True


def sync_now(state: Any, services: dict[str, Any], *, quiet: bool = False) -> bool:
    u = ui(services)
    if not quiet:
        u.clear()
    try:
        report = manager(state, services).sync_now()
    except Exception as exc:
        if isinstance(exc, AccountError) and exc.code == "signed_out":
            _reload(state)
        u.sync_error = getattr(exc, "message", None) or str(exc) or "Sync failed"
        if not quiet:
            _fail(u, exc)
        return False
    u.sync_error = None
    u.devices = devices(state, services)
    u.last_sync = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    changes = report.pushed + report.applied
    u.last_report = (f"sent {report.pushed}, received {report.applied}" if changes else "everything up to date") + \
        (f", {report.conflicts} conflict(s) resolved" if report.conflicts else "")
    if not quiet:
        u.notice = f"Synced: {u.last_report}."
    return True


def turn_off_sync(state: Any, services: dict[str, Any]) -> None:
    u = ui(services)
    u.clear()
    manager(state, services).disable()
    _reload(state)
    u.recovery_key = None
    u.notice = "Sync is off on this device. Everything stays here; other devices keep syncing."


def sign_out(state: Any, services: dict[str, Any]) -> None:
    u = ui(services)
    u.clear()
    manager(state, services).disable()
    client(state, services).logout()
    _reload(state)
    services[UI_KEY] = AccountUi(notice="Signed out. Your data stays on this device.")


def delete_account(state: Any, services: dict[str, Any]) -> bool:
    u = ui(services)
    u.clear()
    try:
        client(state, services).delete_account()
    except Exception as exc:
        _fail(u, exc)
        if isinstance(exc, AccountError) and exc.status in (401, 403):
            u.error = "For safety, sign out and back in, then delete the account within 10 minutes."
        return False
    manager(state, services).disable()
    _reload(state)
    services[UI_KEY] = AccountUi(notice="Your iFlow account and its encrypted copies are deleted. "
                                        "Everything on this device is still here.")
    return True


def devices(state: Any, services: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return client(state, services).devices()
    except Exception as exc:
        log.info("Couldn't list devices: %s", type(exc).__name__)
        return []
