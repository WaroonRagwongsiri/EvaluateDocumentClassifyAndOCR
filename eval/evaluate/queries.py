"""SQL predicates + helper queries for the dashboard filters and review pages.

The four dashboard filters are defined per-files-row in PLAN.md; the type filters
(declared / predicted / OOV-only) AND-compose with them. OOV is derived from the
enum membership (a declared_category is OOV iff it is not one of the 19
classifiable types), queried via pg_enum so it stays in sync with the schema.
"""
from __future__ import annotations

# --- OOV detection -----------------------------------------------------------
# A declared_category value is OOV iff it is NOT a member of classifiable_category_t.
# pg_enum gives us the classifiable enum's labels; everything in declared_category_t
# not in that set is OOV. Kept as a subquery (not materialized) so it's always in
# sync with the enums.
_OOV_PREDICATE = (
    "declared_category::text NOT IN "
    "(SELECT enumlabel FROM pg_enum WHERE enumtypid = 'classifiable_category_t'::regtype)"
)
_IN_VOCAB_PREDICATE = (
    "declared_category::text IN "
    "(SELECT enumlabel FROM pg_enum WHERE enumtypid = 'classifiable_category_t'::regtype)"
)

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

# Human-done: every declared context of the file is fully reviewed. A context
# (sha256, declared_category) is fully reviewed when EVERY file_extracts page
# row for it has BOTH a 'doctype' verdict (doc_types correct?) AND an 'ocr'
# verdict (extracted data correct?). The file is human_done iff it is AI-done
# AND has >=1 context AND no file_extracts page is missing either verdict.
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
            NOT EXISTS (SELECT 1 FROM verdicts v
                        WHERE v.sha256 = x.sha256
                          AND v.declared_category = x.declared_category
                          AND v.page_no = x.page_no
                          AND v.stage = 'doctype')
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
# + NEEDS_REVIEW_CTX). A page is un-verdicted when its doctype OR its ocr
# verdict is missing.
_CTX_UNVERDICTED_PAGE = """EXISTS (
    SELECT 1 FROM file_extracts x
    WHERE x.sha256 = pf.sha256
      AND x.declared_category = pf.declared_category
      AND (
          NOT EXISTS (SELECT 1 FROM verdicts v
                      WHERE v.sha256 = x.sha256
                        AND v.declared_category = x.declared_category
                        AND v.page_no = x.page_no
                        AND v.stage = 'doctype')
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
    """DocType accuracy query. {type_clause_ctx} narrows by declared/predicted/OOV
    when the dashboard's type filters are active (references v/f via the aliases
    used above); '' otherwise. Returns (n_correct, n_total)."""
    return DOCTYPE_ACCURACY_SQL.format(type_clause_ctx=type_clause)


def ocr_accuracy_sql(type_clause: str) -> str:
    """OCR/ADE accuracy query — same {type_clause_ctx} contract. Returns
    (n_good, n_total)."""
    return OCR_ACCURACY_SQL.format(type_clause_ctx=type_clause)


# --- Type filters (AND-compose with a status filter) ------------------------
def type_filter_sql(declared: str | None, predicted: str | None, oov_only: bool) -> str:
    """Build an optional AND-clause for type filters. Returns '' or ' AND (...)'."""
    clauses: list[str] = []
    if declared:
        clauses.append(
            f"f.sha256 IN (SELECT sha256 FROM petition_files WHERE declared_category = '{declared}')"
        )
    if predicted:
        clauses.append(f"f.ai_predicted_category = '{predicted}'")
    if oov_only:
        # file's contexts are ALL OOV (at least one context, none in-vocab)
        clauses.append(
            f"(EXISTS (SELECT 1 FROM petition_files pf WHERE pf.sha256 = f.sha256) "
            f"AND NOT EXISTS (SELECT 1 FROM petition_files pf WHERE pf.sha256 = f.sha256 "
            f"AND pf.declared_category::text IN "
            f"(SELECT enumlabel FROM pg_enum WHERE enumtypid = 'classifiable_category_t'::regtype)))"
        )
    if not clauses:
        return ""
    return " AND (" + " AND ".join(clauses) + ")"


def type_filter_ctx_sql(declared: str | None, predicted: str | None, oov_only: bool) -> str:
    """Type-filter clause for the accuracy queries (queries.py DOCTYPE/OCR_ACCURACY).
    Those queries key off verdicts v + files f (v.declared_category IS the context),
    so 'declared' narrows v.declared_category directly (not via petition_files)
    and OOV is evaluated against v.declared_category. Returns '' or ' AND (...)'."""
    clauses: list[str] = []
    if declared:
        clauses.append(f"v.declared_category = '{declared}'")
    if predicted:
        clauses.append(f"f.ai_predicted_category = '{predicted}'")
    if oov_only:
        clauses.append(
            "v.declared_category::text NOT IN "
            "(SELECT enumlabel FROM pg_enum WHERE enumtypid = 'classifiable_category_t'::regtype)"
        )
    if not clauses:
        return ""
    return " AND (" + " AND ".join(clauses) + ")"


def is_oov(declared_category: str | None) -> bool:
    """Python-side OOV check (for review-page rendering). None -> treat as OOV-safe (no compare)."""
    if not declared_category:
        return True
    from .ai.classify import classifiable_fields
    return declared_category not in classifiable_fields()


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

