# Running c2w without root

These are the units actually running the console today, kept here so the
deployment is reproducible rather than living only in one account's home
directory.

They are the fallback for a host where `deploy/install.sh` cannot be run: they
put PostgreSQL and the API entirely under `$HOME`, with no privileged path and
no `.env` file.

```bash
cp deploy/systemd-user/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now c2w-postgres c2w-api
```

Two details that cost time to find:

* **`Type=exec`, not `Type=notify`.** The `pgserver` build of PostgreSQL is not
  compiled with systemd support, so it never sends a readiness notification and
  `Type=notify` just times out. `ExecStartPost` polls `pg_isready` instead,
  which is what dependents actually need to know.
* **`sudo loginctl enable-linger c2w` is still required.** Without it these
  stop when the account's last session ends and do not start at boot. It is the
  one privileged command this arrangement cannot avoid.

The API binds `0.0.0.0` here because nothing terminates TLS in front of it. That
is a stopgap: the console carries passwords and call recordings, so put
`deploy/nginx/` in front and set `--host 127.0.0.1` as soon as there is a
certificate.
