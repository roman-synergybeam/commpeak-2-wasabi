---
name: cprec-deploy
description: Deploy or restart cprec on the target Linux VM with systemd. Use when releasing a change, changing worker counts, rotating the master key, or verifying service health after a deploy.
---

# Deploying

No containers — `systemd` units on one VM, per the customer requirement.

## Units

| Unit | Role | Scaling |
|---|---|---|
| `cprec-api` | FastAPI + HTMX UI behind nginx | 1 |
| `cprec-worker@N` | transfer/verify/sidecar jobs | template; add instances |
| `cprec-scheduler` | incremental scans, CDR polling, retention | exactly 1 |
| `cprec-reconciler` | nightly three-way diff | exactly 1 |

Scheduler and reconciler must stay singletons; two schedulers double-queue work.
Workers are the scaling knob:

```bash
sudo systemctl enable --now cprec-worker@{1..4}
sudo systemctl disable --now cprec-worker@4
```

## Release

```bash
cd /opt/cprec && sudo -u cprec git pull
sudo -u cprec /opt/cprec/.venv/bin/uv sync --no-dev
sudo -u cprec /opt/cprec/.venv/bin/alembic upgrade head   # before restarting
sudo systemctl restart cprec-api 'cprec-worker@*' cprec-scheduler cprec-reconciler
```

Migrate before restarting: workers running old code against a new schema is
survivable, new code against an old schema is not.

## Verify — do not assume

```bash
systemctl status cprec-api 'cprec-worker@*' cprec-scheduler cprec-reconciler
curl -fsS localhost:8000/api/health && echo
curl -fsS localhost:8000/api/ready  && echo
journalctl -u cprec-api --since '2 minutes ago' -p warning
```

Then confirm work is actually moving, not just that processes are up:

```bash
cprec-admin doctor
```

```sql
SELECT state, count(*) FROM transfer_jobs GROUP BY state;
```

## Configuration and secrets

There are no application config files. Every setting lives in the database and
is changed in the UI under Settings, or with `cprec-admin settings set` — it
applies across every process within seconds, no restart needed (except the few
marked `restart required`).

Only two values come from the environment, because they are what a process needs
before it can read settings: `CPREC_DATABASE_URL` (in the unit, with the
password in `/etc/cprec/database.env`, `0600`) and the master key, delivered by
systemd `LoadCredential=` from `/etc/cprec/master.key` — never in a unit file,
an environment line, or the repo.

Rotating the master key re-wraps each brand's data key, not every sealed value:

```bash
sudo -u cprec cprec-admin keys rotate-master --new-key-file /etc/cprec/master.key.new
```

Back up `/etc/cprec/master.key` somewhere the database backup is not. Losing it
makes every stored connection credential unrecoverable — the recordings survive,
but every connection must be re-entered.

## Backups

`pg_dump` alone is not a restore plan at this size. Verify a restore into a
scratch database before you rely on it, and confirm the master key is
recoverable independently.
