#!/usr/bin/env python3
"""HTTP server for the e-License classify + OCR eval harness.

stdlib http.server.ThreadingHTTPServer; server-rendered HTML (string.Template +
html.escape, no framework/JS build). Spawns the AI worker as a child process
(subprocess.Popen) and drives it via run_control.want_stop. Routes:

  GET  /                        dashboard (filters + run controls + counts)
  GET  /petitions?filter=&view=  filtered petition/file list
  GET  /txn/<txn_id>            a petition's documents (keyed by txn_id)
  GET  /petition/<id>           301 → /txn/<that petition's txn_id> (legacy shim)
  GET  /body/<txn_id>           JSON request body for the petition's modal
  GET  /review/<sha>?declared=   classification + per-page OCR review
  GET  /page/<sha>/<n>           rendered page PNG (Content-Type: image/png)
  POST /verdict                  save a per-page verdict (doctype True/False or ocr Correct/...)
  POST /review/<sha>/rerun       re-run AI on one file
  POST /run/start | /run/stop | /run/continue
  GET  /run/status              JSON {state, last_exit_code, pending}
  POST /index                    re-run the indexer

The entry bootstraps the project root onto sys.path before importing eval.* —
`uv run python -m eval.server` adds only the package dir, not the project root.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

# --- sys.path bootstrap (must precede `from eval import ...`) --------------
# `uv run python -m eval.server` puts the package dir on sys.path but NOT the
# project root, so `import eval.config` works only if the root is prepended.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from eval import config, queries
from eval.db import connect
from eval.templating import base, render, esc

log = logging.getLogger("eval.server")


# --- worker subprocess control (in-process handle behind a lock) -----------
class WorkerControl:
    """Holds the worker Popen handle + run_control state behind a lock.

    Single-server deployment: the handle lives in the server process. Start =
    Popen the worker + clear want_stop + state='running'; Stop = set want_stop +
    state='stopping'; Continue = clear want_stop + Popen again. On the worker
    process exiting, a reaper records last_exit_code and flips state='idle'.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self._reaper_started = False

    def _set(self, **fields) -> None:
        with connect() as conn, conn.cursor() as cur:
            cols = ", ".join(f"{k}=%s" for k in fields)
            cur.execute(f"UPDATE run_control SET {cols}, updated_at=now() WHERE id=1",
                        tuple(fields.values()))
            conn.commit()

    def _ensure_reaper(self) -> None:
        # Start (once) a background thread that waits on the worker process and,
        # when it exits, records the exit code and flips state to idle.
        if self._reaper_started:
            return
        self._reaper_started = True
        threading.Thread(target=self._reaper_loop, daemon=True).start()

    def _reaper_loop(self) -> None:
        while True:
            proc = self.proc
            if proc is None:
                time.sleep(0.3)
                continue
            rc = proc.wait()  # block until this worker exits
            with self.lock:
                if self.proc is proc:
                    self.proc = None
            log.info("worker exited code=%s", rc)
            self._set(last_exit_code=rc, last_stopped_at=datetime.now(timezone.utc),
                      state="idle")
            # clear want_stop so a future Start isn't immediately stopped
            with connect() as conn, conn.cursor() as cur:
                cur.execute("UPDATE run_control SET want_stop=false, updated_at=now() WHERE id=1")
                conn.commit()

    def start(self) -> str:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return "already running"
            # clear want_stop and mark running before spawning
            self._set(want_stop=False, state="running", last_started_at=datetime.now(timezone.utc))
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "eval.worker"],
                cwd=_PROJECT_ROOT,
                stdout=open("/tmp/eval_worker.stdout.log", "ab"),
                stderr=subprocess.STDOUT,
            )
            self._ensure_reaper()
            log.info("worker started pid=%s", self.proc.pid)
            return "started"

    def stop(self) -> str:
        self._set(want_stop=True, state="stopping")
        log.info("worker stop requested")
        return "stopping"

    def cont(self) -> str:
        # Continue = clear want_stop + re-spawn (pending rows resume automatically).
        return self.start()

    def status(self) -> dict:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT state, last_exit_code, want_stop FROM run_control WHERE id=1")
            state, last_exit_code, want_stop = cur.fetchone()
            cur.execute("SELECT count(*) FROM files WHERE ai_class_status='pending'")
            pending = cur.fetchone()[0]
            conn.commit()
        return {"state": state, "last_exit_code": last_exit_code,
                "want_stop": want_stop, "pending": pending}


WORKER = WorkerControl()

# Per-sha re-run throttle (in-memory; single-server deployment). Maps sha256 ->
# monotonic time of the last re-run. 30s per file to avoid thrash (PLAN.md).
_RERUN_AT: dict[str, float] = {}
_RERUN_LOCK = threading.Lock()
RERUN_COOLDOWN_S = 30.0


# --- helpers ----------------------------------------------------------------
def _enum_labels(enum_name: str) -> list[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT enumlabel FROM pg_enum e JOIN pg_type t ON e.enumtypid=t.oid "
                    "WHERE t.typname=%s ORDER BY e.enumsortorder", (enum_name,))
        return [r[0] for r in cur.fetchall()]


def _count_where(predicate_sql: str, type_clause: str) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM files f WHERE {predicate_sql}{type_clause}")
        return cur.fetchone()[0]


def _count_distinct_petitions(type_clause: str) -> int:
    """Count petitions that have >=1 file (optionally narrowed by type filters)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(DISTINCT p.id) FROM petitions p "
                    f"JOIN petition_files pf ON pf.petition_id=p.id "
                    f"JOIN files f ON f.sha256=pf.sha256 "
                    f"WHERE 1=1{type_clause}")
        return cur.fetchone()[0]


def _count_review_queue(type_clause: str = "") -> int:
    """Count AI-finished-and-not-yet-verdicted file contexts (the review queue).
    Returns 0 while the worker hasn't finished anything (honest empty state)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(queries.review_queue_count_sql(type_clause))
        return cur.fetchone()[0]


