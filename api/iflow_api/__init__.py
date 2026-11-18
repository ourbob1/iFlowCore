"""iFlow Core account & sync API (Phase 10).

Accounts: email + password (Argon2id, verified by emailed code) and Google sign-in (ID token verified
against Google's keys). Sessions: short-lived Ed25519 access tokens plus rotating, revocable refresh
tokens per device. The sync endpoints (10.3) store only end-to-end encrypted records.
"""

__version__ = "0.10.0"
