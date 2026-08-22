"""Document quality score worker: batch-score every unscored renderable page
with the DeQA-Doc model (via eval.quality, which owns the scorer subprocess).

A separate OS process (spawned by the HTTP server's QWORKER control, or run by
hand as `python -m eval.quality_worker`). The loop:

  1. Poll run_control.quality_want_stop -> if true, exit 0 (graceful stop).
  2. Pick the oldest unscored renderable page (file_pages.quality_score IS
     NULL, files.content_kind <> 'other') — LIMIT 1, no FOR UPDATE needed:
     scoring is idempotent (quality.score_page returns the stored row if any)
     and the on-demand /quality/<sha>/<page> route may race harmlessly.
  3. quality.score_page(sha, page_no) renders + scores + persists. Returns
     None on failure (cold model, timeout, render error); a failure budget per
     page skips permanently failing pages instead of looping forever.
  4. No unscored pages left -> exit 0. Restart (Start/Continue on the
     worker-log page) resumes whatever is still NULL.

The scorer itself is single-request pipelined (eval.quality._lock), so a
single quality worker is the right concurrency — don't run two.
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


def _next_unscored(conn) -> tuple[str, int] | None:
    """Oldest unscored renderable (sha, page_no), or None when done."""
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
            LIMIT 1
            """,
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


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
            nxt = _next_unscored(conn)
            if nxt is None:
                log.info("no unscored pages -> done")
                return 0
            sha, pno = nxt
            res = quality.score_page(sha, pno)
            if res is not None:
                attempts.pop((sha, pno), None)
                log.info("sha=%s page=%s quality=%.2f %s",
                         sha[:10], pno, res[0], res[1])
                continue
            n = attempts.get((sha, pno), 0) + 1
            attempts[(sha, pno)] = n
            if n >= _MAX_ATTEMPTS:
                _mark_failed(conn, sha, pno)
            else:
                log.info("sha=%s page=%s attempt %s failed -> retrying",
                         sha[:10], pno, n)
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
