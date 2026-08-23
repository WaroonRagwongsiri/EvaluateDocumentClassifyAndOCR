"""SQL predicates + helper queries for the dashboard filters and review pages.

The four dashboard filters are defined per-files-row in PLAN.md; the type filters
(declared / predicted) AND-compose with them.
"""
from __future__ import annotations


# --- Dashboard status filters (per files row f) -----------------------------

# AI-done: classification settled (done/none/error) AND no pending extract contexts.
# (skipped is NOT ai_done — nothing was classified/extracted.) A file with no
# extractable contexts (all-declared are no-extractor) is AI-done as soon as it's
# classified — file_extracts simply has no rows for it.
AI_DONE = """(
    f.ai_class_status IN ('done','none','error')
    AND NOT EXISTS (
        SELECT 1 FROM file_extracts x
        WHERE x.sha256 = f.sha256 AND x.ai_extract_status = 'pending'
    )
)"""

# Still-not-done: the negation of AI-done (the remaining backlog).
STILL_NOT_DONE = f"NOT {AI_DONE}"

# Human-say-AI-wrong: any verdict on this file is 'wrong' (class or OCR).
# 'wrong' is the single unified negative verdict for both stages.
HUMAN_SAY_AI_WRONG = """EXISTS (
    SELECT 1 FROM verdicts v
    WHERE v.sha256 = f.sha256 AND v.verdict = 'wrong'
)"""

# A file_extracts page "has a doctype to grade" iff its extract renders
# doc_types chips — done AND >=1 is_* key (nested under a 'doc_types' object,
# as the attchment/consent extractors write it, or top-level). This mirrors
# _doc_types_block in server.py so the doctype Correct/Wrong form (shown iff
# chips render) and the review-coverage SQL stay in sync: a doctype verdict is
# required exactly for pages that show a doctype form. Pages with no doc_types
# (e.g. waste_document) only need an ocr verdict. Uses the file_extracts alias
# `x` (both HUMAN_DONE and _CTX_UNVERDICTED_PAGE alias file_extracts AS x).
_PAGE_HAS_DOCTYPE = """(
    x.ai_extract_status = 'done'
    AND EXISTS (
        SELECT 1 FROM jsonb_object_keys(
            CASE WHEN jsonb_typeof(x.ai_extract_json -> 'doc_types') = 'object'
                 THEN x.ai_extract_json -> 'doc_types'
                 ELSE COALESCE(x.ai_extract_json, '{}'::jsonb)
            END
        ) AS k
        WHERE k LIKE 'is_%'
    )
)"""

# Human-done: every declared context of the file is fully reviewed. A context
# (sha256, declared_category) is fully reviewed when EVERY file_extracts page
# row for it has the verdicts its extract calls for: an 'ocr' verdict for every
# page (extracted data correct?), PLUS a 'doctype' verdict (doc_types correct?)
# only for pages that HAVE a doc_types classification (see _PAGE_HAS_DOCTYPE — a
# doctype form is shown iff the page renders doc_types chips). The file is
# human_done iff AI-done AND >=1 context AND no file_extracts page is missing a
# required verdict.
#
# A no-extractor context (no file_extracts rows) has nothing to grade, so it is
# vacuously reviewed the moment AI classification settles — the AI_DONE guard
# (not a verdict) is what finishes it. Without that guard an un-classified
# no-extractor file would read as vacuously human_done, so AI_DONE is required.
# Page coverage is per-(sha,declared,page) via file_extracts, matching the
# verdict grain (verdicts are per-page + stage). 'wrong' still counts as
# "verdicted" (it is the unified negative for both stages — see HUMAN_SAY_AI_WRONG).
HUMAN_DONE = f"""(
    {AI_DONE}
    AND EXISTS (SELECT 1 FROM petition_files pf WHERE pf.sha256 = f.sha256)
    AND NOT EXISTS (
        SELECT 1 FROM file_extracts x
        WHERE x.sha256 = f.sha256
        AND (
            ({_PAGE_HAS_DOCTYPE}
             AND NOT EXISTS (SELECT 1 FROM verdicts v
                             WHERE v.sha256 = x.sha256
                               AND v.declared_category = x.declared_category
                               AND v.page_no = x.page_no
                               AND v.stage = 'doctype'))
            OR
            NOT EXISTS (SELECT 1 FROM verdicts v
                        WHERE v.sha256 = x.sha256
                          AND v.declared_category = x.declared_category
                          AND v.page_no = x.page_no
                          AND v.stage = 'ocr')
        )
    )
)"""

