# c2w — agent instructions

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
| **No `.env` files. All settings live in the database** | Declared in `c2w/settings_spec.py`, stored in `app_settings`, edited in the UI or via `c2w-admin settings`. The only exceptions are `C2W_DATABASE_URL` and `C2W_MASTER_KEY(_FILE)`, which are what a process needs *before* it can read settings; both come from the systemd unit. |
| **Brands never share data** | Go4Rex and InterMagnum are different companies. Enforced by RLS, not by `WHERE`. |
| **No credentials in git, logs, templates, API responses or memory** | These are live telephony credentials. Store sealed; see "Secrets". |
| **Never hide a recording because correlation failed** | Losing access to audio is worse than showing a call with thin metadata. Unmatched objects become `orphan` and stay playable. |
| **Transfers are off until an archive exists** | No Wasabi buckets are provisioned yet. Inventory and correlation run regardless; `transfer.enabled` defaults to false and jobs are only created once a destination is configured. |

## Architecture rules

**Talk to storage through the protocols, never to a vendor.**
`c2w.storage.base.ObjectSource` / `ObjectDestination` are the interface.
`CommPeakSource` and `WasabiDestination` are thin configurations of `S3Client`.
Never write `if provider == "wasabi"` in the engine, the scanner or the media
gateway — a brand may move to MinIO or Backblaze, and CommPeak may not be the
only source forever.

**Brand isolation is a database guarantee.**
Every tenant-scoped table has `brand_id` and an RLS policy keyed off the
`c2w.brand_id` session variable. Set it once per request/job via the session
helper; do not rely on application filtering. `cdrs` and `recordings` are
`PARTITION BY LIST (brand_id)`. When you add a tenant-scoped table you must add
`brand_id`, the RLS policy, and a test proving brand A cannot read brand B.

**Verification is not optional.**
An upload is complete only when source size, destination size and the in-stream
SHA-256 all agree and a destination `HEAD` confirms the object. HTTP 200 is not
evidence. Multipart ETags are hashes of part hashes — never compare them to a
whole-object digest; that is why we store `c2w-sha256` as object metadata.

**Never stage media on local disk.**
13.9 TB moves through a 84 GB root volume. Source `GetObject` streams straight
into the destination multipart upload, with the digest computed in the same
pass. Local spool is only for resuming a specific part.

**Errors are classified before they are retried.**
`c2w.storage.errors.classify_exception` maps every failure to an `ErrorClass`.
`AUTH_ERROR`/`ACL_ERROR`/`CONFIG_ERROR` fail fast and alert a human — retrying
them five times only delays the alert. On CommPeak, `ACL_ERROR` almost always
means this server's public IP is missing from the account's Access Control List;
that hint is already wired in, keep it.

## Reading CommPeak's documentation

**Start at `https://docs.commpeak.com/llms.txt`.** It indexes every page and
every OpenAPI spec, and appending `.md` to any docs URL returns markdown.
Section indexes are at `/reference/<section>/llms.txt`. Read the `.md` of an
endpoint before writing a client for it -- guessing a contract from a plausible
URL cost real work here, and the answer was one hop away.

### CDRs — PBX Stats API

`POST https://<instance>.stats.pbx.commpeak.com/api/cdrs`,
form-encoded, API key in the `Authorization` header. Per-instance host, so
every account carries its own base URL.

Paging is `page` (1-based) and `cdrs_per_page`; the range is `from`/`till`;
ordering is `sort_by`/`sort_direction`. Filters include `country` (ISO-3166
alpha-2), `direction`, `call_type`, `destination`, `source`, `extension`,
`did`, `caller_id`, `hangup_cause`, `agent`, `queue`, `uniqueid`, and any
custom field by name. Returns `{"cdrs": [...]}` carrying `country_name`,
`agent_name`, `agent_pbxExtension`, `queue_name`, `bill_duration`,
`waiting_time`, `cost` and `recording_link`.

