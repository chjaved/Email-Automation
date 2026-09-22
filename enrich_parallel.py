"""Parallel enrichment runner - enriches all 'new' leads concurrently."""
import concurrent.futures
import json
import logging

from config import setup_logging
from db import get_conn, init_db
from enricher import enrich_lead

setup_logging()
logger = logging.getLogger(__name__)

WORKERS = 5


def _enrich_one(lead) -> tuple:
    try:
        data = enrich_lead(lead)
        return lead["id"], "enriched", data, None
    except Exception as e:
        return lead["id"], "enrichment_failed", None, str(e)


def main() -> None:
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM leads WHERE status = 'new' ORDER BY id")
    leads = cur.fetchall()
    conn.close()
    total = len(leads)
    print(f"Enriching {total} leads with {WORKERS} workers")

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for lead_id, status, data, err in ex.map(_enrich_one, leads):
            conn = get_conn()
            cur = conn.cursor()
            if status == "enriched":
                cur.execute(
                    "UPDATE leads SET industry = COALESCE(NULLIF(?, ''), industry), "
                    "enriched_data = ?, status = 'enriched' WHERE id = ?",
                    (data.get("industry", "other"), json.dumps(data, ensure_ascii=False), lead_id),
                )
            else:
                cur.execute("UPDATE leads SET status = 'enrichment_failed' WHERE id = ?", (lead_id,))
                logger.warning("Enrichment failed for lead %s: %s", lead_id, err)
            conn.commit()
            conn.close()
            done += 1
            if done % 10 == 0 or done == total:
                print(f"{done}/{total} done")

    print("Enrichment complete.")


if __name__ == "__main__":
    main()
