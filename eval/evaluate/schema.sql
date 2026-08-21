-- E-License Document Classification + OCR Evaluation App
-- Postgres 15+, database `evalutea_classi_ocr`.
-- Idempotent: safe to re-run (CREATE TYPE ... ON CONFLICT-safe via DO blocks,
-- CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS).

-- ============================================================================
-- Enums
-- ============================================================================

-- 19 classifiable types (policy_loader.list_classifiable_types()). The classifier
-- outputs one of these, or the literal "none" sentinel (handled as
-- ai_class_status='none', NOT a member of this enum).
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

-- Declared categories: the 26 build_dataset.ATTACH_ORDER keys PLUS the 4 extras
-- that actually appear in filtered.csv.table_column_name as real buckets:
-- attchment, production_process_diagram, factory_maps_attchment, factory_eia_attchment.
-- = 30 members. The 19 that overlap classifiable_category_t are in-vocab; the 11
-- rest are OOV (never predictable by the classifier).
CREATE TYPE declared_category_t AS ENUM (
  -- --- 19 in-vocab (identical to classifiable_category_t) ---
  'juristic_person_certificate','land_map_diagram','factory_building_plan',
  'factory_safety_certificate','factory_building_diagram','machine_installation_diagram',
  'waste_document','emissions_document','name_change_certificate',
  'power_of_attorney_with_revenue_stamp','factory_document_of_right',
  'consent_document_to_set_up_factory','power_of_attorney_competent_authority',
  'copy_of_house_registration_factory_location','copy_of_professional_engineering_license',
  'factory_operation_risk','environmental_risk','environmental_impact_eia',
  'environmental_impact_iee',
  -- --- 11 OOV (never predictable by the classifier) ---
  'factory_building_plan_certifier','machine_installation_diagram_certifier',
  'waste_document_certifier','emissions_document_certifier','another_document',
  'officer_document_requested','applicant_signature',
  'attchment','production_process_diagram','factory_maps_attchment','factory_eia_attchment'
);

-- ============================================================================
-- Tables
-- ============================================================================

