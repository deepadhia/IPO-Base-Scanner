#!/usr/bin/env python3
"""
scripts/purge_penny_stocks.py

Removes all historical signals and positions from the database where the entry price is below ₹25.00.
This ensures historical database statistics and performance expectation analysis are completely clean
and unpolluted by highly erratic penny stocks.

Usage:
  python scripts/purge_penny_stocks.py            # Dry-run preview
  python scripts/purge_penny_stocks.py --execute  # Execute deletions
"""

import sys
import os
import argparse
from datetime import datetime

# Bootstrap path so we can import from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import signals_col, positions_col, logs_col

def purge_penny_stocks(dry_run=True):
    if signals_col is None or positions_col is None or logs_col is None:
        print("[Error] MongoDB collections are not connected")
        return

    # 1. Purge from positions collection
    all_positions = list(positions_col.find())
    positions_to_purge = []
    for pos in all_positions:
        entry = pos.get("entry_price")
        if entry is not None:
            try:
                if float(entry) < 25.0:
                    positions_to_purge.append(pos)
            except (ValueError, TypeError):
                pass

    print("=" * 60)
    print(f"[Positions Purge] Found {len(positions_to_purge)} records with entry price < Rs.25.00:")
    for p in positions_to_purge:
        print(f"  - {p['symbol']}: Entry price Rs.{p['entry_price']} (status: {p.get('status')})")

    if not dry_run and positions_to_purge:
        deleted_symbols = [p['symbol'] for p in positions_to_purge]
        result = positions_col.delete_many({"symbol": {"$in": deleted_symbols}})
        print(f"  [OK] Executed: Deleted {result.deleted_count} positions from database.")
    elif positions_to_purge:
        print(f"  [Dry Run] Would delete {len(positions_to_purge)} positions from database.")

    # 2. Purge from signals collection
    all_signals = list(signals_col.find())
    signals_to_purge = []
    for sig in all_signals:
        entry = sig.get("entry_price")
        if entry is not None:
            try:
                if float(entry) < 25.0:
                    signals_to_purge.append(sig)
            except (ValueError, TypeError):
                pass

    print("\n" + "=" * 60)
    print(f"[Signals Purge] Found {len(signals_to_purge)} records with entry price < Rs.25.00:")
    for s in signals_to_purge:
        print(f"  - {s['symbol']}: Entry price Rs.{s['entry_price']} (signal_date: {s.get('signal_date')})")

    if not dry_run and signals_to_purge:
        deleted_ids = [s['_id'] for s in signals_to_purge]
        result = signals_col.delete_many({"_id": {"$in": deleted_ids}})
        print(f"  [OK] Executed: Deleted {result.deleted_count} signals from database.")
    elif signals_to_purge:
        print(f"  [Dry Run] Would delete {len(signals_to_purge)} signals from database.")

    # 3. Purge from logs collection
    penny_symbols = ['MADHAVIPL', 'RRIL', 'EMPOWER', 'MERCANTILE', 'MMWL', 'STLNETWORK', 'AEPL', 'AHCL']
    query_logs = {"symbol": {"$in": penny_symbols}}
    logs_count = logs_col.count_documents(query_logs)

    print("\n" + "=" * 60)
    print(f"[Logs Purge] Found {logs_count} log entries associated with penny stock symbols:")
    print(f"  Penny Stock Symbols: {penny_symbols}")

    if not dry_run and logs_count > 0:
        result = logs_col.delete_many(query_logs)
        print(f"  [OK] Executed: Deleted {result.deleted_count} log documents from database.")
    elif logs_count > 0:
        print(f"  [Dry Run] Would delete {logs_count} log documents from database.")

    print("\n" + "=" * 60)
    if dry_run:
        print("Dry-run preview completed. To delete these records permanently, run with --execute")
    else:
        print("Database cleanup completed successfully.")
    print("=" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Purge Penny Stocks (< Rs.25) from MongoDB")
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Execute deletions. Default is dry-run.",
    )
    args = parser.parse_args()
    purge_penny_stocks(dry_run=not args.execute)
