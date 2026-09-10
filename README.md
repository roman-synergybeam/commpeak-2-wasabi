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

There are no configuration files. Every setting lives in the database (112 of
them, 17 categories) and is edited in the web UI under **Settings** or with
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
| Inventory scanner | **being corrected** — resumable and idempotent, but keyed to the wrong prefix shape. See *The object layout* below |
| Job queue | done — PostgreSQL `SKIP LOCKED`, leases, classified retry ladder |
| Transfer + verification | done — stream, verify, sidecar metadata, re-queue on archive loss |
| Workers | done — worker pool, scheduler, nightly reconciler (both singleton-locked) |
| Media delivery | done — presigned URLs, separate play/download permissions, full audit |
| CDR API client | done — against the documented PBX Stats API: form-encoded POST, `page`/`cdrs_per_page`, `from`/`till`, key in `X-API-KEY`. Accepts both CDR shapes CommPeak returns |
| Text messages (SMS) | done — both TextPeak endpoints (sent and received), delivery status, late-receipt handling, search, export |
| Two-factor | done — authenticator app (RFC 6238), single-use codes, recovery codes, forced enrolment, administrator reset |
| People administration | done — create accounts from the console, enable/disable, only a platform admin can create another |
| Active Directory picker | done — search people, groups and OUs from the users page, with bounded timeouts and a message naming what is misconfigured. Reads only |
| Active Directory sign-in | done — bind-as authentication, group→role on every sign-in, empty passwords refused before the server is contacted |
| Turnstile at sign-in | done — enforced, exempt on the LAN, and fails *open* on a Cloudflare outage so a third party cannot lock you out |
| Text-message polling | done — both TextPeak endpoints on a schedule, per organisation, cursor advanced only after the rows are stored |
| Cloudflare tunnel | done — `cloudflared` as a user service, token read from the database and passed by environment so it never reaches argv |
| Microsoft / Google sign-in | done — authorization code with PKCE; state, nonce, signature, issuer, audience, expiry and the domain allow list all verified |
| Transcription | done — Whisper on this server via faster-whisper, digit redaction, per-organisation settings, on a schedule |
| Web UI | done — built on the Console UI Kit design system; dashboard, call search, detail + player, messages, sync status, settings, audit, people, account |
| Alerts | done — Telegram + Slack, per-brand, severity routing, deduplication |
| Deployment | done — systemd units, nginx, idempotent installer |

**Not yet built.** The settings and the schema are in place for each of these,
so nothing has to be migrated when the work happens — but no code runs yet, and
each setting says so where it could be mistaken for working:

- **Voice analysis** beyond transcription — sentiment and keyword scoring.
  The `transcripts` columns for it exist and are not populated. Transcription
  itself runs.
- **Speaker separation.** `transcribe.diarize` is offered and does nothing yet:
  faster-whisper does not diarise, and doing it properly means a second model.
  Transcripts are stored with segments and timings but a single speaker.
- **The four remote recognisers** in the engine menu — OpenAI, Azure, Google,
  AWS. Choosing one says so plainly rather than failing quietly. Whisper on
  this server is the default because the alternative is posting recorded
  customer calls to a third party, which is a decision to make on purpose.
- Microsoft 365 / Google Drive export, FLAC→MP3 transcoding for older browsers,
  and a `connection test` CLI subcommand.

**Still out of scope:** FXRide CRM and Zendesk.

## The object layout

Measured against all eight live buckets, not taken from the documentation:

```
recordings/2022/12/26/in-99150321131757-5031470050247407092-20221226-152523-1672068323.91098.flac
└────────┘ └──┘ └┘ └┘ └┘ └────────────┘ └─────────────────┘ └──────┘ └────┘ └────────┘ └───┘
   root    year mo dy dir   number            extension        date    time   channel-id  seq
```

Two differences from the documented shape, and both matter:

* **There is a `recordings/` root prefix.** The docs show keys starting at the
  year.
* **There is no hour level.** Files sit directly under the day. The docs show
  `/{year}/{month}/{day}/{hour}/`.

