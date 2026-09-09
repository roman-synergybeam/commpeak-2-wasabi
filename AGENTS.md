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
  auth/       local.py rbac.py totp.py mfa.py directory.py oidc.py turnstile.py
  transcribe/ base.py whisper_local.py service.py
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
- **`audit_events` is append-only by PostgreSQL *rules*** (`ON UPDATE/DELETE
  DO INSTEAD NOTHING`), not by revoked privileges -- the app role owns the
  table and an owner keeps its own UPDATE/DELETE grants, so revoking from
  PUBLIC does not stop it. The consequence to know: an attempt is *discarded*,
  reporting zero rows and raising nothing, so a stray
  `DELETE FROM audit_events` looks like it succeeded on an empty table. Data is
  safe; the silence is the surprise.
- **Writing an audit row needs the brand scope set.** The administrative routes
  run on an unscoped session on purpose -- they work across organisations -- so
  `record_admin_event` sets `c2w.brand_id` for the insert and restores it
  afterwards. Without that the RLS `WITH CHECK` rejects every row, and with a
  NULL brand it rejects them anyway (`NULL = NULL` is not true), which is why a
  platform-level action with no organisation in scope is logged as a warning
  instead.
- **In Jinja, `x.items` is `dict.items`, not the key `"items"`.** Attribute
  lookup wins over subscript, so a context dict with an `items` key iterates
  the bound method and raises `'builtin_function_or_method' object is not
  iterable`. The settings rail hit this; the key is named `sections` now,
  which is a better fix than remembering to write `x["items"]` everywhere.
- **A sealed setting's AAD must match the scope the value was *found* at**, not
  the scope that was asked for. `get_secret(key, brand_id=X)` resolves brand
  override -> global row, so a brand inheriting a global secret finds a
  ciphertext sealed with the `global` AAD; unsealing it with the brand's AAD
  fails, and the failure is caught and returned as `""`. That made every
  globally-set credential read as unset for every brand -- the settings page
  said "not set" and the alert senders sent nothing, silently.
  `SettingsService._resolve` returns the scope for exactly this reason.
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

## CommPeak's IP access control list

**Recordings access is IP-restricted, and it is not optional.** CommPeak's own
documentation for Recordings Access Accounts says it twice: *"Ensure that the
public IP address of your remote server or PC is whitelisted in the Access
Control List"* and *"To access your storage, you must configure access rules
for your S3 accounts."*

It is easy to conclude otherwise, because there is no separate page for it: the
**IP ACL** is a *tab* inside the **Recordings Access Accounts** sidebar, next to
the tab where the tokens were created. Portal path:

> my.commpeak.com -> Cloud PBX -> **PBX Instances** (or Dialer -> Dialer
> Instances) -> the instance's three-dot menu -> **Recording Access Accounts**
> -> **IP ACL** tab -> address with a subnet mask -> **Add**

Per account, not per portal. Eight S3 accounts means eight lists.

**How the refusal looks, and how to tell it apart from bad keys.** The endpoint
sits behind nginx, and a blocked address gets nginx's own HTML page:

```
HTTP/1.1 403 Forbidden
Server: nginx
Content-Type: text/html
<html><head><title>403 Forbidden</title></head>...
```

No S3 XML, so botocore reports `Code: '403', Message: 'Forbidden'` rather than
`AccessDenied`. That absence *is* the diagnosis -- the request never reached the
S3 layer. Compare:

| What you see | What is wrong |
|---|---|
| nginx HTML 403, code `403` | this host's address is not on the account's IP ACL |
| XML `AccessDenied` | keys are valid, the operation is not permitted |
| XML `SignatureDoesNotMatch` | the secret is wrong |
| XML `InvalidAccessKeyId` | the token is wrong |

An unsigned `curl https://recordings.commpeak.com/` from the host is the fastest
check: nginx HTML 403 means the address is blocked, and no credential is
involved.

There is also an **Access Summary** tab beside the IP ACL one, with a
downloadable CSV of "IP address, exact time, action, downloaded file path, and
errors" -- refused attempts appear there, which confirms the address being
rejected without guessing.