**Two CDR shapes exist.** The `call_uuid`/`src`/`dst`/`start_at` shape is a
different source (Dialer API or a webhook). `normalise_cdr` accepts both, and
must keep doing so.

### SMS — TextPeak

**Two endpoints, not one**, and they do not return the same fields. API key
bare in `Authorization` for both; both answer `{items, total}`.

| | Outgoing | Incoming |
|---|---|---|
| Path | `/textpeak/streams/messages` | `/textpeak/streams/incoming_messages` |
| Params | `type` `status` `streamId` `phone` `startDate` `endDate` `page` `itemsPerPage` | `destination` `streamId` `phone` `startDate` `endDate` `page` `itemsPerPage` |
| Timestamps | `sent_at`, `delivered_at` | `received_at` |
| Numbers | `source_number`, `destination_number` | `from`, `to` |
| Body | nested: `content.body` | top level: `body` |
| Also | `status` `cost` `platform` `campaign` | `contact_name` `message_length` |

`status` is a free string. The reference documents it as "Delivery status"
with the single example `delivered` and gives **no enumeration**, so there is
no enum for it in the schema -- one would reject real data the first time
CommPeak adds a state. `web/filters.py` maps the values it knows onto the four
pill meanings and shows anything else as-is.

Both shapes land in one `sms_messages` table with a `direction` column, because
what an operator wants is the exchange with a number and that interleaves the
two. `occurred_at` is the one timestamp every row has and is what the index and
the sort use. Several columns are null for one direction, which is honest: a
delivery status on a message sent *to* us is meaningless, not unknown.

**Delivery receipts arrive late** -- a message read a minute after sending says
`sent` and the same message says `delivered` an hour later, sometimes the next
day. So the poll re-reads a wide overlap (`sms.overlap_hours`, default 24) and
the upsert COALESCEs, or the first answer stays on the record for ever.

### The calls page: country, operator, SIP provider

Asked for by name. CommPeak's Search CDRs response schema settles what exists:

```
id call_start call_end duration bill_duration type destination caller_id
country_name agent_name agent_pbxExtension bridged_agent_name
bridged_agent_pbxExtension hangup_cause waiting_time recording_link
queue_alias queue_name desks custom_fields cost
```

* **country** -> `country_name`, stored as `dst_country`.
* **operator** -> the agent who handled it, `agent_name` + extension. A
  transfer has a *second* agent (`bridged_agent_*`) and naming only the first is
  wrong, so the column shows "Ana Ruiz then Luis Diaz".
* **SIP provider** -> **not in the CDR.** There is no carrier field and no trunk
  field. What exists is the CommPeak account the call arrived on -- one account
  per PBX or dialer, each with its own trunk -- which every row already carries
  as `connection_id`. The page shows its name and the column is called "SIP
  account", not "SIP provider", because that is what it is. If the destination
  *network* operator is ever wanted, that is CommPeak's separate Lookup API
  (HLR) and a new integration, not a column we are missing.

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
is the strongest join key available, which is what `c2w.commpeak.correlate`
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

Everything tunable is a `SettingSpec` in `c2w/settings_spec.py` (109 of them,
17 categories) and is stored in `app_settings`. Resolution is **brand override →
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
settings: `C2W_DATABASE_URL` and `C2W_MASTER_KEY_FILE`. See
`c2w/config.py`.

## Authentication