def _petition_cards(status_predicate: str | None, type_clause: str, limit: int = 120) -> tuple[str, int, int]:
    """Render a grid of petition cards for the dashboard. Returns (cards_html,
    shown_count, total_matching_count). total is computed separately so the UI can
    show "+N more" when the LIMIT truncates.

    Each card: document_no (or id short), state, file count, and progress badges
    (done/error/pending/reviewed). Clicking opens /txn/<txn_id>.
    """
    sql = queries.petition_cards_sql(status_predicate, type_clause, limit=limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
        # total matching (unbounded) for the "+N more" line
        where = status_predicate if status_predicate else "1=1"
        cur.execute(f"SELECT count(DISTINCT p.id) FROM petitions p "
                    f"JOIN petition_files pf ON pf.petition_id=p.id "
                    f"JOIN files f ON f.sha256=pf.sha256 "
                    f"WHERE {where}{type_clause}")
        total = cur.fetchone()[0]
        conn.commit()

    cards = []
    for pid, txn, dno, st, nf, nd, ne, nno, nsk, npe, nrev in rows:
        # txn_id is the main identifier the user needs to see in full; document_no
        # and state are secondary context on the sub-line. The card links to
        # /txn/<txn_id> (the primary route key); fall back to the legacy id route
        # only if txn_id is somehow NULL.
        href = f"/txn/{quote(txn)}" if txn else f"/petition/{quote(pid)}"
        title = esc(dno) if dno else f"petition {esc(pid[:8])}…"
        sub = (f"<span class='mono'>{esc(txn)}</span>"
               + (f" · {esc(dno)}" if dno else "")
               + f" · {esc(st or '—')} · {nf} file(s)")
        # segmented mini progress bar (done/pending/none/skipped/error) so each
        # card shows its file mix at a glance; total = the bar's denominator.
        bar_total = nd + npe + nno + nsk + ne
        bar_html = (
            "<span class='card-bar'>"
            + _seg("done", nd, bar_total)
            + _seg("pending", npe, bar_total)
            + _seg("none", nno, bar_total)
            + _seg("skipped", nsk, bar_total)
            + _seg("error", ne, bar_total)
            + "</span>"
        ) if bar_total else "<span class='card-bar'></span>"
        badges = []
        if nd: badges.append(f"<span class='dot done'>{nd} done</span>")
        if ne: badges.append(f"<span class='dot error'>{ne} error</span>")
        if npe: badges.append(f"<span class='dot pending'>{npe} pending</span>")
        if nno: badges.append(f"<span class='dot none'>{nno} none</span>")
        if nsk: badges.append(f"<span class='dot skipped'>{nsk} skipped</span>")
        if nrev: badges.append(f"<span class='vpill vpill-correct'>✓ {nrev} reviewed</span>")
        badge_html = "".join(badges) or "<span class='small'>no AI yet</span>"
        cards.append(
            f"<a class='card' href='{href}'>"
            f"<span class='card-title'>{title}</span>"
            f"<span class='card-sub'>{sub}</span>"
            f"{bar_html}"
            f"<span class='card-badges'>{badge_html}</span>"
            f"</a>")
    return "".join(cards), len(rows), total


def _status_pill(status: str) -> str:
    """Return the AI-status indicator class string for `status` (a files.
    ai_class_status enum value: done/pending/none/skipped/error). Emits the
    DOT family — `dot dot-<status>` — so machine state reads as a small
    colored dot + label, distinct from the verdict VPILL family."""
    return _status_dot(status)


# Order matters only for the stacked bar + count list. `none`/`skipped` are
# muted so they sit at the end of the segment row.
_STATUS_ORDER = ("done", "pending", "none", "skipped", "error")

# Static legend — decodes both the AI-status dot family and the human-verdict
# vpill family on one line. Emitted on the dashboard + review page so a user
# can map a color to its meaning without guessing. Pure static HTML (the
# colors live in base.html's .dot/.vpill CSS), so this is one constant.
LEGEND_HTML = (
    "<div class='legend'>"
    "<span class='lg-label'>AI status</span>"
    "<span class='dot done'>done</span>"
    "<span class='dot pending'>pending</span>"
    "<span class='dot skipped'>skipped</span>"
    "<span class='dot none'>none</span>"
    "<span class='dot error'>error</span>"
    "<span class='lg-sep'>|</span>"
    "<span class='lg-label'>human verdict</span>"
    "<span class='vpill vpill-correct'>✓ correct</span>"
    "<span class='vpill vpill-wrong'>✗ wrong</span>"
    "<span class='vpill vpill-acceptable'>~ acceptable</span>"
    "</div>"
)


def _status_dot(status: str) -> str:
    """Class string for the AI-status dot: `dot dot-<status>` (status defaults
    to pending; an unknown status falls back to none so it still renders)."""
    s = (status or "pending").strip().lower()
    if s not in _STATUS_ORDER:
        s = "none"
    return f"dot dot-{s}"


def _verdict_pill(verdict: str) -> str:
    """Filled <span> for a human verdict (correct/wrong/acceptable), or
    an em-dash placeholder when there is no verdict. Used on the txn-detail
    'human verdict' cell and the OCR per-page 'current' chip. 'wrong' is the
    single unified negative verdict for both classification and OCR stages."""
    if not verdict:
        return "<span class='small dim'>—</span>"
    v = verdict.strip().lower()
    if v not in ("correct", "wrong", "acceptable"):
        return f"<span class='small dim'>{esc(verdict)}</span>"
    glyphs = {"correct": "✓", "wrong": "✗", "acceptable": "~"}
    return f"<span class='vpill vpill-{v}'>{glyphs[v]} {esc(v)}</span>"


def _seg(kind: str, count: int, total: int) -> str:
    """One segment of a segmented progress bar: <span class='seg seg-<kind>'
    style='width:Npx'>. Zero-count segments are omitted (no width). total is
    the sum of all segments; guards div-by-zero (empty bar -> no segments)."""
    if not count or total <= 0:
        return ""
    # 200px-wide track keeps the bar legible without filling the whole card
    px = max(1, round(200 * count / total))
    return f"<span class='seg seg-{kind}' style='width:{px}px' title='{count} {kind}'></span>"


# --- the request handler ----------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter access log
        log.info("%s %s", self.address_string(), fmt % args)

    # -- response helpers --
    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, html_str: str) -> None:
        self._send(code, html_str.encode("utf-8"))

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _qs(self) -> dict[str, str]:
        q = urlparse(self.path).query
        return {k: v[-1] for k, v in parse_qs(q, keep_blank_values=True).items()}

    # -- routing --
    def do_GET(self):
        try:
            self._route_get()
        except Exception as exc:  # never let a handler crash the server
            log.exception("GET %s failed", self.path)
            self._html(500, base("Error", f"<pre>{esc(exc)}</pre>"))

    def do_POST(self):
        try:
            self._route_post()
        except Exception as exc:
            log.exception("POST %s failed", self.path)
            self._html(500, base("Error", f"<pre>{esc(exc)}</pre>"))

    def _route_get(self):
        path = urlparse(self.path).path
        if path == "/":
            self._home()
        elif path == "/dashboard":
            self._dashboard()
        elif path == "/review-queue":
            self._review_queue()
        elif path == "/verdict-pages":
            self._verdict_pages()
        elif path == "/classify-score":
            self._classify_score()
        elif path.startswith("/quality/"):
            self._quality_score(path.removeprefix("/quality/"))
        elif path == "/petitions":
            self._petitions()
        elif path.startswith("/txn/"):
            self._txn_detail(path.removeprefix("/txn/"))
        elif path.startswith("/body/"):
            self._body_json(path.removeprefix("/body/"))
        elif path.startswith("/petition/"):
            self._petition_legacy_redirect(path.removeprefix("/petition/"))
        elif path.startswith("/review/"):
            self._review(path.removeprefix("/review/"))
        elif path.startswith("/page/"):
            self._page_png(path.removeprefix("/page/"))
        elif path == "/run/status":
            self._json(WORKER.status())
        elif path == "/worker-log":
            self._worker_log()
        elif path == "/favicon.ico":
            self._send(204, b"")
        else:
            self._html(404, base("Not found", f"<p>{esc(path)}</p>"))

    def _route_post(self):
        path = urlparse(self.path).path
        if path == "/run/start":
            WORKER.start(); self._redirect("/")
        elif path == "/run/stop":
            WORKER.stop(); self._redirect("/")
        elif path == "/run/continue":
            WORKER.cont(); self._redirect("/")
        elif path == "/run/retry_errors":
            self._post_retry_errors()
        elif path == "/verdict":
            self._post_verdict()
        elif path == "/index":
            self._post_index()
        elif path.endswith("/rerun") and path.startswith("/review/"):
            sha = path.removeprefix("/review/").rsplit("/", 1)[0]
            self._post_rerun(sha)
        else:
            self._html(404, base("Not found", f"<p>{esc(path)}</p>"))

    # -- read form body --
    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return {k: v[-1] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}

    # ===== home: petition list (attachment_browser style) =====
    def _home(self):
        """Landing page — a petition list table styled like elicense-db-ui's
        attachment_browser. Each row leads with txn_id (the primary identity +
        route key, code + copy + link) and carries the internal id as small
        secondary context, plus document_no, state, file count, AI-status-mix
        badges, human-verdict summary. Filter chips narrow by status; a "Needs
        review" chip links to /review-queue; a search box filters by txn_id /
        document_no / id / state."""
        qs = self._qs()
        filt = qs.get("filter") or ""  # empty = all petitions
        q = (qs.get("q") or "").strip()
        type_clause = queries.type_filter_sql(None, None, False)
        status_predicate = queries.STATUS_FILTERS.get(filt)  # None = all

        sql = queries.petition_cards_sql(status_predicate, type_clause, limit=2000)
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            conn.commit()

        # client-side search over txn_id / document_no / id / state
        ql = q.lower()
        if ql:
            def _match(r):
                pid, txn, dno, st = r[0], r[1], r[2], r[3]
                hay = " ".join(str(x or "") for x in (pid, txn, dno, st)).lower()
                return ql in hay
            rows = [r for r in rows if _match(r)]

        # filter chips with counts (all + the 4 status filters) + a "Needs
        # review" chip linking to /review-queue with a live count.
        chip_counts = {name: _count_where(pred, "") for name, pred in queries.STATUS_FILTERS.items()}
        c_all = _count_distinct_petitions("")
        c_review_queue = _count_review_queue()
        chips = [('<a class="filter %s" href="/?filter=">all petitions <span class="count">%d</span></a>'
                  % ("active" if not filt else "", c_all))]
        labels = {"ai_done": "AI-done", "human_done": "Human-done",
                  "human_say_ai_wrong": "AI-wrong", "still_not_done": "Still-not-done"}
        for name in ("ai_done", "human_done", "human_say_ai_wrong", "still_not_done"):
            chips.append('<a class="filter %s" href="/?filter=%s">%s <span class="count">%d</span></a>'
                         % ("active" if filt == name else "", name, labels[name], chip_counts[name]))
        chips.append('<a class="filter" href="/review-queue">Needs review <span class="count">%d</span></a>'
                     % c_review_queue)

        # table rows — txn_id is the primary identity + route key (lead column,
        # code + copy + link to /txn/<txn>); the internal petition id is a small
        # secondary column. The "files" cell also links to the txn detail.
        body_rows = []
        for pid, txn, dno, st, nf, nd, ne, nno, nsk, npe, nrev in rows:
            badges = []
            if nd: badges.append(f"<span class='dot done'>{nd} done</span>")
            if ne: badges.append(f"<span class='dot error'>{ne} error</span>")
            if npe: badges.append(f"<span class='dot pending'>{npe} pending</span>")
            if nno: badges.append(f"<span class='dot none'>{nno} none</span>")
            if nsk: badges.append(f"<span class='dot skipped'>{nsk} skipped</span>")
            if nrev: badges.append(f"<span class='vpill vpill-correct'>✓ {nrev} reviewed</span>")
            badge_html = " ".join(badges) or "<span class='small dim'>no AI yet</span>"
            txn_link = (f"<a class='open' href='/txn/{quote(txn)}'>{esc(txn)}</a>"
                        if txn else "<span class='small dim'>—</span>")
            txn_copy = (f"<button class='cp' data-copy='{esc(txn)}' title='copy txn_id'>⧉</button>"
                        if txn else "")
            body_rows.append(
                f"<tr>"
                f"<td class='pid'><div class='ln'>{txn_link}{txn_copy}</div></td>"
                f"<td class='pid'><div class='ln'><span class='mono small dim'>{esc(pid[:12])}…</span>"
                f"<button class='cp' data-copy='{esc(pid)}' title='copy petition id'>⧉</button></div></td>"
                f"<td>{esc(dno or '—')}</td>"
                f"<td><span class='small'>{esc(st or '—')}</span></td>"
                f"<td><a class='open' href='/txn/{quote(txn) if txn else quote(pid)}'>{nf} file(s)</a></td>"
                f"<td>{badge_html}</td>"
                f"</tr>")

        table = (
            "<table><thead><tr>"
            "<th>txn_id</th><th>petition id</th><th>document_no</th><th>state</th>"
            "<th>files</th><th>AI status</th>"
            "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>")
        if not body_rows:
            table = "<div class='empty'>no petitions match.</div>"

        search = (f"<form method='get' action='/' style='display:inline-flex;gap:6px;align-items:center'>"
                  f"<input type='text' name='q' value='{esc(q)}' placeholder='search txn_id / document_no / id / state' style='min-width:300px'>"
                  + (f"<input type='hidden' name='filter' value='{esc(filt)}'>" if filt else "")
                  + "<button>search</button></form>")

        st = WORKER.status()
        body = (
            f"<div class='filters'>{''.join(chips)}</div>"
            f"{LEGEND_HTML}"
            f"<div style='margin:6px 0 12px'>{search}"
            f"<span class='small' style='margin-left:14px'>{len(rows)} petition(s)"
            + (f" · filtered by <b>{esc(filt)}</b>" if filt else "") + "</span></div>"
            + table
            + "<p class='small' style='margin-top:10px'>worker controls + counts on the "
            "<a class='open' href='/dashboard'>dashboard</a> page.</p>")
        self._html(200, base("TXN", body, nav_home="active",
                             run_state=f"worker: {st['state']} · {st['pending']} pending"))

    # ===== worker log (tail of /tmp/eval_worker.stdout.log) =====
    def _worker_log(self):
        qs = self._qs()
        try:
            n = max(50, min(int(qs.get("n", "400")), 5000))
        except ValueError:
            n = 400
        log_path = "/tmp/eval_worker.stdout.log"
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            lines = []
        tail = lines[-n:] if len(lines) > n else lines
        body_html = "".join(esc(ln) for ln in tail) or "(empty log)"
        st = WORKER.status()
        last_exit = "—" if st["last_exit_code"] is None else st["last_exit_code"]
        # errored file count (class OR any extract page errored) for the "Retry errored" button
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT count(DISTINCT f.sha256) FROM files f
                           WHERE f.ai_class_status='error'
                              OR EXISTS (SELECT 1 FROM file_extracts x
                                         WHERE x.sha256=f.sha256 AND x.ai_extract_status='error')""")
            c_error = cur.fetchone()[0]
            conn.commit()
        # worker control bar (moved here from the dashboard). Start/Stop/Continue
        # + Re-index + Retry errored, plus the worker-state + log-size readouts.
        controls = (
            "<div class='controls'>"
            "<form class='inline-form' method='post' action='/run/start'><button class='run'>Start</button></form>"
            "<form class='inline-form' method='post' action='/run/stop'><button class='stop'>Stop</button></form>"
            "<form class='inline-form' method='post' action='/run/continue'><button>Continue</button></form>"
            "<span class='small'>|</span>"
            f"<span class='wdot wdot-{esc(st['state'])}'>worker {esc(st['state'])}</span>"
            f"<span class='small'>pending: <b>{esc(st['pending'])}</b> files</span>"
            f"<span class='small'>last exit: <b>{esc(last_exit)}</b></span>"
            "<form class='inline-form' method='post' action='/index'>"
            "<button>Re-index</button>"
            "<span class='small'>(re-run CSV+JSON load; idempotent)</span>"
            "</form>"
            "<form class='inline-form' method='post' action='/run/retry_errors'>"
            f"<button>Retry errored ({c_error})</button>"
            "<span class='small'>(reset all error files+pages to pending, clear their verdicts)</span>"
            "</form>"
            "<span class='small'>|</span>"
            "<a class='filter' href='?n=400'>last 400</a>"
            "<a class='filter' href='?n=1000'>1000</a>"
            "<a class='filter' href='?n=5000'>5000</a>"
            "<a class='filter' href='/dashboard'>← dashboard</a>"
            "</div>")
        empty_note = (
            "<p class='small'>No worker log yet (the worker hasn't run, or the log "
            f"<code>{esc(log_path)}</code> was cleared). Use Start above.</p>")
        log_block = (f"<pre class='worker-log'>{body_html}</pre>"
                     if lines else empty_note)
        size_note = (f"<span class='small'>showing last <b>{n}</b> of <b>{len(lines)}</b> lines</span>"
                     if lines else "")
        body = (controls + log_block + size_note)
        self._html(200, base("Worker log", body,
                             run_state=f"worker: {st['state']} · {st['pending']} pending"))

    # ===== dashboard =====
    def _dashboard(self):
        qs = self._qs()
        # Default landing: ALL petitions with files (filter empty). The four status
        # chips narrow the grid to petitions having a file matching that status.
        filt = qs.get("filter") or ""
        view = qs.get("view", "petitions")
        declared = qs.get("declared") or None
        predicted = qs.get("predicted") or None
        oov_only = qs.get("oov_only") == "1"
        type_clause = queries.type_filter_sql(declared, predicted, oov_only)

        counts = {name: _count_where(pred, type_clause)
                  for name, pred in queries.STATUS_FILTERS.items()}
        # total petitions-with-files (the "All petitions" chip count)
        c_all_petitions = _count_distinct_petitions(type_clause)

        # status breakdown: per-ai_class_status counts -> stacked bar + count list.
        # Collected as a dict first so the stacked bar + legend list share one source.
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT ai_class_status, count(*) FROM files GROUP BY ai_class_status")
            status_counts = {s: n for s, n in cur.fetchall()}
            cur.execute("SELECT count(*) FROM files")
            c_total_files = cur.fetchone()[0]
            # files with >=1 per-page verdict (doctype or ocr) — for the
            # "reviewed %" hero stat. (was: count of page_no IS NULL class rows)
            cur.execute("SELECT count(DISTINCT sha256) FROM verdicts WHERE page_no IS NOT NULL")
            c_reviewed_files = cur.fetchone()[0]
            # errored files: count for the "Retry errored" button (class OR any
            # extract page errored). Single-path on file_extracts (the worker no
            # longer writes file_pages).
            cur.execute("""SELECT count(DISTINCT f.sha256) FROM files f
                           WHERE f.ai_class_status='error'
                              OR EXISTS (SELECT 1 FROM file_extracts x
                                         WHERE x.sha256=f.sha256 AND x.ai_extract_status='error')""")
            c_error = cur.fetchone()[0]
            # human-review accuracy (model-quality stat cards): scoped to AI-done
            # AND human-reviewed, both now per-PAGE. doctype_acc = correct /
            # doctype-verdicted pages (doc_types right? True); ocr_acc =
            # (correct+acceptable) / all ocr page verdicts on AI-extracted pages.
            type_clause_ctx = queries.type_filter_ctx_sql(declared, predicted, oov_only)
            cur.execute(queries.doctype_accuracy_sql(type_clause_ctx))
            dt_correct, dt_total = cur.fetchone()
            cur.execute(queries.ocr_accuracy_sql(type_clause_ctx))
            ocr_good, ocr_total = cur.fetchone()
            # per-verdict breakdowns for the Figma-style score chunks (rings per
            # Correct / Wrong for doctype; Correct / Acceptable / Wrong for ocr).
            cur.execute(queries.doctype_breakdown_sql(type_clause_ctx))
            dt_correct_b, dt_wrong, dt_total_b = cur.fetchone()
            cur.execute(queries.ocr_breakdown_sql(type_clause_ctx))
            ocr_correct_b, ocr_acceptable, ocr_wrong, ocr_total_b = cur.fetchone()
            conn.commit()
        doctype_acc_pct = round(100 * dt_correct / dt_total) if dt_total else 0
        ocr_acc_pct = round(100 * ocr_good / ocr_total) if ocr_total else 0
        # breakdown numbers (authoritative denominators are the *_b totals; they
        # match the accuracy totals but are read from the one-shot breakdown query
        # so the chunk is internally consistent with its own rings).
        ocr_correct_pct = round(100 * ocr_correct_b / ocr_total_b) if ocr_total_b else 0
        ocr_acceptable_pct = round(100 * ocr_acceptable / ocr_total_b) if ocr_total_b else 0
        pending = status_counts.get("pending", 0)

        # "How many files left" bar: processed (done/none/skipped/error) vs
        # still-left (pending), widths ∝ counts out of total files. Hover a
        # segment for its exact number.
        bar_total = c_total_files or 1
        n_left = pending
        n_processed = max(0, c_total_files - n_left)
        left_pct = round(100 * n_left / bar_total) if c_total_files else 0
        stacked_bar = (
            f"<div class='bar'>"
            f"<span class='seg seg-done' style='width:{100 - left_pct}%' title='{n_processed} processed'></span>"
            f"<span class='seg seg-pending' style='width:{left_pct}%' title='{n_left} left'></span>"
            f"</div>"
            f"<p class='small' style='margin:6px 0 0'>"
            f"<b>{n_left:,}</b> left of {c_total_files:,} · {n_processed:,} processed</p>"
        ) if c_total_files else "<div class='empty'>no files indexed.</div>"

        # per-status count list (the legend-shaped breakdown under the bar)
        status_list = "".join(
            f"<li class='s-{k}'><b>{status_counts.get(k, 0)}</b> {k}</li>"
            for k in _STATUS_ORDER)

        # hero stat percentages — AI-done% (done+none+error) and reviewed%.
        c_files_done = sum(status_counts.get(k, 0) for k in ("done", "none", "error"))
        ai_done_pct = round(100 * c_files_done / bar_total) if c_total_files else 0
        reviewed_pct = round(100 * c_reviewed_files / bar_total) if c_total_files else 0

        # petition cards for the active filter (empty filter = all petitions)
        status_predicate = queries.STATUS_FILTERS.get(filt)  # None if empty/unknown
        cards_html, shown, total = _petition_cards(status_predicate, type_clause, limit=120)
        if filt:
            cards_header = f"— {esc(filt)} ({total} petition(s))"
        else:
            cards_header = f"— {total} total"
        cards_more = (f"Showing first {shown} of {total} petitions. "
                     f"<a href='/petitions?filter={esc(filt)}&view=petitions'>view all</a>"
                     if total > shown else "")

        # dropdown options
        declared_labels = _enum_labels("declared_category_t")
        classifiable = set(_enum_labels("classifiable_category_t"))
        declared_opts = "".join(
            f"<option value='{esc(l)}' {'selected' if l==declared else ''}>{esc(l)}{' (OOV)' if l not in classifiable else ''}</option>"
            for l in declared_labels)
        predicted_opts = "".join(
            f"<option value='{esc(l)}' {'selected' if l==predicted else ''}>{esc(l)}</option>"
            for l in sorted(classifiable))

        def active(name): return "active" if filt == name else ""
        body = render("dashboard.html",
            pending=pending,
            f_all_active=active(""), f_ai_done_active=active("ai_done"),
            f_human_done_active=active("human_done"),
            f_wrong_active=active("human_say_ai_wrong"),
            f_not_done_active=active("still_not_done"),
            c_all_petitions=c_all_petitions,
            c_ai_done=counts["ai_done"], c_human_done=counts["human_done"],
            c_wrong=counts["human_say_ai_wrong"], c_not_done=counts["still_not_done"],
            c_error=c_error,
            c_review_queue=_count_review_queue(),
            c_total_files=c_total_files,
            ai_done_pct=ai_done_pct, ai_rest_pct=max(0, 100 - ai_done_pct),
            reviewed_pct=reviewed_pct, reviewed_rest_pct=max(0, 100 - reviewed_pct),
            doctype_acc_pct=doctype_acc_pct, doctype_acc_correct=dt_correct, doctype_acc_total=dt_total,
            doctype_acc_rest_pct=max(0, 100 - doctype_acc_pct),
            ocr_acc_pct=ocr_acc_pct, ocr_acc_good=ocr_good, ocr_acc_total=ocr_total,
            ocr_acc_rest_pct=max(0, 100 - ocr_acc_pct),
            # Figma-style score-chunk breakdowns
            doctype_acc_wrong=dt_wrong,
            ocr_acc_correct=ocr_correct_b, ocr_acc_acceptable=ocr_acceptable,
            ocr_acc_wrong=ocr_wrong,
            ocr_correct_pct=ocr_correct_pct, ocr_acceptable_pct=ocr_acceptable_pct,
            stacked_bar=stacked_bar, status_list=status_list,
            legend=LEGEND_HTML,
            cards=cards_html, cards_header=cards_header, cards_more=cards_more,
            filter=esc(filt), view=esc(view),
            # querystring for the /classify-score links (preserves type filters)
            cs_qs=esc("?" + urlencode({k: v for k, v in
                (("declared", declared), ("predicted", predicted),
                 ("oov_only", "1" if oov_only else None)) if v}) if
                (declared or predicted or oov_only) else ""),
            declared_opts=declared_opts, predicted_opts=predicted_opts,
            oov_checked="checked" if oov_only else "",
            index_summary=f"files: see counts above · worker pid may be running",
        )
        st2 = WORKER.status()
        self._html(200, base("Dashboard", body, nav_dash="active",
                             run_state=f"worker: {st2['state']} · {st2['pending']} pending"))

    # ===== petition/file list =====
    def _petitions(self):
        qs = self._qs()
        filt = qs.get("filter") or ""  # empty = all (matches the dashboard's default)
        view = qs.get("view", "petitions")
        declared = qs.get("declared") or None
        predicted = qs.get("predicted") or None
        oov_only = qs.get("oov_only") == "1"
        type_clause = queries.type_filter_sql(declared, predicted, oov_only)
        # status predicate is None for the unfiltered "all" view
        predicate = queries.STATUS_FILTERS.get(filt) or "1=1"

        with connect() as conn, conn.cursor() as cur:
            if view == "files":
                # files view: join petition_files to show each file's txn_id
                # beside its sha256 (review itself is per-file, stays sha-keyed).
                cur.execute(f"""SELECT f.sha256, f.content_kind, f.page_count,
                                       f.ai_class_status, f.ai_predicted_category,
                                       f.declared_filetype_first, pf.txn_id
                                FROM files f
                                LEFT JOIN LATERAL (
                                    SELECT pf.txn_id FROM petition_files pf
                                    WHERE pf.sha256 = f.sha256
                                    ORDER BY pf.declared_category LIMIT 1
                                ) pf ON true
                                WHERE {predicate}{type_clause}
                                ORDER BY f.first_seen_at LIMIT 500""")
                rows = cur.fetchall()
                header_cells = ("<th>sha256</th><th>txn_id</th><th>kind</th><th>pages</th>"
                                "<th>AI status</th><th>AI predicted</th><th>first declared</th><th></th>")
                body_rows = []
                for f in rows:
                    sha, kind, pgs, st, pred, dfirst, ftxn = f
                    href = f"/review/{quote(sha)}?declared={quote(dfirst)}" if dfirst else f"/review/{quote(sha)}"
                    txn_cell = (f"<td class='small mono'>"
                                f"<a class='open' href='/txn/{quote(str(ftxn))}'>{esc(ftxn)}</a></td>"
                                if ftxn else "<td class='small dim'>—</td>")
                    body_rows.append(
                        f"<tr><td class='small mono'>{esc(sha)}</td>"
                        f"{txn_cell}"
                        f"<td>{esc(kind)}</td><td>{pgs}</td>"
                        f"<td><span class='{_status_dot(st)}'>{esc(st)}</span></td>"
                        f"<td>{esc(pred or '—')}</td><td>{esc(dfirst or '—')}</td>"
                        f"<td><a href='{href}'>review</a></td></tr>")
                unit = "files"
            else:
                # petitions view: petitions having >=1 file matching the filter.
                # txn_id is the primary identity + route key; the internal id is
                # shown as small secondary context. Links go to /txn/<txn_id>.
                cur.execute(f"""SELECT p.id, p.txn_id, p.document_no, p.state, count(pf.sha256) AS n
                                FROM petitions p
                                JOIN petition_files pf ON pf.petition_id = p.id
                                JOIN files f ON f.sha256 = pf.sha256
                                WHERE {predicate}{type_clause}
                                GROUP BY p.id, p.txn_id, p.document_no, p.state
                                ORDER BY count(pf.sha256) DESC, p.document_no NULLS LAST LIMIT 500""")
                rows = cur.fetchall()
                header_cells = ("<th>txn_id</th><th>petition id</th><th>document_no</th>"
                                "<th>state</th><th># files</th>")
                body_rows = []
                for pid, txn, dno, st, n in rows:
                    txn_link = (f"<a href='/txn/{quote(str(txn))}' class='mono small'>{esc(str(txn))}</a>"
                                if txn else "<span class='small dim'>—</span>")
                    body_rows.append(
                        f"<tr><td>{txn_link}</td>"
                        f"<td><span class='mono small dim'>{esc(str(pid))}</span></td>"
                        f"<td>{esc(dno or '—')}</td><td>{esc(st or '—')}</td><td>{n}</td></tr>")
                unit = "petitions"
            conn.commit()

        count = len(rows)
        other_view = "files" if view == "petitions" else "petitions"
        view_link = f"<a href='/petitions?filter={esc(filt)}&view={other_view}'>switch to {other_view} view</a>"
        body = render("list.html",
            filter_label=esc(f"filter: {filt}"),
            count=count, unit=unit, view_link=view_link,
            header_cells=header_cells, rows="".join(body_rows))
        self._html(200, base(f"Petitions — {filt or 'all'}", body, nav_home="active"))

    # ===== review queue: AI-finished + not-yet-verdicted file contexts =====
    def _review_queue(self):
        """One row per (file, declared_category) context that is AI-finished
        (status done/none/error, no pending pages) but not yet verdicted. Each
        row shows the full txn_id (link to /txn/<txn>), document_no, the file
        (name + kind + pages), the declared category, the AI predicted type +
        status pill, and a direct review link. Empty state is honest: with the
        LLM down, nothing is AI-done, so the queue is legitimately empty until
        the worker runs against a live endpoint. Type filters via the same
        ?declared=&predicted=&oov_only=1 form as /petitions."""
        qs = self._qs()
        declared = qs.get("declared") or None
        predicted = qs.get("predicted") or None
        oov_only = qs.get("oov_only") == "1"
        type_clause = queries.type_filter_sql(declared, predicted, oov_only)

        sql = queries.review_queue_sql(type_clause, limit=500)
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            cur.execute(queries.review_queue_count_sql(type_clause))
            total = cur.fetchone()[0]
            conn.commit()

        body_rows = []
        for txn, dno, sha, sfn, kind, pgs, decl, aist, pred in rows:
            href = f"/review/{quote(sha)}?declared={quote(decl)}"
            txn_cell = (f"<td class='pid'><div class='ln'>"
                        f"<a class='open' href='/txn/{quote(txn)}'><span class='mono'>{esc(txn)}</span></a>"
                        f"<button class='cp' data-copy='{esc(txn)}' title='copy txn_id'>⧉</button></div></td>"
                        if txn else "<td class='small dim'>—</td>")
            file_cell = (f"<span class='mono small'>{esc(sha[:16])}…</span>"
                         + (f"<br><span class='small'>{esc(sfn)} ({esc(kind)}, {pgs}p)</span>"
                            if sfn else ""))
            body_rows.append(
                f"<tr>{txn_cell}"
                f"<td>{esc(dno or '—')}</td>"
                f"<td class='small'>{file_cell}</td>"
                f"<td>{esc(decl)}</td>"
                f"<td>{esc(pred or '—')}</td>"
                f"<td><span class='{_status_dot(aist)}'>{esc(aist)}</span></td>"
                f"<td><a href='{href}'>review</a></td></tr>")

        if body_rows:
            table = ("<table><thead><tr>"
                     "<th>txn_id</th><th>document_no</th><th>file (sha256) + name</th>"
                     "<th>declared type</th><th>AI predicted</th><th>AI status</th><th></th>"
                     "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>")
        else:
            table = ("<div class='empty'>Nothing needs review yet — files appear here "
                     "once the worker finishes classifying + OCRing them and you "
                     "haven't verdicted them. (With the LLM endpoint down, the queue "
                     "is legitimately empty until a Start/Continue runs against a "
                     "live endpoint.)</div>")

        # type-filter form (same shape as /petitions) so the queue is narrowable
        declared_labels = _enum_labels("declared_category_t")
        classifiable = set(_enum_labels("classifiable_category_t"))
        declared_opts = "".join(
            f"<option value='{esc(l)}' {'selected' if l == declared else ''}>{esc(l)}{' (OOV)' if l not in classifiable else ''}</option>"
            for l in declared_labels)
        predicted_opts = "".join(
            f"<option value='{esc(l)}' {'selected' if l == predicted else ''}>{esc(l)}</option>"
            for l in sorted(classifiable))
        form = (f"<form method='get' action='/review-queue' style='margin:8px 0 12px;display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap'>"
                f"<label>declared: <select name='declared'><option value=''>(any)</option>{declared_opts}</select></label>"
                f"<label>predicted: <select name='predicted'><option value=''>(any)</option>{predicted_opts}</select></label>"
                f"<label><input type='checkbox' name='oov_only' value='1' {'checked' if oov_only else ''}> OOV-only</label>"
                f"<button>Apply</button></form>")

        shown = len(rows)
        more = (f"<p class='small'>Showing first {shown} of {total} context(s) needing review.</p>"
                if total > shown else "")
        body = (f"<h3>Files I need to review — AI-finished, not yet verdicted</h3>"
                f"{form}"
                f"<p class='small'>{total} context(s) need review{(' · filtered by ' + ('OOV-only' if oov_only else 'type') ) if (declared or predicted or oov_only) else ''}.</p>"
                + table + more
                + "<p class='small' style='margin-top:10px'>worker controls on the "
                "<a class='open' href='/dashboard'>dashboard</a> page.</p>")
        self._html(200, base("Review queue", body, nav_home="active"))

    # ===== verdict drill-down: the pages behind one score-chunk ring =====
    def _verdict_pages(self):
        """List every per-page verdict matching ?stage=doctype|ocr&verdict=
        correct|acceptable|wrong — the drill-down behind a dashboard score-chunk
        ring click. One row per page verdict, each linking to /review/<sha> at
        that declared context. Honors the same type filters as the dashboard."""
        qs = self._qs()
        stage = qs.get("stage") or ""
        verdict = qs.get("verdict") or ""
        if stage not in ("doctype", "ocr") or verdict not in ("correct", "acceptable", "wrong"):
            self._html(404, base("Not found",
                "<p>stage must be doctype|ocr and verdict correct|acceptable|wrong.</p>"))
            return
        declared = qs.get("declared") or None
        predicted = qs.get("predicted") or None
        oov_only = qs.get("oov_only") == "1"
        type_clause_ctx = queries.type_filter_ctx_sql(declared, predicted, oov_only)

        with connect() as conn, conn.cursor() as cur:
            cur.execute(queries.verdict_pages_sql(stage, verdict, type_clause_ctx))
            rows = cur.fetchall()
            conn.commit()

        stage_label = "Classify (doctype)" if stage == "doctype" else "OCR / ADE"
        body_rows = []
        for sha, pno, decl, v, comment, pred, pgs, txn in rows:
            href = f"/review/{quote(sha)}?declared={quote(decl)}"
            txn_cell = (f"<td class='small mono'>"
                        f"<a class='open' href='/txn/{quote(txn)}'>{esc(txn)}</a></td>"
                        if txn else "<td class='small dim'>—</td>")
            body_rows.append(
                f"<tr><td class='small mono'>{esc(sha[:16])}…</td>"
                f"{txn_cell}"
                f"<td>page {esc(pno)} / {esc(pgs)}</td>"
                f"<td>{esc(decl)}</td>"
                f"<td>{esc(pred or '—')}</td>"
                f"<td>{_verdict_pill(v)}</td>"
                f"<td class='small'>{esc(comment or '')}</td>"
                f"<td><a href='{href}'>review</a></td></tr>")

        if body_rows:
            table = ("<table><thead><tr>"
                     "<th>file (sha256)</th><th>txn_id</th><th>page</th>"
                     "<th>declared type</th><th>AI predicted</th><th>verdict</th>"
                     "<th>comment</th><th></th>"
                     "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>")
        else:
            table = (f"<div class='empty'>No {esc(verdict)} {esc(stage)} page "
                     f"verdicts yet.</div>")

        # preserve the active type filters on the way back to the dashboard
        back_qs = urlencode({k: v for k, v in
            (("declared", declared), ("predicted", predicted), ("oov_only", "1" if oov_only else None)) if v})
        back = "/dashboard?" + back_qs if back_qs else "/dashboard"
        body = (f"<h3>{esc(stage_label)} — {esc(verdict)} pages</h3>"
                f"<p class='small'>{len(rows)} page verdict(s) · "
                f"<a class='open' href='{back}'>← back to dashboard</a></p>"
                + table)
        self._html(200, base("Verdict pages", body, nav_dash="active"))

    # ===== quality score: on-demand DeQA-Doc page scoring =====
    def _quality_score(self, rest: str):
        """GET /quality/<sha>/<page> — score one page on demand (DeQA-Doc on
        GPU 4) and return {"score":…, "level":…}. Used by the review page's
        lazy pill fill. Errors return {"score": null} with 200 so the client
        can quietly leave the pill unscored."""
        from eval import quality
        parts = rest.split("/")
        if len(parts) != 2:
            self._json({"score": None, "error": "bad path"}); return
        sha, n_str = parts
        try:
            pno = int(n_str)
        except ValueError:
            self._json({"score": None, "error": "bad page"}); return
        res = quality.score_page(sha, pno)
        if res is None:
            self._json({"score": None}); return
        self._json({"score": res[0], "level": res[1]})

    # ===== classify-score: doctype confusion matrix =====
    def _classify_score(self):
        """Per-page doc_types confusion matrix (human answer vs AI answer) behind
        the dashboard's Classify score chunk. Human class = corrected_type when
        the reviewer marked the page wrong (and picked the true class), else the
        AI's own class (the human agreed). Rows = human, cols = AI. Honors the
        same type filters as the dashboard."""
        qs = self._qs()
        declared = qs.get("declared") or None
        predicted = qs.get("predicted") or None
        oov_only = qs.get("oov_only") == "1"
        type_clause_ctx = queries.type_filter_ctx_sql(declared, predicted, oov_only)

        with connect() as conn, conn.cursor() as cur:
            cur.execute(queries.confusion_sql(type_clause_ctx))
            rows = cur.fetchall()
            conn.commit()

        matrix, total, n_unlabeled = _confusion_matrix(rows)

        # class order: the slug list plus any extra slugs seen in the data on
        # either axis (extractors emit extra is_* sub-types like passport /
        # id_card beyond the 21 declared slugs — they appear as columns when
        # the AI answers them and as rows when a correction names them)
        col_slugs = list(DOC_TYPE_SLUGS)
        extra = sorted({s for pair in matrix for s in pair if s != "?"
                        and s not in col_slugs})
        col_slugs += extra
        # columns: known+seen slugs + "?" (wrong with no resolvable AI class)
        ai_cols = col_slugs + ["?"]

        def _cell(h, a):
            n = matrix.get((h, a), 0)
            if not n:
                return "<td class='cm-zero'>—</td>"
            if h == a:
                pct = min(100, max(15, round(100 * n / max(matrix.get((h, h), 0), 1))))
                return (f"<td class='cm-hit' style=\"background:color-mix(in srgb, "
                        f"var(--vd-correct) {pct}%, var(--paper))\" title='{esc(h)} → {esc(a)}: {n}'>"
                        f"{n}</td>")
            return (f"<td class='cm-miss' style=\"background:color-mix(in srgb, "
                    f"var(--vd-wrong) {min(60, 15 + 9 * n)}%, var(--paper))\" "
                    f"title='{esc(h)} → {esc(a)}: {n}'>{n}</td>")

        head = ("<tr><th class='cm-corner'>human ↓ · AI →</th>"
                + "".join(f"<th class='cm-col'>{esc(a)}</th>" for a in ai_cols)
                + "<th class='cm-total'>row</th></tr>")
        body_rows = []
        used_rows = [h for h in col_slugs
                     if any((h, a) in matrix for a in ai_cols)] or col_slugs
        for h in used_rows:
            row_total = sum(matrix.get((h, a), 0) for a in ai_cols)
            body_rows.append(
                f"<tr><th class='cm-row'>{esc(h)}</th>"
                + "".join(_cell(h, a) for a in ai_cols)
                + f"<td class='cm-total'>{row_total}</td></tr>")
        col_totals = [sum(matrix.get((h, a), 0) for h in used_rows) for a in ai_cols]
        foot = ("<tr><th class='cm-row'>col total</th>"
                + "".join(f"<td class='cm-total'>{n}</td>" if n else "<td class='cm-zero'>—</td>"
                          for n in col_totals)
                + f"<td class='cm-total'><b>{total}</b></td></tr>")

        if total or n_unlabeled:
            table = (f"<div class='cm-wrap'><table class='cm-table'><thead>{head}</thead>"
                     f"<tbody>{''.join(body_rows)}<tr class='cm-foot'>{foot}</tr></tbody></table></div>")
            legend = ("<div class='legend'>"
                      "<span class='lg-label'>cells</span>"
                      "<span class='vpill vpill-correct'>diagonal = agreed</span>"
                      "<span class='vpill vpill-wrong'>off-diagonal = confused</span>"
                      "<span class='small dim'>— = 0 · rows: human class · cols: AI class</span>"
                      "</div>")
        else:
            table = ("<div class='empty'>No docType page verdicts yet — the matrix "
                     "fills in as you mark pages Correct/Wrong on review pages.</div>")
            legend = ""

        unlabeled_note = (f"<p class='small'>{n_unlabeled} wrong-verdict page(s) "
                          f"with no resolvable class (no true doc_types flag and no "
                          f"corrected class picked) — excluded from the matrix.</p>"
                          if n_unlabeled else "")

        back_qs = urlencode({k: v for k, v in
            (("declared", declared), ("predicted", predicted),
             ("oov_only", "1" if oov_only else None)) if v})
        back = "/dashboard?" + back_qs if back_qs else "/dashboard"
        filter_note = (f" · filtered by {('OOV-only' if oov_only else 'type')}"
                       if (declared or predicted or oov_only) else "")
        body = (f"<h3>Classify score — doc_types confusion matrix (per page)</h3>"
                f"<p class='small'>{total} page verdict(s) counted{filter_note} · "
                f"<a class='open' href='{back}'>← back to dashboard</a></p>"
                + legend + table + unlabeled_note)
        self._html(200, base("Classify score", body, nav_classify="active"))

    def _petition_legacy_redirect(self, pid_str: str):
        """Old links keyed by the internal petition id still work: look the
        petition up by id and 301 to its /txn/<txn_id> page. Unknown id → 404."""
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT txn_id FROM petitions WHERE id=%s", (pid_str,))
            row = cur.fetchone()
            conn.commit()
        if row is None or row[0] is None:
            self._html(404, base("Not found", "<p>no such petition</p>")); return
        self.send_response(301)
        self.send_header("Location", f"/txn/{quote(str(row[0]))}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ===== petition detail (keyed by txn_id) =====
    def _txn_detail(self, txn_str: str):
        with connect() as conn, conn.cursor() as cur:
            # txn_id is UNIQUE; look the petition up by it (the user's real key).
            cur.execute("SELECT id, txn_id, document_no, state, raw_json FROM petitions WHERE txn_id=%s", (txn_str,))
            prow = cur.fetchone()
            if prow is None:
                self._html(404, base("Not found", "<p>no such petition (txn_id not found)</p>")); return
            pid, txn, dno, st, raw_json = prow
            cur.execute("""SELECT pf.sha256, pf.declared_category, pf.txn_id, pf.source_table,
                                  pf.source_column, pf.source_file_name,
                                  f.ai_class_status, f.ai_predicted_category, f.page_count, f.content_kind
                           FROM petition_files pf JOIN files f ON f.sha256 = pf.sha256
                           WHERE pf.petition_id=%s ORDER BY pf.declared_category, pf.sha256""", (pid,))
            files = cur.fetchall()
            conn.commit()

        # Frame 2: group the files by declared_category and render each group as
        # a bordered "Document Item" section (Thai type + (english_key) heading,
        # a rule, then 2px-bordered rows: Filename+Page · source · AI status ·
        # Human Verdict · Review). `files` is already ORDER BY declared_category.
        rows_html = _txn_grouped_rows(files)

        # Request Body button is shown only when raw_json is present (the 7,000
        # petitions that came from the GET mock). The 2,512 CSV-only stubs get a
        # small "no request body" note instead. The modal is built client-side
        # by ck-body.js (in base.html) on click; data-txn parameterizes the fetch.
        has_body = raw_json is not None
        if has_body:
            body_btn = (f"<button id='ckBodyBtn' class='ck-body-btn' data-txn='{esc(txn)}'>"
                        f"📋 Request body</button>"
                        f"<div id='ckModalMount'></div>")
        else:
            body_btn = "<span class='small dim'>no request body (CSV-only petition)</span>"

        meta = (f"<b>txn_id</b> <span class='mono'>{esc(txn) if txn else '—'}</span>"
                + (f"<button class='cp' data-copy='{esc(txn)}' title='copy txn_id'>⧉</button>" if txn else "")
                + f" · <b>petition id</b> <span class='mono small'>{esc(str(pid))}</span>"
                + f" · document_no={esc(dno or '—')} · state={esc(st or '—')} · {len(files)} file(s)")
        body = render("petition.html", meta=meta, rows="".join(rows_html), body_btn=body_btn)
        self._html(200, base(f"TXN {txn}" if txn else f"Petition {dno or txn_str[:8]}",
                             body, nav_home="active"))

    # ===== request body JSON (modal payload, lazy-built on click) =====
    def _body_json(self, txn_str: str):
        from eval import body
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, txn_id, document_no, state, raw_json FROM petitions WHERE txn_id=%s", (txn_str,))
            prow = cur.fetchone()
            conn.commit()
        if prow is None:
            self._json({"error": "no such petition"}, 404); return
        rb = body.build_request_body(prow)
        if rb is None:
            self._json({"error": "no request body"}, 404); return
        self._json(rb)

    # ===== review page =====
    def _review(self, rest: str):
        # rest == "<sha>?declared=..." — query parsed separately
        sha = rest.split("?", 1)[0]
        qs = self._qs()
        declared = qs.get("declared")
        if not declared:
            self._html(400, base("Bad request", "<p>?declared= is required</p>")); return

        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT local_path, content_kind, page_count, ai_class_status,
                                  ai_predicted_category
                           FROM files WHERE sha256=%s""", (sha,))
            f = cur.fetchone()
            if f is None:
                self._html(404, base("Not found", "<p>no such file</p>")); return
            local_path, kind, pgs, aist, pred = f
            # NOTE: the 19-way file-class verdict block + its two-stage gate were
            # removed. Page (OCR/extract) verdicts no longer require a class verdict
            # first; the per-page doc_types classification is shown inline instead.
            # existing page verdicts, split by stage. Two independent checks per
            # page: doctype (doc_types correct? True/False) and ocr (extracted data
            # correct? Correct/Acceptable/Wrong). Each keyed by page_no.
            cur.execute("""SELECT page_no, stage, verdict, comment, corrected_type FROM verdicts
                           WHERE sha256=%s AND declared_category=%s AND page_no IS NOT NULL
                           ORDER BY page_no""", (sha, declared))
            doctype_verdicts: dict[int, tuple[str, str, str | None]] = {}
            ocr_verdicts: dict[int, tuple[str, str, str | None]] = {}
            for pno, stage, verdict, comment, corrected in cur.fetchall():
                (doctype_verdicts if stage == 'doctype' else ocr_verdicts)[pno] = (
                    verdict, comment, corrected)
            # page extract results — per-(sha, declared, page) JSON from the real
            # per-filetype extractor. Declared-scoped: a file appears under
            # multiple declared contexts (petition_files), each with its own
            # extractor run, so the rows are filtered to THIS context.
            cur.execute("""SELECT page_no, ai_extract_status, ai_extract_json,
                                  ai_extract_error, ai_extract_latency_s
                           FROM file_extracts
                           WHERE sha256=%s AND declared_category=%s
                           ORDER BY page_no""", (sha, declared))
            pages = cur.fetchall()
            # every context this file appears in: (petition id, txn_id,
            # declared_category, source_file_name), so the reviewer sees the full
            # txn_id(s) and can switch contexts. source_file_name feeds the
            # Frame-3 heading block's "filename" line.
            cur.execute("""SELECT pf.petition_id::text, pf.txn_id::text, pf.declared_category,
                                  pf.source_file_name
                           FROM petition_files pf WHERE pf.sha256=%s
                           ORDER BY pf.declared_category""", (sha,))
            contexts = cur.fetchall()
            conn.commit()

        # OOV declared types can never match the classifier output (they're outside
        # its vocabulary); flagged in the heading, not auto-compared.
        is_oov = queries.is_oov(declared)

        # One Document Section card per page. Each card shows: the extract JSON,
        # then the per-page doc_types classification block, then TWO independent
        # per-page verdict forms — DocType (True/False) and OCR/ADE
        # (Correct/Acceptable/Wrong). The 19-way file-class block and its
        # two-stage gate were removed — both page verdicts save directly.
        if pages:
            # stored DeQA-Doc quality scores for this file's pages (keyed
            # (sha, page_no) on file_pages — context-independent), rendered as
            # pills in each card header; unscored pages fill lazily via /quality
            from eval import quality as _quality
            with connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT page_no, quality_score, quality_level "
                            "FROM file_pages WHERE sha256=%s AND quality_score IS NOT NULL",
                            (sha,))
                q_scores = {pno: (s, lv) for pno, s, lv in cur.fetchall()}
                conn.commit()
            sections = []
            for page in pages:
                pno = page[0]
                sections.append(_page_section(
                    sha, declared, pno, page,
                    doctype_verdicts.get(pno), ocr_verdicts.get(pno),
                    q_scores.get(pno)))
            page_sections = "".join(sections)
        else:
            # No extract pages for this context: either the declared type has no
            # production extractor (no-extractor context — file_extracts has no
            # rows for it), or the file isn't renderable (page_count=0). Nothing
            # to show here without a page/extract.
            page_sections = ("<div class='doc-section review-card'>"
                             "<div class='doc-section-h'>No extract pages</div>"
                             "<div class='page-body'>"
                             "<p class='small'>no extract pages for this context "
                             "(no extractor for this declared type, or content not renderable).</p>"
                             "</div></div>")

        # Every petition context this file appears in, with the full txn_id for each
        # (the main thing the user needs to identify the source petition). The txn_id
        # links to /txn/<txn_id> (the primary route key); the internal id is small
        # secondary. The current declared context is highlighted; the others link
        # to review under their own type.
        ctx_links = []
        # the current context (matching declared) — its txn_id + source_file_name
        # drive the Frame-3 heading block; fall back to the first context.
        cur_ctx = next((c for c in contexts if c[2] == declared), contexts[0] if contexts else None)
        head_txn = cur_ctx[1] if cur_ctx else None
        head_filename = cur_ctx[3] if cur_ctx else None
        for pid, txn, dcat, sfn in contexts:
            review_link = f"/review/{quote(sha)}?declared={quote(dcat)}"
            txn_link = (f"<a class='open' href='/txn/{quote(txn)}'><span class='mono'>{esc(txn)}</span></a>"
                        if txn else "<span class='mono dim'>—</span>")
            label = (f"{txn_link}"
                     f" · <span class='small dim'>id {esc(str(pid)[:8])}…</span>"
                     f" · <a class='open' href='{review_link}'>declared={esc(dcat)}</a>")
            cur_flag = ' <b>(this context)</b>' if dcat == declared else ''
            ctx_links.append(f"<li>{label}{cur_flag}</li>")
        contexts_html = (
            f"<p class='small'>sha256: <span class='mono'>{esc(sha)}</span><br>"
            f"local_path: {esc(local_path)}<br>"
            f"appears in {len(contexts)} context(s):</p>"
            f"<ul class='small'>{''.join(ctx_links)}</ul>")

        # Frame-3 heading block: TXN <uuid> is the page <h2> (passed to base());
        # the declared type (Thai + key) + filename render as 24px lines here.
        head_declared = (f"{esc(_declared_thai_label(declared))}"
                         f" <span class='dim'>({esc(declared)})</span>"
                         + (" <span class='pill pill-oov'>OOV</span>" if is_oov else ""))
        head_filename = esc(head_filename) if head_filename else esc(local_path or "—")
        review_heading = (
            f"<div class='review-heading'>"
            f"<div class='rh-declared'>{head_declared}</div>"
            f"<div class='rh-filename'>{head_filename}</div>"
            f"</div>")

        body = render("review.html",
            breadcrumbs=f"<a href='/'>dashboard</a> · review",
            review_heading=review_heading,
            declared=esc(declared),
            legend=LEGEND_HTML,
            stale_pages_note="",   # 19-way class block removed; no stale-page banner
            page_sections=page_sections,
            contexts=contexts_html,
            sha=quote(sha),
        )
        self._html(200, base(f"TXN {head_txn}" if head_txn else f"Review {sha[:12]}", body))

    # ===== page PNG =====
    def _page_png(self, rest: str):
        from eval.render import render_page
        parts = rest.split("/")
        if len(parts) != 2:
            self._html(404, base("Not found", "<p>bad page path</p>")); return
        sha, n_str = parts
        try:
            n = int(n_str)
        except ValueError:
            self._html(404, base("Not found", "<p>bad page number</p>")); return
        try:
            path = render_page(sha, n)
            self._send(200, path.read_bytes(), "image/png")
        except FileNotFoundError:
            self._html(404, base("Not found", "<p>no such file</p>"))
        except Exception as exc:
            log.warning("render %s/%s failed: %s", sha, n, exc)
            self._html(500, base("Render error", f"<pre>{esc(exc)}</pre>"))

    # ===== POST /verdict =====
    def _post_verdict(self):
        form = self._form()
        sha = form.get("sha")
        declared = form.get("declared")
        verdict = form.get("verdict")
        page_no = form.get("page_no")          # required now — every verdict is per-page
        stage = form.get("stage") or "ocr"     # 'doctype' (True/False) | 'ocr' (Correct/...)
        comment = form.get("comment") or None
        corrected_type = form.get("corrected_type") or None
        if not sha or not declared or not verdict or not page_no:
            self._html(400, base("Bad request",
                "<p>missing sha/declared/verdict/page_no</p>")); return
        if stage not in ("doctype", "ocr"):
            self._html(400, base("Bad request", "<p>bad stage</p>")); return
        # corrected_type only applies to a doctype 'wrong' verdict (the true
        # doc_types slug the reviewer picked); it's cleared otherwise. Validated
        # as a bare slug (lowercase/digits/underscore) so a stale or garbage
        # form value can't land — the class space is wider than the 21 declared
        # slugs because extractors emit extra is_* sub-types (passport,
        # id_card, …), which the matrix adds as extra rows/cols on sight.
        if not (stage == "doctype" and verdict == "wrong"
                and corrected_type and re.fullmatch(r"[a-z0-9_]{1,64}", corrected_type)):
            corrected_type = None

        page_n = int(page_no)

        with connect() as conn, conn.cursor() as cur:
            # Two independent per-page verdicts, one row each:
            #   stage='doctype' : is the doc_types classification correct?  (correct|wrong)
            #   stage='ocr'     : is the extracted data correct?            (correct|acceptable|wrong)
            # The old "page_no NULL = 19-way class verdict" model was removed; page_no
            # is now always required. Uniqueness is enforced by two stage-scoped partial
            # unique indexes sharing the key columns (sha256, declared_category, page_no)
            # — ON CONFLICT names the matching one via its partial predicate so the two
            # rows for the same page (one per stage) can't collide. `stage` is validated
            # above to exactly 'doctype'|'ocr', so inlining it as a SQL literal is safe.
            cur.execute(f"""INSERT INTO verdicts (sha256, declared_category, page_no, stage, verdict,
                                                 corrected_type, comment, annotator)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (sha256, declared_category, page_no) WHERE page_no IS NOT NULL
                                AND stage = '{stage}'
                           DO UPDATE SET verdict=EXCLUDED.verdict,
                                        corrected_type=EXCLUDED.corrected_type,
                                        comment=EXCLUDED.comment, created_at=now()""",
                        (sha, declared, page_n, stage, verdict, corrected_type,
                         comment, "web"))
            conn.commit()

        # back to the review page
        self._redirect(f"/review/{quote(sha)}?declared={quote(declared)}")

    # ===== POST /review/<sha>/rerun =====
    def _post_rerun(self, sha: str):
        # Rate-limit per file: no re-run within 30s of the last one for this sha256.
        # Tracked in-memory (single-server deployment) so it survives the reset itself
        # (ai_class_at is nulled by the reset, so it can't double as the throttle stamp).
        with _RERUN_LOCK:
            last = _RERUN_AT.get(sha)
            now_mono = time.monotonic()
            if last is not None and (now_mono - last) < RERUN_COOLDOWN_S:
                remaining = int(RERUN_COOLDOWN_S - (now_mono - last))
                self._html(429, base("Rate limited",
                    f"<p>Re-run throttled for this file ({remaining}s remaining). "
                    f"<a href='/review/{quote(sha)}'>back</a></p>"))
                return
            _RERUN_AT[sha] = now_mono

        with connect() as conn, conn.cursor() as cur:
            # one-transaction reset
            cur.execute("DELETE FROM verdicts WHERE sha256=%s", (sha,))
            cur.execute("""UPDATE files SET ai_class_status='pending', ai_predicted_category=NULL,
                               ai_class_raw=NULL, ai_class_error=NULL, ai_class_latency_s=NULL,
                               ai_class_model=NULL, ai_class_at=NULL WHERE sha256=%s""", (sha,))
            cur.execute("""UPDATE file_pages SET ai_ocr_status='pending', ai_ocr_text=NULL,
                               ai_ocr_raw=NULL, ai_ocr_error=NULL, ai_ocr_latency_s=NULL,
                               ai_ocr_model=NULL, ai_ocr_at=NULL WHERE sha256=%s""", (sha,))
            cur.execute("""UPDATE file_extracts SET ai_extract_status='pending',
                               ai_extract_json=NULL, ai_extract_error=NULL,
                               ai_extract_latency_s=NULL, ai_extract_model=NULL,
                               ai_extract_at=NULL WHERE sha256=%s""", (sha,))
            conn.commit()

        # auto-start worker if idle so the re-run isn't stranded
        st = WORKER.status()
        if st["state"] == "idle":
            WORKER.start()
        declared = self._form().get("declared")
        target = f"/review/{quote(sha)}" + (f"?declared={quote(declared)}" if declared else "")
        self._redirect(target)

    # ===== POST /index (re-index button) =====
    def _post_index(self):
        from eval.index import run
        run()
        self._redirect("/")

    # ===== POST /run/retry_errors (bulk retry after a backend outage) =====
    def _post_retry_errors(self):
        """Reset every errored file (class error, or any page error) back to pending
        and clear its verdicts, then auto-start the worker if idle. This is the bulk
        analog of the per-file /review/<sha>/rerun: after the LLM endpoint recovers,
        a Start/Continue won't reclaim error files with no pending pages (their pages
        are errored too, so the claim query sees nothing to do). This button re-queues
        them. Errored human verdicts are cleared so they're re-reviewed against fresh AI.
        """
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT f.sha256 FROM files f
                           WHERE f.ai_class_status='error'
                              OR EXISTS (SELECT 1 FROM file_extracts x
                                         WHERE x.sha256=f.sha256 AND x.ai_extract_status='error')""")
            shas = [r[0] for r in cur.fetchall()]
            if shas:
                cur.execute("DELETE FROM verdicts WHERE sha256 = ANY(%s)", (shas,))
                cur.execute("""UPDATE files SET ai_class_status='pending', ai_predicted_category=NULL,
                                   ai_class_raw=NULL, ai_class_error=NULL, ai_class_latency_s=NULL,
                                   ai_class_model=NULL, ai_class_at=NULL
                               WHERE sha256 = ANY(%s)""", (shas,))
                cur.execute("""UPDATE file_pages SET ai_ocr_status='pending', ai_ocr_text=NULL,
                                   ai_ocr_raw=NULL, ai_ocr_error=NULL, ai_ocr_latency_s=NULL,
                                   ai_ocr_model=NULL, ai_ocr_at=NULL
                               WHERE sha256 = ANY(%s)""", (shas,))
                cur.execute("""UPDATE file_extracts SET ai_extract_status='pending',
                                   ai_extract_json=NULL, ai_extract_error=NULL,
                                   ai_extract_latency_s=NULL, ai_extract_model=NULL,
                                   ai_extract_at=NULL
                               WHERE sha256 = ANY(%s)""", (shas,))
            conn.commit()
        log.info("retry_errors reset %d errored files", len(shas))
        st = WORKER.status()
        if st["state"] == "idle" and shas:
            WORKER.start()
        self._redirect("/")


# --- form helpers (server-side rendered) ---
# (The 19-way file-class form helper _class_form was removed along with the
# two-stage gate. Page-verdict forms are rendered inline in _page_section.)


# The 21 extractor doc_types slugs (eval.ai.extract.SLUG_TO_FN keys — see
# filetype-list.md). Row/column order for the /classify-score confusion matrix
# and the validation set for verdicts.corrected_type. is_* keys in an extract
# JSON map onto these by stripping the 'is_' prefix (is_passport→passport etc.
# per extractor; slugs that don't appear as is_* keys simply never light up).
DOC_TYPE_SLUGS: tuple[str, ...] = (
    "juristic", "land_map", "factory_location_map", "poa_revenue_stamp",
    "attchment", "name_change", "production_diagram", "building_diagram",
    "machine_diagram", "land_doc", "consent", "house_registration",
    "engineer_license", "safety_cert", "building_plan", "waste",
    "emissions", "factory_operation_risk", "environmental_risk", "eia", "iee",
)


def _ai_doc_type_slug(ejson) -> str | None:
    """The page's AI doc_types answer: the (first) is_* key that is true in the
    extract JSON, as a slug (is_id_card → 'id_card'). Uses the same nested/
    top-level parsing as _doc_types_block. None when no flag is true (all-false
    or malformed JSON) — such pages can't be placed in an AI column."""
    if not isinstance(ejson, dict):
        return None
    nested = ejson.get("doc_types")
    src = nested if isinstance(nested, dict) else ejson
    for k, v in src.items():
        if k.startswith("is_") and v:
            return k[3:]
    return None