## Reading Active Directory

`c2w.auth.directory` searches AD for people, groups and OUs so an
administrator can pick somebody instead of retyping an address. Getting that
address wrong is not a validation error -- the account is created and then
simply never matches at sign-in, which is a worse failure than a typo.

Three rules, all of them because a directory is a remote system on the far
side of a web request:

- **Every call is bounded.** ldap3 is synchronous, so it runs in a worker
  thread with a connect timeout, a receive timeout, a result cap and an outer
  `asyncio.wait_for`. A controller that accepts a TCP connection and then says
  nothing must not hold a request open.
- **Failure is a sentence, never an exception through the page.** Everything
  returns `DirectoryResult` with either entries or a message naming which of
  the six things that can be wrong actually is -- credentials, address, DNS,
  TLS, base DN, size. That is the whole job when a directory will not answer.
- **Nothing is ever written.** It binds `read_only=True` and searches; a test
  asserts the module contains no `add`/`modify`/`delete` call. Group
  membership decides a role at sign-in, and is not edited from here.

**ldap3's SAFE_SYNC does not populate `connection.entries`.** This is the
trap in this module and it cost every directory search: `_connection` dials
SAFE_SYNC, which returns `(status, result, response, request)` from `search()`
and leaves `.entries` empty. The code read `.entries`, so against a real
controller every search found nothing -- the settings test reported "found no
people under the starting point" about a domain holding fifty accounts, the
user type-ahead offered nobody, and a directory sign-in could never locate the
account it needed to bind as. **The test suite could not catch it**, because it
injects a `MOCK_SYNC` connection and that strategy *does* fill `.entries`: the
mock and the real client disagreed about where the answer lives, and only the
mock was ever asked. `_response_rows` now reads the response list -- from the
return tuple under SAFE_SYNC, from `connection.response` otherwise -- and the
guards for it in `test_directory.py` deliberately do **not** use the mock, but
a stand-in shaped like SAFE_SYNC with `.entries` empty. When a fake and the
real library differ in shape, test the shape the real one has.

Related: `sizeLimitExceeded` is a *successful* truncated search, not an error.
The server answered and stopped at the ceiling we asked for; the rows it sent
are all good. That is what `DirectoryResult.truncated` is for.

**A test button must not refuse because the feature is switched off.** The
Active Directory check used to return "Active Directory is switched off on this
page." and do nothing, which had the order backwards -- you prove the address
and the reading account work *first* and turn it on once they do. The switch is
reported as a note now and the probe runs regardless, with the summary saying
sign-in is still off so a green result cannot be mistaken for "AD login is
live". Turnstile and the tunnel checks already worked this way.

LDAP filters get the same treatment as SQL: `escape_filter` escapes the five
RFC 4515 characters, and the tests assert on the **filter string**, not on what
a server does with it -- ldap3's mock treats `\2a` as a wildcard rather than a
literal asterisk, so a mock-driven test would report an injection a real
controller does not have, and would pass just as happily with the escaping
removed.

Tests run against ldap3's `MOCK_SYNC` strategy, which makes the happy path
verifiable without a domain controller. The mock's entries live on the
*connection's* strategy, not the server's, which is why `search()` takes a
connection. Not covered, and needing a real controller: TLS negotiation,
referrals, paging past the size limit.

## Signing in with Microsoft or Google

`c2w.auth.oidc` is the authorization-code flow with PKCE, written out rather
than handed to a framework helper -- the parts that matter are the checks, and
a helper that silently skips one is worse than code you can read. On the way
back, all of these are verified before any claim is trusted: **state** (against
a signed short-lived cookie), **nonce** (which ties the token to *this*
attempt), the **PKCE verifier**, the **signature** against the provider's JWKS,
the **issuer** and **audience** (a valid token minted for another application
is still not for us), **expiry**, and the **domain allow list** -- because
"signed in with Google" is not "works for this company".

Two details that will bite anyone editing it:

- **The flow cookie must be `samesite=lax`, never `strict`.** The provider
  redirects the browser back with a top-level GET, and `strict` withholds the
  cookie on exactly that request, breaking every sign-in.
