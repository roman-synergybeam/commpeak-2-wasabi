# cprec — agent instructions

CommPeak → Wasabi call-recording offload and CDR platform.
Read this before changing anything. `CLAUDE.md` is a symlink to this file.

## What this system does

Two unrelated companies (**Go4Rex**, **InterMagnum**) keep call recordings in
CommPeak's S3-compatible storage — 8 buckets, ~19.3M objects, ~13.9 TB today and
growing. This platform:

1. inventories those buckets,
2. polls the CommPeak CDR API for call metadata,
3. correlates each recording to a CDR,
4. streams recordings to Wasabi and verifies them byte-for-byte,
5. serves a per-brand CDR UI with search, filter, sort, export and playback,
6. optionally deletes the CommPeak copy after a retention window (default 90 days),
7. alerts to Telegram and Slack.

CommPeak is the source of truth for **call metadata**.
Wasabi is the source of truth for **retained media**.

## Hard constraints — do not violate these

| Constraint | Why |
|---|---|
| **CommPeak is strictly read-only** | We never write to or delete from CommPeak. `CommPeakSource` overrides every mutating method to raise `SourceIsReadOnly`, and the `delete_source_*` columns were dropped in migration 0002. There is no setting that re-enables it; doing so would be a deliberate code change with a review attached. |
| **No Docker, no containers** | Explicit customer requirement. systemd units on one Linux VM. |
| **No Redis, no extra daemons** | PostgreSQL is the job queue (`FOR UPDATE SKIP LOCKED`). Adding a broker re-introduces the operational surface the deployment rules out. |
| **No `.env` files. All settings live in the database** | Declared in `cprec/settings_spec.py`, stored in `app_settings`, edited in the UI or via `cprec-admin settings`. The only exceptions are `CPREC_DATABASE_URL` and `CPREC_MASTER_KEY(_FILE)`, which are what a process needs *before* it can read settings; both come from the systemd unit. |
| **Brands never share data** | Go4Rex and InterMagnum are different companies. Enforced by RLS, not by `WHERE`. |
| **No credentials in git, logs, templates, API responses or memory** | These are live telephony credentials. Store sealed; see "Secrets". |
| **Never hide a recording because correlation failed** | Losing access to audio is worse than showing a call with thin metadata. Unmatched objects become `orphan` and stay playable. |
| **Transfers are off until an archive exists** | No Wasabi buckets are provisioned yet. Inventory and correlation run regardless; `transfer.enabled` defaults to false and jobs are only created once a destination is configured. |

## Architecture rules

**Talk to storage through the protocols, never to a vendor.**
`cprec.storage.base.ObjectSource` / `ObjectDestination` are the interface.
`CommPeakSource` and `WasabiDestination` are thin configurations of `S3Client`.
Never write `if provider == "wasabi"` in the engine, the scanner or the media
gateway — a brand may move to MinIO or Backblaze, and CommPeak may not be the
only source forever.

**Brand isolation is a database guarantee.**
Every tenant-scoped table has `brand_id` and an RLS policy keyed off the
`cprec.brand_id` session variable. Set it once per request/job via the session
helper; do not rely on application filtering. `cdrs` and `recordings` are
`PARTITION BY LIST (brand_id)`. When you add a tenant-scoped table you must add
`brand_id`, the RLS policy, and a test proving brand A cannot read brand B.

**Verification is not optional.**
An upload is complete only when source size, destination size and the in-stream
SHA-256 all agree and a destination `HEAD` confirms the object. HTTP 200 is not
evidence. Multipart ETags are hashes of part hashes — never compare them to a
whole-object digest; that is why we store `cprec-sha256` as object metadata.

**Never stage media on local disk.**
13.9 TB moves through a 84 GB root volume. Source `GetObject` streams straight
into the destination multipart upload, with the digest computed in the same
pass. Local spool is only for resuming a specific part.

