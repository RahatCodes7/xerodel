#!/usr/bin/env python3
"""Wait until PostgreSQL accepts connections (used by the container entrypoint)."""
import os
import sys
import time

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import normalize_database_url  # noqa: E402

url = os.environ.get("DATABASE_URL", "").strip()
if not url:
    sys.exit("[wait_for_db] DATABASE_URL is not set")

timeout = int(os.environ.get("DB_WAIT_SECONDS", "60"))
deadline = time.time() + timeout
attempt = 0
last_error = None

while time.time() < deadline:
    attempt += 1
    try:
        with psycopg.connect(normalize_database_url(url), connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        print(f"[wait_for_db] database ready after {attempt} attempt(s)")
        sys.exit(0)
    except Exception as exc:  # connection refused, DNS not ready, auth delay, etc.
        last_error = exc
        time.sleep(2)

print(f"[wait_for_db] database not reachable within {timeout}s: {last_error}", file=sys.stderr)
sys.exit(1)