- **`none` is stripped from the accepted algorithm list.** An unsigned token is
  the whole attack, and a permissive list is how it gets accepted.

`link_federated_user` refuses to attach an external identity to an address
already held by a **local** account, or by a *different* provider. Either would
be account takeover by anyone able to create a directory entry.

Tested by minting ID tokens with a locally generated RSA key against a stubbed
discovery document, so every refusal above is exercised. Not covered, and
needing a real tenant: the consent screen and whether the registered redirect
address matches.

`authlib.jose` is deprecated in favour of `joserfc` and stays compatible until
authlib 2.0; it is the only thing pinning that.

## Transcription

Whisper on this server, via `faster-whisper` (CTranslate2) -- several times
quicker on CPU for the same model, which matters with no GPU and 19M
recordings. Local by default on purpose: the alternative is posting recorded
customer calls to a third party, and that should be a deliberate choice rather
than an inherited default.

- **Read from the archive, never from CommPeak.** Pulling 13.9 TB through here
  a second time to transcribe it would double the transfer this system exists
  to do once. A recording with no `destination_key` is skipped, not fetched
  from source.
- **The engine menu offers six and two are built.** Choosing one of the other
  four raises `EngineUnavailable` with a sentence saying so, once per pass --
  not silently nothing, and not a failure per recording.
- **Whisper wants a base language code.** The menu reads "pt-BR — Portuguese
  (Brazil)" because that is what a person chooses between; Whisper takes "pt"
  and silently ignores a regional variant, so `_language_code` strips it.
- **Redaction starts at seven digits.** Six is a date or an amount far more
  often than an account number, and masking those makes a transcript unreadable
  for no gain. The last two digits are kept, because "ending 44" is what makes
  a redacted transcript still findable.
- Recognition runs in a worker thread: CTranslate2 is CPU-bound C++ and would
  block the event loop for the length of the recording otherwise.
- `transcribe.diarize` is in the settings and does nothing yet. faster-whisper
  does not diarise; doing it properly needs a second model.

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

## The settings rail holds two things that are not settings

`Organisations` and `Users` were pages of their own in the top menu and are now
sections of `/admin/settings`. Both are configuration by any reading -- an
organisation is the thing every other setting hangs off, and who may sign in
belongs beside the sign-in sources it depends on -- and the top menu is for
what an operator does daily.

They are not `SettingSpec` categories, so three things had to be arranged and
must stay arranged:

- **Each keeps its own permission.** `_SECTION_PERMISSION` maps the section to
  `brands.manage` and `users.manage`, and `_settings_sections` drops a section
  the role lacks. The settings page itself only requires `settings.view`, so
  without that map, moving these pages in would have handed every organisation
  admin the platform-wide organisations view. A section the role may not see is
  absent from `by_slug` and so falls back to the checklist, which is also the
  right answer for a link pasted by somebody with more access. Nobody is locked
  out by the move: `ADMIN` holds everything except `brands.manage`, so anyone
  who could manage users could already view settings.
- **The pane branches before the settings card.** `categories[active_section]`
  raises a `KeyError` for a section with no specs, so `settings.html` renders
  `_organisations.html` / `_users.html` and returns rather than falling through
  to `_settings_card.html`.
- **The old addresses stay, as 303s.** About a dozen POST handlers redirect to
  `/admin/users?saved=…` afterwards, and `_moved_to` forwards those to the
  section with the message intact. Forwarding from one place beats editing a
  dozen and missing one. In-app links (`_MANAGE_LINKS`, the setup checklist,
  the back-link on `/admin/users/<email>`) point at the section directly, so
  only outside links pay for the hop.

`/admin/users/<email>` is still a page in its own right. Editing one person is
not a settings pane.

## Out of scope for v1

Voice transcription and analysis, FXRide CRM, Zendesk. Do not build these; do
not add dependencies for them. Keep the recording/CDR model open enough that a
transcript can attach to a recording later.

## CommPeak's IP ACL cannot be automated

