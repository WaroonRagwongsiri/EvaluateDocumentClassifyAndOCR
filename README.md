# evalutate — E-License Classification + OCR Evaluation

Measures how well **document classification** and **OCR** work on e-License petition
attachments. A plain HTTP server (Python stdlib, no web framework) serves a petition
list (the landing page), a dashboard (worker controls + status filters), and a
per-document two-stage review (classify, then OCR per page). A separate AI worker
process (spawned by the server) classifies page 1 and OCRs every page of each unique
file. See `PLAN.md` for the full design.

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

# 2) Bring up Postgres (host port 5435 → container 5432, db evalutea_classi_ocr)
docker compose up -d

# 3) Apply the schema + load the index (CSV files + GET-mock JSON petitions)
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

> **No auto-start:** booting the server does **not** start the worker. The petition
> list and dashboard render against DB state immediately; you Start the worker
> explicitly from the dashboard.

## Run the QualityScore service (inside tmux)

A separate long-lived process scores page-image quality with **DeQA-Doc-Overall**
(HuggingFace `mapo80/DeQA-Doc-Overall` — fully fine-tuned mPLUG-Owl2-7B, merged
weights, no LoRA), on GPU 4. The service script lives at the repo root
(`scorer_service.py`); the DeQA-Score code and model weights live under
`QualityScore/` (a git submodule of the upstream repo). See
`QualityScore/Readme.md` for details.

```bash
tmux new-session -s quality_score
# inside the session, from the repo root:
CUDA_VISIBLE_DEVICES=4 QualityScore/.venv/bin/python scorer_service.py
```

The model dir and torch device are configurable in `.env` (`QUALITY_MODEL`,
`QUALITY_DEVICE`) — no CLI flags needed for the default setup.
```bash
tmux attach -t quality_score     # attach to watch the JSONL protocol
```

It prints one `{"ready": true, …}` line once the model is loaded, then speaks
JSON on stdin/stdout (one `{"id", "image"}` in → one `{"id", "score", …}` out).
Stop it gracefully by killing the PID (SIGTERM) — GPU 4 memory is freed on exit.

## Using the app

There are two pages, linked in the header:

1. **Petition list (`/`)** — the landing page. One row per petition with its id
   (link to detail), full `txn_id`, `document_no`, `state`, file count, and an
   AI-status-mix of pill badges showing how many of its files are
   done / none / error / skipped / pending. Filter chips (all / AI-done /
   human-done / human-say-AI-wrong / still-not-done) and a `txn_id`/`document_no`
   search box narrow the list. This is the attachment_browser.py design: Segoe UI,
   deep-blue header, monospaced ids, copy `⧉` buttons.
