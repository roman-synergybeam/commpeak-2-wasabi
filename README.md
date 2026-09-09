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
| Local auth + RBAC | done — Argon2id, revocable sessions, lockout, 7 roles, 17 permissions |
| Inventory scanner | done — hour-prefix, resumable, idempotent, works with no archive configured |
| Job queue | done — PostgreSQL `SKIP LOCKED`, leases, classified retry ladder |
| Transfer + verification | done — stream, verify, sidecar metadata, re-queue on archive loss |
| Workers | done — worker pool, scheduler, nightly reconciler (both singleton-locked) |
| Media delivery | done — presigned URLs, separate play/download permissions, full audit |
| CDR API client | done — field mapping pinned to the real payload; transport configurable |
| Web UI | done — built on the Console UI Kit design system; dashboard, call search, detail + player, sync status, settings, audit, people |
| Alerts | done — Telegram + Slack, per-brand, severity routing, deduplication |
| Deployment | done — systemd units, nginx, idempotent installer |

**Not yet built:** Entra ID / Google Workspace SSO (schema and settings are in
place, the flow is not), M365/Drive export, FLAC→MP3 transcoding, and a
`connection test` CLI subcommand.

**Deliberately out of scope for v1:** voice transcription/analysis, FXRide CRM,
Zendesk.

Transfers stay disabled until an archive destination exists — no Wasabi buckets
are provisioned yet. Inventory, correlation and CDR search all work without one,
so the UI is useful in the meantime and nothing needs re-scanning later.

## Development

```bash
uv sync --extra dev
uv run pytest -q                     # 156 tests
uv run ruff check src/ tests/
uv run uvicorn c2w.api.app:app --reload
```

Most tests need PostgreSQL, because what they test *is* database behaviour —
RLS policies, `SKIP LOCKED` claims, partition routing. They skip cleanly
without one (75 pass, 81 skip):

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