Checked against all three relevant specs (`cloud-pbx-api-v201`,
`dialer-api-v201`, and the `llms.txt` index): **there is no endpoint for
Recordings Access Accounts, S3 accounts, or the IP ACL.** The Cloud PBX API
manages calls, users, desks, roles, devices, caller ids, CDRs and speech
recognition, and nothing about storage access. So whitelisting this host is
eight visits to the portal, one per S3 account, and no amount of API work
shortens it. Do not go looking again.

The ACL work does take effect, which is worth knowing before spending a day on
the credentials instead: after `145.239.102.215` was added to the ACL of
`b7ac3a8c…` (`go4rex.pbx`), that account probed `OK` -- a complete, signed,
authenticated listing. The sealed tokens and secrets are therefore correct, and
nothing about them needs revisiting. See the paragraph below on how that access
then disappeared again.

**An unsigned `curl` does not test the IP ACL, in either form.** The IP ACL
section above suggests `curl -i https://recordings.commpeak.com/` as the quick
check, and `storage/errors.py` used to say so in its ACL hint. Measured: a request to `/`
and a request to a specific bucket both return nginx's HTML 403 *regardless* of
whether the address is on that account's list -- nginx refuses an unsigned
request before any ACL decision is reached. The contrast the hint describes is
real, but only on a **signed** request, which means the account probe on the
page is the only way to see it. The hint in `storage/errors.py` was corrected
to say so.

**The discriminator, confirmed on live accounts.** Both shapes were observed
within one minute of each other on 2026-09-09, which is what makes them
trustworthy rather than assumed:

| `status_detail` | Reached | Means |
|---|---|---|
| `Forbidden` (nginx HTML, no S3 code) | nginx only | address not accepted |
| `Access Denied.` (S3 XML) | the S3 layer | address accepted, keys or permission wrong |

So `Access Denied.` is *progress*, not a worse failure. It is the message that
says the ACL work landed.

**Access appeared and then went away again, from an unchanged address.** At
19:51 local, `b7ac3a8c` (`go4rex.pbx`) probed `OK` outright and `b38e533f`
(`go4rex.td`) got S3 XML `Access Denied.` -- both had reached S3. Within
minutes, and on four probes spaced twenty seconds apart, every account returned
nginx `Forbidden`, with this host's egress address still `145.239.102.215` on
two independent checks. Two explanations fit and this end cannot separate them:
the ACL entries were changed at CommPeak, or the roughly twenty probe clicks
between 19:20 and 19:51 tripped a rate limit that answers with the same nginx
403 as a block. **Do not read a single `Forbidden` as proof the ACL is
unset** -- that is what the persisted `status` column implies and it is not
sound. Space the probes out and re-check before concluding anything.

**`status` on `commpeak_connections` is a record of the last probe, not a live
reading.** `test_connection` writes it, so a row can say `OK` long after access
has stopped working, which is exactly how the page came to show one green
account that a probe seconds later refused. Treat it as a timestamped history
and read `updated_at` beside it.

## The tunnel check, and inferring instead of proving

`_check_tunnel` used to resolve the tunnel hostname and expect a CNAME to
`<tunnel-id>.cfargotunnel.com`. **That record is never publicly visible.** A
tunnel route is always proxied, so Cloudflare answers with its own anycast A
records -- meaning the check returned "does not point into this tunnel"
exactly when the tunnel was configured correctly, and it `return`ed on that
verdict, so the end-to-end fetch that would have disproved it never ran. The
page sent an operator to fix something that worked.

What replaced it is the shape to copy: fetch `https://<hostname>/api/health`,
require *our own* health JSON back (a stranger's 200 is not a pass), and prove
the request travelled through *this* cloudflared by reading
`cloudflared_tunnel_total_requests` from its local metrics before and after.
A counter that does not move is reported as a note, not a failure -- the
console demonstrably answers, and calling that an error would be the original
mistake pointed the other way. `tests/test_tunnel_check.py` pins the
proxied-hostname case and strips docstrings before asserting the DNS inference
is gone, so the explanation above cannot be what satisfies the guard.
