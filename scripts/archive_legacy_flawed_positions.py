#!/usr/bin/env python3
"""
scripts/archive_legacy_flawed_positions.py

Safe archival script:
Moves all pre-July-5, 2026 legacy flawed positions (generated under pre-clean cohort rules)
from `positions` into `positions_legacy_archive`.
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

    positions_col = db["positions"]
    archive_col = db["positions_legacy_archive"]

    # Clean cohort cut point as per EXPERIMENT_CHANGELOG.md
    CLEAN_COHORT_CUTOFF = "2026-07-05"

    print("=" * 80)
    print("📦 SAFE ARCHIVE OF PRE-CLEAN COHORT LEGACY POSITIONS")
    print(f"Cutoff Date: {CLEAN_COHORT_CUTOFF}")
    print("=" * 80)

    # 1. Identify legacy flawed positions
    all_positions = list(positions_col.find({}))
    legacy_positions = []
    clean_positions = []

    for p in all_positions:
        entry_date_str = str(p.get("entry_date", ""))[:10]
        if entry_date_str < CLEAN_COHORT_CUTOFF:
            legacy_positions.append(p)
        else:
            clean_positions.append(p)

    print(f"Total positions in DB:        {len(all_positions)}")
    print(f"Legacy pre-cutoff positions:  {len(legacy_positions)}")
    print(f"Clean post-cutoff positions:   {len(clean_positions)}")

    if not legacy_positions:
        print("\n✅ No legacy positions to archive.")
        return

    # 2. Prepare documents for archive
    now_utc = datetime.now(timezone.utc)
    archived_docs = []
    legacy_ids = []

    for p in legacy_positions:
        p_copy = dict(p)
        legacy_ids.append(p["_id"])
        p_copy["archived_at"] = now_utc
        p_copy["archive_reason"] = "PRE_CLEAN_COHORT_LEGACY_LOGIC (Prior to 2026-07-05 v3.3.0 Parameter Tightening)"
        archived_docs.append(p_copy)

    # 3. Insert into positions_legacy_archive (using update_one with upsert for idempotency)
    print(f"\n[1/3] Archiving {len(archived_docs)} documents into 'positions_legacy_archive'...")
    for doc in archived_docs:
        sym = doc.get("symbol")
        archive_col.update_one({"symbol": sym, "entry_date": doc.get("entry_date")}, {"$set": doc}, upsert=True)

    archived_count = archive_col.count_documents({})
    print(f"  ✅ Archive collection now holds {archived_count} records.")

    # 4. Remove from active positions collection
    print(f"\n[2/3] Removing {len(legacy_ids)} legacy documents from 'positions'...")
    delete_result = positions_col.delete_many({"_id": {"$in": legacy_ids}})
    print(f"  ✅ Deleted {delete_result.deleted_count} legacy records from 'positions'.")

    # 5. Summary of clean active / paper portfolio
    remaining_positions = list(positions_col.find({}))
    print(f"\n[3/3] Active & Clean Portfolio Summary ({len(remaining_positions)} records):")
    print("-" * 80)
    print(f"{'Symbol':<14} {'Status':<12} {'Grade':<18} {'Entry Date':<12} {'Entry (Rs)':<10} {'PnL %':<10}")
    print("-" * 80)
    for p in sorted(remaining_positions, key=lambda x: str(x.get("entry_date", ""))):
        pnl = p.get("pnl_pct")
        pnl_str = f"{pnl:+.2f}%" if pnl is not None else "0.0%"
        print(f"{p.get('symbol'):<14} {p.get('status'):<12} {str(p.get('grade')):<18} {str(p.get('entry_date'))[:10]:<12} {str(p.get('entry_price')):<10} {pnl_str:<10}")

    print("=" * 80)
    print("✅ Archival completed safely and cleanly.")
    client.close()

if __name__ == "__main__":
    main()
