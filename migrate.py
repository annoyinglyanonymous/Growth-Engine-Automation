"""Apply numbered SQL migrations to Supabase, once each, in order.

    python migrate.py --dry-run     # show what would run, connect to nothing
    python migrate.py --status      # what is applied, what is pending
    python migrate.py               # apply pending migrations

Tracks applied files in public.schema_migrations with a checksum, so editing an
already-applied migration is reported rather than silently ignored.

Each .sql file carries its own BEGIN/COMMIT, so this runs with autocommit on
and lets the file govern its own transaction. That way the same file applies
identically here, through `supabase db push`, or pasted into the dashboard SQL
editor -- none of those three paths is privileged.

Requires: psycopg[binary]   (pip install "psycopg[binary]")
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

TRACKING_TABLE_DDL = """
create table if not exists public.schema_migrations (
    filename   text primary key,
    checksum   text        not null,
    applied_at timestamptz not null default now()
)
"""


def load_database_url() -> str:
    """Read DATABASE_URL from the environment, falling back to .env."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    env_path = PROJECT_ROOT / ".env"
    if env_path.is_file():
        try:
            from dotenv import dotenv_values
        except ImportError:
            sys.exit("python-dotenv not available and DATABASE_URL is not set")
        url = (dotenv_values(env_path) or {}).get("DATABASE_URL")

    if not url:
        sys.exit("DATABASE_URL not found in the environment or .env")
    if "sslmode=" not in url:
        print("  warning: DATABASE_URL has no sslmode=require", file=sys.stderr)
    return url


def discover() -> list[tuple[str, str, str]]:
    """-> [(filename, sql, checksum)] sorted by filename."""
    if not MIGRATIONS_DIR.is_dir():
        sys.exit(f"no migrations directory at {MIGRATIONS_DIR}")

    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        out.append((path.name, sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()))
    return out


def connect(url: str):
    try:
        import psycopg
    except ImportError:
        sys.exit('psycopg is not installed. pip install "psycopg[binary]"')
    # autocommit: each .sql file manages its own transaction
    return psycopg.connect(url, autocommit=True)


def applied_state(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(TRACKING_TABLE_DDL)
        cur.execute("select filename, checksum from public.schema_migrations")
        return dict(cur.fetchall())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list migrations without connecting")
    parser.add_argument("--status", action="store_true",
                        help="show applied vs pending, apply nothing")
    args = parser.parse_args()

    migrations = discover()
    if not migrations:
        print(f"no .sql files in {MIGRATIONS_DIR}")
        return 0

    if args.dry_run:
        print(f"{len(migrations)} migration(s) in {MIGRATIONS_DIR}:")
        for name, sql, checksum in migrations:
            # update/delete/do belong here too. Without them a migration that
            # only changes DATA -- 023's parking, 024's themes -- reports
            # "~0 statements", which reads as "this file does nothing" to
            # exactly the person running --dry-run to find out what it does.
            statements = sum(1 for line in sql.splitlines()
                             if line.strip().lower().startswith(
                                 ("create", "alter", "insert", "drop",
                                  "comment", "update", "delete", "do ",
                                  "grant", "revoke", "truncate")))
            print(f"  {name:<28} {len(sql):>6} bytes  ~{statements} statements  "
                  f"{checksum[:12]}")
        print("\n(dry run -- nothing connected, nothing applied)")
        return 0

    conn = connect(load_database_url())
    try:
        already = applied_state(conn)

        pending, changed = [], []
        for name, sql, checksum in migrations:
            if name not in already:
                pending.append((name, sql, checksum))
            elif already[name] != checksum:
                changed.append(name)

        for name in changed:
            print(f"  CHANGED SINCE APPLIED: {name} -- edit a new migration "
                  f"instead of an applied one", file=sys.stderr)

        if args.status:
            print(f"applied: {len(already)}   pending: {len(pending)}")
            for name in sorted(already):
                print(f"  [x] {name}")
            for name, _, _ in pending:
                print(f"  [ ] {name}")
            return 1 if changed else 0

        if not pending:
            print("nothing to apply -- database is up to date")
            return 1 if changed else 0

        for name, sql, checksum in pending:
            print(f"applying {name} ...", end=" ", flush=True)
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "insert into public.schema_migrations (filename, checksum) "
                    "values (%s, %s)",
                    (name, checksum),
                )
            print("ok")

        print(f"\napplied {len(pending)} migration(s)")
        return 1 if changed else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
