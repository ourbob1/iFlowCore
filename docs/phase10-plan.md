# Phase 10 — Feature Updates: iFlow Core rebrand, accounts, end-to-end encrypted sync

Status: **in progress** · Phase 9 signed off 2026-09-27 · backup `iFlowCore-pre-phase10.tgz`

## Decisions (confirmed)

| Topic | Decision |
|---|---|
| Product name | **iFlow Core** (was Ruby Soho) |
| Sign-in | **Google** and **email + password**. Apple deferred (see App Store note below) |
| Sync target | Database on **AWS**, reached only through our own sync API |
| Privacy | **End-to-end encryption**: AWS stores ciphertext and sync bookkeeping only |
| Local encryption | Unchanged: SQLCipher passphrase on desktop, Keychain/Keystore device key on mobile |

## Milestones

### 10.1 Rebrand to iFlow Core (no AWS needed)
- User-facing name **iFlow Core** everywhere: window title, unlock screen, installers, README, AI service docs.
- Artifacts: `iFlowCore` (PyInstaller/flet artifact), `iflowcore` (.deb package + `/usr/bin/iflowcore`),
  bundle ID `dev.iflowcore.app`, org `dev.iflowcore`. The .deb `Replaces`/`Conflicts` the old `rubysoho` package.
- Environment variables: `IFLOW_*` (e.g. `IFLOW_DB_KEY`, `IFLOW_ENV`). Legacy `RUBYSOHO_*` values are still
  honoured when the new name is unset, so existing setups keep working.