# Filter name -> predicate. The dashboard lists these four with live counts.
STATUS_FILTERS: dict[str, str] = {
    "ai_done": AI_DONE,
    "human_done": HUMAN_DONE,
    "human_say_ai_wrong": HUMAN_SAY_AI_WRONG,
    "still_not_done": STILL_NOT_DONE,
}


# --- Needs-review queue -----------------------------------------------------
# "Files I need to review" = AI-finished file contexts with at least one page
# still missing a verdict. A context is (sha256, declared_category). It needs
# review when the file is AI-done AND some file_extracts page for that context
# is missing the doctype OR the ocr verdict. One row per context, joined to the
# petition for txn_id + document_no. A no-extractor context has no file_extracts
# rows -> never needs review (nothing to grade). AI_DOWN state legitimately
# empties this (nothing is AI-done yet).
#
# _CTX_UNVERDICTED_PAGE is the per-context "a page still needs a verdict"
# predicate, keyed off the petition_files alias `pf` (so it only composes
# inside a query whose FROM includes petition_files AS pf — see REVIEW_QUEUE_SQL
# + NEEDS_REVIEW_CTX). A page is un-verdicted when a required verdict is
# missing: a doctype verdict only for pages that HAVE doc_types (see
# _PAGE_HAS_DOCTYPE), and an ocr verdict for every page. Built as an f-string
# so _PAGE_HAS_DOCTYPE (which contains a literal '{}'::jsonb) is inlined once
# at definition time; the result is then passed verbatim as the {ctx_unverdicted}
# value to REVIEW_QUEUE_SQL.format(), which does not re-scan substituted values.
_CTX_UNVERDICTED_PAGE = f"""EXISTS (
    SELECT 1 FROM file_extracts x
    WHERE x.sha256 = pf.sha256
      AND x.declared_category = pf.declared_category
      AND (
          ({_PAGE_HAS_DOCTYPE}
           AND NOT EXISTS (SELECT 1 FROM verdicts v
                           WHERE v.sha256 = x.sha256
                             AND v.declared_category = x.declared_category
                             AND v.page_no = x.page_no
                             AND v.stage = 'doctype'))
          OR
          NOT EXISTS (SELECT 1 FROM verdicts v
                      WHERE v.sha256 = x.sha256
                        AND v.declared_category = x.declared_category
                        AND v.page_no = x.page_no
                        AND v.stage = 'ocr')
      )
)"""

# NEEDS_REVIEW_CTX is the per-(file,context) predicate. It references petition_files
# pf + files f (via AI_DONE), so it only composes inside a query whose FROM
# includes both with those aliases.
NEEDS_REVIEW_CTX = f"""(
    {AI_DONE}
    AND {_CTX_UNVERDICTED_PAGE}
)"""

# Flat queue rows: one per (file, declared_category) context needing review.
# The {ai_done} placeholder is AI_DONE (f.-prefixed), {ctx_unverdicted} is
# _CTX_UNVERDICTED_PAGE (pf.-prefixed), and {type_clause} is the output of
# type_filter_sql() (may be ''). All compose inside this pf/f join.
REVIEW_QUEUE_SQL = """
SELECT pf.txn_id::text, p.document_no, pf.sha256, pf.source_file_name,
       f.content_kind, f.page_count, pf.declared_category,
       f.ai_class_status, f.ai_predicted_category
FROM petition_files pf
JOIN files f ON f.sha256 = pf.sha256
LEFT JOIN petitions p ON p.id = pf.petition_id
WHERE {ai_done}
  AND {ctx_unverdicted}
  {type_clause}
ORDER BY pf.txn_id, pf.declared_category
LIMIT {limit}
"""


def review_queue_sql(type_clause: str, limit: int = 500) -> str:
    """Build the review-queue query. {type_clause} is the output of
    type_filter_sql() (may be '')."""
    return REVIEW_QUEUE_SQL.format(ai_done=AI_DONE, ctx_unverdicted=_CTX_UNVERDICTED_PAGE,
                                   type_clause=type_clause, limit=limit)