-- petitions — one row per GET-mock petition (~7,157 JSONs).
CREATE TABLE IF NOT EXISTS petitions (
  id           uuid PRIMARY KEY,            -- result.id (from JSON; also in filename)
  txn_id       uuid UNIQUE,                  -- result.txn_id (join key to filtered.csv)
  document_no  text,                         -- e.g. '69-10-RG-000536'
  state        text,                         -- e.g. 'accept'
  raw_json     jsonb,                        -- full GET-mock result (NULL if known only via CSV)
  indexed_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS petitions_document_no_idx ON petitions (document_no);
CREATE INDEX IF NOT EXISTS petitions_state_idx       ON petitions (state);

-- files — one row per unique content (sha256). AI classification lives here (1:1).
CREATE TABLE IF NOT EXISTS files (
  sha256                   text PRIMARY KEY,          -- = filtered.csv.hashed_value
  local_path               text NOT NULL,             -- = filtered.csv.local_file_path||'/'||filename
  filename                 text NOT NULL,             -- filtered.csv.filename
  ext                      text,                      -- lower(suffix): pdf|png|jpg|docx...
  content_kind              text NOT NULL,             -- pdf | image | other
  size_bytes               bigint NOT NULL,
  page_count              int  NOT NULL,             -- via PyMuPDF; 1 for images; 0 for unprocessable
  declared_filetype_first  declared_category_t,      -- first-seen declared category (fast-path prefill)

  -- AI classification (once per sha256; prompt is content-only, no declared type)
  ai_class_status         text NOT NULL DEFAULT 'pending',  -- pending|done|none|error|skipped
  ai_predicted_category   classifiable_category_t,          -- set when status='done'; NULL otherwise
  ai_class_latency_s     real,
  ai_class_error          text,                              -- set when status='error'
  ai_class_raw           jsonb,                             -- full do_chat dict
  ai_class_model         text,
  ai_class_at            timestamptz,

  first_seen_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS files_pending_idx ON files (sha256) WHERE ai_class_status = 'pending';

-- file_pages — one row per page; AI OCR lives here (1:N). Created by the indexer.
CREATE TABLE IF NOT EXISTS file_pages (
  sha256           text NOT NULL REFERENCES files(sha256) ON DELETE CASCADE,
  page_no          int  NOT NULL,               -- 1-based
  ai_ocr_status    text NOT NULL DEFAULT 'pending',  -- pending|done|error
  ai_ocr_text      text,                              -- do_chat.content
  ai_ocr_latency_s real,
  ai_ocr_error     text,
  ai_ocr_raw       jsonb,                             -- full do_chat dict
  ai_ocr_model     text,
  ai_ocr_at        timestamptz,
  PRIMARY KEY (sha256, page_no)
);
CREATE INDEX IF NOT EXISTS file_pages_pending_idx ON file_pages (sha256, page_no) WHERE ai_ocr_status = 'pending';

-- file_extracts — one row per (sha256, declared_category, page_no); the real
-- per-filetype production extractor's per-page JSON lives here. This is the
-- single per-page AI store: the extractor is keyed by the DECLARED filetype, so
-- its output is per-(sha, declared, page) — a file appears under multiple declared
-- contexts (petition_files), each getting its own extractor run. Pre-created by
-- the indexer for every (sha, declared) context that has an extractor
-- (eval.ai.extract.extract_fn_for) and page_count>0; the worker fills the AI
-- columns. Retires file_pages from use (file_pages DDL left above, vestigial —
-- the indexer still creates its rows but nothing reads/writes them anymore).
CREATE TABLE IF NOT EXISTS file_extracts (
  sha256            text NOT NULL REFERENCES files(sha256) ON DELETE CASCADE,
  declared_category declared_category_t NOT NULL,
  page_no           int  NOT NULL,               -- 1-based, matches extractor pages[*].page
  ai_extract_status text NOT NULL DEFAULT 'pending',  -- pending|done|error
  ai_extract_json   jsonb,                       -- per-page result dict (is_X + data + ocr_text), WITHOUT rotated_base64
  ai_extract_latency_s real,
  ai_extract_error  text,
  ai_extract_raw    jsonb,                       -- full extractor response (debug), WITHOUT rotated_base64
  ai_extract_model  text,
  ai_extract_at     timestamptz,
  PRIMARY KEY (sha256, declared_category, page_no)
);
CREATE INDEX IF NOT EXISTS file_extracts_pending_idx
  ON file_extracts (sha256) WHERE ai_extract_status = 'pending';

-- petition_files — many-to-many: a file as it appears in a petition under a declared type.
CREATE TABLE IF NOT EXISTS petition_files (
  petition_id       uuid NOT NULL REFERENCES petitions(id) ON DELETE CASCADE,
  sha256            text NOT NULL REFERENCES files(sha256)  ON DELETE CASCADE,
  declared_category declared_category_t NOT NULL,         -- = filtered.csv.table_column_name
  txn_id            uuid,                                  -- = filtered.csv.txn_id (traceability)
  source_table      text,                                  -- filtered.csv.table_name
  source_column     text,                                  -- filtered.csv.table_column_name (raw)
  source_file_name  text,                                  -- filtered.csv.filename (display)
  indexed_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (petition_id, sha256, declared_category)
);
CREATE INDEX IF NOT EXISTS petition_files_sha256_idx     ON petition_files (sha256);
CREATE INDEX IF NOT EXISTS petition_files_declared_idx   ON petition_files (declared_category);
CREATE INDEX IF NOT EXISTS petition_files_petition_idx   ON petition_files (petition_id);

-- verdicts — human review. Two independent per-page checks, one row each:
--   stage='doctype' : is the page's doc_types classification correct?  verdict IN ('correct','wrong')
--                     (correct = "the doc_types is right", wrong = "it's not") — i.e. True / False.
--   stage='ocr'     : is the extracted data correct?                  verdict IN ('correct','acceptable','wrong')
-- Every verdict row is per-page (page_no is always set). The old "page_no NULL = 19-way
-- file-class verdict" model was removed; the migration below drops those rows and tags
-- the surviving per-page rows with stage. Uniqueness is enforced by two stage-scoped
-- partial unique indexes (Postgres PK cols are NOT NULL, but the CHECK — not the column
-- definition — is what forces page_no non-null, so the column stays nullable to keep the
-- table migrateable without an ALTER COLUMN).
CREATE TABLE IF NOT EXISTS verdicts (
  sha256            text NOT NULL REFERENCES files(sha256) ON DELETE CASCADE,
  declared_category declared_category_t NOT NULL,
  page_no           int,                                   -- 1-based page; always set (NULL rows removed)
  stage             text NOT NULL DEFAULT 'ocr',          -- 'doctype' | 'ocr' (which per-page check)
  verdict           text NOT NULL,
  corrected_type    declared_category_t,                   -- vestigial (class verdict removed); left in place
  comment           text,                                  -- free text (any stage)
  annotator         text,                                  -- who (open; nullable for now)
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT verdicts_check CHECK (
    page_no IS NOT NULL
    AND ( (stage='doctype' AND verdict IN ('correct','wrong'))
          OR (stage='ocr' AND verdict IN ('correct','acceptable','wrong')) )
  )
);
CREATE INDEX IF NOT EXISTS verdicts_lookup_idx   ON verdicts (sha256, declared_category);
CREATE INDEX IF NOT EXISTS verdicts_verdict_idx   ON verdicts (verdict);
CREATE INDEX IF NOT EXISTS verdicts_annotator_idx ON verdicts (annotator);

-- ── migration from the old "page_no NULL = class verdict" model ──────────────
-- The 19-way file-class verdict was removed; every verdict row is now per-page and
-- tagged with a stage ('doctype' | 'ocr'). These statements are idempotent so
-- apply_schema (db.py) can re-run schema.sql on every boot:
ALTER TABLE verdicts ADD COLUMN IF NOT EXISTS stage text NOT NULL DEFAULT 'ocr';
DELETE FROM verdicts WHERE page_no IS NULL;            -- drop orphaned class rows (no UI now)
UPDATE verdicts SET stage='ocr' WHERE stage IS NULL OR stage='';
ALTER TABLE verdicts DROP CONSTRAINT IF EXISTS verdicts_check;
ALTER TABLE verdicts ADD CONSTRAINT verdicts_check CHECK (
    page_no IS NOT NULL
    AND ( (stage='doctype' AND verdict IN ('correct','wrong'))
          OR (stage='ocr' AND verdict IN ('correct','acceptable','wrong')) )
);
-- drop the old class-row + single-OCR unique indexes, then add the two stage-scoped ones:
DROP INDEX IF EXISTS verdicts_class_uniq;              -- old class (page_no NULL) unique index
DROP INDEX IF EXISTS verdicts_page_uniq;               -- old single OCR unique index (no stage)
-- one DocType (True/False) verdict per (sha256, declared_category, page_no):
CREATE UNIQUE INDEX IF NOT EXISTS verdicts_doctype_uniq
  ON verdicts (sha256, declared_category, page_no) WHERE page_no IS NOT NULL AND stage='doctype';
-- one OCR/ADE (Correct/Acceptable/Wrong) verdict per (sha256, declared_category, page_no):
CREATE UNIQUE INDEX IF NOT EXISTS verdicts_ocr_uniq
  ON verdicts (sha256, declared_category, page_no) WHERE page_no IS NOT NULL AND stage='ocr';

-- run_control — single row for worker stop/continue.
CREATE TABLE IF NOT EXISTS run_control (
  id              int PRIMARY KEY CHECK (id = 1),  -- enforces the single row
  want_stop       boolean NOT NULL DEFAULT false,  -- worker polls each iteration & between pages
  state           text NOT NULL DEFAULT 'idle',    -- idle|running|stopping (server-maintained)
  last_started_at timestamptz,
  last_stopped_at timestamptz,
  last_exit_code  int,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Bootstrap the single run_control row.
INSERT INTO run_control (id) VALUES (1) ON CONFLICT DO NOTHING;
