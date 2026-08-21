"""Indexer: load filtered.csv + the GET-mock JSON tree and join them into Postgres.

Two sources, joined by txn_id:
  1. GET-mock JSONs (MOCK_ROOT, recursive glob GET_*.json) -> `petitions`
     (id=result.id, txn_id, document_no, state, raw_json). JSON-first so the
     real petition PK is in place before petition_files reference it.
  2. filtered.csv (project root) -> `files` (sha256=hashed_value, content-deduped)
     + `file_pages` (one per page), and `petition_files` (the many-to-many under a
     declared category). For a txn_id with no JSON, a stub `petitions` row is
     created with id=txn_id and raw_json=NULL (the CSV-only case).

The CSV is the sha256/local-path authority (no S3, no hashing); the JSON file
records are NOT used for the join — the CSV already encodes file→petition→declared
type. Idempotent: ON CONFLICT DO UPDATE/NOTHING, so re-runs are safe.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF

from . import config
from .db import connect

log = logging.getLogger("eval.index")

# Image extensions we can render/OCR. Everything else (doc/docx/xlsx/dwg/...)
# is content_kind='other' -> the worker sets ai_class_status='skipped'.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".jfif", ".heic", ".gif", ".bmp", ".webp"}
_PDF_EXTS = {".pdf"}


def _content_kind_and_pages(path: Path, ext: str) -> tuple[str, int]:
    """Return (content_kind, page_count) for a local file.

    pdf/image render to PIL pages via pdf_image_utils; 'other' (doc/xlsx/...) is
    not processable -> page_count 0. Errors degrade to ('other', 0) so the worker
    skips them rather than crashing the whole index.
    """
    try:
        if ext in _PDF_EXTS:
            with fitz.open(str(path)) as doc:
                return "pdf", doc.page_count
        if ext in _IMAGE_EXTS:
            return "image", 1
    except Exception as exc:  # corrupt pdf, truncated image, permission, ...
        log.warning("could not open %s: %s", path, exc)
    return "other", 0


def _load_petitions_from_json(conn) -> int:
    """Walk MOCK_ROOT for GET_*.json and upsert petitions. Returns count parsed."""
    root = config.MOCK_ROOT
    n = 0
    with conn.cursor() as cur:
        for path in sorted(root.rglob("GET_*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("skipping %s: %s", path, exc)
                continue
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                continue
            pid = result.get("id")
            txn_id = result.get("txn_id")
            if not pid or not txn_id:
                continue
            # petitions has TWO independent unique constraints: id (PK) and
            # txn_id. ON CONFLICT (id) alone can't catch a stale row carrying
            # the same txn_id under a different id (e.g. a CSV-only stub from
            # an earlier run that used id=txn_id), so clear that stale row
            # first in its own statement — a DML-in-CTE is NOT visible to the
            # same statement's ON CONFLICT arbiter, so it must be separate.
            # petition_files -> petitions(id) ON DELETE CASCADE re-creates its
            # rows under the authoritative id below. JSON id/txn_id are stable,
            # so on the happy path this deletes nothing.
            cur.execute(
                "DELETE FROM petitions WHERE txn_id = %s AND id IS DISTINCT FROM %s",
                (txn_id, pid),
            )
            cur.execute(
                """
                INSERT INTO petitions (id, txn_id, document_no, state, raw_json)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                    txn_id     = EXCLUDED.txn_id,
                    document_no= COALESCE(EXCLUDED.document_no, petitions.document_no),
                    state      = COALESCE(EXCLUDED.state, petitions.state),
                    raw_json   = COALESCE(EXCLUDED.raw_json, petitions.raw_json)
                """,
                (pid, txn_id, result.get("document_no"), result.get("state"),
                 json.dumps(data, ensure_ascii=False)),
            )
            n += 1
    conn.commit()
    log.info("petitions from JSON: %d", n)
    return n


def _txn_to_petition_id(conn) -> dict[str, str]:
    """Map every petitions.txn_id -> petitions.id (the join key for petition_files)."""
    with conn.cursor() as cur:
        cur.execute("SELECT txn_id, id FROM petitions WHERE txn_id IS NOT NULL")
        return {str(r[0]): str(r[1]) for r in cur.fetchall()}


def _load_files_from_csv(conn, txn_to_pid: dict[str, str]) -> tuple[int, int, int]:
    """Load filtered.csv -> files + file_pages + petition_files. Idempotent.

    Returns (files_upserted, file_pages_upserted, petition_files_upserted).
    CRLF is handled by stripping a trailing \\r from each field (the file is
    CRLF-ish on some rows); csv.reader already splits on the line terminator, so
    we only need to normalize stray \\r.
    """
    csv_path = config.CSV_PATH
    files_n = pages_n = pf_n = 0
    # declared_filetype_first: first declared category seen per sha256 (fast-path prefill).
    declared_first: dict[str, str] = {}

    with conn.cursor() as cur:
        with csv_path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                # Normalize CRLF residuals in fields.
                row = {k: (v.rstrip("\r") if isinstance(v, str) else v) for k, v in row.items()}
                sha = row["hashed_value"]
                filename = row["filename"]
                # CSV's local_file_path is a machine-specific absolute path
                # (/home/admins/...). Rebasing the filename under DOC_ROOT
                # (the configured local attachment tree) keeps loading portable
                # across machines; filenames are unique within the tree.
                local_path = str(config.DOC_ROOT / filename)
                declared = row["table_column_name"]
                txn_id = row["txn_id"]
                source_table = row["table_name"]

                path = Path(local_path)
                ext = path.suffix.lower()
                try:
                    size_bytes = path.stat().st_size
                except OSError:
                    size_bytes = 0
                content_kind, page_count = _content_kind_and_pages(path, ext)

                # First-seen declared category for this sha256 (fast-path prefill).
                # On a re-run the dict starts empty, but the ON CONFLICT clause's
                # COALESCE keeps the already-stored value, so it stays stable.
                first_declared = declared_first.setdefault(sha, declared)

                cur.execute(
                    """
                    INSERT INTO files (sha256, local_path, filename, ext, content_kind,
                                       size_bytes, page_count, declared_filetype_first)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sha256) DO UPDATE SET
                        local_path  = EXCLUDED.local_path,
                        filename    = EXCLUDED.filename,
                        ext         = COALESCE(EXCLUDED.ext, files.ext),
                        content_kind= EXCLUDED.content_kind,
                        size_bytes  = EXCLUDED.size_bytes,
                        page_count  = EXCLUDED.page_count,
                        declared_filetype_first = COALESCE(files.declared_filetype_first,
                                                           EXCLUDED.declared_filetype_first)
                    """,
                    (sha, local_path, filename, ext or None, content_kind,
                     size_bytes, page_count, first_declared),
                )
                files_n += 1

                # file_pages: one row per page (1-based). Idempotent upsert; the
                # worker only ever fills the AI columns on pending rows.
                if page_count > 0:
                    rows = [(sha, pn) for pn in range(1, page_count + 1)]
                    cur.executemany(
                        """
                        INSERT INTO file_pages (sha256, page_no)
                        VALUES (%s, %s)
                        ON CONFLICT (sha256, page_no) DO NOTHING
                        """,
                        rows,
                    )
                    pages_n += page_count

                # petition_files: this file as it appears in a petition under a
                # declared type. Petition must exist (JSON or CSV stub below).
                pid = txn_to_pid.get(txn_id)
                if pid is None:
                    # CSV-only petition: no JSON. Stub it with id=txn_id (disjoint
                    # from real result.id uuids), raw_json=NULL.
                    cur.execute(
                        """
                        INSERT INTO petitions (id, txn_id, raw_json)
                        VALUES (%s, %s, NULL)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (txn_id, txn_id),
                    )
                    pid = txn_id
                    txn_to_pid[txn_id] = pid

                cur.execute(
                    """
                    INSERT INTO petition_files (petition_id, sha256, declared_category,
                                                txn_id, source_table, source_column,
                                                source_file_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (petition_id, sha256, declared_category) DO NOTHING
                    """,
                    (pid, sha, declared, txn_id, source_table, declared, filename),
                )
                pf_n += 1

    conn.commit()
    log.info("files upserted: %d, file_pages upserted: %d, petition_files upserted: %d",
             files_n, pages_n, pf_n)
    return files_n, pages_n, pf_n


