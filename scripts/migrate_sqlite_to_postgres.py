#!/usr/bin/env python3
"""
Copy data from the legacy SQLite database into PostgreSQL.

Usage:
    DATABASE_URL=postgresql://user:pass@host:5432/db \
        python scripts/migrate_sqlite_to_postgres.py --sqlite data/app.db --replace

The target database must exist (the script creates the tables if needed).
Without --replace the script refuses to run when the target already has data.

Proof images are NOT copied by this script; copy the `data/proofs` directory
to the same PROOF_DIR path on the server.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

TABLES = [
    "users",
    "products",
    "inventory",
    "orders",
    "order_items",
    "order_inventory",
    "payment_proofs",
    "settings",
    "admin_actions",
]

SEQUENCED_TABLES = [
    "products",
    "inventory",
    "orders",
    "order_items",
    "payment_proofs",
    "admin_actions",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Migrate SQLite data into PostgreSQL")
    parser.add_argument("--sqlite", default=settings.DATABASE_PATH, help="Path to the old SQLite file")
    parser.add_argument("--database-url", default=settings.DATABASE_URL, help="PostgreSQL connection string")
    parser.add_argument("--replace", action="store_true", help="Delete existing target rows first")
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.database_url:
        sys.exit("DATABASE_URL (or --database-url) is required.")
    if not Path(args.sqlite).exists():
        sys.exit(f"SQLite database not found: {args.sqlite}")

    settings.DATABASE_URL = args.database_url
    settings.SEED_DEMO = False

    from app.database import init_db, normalize_database_url  # imported after settings are set

    init_db()

    import psycopg

    source = sqlite3.connect(args.sqlite)
    source.row_factory = sqlite3.Row
    target = psycopg.connect(normalize_database_url(args.database_url))

    existing = 0
    for table in TABLES:
        existing += target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if existing and not args.replace:
        sys.exit(
            "Target database already contains data. Re-run with --replace to wipe it first."
        )

    pg_columns = {}
    for table in TABLES:
        rows = target.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        ).fetchall()
        pg_columns[table] = {r[0] for r in rows}

    # Close the implicit transaction opened by the count/column checks so the
    # migration transaction below is a real top-level transaction.
    target.commit()

    with target.transaction():
        if args.replace:
            target.execute(
                "TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"
            )

        copied = {}
        for table in TABLES:
            sqlite_cols = [r["name"] for r in source.execute(f"PRAGMA table_info({table})")]
            cols = [c for c in sqlite_cols if c in pg_columns[table]]
            rows = source.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
            copied[table] = len(rows)
            if not rows:
                continue
            placeholders = ", ".join(["%s"] * len(cols))
            # Settings must overwrite the environment-derived defaults created by
            # init_db(); other tables are only ever migrated into an empty target.
            conflict = (
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
                if table == "settings"
                else "ON CONFLICT DO NOTHING"
            )
            with target.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) {conflict}",
                    [tuple(r) for r in rows],
                )

        for table in SEQUENCED_TABLES:
            target.execute(
                f"""SELECT setval(
                        pg_get_serial_sequence('{table}', 'id'),
                        COALESCE((SELECT MAX(id) FROM {table}), 1),
                        (SELECT COUNT(*) FROM {table}) > 0
                    )"""
            )

    source.close()
    target.close()

    print("Migration complete:")
    for table in TABLES:
        print(f"  {table}: {copied.get(table, 0)} rows")
    print("\nNow copy data/proofs to the server's PROOF_DIR and start the app.")


if __name__ == "__main__":
    main()