# Count form of the queue (same FROM/WHERE, no LIMIT, no ORDER BY). Used for
# the live chip count.
REVIEW_QUEUE_COUNT_SQL = """
SELECT count(*)
FROM petition_files pf
JOIN files f ON f.sha256 = pf.sha256
WHERE {ai_done}
  AND {ctx_unverdicted}
  {type_clause}
"""


def review_queue_count_sql(type_clause: str) -> str:
    return REVIEW_QUEUE_COUNT_SQL.format(ai_done=AI_DONE, ctx_unverdicted=_CTX_UNVERDICTED_PAGE,
                                          type_clause=type_clause)


# --- Human-review accuracy (dashboard stat cards) ---------------------------
# Two model-quality metrics, each scoped to AI-done AND human-reviewed rows so
# the percentage reflects only what's been both processed and verdicted. Both
# are now per-PAGE (the unit of review), keyed off file_extracts rows.
#
# DocType accuracy: of the pages a human DocType-verdicted (stage='doctype') on
# AI-extracted pages (ai_extract_status='done' on AI-classified files), the share
# marked 'correct' (= "the doc_types classification is right" / True). One count
# per page verdict.
DOCTYPE_ACCURACY_SQL = """
SELECT
  count(*) FILTER (WHERE v.verdict = 'correct')                    AS n_correct,
  count(*)                                                          AS n_total
FROM verdicts v
JOIN files f ON f.sha256 = v.sha256
JOIN file_extracts x ON x.sha256 = v.sha256
                       AND x.declared_category = v.declared_category
                       AND x.page_no = v.page_no
WHERE v.stage = 'doctype'
  AND v.page_no IS NOT NULL
  AND v.declared_category IS NOT NULL
  AND f.ai_class_status = 'done'
  AND x.ai_extract_status = 'done'
{type_clause_ctx}
"""

# OCR/ADE accuracy: of the page verdicts (stage='ocr') on pages the AI extractor
# filled (ai_extract_status='done') on AI-classified files, the share the human
# marked 'correct' OR 'acceptable'. One count per page verdict. The per-context
# join (declared_category) is more correct than a declared-agnostic per-page
# join, since file_extracts is per-(sha,declared,page) — a page verdict is only
# gradeable against the extract for the SAME declared context.
OCR_ACCURACY_SQL = """
SELECT
  count(*) FILTER (WHERE v.verdict IN ('correct','acceptable'))     AS n_good,
  count(*)                                                          AS n_total
FROM verdicts v
JOIN files f ON f.sha256 = v.sha256
JOIN file_extracts x ON x.sha256 = v.sha256
                       AND x.declared_category = v.declared_category
                       AND x.page_no = v.page_no
WHERE v.stage = 'ocr'
  AND v.page_no IS NOT NULL
  AND v.declared_category IS NOT NULL
  AND f.ai_class_status = 'done'
  AND x.ai_extract_status = 'done'
{type_clause_ctx}
"""


def doctype_accuracy_sql(type_clause: str) -> str:
    """DocType accuracy query. {type_clause_ctx} narrows by declared/predicted
    when the dashboard's type filters are active (references v/f via the aliases
    used above); '' otherwise. Returns (n_correct, n_total)."""
    return DOCTYPE_ACCURACY_SQL.format(type_clause_ctx=type_clause)


def ocr_accuracy_sql(type_clause: str) -> str:
    """OCR/ADE accuracy query — same {type_clause_ctx} contract. Returns
    (n_good, n_total)."""
    return OCR_ACCURACY_SQL.format(type_clause_ctx=type_clause)