def _create_file_extracts(conn) -> int:
    """Pre-create file_extracts rows for every (sha, declared) context that has a
    production extractor and page_count>0. The worker fills the AI columns on
    these pending rows. Deduped across petitions by PK (sha256, declared_category,
    page_no). Returns the count of page-row insert attempts.

    A declared context with no extractor (the *_certifier / another_document /
    officer_document_requested / applicant_signature / factory_eia_attchment
    contexts, plus power_of_attorney_competent_authority) gets NO rows — nothing
    to extract; the review page shows the classification block only, and
    HUMAN_DONE is satisfied vacuously once AI classification settles (file_extracts
    has no pages for it -> there are no doctype/ocr page verdicts to supply).
    """
    try:
        from .ai import extract
    except Exception as exc:
        log.warning("could not import eval.ai.extract -> skipping file_extracts "
                    "pre-creation: %s", exc)
        return 0

    # Distinct (sha, declared, page_count) across all petition_files contexts.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT pf.sha256, pf.declared_category, f.page_count
            FROM petition_files pf
            JOIN files f ON f.sha256 = pf.sha256
            """
        )
        rows = cur.fetchall()

    n = 0
    n_ctx = 0
    with conn.cursor() as cur:
        for sha, declared, page_count in rows:
            if not declared or page_count <= 0:
                continue
            if extract.extract_fn_for(declared) is None:
                continue  # no extractor for this declared context
            page_rows = [(sha, declared, pn) for pn in range(1, page_count + 1)]
            cur.executemany(
                """
                INSERT INTO file_extracts (sha256, declared_category, page_no)
                VALUES (%s, %s, %s)
                ON CONFLICT (sha256, declared_category, page_no) DO NOTHING
                """,
                page_rows,
            )
            n += page_count
            n_ctx += 1
    conn.commit()
    log.info("file_extracts pre-created: %d page-row slots across %d contexts", n, n_ctx)
    return n


def run() -> dict:
    """Run the full index. Returns a small summary dict for CLI/display."""
    conn = connect()
    try:
        t0 = time.perf_counter()
        _load_petitions_from_json(conn)
        txn_to_pid = _txn_to_petition_id(conn)
        files_n, pages_n, pf_n = _load_files_from_csv(conn, txn_to_pid)
        _create_file_extracts(conn)
        summary = {
            "petitions_json": _count(conn, "petitions"),
            "files": _count(conn, "files"),
            "file_pages": _count(conn, "file_pages"),
            "file_extracts": _count(conn, "file_extracts"),
            "petition_files": _count(conn, "petition_files"),
            "elapsed_s": round(time.perf_counter() - t0, 1),
        }
        log.info("index complete: %s", summary)
        return summary
    finally:
        conn.close()


def _count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")  # table is a literal, not user input
        return cur.fetchone()[0]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Re-apply schema (idempotent) so a fresh DB is ready, then index.
    from .db import apply_schema
    conn = connect()
    try:
        apply_schema(conn)
    finally:
        conn.close()
    summary = run()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
