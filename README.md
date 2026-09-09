# c2w — CommPeak to Wasabi

CommPeak → Wasabi call-recording offload and CDR platform.

Recordings accumulate in CommPeak's S3-compatible storage — 8 buckets, ~19.3M
objects, ~13.9 TB across two unrelated companies. `c2w` inventories those
buckets, polls the CommPeak CDR API for call metadata, correlates each recording
to a call, streams it to an S3-compatible archive with byte-level verification,
and serves a per-brand CDR UI with search, filter, sort, export and in-browser
playback.

CommPeak stays the source of truth for call metadata; the archive becomes the
source of truth for retained media.

**The platform is strictly read-only against CommPeak.** It never writes there
and never deletes there — enforced in code, not by convention.

Everything is named **c2w**: the package, the CLI (`c2w-admin`), the systemd
units, the database, the `C2W_` environment prefix, and the system account the
services run as. One account per application is how apps on a shared host stay
separated — the services need no privilege at runtime and never run as root.

See [AGENTS.md](AGENTS.md) for architecture rules and the gotchas that have
already bitten.

## How it is configured

There are no configuration files. Every setting lives in the database (46 of
them, 9 categories) and is edited in the web UI under **Settings** or with
`c2w-admin settings set`. Changes apply across every process within seconds.

Exactly two values come from the environment, because they are what a process
needs *before* it can read a setting — the database URL and the master key that
unseals stored credentials. Both are supplied by the systemd unit.

All credential settings ship empty, to be filled in by an operator.

## Status

| Area | State |
|---|---|
| Recording key parser | done — documented + variant patterns, split recordings, salvage path |
| CDR correlation | done — 5 match tiers, ambiguity detection, accuracy reporting |
| S3 adapter layer | done — streaming, in-flight SHA-256, multipart, resume, presign, bandwidth cap |
| CommPeak source | done — **read-only by construction**; every mutating method raises |
| Error classification | done — 11 classes, retry/alert policy, CommPeak ACL hints |
| Credential sealing | done — AES-256-GCM envelope encryption, per-brand keys, AAD binding |
| Schema + brand isolation | done — RLS with `FORCE`, LIST partitioning, append-only audit |
| Settings in the database | done — registry, validation, brand overrides, sealed secrets, history |
| Local auth + roles | done — Argon2id, revocable sessions, lockout. Three roles: platform admin, admin (the only one that can delete), operator (search, listen, export) |
| Inventory scanner | done — hour-prefix, resumable, idempotent, works with no archive configured |
| Job queue | done — PostgreSQL `SKIP LOCKED`, leases, classified retry ladder |
| Transfer + verification | done — stream, verify, sidecar metadata, re-queue on archive loss |
| Workers | done — worker pool, scheduler, nightly reconciler (both singleton-locked) |
| Media delivery | done — presigned URLs, separate play/download permissions, full audit |
| CDR API client | done — against the documented PBX Stats API: form-encoded POST, `page`/`cdrs_per_page`, `from`/`till`. Accepts both CDR shapes CommPeak returns |
| Web UI | done — built on the Console UI Kit design system; dashboard, call search, detail + player, sync status, settings, audit, people |
| Alerts | done — Telegram + Slack, per-brand, severity routing, deduplication |
| Deployment | done — systemd units, nginx, idempotent installer |

**Not yet built.** The settings and the schema are in place for each of these,
so nothing has to be migrated when the work happens — but no code runs yet, and
each setting says so where it could be mistaken for working:

- **Transcription and voice analysis.** `transcripts` and `transcript_segments`
  exist, partitioned and isolated like everything else, with full-text indexes
  for English, Spanish and Brazilian Portuguese — the three languages these
  calls are in. The recogniser adapter is the remaining work; Whisper on this
  server keeps recordings and transcripts inside your own infrastructure, which
  is why it is the default in the settings.
- **Single sign-on** through Microsoft 365, Google Workspace or Active
  Directory. Columns, group-to-role mapping and settings are there; the sign-in
  flow is not.
- **Two-factor** for local accounts. Single sign-on already carries whatever
  second factor your directory enforces.
- **Cloudflare** tunnel and Turnstile.
- Microsoft 365 / Google Drive export, FLAC→MP3 transcoding for older browsers,
  and a `connection test` CLI subcommand.

**Still out of scope:** FXRide CRM and Zendesk.

## Organisations, accounts and people

An **organisation** is a customer company, and the hard isolation boundary —
Go4Rex and InterMagnum share nothing, enforced by row-level security rather
than by application filtering.