# --- Per-verdict breakdown for the Figma-style score chunks ------------------
# Same scoping as the accuracy cards (AI-done AND human-reviewed, per-PAGE), but
# returning the count of EACH verdict value so the dashboard rings (Correct /
# Wrong for doctype; Correct / Acceptable / Wrong for ocr) each have their own
# number. n_total is the denominator for every ring in that chunk.
DOCTYPE_BREAKDOWN_SQL = """
SELECT
  count(*) FILTER (WHERE v.verdict = 'correct')                    AS n_correct,
  count(*) FILTER (WHERE v.verdict = 'wrong')                      AS n_wrong,
  count(*)                                                          AS n_total
FROM verdicts v
JOIN files f ON f.sha256 = v.sha256
JOIN file_extracts x ON x.sha256 = v.sha256
                       AND x.declared_category = v.declared_category
                       AND x.page_no = v.page_no
WHERE v.stage = 'doctype'
  AND v.page_no IS NOT NULL
  AND v.declared_category IS NOT NULL
  AND f.ai_class_status = 'done'
  AND x.ai_extract_status = 'done'
{type_clause_ctx}
"""

OCR_BREAKDOWN_SQL = """
SELECT
  count(*) FILTER (WHERE v.verdict = 'correct')                    AS n_correct,
  count(*) FILTER (WHERE v.verdict = 'acceptable')                 AS n_acceptable,
  count(*) FILTER (WHERE v.verdict = 'wrong')                      AS n_wrong,
  count(*)                                                          AS n_total
FROM verdicts v
JOIN files f ON f.sha256 = v.sha256
JOIN file_extracts x ON x.sha256 = v.sha256
                       AND x.declared_category = v.declared_category
                       AND x.page_no = v.page_no
WHERE v.stage = 'ocr'
  AND v.page_no IS NOT NULL
  AND v.declared_category IS NOT NULL
  AND f.ai_class_status = 'done'
  AND x.ai_extract_status = 'done'
{type_clause_ctx}
"""


def doctype_breakdown_sql(type_clause: str) -> str:
    """DocType breakdown: returns (n_correct, n_wrong, n_total)."""
    return DOCTYPE_BREAKDOWN_SQL.format(type_clause_ctx=type_clause)


def ocr_breakdown_sql(type_clause: str) -> str:
    """OCR breakdown: returns (n_correct, n_acceptable, n_wrong, n_total)."""
    return OCR_BREAKDOWN_SQL.format(type_clause_ctx=type_clause)


# --- Drill-down list for a dashboard score-chunk ring -------------------------
# Same scoping as the breakdown queries (AI-done AND human-reviewed, per-PAGE),
# but returns the matching ROWS (one per page verdict with the given stage +
# verdict) so /verdict-pages can list them. verdict is validated in server.py
# before interpolation (it's one of correct/acceptable/wrong).
VERDICT_PAGES_SQL = """
SELECT v.sha256, v.page_no, v.declared_category, v.verdict, v.comment,
       f.ai_predicted_category, f.page_count,
       (SELECT pf.txn_id::text FROM petition_files pf
         WHERE pf.sha256 = v.sha256
         ORDER BY pf.declared_category LIMIT 1) AS txn_id
FROM verdicts v
JOIN files f ON f.sha256 = v.sha256
JOIN file_extracts x ON x.sha256 = v.sha256
                       AND x.declared_category = v.declared_category
                       AND x.page_no = v.page_no
WHERE v.stage = '{stage}'
  AND v.verdict = '{verdict}'
  AND v.page_no IS NOT NULL
  AND v.declared_category IS NOT NULL
  AND f.ai_class_status = 'done'
  AND x.ai_extract_status = 'done'
{type_clause_ctx}
ORDER BY v.sha256, v.page_no
LIMIT 500
"""


def verdict_pages_sql(stage: str, verdict: str, type_clause_ctx: str) -> str:
    """Per-page rows behind one score-chunk ring. stage: 'doctype'|'ocr';
    verdict: 'correct'|'acceptable'|'wrong' (both validated by the caller)."""
    return VERDICT_PAGES_SQL.format(stage=stage, verdict=verdict,
                                    type_clause_ctx=type_clause_ctx)


# --- Confusion-matrix rows for /classify-score --------------------------------
# One row per doctype-verdicted page (same scoping as the accuracy queries:
# AI-done AND human-reviewed, per-PAGE), carrying the verdict + corrected_type
# + the page's raw extract JSON. The AI's doc_types slug and the human class
# are resolved in Python (server.py) — the is_* key → slug mapping is
# nontrivial in SQL (keys nest under 'doc_types' for some extractors, sit at
# the top level for others; see _doc_types_block).
CONFUSION_SQL = """
SELECT v.sha256, v.page_no, v.verdict, v.corrected_type, x.ai_extract_json
FROM verdicts v
JOIN files f ON f.sha256 = v.sha256
JOIN file_extracts x ON x.sha256 = v.sha256
                       AND x.declared_category = v.declared_category
                       AND x.page_no = v.page_no
WHERE v.stage = 'doctype'
  AND v.page_no IS NOT NULL
  AND v.declared_category IS NOT NULL
  AND f.ai_class_status = 'done'
  AND x.ai_extract_status = 'done'
{type_clause_ctx}
"""