def _confusion_matrix(rows) -> tuple[dict, int, int]:
    """Build the {(human_slug, ai_slug): count} matrix from CONFUSION_SQL rows
    (sha, page_no, verdict, corrected_type, ai_extract_json).

    human class: verdict='wrong' + corrected_type → the reviewer's slug;
    verdict='correct' (or a wrong with no corrected_type yet, which falls back
    anyway) → the AI's own slug (the human agreed with it). corrected_type is
    pre-validated at POST time as a bare slug, so it's trusted here. Rows whose
    AI slug can't be resolved (no true is_* flag) land in the '?' column when
    the human class is known, else are skipped. Returns (matrix,
    total_counted, n_unplaced) where n_unplaced counts pages with neither a
    human nor an AI class."""
    matrix: dict[tuple[str, str], int] = {}
    total = 0
    n_unplaced = 0
    for _sha, _pno, verdict, corrected, ejson in rows:
        ai_slug = _ai_doc_type_slug(ejson)
        if verdict == "wrong" and corrected:
            human = corrected
        elif ai_slug is not None:
            human = ai_slug
        else:
            n_unplaced += 1
            continue
        col = ai_slug if ai_slug is not None else "?"
        matrix[(human, col)] = matrix.get((human, col), 0) + 1
        total += 1
    return matrix, total, n_unplaced


