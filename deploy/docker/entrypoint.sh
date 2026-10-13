#!/bin/sh
set -eu

python - <<'PY'
import asyncio, os, sys, time

import asyncpg

dsn = os.environ.get("DB_CONNECTION_STRING", "")
if not dsn.startswith(("postgres://", "postgresql://")):
    sys.exit(0)

async def ping():
    await (await asyncpg.connect(dsn, timeout=3)).close()

for attempt in range(1, 31):
    try:
        asyncio.run(ping())
        sys.exit(0)
    except Exception as exc:
        print(f"waiting for database ({attempt}/30): {exc}", flush=True)
        time.sleep(2)
sys.exit("database not reachable")
PY

if [ "${RUN_MIGRATIONS:-true}" != "false" ]; then
    aerich upgrade
fi

exec "$@"
