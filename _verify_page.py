"""Ad-hoc verification of _page_section's rendered HTML: the page card must show,
in order, Extract JSON -> Classify Doctype chips -> DocType True/False form ->
OCR/ADE Correct/Acceptable/Wrong form. Read-only. Run via: uv run python _verify_page.py"""
import sys
sys.path.insert(0, ".")
from eval.db import connect
from eval.server import _page_section


def main():
    conn = connect()
    try:
        with conn.cursor() as cur:
            # a file_extracts row that actually has a done extract (so the JSON +
            # doc_types chips render)
            cur.execute(
                """SELECT sha256, declared_category, page_no, ai_extract_status,
                          ai_extract_json, ai_extract_error, ai_extract_latency_s
                   FROM file_extracts WHERE ai_extract_status='done' LIMIT 1"""
            )
            r = cur.fetchone()
            if not r:
                cur.execute(
                    """SELECT sha256, declared_category, page_no, ai_extract_status,
                              ai_extract_json, ai_extract_error, ai_extract_latency_s
                       FROM file_extracts LIMIT 1"""
                )
                r = cur.fetchone()
        sha, declared, pno, ostatus, ejson, oerr, olat = r
        row = (pno, ostatus, ejson, oerr, olat)
        html = _page_section(sha, declared, pno, row, None, None)
    finally:
        conn.close()

    checks = {
        "Extract JSON pre": "extract-json" in html,
        "Classify Doctype block": "Classify Doctype" in html,
        "DocType verdict label": "DocType verdict" in html,
        "doctype stage hidden input": 'name="stage" value="doctype"' in html,
        "True button": "✓ True" in html,
        "False button": "✗ False" in html,
        "OCR/ADE verdict label": "OCR / ADE verdict" in html,
        "ocr stage hidden input": 'name="stage" value="ocr"' in html,
        "Correct button": "✓ Correct" in html,
        "Acceptable button": "~ Acceptable" in html,
        "Wrong button": "✗ Wrong" in html,
        "two separate .verdict-form": html.count('class="verdict-form') == 2,
        "two hidden page_no inputs": html.count('name="page_no"') == 2,
        "two hidden verdict inputs": html.count('name="verdict"') == 2,
    }
    ok = True
    for k, v in checks.items():
        print(f"  {'OK ' if v else 'FAIL'} {k}")
        ok = ok and v

    # order: doctype form must appear before ocr form
    dt_pos = html.find('name="stage" value="doctype"')
    oc_pos = html.find('name="stage" value="ocr"')
    order_ok = dt_pos != -1 and oc_pos != -1 and dt_pos < oc_pos
    print(f"  {'OK ' if order_ok else 'FAIL'} order: doctype form before ocr form")
    ok = ok and order_ok

    print("doctype form class present:", "doctype-verdict-form" in html)
    print("ALL OK" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
