# evalutate — E-License Classification + OCR Evaluation

Measures how well **document classification** and **OCR** work on e-License petition
attachments. A plain HTTP server (Python stdlib, no web framework) serves a petition
list (the landing page), a dashboard (worker controls + status filters), and a
per-document two-stage review (classify, then OCR per page). A separate AI worker
process (spawned by the server) classifies page 1 and OCRs every page of each unique
file. A second quality worker batch-scores every page image with DeQA-Doc (1–5
document quality). See `PLAN.md` for the full design.

The AI primitives are vendored (copied) from `wind/doc_validator_webui`:
classification prompt/parser + per-page OCR + the OpenAI-compatible client. This
repo depends on wind only as a one-time copy source, not at runtime.

## Prerequisites

- `docker` + `docker compose` (for Postgres 15)
- `uv` (runs the Python)
- The local attachment corpus — the S3-bucket mirror of petition PDFs/images.
  Set `DOC_ROOT` in `.env`; on this box
  `/home/user/aiProject/elicenseDocuments/miid-attachment-prod/`. `filtered.csv`
  (in this repo) lists each file by name; the indexer rebases those names under
  `DOC_ROOT`, so the CSV's machine-specific `local_file_path` column is ignored.
- The GET-mock JSON tree (petition metadata) — set `MOCK_ROOT` in `.env`; on
  this box `…/แกะapiระบบElicense-ดึงจากDB` under `/home/user/…`.
- Province/district/subdistrict master-data JSONs for the request-body modal —
  set `GEO_MASTER_DIR` in `.env` (`…/Elicense-network-collection`).
- The raw-petition browser (elicense-db-ui) for the globe / Elicense links —
  set `PETITION_API_BASE` in `.env` (default `http://localhost:8765`). Its
  `manifest.jsonl` inside `MOCK_ROOT` supplies the txn_id → `GET_*.json`
  filename mapping those links use.
- A reachable OpenAI-compatible LLM endpoint (`LLM_ENDPOINT` / `MODEL_API_KEY` /
  `MODEL_NAME` in `.env`). Default points at `localhost:4000`.

> The code defaults in `eval/config.py` point at `/home/admins/…` (the box the
> project was authored on). This machine has no `admins` user, so `.env`
> overrides `MOCK_ROOT`, `DOC_ROOT`, and `GEO_MASTER_DIR` to the `/home/user/…`
> equivalents.

## One-time setup

```bash
cd /home/user/aiProject/dev/rain/document_classifier_evaluate_qwen

# 1) Install Python deps
uv sync

# 2) Bring up Postgres (host port 5435 → container 5432)
docker compose up -d

# 3) Create the per-model database + apply the schema
uv run switch_model.py

# 4) Load the index (CSV files + GET-mock JSON petitions)
uv run eval-index
# Expect: files ≈ 37,678 · petitions ≈ 9,512 · file_pages ≈ 286,906 · petition_files ≈ 37,678
# (~14 corrupt PDFs fail to open and degrade to content_kind='other' — expected, not an error.)
```

`eval-index` is idempotent (`ON CONFLICT … DO UPDATE/NOTHING`, plus a pre-DELETE
that clears stale `txn_id` stub rows); re-running is safe. File paths are rebased
under `DOC_ROOT` on every run, so the corpus can move without regenerating
`filtered.csv`. First run takes ~4–5 min (PyMuPDF opens ~37k files).

## Run the app (inside tmux, so it survives a dropped SSH)

```bash
tmux new-session -s evaluate_classify_ocr
# inside the session:
HTTP_PORT=8082 uv run python -m eval.server         # the review UI + dashboard (spawns the worker as its child)
```
```bash
tmux attach -t evaluate_classify_ocr
```

Open the app in a browser: **http://localhost:8082** (`/` = petition list).

> **No auto-start:** booting the server does **not** start either worker. The petition
> list and dashboard render against DB state immediately; you Start the workers
> explicitly from `/worker-log`.

## Run the QualityScore service (optional — usually automatic)

Page-image quality is scored with **DeQA-Doc-Overall** (HuggingFace
`mapo80/DeQA-Doc-Overall` — fully fine-tuned mPLUG-Owl2-7B, merged weights, no
LoRA), on GPU 4. `eval.quality` spawns `scorer_service.py` itself as a
subprocess on first use (on-demand pill fill, or the quality worker below), so
you normally don't run it by hand. The DeQA-Score code and model weights live
under `QualityScore/` (a git submodule of the upstream repo). See
`QualityScore/Readme.md` for details.