`hour_prefix()` builds `2025/11/11/02/`, which matches nothing in any of these
buckets, so a scan lists empty prefix after empty prefix and reports **zero
recordings found with no error** — the worst kind of failure, because access
looks fine and nothing appears. Correcting this is the open work in the status
table above.

Also found while measuring, and worth knowing before trusting a parse:

* `go4rex.pbx` has a non-date branch, `recordings/default/997/tmp/`. Year
  enumeration has to skip it rather than fail on it.
* The **extension** field is not always a short extension — one real key
  carries a 19-digit identifier there.
* The **sequence** is not always `0`/`1`/`2`; `91098` occurs.
* History is deeper than assumed. Per account, the years actually present are:

  | Account | Years held |
  |---|---|
  | go4rex.pbx | 2022–2026 (+ `default/`) |
  | go4rex.td | 2022–2026 |
  | go4rexsv.pbx | 2025–2026 |
  | go4rexnew.td | 2026 |
  | verificationgo4rex.td | 2026 |
  | intermagnum.pbx | 2025–2026 |
  | intermagnumretention.pbx | 2023–2026 |
  | intermagnum.td | 2026 |

## The access incident of 9 September 2026

All eight accounts returned nginx `403 Forbidden` for about four hours. It is
written up here because the diagnosis was wrong twice before it was right, and
both wrong turns are easy to repeat.

**What it was not.** Not the credentials: one account completed a full signed,
authenticated bucket listing at 19:51 BST with the same sealed token and secret
still in use. Not the per-account IP ACL either — the decisive test was setting
one account's ACL to `0.0.0.0/0`, *allow every address on the internet*, and
still being refused, sixteen times over twenty minutes. If allow-all does not
admit you, no narrower entry can, so adding address ranges was provably not the
lever. Not our egress address: one interface, one gateway, no proxy, and seven
independent echo services agreeing on `145.239.102.215`.

**What it almost certainly was.** A rate limit or automatic ban at CommPeak,
above the per-account ACL and invisible from this side. The timing fits: about
twenty account tests were run between 19:20 and 19:51 BST while the integration
was being set up; access failed from 19:52; the last confirmed failure was
22:55 UTC and the first recovery 23:27 UTC — roughly four hours after the
burst. Nothing on our side changed in between. **This has not been confirmed by
CommPeak**, and the only place that could confirm it is the account's *Access
Summary* tab, which logs the source IP, time and error of every refusal.

**Why it was hard to see.** CommPeak's nginx answers a refused request with its
own HTML 403 and no S3 XML, and it answers *identically* whether the address is
missing from the ACL or blocked for some other reason. So the response cannot
distinguish the two, and the hint that used to say "your IP is probably missing
from the Access Control List" sent an operator round the portal re-checking
lists that were already correct. It now names both causes and points at the
Access Summary tab as the only thing that can settle it.

**One trap worth stating plainly:** an *unsigned* request to the recordings host
returns nginx 403 whether or not access works — it still does today, with
everything working. An unsigned request carries no account, so there is no ACL
to consult and it can never succeed. It is not a reachability test. Only a
signed request tells you anything.

**What changed as a result.**

* `test_connection` enforces a cooldown (`PROBE_COOLDOWN_SECONDS`) using the
  row's own `last_probe_at`, so it holds across processes and restarts. A burst
  of tests can no longer manufacture the failure it is trying to diagnose.
* Inventory no longer scans an account that is in `ERROR`. Eight refused
  accounts on a five-minute timer produced about ninety-six failed requests an
  hour, indefinitely — which, against a source that rate-limits, is not a retry
  policy but a way of keeping a block alive.
* Those accounts are retried by a watch instead: one account per turn, least
  recently checked first, a single cheap listing, and an alert **on the
  transition** rather than on the state. It detected all eight recoveries on
  its own and sent eight Telegram messages, which is how the outage ended
  without anybody sitting on the settings page pressing Test.
