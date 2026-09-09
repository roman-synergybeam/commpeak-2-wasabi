---
name: c2w-deploy
description: Deploy or restart c2w on the target Linux VM with systemd. Use when releasing a change, changing worker counts, rotating the master key, or verifying service health after a deploy.
---

# Deploying

No containers — `systemd` units on one VM, per the customer requirement.

## Units

| Unit | Role | Scaling |
|---|---|---|
| `c2w-api` | FastAPI + HTMX UI behind nginx | 1 |
| `c2w-worker@N` | transfer/verify/sidecar jobs | template; add instances |
| `c2w-scheduler` | incremental scans, CDR polling, retention | exactly 1 |
| `c2w-reconciler` | nightly three-way diff | exactly 1 |

Scheduler and reconciler must stay singletons; two schedulers double-queue work.
Workers are the scaling knob:

```bash
sudo systemctl enable --now c2w-worker@{1..4}
sudo systemctl disable --now c2w-worker@4
```

## Release

```bash
cd /opt/c2w && sudo -u c2w git pull
sudo -u c2w /opt/c2w/.venv/bin/uv sync --no-dev
sudo -u c2w /opt/c2w/.venv/bin/alembic upgrade head   # before restarting
sudo systemctl restart c2w-api 'c2w-worker@*' c2w-scheduler c2w-reconciler
```

Migrate before restarting: workers running old code against a new schema is
survivable, new code against an old schema is not.

## Verify — do not assume

```bash
systemctl status c2w-api 'c2w-worker@*' c2w-scheduler c2w-reconciler
curl -fsS localhost:8000/api/health && echo
curl -fsS localhost:8000/api/ready  && echo
journalctl -u c2w-api --since '2 minutes ago' -p warning
```

Then confirm work is actually moving, not just that processes are up:

```bash
c2w-admin doctor
```

```sql
SELECT state, count(*) FROM transfer_jobs GROUP BY state;
```

## Configuration and secrets

There are no application config files. Everything is configured in the console:
97 settings under **Settings**, and the CommPeak accounts and archive buckets on
their own pages, credentials included. `c2w-admin settings set` does the same
for scripting. A change applies across every process within seconds without a
restart, except the few marked `restart required`.

Only two values come from the environment, because they are what a process needs
before it can read settings: `C2W_DATABASE_URL` (in the unit, with the
password in `/etc/c2w/database.env`, `0600`) and the master key, delivered by
systemd `LoadCredential=` from `/etc/c2w/master.key` — never in a unit file,
an environment line, or the repo.

Rotating the master key re-wraps each brand's data key, not every sealed value:

```bash
sudo -u c2w c2w-admin keys rotate-master --new-key-file /etc/c2w/master.key.new
```

Back up `/etc/c2w/master.key` somewhere the database backup is not. Losing it
makes every stored connection credential unrecoverable — the recordings survive,
but every connection must be re-entered.

## Backups

`pg_dump` alone is not a restore plan at this size. Verify a restore into a
scratch database before you rely on it, and confirm the master key is
recoverable independently.
