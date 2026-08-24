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
                    line = f"{db}: {len(tables)} tables, verdicts={n} rows"
                    if "file_pages" in tables:
                        # quality coverage + mean score + per-level counts
                        avg, = c.execute(
                            "SELECT round(avg(quality_score)::numeric, 2) FROM file_pages"
                            " WHERE quality_score IS NOT NULL"
                        ).fetchone() or (None,)
                        scored, total, = c.execute(
                            "SELECT count(*) FILTER (WHERE quality_score IS NOT NULL),"
                            " count(*) FROM file_pages"
                        ).fetchone()
                        levels = dict(
                            c.execute(
                                "SELECT quality_level, count(*) FROM file_pages"
                                " WHERE quality_level IS NOT NULL"
                                " GROUP BY quality_level ORDER BY quality_level"
                            ).fetchall()
                        )
                        lv = " ".join(f"{k}:{v}" for k, v in levels.items())
                        line += (f" · quality {scored}/{total} scored"
                                 + (f", avg {avg}" if avg is not None else "")
                                 + (f" [{lv}]" if lv else ""))
                    print(line)
            except psycopg.Error as e:
                print(f"{db}: unreadable ({e})")


def sync_quality(from_db: str) -> None:
    """Copy file_pages.quality_* from `from_db` into the current MODEL_NAME's
    database, joined on (sha256, page_no). Quality (DeQA-Doc) is model-
    independent, so one run serves every model DB. Existing scores in the
    target are never overwritten (COALESCE keeps them)."""
    import json

    target = config.DB_DSN
    parts = urlsplit(target)
    if from_db == parts.path.lstrip("/"):
        raise SystemExit(f"--sync-quality: source ({from_db}) is the current database")
    source = parts._replace(path=f"/{from_db}").geturl()

    with psycopg.connect(source) as src, psycopg.connect(target) as dst:
        cur = src.cursor()
        cur.execute(
            "SELECT sha256, page_no, quality_score, quality_level,"
            " quality_probs::text, quality_model, quality_at"
            " FROM file_pages WHERE quality_score IS NOT NULL"
        )
        rows = cur.fetchall()
        with dst.cursor() as out:
            out.executemany(
                """
                UPDATE file_pages SET
                    quality_score = COALESCE(file_pages.quality_score, %s),
                    quality_level = COALESCE(file_pages.quality_level, %s),
                    quality_probs = COALESCE(file_pages.quality_probs, %s::jsonb),
                    quality_model = COALESCE(file_pages.quality_model, %s),
                    quality_at    = COALESCE(file_pages.quality_at, %s)
                WHERE sha256 = %s AND page_no = %s
                """,
                [(r[2], r[3], r[4], r[5], r[6], r[0], r[1]) for r in rows],
            )
        dst.commit()
    print(f"synced quality: {len(rows)} scored pages {from_db} -> "
          f"{parts.path.lstrip('/')} (existing target scores kept)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true", help="list per-model databases")
    ap.add_argument("--sync-quality", metavar="FROM_DB",
                    help="copy quality scores from FROM_DB into the current model's db")
    args = ap.parse_args()
    if args.status:
        status()
    elif args.sync_quality:
        sync_quality(args.sync_quality)
    else:
        ensure_database()