def _doc_types_block(ejson, sha: str, declared: str, pno: int,
                     doctype_verdict: tuple | None) -> str:
    # doctype_verdict is (verdict, comment, corrected_type) — comment/corrected
    # are unused here; the form only reflects the chosen verdict + correction.
    """The per-page "Classify Doctype" block: pull the page's doc_types out of the
    extract JSON and render each is_* flag as a yes/no chip, then put the DocType
    Correct/Wrong verdict form RIGHT THERE under the chips — so the reviewer
    sees the chips, looks at the page image, decides "oh it really is this
    docType" and clicks Correct (or Wrong) in place, then proceeds down to the
    OCR/ADE form.

    The attchment extractor nests its flags under a 'doc_types' dict
    ({is_passport, is_id_card, is_house_registration}); some other extractors put
    is_* at the top level. So prefer ejson['doc_types'] when it's a dict, else
    fall back to top-level is_* keys. Returns '' when there are no is_* keys
    (a non-classifying extract or a failed run) — in which case NO doctype form
    is rendered either, mirroring _PAGE_HAS_DOCTYPE in queries.py (a doctype
    verdict is required only for pages that show chips).

    The Correct/Wrong form is its own <form class='verdict-form'> POSTing
    stage='doctype', so the generic .verdict-btn click handler (base.html, which
    keys off .verdict-form / input[name='verdict'] per form) and _post_verdict
    work unchanged. 'correct' = "the doc_types is right", 'wrong' = "it's not".
    """
    if not isinstance(ejson, dict):
        return ""
    nested = ejson.get("doc_types")
    src = nested if isinstance(nested, dict) else ejson
    pairs = []
    for k, v in src.items():
        if not k.startswith("is_"):
            continue
        # Coerce to bool truthiness but keep the raw value as a title tooltip so
        # a malformed non-bool answer is visible, not silently coerced.
        raw = "true" if v else "false"
        mark = "✓" if v else "✗"
        cls = "dt-yes" if v else "dt-no"
        label = k[3:].replace("_", " ")  # is_id_card -> "id card"
        pairs.append((label, mark, cls, raw))
    if not pairs:
        return ""
    chips = "".join(
        f"<span class='doctype-chip {cls}' title='{esc(label)}: {esc(raw)}'>"
        f"{esc(mark)} {esc(label)}</span>"
        for label, mark, cls, raw in pairs
    )
    # DocType verdict (Correct/Wrong) inline with the chips. Only reached when
    # chips render (we're past the `if not pairs` guard), so pages with no
    # doc_types get no doctype form — matching _PAGE_HAS_DOCTYPE in queries.py.
    dt_v = doctype_verdict
    dt_correct = 'sel' if (dt_v and dt_v[0] == 'correct') else ''
    dt_wrong = 'sel' if (dt_v and dt_v[0] == 'wrong') else ''
    dt_val = dt_v[0] if dt_v else ''
    dt_corrected = dt_v[2] if (dt_v and dt_v[0] == 'wrong') else None
    # corrected-type select: only meaningful with a Wrong verdict (records the
    # TRUE class for the /classify-score confusion matrix). It rides along in
    # the same form — the auto-save handler POSTs the whole FormData, and
    # _post_verdict ignores it unless verdict=wrong. Options = the 21 known
    # slugs PLUS this page's own chip keys (extractors emit extra is_* sub-types
    # like passport/id_card that aren't declared slugs but are exactly what a
    # wrong-verdict correction would name).
    page_keys = [label.replace(" ", "_") for label, _m, _c, _r in pairs]
    option_slugs = list(DOC_TYPE_SLUGS) + sorted(
        k for k in page_keys if k not in DOC_TYPE_SLUGS)
    corrected_opts = "".join(
        f"<option value='{esc(s)}' {'selected' if s == dt_corrected else ''}>{esc(s)}</option>"
        for s in option_slugs)
    form = (
        f"<form method='post' action='/verdict' class='verdict-form doctype-verdict-form'>"
        f"<input type='hidden' name='sha' value='{esc(sha)}'>"
        f"<input type='hidden' name='declared' value='{esc(declared)}'>"
        f"<input type='hidden' name='page_no' value='{pno}'>"
        f"<input type='hidden' name='stage' value='doctype'>"
        f"<div class='verdict-btns'>"
        f"<button type='button' class='verdict-btn verdict-correct {dt_correct}' data-verdict='correct'>✓ Correct</button>"
        f"<button type='button' class='verdict-btn verdict-wrong {dt_wrong}' data-verdict='wrong'>✗ Wrong</button>"
        f"<label class='corrected-row'>if wrong, true doc_type is:"
        f"<select name='corrected_type'>"
        f"<option value='' {'selected' if not dt_corrected else ''}>(unlabeled)</option>"
        f"{corrected_opts}</select></label>"
        f"</div>"
        f"<input type='hidden' name='verdict' class='page_verdict' value='{esc(dt_val)}'>"
        f"</form>"
    )
    return (f"<div class='doc-types-block'>"
            f"<div class='ocr-label'>Classify Doctype</div>"
            f"<div class='doctype-chips'>{chips}</div>"
            f"{form}"
            f"</div>")