A manual run is **only for debugging the JSONL protocol** — the server never
connects to an externally started scorer (it always spawns its own), so a
hand-run instance just loads a second copy of the model onto GPU 4 and sits
idle unless you pipe requests into it yourself:

```bash
tmux new-session -s quality_score
# inside the session, from the repo root:
CUDA_VISIBLE_DEVICES=4 QualityScore/.venv/bin/python scorer_service.py   # 4 = the GPU in QUALITY_CUDA_VISIBLE_DEVICES
```

The model dir, GPU, and torch device are configurable in `.env`
(`QUALITY_MODEL`, `QUALITY_CUDA_VISIBLE_DEVICES`, `QUALITY_DEVICE`) — no CLI
flags needed for the default setup.

It prints one `{"ready": true, …}` line once the model is loaded, then speaks
JSON on stdin/stdout (one `{"id", "image"}` in → one `{"id", "score", …}` out).
Stop it gracefully by killing the PID (SIGTERM) — GPU 4 memory is freed on exit.

## Document quality scoring

Every page carries a DeQA-Doc quality score (1.0–5.0) mapped to a level:

| Score Range | Quality Level | Description |
|---|---|---|
| 4.5 - 5.0 | **Excellent** | Perfect quality, no visible defects |
| 3.5 - 4.5 | **Good** | Minor imperfections, highly readable |
| 2.5 - 3.5 | **Fair** | Noticeable issues but still usable |
| 1.5 - 2.5 | **Poor** | Significant quality problems |
| 1.0 - 1.5 | **Bad** | Severe degradation, hard to read |

Scores fill two ways, both persisting into `file_pages.quality_*`:

- **On demand** — a review page's quality pill (`◆ …`) scores its page lazily
  via `GET /quality/<sha>/<page>` when first viewed.
