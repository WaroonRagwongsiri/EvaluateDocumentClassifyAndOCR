"""AI worker: claim pending files -> classify page 1 -> run the real per-filetype
production extractor for every (sha, declared) context -> write.

A separate OS process (spawned by the HTTP server via subprocess.Popen, or run
by hand as `eval-worker`). The claim loop:

  1. Claim one pending file (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1) in a txn.
  2. content_kind='other' -> mark 'skipped', commit, loop.
  3. Else render page 1, classify it -> set ai_class_status (done|none|error) +
     predicted/raw/latency/error/model/at. Commit.
  4. For every pending (sha, declared_category) context in file_extracts, run the
     real production extractor `process_<slug>_files` (one call returns all pages),
     store each page's JSON (is_X + data + ocr_text) into file_extracts. Commit
     PER CONTEXT (not per page) so a mid-store crash loses only the in-flight
     context, not the whole extractor's N LLM calls.
  5. Poll run_control.want_stop between files AND between contexts -> if true,
     commit and exit 0 (graceful stop). Continue re-spawns and resumes pending.

Crash safety: ai_class_status flips to a terminal value only on completion, so a
crash mid-file leaves it 'pending' -> re-claimed next run. Extract upserts are
idempotent (ON CONFLICT). No 'running' status is stored, so a crashed worker never
leaves stuck rows.

Async boundary: the production extractor fns are async (aiohttp LLM + httpx OCR).
Both clients build a fresh session per request (chatcompletions_service.py:203,
central_ocr_client.py:116/180 — `async with ...ClientSession`/`AsyncClient`), so a
single (llm, ocr_client) pair constructed per file is safely reused across one
`asyncio.run(...)` call per context.

Security invariant: the eval app never hits S3/network for file fetch. We read
`files.local_path` under config.DOC_ROOT ourselves and base64-encode; no file
path crosses into the extractor — it reads bytes only from
`file_info["base64"]` (image_utils:231-258).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import sys
import time
from datetime import datetime, timezone

from . import config
from .ai import classify, extract, pdf_image_utils as piu
from .db import connect

log = logging.getLogger("eval.worker")

_POLL_EVERY_CONTEXTS = 1  # check want_stop between every context (responsive stop)


def _want_stop(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT want_stop FROM run_control WHERE id = 1")
        row = cur.fetchone()
    return bool(row and row[0])


def _claim_one(conn) -> tuple[str, str, str, str, bool] | None:
    """Claim one file needing AI work. Returns (sha256, local_path, content_kind,
    filename, needs_classify) or None when nothing remains.

    A file is work-ready if EITHER it has never been classified (status='pending')
    OR it was classified but still has pending extract contexts (a crash/stop
    during extraction, or a re-run that reset only extracts). The first path
    classifies then extracts; the second skips re-classification (it's settled)
    and just finishes the pending contexts.

    FOR UPDATE SKIP LOCKED prevents double-claims across workers.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.sha256, f.local_path, f.content_kind, f.filename,
                   (f.ai_class_status = 'pending') AS needs_classify
            FROM files f
            WHERE f.ai_class_status = 'pending'
               OR (
                   f.ai_class_status IN ('done','none','error')
                   AND EXISTS (
                       SELECT 1 FROM file_extracts x
                       WHERE x.sha256 = f.sha256 AND x.ai_extract_status = 'pending'
                   )
               )
            ORDER BY f.first_seen_at
            FOR UPDATE OF f SKIP LOCKED
            LIMIT 1
            """,
        )
        row = cur.fetchone()
    return (row[0], row[1], row[2], row[3], bool(row[4])) if row else None


def _classify_file(conn, sha256: str, local_path: str, content_kind: str) -> None:
    """Classify page 1 and write the result onto the files row. Commits."""
    now = datetime.now(timezone.utc)
    if content_kind == "other":
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE files SET ai_class_status='skipped', ai_class_at=%s
                   WHERE sha256=%s""",
                (now, sha256),
            )
        conn.commit()
        log.info("sha=%s skipped (content_kind=other)", sha256[:10])
        return

    # Render page 1 at the OCR/classify resolution (150dpi).
    try:
        pages = piu.load_pages(_read_bytes(local_path), _basename(local_path))
        if not pages:
            raise RuntimeError("no pages rendered")
        img_b64 = piu.image_to_b64(pages[0])
    except Exception as exc:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE files SET ai_class_status='error', ai_class_error=%s,
                   ai_class_at=%s WHERE sha256=%s""",
                (f"render page 1 failed: {exc}", now, sha256),
            )
        conn.commit()
        log.warning("sha=%s render error: %s", sha256[:10], exc)
        return

    predicted, status, err, raw = classify.classify_page(img_b64)
    status_sql = status if status in ("done", "none", "error") else "error"
    if status == "error" and not err:
        err = "classification error"
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE files SET
                 ai_class_status=%s,
                 ai_predicted_category=%s,
                 ai_class_latency_s=%s,
                 ai_class_error=%s,
                 ai_class_raw=%s::jsonb,
                 ai_class_model=%s,
                 ai_class_at=%s
               WHERE sha256=%s""",
            (
                status_sql,
                predicted,  # None for none/error -> NULL
                round(float(raw.get("latency_s") or 0.0), 3),
                err if status == "error" else None,
                _json_dumps(raw),
                config.MODEL_NAME,
                now,
                sha256,
            ),
        )
    conn.commit()
    log.info("sha=%s classified: status=%s predicted=%s", sha256[:10], status_sql, predicted)