* `status` on `commpeak_connections` is the last probe's verdict, not a live
  reading. Read `last_probe_at` beside it; a row can say `OK` long after access
  stopped, and did.

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

Transfers stay disabled until an archive destination exists. Both
organisations now have one — Wasabi, `eu-central-1`, both verified by writing
an object, reading it back and removing it — and every CommPeak account is
linked to the bucket belonging to **its own** organisation, enforced by a
brand-matched join rather than a hand-entered id.

Transfers are switched on and deliberately throttled: 4 concurrent globally,
2 per bucket, 2 per organisation, a 25 Mbps ceiling, 2 jobs claimed at a time.
That is well inside CommPeak's documented ~5 per account, and every figure is a
setting to raise from the UI once it has been proven steady. Inventory,
correlation and CDR search all work without an archive anyway, so the UI is
useful before any bytes move and nothing needs re-scanning later.

## Development

```bash
uv sync --extra dev
uv run pytest -q                     # 313 tests
uv run ruff check src/ tests/
uv run uvicorn c2w.api.app:app --reload
```

Most tests need PostgreSQL, because what they test *is* database behaviour —
RLS policies, `SKIP LOCKED` claims, partition routing. They skip cleanly
without one (193 pass, 120 skip):

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

**Back up as a superuser, not as the `c2w` role.** Every brand-scoped table has
`FORCE ROW LEVEL SECURITY`, which applies to the table's owner as well — so
`pg_dump` running as `c2w` fails on `audit_events` and produces a *partial*
dump. Use `postgres`, or a role with `BYPASSRLS`.

### Running without root

The production path above needs root. On a host where that is not available
yet, the same thing runs entirely out of `$HOME` — this is what is deployed
today:

| | Path |
|---|---|
| PostgreSQL binaries | `/home/c2w/pgsql-venv` (`pgserver`, PostgreSQL 16.2) |
| Data directory | `/home/c2w/pgdata`, listening on `127.0.0.1:5432` |
| Socket, logs | `/home/c2w/pgrun`, `/home/c2w/pglog` |
| Master key | `/home/c2w/.config/c2w/master.key` (0600) |
| Services | `systemctl --user` units in `~/.config/systemd/user` |
| Backups | `/home/c2w/backups` |
| Survives a reboot | yes — `loginctl enable-linger c2w` is enabled |

Six units run: `c2w-postgres`, `c2w-api`, `c2w-worker@1`, `c2w-scheduler`,
`c2w-reconciler` and `c2w-tunnel`. The three worker units live in
`deploy/systemd/user/` and are **not** the ones in `deploy/systemd/` — those
describe a root install under `/opt/c2w` and cannot start here at all, which is
why they were never installed and why nothing inventoried, polled or copied
anything for a while. See that directory's README.

```bash
systemctl --user status 'c2w-*'
systemctl --user restart c2w-api
journalctl --user -u c2w-scheduler -n 50
```

The workers additionally need `C2W_PLATFORM_DATABASE_URL` pointing at the
`c2w_platform` role, which holds `BYPASSRLS`. Without it they fall back to the
ordinary role, whose RLS is forced with no brand set, and every tenant-scoped
table reads as **empty** — the scheduler, worker and reconciler then do nothing
at all and report nothing. That role also needs table privileges; `BYPASSRLS`
alone is not access.

Two things this arrangement still needs, and neither can be done without root:

1. **TLS.** The API currently binds `0.0.0.0:8000` directly because there is no
   reverse proxy, so sign-ins and recordings cross the network in clear. Put
   nginx (`deploy/nginx/`) in front, then change the unit back to
   `--host 127.0.0.1`.
2. **`pg_trgm`.** Searching for *part* of a phone number works but scans
   instead of using an index. `apt-get install postgresql-contrib`, then
   `CREATE EXTENSION pg_trgm;` and the two GIN indexes named in migration 0001.

**Never put the data directory under `/tmp`.** It was there, and `/tmp` on this
host is tmpfs — the entire database was in RAM and did not survive a reboot.

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