Each organisation can have **as many CommPeak accounts as it has PBXes and
dialers**, each with its own bucket and credentials, and **as many Wasabi
buckets as it needs**, each with its own region and keys. Settings apply across
an organisation's accounts; the accounts themselves are managed on the CommPeak
and Archive pages.

Three roles, each describable in a sentence:

| Role | Can |
|---|---|
| Platform admin | Every organisation, everything in each |
| Admin | One organisation, everything there — the only role that can delete a recording |
| Operator | Search calls, listen, download and export |

Transfers stay disabled until an archive destination exists — no Wasabi buckets
are provisioned yet. Inventory, correlation and CDR search all work without one,
so the UI is useful in the meantime and nothing needs re-scanning later.

## Development

```bash
uv sync --extra dev
uv run pytest -q                     # 203 tests
uv run ruff check src/ tests/
uv run uvicorn c2w.api.app:app --reload
```

Most tests need PostgreSQL, because what they test *is* database behaviour —
RLS policies, `SKIP LOCKED` claims, partition routing. They skip cleanly
without one (104 pass, 99 skip):

```bash
createdb c2w_test
C2W_DATABASE_URL=postgresql+asyncpg://user@localhost/c2w_test \
    uv run alembic upgrade head
C2W_TEST_DATABASE_URL=postgresql+asyncpg://user@localhost/c2w_test \
    uv run pytest
```

The connecting role must not be a superuser and must not hold `BYPASSRLS`;
either bypasses every policy and the isolation tests would pass while proving
nothing.

### Automatic commit and push

`.claude/auto-sync.sh` runs as a Claude Code `Stop` hook and commits and pushes
whatever changed, once per turn. It refuses, and logs why to
`.claude/auto-sync.log`, when:

- a credential pattern appears in an added line of a tracked file, or anywhere
  in a new untracked one — a private key, an `AKIA…` id, `aws_secret_access_key`,
  a GitHub or Slack token, or `C2W_MASTER_KEY=`. Removing a credential is always
  allowed;
- `.env`, `master.key`, `secrets.env` or `database.env` is in the tree;
- `pytest` fails, or `ruff check` does;
- `HEAD` is detached, or a merge or rebase is in progress.

Because a Stop hook does not inherit your shell environment, it reads the test
database from **`~/.config/c2w/test-database-url`** (one line, `0600`):

```bash
mkdir -p ~/.config/c2w
echo 'postgresql+asyncpg://c2w@127.0.0.1:5432/c2w_test' \
    > ~/.config/c2w/test-database-url
chmod 600 ~/.config/c2w/test-database-url
```

The guard's pattern list is itself covered by `tests/test_auto_sync_guard.py`,
which runs the real `grep` invocation against known leak shapes and against
prose that merely names a credential field — it has to pass both, since a guard
that blocks its own documentation gets loosened by whoever it blocks. Because
the hook refuses to push on a failing suite, breaking that regex now stops the
push instead of silently matching nothing.

Without the URL file the database-backed suites skip themselves, the push still
happens, and both the log line and the commit message say so — `NO DATABASE, so
the RLS/pipeline/web suites skipped` rather than a bare green tick. If the file
points at a database that no longer answers, the hook notes it and proceeds
without it, rather than erroring and blocking every later commit.

## Deployment

```bash
sudo ./deploy/install.sh --workers 2
```

Then:

```bash
c2w-admin superadmin create --email you@example.com
c2w-admin brand add --name 'Go4Rex' --slug go4rex
c2w-admin tenant add --brand go4rex --name 'go4rex.td.commpeak.com' --slug go4rex-td
c2w-admin connection add --brand go4rex --tenant go4rex-td \
    --name 'Go4Rex TD' --bucket <account-uuid>      # prompts for token and secret
c2w-admin doctor
```

CommPeak requires this server's public IP on each S3 account's Access Control
List — that is the most common onboarding failure, and the connection self-test
names it explicitly.

Back up `/etc/c2w/master.key` somewhere the database backup is not. Without
it, every stored credential is unrecoverable.

## Security

- Brand isolation is enforced by PostgreSQL RLS with `FORCE`, not by application
  filtering. An unscoped query returns zero rows rather than everything.
- Per-connection credentials are sealed with AES-256-GCM under a per-brand data
  key, wrapped by a master key delivered via systemd `LoadCredential=`. AAD
  binds each ciphertext to its connection and field.
- `audit_events` and `setting_history` are append-only; `UPDATE`/`DELETE` are
  rewritten to no-ops.
- Playback issues short-lived presigned URLs; the browser never sees storage
  credentials, audio never passes through the application, and every access —
  allowed or refused — is audited.
- `recordings.play` and `recordings.download` are separate permissions, because
  some organisations allow listening but forbid taking copies away.