def _extract_contexts(
    conn, sha256: str, local_path: str, filename: str
) -> bool:
    """Run the real per-filetype extractor for every pending (sha, declared)
    context. Returns False if asked to stop.

    The extractor is keyed by the DECLARED filetype, so its output is per-(sha,
    declared, page). One extractor call per context returns all pages; we commit
    PER CONTEXT so a mid-store crash loses only the in-flight context, not the
    whole extractor's N LLM calls. Bytes are read once from local_path under
    DOC_ROOT and base64-encoded once (shared across all contexts of this file).
    """
    # Fetch the pending extractable contexts (rows exist only for contexts the
    # indexer pre-created, i.e. those with an extractor fn).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT declared_category FROM file_extracts "
            "WHERE sha256=%s AND ai_extract_status='pending'",
            (sha256,),
        )
        contexts = [r[0] for r in cur.fetchall()]
    if not contexts:
        return True  # nothing to do (no-extractor file, or re-run after all done)

    # Read + base64-encode the file bytes ONCE (shared across all contexts).
    try:
        raw_bytes = _read_bytes(local_path)
    except Exception as exc:
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE file_extracts SET ai_extract_status='error',
                   ai_extract_error=%s, ai_extract_model=%s, ai_extract_at=%s
                   WHERE sha256=%s AND ai_extract_status='pending'""",
                (f"read file failed: {exc}", config.MODEL_NAME, now, sha256),
            )
        conn.commit()
        log.warning("sha=%s read error: %s", sha256[:10], exc)
        return True
    file_b64 = base64.b64encode(raw_bytes).decode("ascii")

    # Build the production clients ONCE for this file (LLM + optional central
    # OCR). Both clients build a fresh session per request, so reusing the pair
    # across per-context asyncio.run calls is safe.
    llm, ocr_client = extract.make_clients()

    for declared in contexts:
        if _want_stop(conn):
            log.info("want_stop set -> committing and exiting")
            conn.commit()
            return False

        fn = extract.extract_fn_for(declared)
        if fn is None:
            # Safety: the indexer should not have pre-created rows for a context
            # with no extractor. Clear any stray pending rows so they don't loop.
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE file_extracts SET ai_extract_status='error',
                       ai_extract_error='no extractor fn for declared_category',
                       ai_extract_at=%s
                       WHERE sha256=%s AND declared_category=%s
                       AND ai_extract_status='pending'""",
                    (datetime.now(timezone.utc), sha256, declared),
                )
            conn.commit()
            continue

        file_type = extract.file_type_string(declared)
        file_info = {
            "filename": filename,
            "base64": file_b64,
            "fileType": file_type,
        }
        now = datetime.now(timezone.utc)

        try:
            t0 = time.monotonic()
            pages = asyncio.run(
                extract.run_extractor(fn, file_info, ocr_client, llm)
            )
            latency = round(time.monotonic() - t0, 3)
        except Exception as exc:
            # Whole context failed: mark all its pending pages 'error'.
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE file_extracts SET ai_extract_status='error',
                       ai_extract_error=%s, ai_extract_model=%s, ai_extract_at=%s
                       WHERE sha256=%s AND declared_category=%s
                       AND ai_extract_status='pending'""",
                    (f"extractor failed: {exc}", config.MODEL_NAME, now,
                     sha256, declared),
                )
            conn.commit()
            log.warning("sha=%s ctx=%s extractor error: %s",
                        sha256[:10], declared, exc)
            continue

        # Persist each page (stripping the bulky rotated_base64 from storage; the
        # review page serves render_page at 150dpi instead — known minor
        # limitation if a page was rotation-corrected).
        returned_pages: set[int] = set()
        with conn.cursor() as cur:
            for idx, page_dict in enumerate(pages):
                page_no = page_dict.get("page") or (idx + 1)
                returned_pages.add(page_no)
                page_json = {k: v for k, v in page_dict.items()
                             if k != "rotated_base64"}
                cur.execute(
                    """INSERT INTO file_extracts
                       (sha256, declared_category, page_no, ai_extract_status,
                        ai_extract_json, ai_extract_latency_s, ai_extract_raw,
                        ai_extract_model, ai_extract_at)
                       VALUES (%s, %s, %s, 'done', %s::jsonb, %s, %s::jsonb, %s, %s)
                       ON CONFLICT (sha256, declared_category, page_no) DO UPDATE SET
                         ai_extract_status='done',
                         ai_extract_json=EXCLUDED.ai_extract_json,
                         ai_extract_latency_s=EXCLUDED.ai_extract_latency_s,
                         ai_extract_raw=EXCLUDED.ai_extract_raw,
                         ai_extract_model=EXCLUDED.ai_extract_model,
                         ai_extract_at=EXCLUDED.ai_extract_at,
                         ai_extract_error=NULL""",
                    (sha256, declared, page_no, _json_dumps(page_json), latency,
                     _json_dumps(page_json), config.MODEL_NAME, now),
                )
            # Any pre-created pages the extractor did NOT return: mark error so
            # they don't stay pending forever (extractor returned fewer pages
            # than the indexer pre-created from page_count).
            cur.execute(
                """UPDATE file_extracts SET ai_extract_status='error',
                   ai_extract_error=%s, ai_extract_model=%s, ai_extract_at=%s
                   WHERE sha256=%s AND declared_category=%s
                   AND ai_extract_status='pending'""",
                (f"extractor returned {len(pages)} page(s) (fewer than pre-created)",
                 config.MODEL_NAME, now, sha256, declared),
            )
        conn.commit()
        log.info("sha=%s ctx=%s extracted: %d pages in %ss",
                 sha256[:10], declared, len(pages), latency)

    return True


def run_once() -> int:
    """Claim and process work-ready files until none remain or want_stop is set."""
    conn = connect()
    try:
        while True:
            if _want_stop(conn):
                log.info("want_stop set at loop top -> exiting")
                return 0
            claimed = _claim_one(conn)
            if claimed is None:
                log.info("no work-ready files -> done")
                return 0
            sha256, local_path, content_kind, filename, needs_classify = claimed

            # Classify only on first sight (status was 'pending'); a file reclaimed
            # for leftover extracts is already classified — skip re-classification.
            if needs_classify:
                _classify_file(conn, sha256, local_path, content_kind)

            # Run the real per-filetype extractor for every pending context.
            # content_kind='other' has no extract rows, so this is a no-op for
            # skipped files. Extraction is independent of the human verdicts and is
            # scored per-(sha,declared,page), so even error/none files still get
            # their extractable contexts extracted.
            if content_kind != "other":
                if not _extract_contexts(conn, sha256, local_path, filename):
                    return 0  # want_stop during extraction
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run_once()


# --- small helpers ------------------------------------------------------------

def _read_bytes(local_path: str) -> bytes:
    with open(local_path, "rb") as fh:
        return fh.read()


def _basename(local_path: str) -> str:
    return local_path.rsplit("/", 1)[-1]


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)


if __name__ == "__main__":
    sys.exit(main())
