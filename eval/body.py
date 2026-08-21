"""Build the "Request Body" payload for the petition detail modal.

This is the bridge between the vendored ``eval.ai.build_dataset`` (which reads
``raw_json.result.data`` + the local doc root + master-data geo files) and the
eval app's ``files`` table (which carries each file's ``sha256`` / ``content_kind``
/ ``page_count`` for the 👁️ preview route).

``build_request_body`` is called lazily — only by the ``GET /body/<txn_id>``
endpoint on click — so the heavy disk reads (geo master-data + attachment tree
walk) never happen on page load. A CSV-only petition (``raw_json`` NULL) returns
``None`` so the caller shows "no request body" instead of the button.
"""
from __future__ import annotations

import logging
from typing import Any

from eval import config
from eval.ai import build_dataset as BDS
from eval.db import connect

log = logging.getLogger("eval.body")


def build_request_body(petition_row: tuple) -> dict[str, Any] | None:
    """Build the request body dict for a petition.

    ``petition_row`` is the (id, txn_id, document_no, state, raw_json) tuple from
    the detail-page lookup — we only read ``raw_json`` here. Returns ``None`` for
    a CSV-only petition (no raw_json) so the caller renders the "no request body"
    text instead of the modal button.

    On success returns::

        {"dataString": <6-block obj>,
         "dataString_blocks": 6,
         "pdfs": [...], "imgs": [...],   # each row enriched with sha256/kind/pages
         "pdf_found": n, "pdf_total": m,
         "img_found": n, "img_total": m}

    Each pdfs/imgs row carries: ``category`` (declared category), ``filetype``
    (Thai label), ``filename`` (display name, "" for MISSING), ``status``
    ("FOUND"/"MISSING"), and — for FOUND rows whose ``key`` (== local filename)
    joins to a ``files`` row — ``sha256`` / ``content_kind`` / ``page_count`` so
    the modal's 👁️ preview targets ``/page/<sha>/1`` (render.py renders PDF page
    1 and images alike). A FOUND file not in the files table keeps status FOUND
    but has sha256=None (no preview button).
    """
    # petition_row = (id, txn_id, document_no, state, raw_json)
    raw_json = petition_row[4]
    if raw_json is None:
        return None

    try:
        data = raw_json["result"]["data"]
    except (KeyError, TypeError):
        log.warning("raw_json missing result.data; treating as no request body")
        return None

    geo = BDS.GeoResolver(config.GEO_MASTER_DIR)
    data_string = BDS.build_datastring(data, geo)
    _pdfs, _imgs, manifest = BDS.build_attachments(data, str(config.DOC_ROOT))

    # Enrich FOUND rows by joining their key (== local filename == files.filename)
    # to the files table, attaching sha256/content_kind/page_count for the preview.
    found_keys = [m["key"] for m in manifest if m["status"] == "FOUND" and m.get("key")]
    key_to_file: dict[str, tuple[str, str, int]] = {}
    if found_keys:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT filename, sha256, content_kind, page_count FROM files "
                "WHERE filename = ANY(%s)",
                (found_keys,),
            )
            for fn, sha, kind, pages in cur.fetchall():
                key_to_file[fn] = (sha, kind, pages)
            conn.commit()

    # Build the modal rows straight from the manifest (it already has status +
    # key + category + filetype + filename per row), splitting by bucket.
    def _rows(bucket: str) -> tuple[list[dict], int, int]:
        out, found, total = [], 0, 0
        for m in manifest:
            if m.get("bucket") != bucket:
                continue
            total += 1
            row = {
                "category": m["category"],
                "filetype": m["filetype"],
                "filename": m.get("filename") or "",
                "status": m["status"],
            }
            if m["status"] == "FOUND":
                found += 1
                f = key_to_file.get(m.get("key"))
                if f:
                    row["sha256"] = f[0]
                    row["content_kind"] = f[1]
                    row["page_count"] = f[2]
                else:
                    row["sha256"] = None
                    row["content_kind"] = None
                    row["page_count"] = None
            else:
                row["sha256"] = None
                row["content_kind"] = None
                row["page_count"] = None
            out.append(row)
        return out, found, total

    pdfs, pdf_found, pdf_total = _rows("pdf")
    imgs, img_found, img_total = _rows("image")

    return {
        "dataString": data_string,
        "dataString_blocks": len(data_string),
        "pdfs": pdfs,
        "imgs": imgs,
        "pdf_found": pdf_found,
        "pdf_total": pdf_total,
        "img_found": img_found,
        "img_total": img_total,
    }
