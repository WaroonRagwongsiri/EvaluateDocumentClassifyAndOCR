"""Document quality score worker: batch-score every unscored renderable page
with the DeQA-Doc model (via eval.quality, which owns the scorer subprocess).

A separate OS process (spawned by the HTTP server's QWORKER control, or run by
hand as `python -m eval.quality_worker`). The loop:

  1. Poll run_control.quality_want_stop -> if true, exit 0 (graceful stop).
  2. Pick the oldest unscored renderable pages (file_pages.quality_score IS
     NULL, files.content_kind <> 'other') — LIMIT QUALITY_BATCH_SIZE, no FOR
     UPDATE needed: scoring is idempotent (quality.score_pages_batch returns
     the stored row if any) and the on-demand /quality/<sha>/<page> route may
     race harmlessly.
  3. quality.score_pages_batch(pages) renders them in parallel and scores the
     batch in ONE model forward. Per-page failure budget skips permanently
     failing pages instead of looping forever.
  4. No unscored pages left -> exit 0. Restart (Start/Continue on the
     worker-log page) resumes whatever is still NULL.

The scorer subprocess serves one request at a time behind eval.quality._lock,
but each request is a batch (QUALITY_BATCH_SIZE pages per forward), so a
single worker at a large batch size is the right concurrency — don't run two
workers, raise the batch size instead.
"""
from __future__ import annotations

import logging
import sys

from . import config  # noqa: F401  (loads env for db/render paths)
from .db import connect
from . import quality

log = logging.getLogger("eval.quality_worker")

# attempts per page before giving up on it (the on-demand pill still retries)
_MAX_ATTEMPTS = 3


def _want_stop(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT quality_want_stop FROM run_control WHERE id = 1")
        row = cur.fetchone()
    return bool(row and row[0])


def _next_unscored(conn, limit: int) -> list[tuple[str, int]]:
    """Oldest unscored renderable (sha, page_no) rows (up to `limit`), or an
    empty list when done."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fp.sha256, fp.page_no
            FROM file_pages fp
            JOIN files f ON f.sha256 = fp.sha256
            WHERE fp.quality_score IS NULL
              AND fp.quality_level IS DISTINCT FROM 'error'
              AND f.content_kind <> 'other'
            ORDER BY f.first_seen_at, fp.sha256, fp.page_no
            LIMIT %s
            """,
            (limit,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _mark_failed(conn, sha256: str, page_no: int) -> None:
    """Record a terminal failure so the page isn't re-picked forever: the score
    stays NULL (so dashboards ignore it) but quality_level='error' marks the
    page as tried-and-skipped; _next_unscored filters those out."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE file_pages SET quality_level='error' "
            "WHERE sha256=%s AND page_no=%s", (sha256, page_no))
    conn.commit()
    log.warning("sha=%s page=%s failed quality scoring %sx -> skipped",
                sha256[:10], page_no, _MAX_ATTEMPTS)


def run_once() -> int:
    """Score every unscored page until none remain or quality_want_stop is set."""
    attempts: dict[tuple[str, int], int] = {}
    conn = connect()
    try:
        while True:
            if _want_stop(conn):
                log.info("quality_want_stop set -> exiting")
                return 0
            batch = _next_unscored(conn, quality.BATCH_SIZE)
            if not batch:
                log.info("no unscored pages -> done")
                return 0
            results = quality.score_pages_batch(batch)
            for page in batch:
                res = results.get(page)
                if res is not None:
                    attempts.pop(page, None)
                    log.info("sha=%s page=%s quality=%.2f %s",
                             page[0][:10], page[1], res[0], res[1])
                    continue
                n = attempts.get(page, 0) + 1
                attempts[page] = n
                if n >= _MAX_ATTEMPTS:
                    _mark_failed(conn, *page)
                else:
                    log.info("sha=%s page=%s attempt %s failed -> retrying",
                             page[0][:10], page[1], n)
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
