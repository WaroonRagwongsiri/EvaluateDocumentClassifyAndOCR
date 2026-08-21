"""Client-side wrapper for the DeQA-Doc quality-scorer service.

The scorer is a persistent subprocess (QualityScore/scorer_service.py) holding
the fine-tuned mPLUG-Owl2 + LoRA on GPU 4 (the only free GPU — the process is
launched with CUDA_VISIBLE_DEVICES=4). This module owns its lifecycle from the
eval server: lazy start, one JSONL request per page, in-flight lock (the
service is single-request pipelined — simplest correct protocol).

score_page(sha, page_no) -> dict | None:
    Renders the page PNG (reusing eval.render.render_page's disk cache),
    asks the scorer, upserts into file_pages.quality_*, and returns the row.
    Returns None on any failure (cold model, timeout, render error) — callers
    render a neutral pill and the next page view retries.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from . import config
from .db import connect

log = logging.getLogger("eval.quality")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCORER_SCRIPT = _PROJECT_ROOT / "scorer_service.py"
_SCORER_PY = _PROJECT_ROOT / "QualityScore" / ".venv" / "bin" / "python"
_MODEL_DIR = (_PROJECT_ROOT / "QualityScore" / "models" / "models" /
              "mapo80--DeQA-Doc-Overall")

# How long a page request may block the HTTP handler. The model takes ~1-2 min
# to cold-load; a first-touch score must NOT hang the page render, so requests
# time out and the pill stays "not yet" until the model is warm.
REQUEST_TIMEOUT_S = float(os.environ.get("QUALITY_TIMEOUT_S", "60"))
# Cold-load budget for the first _ensure_started (weights download from disk +
# LoRA merge measured ~30s; generous margin for cache-cold starts).
MODEL_LOAD_TIMEOUT_S = float(os.environ.get("QUALITY_LOAD_TIMEOUT_S", "180"))

_lock = threading.Lock()
_proc: subprocess.Popen | None = None


def _ensure_started() -> bool:
    """Start the scorer subprocess if it isn't running. Returns True when a
    request can be attempted (process alive AND ready line seen)."""
    global _proc
    if _proc is not None and _proc.poll() is None:
        return True
    if not (_SCORER_PY.exists() and _MODEL_DIR.exists()):
        log.warning("quality scorer unavailable: %s or %s missing", _SCORER_PY, _MODEL_DIR)
        return False
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="4",
               PYTHONPATH=str(_PROJECT_ROOT / "QualityScore" / "DeQA-Score"))
    _proc = subprocess.Popen(
        [str(_SCORER_PY), str(_SCORER_SCRIPT), "--model", str(_MODEL_DIR)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1, env=env, cwd=_PROJECT_ROOT)
    # consume the {"ready": true} first line — bounded by the cold-load budget
    # (~30s model load + LoRA merge) so a broken model can't hang a request
    deadline = time.monotonic() + MODEL_LOAD_TIMEOUT_S
    import threading as _t
    line_box: dict = {}

    def _read_ready():
        line_box["line"] = _proc.stdout.readline()

    reader = _t.Thread(target=_read_ready, daemon=True)
    reader.start()
    reader.join(MODEL_LOAD_TIMEOUT_S)
    if reader.is_alive() or _proc.poll() is not None or not line_box.get("line"):
        log.warning("quality scorer failed to signal ready within %ss (rc=%s)",
                    MODEL_LOAD_TIMEOUT_S, _proc.poll())
        return False
    try:
        ready = json.loads(line_box["line"])
        if not ready.get("ready"):
            log.warning("quality scorer ready line unexpected: %r", line_box["line"])
            return False
    except json.JSONDecodeError:
        log.warning("quality scorer ready line unparseable: %r", line_box["line"])
        return False
    log.info("quality scorer ready in %ss (load %ss)",
             ready.get("load_s", "?"), ready.get("load_s", "?"))
    return True


def _ask(image_path: str, timeout: float) -> dict | None:
    """Send one JSONL request, read one JSONL response. None on timeout/error."""
    if _proc is None or _proc.stdin is None or _proc.stdout is None:
        return None
    try:
        _proc.stdin.write(json.dumps({"id": 1, "image": image_path}) + "\n")
        _proc.stdin.flush()
        # bounded read: readline blocks, so guard with a timer thread that
        # closes stdout on overrun (the request is then abandoned)
        import threading as _t
        result: dict | None = {}

        def _read():
            line = _proc.stdout.readline()
            result["line"] = line

        reader = _t.Thread(target=_read, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive() or not result.get("line"):
            log.warning("quality scorer timed out after %ss", timeout)
            return None
        resp = json.loads(result["line"])
        return resp if "score" in resp else None
    except Exception as exc:
        log.warning("quality scorer request failed: %s", exc)
        return None


def get_stored(sha: str, page_no: int) -> tuple[float, str] | None:
    """The cached (score, level) for a page, or None."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT quality_score, quality_level FROM file_pages "
                    "WHERE sha256=%s AND page_no=%s", (sha, page_no))
        row = cur.fetchone()
        conn.commit()
    return (row[0], row[1]) if row and row[0] is not None else None


def _store(sha: str, page_no: int, score: float, level: str, probs: dict) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE file_pages
                       SET quality_score=%s, quality_level=%s, quality_probs=%s,
                           quality_model='DeQA-Doc-Overall', quality_at=now()
                       WHERE sha256=%s AND page_no=%s""",
                    (score, level, json.dumps(probs), sha, page_no))
        conn.commit()


def score_page(sha: str, page_no: int) -> tuple[float, str] | None:
    """On-demand score: render the page PNG (disk-cached), ask the scorer,
    persist. Returns (score, level) or None when unavailable right now."""
    stored = get_stored(sha, page_no)
    if stored:
        return stored
    try:
        from .render import render_page
        png = render_page(sha, page_no)
    except Exception as exc:
        log.info("quality render %s/%s failed: %s", sha[:12], page_no, exc)
        return None
    with _lock:
        if not _ensure_started():
            return None
        resp = _ask(str(png), REQUEST_TIMEOUT_S)
    if resp is None:
        return None
    score, level, probs = resp["score"], resp["level"], resp.get("probs", {})
    _store(sha, page_no, score, level, probs)
    return (score, level)
