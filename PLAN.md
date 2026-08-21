# E-License Document Classification + OCR Evaluation App

## Context

We want to measure how well **document classification** and **OCR** work on e-License
petition attachments. The existing eLicense AI API returns only petition-level
question/answer cards — it does **not** expose per-document predicted types or
per-page OCR text — so it cannot be the thing we evaluate.

The AI we evaluate is the **wind `doc_validator_webui`** pipeline, kept for
**classification + OCR only** (no correctness/completeness checks — those parts
simply don't exist in that repo). Classification exists only as dormant helpers
(`tasks.build_classification_prompt` / `parse_classification`); OCR is a working
per-page vision call. We **copy those primitives into this repo** (vendor them)
rather than import across the repo boundary — i.e. lift just the classify + OCR
call code into a local module, strip wind's Streamlit rendering, and drop the
correctness/completeness parts. This makes `evalutate` self-contained: it depends
on the wind repo only as a one-time source to copy from, not at runtime.

The app is a **plain HTTP server** (Python stdlib `http.server`, no web
framework): server-rendered HTML pages for review + dashboard, forms that post
back, and page PNGs served as `<img>` routes. Review flow (see Review flow
below for detail): a **petition list** → drill into a petition's documents → for each
document the human first judges **classification (correct / wrong + corrected type)**.
If wrong, OCR is **skipped** for that document. If correct, the human evaluates each
page's **OCR as Correct / Acceptable / Bad + a comment**.
a **dashboard** with four status filters — **AI-done, Human-done,
Human-say-AI-wrong, Still-not-done** (each a named filter with a live count) —
plus Start / graceful Stop / Continue buttons for the AI run, because there are ~7,157
petitions (~37,678 unique files). Identical file content is deduped by sha256 so AI is run once and
humans don't re-review the same content twice.

## Architecture overview

- **Postgres** (docker-compose, port **5435**) holds the eval data: petitions,
  unique files (by sha256), per-file AI classification, per-page AI OCR,
  per-occurrence declared types, human verdicts, and a 1-row run-control table
  for stop/continue.
- **Indexer** (CLI / dashboard button) loads from **two sources** and joins them:
  - **`filtered.csv`** (in the project root) — a pre-built index of **37,678 unique files**
    with `hashed_value` (verified sha256), `local_file_path`
    (`/home/admins/aiProject/eLicenseDocuments/miid-attachment-prod/<filename>`),
    `txn_id`, and `table_column_name` (the declared category). All files verified
    present on disk; no S3 fetch needed. This populates `files` (sha256 + local path,
    already content-deduped — every CSV row has a distinct sha256) and `petition_files`.
  - **GET-mock JSONs** (~7,157 under the elicense-query tree; filenames
    `GET_<type>_<state>_<result.id.short>_<txn_id.short>.json`) — the authority for
    *which petitions exist* and their `result.id` / `document_no` / `state` / `raw_json`.
    Only ~6,187 distinct `txn_id` appear in the CSV, so the JSONs cover ~970 extra
    petitions that have files only in JSON form (not yet locally indexed) — those get
    `petitions` rows but no `petition_files` rows until/unless their files are located.
  The join key is `txn_id`: each JSON's `result.txn_id` maps a petition (`result.id`)
    to its CSV rows. Petitions present only in JSONs are inserted with no files.
- **AI worker** (separate OS process, **spawned by the HTTP server** via
  `subprocess.Popen`) claims pending unique files, classifies the first page,
  OCRs every page, writes results. It polls a DB `want_stop` flag every iteration
  and between pages → graceful stop; Continue just re-spawns it (pending rows
  resume automatically).
- **HTTP server** (stdlib `http.server.ThreadingHTTPServer`, module `eval.server`)
  serves server-rendered HTML for review + dashboard and the page PNGs as `<img>`
  routes. Routes:
  - `GET /` → dashboard: the filter bar (see Filters below) + run status + Start /
    Stop / Continue buttons + aggregate counts per filter.
  - `GET /petitions?filter=<name>&view=<unit>` → petition list, filtered by `<name>`
    (one of the filter values below); `<view>` ∈ `petitions` | `files` switches the
    row unit (a filter can be browsed either as petitions or as unique files).
  - `GET /petition/<id>` → a petition's documents/files.
  - `GET /review/<sha256>?declared=<type>` → classification review + per-page OCR
    review; pages render as `<img src="/page/<sha256>/<n>">`.
  - `GET /page/<sha256>/<n>` → rendered page PNG (`Content-Type: image/png`) from
    the sha256-keyed cache dir.
  - `POST /verdict` → save a human verdict (classification when `page_no IS NULL`,
    per-page OCR when `page_no = N`).
  - `POST /review/<sha256>/rerun` → **re-run AI on this one file**: reset
    `files.ai_class_status='pending'` and every `file_pages.ai_ocr_status='pending'`,
    delete that file's human `verdicts` rows (they'd be scored against stale AI
    output), and let the worker re-claim it. Only enabled while the worker is
    running (the route auto-starts it if idle — see below).
  - `POST /run/start` / `/run/stop` / `/run/continue`, `GET /run/status` → worker
    control + JSON status for light polling.

