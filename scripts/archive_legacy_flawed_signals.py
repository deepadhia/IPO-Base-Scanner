#!/usr/bin/env python3
"""
scripts/archive_legacy_flawed_signals.py

Safe archival script:
Moves all pre-July-5, 2026 legacy flawed signals (generated under pre-clean cohort rules)
from `signals` into `signals_legacy_archive`.
"""

import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv
from pymongo import MongoClient

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

load_dotenv(os.path.join(PROJECT_DIR, ".env"))

def main():
    mongo_uri = os.getenv("MONGO_URI", "")
    if not mongo_uri:
        print("❌ Error: MONGO_URI not configured.")
        sys.exit(1)

    client = MongoClient(mongo_uri)
    db = client["ipo_scanner_v2"]

    signals_col = db["signals"]
    archive_col = db["signals_legacy_archive"]

    CLEAN_COHORT_CUTOFF = "2026-07-05"

    print("=" * 80)
    print("📦 SAFE ARCHIVE OF PRE-CLEAN COHORT LEGACY SIGNALS")
    print(f"Cutoff Date: {CLEAN_COHORT_CUTOFF}")
    print("=" * 80)

    all_signals = list(signals_col.find({}))
    legacy_signals = []
    clean_signals = []

    for s in all_signals:
        sig_date_str = str(s.get("signal_date") or s.get("created_at") or "")[:10]
        if sig_date_str < CLEAN_COHORT_CUTOFF:
            legacy_signals.append(s)
        else:
            clean_signals.append(s)

    print(f"Total signals in DB:        {len(all_signals)}")
    print(f"Legacy pre-cutoff signals:  {len(legacy_signals)}")
    print(f"Clean post-cutoff signals:   {len(clean_signals)}")

    if not legacy_signals:
        print("\n✅ No legacy signals to archive.")
        return

    now_utc = datetime.now(timezone.utc)
    archived_docs = []
    legacy_ids = []

    for s in legacy_signals:
        s_copy = dict(s)
        legacy_ids.append(s["_id"])
        s_copy["archived_at"] = now_utc
        s_copy["archive_reason"] = "PRE_CLEAN_COHORT_LEGACY_LOGIC (Prior to 2026-07-05 v3.3.0 Parameter Tightening)"
        archived_docs.append(s_copy)

    print(f"\n[1/2] Archiving {len(archived_docs)} documents into 'signals_legacy_archive'...")
    for doc in archived_docs:
        sig_id = doc.get("signal_id")
        if sig_id:
            archive_col.update_one({"signal_id": sig_id}, {"$set": doc}, upsert=True)
        else:
            archive_col.update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)

    archived_count = archive_col.count_documents({})
    print(f"  ✅ Archive collection now holds {archived_count} records.")

    print(f"\n[2/2] Removing {len(legacy_ids)} legacy documents from 'signals'...")
    delete_result = signals_col.delete_many({"_id": {"$in": legacy_ids}})
    print(f"  ✅ Deleted {delete_result.deleted_count} legacy records from 'signals'.")

    remaining_signals = list(signals_col.find({}))
    print(f"\nClean Signals Summary ({len(remaining_signals)} records):")
    print("-" * 80)
    for s in sorted(remaining_signals, key=lambda x: str(x.get("signal_date", ""))):
        print(f"  • {s.get('symbol'):<12} | Date: {str(s.get('signal_date'))[:10]} | Type: {s.get('signal_type', 'N/A')} | Grade: {s.get('grade', 'N/A')} | Price: Rs.{s.get('entry_price')}")

    print("=" * 80)
    print("✅ Signals archival completed safely and cleanly.")
    client.close()

if __name__ == "__main__":
    main()
