#!/usr/bin/env python3
"""
scripts/stamp_active_versions.py

One-off migration: stamps strategy_version, execution_version, and
risk_model_version onto all existing ACTIVE signals and positions that
were created before version metadata was introduced in v2.5.0.

Routing logic:
  - grade == "LISTING_BREAKOUT"  -> strategy_version = "2.5.0-listing-day"
  - grade in (A+, A, B, C, etc) -> strategy_version = "2.5.0-consolidation"

Both collections get:
  - execution_version  = "2.5.0-single-writer"
  - risk_model_version = "2.5.0-archetype-velocity"

Usage:
  python scripts/stamp_active_versions.py [--dry-run]

Flags:
  --dry-run   Print what would be updated without writing to the database.
"""

import sys
import os
from datetime import datetime, timezone

# Bootstrap path so we can import from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from db import signals_col, positions_col
except ImportError:
    print("[Error] Could not import database connections from db.py")
    sys.exit(1)

# ── Version constants (must match what the scanners now stamp) ──────────────
EXECUTION_VERSION  = "2.5.0-single-writer"
RISK_MODEL_VERSION = "2.5.0-archetype-velocity"
STRATEGY_CONSOLIDATION = "2.5.0-consolidation"
STRATEGY_LISTING_DAY   = "2.5.0-listing-day"

LISTING_GRADES = {"LISTING_BREAKOUT"}


def resolve_strategy_version(doc: dict) -> str:
    """Return the correct strategy_version for a given document."""
    grade        = doc.get("grade", "")
    signal_type  = doc.get("signal_type", "")
    scanner      = doc.get("scanner", "")

    if (grade in LISTING_GRADES
            or signal_type == "LISTING_DAY_BREAKOUT"
            or scanner == "listing_day"):
        return STRATEGY_LISTING_DAY
    return STRATEGY_CONSOLIDATION


def stamp_collection(col, col_name: str, dry_run: bool) -> tuple[int, int]:
    """
    Stamp version fields onto ACTIVE documents that are missing them.

    Returns (stamped_count, already_stamped_count).
    """
    stamped = 0
    already_ok = 0

    # Only touch ACTIVE docs — do not modify CLOSED history
    docs = list(col.find({"status": "ACTIVE"}))

    for doc in docs:
        sym = doc.get("symbol", "UNKNOWN")
        has_versions = (
            "strategy_version"   in doc
            and "execution_version"  in doc
            and "risk_model_version" in doc
        )

        if has_versions:
            already_ok += 1
            print(f"  [SKIP] {col_name} | {sym}: already stamped "
                  f"({doc['strategy_version']})")
            continue

        strategy_ver = resolve_strategy_version(doc)

        if dry_run:
            print(f"  [DRY-RUN] Would stamp {col_name} | {sym}: "
                  f"strategy={strategy_ver} | exec={EXECUTION_VERSION} | "
                  f"risk={RISK_MODEL_VERSION}")
            stamped += 1
            continue

        # Perform the actual update
        result = col.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "strategy_version":   strategy_ver,
                "execution_version":  EXECUTION_VERSION,
                "risk_model_version": RISK_MODEL_VERSION,
                "updated_at":         datetime.now(timezone.utc),
            }}
        )

        if result.modified_count == 1:
            print(f"  [STAMPED] {col_name} | {sym}: strategy={strategy_ver}")
            stamped += 1
        else:
            print(f"  [WARN] {col_name} | {sym}: update returned "
                  f"modified_count={result.modified_count}")

    return stamped, already_ok


def run(dry_run: bool = False):
    mode_label = " [DRY-RUN MODE - NO WRITES]" if dry_run else ""
    print("=" * 70)
    print(f"  stamp_active_versions.py{mode_label}")
    print(f"  Run Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)

    if signals_col is None or positions_col is None:
        print("[FAIL] MongoDB collections not initialized. Check MONGO_URI in .env")
        return 1

    total_stamped = 0
    total_skipped = 0

    for col, col_name in [(signals_col, "signals"), (positions_col, "positions")]:
        print(f"\n-- Stamping collection: {col_name} --")
        stamped, skipped = stamp_collection(col, col_name, dry_run)
        total_stamped += stamped
        total_skipped += skipped

    print("\n" + "=" * 70)
    action = "Would stamp" if dry_run else "Stamped"
    print(f"  DONE | {action}: {total_stamped} docs | Already versioned: {total_skipped} docs")
    print("=" * 70)

    if dry_run:
        print("\n[INFO] Dry-run complete. Re-run without --dry-run to apply changes.")
    else:
        print("\n[SUCCESS] Version metadata stamped. Run nightly_db_audit.py to verify.")

    return 0


if __name__ == "__main__":
    is_dry_run = "--dry-run" in sys.argv
    sys.exit(run(dry_run=is_dry_run))