- Data directory: `iFlowCore` (`~/.local/share/iFlowCore`, `%LOCALAPPDATA%\iFlowCore`,
  `~/Library/Application Support/iFlowCore`). On first launch an existing `RubySoho` directory is moved over
  (only if the new one doesn't exist yet); `rubysoho.db` is renamed to `iflowcore.db` together with its
  WAL/SHM files.
- **Frozen identifiers (not renamed):** these are cryptographic contexts and storage names. Changing them would
  lock existing installs out of their own data:
  - `rubysoho/field-sealing/v1` (HKDF info for sealed fields)
  - `rubysoho.device_key.v1` and keychain account `rubysoho` (mobile device key)
  - `-- rubysoho: disable-foreign-keys` (migration marker)

### 10.2 Account service (local dev stack, no AWS needed) — ✅ done (see README Phase 10 verification)
- `api/`: FastAPI **account + sync API**, PostgreSQL via SQLAlchemy/asyncpg, Alembic migrations,
  `docker compose` for local development.
- **Email sign-up**:
  - Argon2id password hashing and an email verification code (sent through SES; logged to the console in
    dev).
  - Password reset, rate limiting and lockout.
- **Google sign-in**:
  - The app gets a Google ID token (OAuth 2.0 with PKCE: system browser on desktop, native flow on
    Android/iOS).
  - The API verifies the signature, `aud` (our client IDs), `iss` and `exp`.
  - An existing account with the same verified email is linked, not duplicated.
- **Sessions**: short-lived access token (Ed25519-signed JWT, 15 min) and a rotating refresh token (stored
  hashed, revocable, one per device). On the device, the refresh token is sealed in the encrypted DB (desktop) or
  in secure storage (mobile).
- Device list and "sign out everywhere"; account deletion (erases all server rows).

### 10.3 End-to-end encryption & sync engine — ✅ done (see README Phase 10 verification)
- **Key hierarchy** (all client-side):
  - **Account Data Key (ADK)**: 256 bits, random, created on the first device.
  - **KEK**: `Argon2id(sync passphrase, per-account salt)`; it wraps the ADK. The server stores only the
    wrapped blob and the salt.
  - **Recovery key**: 256 bits, shown once as words or a QR code; it also wraps the ADK. It's the only way
    back if the passphrase is lost.
  - **Record encryption**: AES-256-GCM with a per-record key derived by HKDF from the ADK.
    AAD = account ‖ collection ‖ record id ‖ version, so the server can't swap or replay records.
  - **Collection names** are HMAC'd, so the server doesn't learn which tables exist.
- **What the server sees**:
  - Account ID, email, opaque record IDs (UUIDs), HMAC'd collection IDs.
  - Server-assigned sequence numbers, ciphertext size, and upload time.
  - Never amounts, locations, trip times, plan details or any health data.
- **Protocol**: push/pull over a server sequence cursor, idempotent uploads, and optimistic concurrency on
  `version`.
  - Conflict policy: last-writer-wins per record, keeping the losing version as a local `conflict` row for
    review.
  - Soft deletes propagate.
- **What syncs**: every table with sync columns.
- **Never synced**:
  - `trip_zones`, device settings, license/device keys, telemetry.
  - Anything marked local-only.
- On desktop the sync passphrase can be the same as the unlock passphrase (one prompt), but they are
  separate keys. On mobile it's asked once when sync is enabled, then the ADK is kept in secure storage.

### 10.4 AWS deployment (needs your AWS account)
- Infrastructure as code: **AWS CDK (Python)** in `infra/`, reviewed before anything is created.
- **Compute**: sync API on **ECS Fargate behind an ALB** (HTTPS, autoscaling). *Changed from App Runner while
  building it: App Runner's VPC egress needs a NAT gateway (~US$32/month) to reach Google and SES; Fargate in
  public subnets that accept only the load balancer needs none. See infra/README.md.*
- **Database**: **RDS PostgreSQL** (`db.t4g.micro` to start, encrypted at rest, private subnets, automated
  backups, deletion protection).
- **Secrets**: DB credentials and the JWT signing key in **Secrets Manager**. Nothing is baked into app binaries.
  Clients only know the API URL and the Google client IDs, which are public.
- **Email**: **SES** for verification and reset email. Needs a domain you control.
- **Operations**: CloudWatch logs (request metadata only, no bodies), budget alarm, WAF rate limits.
- Rough cost at low volume, to check against current AWS pricing: about US$40–80/month. RDS, App Runner and
  a NAT gateway are the main items.

### 10.5 App UI
- Settings → **Account**:
  - Sign up / sign in (Google, email) and verify email.
  - Set the sync passphrase and save the recovery key.
  - Devices, sync now, sync status, sign out, delete account.
- Sync status in the shell (the existing sync chip becomes real).
- Adding a second device: sign in, enter the sync passphrase or recovery key, then pull.

### 10.6 Verification
- Unit and property tests for the crypto:
  - Round-trips; a tampered record, swapped AAD or wrong key is refused.
  - Recovery-key restore.
- API tests: sign-up, verification, login, Google token checks (forged, wrong audience or expired tokens are
  rejected), refresh rotation and reuse detection, isolation between accounts.
- Two-device sync tests against the dockerized API, covering conflicts, deletes and offline edits.
- A privacy test that dumps every server table after a sync and asserts no plaintext field values appear.
- A smoke test after AWS deploy.

## What you need to do (I can't create accounts or enter payment details)

1. **AWS account**:
   - Create it at aws.amazon.com, turn on MFA for the root user, and set a budget alert.
   - Create an admin user in IAM Identity Center for deployments.
   - Sign in on this machine with `aws configure sso`. Never paste access keys into chat; the CLI profile is
     enough for me to run `cdk deploy` after you approve the plan.
   - Pick a region, for example `us-east-1`.
2. **Domain**:
   - One you control for the API (e.g. `api.<yourdomain>`) and the email sender (SES verification).
   - `iflowcore.dev` in the config is only a placeholder.
3. **Google Cloud**:
   - Create a project and an OAuth consent screen.
   - Create OAuth client IDs: Web (for the API audience), Android (package name + SHA-1 of your signing key),
     iOS and Desktop. I'll list exact values when 10.2 is ready.

## App Store note
Apple's guideline 4.8 requires an app that offers a third-party login such as Google to *also* offer an
equivalent privacy-preserving option (in practice, Sign in with Apple). Android and desktop can ship with
Google + email. For the **iOS** release, add Sign in with Apple, or ship iOS with email sign-in only, before
App Store submission.
