#!/usr/bin/env python3
"""
Backfill script to load historical SHAB data.
Usage: python backfill_shab.py --days 30
"""

import os
import sys
import logging
import argparse
from datetime import date, timedelta

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from routes.zevix import (
    get_conn,
    fetch_shab_neueintragungen,
    ai_branche,
    ensure_leads_table,
    RECHTSFORMEN,
)


def backfill_date(target_date: str):
    """Backfill leads for a single date."""

    logging.info("--- Processing %s ---", target_date)

    try:
        publications = fetch_shab_neueintragungen(target_date, target_date)
        logging.info("  Fetched %d entries", len(publications) if publications else 0)
    except Exception as exc:
        logging.error("  SHAB API error: %s", exc)
        return 0, 0, 0

    if not publications:
        return 0, 0, 0

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

                        # GPT Classification
                        branche = ""
                        if zweck:
                            try:
                                logging.info("    [%d/%d] %s", i + 1, len(publications), firma[:40])
                                branche = ai_branche(zweck)
                            except Exception as gpt_err:
                                logging.warning("    GPT error: %s", gpt_err)
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
                        logging.warning("    Error %s: %s", uid or "unknown", exc)
                        errors += 1
                        conn.rollback()
                        continue

    except Exception as exc:
        logging.error("  Database error: %s", exc)
        return 0, 0, len(publications)

    logging.info("  Done: +%d inserted, ~%d updated, %d errors", inserted, updated, errors)
    return inserted, updated, errors


def main():
    parser = argparse.ArgumentParser(description="Backfill historical SHAB data")
    parser.add_argument("--days", type=int, default=30, help="Number of days to backfill (default: 30)")
    parser.add_argument("--start-date", type=str, help="Start date (YYYY-MM-DD), overrides --days")
    parser.add_argument("--end-date", type=str, help="End date (YYYY-MM-DD), default: yesterday")
    args = parser.parse_args()

    # Calculate date range
    end_date = date.today() - timedelta(days=1)
    if args.end_date:
        end_date = date.fromisoformat(args.end_date)

    if args.start_date:
        start_date = date.fromisoformat(args.start_date)
    else:
        start_date = end_date - timedelta(days=args.days - 1)

    logging.info("=" * 60)
    logging.info("BACKFILL: %s to %s (%d days)", start_date, end_date, (end_date - start_date).days + 1)
    logging.info("=" * 60)

    total_inserted = 0
    total_updated = 0
    total_errors = 0

    current_date = start_date
    while current_date <= end_date:
        inserted, updated, errors = backfill_date(current_date.isoformat())
        total_inserted += inserted
        total_updated += updated
        total_errors += errors
        current_date += timedelta(days=1)

    logging.info("=" * 60)
    logging.info("BACKFILL COMPLETED")
    logging.info("  Total inserted: %d", total_inserted)
    logging.info("  Total updated: %d", total_updated)
    logging.info("  Total errors: %d", total_errors)
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