**Errors are classified before they are retried.**
`cprec.storage.errors.classify_exception` maps every failure to an `ErrorClass`.
`AUTH_ERROR`/`ACL_ERROR`/`CONFIG_ERROR` fail fast and alert a human — retrying
them five times only delays the alert. On CommPeak, `ACL_ERROR` almost always
means this server's public IP is missing from the account's Access Control List;
that hint is already wired in, keep it.

## CommPeak specifics

Source: `https://recordings.commpeak.com`, **path-style addressing**, SigV4,
bucket name is the account UUID, calling IP must be whitelisted in the account
ACL, ~5 concurrent transfers per account.

Object keys look like:

```
/2025/11/11/02/out-441632960770-101-20211111-125343-1636635223.0.flac
   year/mo/dy/hr  dir number       ext date     time   channel-id  seq  format
```

**Keys carry no `call_uuid`.** The trailing `1636635223` is a FreeSWITCH channel
id — a unix epoch second at channel creation — and in the documented example it
decodes to exactly the filename's wall clock (`2021-11-11 12:53:43Z`). That epoch
is the strongest join key available, which is what `cprec.commpeak.correlate`
is built on. Match tiers, best first:

| Tier | Rule | Confidence |
|---|---|---|
| `epoch_exact` | epoch within ±2 s of CDR `start_at` **and** number agrees | 0.99 |
| `epoch_only` | epoch within ±2 s, number disagrees | 0.90 |
| `time_number` | within ±90 s and number agrees | 0.75 |
| `time_only` | within ±90 s, nothing else agrees | 0.40 (review) |
| `orphan` | no candidate | 0.0 (still offloaded) |

Ambiguous matches (two candidates at the same tier, <1 s apart) are recorded as
`match_ambiguous` with halved confidence — never silently resolved.

Numbers are compared as digit **suffixes** (`593990899917@did.commpeak.com` vs
`441632960770` vs `00441632960770`), but values shorter than 6 digits — agent
extensions like `101` — must match exactly, or `101` would wrongly agree with
`9101`. See `numbers_agree`.

The CDR's `public_recording_url` (`/record/public/<call_id>/<sha1>/as/mp3`) is an
independent MP3 fetch path; keep it as a fallback and cross-check, not the
primary source.

## Configuration

Everything tunable is a `SettingSpec` in `cprec/settings_spec.py` (46 of them,
9 categories) and is stored in `app_settings`. Resolution is **brand override →
global row → registry default**. Read them through `settings_service`, never
from the environment:

```python
ttl = await settings_service.get_int(session, "media.presign_ttl_seconds")
days = await settings_service.get_int(session, "retention.offload_after_days",
                                      brand_id=brand.id)
token = await settings_service.get_secret(session, "alerts.telegram_bot_token")
```

Adding a setting means adding a spec — a bare string key raises `KeyError`
rather than silently returning a default. Secrets are sealed before storage,
reported to the UI as configured/not, and never written to `setting_history`.
All credential settings ship empty; the operator fills them in later.

Two values are *not* settings, because they are how a process reaches the
settings: `CPREC_DATABASE_URL` and `CPREC_MASTER_KEY_FILE`. See
`cprec/config.py`.

## Authentication

Local accounts now, Active Directory / Entra ID later. `cprec-admin superadmin
create` makes the first account; `SUPER_ADMIN` is the only brand-less role and
remains the break-glass login once `auth.local_accounts_enabled` is turned off
for everyone else. The Entra group → role mapping writes into the same `users`
table, so switching to AD changes how a user is *authenticated*, not how
permissions are *evaluated*. `recordings.play` and `recordings.download` are
deliberately separate permissions.

## Secrets

- Per-connection S3 tokens and CDR API keys are sealed with AES-256-GCM under a
  **per-brand data key**, which is itself wrapped by `CPREC_MASTER_KEY`
  (delivered via systemd `LoadCredential=`). See `cprec.crypto`.
- Sealing binds AAD (`conn:<id>:<field>`), so a ciphertext cannot be moved to
  another connection or another column. Preserve that when you touch it.