- **Quality worker** — Start it on `/worker-log` ("Document quality score
  worker" section). It batch-scores every unscored renderable page oldest-first
  (`/qrun/start|stop|continue`, status at `/qrun/status`), logging to
  `/tmp/eval_quality_worker.stdout.log`. Graceful stop via `quality_want_stop`
  in `run_control`; a page that fails 3 times is marked `quality_level='error'`
  and skipped. The dashboard's quality chunk (rings per level) links each level
  to `/quality-pages?level=<level>` for the drill-down list.

## Using the app

There are two pages linked in the header (petition list, dashboard), plus the
worker-log, score-detail, and review-queue pages and the review flow:

1. **Petition list (`/`)** — the landing page. One row per petition with its id
   (link to detail), full `txn_id`, `document_no`, `state`, file count, and an
   AI-status-mix of pill badges showing how many of its files are
   done / none / error / skipped / pending. Filter chips (all / AI-done /
   human-done / human-say-AI-wrong / still-not-done) and a `txn_id`/`document_no`
   search box narrow the list. This is the attachment_browser.py design: Segoe UI,
   deep-blue header, monospaced ids, copy `⧉` buttons. A small **globe** button
   next to the txn_id opens that petition's raw GET-mock JSON in the
   raw-petition browser (new tab) when the txn has a `manifest.jsonl` entry.
2. **Dashboard (`/dashboard`)** — status filters + score chunks:
   - **Score chunks** — Classify score (Correct/Wrong count discs +
     confusion-matrix link), OCR / ADE score (Correct/Acceptable/Wrong count
     discs), and Document quality score (a disc per level:
     Excellent/Good/Fair/Poor/Bad + the score-range legend). Each disc drills
     into the matching page list
     (`/verdict-pages` or `/quality-pages`).
   - Four named filters, each with a live count:
     - **AI-done** — classification settled (done/none/error) AND all pages OCR'd.
     - **Human-done** — every declared-type context of the file fully reviewed.
     - **Human-say-AI-wrong** — any verdict is `wrong` (class) or `bad` (OCR).
     - **Still-not-done** — the remaining backlog (not AI-done).
     Type filters (declared / predicted category) AND-compose with the
     selected status filter. Each filter can be browsed as petitions or as unique
     files (`view=petitions|files`). The **Errors** card links here (the retry
     form lives on `/worker-log`). Score-chunk heads link to their detail
     pages: `/classify-score` (confusion matrix, human vs AI), `/ocr-score`
     (per-verdict OCR stats), and `/quality-score` (quality distribution).
     The detail pages have no filter forms of their own — filters only ride in
     via the query string when following a filtered dashboard link.
     `/classify-score` shows a **full fixed-axis confusion matrix**: every
     doc-type slug is always both a row (human class) and a column (AI class)
     — empty classes render as `—`, there's a per-row total but no column
     totals, plus a final `none` column for AI answers with no true type flag.
3. **Review queue (`/review-queue`)** — AI-finished files with pages still
   awaiting a human verdict — the working list for review. Styled like the
   petition list: the same filter chips (with the Needs-review chip active),
   a search box (txn_id / document_no / sha256 / filename / type), and a
   table of txn_id · document_no · file · AI status · review link.
4. **Worker log (`/worker-log`)** — controls + log tails for both workers:
   - **AI worker (classify + extract)** — **Start** spawns the worker; it claims
     pending files, classifies page 1, then runs the per-filetype extractor on
     every page. **Stop** sets a `want_stop` flag → the worker finishes the
     in-flight page, commits, and exits 0 (graceful). **Continue** re-spawns it;
     pending rows resume automatically. `/run/status` returns JSON for polling.
     **Retry errored** resets every file with `ai_class_status='error'` (or any
     errored page) back to `pending`, clears its verdicts, and auto-starts the
     worker — use this after the LLM endpoint recovers from an outage (a plain
     Continue won't reclaim error files whose pages are also errored).
     **Re-index** re-runs the CSV+JSON load (idempotent). Log:
     `/tmp/eval_worker.stdout.log`. All control buttons (`/run/*` and `/qrun/*`)
     redirect back to `/worker-log` after the POST.
   - **Document quality score worker** — same Start/Stop/Continue shape
     (`/qrun/*`, status at `/qrun/status`), pending = unscored renderable
     pages. Log: `/tmp/eval_quality_worker.stdout.log`.
5. **Petition detail** (`/txn/<txn_id>`, legacy `/petition/<id>` redirects) —
   the files in one petition, with each file's sha256, name, declared type,
   `txn_id`, source table/column, AI predicted type + status, and human
   verdict; links into per-file review. The header panel has a **📋 Request
   body** button (modal of the stored GET-mock JSON) and, when the txn has a
   `manifest.jsonl` entry, an **🌐 Elicense Approval Support System** button
   opening the raw petition in the browser service.
6. **Review** (`/review/<sha>?declared=<type>` — `declared` is required):
   - The `TXN <uuid>` page heading links to that txn's detail page.
   - Every page card (with or without extractor output) shows its **quality
     pill** (`◆ score level`); unscored pills fill lazily on view.
   - **Stage 1 — Classification:** judge **Correct | Wrong** (+ `corrected_type`
     when wrong).
   - **Stage 2 — OCR** appears only after a `correct` classification verdict is
     saved. For each page: AI OCR text vs the page image → **Correct | Acceptable |
     Bad** + optional comment.
   - **Re-run** button resets that one file (class + all pages) to `pending`,
     clears its verdicts, and auto-starts the worker if idle. Rate-limited 30 s/file.
7. Files whose declared type has **no extractor** render preview-only page cards
   (image + quality pill, no verdict forms).
8. Flipping classification back to `wrong` after entering OCR verdicts deletes the
   stale OCR verdicts for that context (OCR is meaningless once class is wrong).

## CLI entry points

| Command | What it does |
|---|---|
| `uv run switch_model.py` | ensure the current `MODEL_NAME`'s database exists + apply schema (idempotent); `--status` lists all model databases; `--sync-quality FROM_DB` copies quality scores from another model's db |
| `uv run eval-index` | apply schema + load CSV files + JSON petitions (idempotent) |
| `uv run eval-worker` | run the AI worker once until no pending work / `want_stop` |
| `uv run python -m eval.quality_worker` | run the quality-score worker once until no unscored pages / `quality_want_stop` |
| `uv run python -m eval.server` | the HTTP server (`/` petition list, `/dashboard` controls, review UI; spawns worker) |

The worker can also be run by hand (`uv run eval-worker`) for debugging; it reads
`run_control.want_stop` and exits gracefully, so the dashboard's Stop and a manual
worker interoperate.

## Switching models

Results are isolated **one Postgres database per model**: `DB_DSN` defaults to
`evalutea_<model_slug>` derived from `MODEL_NAME` (`Qwen/Qwen3.6-35B-A3B` →
`evalutea_qwen3_6_35b_a3b`), so changing `MODEL_NAME` in `.env` switches the
database too. To evaluate a different model:

```bash
# 1) edit MODEL_NAME in .env, then:
uv run switch_model.py        # create the new model's db if missing + apply schema (idempotent)
uv run eval-index             # index petitions/files into the fresh db
uv run switch_model.py --sync-quality evalutea_qwen3_6_35b_a3b  # reuse existing quality scores
uv run python -m eval.server  # run the worker/review against the new model
```

`switch_model.py` never drops or recreates an existing database — re-running it
for the current model only reapplies the schema (a no-op). Use
`uv run switch_model.py --status` to list every `evalutea_*` database with its
verdict count and quality-score coverage (per-model results stay frozen side by
side). After indexing a fresh model db, copy the quality scores over from a
model that already has them (quality is model-independent — one run serves
every model db):

```bash
uv run switch_model.py --sync-quality evalutea_qwen3_6_35b_a3b
# copies file_pages.quality_* joined on (sha256, page_no); existing target
# scores are never overwritten (COALESCE keeps them)
```

Two things are
shared across models, so a switch is cheaper than a fresh start: the page-PNG
cache (`CACHE_DIR`, keyed by sha256 — no re-rendering) and the Postgres instance
itself. Set `DB_DSN` explicitly in `.env` only to pin a database and ignore
`MODEL_NAME` (e.g. the legacy shared `evalutea_classi_ocr`).

> History: the project originally used a single shared db
> (`evalutea_classi_ocr`), which was renamed to `evalutea_qwen3_6_35b_a3b`
> (286k indexed pages preserved) when per-model databases were introduced.

## Configuration (`.env`)

| Var | Default | Notes |
|---|---|---|
| `DB_DSN` | `postgresql://eval:eval@localhost:5435/evalutea_<model_slug>` | derived from `MODEL_NAME` (one db per model); set explicitly to pin/override |
| `LLM_ENDPOINT` | `http://localhost:4000` | OpenAI-compatible `/v1/chat/completions` |
| `MODEL_API_KEY` | `sk-1234` | |
| `MODEL_NAME` | `Qwen/Qwen3.6-35B-A3B` | must exist on the endpoint |
| `CACHE_DIR` | `./.cache/pages` | rendered page PNGs, keyed by sha256 |
| `MOCK_ROOT` | `…/แกะapiระบบElicense-ดึงจากDB` | GET-mock JSON tree (petition metadata). Code default is `/home/admins/…`; `.env` overrides to `/home/user/…` |
| `DOC_ROOT` | `/home/admins/aiProject/eLicenseDocuments/miid-attachment-prod` | local attachment tree; CSV filenames are rebased under this. `.env` overrides to `/home/user/aiProject/elicenseDocuments/…` (lowercase) |
| `GEO_MASTER_DIR` | `/home/admins/…/Elicense-network-collection` | province/district/subdistrict master-data JSONs for the request-body modal. `.env` overrides to `/home/user/…` |
| `PETITION_API_BASE` | `http://localhost:8765` | base URL of the raw-petition browser; the globe / Elicense buttons link to `{base}/api/petition?name=GET_*.json` via the txn map from `MOCK_ROOT/manifest.jsonl` |
| `HTTP_PORT` | `8080` | server port (use `8082` — 8080 is taken on this box) |

## Data model (quick)

- `files` (one row per unique sha256): AI classification lives here (once per file).
- `file_pages` (one row per page): AI OCR lives here, plus the DeQA-Doc quality
  columns (`quality_score`/`quality_level`/`quality_probs`/`quality_model`/
  `quality_at`; `quality_level='error'` = tried and skipped by the quality worker).
- `run_control` (single row): stop flags + state for both workers — `want_stop`/
  `state`/`last_exit_code` for the AI worker, `quality_want_stop`/`quality_state`/
  `quality_last_exit_code` for the quality worker.
- `petitions` / `petition_files`: many-to-many; the same file can appear in
  multiple petitions under different declared types — each `(sha256,
  declared_category)` is a distinct correctness question.
- `verdicts`: human review, keyed by `(sha256, declared_category, page_no)` where
  `page_no IS NULL` = classification and `page_no = N` = OCR page N. AI output
  carries no confidence; the full `do_chat` dict is
  persisted in `*_raw` JSONB for later re-scoring.

See `PLAN.md` for the full spec and `schema.dbml` for the DDL reference.

## Crash recovery

A crash leaves the in-flight file `pending` (status flips to a terminal value only
on completion) → it's re-claimed next run; already-done pages are skipped. No
`running` status is stored, so a crashed worker never leaves stuck rows. Killing the
server mid-run and restarting it lets the worker resume from `pending`.
