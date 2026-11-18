# iFlow Core AI service

A small, self-hosted HTTPS API that the iFlow Core app uses for three things:

| Endpoint | What it does | Data sent by the app |
|---|---|---|
| `POST /v1/receipts/extract` | Reads a receipt photo/PDF → vendor, date, subtotal, tax, total, items | The receipt file (decoded in memory, never stored) |
| `POST /v1/tax/ask` | Answers a tax question from the app's own computed figures | The question + yearly aggregates (no transactions, vendors or places) |
| `POST /v1/telemetry/events` | Stores crash reports (opt-in in the app) | Scrubbed error type/message/stack frames |

Everything runs on your server: no third-party AI API, no SaaS crash reporter.

## How it works

```
phone / desktop app ──HTTPS + bearer token──▶ Caddy (TLS, 26 MB body cap) ──▶ ai:8080 (FastAPI, 1 worker)
                                                                               ├─ Tesseract OCR
                                                                               ├─ Qwen2.5-1.5B-Instruct (receipts → JSON, tax Q&A)
                                                                               └─ /data/telemetry.db (SQLite)
```

**Receipts** (`RS_AI_RECEIPT_BACKEND=ocr_llm`, default): Tesseract reads the text (or the PDF's text
layer is used directly), the LLM turns it into JSON, and regex heuristics cross-check it. A total the
model "read" that isn't printed anywhere on the receipt is replaced by the receipt's own TOTAL line and
flagged. Each field comes back with `high`/`medium`/`low` confidence, and the app always shows a review
dialog before saving anything.

`RS_AI_RECEIPT_BACKEND=vlm` swaps in a vision-language model (default `Qwen/Qwen2.5-VL-7B-Instruct`)
that reads the photo directly. It handles crumpled or angled photos better but needs ~20 GB RAM or a GPU.

**Tax Q&A** is deterministic first, because small CPU models are unreliable with numbers:

1. **Topic gate**: questions with no tax/money vocabulary ("what's the capital of France?") and prompt-injection
   attempts are declined before any model runs.
2. **Templates**: the common questions (how much to set aside, per month, next payment, why SE tax is high,
   effective/marginal rate, total owed, "can I deduct X?") are answered from the app's figures by rules:
   exact and instant. All of the app's suggested questions land here.
3. **LLM fallback** for everything else, with the app's figures (each with a one-line meaning) as `FACTS`.
   A **grounding check** then rejects any answer containing a number that isn't in the facts (or a fixed
   rate such as 15.3%); the user gets a plain summary of their real figures instead.

The app labels each answer "Calculated from your numbers" or "AI-generated… double-check", and always
shows the planning-estimate disclaimer. The app's deterministic tax engine stays the source of truth.

**Crash reports**: grouped by fingerprint into issues. Read them with the admin token:

```bash
curl -s -H "Authorization: Bearer $RS_AI_ADMIN_TOKEN" https://ai.example.com/v1/telemetry/issues | jq
```

## Server sizing

CPU-only is fine for one person or a small team. Rough guide for the default models:

| | Minimum | Recommended |
|---|---|---|
| vCPU | 4 | 8 |
| RAM | 8 GB (`RS_AI_DTYPE=bfloat16`, `AI_MEM_LIMIT=6g`) | 16 GB (`float32`, `AI_MEM_LIMIT=10g`) |
| Disk | 20 GB (image ~3 GB, model ~3 GB) | 40 GB |
| OS | Ubuntu 24.04 LTS, x86_64 | |

Measured with the default model (float32, 8 threads on a desktop Ryzen CPU): receipt extraction
≈ 35 s once warm, open tax questions 11–27 s, template answers instant, first model load ≈ 10 s.
A shared VPS vCPU is usually slower than a desktop core, so expect up to 2× on a 4–8 vCPU server.
The app shows progress and uses 180 s / 120 s timeouts.

**Accuracy (smoke test, same model):** receipts: vendor, date, subtotal, tax and total all correct on
clean, café (with tip) and OCR-garbled samples and on a PDF invoice. Tax: templates exact; the model
answered simple open questions correctly, invented numbers were caught by the grounding check, but
an open comparison question ("actual expenses vs mileage?") got a confident, unhelpful answer with no
numbers in it. That is the limit of a 1.5B model. If the server has 32 GB RAM, `Qwen/Qwen2.5-7B-Instruct`
(Apache-2.0) with `RS_AI_DTYPE=bfloat16` gives noticeably better open answers at ~3–4× the latency.

## Deploy on an IONOS Linux server

1. **Server**: create a Linux VPS or Cloud Server with Ubuntu 24.04 in the IONOS Cloud Panel, sized
   as above. Add your SSH key during setup.
2. **Firewall**: in the Cloud Panel's firewall policy for the server, allow inbound TCP 80 and 443
   (and UDP 443 for HTTP/3) from anywhere, and TCP 22 **only from your own IP**. Don't open 8080.