def _page_section(sha: str, declared: str, pno: int, ocr_row: tuple,
                  doctype_verdict: tuple | None, ocr_verdict: tuple | None,
                  quality: tuple[float, str] | None = None) -> str:
    """One "Document Section" card: page image on the left, results + controls on
    the right.

    The right-hand body is, in order:
      1. Extract JSON  — the real per-filetype extractor's per-page dict
         (is_X + data, + ocr_text for the 6 fulltext slugs), pretty-printed in a
         scrolling <pre class='extract-json'>.
      2. Classify Doctype block — the page's doc_types (is_*) pulled out of the
         JSON and shown as yes/no chips, with the DocType Correct/Wrong verdict
         form right under the chips (so the reviewer judges "is this docType
         right?" in place, looking at the chips + page image). The form POSTs
         stage='doctype' ('correct'=right / 'wrong'=not). It renders ONLY when
         chips render, i.e. only for pages whose extract has an is_* doc_type —
         pages with no doc_types (e.g. waste_document) get no doctype form.
      3. OCR / ADE verdict — is the extracted data correct? Correct/Acceptable/Wrong
         + comment (POSTs stage='ocr'). No two-stage gate: both verdicts save
         independently and directly.

    ocr_row is a file_extracts row: (page_no, ai_extract_status, ai_extract_json,
    ai_extract_error, ai_extract_latency_s). ai_extract_json is a dict (psycopg
    decodes jsonb); rotated_base64 was stripped before storage. doctype_verdict /
    ocr_verdict are (verdict, comment) tuples for the matching stage, or None."""
    _rpno, ostatus, ejson, oerr, olat = ocr_row
    img = f"<img src='/page/{quote(sha)}/{pno}' class='zoomable' data-page='{pno}'>"
    is_fulltext = isinstance(ejson, dict) and "ocr_text" in ejson
    if ostatus == "done" and ejson:
        result_label = "OCR text + classification" if is_fulltext else "Extract (JSON)"
        result_box = f"<pre class='extract-json'>{esc(json.dumps(ejson, indent=2, ensure_ascii=False))}</pre>"
    else:
        result_label = "Extract"
        result_box = (f"<div class='ocr-text'><p class='small' style='margin:0'>"
                      f"<span class='{_status_dot(ostatus)}'>{esc(ostatus)}</span>"
                      + (f" — {esc(oerr[:120])}" if oerr else "") + "</p></div>")

    # Classify Doctype chips + inline DocType Correct/Wrong form (renders only
    # when the extract has doc_types is_* flags — see _doc_types_block).
    doc_types_html = _doc_types_block(ejson, sha, declared, pno, doctype_verdict) if ostatus == "done" else ""

    # doctype header pill (the form itself is inside doc_types_html).
    dt_v = doctype_verdict
    dt_label = _verdict_pill(dt_v[0]) if dt_v else "<span class='small'>not yet</span>"

    # OCR / ADE verdict (Correct/Acceptable/Wrong) — the existing 3-way check.
    oc_v = ocr_verdict
    oc_label = _verdict_pill(oc_v[0]) if oc_v else "<span class='small'>not yet</span>"
    oc_correct = 'sel' if (oc_v and oc_v[0] == 'correct') else ''
    oc_accept = 'sel' if (oc_v and oc_v[0] == 'acceptable') else ''
    oc_wrong = 'sel' if (oc_v and oc_v[0] == 'wrong') else ''
    oc_val = oc_v[0] if oc_v else ''

    # DeQA-Doc quality pill: stored score renders immediately; unscored pages
    # get a placeholder span the client JS fills via GET /quality/<sha>/<pno>
    # (the endpoint scores on demand and caches in file_pages).
    if quality:
        q_pill = (f"<span class='qpill q-{quality[1]}' "
                  f"title='DeQA-Doc page quality (1-5)'>◆ {quality[0]:.2f} {esc(quality[1])}</span>")
    else:
        q_pill = (f"<span class='qpill q-pending' data-quality-sha='{esc(sha)}' "
                  f"data-quality-page='{pno}' "
                  f"title='DeQA-Doc page quality (1-5)'>◆ …</span>")

    header = (f"Page {pno} · Extract <span class='{_status_dot(ostatus)}'>{esc(ostatus)}</span> "
              f"({round(olat, 2) if olat else '—'}s)"
              f" · quality: {q_pill} · doctype: {dt_label} · ocr: {oc_label}")

    return f"""
    <div class="doc-section review-card">
      <div class="doc-section-h">{header}</div>
      <div class="page-row" style="border:none;padding:0;margin:0;align-items:stretch">
        <div class="page-img"><div class="doc-preview" style="padding:6px;border:2px solid var(--rule-strong)"><div>{img}</div></div></div>
        <div class="page-body">
          <div class="ocr-label">{result_label}</div>
          {result_box}
          {doc_types_html}
          <div class="ocr-label">OCR / ADE verdict — is the extracted data correct?</div>
          <form method="post" action="/verdict" class="verdict-form">
            <input type="hidden" name="sha" value="{esc(sha)}">
            <input type="hidden" name="declared" value="{esc(declared)}">
            <input type="hidden" name="page_no" value="{pno}">
            <input type="hidden" name="stage" value="ocr">
            <div class="verdict-btns">
              <button type="button" class="verdict-btn verdict-correct {oc_correct}" data-verdict="correct">✓ Correct</button>
              <button type="button" class="verdict-btn verdict-acceptable {oc_accept}" data-verdict="acceptable">~ Acceptable</button>
              <button type="button" class="verdict-btn verdict-wrong {oc_wrong}" data-verdict="wrong">✗ Wrong</button>
            </div>
            <input type="hidden" name="verdict" class="page_verdict" value="{esc(oc_val)}">
            <label class="comment-row">Comment
              <textarea name="comment" rows="1" placeholder="notes…"></textarea>
            </label>
          </form>
        </div>
      </div>
    </div>"""


