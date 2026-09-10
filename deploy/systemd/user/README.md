# The units that actually run this host

`deploy/systemd/` (one level up) describes a **root** install: `/opt/c2w`,
a dedicated `c2w` user, `/etc/c2w/master.key` via `LoadCredential=`, and
`EnvironmentFile=/etc/c2w/database.env`. Keep it for a deployment built that
way.

This directory is what is installed **here**, as `systemctl --user` units in
`~/.config/systemd/user/`, and it is not a stylistic variation. Three things in
the root units cannot work in a user unit:

* `User=` / `Group=` are meaningless -- a user unit already runs as that user,
  and systemd refuses the directives.
* `ProtectHome=yes` would hide `/home/c2w/commpeak-2-wasabi`, which is both the
  working directory and the virtualenv.
* `/etc/c2w/database.env` and `/etc/c2w/master.key` do not exist; the master key
  lives at `~/.config/c2w/master.key` and is passed as
  `C2W_MASTER_KEY_FILE`.

That mismatch is why `c2w-worker@`, `c2w-scheduler` and `c2w-reconciler` were
never installed: the templates on offer could not start, so only the API,
PostgreSQL and the tunnel were ever running. **Nothing inventoried buckets,
polled call records or copied anything, and no page said so** -- the setup
checklist showed steps that could never complete because the thing meant to
complete them did not exist.

`C2W_DATABASE_URL` and `C2W_MASTER_KEY_FILE` are the only environment values
here, because they are what a process needs *before* it can read settings from
the database. Everything else is a setting.

## Installing

```bash
cp deploy/systemd/user/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now c2w-postgres c2w-api c2w-worker@1
systemctl --user enable --now c2w-scheduler c2w-reconciler
sudo loginctl enable-linger c2w      # or none of it survives a reboot
```

More workers are more instances: `c2w-worker@2` … `c2w-worker@5`. Five are
enabled here.

**`source.concurrency_per_connection` must come down as workers go up.** It is
enforced per worker *process*, so N workers at that value put N times as many
concurrent requests on one CommPeak account, and CommPeak recommends about
five. Five workers therefore run it at `1`. Leaving it at `5` while adding
workers would put twenty-five on a single account, and the punishment is a
rate-limited 403 indistinguishable from a missing ACL entry.

`c2w-scheduler` is the unit that reaches out to CommPeak on a timer. While an
account is being refused there, running it only produces a failed scan every
interval -- and on a rate-limited refusal, repeated attempts are what caused
the refusal. Start it once an account probes green.
