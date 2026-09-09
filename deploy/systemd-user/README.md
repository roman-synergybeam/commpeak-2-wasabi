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

## The Cloudflare tunnel

`c2w-tunnel.service` runs `cloudflared` so the console can be reached from
outside without opening a port. Two things about it are deliberate:

* **The token is read from the database at start and passed as
  `TUNNEL_TOKEN`.** Never as `--token`: an argument is visible to every account
  on the host in `ps`, while a process environment is readable only by its
  owner and root. It is not written to a file either — the database is where
  every other credential here lives.
* **`--no-autoupdate`.** An unattended binary replacing itself is a change
  nobody approved, on a host that publishes an admin console.

The tunnel connecting is not the same as the hostname working. A connector
token authorises the daemon to join the tunnel; it cannot create the DNS route,
which needs an origin certificate from `cloudflared login`. So the public
hostname has to be routed in the dashboard:

> Cloudflare Zero Trust → Networks → Tunnels → this tunnel → **Public
> Hostname** → add the hostname, service `http://localhost:8000`

Until that exists the hostname resolves to whatever it pointed at before, and
the tunnel sits connected with nothing routed into it. `journalctl --user -u
c2w-tunnel` shows `Registered tunnel connection` lines when the daemon is
healthy, which is the half this host controls.

The `ping_group_range` warning in its log is harmless: it only disables
cloudflared's ICMP proxy, which HTTP does not use.
