#!/usr/bin/env python3
"""
Standalone cron script for daily SHAB sync.
Run directly: python cron_shab_sync.py
No HTTP timeout issues!
"""

import os
import sys
import logging
from datetime import date, timedelta

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Add project root to path so routes package can be imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from routes.zevix import (
    get_conn,
    fetch_shab_neueintragungen,
    ai_branche,
    ensure_leads_table,
    RECHTSFORMEN,
)


def main():
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    logging.info("=== CRON: Starting daily SHAB sync for %s ===", yesterday)

    # Fetch from SHAB API
    try:
        publications = fetch_shab_neueintragungen(yesterday, yesterday)
        logging.info("SHAB API: Fetched %d entries", len(publications) if publications else 0)
    except Exception as exc:
        logging.error("SHAB API error: %s", exc)
        sys.exit(1)

    if not publications:
        logging.info("No new entries for %s", yesterday)
        return

    inserted = 0
    updated = 0
    errors = 0

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                ensure_leads_table(cur)
                conn.commit()

                for i, pub in enumerate(publications):
                    uid = ""
                    try:
                        meta = pub.get("meta", {})
                        content = pub.get("content", {})
                        commons = content.get("commonsNew", {}) or content.get("commonsActual", {})
                        company = commons.get("company", {})
                        address = company.get("address", {})

                        uid = company.get("uid") or ""
                        if not uid:
                            continue

                        firma = company.get("name") or ""
                        rechtsform_code = company.get("legalForm") or ""
                        rechtsform = RECHTSFORMEN.get(str(rechtsform_code), str(rechtsform_code))

                        strasse = address.get("street") or ""
                        hausnummer = address.get("houseNumber") or ""
                        plz = str(address.get("swissZipCode") or "")
                        ort = address.get("town") or ""
                        sitz = company.get("seat") or ort

                        cantons = meta.get("cantons") or []
                        kanton = cantons[0] if cantons else ""

                        zweck = commons.get("purpose") or ""

                        pub_date_raw = meta.get("publicationDate") or ""
                        pub_date = pub_date_raw[:10] if pub_date_raw else None

                        # GPT Classification - with error handling
                        branche = ""
                        if zweck:
                            try:
                                logging.info("  [%d/%d] Classifying: %s", i + 1, len(publications), firma[:50])
                                branche = ai_branche(zweck)
                                logging.info("    → Branche: %s", branche)
                            except Exception as gpt_err:
                                logging.warning("    → GPT error: %s", gpt_err)
                                branche = ""

                        cur.execute(
                            """
                            INSERT INTO leads
                                (uid, firma, rechtsform, strasse, hausnummer, plz, ort,
                                 sitz, kanton, zweck, branche_ai, publikation_datum)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (uid) DO UPDATE SET
                                firma = EXCLUDED.firma,
                                rechtsform = EXCLUDED.rechtsform,
                                strasse = EXCLUDED.strasse,
                                hausnummer = EXCLUDED.hausnummer,
                                plz = EXCLUDED.plz,
                                ort = EXCLUDED.ort,
                                sitz = EXCLUDED.sitz,
                                kanton = EXCLUDED.kanton,
                                zweck = EXCLUDED.zweck,
                                branche_ai = EXCLUDED.branche_ai,
                                publikation_datum = EXCLUDED.publikation_datum
                            RETURNING (xmax = 0) AS inserted
                            """,
                            (uid, firma, rechtsform, strasse, hausnummer, plz, ort,
                             sitz, kanton, zweck, branche, pub_date)
                        )

                        row = cur.fetchone()
                        if row and row.get("inserted"):
                            inserted += 1
                        else:
                            updated += 1

                        conn.commit()

                    except Exception as exc:
                        logging.warning("Error processing entry %s: %s", uid or "unknown", exc)
                        errors += 1
                        conn.rollback()
                        continue

    except Exception as exc:
        logging.error("Database error: %s", exc)
        sys.exit(1)

    logging.info("=== CRON: Completed ===")
    logging.info("  Date: %s", yesterday)
    logging.info("  Total: %d", len(publications))
    logging.info("  Inserted: %d", inserted)
    logging.info("  Updated: %d", updated)
    logging.info("  Errors: %d", errors)


if __name__ == "__main__":
    main()
