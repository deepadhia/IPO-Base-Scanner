#!/usr/bin/env python3
"""
scripts/restamp_listing_breakout_grade.py

One-off migration: fix open listing/re-entry positions (and matching signals)
that stored winner_label in `grade` instead of "LISTING_BREAKOUT".

Exit logic in stop_loss_update_scan only applies IPO dead-money / volume-exhaustion
/ trail thresholds when grade == "LISTING_BREAKOUT".

Identification (any match, open book only):
  - grade in {STANDARD, POSSIBLE_WINNER, WATCHLIST_ONLY}
  - strategy_version == "2.5.0-listing-day" and grade != LISTING_BREAKOUT
  - is_reentry is True and grade != LISTING_BREAKOUT
  - signal_id startswith BREAKOUT_ and grade != LISTING_BREAKOUT

Never touches: INTRADAY, consol letter grades (A+/B/C/D), CLOSED history.

Usage:
  python scripts/restamp_listing_breakout_grade.py --dry-run
  python scripts/restamp_listing_breakout_grade.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import signals_col, positions_col

TARGET_GRADE = "LISTING_BREAKOUT"
OPEN_STATUSES = ("ACTIVE", "PAPER_ONLY")
# Winner labels that were incorrectly written into position.grade
MISPLACED_WINNER_GRADES = {"STANDARD", "POSSIBLE_WINNER", "WATCHLIST_ONLY"}
LISTING_STRATEGY = "2.5.0-listing-day"


def is_listing_misgraded(doc: dict) -> bool:
    grade = str(doc.get("grade") or "")
    if grade == TARGET_GRADE:
        return False
    if grade == "INTRADAY":
        return False
    # Letter grades belong to consolidation — leave alone
    if grade in {"A+", "A", "B", "C", "D"}:
        return False

    if grade in MISPLACED_WINNER_GRADES:
        return True
    if doc.get("strategy_version") == LISTING_STRATEGY:
        return True
    if doc.get("is_reentry") is True:
        return True
    signal_id = str(doc.get("signal_id") or "")
    if signal_id.startswith("BREAKOUT_"):
        return True
    if doc.get("scanner") == "listing_day":
        return True
    return False


def preview_doc(doc: dict) -> str:
    return (
        f"  {doc.get('symbol')} | status={doc.get('status')} | "
        f"grade={doc.get('grade')!r} -> {TARGET_GRADE!r} | "
        f"strategy={doc.get('strategy_version')!r} | "
        f"signal_id={doc.get('signal_id')!r} | "
        f"is_reentry={doc.get('is_reentry')}"
    )


def migrate_collection(col, col_name: str, apply: bool) -> tuple[int, int]:
    if col is None:
        print(f"[skip] {col_name}: collection unavailable")
        return 0, 0

    open_docs = list(col.find({"status": {"$in": list(OPEN_STATUSES)}}))
    candidates = [d for d in open_docs if is_listing_misgraded(d)]
    already_ok = sum(
        1 for d in open_docs
        if str(d.get("grade") or "") == TARGET_GRADE
        and (
            d.get("strategy_version") == LISTING_STRATEGY
            or d.get("is_reentry") is True
            or str(d.get("signal_id") or "").startswith("BREAKOUT_")
            or d.get("scanner") == "listing_day"
        )
    )

    print(f"\n=== {col_name} ===")
    print(f"Open rows scanned: {len(open_docs)}")
    print(f"Already LISTING_BREAKOUT (listing-like): {already_ok}")
    print(f"Candidates to restamp: {len(candidates)}")

    for doc in candidates:
        print(preview_doc(doc))
        if not apply:
            continue
        update = {
            "grade": TARGET_GRADE,
            "updated_at": datetime.now(timezone.utc),
            "grade_restamped_at": datetime.now(timezone.utc),
            "grade_restamp_reason": "winner_label_was_written_as_grade",
        }
        # Preserve misplaced value into winner_label if missing
        old_grade = doc.get("grade")
        if old_grade in MISPLACED_WINNER_GRADES and not doc.get("winner_label"):
            update["winner_label"] = old_grade

        col.update_one({"_id": doc["_id"]}, {"$set": update})

    return len(candidates), already_ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview only")
    group.add_argument("--apply", action="store_true", help="Write updates")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"restamp_listing_breakout_grade [{mode}] @ {datetime.now(timezone.utc).isoformat()}")

    pos_n, pos_ok = migrate_collection(positions_col, "positions", args.apply)
    sig_n, sig_ok = migrate_collection(signals_col, "signals", args.apply)

    print("\n--- summary ---")
    print(f"positions: {pos_n} {'updated' if args.apply else 'would update'} ({pos_ok} already ok)")
    print(f"signals:   {sig_n} {'updated' if args.apply else 'would update'} ({sig_ok} already ok)")
    if args.dry_run:
        print("\nNo writes performed. Re-run with --apply to persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