3. **DNS**: create an `A` record (and `AAAA` if the server has IPv6) such as `ai.yourdomain.com`
   pointing at the server's public IP. If the domain is at IONOS, do this under Domains & SSL → DNS.
   Wait until `dig +short ai.yourdomain.com` returns the IP.
4. **Docker**: SSH in and install Docker Engine + the compose plugin from Docker's official apt
   repository (docs.docker.com/engine/install/ubuntu), then `sudo usermod -aG docker $USER` and log
   in again.
5. **Code**: copy this `services/ai` folder to the server, e.g.
   `rsync -a services/ai/ you@server:~/iflow-ai/`.
6. **Config**:
   ```bash
   cd ~/iflow-ai
   cp .env.example .env
   openssl rand -hex 32     # → RS_AI_TOKENS (one per device; comma-separate several)
   openssl rand -hex 32     # → RS_AI_ADMIN_TOKEN
   nano .env                # RS_AI_DOMAIN, ACME_EMAIL, tokens; review model and memory settings
   chmod 600 .env
   ```
7. **Start**:
   ```bash
   docker compose build
   docker compose run --rm ai python scripts/prefetch_models.py   # optional: download models now
   docker compose up -d
   docker compose logs -f ai                                      # wait for "Application startup complete"
   ```
   Caddy gets a Let's Encrypt certificate automatically on the first HTTPS request.
8. **Check**:
   ```bash
   curl https://ai.yourdomain.com/healthz
   curl -H "Authorization: Bearer <client token>" https://ai.yourdomain.com/v1/status
   ```
   `models` should show `ready` for `llm` and `ocr` once warm-up finishes.
9. **Connect the app**: Settings → AI service & diagnostics → server URL + access token → Save →
   Test connection. Then turn on receipt scanning, tax questions, and/or crash reports.

### Updating and operating

- **Update**: copy the new `services/ai`, then `docker compose build && docker compose up -d`.
- **Pin models**: set `RS_AI_LLM_REVISION` to a commit hash from the model page so a rebuild never
  changes behaviour silently.
- **Revoke a device**: remove its token from `RS_AI_TOKENS` and `docker compose up -d`.
- **Backups**: only `/data/telemetry.db` (the `data` volume) holds state; models can be re-downloaded.
- **Unattended upgrades**: `sudo apt install unattended-upgrades` keeps the host patched.

## Configuration

All settings are environment variables in `.env`; see [.env.example](.env.example) for every option
with comments. The important ones:

| Variable | Default | |
|---|---|---|
| `RS_AI_TOKENS` | required | Client bearer tokens, 32+ chars, comma-separated |
| `RS_AI_ADMIN_TOKENS` | — | Admin-portal tokens (one per operator), 32+ chars, comma-separated; must differ from client tokens. Admin routes answer 404 when unset |
| `RS_AI_ADMIN_TOKEN` | — | Older single admin token; still accepted and merged into `RS_AI_ADMIN_TOKENS` |
| `RS_AI_LICENSE_SIGNING_KEY` | — | 64 hex chars: the Ed25519 key that signs licenses. Issuing is off without it |
| `RS_AI_ADMIN_FAILURES_PER_10MIN` | `10` | Failed admin sign-ins allowed per IP before a 10-minute lockout |
| `RS_AI_RECEIPT_BACKEND` | `ocr_llm` | `ocr_llm` / `vlm` / `off` |
| `RS_AI_TAX_BACKEND` | `llm` | `llm` / `off` |
| `RS_AI_LLM_MODEL` | `Qwen/Qwen2.5-1.5B-Instruct` | Any HF chat model; `Qwen/Qwen2.5-7B-Instruct` is more accurate with 32 GB RAM |
| `RS_AI_DTYPE` | `float32` | `bfloat16` halves memory |
| `RS_AI_MAX_CONCURRENCY` | `1` | Parallel inferences |
| `RS_AI_MAX_UPLOAD_MB` | `25` | Matches the app's receipt limit |

Check each model's license on its Hugging Face page before using it commercially. Qwen2.5-1.5B-Instruct
and Qwen2.5-VL-7B-Instruct are Apache-2.0; some other sizes in those families use different licenses.

## Admin Console (Phase 11)

Operators run a separate desktop app, the **Admin Console**: install `iflowcore-admin_<version>_amd64.deb`
(or `python admin_console.py` from the repo root). It never opens an iFlow Core user database.

- **This computer** (default): the console runs this admin API itself on 127.0.0.1, using
  `~/.local/share/iflow-ai`. The first launch creates the owner account; nothing to start by hand.
- **Another server**: it signs in to a deployed service over HTTPS and calls `/admin/v1/*` here.

