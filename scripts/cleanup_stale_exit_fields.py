"""
cleanup_stale_exit_fields.py
----------------------------
One-time utility to remove stale exit-cycle fields from ACTIVE / PAPER_ONLY
positions in the ipo_scanner_v2 MongoDB collection.

These fields can be left behind when a position that was previously CLOSED is
re-opened by the scanner without first unsetting the exit metadata:
  exit_date, exit_price, pnl_pct, days_held, outcome_type,
  holding_efficiency_pct, time_to_failure_days, time_to_failure_min,
  max_runup_pct, max_drawdown_pct

Usage
-----
  # Preview affected documents (no writes):
  python scripts/cleanup_stale_exit_fields.py

  # Apply the cleanup:
  python scripts/cleanup_stale_exit_fields.py --execute
"""

import sys
import os
import argparse
from datetime import datetime, timezone

# ── allow running from the repo root ──────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

# ── constants ─────────────────────────────────────────────────────────────────

MONGO_URI  = os.getenv("MONGO_URI", "")
DB_NAME    = "ipo_scanner_v2"
COLLECTION = "positions"

OPEN_STATUSES = ["ACTIVE", "PAPER_ONLY"]

# Fields that must be absent on any open position
STALE_EXIT_FIELDS = [
    "exit_date",
    "exit_price",
    "pnl_pct",
    "days_held",
    "outcome_type",
    "holding_efficiency_pct",
    "time_to_failure_days",
    "time_to_failure_min",
    "max_runup_pct",
    "max_drawdown_pct",
]


def build_stale_filter() -> dict:
    """
    Match ACTIVE / PAPER_ONLY positions that contain at least one of the
    stale exit fields (with any non-null / non-empty value).
    """
    or_clauses = [
        {field: {"$exists": True}}
        for field in STALE_EXIT_FIELDS
    ]
    return {
        "status": {"$in": OPEN_STATUSES},
        "$or": or_clauses,
    }


def dry_run(col) -> list:
    """Return a preview list of affected documents (symbol + stale fields)."""
    affected = []
    stale_filter = build_stale_filter()
    projection = {"symbol": 1, "status": 1, "_id": 0}
    for field in STALE_EXIT_FIELDS:
        projection[field] = 1

    for doc in col.find(stale_filter, projection):
        present_stale = [f for f in STALE_EXIT_FIELDS if f in doc]
        affected.append({
            "symbol": doc.get("symbol"),
            "status": doc.get("status"),
            "stale_fields": present_stale,
        })
    return affected


def execute_cleanup(col) -> int:
    """
    Remove stale exit fields from all affected documents.
    Returns the count of modified documents.
    """
    stale_filter = build_stale_filter()
    unset_payload = {field: "" for field in STALE_EXIT_FIELDS}

    result = col.update_many(
        stale_filter,
        {
            "$unset": unset_payload,
            "$set":   {"cleanup_applied_at": datetime.now(timezone.utc)},
        }
    )
    return result.modified_count


def main():
    parser = argparse.ArgumentParser(
        description="Remove stale exit fields from ACTIVE / PAPER_ONLY positions."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the cleanup. Without this flag the script runs in dry-run mode.",
    )
    args = parser.parse_args()

    if not MONGO_URI:
        print("ERROR: MONGO_URI is not set in .env")
        sys.exit(1)

    client = MongoClient(MONGO_URI, tz_aware=True)
    col    = client[DB_NAME][COLLECTION]

    print(f"\n{'='*60}")
    print(f"  IPO Scanner -- Stale Exit Field Cleanup")
    print(f"  Database  : {DB_NAME}")
    print(f"  Collection: {COLLECTION}")
    print(f"  Mode      : {'EXECUTE (live writes)' if args.execute else 'DRY-RUN (read-only)'}")
    print(f"{'='*60}\n")

    # ── dry-run preview ───────────────────────────────────────────────────────
    affected = dry_run(col)

    if not affected:
        print("[OK] No affected documents found. Collection is already clean.")
        client.close()
        return

    print(f"Found {len(affected)} document(s) with stale exit fields:\n")
    for item in affected:
        print(f"  [{item['status']}] {item['symbol']}")
        print(f"         stale fields: {', '.join(item['stale_fields'])}")

    if not args.execute:
        print(f"\n[DRY-RUN] Run with --execute to apply the cleanup.")
        print(f"  Tip: run mongodb_backup.py first to snapshot the collection.\n")
        client.close()
        return

    # ── live cleanup ──────────────────────────────────────────────────────────
    print(f"\nApplying cleanup to {len(affected)} document(s) ...")
    modified = execute_cleanup(col)
    print(f"\n[OK] Cleanup complete. Modified: {modified} document(s).")

    # ── post-run verification ─────────────────────────────────────────────────
    remaining = dry_run(col)
    if remaining:
        print(f"\n[WARN] {len(remaining)} document(s) still have stale fields. Re-run may be needed.")
    else:
        print("[OK] Post-run check passed -- no stale exit fields remain on open positions.")

    client.close()


if __name__ == "__main__":
    main()