def confusion_sql(type_clause_ctx: str) -> str:
    """Confusion-matrix rows for /classify-score. {type_clause_ctx} narrows by
    declared/predicted when the dashboard's type filters are active
    (references v/f via the aliases used above); '' otherwise."""
    return CONFUSION_SQL.format(type_clause_ctx=type_clause_ctx)


# --- Type filters (AND-compose with a status filter) ------------------------
def type_filter_sql(declared: str | None, predicted: str | None) -> str:
    """Build an optional AND-clause for type filters. Returns '' or ' AND (...)'."""
    clauses: list[str] = []
    if declared:
        clauses.append(
            f"f.sha256 IN (SELECT sha256 FROM petition_files WHERE declared_category = '{declared}')"
        )
    if predicted:
        clauses.append(f"f.ai_predicted_category = '{predicted}'")
    if not clauses:
        return ""
    return " AND (" + " AND ".join(clauses) + ")"


def type_filter_ctx_sql(declared: str | None, predicted: str | None) -> str:
    """Type-filter clause for the accuracy queries (queries.py DOCTYPE/OCR_ACCURACY).
    Those queries key off verdicts v + files f (v.declared_category IS the context),
    so 'declared' narrows v.declared_category directly (not via petition_files).
    Returns '' or ' AND (...)'."""
    clauses: list[str] = []
    if declared:
        clauses.append(f"v.declared_category = '{declared}'")
    if predicted:
        clauses.append(f"f.ai_predicted_category = '{predicted}'")
    if not clauses:
        return ""
    return " AND (" + " AND ".join(clauses) + ")"


# --- Petition card query (dashboard landing grid) ---------------------------
# One row per petition that has >=1 file matching the active status+type filters.
# Carries a per-petition file-status mix (done/none/error/skipped/pending) so the
# card can show progress badges. `status_predicate` is a STATUS_FILTERS value or
# None (None = all petitions with files, the unfiltered landing). `type_clause`
# is the output of type_filter_sql() (may be '').
PETITION_CARDS_SQL = """
SELECT p.id::text, p.txn_id::text, p.document_no, p.state,
       count(DISTINCT f.sha256) AS n_files,
       count(DISTINCT f.sha256) FILTER (WHERE f.ai_class_status='done')   AS n_done,
       count(DISTINCT f.sha256) FILTER (WHERE f.ai_class_status='error')  AS n_error,
       count(DISTINCT f.sha256) FILTER (WHERE f.ai_class_status='none')   AS n_none,
       count(DISTINCT f.sha256) FILTER (WHERE f.ai_class_status='skipped')AS n_skipped,
       count(DISTINCT f.sha256) FILTER (WHERE f.ai_class_status='pending')AS n_pending,
       count(DISTINCT f.sha256) FILTER (WHERE EXISTS (
           SELECT 1 FROM verdicts v WHERE v.sha256=f.sha256))             AS n_reviewed
FROM petitions p
JOIN petition_files pf ON pf.petition_id = p.id
JOIN files f ON f.sha256 = pf.sha256
WHERE {where}
GROUP BY p.id, p.txn_id, p.document_no, p.state
-- biggest/most-reviewable petitions first, then by document_no for stable tiebreak
ORDER BY n_files DESC, p.document_no NULLS LAST
LIMIT {limit}
"""


def petition_cards_sql(status_predicate: str | None, type_clause: str, limit: int = 120) -> str:
    """Build the petition-cards query. When status_predicate is None, the cards
    show every petition with files (the unfiltered landing)."""
    where = "1=1"
    if status_predicate:
        where = status_predicate  # already references f.*
    return PETITION_CARDS_SQL.format(where=where + type_clause, limit=limit)

