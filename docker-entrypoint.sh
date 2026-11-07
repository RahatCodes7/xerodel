#!/usr/bin/env bash
set -euo pipefail

# Anything passed as a command (e.g. `docker run ... bash`) is respected.
if [ "$#" -gt 0 ]; then
  exec gosu app "$@"
fi

export PORT="${PORT:-8000}"
export PROOF_DIR="${PROOF_DIR:-/app/data/proofs}"

# A bind-mounted ./data volume is often created as root; make it writable by
# the unprivileged app user before dropping privileges.
mkdir -p "$PROOF_DIR"
chown -R app:app /app/data "$PROOF_DIR" 2>/dev/null || true

if [ -z "${DATABASE_URL:-}" ]; then
  echo "[entrypoint] DATABASE_URL is not set — configure the PostgreSQL connection string." >&2
  exit 1
fi

echo "[entrypoint] waiting for PostgreSQL..."
python scripts/wait_for_db.py

echo "[entrypoint] creating/updating database schema..."
gosu app python -c "from app.database import init_db; init_db()"

echo "[entrypoint] starting app on 0.0.0.0:${PORT}"
# Single worker on purpose: the Telegram bot runs inside this process.
exec gosu app uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --proxy-headers \
  --forwarded-allow-ips='*'
