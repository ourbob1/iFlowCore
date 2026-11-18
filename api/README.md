# iFlow Core account & sync API

Phase 10.2 accounts (email + Google), sessions and devices; 10.3 end-to-end encrypted sync
(a zero-knowledge record store). AWS deployment is 10.4. Plan: [../docs/phase10-plan.md](../docs/phase10-plan.md).

## What it stores

The server stores:
- Email and whether it's verified.
- An Argon2id password hash (email accounts only).
- A Google subject ID (Google accounts only).
- A device label and platform, plus the day each device was last seen.
- SHA-256 hashes of refresh tokens.
- Peppered HMACs of one-time email codes.

It stores no names, IP addresses or readable app data. Sync records are ciphertext only; see
`core/sync/crypto.py`. Deleting an
account removes every row.

## Endpoints

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/v1/auth/signup` | `email`, `password` | 202; a code is emailed |
| POST | `/v1/auth/verify/resend` | `email` | 202 |
| POST | `/v1/auth/verify` | `email`, `code`, `device{name,platform}` | tokens |
| POST | `/v1/auth/login` | `email`, `password`, `device` | tokens |
| POST | `/v1/auth/google` | `id_token`, `device` | tokens |
| POST | `/v1/auth/refresh` | `refresh_token` | tokens (rotated) |
| POST | `/v1/auth/password/forgot` | `email` | 202 |
| POST | `/v1/auth/password/reset` | `email`, `code`, `new_password` | 204; all devices signed out |
| POST | `/v1/auth/logout` | (bearer) | 204 |
| GET | `/v1/account` | (bearer) | account |
| POST | `/v1/account/password` | (bearer) `current_password`, `new_password` | 204; other devices signed out |
| GET | `/v1/account/devices` | (bearer) | devices |
| DELETE | `/v1/account/devices/{id}` | (bearer) | 204 |
| POST | `/v1/account/logout-all` | (bearer) | `{revoked}` |
| DELETE | `/v1/account` | (bearer, signed in within 10 min) | 204 |
| GET | `/v1/sync/keys` | (bearer) | `{envelope, key_version}`, or 404 |
| PUT | `/v1/sync/keys` | (bearer) `envelope`, `expected_version` | `{key_version}`; 409 if another device changed it |
| POST | `/v1/sync/push` | (bearer) `items[{collection, record_id, base_version, ciphertext}]` (max 200) | per item: `ok` (new version and seq) or `conflict` (the server's copy) |
| GET | `/v1/sync/pull?after=&limit=` | (bearer) | `{items, cursor, more}` |
| GET | `/v1/sync/status` | (bearer) | counts |
| DELETE | `/v1/sync` | (bearer, signed in within 10 min) | 204; erases synced data and keys |

"tokens" is: `access_token` (Ed25519 JWT, 15 min), `refresh_token` (single-use, 60 days),
`device_id` and `account`.

Errors look like `{"error": code, "message": text}`, with `Retry-After` on 429.

**Security behaviour:**
- **No account enumeration:** sign-up, resend and forgot-password answer the same way whether or not the
  email exists.
- **No account squatting:** an unverified sign-up can't hold an email hostage, whether against a later
  sign-up by the owner or a Google sign-in.
- **Lockout:** 5 wrong passwords lock the account for 30 s, doubling with each further failure up to 1 h.
- **Rate limits** per IP and per email.
- **Stolen-token detection:** reusing a refresh token signs that device out.
- **Immediate revocation:** signing out takes effect on the next request.

## Run locally

Without Docker (SQLite, codes printed to the console):

```bash
cd api
pip install -r requirements.txt
IFLOW_API_ENV=dev uvicorn iflow_api.app:create_app --factory --port 8000
```

With Docker (PostgreSQL):

```bash
cd api
docker compose up --build
```

Interactive API docs are served at `/docs` in dev only.

## Configuration

All settings are `IFLOW_API_*` environment variables; see `iflow_api/settings.py`. In production:

- `ENV=production`
- `DATABASE_URL`: PostgreSQL
- `JWT_PRIVATE_KEY`: generate with `python -m iflow_api.keys`
- `CODE_PEPPER`: 32 random bytes as hex (`openssl rand -hex 32`)
- `MAIL_BACKEND=ses` and `MAIL_FROM`
- `GOOGLE_CLIENT_IDS`
- `TRUST_PROXY=true` behind App Runner

The service **refuses to start** in production with SQLite, the console mailer, or missing keys.
Secrets come from AWS Secrets Manager (10.4), never from the image or the app.

Apply the schema before deploying:

```bash
alembic upgrade head
```

Key rotation:
1. Deploy a new `JWT_PRIVATE_KEY` with a new `JWT_KEY_ID`.
2. Put the old public key in `JWT_PREVIOUS_KEYS` (`kid=PEM`) for at least 15 minutes.

## Tests

From the repository root:

```bash
pytest tests/test_account_api.py tests/test_account_client.py

# the same suite against PostgreSQL:
IFLOW_API_TEST_DATABASE_URL=postgresql+psycopg://user@host/db pytest tests/test_account_api.py
```