The server holds the worker `subprocess.Popen` handle in process memory behind a
lock: Start = `Popen` the worker CLI; Stop = set `run_control.want_stop=1`
(worker exits gracefully on next poll); Continue = clear `want_stop` and `Popen`
again. `/run/status` returns running / last-exit-code / pending-count (read from
DB). Single-server deployment assumed (handle is in-process).

### Re-run one file

`POST /review/<sha256>/rerun` (button on the review page) re-runs AI on a single
file without touching the rest of the run. It runs in one transaction:
1. `DELETE FROM verdicts WHERE sha256 = <sha256>` — clear human verdicts for this
   file (scoring old verdicts against new AI output would be misleading; the human
   re-reviews).
2. `UPDATE files SET ai_class_status='pending', ai_predicted_category=NULL,
   ai_class_raw=NULL, ...` and `UPDATE file_pages SET ai_ocr_status='pending',
   ai_ocr_text=NULL, ai_ocr_raw=NULL WHERE sha256=<sha256>`.
The reset flips statuses to `pending`, so the existing worker claim query
(`… WHERE ai_class_status='pending' FOR UPDATE SKIP LOCKED`) re-claims the file
with no special-case worker code. The route **auto-starts the worker if it's
idle** (so a re-run isn't silently stranded), then 302-redirects back to the
review page; the UI shows a "re-running…" state until `/run/status`/polling shows
the file back to `done`. Rate-limit per file (e.g. no re-run within 30s of the
last one for that sha256) to avoid thrash.

### Dashboard filters

The dashboard filter bar exposes exactly these four named filters (each shown with
its live count). A "file" here means a unique `files` row (sha256); a "petition file"
means a `petition_files` occurrence. `files.ai_class_status` ∈
{pending, done, none, error, skipped}; a file is **AI-done** when status ∈
{done, none, error} (skipped counts as not-AI-done — nothing was classified/OCR'd).
Human review is **two-stage**: classify first, then OCR only if classify is `correct`
(see Review flow) — so human_done depends on the classification verdict.

| Filter (`?filter=`) | Meaning | SQL predicate (per `files` row `f`) |
|---|---|---|
| `ai_done` | AI has finished this file (classify done/none/error, and all pages OCR'd) | `f.ai_class_status IN ('done','none','error') AND NOT EXISTS (SELECT 1 FROM file_pages p WHERE p.sha256=f.sha256 AND p.ai_ocr_status='pending')` |
| `human_done` | A human has reviewed this file (see expansion below) | `(class verdict='wrong') OR (class verdict='correct' AND all pages have OCR verdicts)`, for every declared context |
| `human_say_ai_wrong` | The human marked the AI wrong (classification `verdict='wrong`, OR any OCR page `verdict='bad'`) | `EXISTS (SELECT 1 FROM verdicts v WHERE v.sha256=f.sha256 AND (v.verdict='wrong' OR v.verdict='bad'))` |
| `still_not_done` | Not AI-done (pending/skipped, or pages still pending) — the remaining backlog | `NOT (<ai_done predicate>)` |

`human_done` expands to, for **each** `(sha256, declared_category)` context of the
file: a classification verdict exists, AND either (a) classification `verdict='wrong'`
(OCR skipped → no page verdicts required), or (b) classification `verdict='correct'`
AND for every `file_pages` row there is an OCR verdict for that page + declared
context. Because human verdicts are keyed by `(sha256, declared_category, page_no)`,
a file shared across petitions under different declared types needs verdicts per
declared type to count as fully human_done — the filter counts a file as human_done
only when all its declared-type contexts are reviewed. (If you'd rather count "any
one context reviewed" as human_done for the dashboard count, say so; the strict
all-contexts definition is the default so the count can't hide un-reviewed contexts.)

Notes:
- Filters compose with the existing dimension filters (AI-done/human-done/wrong are
  *status* filters; the dashboard keeps them separate from *type* filters like
  declared category / predicted category / OOV-only). Combinations are AND-ed.
- `human_say_ai_wrong` is the disagreement set — the primary worklist for re-review.
  It is a subset of `human_done` by definition (a wrong/bad verdict is still a human
  verdict). `still_not_done` is disjoint from `ai_done`.
- "Wrong" = classification disagreement; "Bad" = OCR disagreement. They are distinct
  labels in distinct stages (a document can be class=correct but OCR=bad).

AI calls use the **vendored** classify + OCR primitives (copied from wind's
`openai_client.chat` + `tasks.*` prompts/parsers + `pdf_image_utils`, stripped of
wind's Streamlit rendering — wind's `pipeline_steps.py` is **not** copied). Files +
rendered page PNGs live on disk under a cache dir keyed by sha256. HTML is rendered
with stdlib `string.Template` + `html.escape` (no Jinja2, no JS build step).

**Dependencies / launch:** deps are `psycopg`, `pymupdf`, `pillow`, `requests`,
`python-dotenv` (no Streamlit / web framework). Launch with
`uv run python -m eval.server` (default port ~8080). The server entry must
bootstrap the project root onto `sys.path` before importing `eval.*` — `uv run`
adds only the script dir, not the project root.

**Run inside tmux.** Both the HTTP server and (if started by hand) the AI worker are
launched inside a tmux session so they survive a dropped SSH connection:

```
tmux new-session -s evaluate_classify_ocr
# inside the session:
uv run python -m eval.server          # the review UI + dashboard (and spawns the worker)
```

Re-attach later with `tmux attach -t evaluate_classify_ocr`. The dashboard's
Start/Stop/Continue buttons still drive the worker via `subprocess.Popen` (the worker
process is a child of the server in that same tmux session), so the browser is the
primary control surface; tmux just keeps the server alive.

## Data model (overview)

- **AI runs per sha256** (content) → stored once on `files` / `file_pages`. Not
  re-run automatically — but a human can trigger a **re-run on one specific file**
  (`POST /review/<sha256>/rerun`), which resets its AI status to pending and clears
  its verdicts (see Routes & Re-run).
- **Human verdict keyed by `(sha256, declared_category, page_no)`**, not sha256 alone:
  the same file declared as two different types in two petitions is a *different*
  correctness question. `page_no IS NULL` = classification verdict; `page_no = N` =
  per-page OCR verdict. The many-to-many (same content across petitions, possibly
  different declared types) lives in `petition_files`. `files.declared_filetype_first`
  gives a first-seen label for fast-path pre-fill.

## Database schema (detailed)

Postgres 15+, database `evalutea_classi_ocr`, host port **5435** (docker-compose).
Two fixed vocabularies are modeled as enum types (members are the literal snake_case
keys; they are identical strings in both lists for the 19 in-vocab types):

```sql
-- 19 classifiable types (policy_loader.list_classifiable_types(); the classifier
-- can only output one of these, or the literal "none" sentinel handled as a status).
CREATE TYPE classifiable_category_t AS ENUM (
  'juristic_person_certificate','land_map_diagram','factory_building_plan',
  'factory_safety_certificate','factory_building_diagram','machine_installation_diagram',
  'waste_document','emissions_document','name_change_certificate',
  'power_of_attorney_with_revenue_stamp','factory_document_of_right',
  'consent_document_to_set_up_factory','power_of_attorney_competent_authority',
  'copy_of_house_registration_factory_location','copy_of_professional_engineering_license',
  'factory_operation_risk','environmental_risk','environmental_impact_eia',
  'environmental_impact_iee'
);

-- Declared categories: the 26 build_dataset.ATTACH_ORDER PLUS the 4 extras that
-- actually appear in the index (filtered.csv table_column_name) as real buckets:
-- attchment, production_process_diagram, factory_maps_attchment, factory_eia_attchment.
-- = 30 members. The 19 that overlap classifiable_category_t are in-vocab; the 11 rest are OOV.
CREATE TYPE declared_category_t AS ENUM (
  -- 19 in-vocab (identical to classifiable_category_t)
  'juristic_person_certificate','land_map_diagram','factory_building_plan',
  'factory_safety_certificate','factory_building_diagram','machine_installation_diagram',
  'waste_document','emissions_document','name_change_certificate',
  'power_of_attorney_with_revenue_stamp','factory_document_of_right',
  'consent_document_to_set_up_factory','power_of_attorney_competent_authority',
  'copy_of_house_registration_factory_location','copy_of_professional_engineering_license',
  'factory_operation_risk','environmental_risk','environmental_impact_eia',
  'environmental_impact_iee',
  -- 11 OOV (never predictable by the classifier)
  'factory_building_plan_certifier','machine_installation_diagram_certifier',
  'waste_document_certifier','emissions_document_certifier','another_document',
  'officer_document_requested','applicant_signature',
  'attchment','production_process_diagram','factory_maps_attchment','factory_eia_attchment'
);
```

### petitions — one row per GET-mock petition (~7,157 JSONs)
```sql
CREATE TABLE petitions (
  id           uuid PRIMARY KEY,            -- result.id  (from the JSON's result.id; also in filename)
  txn_id       uuid UNIQUE,                  -- result.txn_id  (the join key to filtered.csv rows)
  document_no  text,                         -- e.g. '69-10-RG-000536'
  state        text,                         -- e.g. 'accept'
  raw_json     jsonb,                        -- full GET-mock result (NULL for petitions known only via CSV)
  indexed_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX petitions_document_no_idx ON petitions (document_no);
CREATE INDEX petitions_state_idx       ON petitions (state);
```
`txn_id UNIQUE` makes it the reliable join key to `filtered.csv`. `raw_json` is
nullable because some petitions may surface first from the CSV before their JSON is
processed; it's filled in when the matching GET-mock JSON is read.

### files — one row per unique content (sha256). AI classification lives here (1:1).
The indexer **reads each row of `filtered.csv`** (sha256 = `hashed_value`,
`local_path` = `local_file_path`/`filename`), so no S3 download and no hashing is
needed — the CSV is already content-deduped (every row's sha256 is distinct). It
then opens each local file with PyMuPDF to derive `page_count`/`content_kind` and
inserts the row + its `file_pages` rows. Page PNGs (PyMuPDF @150dpi) are rendered
on demand and cached on disk at `<cache>/<sha256>/page_<n>.png` (path derivable,
not stored).
```sql
CREATE TABLE files (
  sha256                  text PRIMARY KEY,          -- = filtered.csv.hashed_value
  local_path              text NOT NULL,             -- = filtered.csv.local_file_path||'/'||filename
  filename                text NOT NULL,             -- = filtered.csv.filename (e.g. '1741938446467.pdf')
  ext                     text,                      -- lower(filename suffix): 'pdf'|'png'|'jpg'|'docx'...
  content_kind            text NOT NULL,             -- 'pdf' | 'image' (jpg/png/...) | 'other' (doc/xlsx -> not processable)
  size_bytes              bigint NOT NULL,
  page_count              int  NOT NULL,             -- derived via PyMuPDF; 1 for single images; 0 for unprocessable
  declared_filetype_first declared_category_t,      -- first-seen declared category (fast-path prefill)

  -- AI classification (runs once per sha256; the prompt is content-only, no declared type)
  ai_class_status         text NOT NULL DEFAULT 'pending',  -- pending|done|none|error|skipped
  ai_predicted_category   classifiable_category_t,          -- set when status='done'; NULL otherwise
  ai_class_latency_s     real,
  ai_class_error          text,                              -- set when status='error'
  ai_class_raw            jsonb,                             -- full do_chat dict {content,thinking,raw,latency_s,error}
  ai_class_model         text,
  ai_class_at             timestamptz,

  first_seen_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX files_pending_idx ON files (sha256) WHERE ai_class_status = 'pending';
```
`ai_class_status` values:
- `pending` — not yet classified (worker claims these).
- `done` — model returned one of the 19; `ai_predicted_category` set.
- `none` — model returned the `none` sentinel (no type matched); predicted is NULL.
- `error` — call failed; `ai_class_error` set.
- `skipped` — not classifiable (`content_kind='other'`: doc/docx/xlsx), so the worker
  renders nothing and skips both classification and OCR.
`ai_class_status` values:
- `pending` — not yet classified (worker claims these).
- `done` — model returned one of the 19; `ai_predicted_category` set.
- `none` — model returned the `none` sentinel (no type matched); predicted is NULL.
- `error` — call failed; `ai_class_error` set.

> **OOV modeling note (supersedes earlier "ai_class_status='oov'" wording):** OOV is
> a property of the *declared* type, which varies per `petition_files` row, not of the
> file content — and classification is content-only. So OOV is **not** a stored value on
> `files`. It is a derived flag at the `(sha256, declared_category)` scoring level:
> `declared_category` is OOV iff it is not a member of `classifiable_category_t`. For
> OOV occurrences the dashboard skips the auto predicted-vs-declared comparison (it
> would always mismatch, since the classifier can't predict the 11 OOV types); the human
> still judges the AI's guess and may supply a `corrected_type`. If you'd rather the
> worker *skip the classification call entirely* for files declared only as OOV types
> (saves vision calls at the cost of no AI guess for those files), say so and we'll add
> an `oov` status.

### file_pages — one row per page; AI OCR lives here (1:N). Created by the indexer.
```sql
CREATE TABLE file_pages (
  sha256           text NOT NULL REFERENCES files(sha256) ON DELETE CASCADE,
  page_no          int  NOT NULL,               -- 1-based
  ai_ocr_status    text NOT NULL DEFAULT 'pending',  -- pending|done|error
  ai_ocr_text      text,                              -- do_chat.content (transcribed text)
  ai_ocr_latency_s real,
  ai_ocr_error     text,
  ai_ocr_raw       jsonb,                             -- full do_chat dict
  ai_ocr_model     text,
  ai_ocr_at        timestamptz,
  PRIMARY KEY (sha256, page_no)
);
CREATE INDEX file_pages_pending_idx ON file_pages (sha256, page_no) WHERE ai_ocr_status = 'pending';
```

### petition_files — many-to-many: a file as it appears in a petition under a declared type.
This is the crux: the same sha256 can appear in multiple petitions, possibly under
different declared categories — each `(sha256, declared_category)` is a distinct
correctness question.
```sql
CREATE TABLE petition_files (
  petition_id       uuid NOT NULL REFERENCES petitions(id) ON DELETE CASCADE,
  sha256            text NOT NULL REFERENCES files(sha256)  ON DELETE CASCADE,
  declared_category declared_category_t NOT NULL,         -- = filtered.csv.table_column_name
  txn_id            uuid,                                  -- = filtered.csv.txn_id (traceability; == petitions.txn_id)
  source_table      text,                                  -- = filtered.csv.table_name  (general_petition_attachment|process|personal|factory)
  source_column     text,                                  -- = filtered.csv.table_column_name (raw column name)
  source_file_name  text,                                  -- = filtered.csv.filename (display)
  indexed_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (petition_id, sha256, declared_category)
);
CREATE INDEX petition_files_sha256_idx     ON petition_files (sha256);
CREATE INDEX petition_files_declared_idx   ON petition_files (declared_category);
CREATE INDEX petition_files_petition_idx   ON petition_files (petition_id);
```
Note: `filtered.csv` gives the declared category as `table_column_name` and the
provenance as `table_name` (which petition sub-table the file lived under:
`general_petition_attachment`, `_process`, `_personal`, `_factory`). There are no S3
URLs/keys in the CSV — the file is referenced purely by `sha256` → `files.local_path`.
`source_column` is kept (raw, unvalidated) alongside `declared_category` (enum) for
traceability in case a future CSV value isn't in the enum yet.

### verdicts — human review. One classification verdict (page_no NULL) + N OCR verdicts.
Classification and OCR are **stages**: the human judges classification first; OCR
verdicts exist only when classification was judged `correct` (if classification is
`wrong`, OCR is skipped — no OCR verdict rows are created). OCR labels are
`correct` / `acceptable` / `bad`.
```sql
CREATE TABLE verdicts (
  sha256            text NOT NULL REFERENCES files(sha256) ON DELETE CASCADE,
  declared_category declared_category_t NOT NULL,         -- which declared-type context
  page_no           int,                                   -- NULL = classification; N = OCR page N
  verdict           text NOT NULL,
  corrected_type    declared_category_t,                   -- human's true type (classification, when wrong)
  comment           text,                                  -- free text (any stage)
  annotator         text,                                  -- who (open question; nullable for now)
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sha256, declared_category, page_no)        -- NULL page_no is distinct → one class row per (sha256,declared)
  CHECK (
    (page_no IS NULL     AND verdict IN ('correct','wrong'))                       -- classification verdict
    OR
    (page_no IS NOT NULL AND verdict IN ('correct','acceptable','bad'))            -- per-page OCR verdict (only when class is correct)
  )
);
CREATE INDEX verdicts_lookup_idx ON verdicts (sha256, declared_category);
CREATE INDEX verdicts_verdict_idx ON verdicts (verdict);
CREATE INDEX verdicts_annotator_idx ON verdicts (annotator);
```
"OCR only when class is correct" is enforced by the app (the OCR form section is
gated on the saved classification verdict being `correct`; `POST /verdict` for a
page rejects if no `correct` classification verdict exists). It is not a DB CHECK
because it's a cross-row condition (a page verdict depends on the classification
row of the same `(sha256, declared_category)`); the route handler validates it.

### Review flow (human, per document)

The end-to-end flow:

```
run_start (dashboard) → AI worker: classify page 1 → OCR every page → write files/file_pages
                                                          ↓
human: dashboard → pick a txn → petition document list (GET /petition/<id>)
                                                          ↓
        for each document (a petition_files row → one sha256 + declared_category):
        1. see the AI's predicted category vs the declared category
        2. judge CLASSIFICATION: Correct | Wrong
             • Wrong  → enter corrected_type → SKIP OCR for this document (done; no OCR verdicts)
             • Correct→ proceed to step 3
        3. for each page: see AI OCR text vs the page image, judge OCR: Correct | Acceptable | Bad
             • optionally add a Comment (per page)
```

- A document is **human_done** when it has a classification verdict *and* either
  classification is `wrong` (OCR skipped) or classification is `correct` *and* every
  page has an OCR verdict.
- The OCR form section on `GET /review/<sha256>?declared=<type>` is only rendered after
  a `correct` classification verdict is saved; if the human flips classification back
  to `wrong` after entering OCR verdicts, the app deletes the stale OCR verdict rows
  for that `(sha256, declared_category)` (OCR is meaningless once class is wrong).
- Because the worker runs per sha256 (content) but the human reviews per
  `(sha256, declared_category)` (context), the same file shared across petitions under
  different declared types is reviewed once per context — the AI results are shown for
  each context; only the verdict differs.

### run_control — single row for worker stop/continue.
```sql
CREATE TABLE run_control (
  id              int PRIMARY KEY CHECK (id = 1),  -- enforces the single row
  want_stop       boolean NOT NULL DEFAULT false, -- worker polls this each iteration & between pages
  state           text NOT NULL DEFAULT 'idle',    -- idle|running|stopping (server-maintained, display only)
  last_started_at timestamptz,
  last_stopped_at timestamptz,
  last_exit_code  int,
  updated_at      timestamptz NOT NULL DEFAULT now()
);
INSERT INTO run_control (id) VALUES (1) ON CONFLICT DO NOTHING;
```
The HTTP server holds the worker's `subprocess.Popen` handle in-process; `state` /
timestamps / `last_exit_code` are maintained by the server for `/run/status`. The
**worker** only reads `want_stop`. `/run/status` returns `{state, last_exit_code,
pending: <count of files where ai_class_status='pending'>}`.

### Worker claiming & crash recovery
- Claim a file: `SELECT … FROM files WHERE ai_class_status='pending' ORDER BY …
  FOR UPDATE SKIP LOCKED LIMIT 1`. If `content_kind='other'` (doc/docx/xlsx — not
  renderable), set `ai_class_status='skipped'` and move on (no pages, no OCR). For
  pdf/image: classify page 1 → set `ai_class_status`/`ai_predicted_category`/
  `ai_class_raw`; then OCR each `file_pages` row where `ai_ocr_status='pending'`.
  Poll `want_stop` between files and between pages.
- A crash leaves the in-flight file as `pending` (status flips to `done`/`none`/
  `error`/`skipped` only on completion) → it is re-claimed and re-processed on the
  next run (writes are idempotent upserts); already-`done` pages are skipped. No
  `running` status is stored, so a crashed worker never leaves stuck rows.

## Vocabulary alignment (important)

- Classifier outputs one of **19** `field` keys (`policy_loader.list_classifiable_types()`),
  or the literal `none` sentinel — modeled by `classifiable_category_t` (19 members) plus
  an `ai_class_status='none'` state. The prompt is **content-only** (no declared type in
  context), so classification is genuinely per-sha256.
- Petitions declare categories from a ~30-member set — modeled by `declared_category_t`.
  This is the 26 `build_dataset.ATTACH_ORDER` keys PLUS 4 that actually appear in
  `filtered.csv.table_column_name` as real buckets: `attchment`, `production_process_diagram`,
  `factory_maps_attchment`, `factory_eia_attchment`. The **11** OOV members (never
  predictable by the classifier) are: the 4 `*_certifier` types, `another_document`,
  `officer_document_requested`, `applicant_signature`, `attchment`,
  `production_process_diagram`, `factory_maps_attchment`, `factory_eia_attchment`.
  The other **19** are identical strings to `classifiable_category_t` (in-vocab).
- **OOV is derived, not stored** (see the note under `files`): `declared_category` is OOV
  iff it is not a member of `classifiable_category_t`. For OOV occurrences the dashboard
  skips the auto predicted-vs-declared comparison (it can never match); the human still
  judges the AI's guess and may supply a `corrected_type`. The 19 classifiable keys are
  identical strings in both vocabularies, so for in-vocab types
  `ai_predicted_category == declared_category` is a direct string comparison.
- AI output has **no confidence score** — classification is a single field string (longest-
  first substring match, via `tasks.parse_classification`); OCR is transcribed text only.
  Both persist the full `do_chat` dict (`{content, thinking, raw, latency_s, error}`) in
  their `*_raw` JSONB columns for later re-scoring without re-running.

Remaining open questions: annotator identity (verdicts.annotator is nullable for now),
and the precise semantics of OCR "Acceptable" (between Correct and Bad — left to the
human's judgment; the scoreline just counts the three buckets). Resolved: endpoint =
wind default `localhost:4000` + key `sk-1234`;
local file access = `filtered.csv` supplies sha256 + on-disk `local_path` for all 37,678
files (no S3 fetch); petitions come from the ~7,157 GET-mock JSONs (PK `result.id`,
joined to the CSV by `txn_id`); AI output carries no confidence — full `do_chat` dicts
persisted in JSONB for later re-scoring.
