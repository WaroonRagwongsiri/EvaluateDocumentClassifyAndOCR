"""Ad-hoc verification of the stage-scoped verdict UPSERT (the _post_verdict SQL)
against a real file_extracts row, then cleans up its own test rows so the user's
data is untouched. Run via: uv run python _verify_verdict.py"""
import sys
sys.path.insert(0, ".")
from eval.db import connect


def upsert(conn, sha, declared, page_n, stage, verdict, comment=None):
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO verdicts (sha256, declared_category, page_no, stage, verdict,
                                                 comment, annotator)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (sha256, declared_category, page_no) WHERE page_no IS NOT NULL
                                AND stage = '{stage}'
                           DO UPDATE SET verdict=EXCLUDED.verdict, comment=EXCLUDED.comment,
                                        created_at=now()""",
            (sha, declared, page_n, stage, verdict, comment, "verify"),
        )


def count_stage(conn, sha, declared, page_n, stage):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*), string_agg(verdict, ',') FROM verdicts
               WHERE sha256=%s AND declared_category=%s AND page_no=%s AND stage=%s""",
            (sha, declared, page_n, stage),
        )
        return cur.fetchone()


def main():
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT sha256, declared_category, page_no FROM file_extracts
                   WHERE ai_extract_status='done' LIMIT 1"""
            )
            r = cur.fetchone()
        if not r:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT sha256, declared_category, page_no FROM file_extracts LIMIT 1"""
                )
                r = cur.fetchone()
        assert r, "no file_extracts rows at all"
        sha, declared, page_n = r
        print(f"test context: sha={sha[:12]}... declared={declared} page={page_n}")

        # clean any prior verify rows for this exact (sha,decl,page,stage) so the
        # test starts from a known state
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM verdicts WHERE sha256=%s AND declared_category=%s
                   AND page_no=%s AND annotator='verify'""",
                (sha, declared, page_n),
            )
        conn.commit()

        # 1. insert a doctype verdict (True)
        upsert(conn, sha, declared, page_n, "doctype", "correct")
        conn.commit()
        n, vs = count_stage(conn, sha, declared, page_n, "doctype")
        print(f"after doctype(correct): doctype rows={n} verdicts={vs}  (expect 1, correct)")

        # 2. insert an ocr verdict for the SAME page — independent index, must succeed
        upsert(conn, sha, declared, page_n, "ocr", "acceptable")
        conn.commit()
        n, vs = count_stage(conn, sha, declared, page_n, "ocr")
        print(f"after ocr(acceptable): ocr rows={n} verdicts={vs}  (expect 1, acceptable)")
        n, vs = count_stage(conn, sha, declared, page_n, "doctype")
        print(f"doctype still intact: doctype rows={n} verdicts={vs}  (expect 1, correct)")

        # 3. second doctype verdict for the same page -> UPSERT (UPDATE), count stays 1
        upsert(conn, sha, declared, page_n, "doctype", "wrong")
        conn.commit()
        n, vs = count_stage(conn, sha, declared, page_n, "doctype")
        print(f"after doctype(wrong) again: doctype rows={n} verdicts={vs}  (expect 1, wrong)")

        # 4. second ocr verdict -> UPSERT, count stays 1
        upsert(conn, sha, declared, page_n, "ocr", "correct")
        conn.commit()
        n, vs = count_stage(conn, sha, declared, page_n, "ocr")
        print(f"after ocr(correct) again: ocr rows={n} verdicts={vs}  (expect 1, correct)")

        # 5. total verdict rows for this page must be exactly 2 (one per stage)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT stage, verdict FROM verdicts
                   WHERE sha256=%s AND declared_category=%s AND page_no=%s
                   ORDER BY stage""",
                (sha, declared, page_n),
            )
            rows = cur.fetchall()
        print(f"final rows for page: {rows}  (expect [('doctype','wrong'), ('ocr','correct')])")
        assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"

        # cleanup: remove verify rows so the user's data is untouched
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM verdicts WHERE sha256=%s AND declared_category=%s
                   AND page_no=%s AND annotator='verify'""",
                (sha, declared, page_n),
            )
        conn.commit()
        print("cleanup: removed test rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