2. **Dashboard (`/dashboard`)** — worker controls + status filters:
   - **Start** spawns the worker; it claims pending files, classifies page 1, then
     OCRs every page. **Stop** sets a `want_stop` flag → the worker finishes the
     in-flight page, commits, and exits 0 (graceful). **Continue** re-spawns it;
     pending rows resume automatically. `/run/status` returns JSON for polling.
     **Retry errored** resets every file with `ai_class_status='error'` (or any
     errored page) back to `pending`, clears its verdicts, and auto-starts the
     worker — use this after the LLM endpoint recovers from an outage (a plain
     Continue won't reclaim error files whose pages are also errored).
   - Four named filters, each with a live count:
     - **AI-done** — classification settled (done/none/error) AND all pages OCR'd.
     - **Human-done** — every declared-type context of the file fully reviewed.
     - **Human-say-AI-wrong** — any verdict is `wrong` (class) or `bad` (OCR).
     - **Still-not-done** — the remaining backlog (not AI-done).
     Type filters (declared / predicted category, OOV-only) AND-compose with the
     selected status filter. Each filter can be browsed as petitions or as unique
     files (`view=petitions|files`).
3. **Petition detail** (`/petition/<id>`) — the files in one petition, with each
   file's sha256, name, declared type, `txn_id`, source table/column, AI predicted
   type + status, and human verdict; links into per-file review.
4. **Review** (`/review/<sha>?declared=<type>`):
   - **Stage 1 — Classification:** judge **Correct | Wrong** (+ `corrected_type`
     when wrong). The auto predicted-vs-declared comparison is skipped for OOV
     declared types (the classifier can't predict them).
   - **Stage 2 — OCR** appears only after a `correct` classification verdict is
     saved. For each page: AI OCR text vs the page image → **Correct | Acceptable |
     Bad** + optional comment.
   - **Re-run** button resets that one file (class + all pages) to `pending`,
     clears its verdicts, and auto-starts the worker if idle. Rate-limited 30 s/file.
5. Flipping classification back to `wrong` after entering OCR verdicts deletes the
   stale OCR verdicts for that context (OCR is meaningless once class is wrong).

## CLI entry points

| Command | What it does |
|---|---|
| `uv run eval-index` | apply schema + load CSV files + JSON petitions (idempotent) |
| `uv run eval-worker` | run the AI worker once until no pending work / `want_stop` |
| `uv run python -m eval.server` | the HTTP server (`/` petition list, `/dashboard` controls, review UI; spawns worker) |

The worker can also be run by hand (`uv run eval-worker`) for debugging; it reads
`run_control.want_stop` and exits gracefully, so the dashboard's Stop and a manual
worker interoperate.

## Configuration (`.env`)

| Var | Default | Notes |
|---|---|---|
| `DB_DSN` | `postgresql://eval:eval@localhost:5435/evalutea_classi_ocr` | the docker-compose Postgres |
| `LLM_ENDPOINT` | `http://localhost:4000` | OpenAI-compatible `/v1/chat/completions` |
| `MODEL_API_KEY` | `sk-1234` | |
| `MODEL_NAME` | `Qwen/Qwen3.6-35B-A3B` | must exist on the endpoint |
| `CACHE_DIR` | `./.cache/pages` | rendered page PNGs, keyed by sha256 |
| `MOCK_ROOT` | `…/แกะapiระบบElicense-ดึงจากDB` | GET-mock JSON tree (petition metadata). Code default is `/home/admins/…`; `.env` overrides to `/home/user/…` |
| `DOC_ROOT` | `/home/admins/aiProject/eLicenseDocuments/miid-attachment-prod` | local attachment tree; CSV filenames are rebased under this. `.env` overrides to `/home/user/aiProject/elicenseDocuments/…` (lowercase) |
| `GEO_MASTER_DIR` | `/home/admins/…/Elicense-network-collection` | province/district/subdistrict master-data JSONs for the request-body modal. `.env` overrides to `/home/user/…` |
| `HTTP_PORT` | `8080` | server port (use `8082` — 8080 is taken on this box) |

## Data model (quick)

- `files` (one row per unique sha256): AI classification lives here (once per file).
- `file_pages` (one row per page): AI OCR lives here.
- `petitions` / `petition_files`: many-to-many; the same file can appear in
  multiple petitions under different declared types — each `(sha256,
  declared_category)` is a distinct correctness question.
- `verdicts`: human review, keyed by `(sha256, declared_category, page_no)` where
  `page_no IS NULL` = classification and `page_no = N` = OCR page N. OOV is a
  derived property of the declared type (not in the 19 classifiable types), not
  a stored value. AI output carries no confidence; the full `do_chat` dict is
  persisted in `*_raw` JSONB for later re-scoring.

See `PLAN.md` for the full spec and `schema.dbml` for the DDL reference.

## Crash recovery

A crash leaves the in-flight file `pending` (status flips to a terminal value only
on completion) → it's re-claimed next run; already-done pages are skipped. No
`running` status is stored, so a crashed worker never leaves stuck rows. Killing the
server mid-run and restarting it lets the worker resume from `pending`.