**Two-factor is built.** `c2w.auth.totp` is RFC 6238 on the standard library
(checked against the RFC's own vectors) and `c2w.auth.mfa` holds the state
machine: no secret -> pending -> active. A *pending* secret is one that has
never been proved against a real authenticator and is never demanded at login,
or an abandoned setup locks the account out. A code is single-use -- the
accepted counter is persisted and `verify` returns it precisely so the caller
must store it. Between the password and the code the browser holds a signed
five-minute ticket (`c2w_mfa`), never a session: a session that exists before
the code is checked is a session that works. Recovery codes are Argon2-hashed,
single-use, and using one revokes every other session.

Local accounts now, Active Directory / Entra ID later. `c2w-admin superadmin
create` makes the first account; `SUPER_ADMIN` is the only brand-less role and
remains the break-glass login once `auth.local_accounts_enabled` is turned off
for everyone else. The Entra group → role mapping writes into the same `users`
table, so switching to AD changes how a user is *authenticated*, not how
permissions are *evaluated*. `recordings.play` and `recordings.download` are
deliberately separate permissions.

## Secrets

- Per-connection S3 tokens and CDR API keys are sealed with AES-256-GCM under a
  **per-brand data key**, which is itself wrapped by `C2W_MASTER_KEY`
  (delivered via systemd `LoadCredential=`). See `c2w.crypto`.
- Sealing binds AAD (`conn:<id>:<field>`), so a ciphertext cannot be moved to
  another connection or another column. Preserve that when you touch it.
- `Settings.masked_dump()` is the only safe way to render configuration.
- Columns holding sealed values are suffixed `_sealed`. Never add one to an API
  response model or a template context.
- Credentials are entered through `c2w-admin` (prompted, no echo) or the UI,
  never committed, never logged. `c2w/storage/factory.py` is the only place
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
uv run uvicorn c2w.api.app:app --reload   # dev server
```

## Layout

```
src/c2w/
  config.py          bootstrap only (database URL + master key)
  settings_spec.py   the settings registry
  settings.py        DB-backed settings service, with a short-lived cache
  crypto.py          AES-GCM envelope encryption
  cli.py             c2w-admin
  logging.py         structlog, with credential redaction
  storage/    base.py errors.py s3_adapter.py commpeak.py wasabi.py factory.py
  commpeak/   keyparse.py correlate.py cdr_client.py sms_client.py
  db/         base.py session.py models/{core,auth,settings}.py
  sync/       queue.py inventory.py transfer.py
  media/      sdr.py
  auth/       local.py rbac.py totp.py mfa.py
  alerts/     base.py telegram.py slack.py
  api/        app.py deps.py v1/cdrs.py v1/messages.py
  web/        routes.py filters.py templates/ static/
  workers/    worker.py scheduler.py reconciler.py
deploy/       systemd/ nginx/ install.sh
```

The UI is server-rendered Jinja2 with htmx, and htmx/Alpine are **vendored** in
`web/static/vendor/` — this runs on a private network and must not need
outbound internet to render a page. There is no build step.

## The design system

`web/static/app.css` is the **Console UI Kit**, the customer's own house style
for operator consoles. Plain CSS tokens, no dependencies. Its rules are not
decoration and several are enforced in `web/filters.py`:

- **Four pill meanings only** — `ok` / `warn` / `err` / `idle`. Every state
  collapses into one. State colour is never used decoratively, so that amber on
  the page always means something.
- **No literal colours outside the token block.** Retheming is that block and
  nothing else.
- **Machine values get human labels.** The database stores `MISSING_SOURCE`;
  the page reads "gone from source". The raw value stays only where it is
  needed — a settings key, a technical-detail panel.
- **`flash` vs `note` vs `note.caution`** are distinct: the result of an action,
  an explainer, and the irreversible. Do not reach for caution to add emphasis.
- **`.tw` wraps every table**; add `.stack` plus a `data-label` per cell when it
  has more than about six columns, or it is unreadable on a phone.
- **Every figure carries a caption and a unit**, `tabular-nums` if it updates.
- Traps the kit names: `min-width:0` on grid and flex children;
  `overflow-x:clip` on body, never `hidden`; a `<button>` rule with a
  background will paint your chips.

## Gotchas that have already bitten

- `SET LOCAL` / `set_config(..., true)` ends at **commit**. Each transaction must
  set `c2w.brand_id` again. That transaction-bound lifetime is what stops a
  scope leaking to the next request on a pooled connection.
- The RLS policy uses `NULLIF(current_setting('c2w.brand_id', true), '')` —
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
- **`RETURNING (xmax = 0)` does not work on a partitioned table.** It is the
  usual way to ask an upsert "was this row new?", but `xmax` is a system column
  and asyncpg refuses to read one from an INSERT routed through a partitioned
  parent: *cannot retrieve a system column in this context*. `cdrs`,
  `recordings` and `sms_messages` are all partitioned by brand, so the CDR
  ingest carried this bug from the initial commit and would have failed on its
  first real row -- nothing covered it. Both ingest paths now return
  `(created_at = updated_at)` and set `updated_at = clock_timestamp()` on
  update; `clock_timestamp()` advances within a transaction where `now()` does
  not, which keeps the answer exact even when a batch inserts and updates the
  same row.
- **`normalise_msisdn` is for phone numbers only.** An SMS sender is often an
  alphanumeric sender ID, and the normaliser dutifully extracted the stray
  digit from `Go4Rex` and returned `"4"` -- which, in the indexed search
  column, made every message from the brand match a search for a number ending
  in 4. `sms_client._numeric_suffix` returns None for anything containing a
  letter. Do not reuse `normalise_msisdn` on a field that can hold a name.
- **`pg_trgm` is wanted, not required.** Migration 0001 used to abort without
  it, which contradicted both `c2w-admin doctor` (reports it as a warning) and
  the search builder (falls back to `ILIKE`), and locked out any host whose
  PostgreSQL lacks contrib. It now probes `pg_available_extensions` first --
  probing, not catching, because a failed `CREATE EXTENSION` aborts the
  transaction alembic keeps its own version row in -- skips the two GIN indexes
  and warns with the exact SQL to add them later.
- **`TRUNCATE` on a partitioned parent empties every partition.** A pipeline
  fixture ran `TRUNCATE ... recordings, cdrs CASCADE` to reset its own working
  set and silently deleted every other organisation's calls and recordings with
  it -- invisible in a throwaway database, destructive in one anybody else is
  using. It now deletes `WHERE brand_id = :b`, with the brand scope set first
  so RLS narrows it even if the predicate were dropped.
- The test suite creates a brand per run and never drops it, and each brand adds
  a partition to three tables. A query on a partitioned parent takes one lock
  per partition, so a long-lived scratch database eventually fails with *out of
  shared memory* at `max_locks_per_transaction` (default 64). Recreate the
  scratch database periodically, or raise that setting.

## The auto-sync hook

`.claude/auto-sync.sh` commits and pushes on every Stop. Two things about it
are load-bearing, and both were learned the hard way when its first run pushed
a file containing a fake AWS key past its own guard:

- **The credential scan must read the working tree, not `git diff`.** `git diff`
  reports tracked changes only, so a brand-new file — the shape a leak actually
  takes — is invisible to it. It greps `git ls-files --others
  --exclude-standard` too, and restricts the tracked scan to `+` lines so that
  *removing* a credential is never blocked.
- **A guard that depends on an environment variable a hook cannot see is not a
  guard.** The test gate keyed off `C2W_TEST_DATABASE_URL`, which a Stop hook
  does not inherit, so the RLS, pipeline and web suites silently never ran. It
  now reads `~/.config/c2w/test-database-url`, probes the port before trusting
  it, and states in the log and the commit message whether those suites really
  ran. If you add a gate here, ask what it does when its input is missing —
  silently passing is the wrong answer.
- **A third failure mode, found while fixing the first two:** the pattern list
  was rewritten to begin with `-----BEGIN`, which `grep` parsed as a bundle of
  short options. It exited 2 and matched nothing, while the log still read
  clean. Hence `grep -qE -e "$PATTERNS"`, and hence
  `tests/test_auto_sync_guard.py`, which runs the real invocation against leak
  shapes *and* against prose that merely names a credential field. The hook
  will not push on a failing suite, so that test is what keeps the guard from
  quietly dying again. Fixtures in it are assembled from two halves on purpose
  — as literals they trip the guard and it refuses to commit its own tests.

## Out of scope for v1

Voice transcription and analysis, FXRide CRM, Zendesk. Do not build these; do
not add dependencies for them. Keep the recording/CDR model open enough that a
transcript can attach to a recording later.
