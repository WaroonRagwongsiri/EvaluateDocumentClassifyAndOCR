"""Switch the eval model: ensure the per-model Postgres database exists and
has the schema applied. Run after editing MODEL_NAME in .env:

    uv run switch_model.py          # create + schema for current MODEL_NAME
    uv run switch_model.py --status # list per-model databases and row counts

CREATE DATABASE cannot run inside a transaction, so we connect to the
maintenance database (postgres) with autocommit first, then connect to the
new database to apply schema.sql (idempotent via eval.db.apply_schema).
"""
import argparse
import sys
from urllib.parse import urlsplit

import psycopg

from eval import config
from eval.db import apply_schema


def _maintenance_dsn(dsn: str) -> str:
    """Same server, maintenance db ('postgres') — CREATE DATABASE goes there."""
    parts = urlsplit(dsn)
    return parts._replace(path="/postgres").geturl()


def ensure_database() -> None:
    db_name = urlsplit(config.DB_DSN).path.lstrip("/")
    with psycopg.connect(_maintenance_dsn(config.DB_DSN), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"created database {db_name}")
        else:
            print(f"database {db_name} already exists")

    with psycopg.connect(config.DB_DSN) as conn:
        apply_schema(conn)
    print(f"schema applied to {db_name}")


def status() -> None:
    """List evalutea_* databases on this server with a per-db row count of
    classification results (0 = schema-only / not indexed)."""
    dsn = _maintenance_dsn(config.DB_DSN)
    with psycopg.connect(dsn) as conn:
        dbs = [
            r[0]
            for r in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'evalutea_%' ORDER BY 1"
            )
        ]
        for db in dbs:
            parts = urlsplit(config.DB_DSN)
            target = parts._replace(path=f"/{db}").geturl()
            try:
                with psycopg.connect(target) as c:
                    tables = [
                        r[0]
                        for r in c.execute(
                            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"
                        )
                    ]
                    n = (
                        c.execute("SELECT count(*) FROM verdicts").fetchone()[0]
                        if "verdicts" in tables
                        else 0
                    )
                    print(f"{db}: {len(tables)} tables, verdicts={n} rows")
            except psycopg.Error as e:
                print(f"{db}: unreadable ({e})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true", help="list per-model databases")
    args = ap.parse_args()
    if args.status:
        status()
    else:
        ensure_database()
