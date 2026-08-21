"""Ad-hoc verification that the dashboard + review-queue queries execute without
SQL error after the page-coverage rewrite. Read-only (no writes). Run via:
uv run python _verify_queries.py"""
import sys
sys.path.insert(0, ".")
from eval.db import connect
from eval import queries


def main():
    conn = connect()
    try:
        with conn.cursor() as cur:
            # dashboard accuracy cards (steps 5)
            cur.execute(queries.doctype_accuracy_sql(""))
            dt_correct, dt_total = cur.fetchone()
            print(f"doctype_accuracy_sql: correct={dt_correct} total={dt_total}")
            cur.execute(queries.ocr_accuracy_sql(""))
            oc_good, oc_total = cur.fetchone()
            print(f"ocr_accuracy_sql:      good={oc_good} total={oc_total}")

            # reviewed-files hero stat (page_no IS NOT NULL now)
            cur.execute("SELECT count(DISTINCT sha256) FROM verdicts WHERE page_no IS NOT NULL")
            print("c_reviewed_files =", cur.fetchone()[0])

            # four status filters + needs-review queue (steps 5, 7)
            for name, pred in queries.STATUS_FILTERS.items():
                cur.execute(f"SELECT count(*) FROM files f WHERE {pred}")
                print(f"status_filter {name} = {cur.fetchone()[0]}")
            cur.execute(queries.review_queue_count_sql(""))
            print("review_queue_count =", cur.fetchone()[0])

            # a sample review-queue row (the actual list query)
            cur.execute(queries.review_queue_sql("", limit=3))
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
            print("review_queue_sql cols:", cols)
            print("review_queue_sql first rows:", rows[:2])

            # step 6: the /txn coverage pill for one context
            cur.execute(
                """SELECT pf.sha256, pf.declared_category FROM petition_files pf
                   JOIN files f ON f.sha256=pf.sha256
                   LIMIT 1"""
            )
            sr = cur.fetchone()
            if sr:
                # import the server-side helper (render pill)
                from eval.server import _review_coverage_pill
                pill = _review_coverage_pill(sr[0], sr[1])
                print(f"_review_coverage_pill({sr[1]}): {pill!r}")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
