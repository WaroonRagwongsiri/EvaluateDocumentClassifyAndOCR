"""psycopg connection helper + idempotent schema apply."""
from pathlib import Path

import psycopg

from . import config

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect() -> "psycopg.Connection":
    """Open a psycopg connection from DB_DSN. Caller owns the lifetime."""
    return psycopg.connect(config.DB_DSN, autocommit=False)


def apply_schema(conn: "psycopg.Connection") -> None:
    """Run schema.sql idempotently. CREATE TYPE is NOT IF NOT EXISTS in postgres,
    so we guard enum creation with DO blocks; everything else uses IF NOT EXISTS."""
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")

    # The two CREATE TYPE statements are the only non-idempotent parts of
    # schema.sql. Replace them with DO-block guards so re-running apply_schema
    # (e.g. after adding a column) doesn't crash on the enums already existing.
    guarded = sql
    for enum_name in ("classifiable_category_t", "declared_category_t"):
        # The enum literal block for each type spans from "CREATE TYPE <name> AS ENUM ("
        # to its closing ");". Re-create that substring as a DO block.
        marker = f"CREATE TYPE {enum_name} AS ENUM ("
        start = guarded.index(marker)
        end = guarded.index(");", start) + len(");")
        block = guarded[start:end]
        guard = (
            "DO $$ BEGIN\n"
            f"  {block}\n"
            f"EXCEPTION WHEN duplicate_object THEN NULL;\n"
            f"END $$;"
        )
        guarded = guarded[:start] + guard + guarded[end:]

    with conn.cursor() as cur:
        cur.execute(guarded)
    conn.commit()
