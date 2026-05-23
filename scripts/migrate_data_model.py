#!/usr/bin/env python3
"""
scripts/migrate_data_model.py

One-time migration script to:
  1. Fix signals with stale lifecycle_state='POSITION_ACTIVE' but status!='ACTIVE' -> CLOSED
  2. Reconstruct outcome_type and exit_reason for old closed positions missing it.
     Explicitly tag these with outcome_source='BACKFILLED' for clear telemetry tracking.

Usage:
  python scripts/migrate_data_model.py            # Dry-run preview
  python scripts/migrate_data_model.py --execute  # Write modifications to MongoDB
"""

import sys
import os
import argparse
from datetime import datetime, timezone

# Bootstrap path so we can import from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import signals_col, positions_col

def migrate_stale_lifecycle(dry_run=True):
    """Fix signals with lifecycle_state=POSITION_ACTIVE but status!=ACTIVE."""
    if signals_col is None:
        print("[Error] signals_col is not connected")
        return
        
    query = {"lifecycle_state": "POSITION_ACTIVE", "status": {"$ne": "ACTIVE"}}
    count = signals_col.count_documents(query)
    
    print(f"\n[Lifecycle Check] Found {count} stale active-lifecycle signal records.")
    
    if count > 0:
        docs = list(signals_col.find(query, {"symbol": 1, "status": 1, "signal_date": 1}))
        for d in docs[:10]:
            print(f"  - {d['symbol']} on {d.get('signal_date')} (status: {d.get('status')})")
        if count > 10:
            print(f"  - ...and {count - 10} more")
            
        if not dry_run:
            result = signals_col.update_many(query, {"$set": {"lifecycle_state": "CLOSED", "updated_at": datetime.now(timezone.utc)}})
            print(f"  [OK] Executed: Updated {result.modified_count} signals to lifecycle_state='CLOSED'")
        else:
            print("  [Dry Run] Would update these records to lifecycle_state='CLOSED'")

def migrate_reconstruct_outcome_type(dry_run=True):
    """
    Reconstruct outcome_type for all closed positions missing it.
    Marks them with outcome_source='BACKFILLED'.
    """
    if positions_col is None:
        print("[Error] positions_col is not connected")
        return
        
    query = {"status": "CLOSED", "outcome_type": {"$exists": False}}
    count = positions_col.count_documents(query)
    
    print(f"\n[Outcome Type Check] Found {count} closed positions missing 'outcome_type'.")
    
    if count > 0:
        closed = list(positions_col.find(query))
        
        updated_count = 0
        for pos in closed:
            symbol = pos["symbol"]
            pnl = pos.get("pnl_pct", 0) or 0
            runup = pos.get("max_runup_pct", 0) or 0
            drawdown = pos.get("max_drawdown_pct", 0) or 0
            days = pos.get("days_held", 0) or 0
            
            # Classification logic
            if runup > 10.0 and days <= 5:
                outcome = "FAST_WINNER"
            elif runup > 10.0 and days > 5:
                outcome = "SLOW_WINNER"
            elif runup <= 3.0 and drawdown <= -3.0:
                outcome = "FAILED_BREAKOUT"
            elif runup < 1.0 and pnl < 0:
                outcome = "IMMEDIATE_FAILURE"
            else:
                outcome = "NO_FOLLOW_THROUGH"
                
            print(f"  - {symbol}: outcome={outcome} (PnL={pnl:+.1f}%, Peak Runup={runup:+.1f}%, Days={days})")
            
            if not dry_run:
                positions_col.update_one(
                    {"_id": pos["_id"]},
                    {"$set": {
                        "outcome_type": outcome,
                        "outcome_source": "BACKFILLED",
                        "updated_at": datetime.now(timezone.utc)
                    }}
                )
                updated_count += 1
                
        if not dry_run:
            print(f"  [OK] Executed: Reconstructed outcome_type for {updated_count} closed positions.")
        else:
            print(f"  [Dry Run] Would reconstruct outcome_type for {len(closed)} closed positions.")

def run(dry_run=True):
    if dry_run:
        print("=" * 60)
        print("DRY-RUN MODE — Previewing changes, making no DB writes")
        print("=" * 60)
    else:
        print("=" * 60)
        print("EXECUTE MODE — Writing changes to MongoDB")
        print("=" * 60)
        
    migrate_stale_lifecycle(dry_run)
    migrate_reconstruct_outcome_type(dry_run)
    
    print("\n" + "=" * 60)
    if dry_run:
        print("Dry-run preview completed. To write changes, re-run with --execute")
    else:
        print("Data migration completed successfully.")
    print("=" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate IPO Base Scanner MongoDB Data Model")
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Execute database writes. Default is dry-run.",
    )
    args = parser.parse_args()
    run(dry_run=not args.execute)