- **Accounts & roles:** console users sign in with a username and password (Argon2id).
  - A temporary password must be changed at first sign-in; until then only the sign-in routes work.
  - Five wrong passwords lock the account for 15 minutes (an admin can unlock it sooner).
  - Sessions end after 30 minutes idle or 12 hours, and on sign-out, password reset, role change or
    disable.
  - Roles: **Owner** (everything), **Administrator** (everything except managing owners), **Developer**
    (Parser Studio, crash reports, health), **Support** (licenses, crash reports, health). There is
    always at least one active owner.
  - Service tokens (`RS_AI_ADMIN_TOKENS`) still work, as owners, for automation and break-glass access.
- **Users:** add users (the server generates a temporary password, shown once), change roles,
  disable/enable, reset passwords, unlock. A roles-and-permissions matrix is shown too.
- **Parser Studio:** statement formats for new platforms.
  - Map columns to gross, tips, quests, fees, tolls, trip date, payout date and reference, with
    optional regex extractors and basis-point multipliers.
  - Test Parse runs on the operator's machine (samples never reach this server) and previews integer
    cents.
  - Save draft → publish (a new version each time) → a bundle signed with the vendor key.
  - Publishing writes the signed bundle as static files to the CDN (`RS_AI_FORMATS_BUCKET` +
    `RS_AI_FORMATS_DISTRIBUTION_ID`, from the `IflowCoreFormats` stack; or `RS_AI_FORMATS_DIR` for a
    folder). No API route serves bundles to apps.
  - Apps download `https://formats.iflowcore.com/v1/bundle.ifpb` daily, verify the signature against the
    key built into the app, and refuse downgrades.
- **Licenses:**
  - Issue signed iFlow Pro or Health & Guild keys (monthly or annual, 1–36 terms, or complimentary).
  - Check any key.
  - See the ledger and revenue by month, in integer cents.
- **Crash reports:** review scrubbed issues, open each for details, resolve or reopen, and delete reports
  older than 90 days. An issue that happens again after being resolved reopens automatically.
- **Health:** uptime, container, OCR and model state, inference queue, request counts and latency, 429s,
  disk.
- **Audit trail:** every sign-in and admin action, with who, what and when. Never passwords or tokens.

Create the first owner on the server (the password is read from `RS_AI_BOOTSTRAP_PASSWORD`, or asked for;
it's temporary, so it must be changed at first sign-in):

```bash
python -m iflow_ai.admin_users bootstrap --username jdoe --name "Jane Doe" --role owner --data-dir /data
```

Set it up once:

```bash
openssl rand -hex 32          # → RS_AI_ADMIN_TOKENS (optional: automation / break-glass; keep them out of apps)
openssl rand -hex 32          # → RS_AI_LICENSE_SIGNING_KEY (keep it secret and backed up)
docker compose up -d
```

The app has to trust the signing key's **public** half (licenses and statement formats both use it).
Find it as `public_key` in `/admin/v1/licenses/plans` or `/admin/v1/parsers/bundle`.

- Build the app with `IFLOW_LICENSE_PUBKEYS=<public key>`, or pin it in
  `core/licensing/tokens.py` → `VENDOR_PUBLIC_KEYS`.
- Licenses verify offline in the app; the app never contacts this server to check one.

Privacy:
- The ledger (`admin.db`) holds license ids, plans, integer-cent amounts, dates and an optional
  **opaque** customer reference such as an order id. Emails and names are refused.
- Crash reports show install *counts*, and install ids are removed from report bodies.
- Back up `admin.db` together with the signing key.

## Security notes

- Tokens are compared in constant time against SHA-256 digests; requests without a valid token get 401.
- Per-token rate limits (429) and a single inference queue (503 + `Retry-After` when full).
- Upload size is enforced by Caddy and again by the app server; file signatures are checked before
  decoding; Pillow's decompression-bomb guard is on.
- The container runs as a non-root user with a read-only root filesystem, no Linux capabilities, and
  `no-new-privileges`. Port 8080 is only reachable from Caddy.
- `/docs` and `/openapi.json` are off unless `RS_AI_DOCS=true`.
- **Admin tokens:**
  - Stored only as SHA-256 digests, and each request is compared against *every* admin digest in
    constant time.
  - Client tokens never grant admin access.
  - After 10 failed admin attempts from one IP (configurable), the portal is locked for that IP for
    10 minutes (429).

## Development

```bash
pip install -r requirements.txt       # plus torch: pip install torch --index-url https://download.pytorch.org/whl/cpu
RS_AI_TOKENS=$(openssl rand -hex 32) RS_AI_DATA_DIR=/tmp/rs-ai uvicorn iflow_ai.app:create_app --factory --port 8080
```

Tests live in the repo's main suite (`tests/test_ai_service.py`) and use fake models, so they run without
torch: `pytest tests/test_ai_service.py` from the repo root.