- `Settings.masked_dump()` is the only safe way to render configuration.
- Columns holding sealed values are suffixed `_sealed`. Never add one to an API
  response model or a template context.
- Credentials are entered through `cprec-admin` (prompted, no echo) or the UI,
  never committed, never logged. `cprec/storage/factory.py` is the only place
  they are unsealed.

## Conventions

- Python 3.13, `uv` for everything: `uv sync --extra dev`, `uv run pytest`,
  `uv run ruff check src/ tests/`, `uv run mypy src/`.
- Line length 100. Run `uv run ruff check --fix` before finishing.
- Async throughout (`asyncio_mode = "auto"`; tests need no decorator).
- SQLAlchemy 2.0 typed `Mapped[...]` style. Schema changes go through Alembic —
  never `create_all` against a real database.
- S3 behaviour is tested against a real `moto` server, not a mocked client, so
  multipart assembly, Range requests and error codes are actually covered.
- Docstrings explain *why*; the code already says what.

## Commands

```bash
uv sync --extra dev                     # install
uv run pytest -q                        # all tests
uv run pytest tests/test_correlation.py # correlation only
uv run ruff check --fix src/ tests/     # lint
uv run alembic upgrade head             # migrate
uv run alembic revision -m "..."        # new migration
uv run uvicorn cprec.api.app:app --reload   # dev server
```

## Layout

```
src/cprec/
  config.py          bootstrap only (database URL + master key)
  settings_spec.py   the settings registry
  settings.py        DB-backed settings service, with a short-lived cache
  crypto.py          AES-GCM envelope encryption
  cli.py             cprec-admin
  logging.py         structlog, with credential redaction
  storage/    base.py errors.py s3_adapter.py commpeak.py wasabi.py factory.py
  commpeak/   keyparse.py correlate.py cdr_client.py
  db/         base.py session.py models/{core,auth,settings}.py
  sync/       queue.py inventory.py transfer.py
  media/      sdr.py
  auth/       local.py rbac.py
  alerts/     base.py telegram.py slack.py
  api/        app.py deps.py v1/cdrs.py
  web/        routes.py filters.py templates/ static/
  workers/    worker.py scheduler.py reconciler.py
deploy/       systemd/ nginx/ install.sh
```

The UI is server-rendered Jinja2 with htmx, and htmx/Alpine are **vendored** in
`web/static/vendor/` — this runs on a private network and must not need
outbound internet to render a page. There is no build step.

## Gotchas that have already bitten

- `SET LOCAL` / `set_config(..., true)` ends at **commit**. Each transaction must
  set `cprec.brand_id` again. That transaction-bound lifetime is what stops a
  scope leaking to the next request on a pooled connection.
- The RLS policy uses `NULLIF(current_setting('cprec.brand_id', true), '')` —
  without the `NULLIF`, an unscoped query on a reset connection *errors* instead
  of returning zero rows.
- Queue claims use a **CTE**, not `UPDATE ... WHERE id IN (SELECT ... LIMIT n
  FOR UPDATE SKIP LOCKED)`. The latter can be planned as a semi-join and
  re-executed per row, so a worker asking for 3 jobs takes the whole queue.
- `app_settings` uniqueness is two **partial** indexes (global vs per-brand), so
  `ON CONFLICT` must restate the index predicate.
- Migrations must use `postgresql.ENUM(name=..., create_type=False)`;
  `sa.Enum(name=..., create_type=False)` emits `CREATE TYPE ... AS ENUM ()`.
- A Jinja filter returning HTML must return `Markup`, or it renders escaped.
- `pg_try_advisory_lock` is session-scoped: hold the connection for the life of
  the process, or the lock is released the moment the session returns to the pool.

## Out of scope for v1

Voice transcription and analysis, FXRide CRM, Zendesk. Do not build these; do
not add dependencies for them. Keep the recording/CDR model open enough that a
transcript can attach to a recording later.
