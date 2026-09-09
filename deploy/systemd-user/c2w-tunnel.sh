#!/usr/bin/env bash
#
# Start the Cloudflare tunnel using the token held in the database.
#
# The token is fetched at start and passed as TUNNEL_TOKEN, never as an
# argument: `cloudflared --token <t>` puts a credential in argv, where every
# other account on the host can read it out of `ps`. The environment of a
# process is only readable by its owner and root.
#
# It is not written to a file either. The database is where every other
# credential in this system lives, and a second copy on disk is a second thing
# to leak and to keep in step.
set -euo pipefail

REPO=/home/c2w/commpeak-2-wasabi
export C2W_DATABASE_URL="${C2W_DATABASE_URL:-postgresql+asyncpg://c2w@127.0.0.1:5432/c2w}"
export C2W_MASTER_KEY_FILE="${C2W_MASTER_KEY_FILE:-/home/c2w/.config/c2w/master.key}"

read_setting() {
    "$REPO/.venv/bin/python" - "$1" <<'PY'
import asyncio, sys
from c2w.db.session import get_sessionmaker
from c2w.settings import settings_service

async def main() -> None:
    key = sys.argv[1]
    async with get_sessionmaker()() as session:
        if key.endswith("token"):
            print(await settings_service.get_secret(session, key), end="")
        else:
            print(await settings_service.get_str(session, key), end="")

asyncio.run(main())
PY
}

cd "$REPO"
if [ "$("$REPO/.venv/bin/python" -c '
import asyncio
from c2w.db.session import get_sessionmaker
from c2w.settings import settings_service
async def m():
    async with get_sessionmaker()() as s:
        print("yes" if await settings_service.get_bool(s, "tunnel.enabled") else "no", end="")
asyncio.run(m())')" != "yes" ]; then
    echo "tunnel.enabled is off in Settings; not starting." >&2
    exit 0
fi

TUNNEL_TOKEN="$(read_setting tunnel.token)"
if [ -z "$TUNNEL_TOKEN" ]; then
    echo "No tunnel token is stored in Settings; not starting." >&2
    exit 1
fi
export TUNNEL_TOKEN

# --no-autoupdate: an unattended binary replacing itself is a change nobody
# approved, on a host that publishes an admin console.
exec /home/c2w/bin/cloudflared tunnel --no-autoupdate --loglevel info run