def _txn_grouped_rows(files: list) -> str:
    """Frame 2 layout: group the petition's files by declared_category and
    render each group as a bordered "Document Item" section — a 24px heading of
    the declared type (Thai label + (english_key) when available), a hairline
    rule, a small column header, then one 2px-bordered row per file with
    columns: Filename+Page · source (table/column) · AI status · Human Verdict ·
    Review. `files` rows: (sha, decl, ftxn, stbl, scol, sfn, aist, pred, pgs,
    kind)."""
    groups: dict[str, list] = {}
    for sha, decl, ftxn, stbl, scol, sfn, aist, pred, pgs, kind in files:
        groups.setdefault(decl or "—", []).append(
            (sha, decl, ftxn, stbl, scol, sfn, aist, pred, pgs, kind))

    COLS = ("<div class='doc-group-head'>"
            "<div>Filename + Page</div>"
            "<div>source (table / column)</div>"
            "<div>AI status</div>"
            "<div>Human verdict</div>"
            "<div>Review</div>"
            "</div>")

    sections = []
    for decl, group in groups.items():
        thai = _declared_thai_label(decl)
        head = (f"<div class='doc-section-h'>{esc(thai)}"
                f" <span class='en'>({esc(decl)})</span></div>"
                f"<hr class='doc-rule'>{COLS}")
        rows = []
        for sha, _decl, ftxn, stbl, scol, sfn, aist, pred, pgs, kind in group:
            cur_verdict = _review_coverage_pill(sha, decl)
            href = f"/review/{quote(sha)}?declared={quote(decl)}"
            name_line = (f"<span class='mono'>{esc(sha[:16])}…</span>"
                         f"<br><span class='small'>{esc(sfn or '—')} ({esc(kind)}, {pgs}p)</span>")
            src_line = (f"{esc(stbl or '—')}"
                        f"<br><span class='small'>{esc(scol or '—')}</span>")
            rows.append(
                f"<div class='doc-row'>"
                f"<div>{name_line}</div>"
                f"<div class='small'>{src_line}</div>"
                f"<div><span class='{_status_dot(aist)}'>{esc(aist)}</span>"
                f"<br><span class='small'>{esc(pred or '—')}</span></div>"
                f"<div>{cur_verdict}</div>"
                f"<div><a href='{href}'>Review</a></div>"
                f"</div>")
        sections.append(f"<div class='doc-section'>{head}{''.join(rows)}</div>")
    if not sections:
        return "<div class='empty'>no files for this petition.</div>"
    return "".join(sections)


def _declared_thai_label(declared: str | None) -> str:
    """Best-effort Thai label for a declared_category english key. Reuses the
    ATTACH_FILETYPE map (declared_category → Thai filetype description) from
    eval.ai.build_dataset; falls back to the raw declared string so the heading
    always shows something."""
    if not declared:
        return "—"
    try:
        from eval.ai.build_dataset import ATTACH_FILETYPE
    except Exception:
        return declared
    return ATTACH_FILETYPE.get(declared, declared)


def _review_coverage_pill(sha: str, declared: str) -> str:
    """A small page-coverage pill for the /txn listing's 'Human verdict' cell:
    how far the (sha, declared) context is through its two per-page checks.
    A context is 'done' when every file_extracts page has BOTH a doctype and an
    ocr verdict; 'reviewing' when some but not all pages are fully covered; '—'
    when nothing is verdicted yet (or the context has no extract pages — nothing
    to grade). Replaces the old _class_verdict_label, which read the removed
    page_no IS NULL class row."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM file_extracts
                       WHERE sha256=%s AND declared_category=%s""", (sha, declared))
        n_pages = cur.fetchone()[0]
        if not n_pages:
            conn.commit()
            return "<span class='small dim'>—</span>"
        # file_extracts pages that have BOTH a doctype and an ocr verdict.
        cur.execute("""SELECT count(*) FROM file_extracts x
                       WHERE x.sha256=%s AND x.declared_category=%s
                         AND EXISTS (SELECT 1 FROM verdicts v
                                     WHERE v.sha256=x.sha256
                                       AND v.declared_category=x.declared_category
                                       AND v.page_no=x.page_no AND v.stage='doctype')
                         AND EXISTS (SELECT 1 FROM verdicts v
                                     WHERE v.sha256=x.sha256
                                       AND v.declared_category=x.declared_category
                                       AND v.page_no=x.page_no AND v.stage='ocr')""",
                    (sha, declared))
        n_done = cur.fetchone()[0]
        conn.commit()
    if n_done >= n_pages:
        return "<span class='vpill vpill-correct'>✓ done</span>"
    if n_done > 0:
        return f"<span class='vpill vpill-acceptable'>~ reviewing ({n_done}/{n_pages})</span>"
    return "<span class='small dim'>—</span>"


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = config.HTTP_PORT
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("eval server on http://localhost:%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        if WORKER.proc and WORKER.proc.poll() is None:
            WORKER.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
