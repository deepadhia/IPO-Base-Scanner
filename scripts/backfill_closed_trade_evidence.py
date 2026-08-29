#!/usr/bin/env python3
"""
scripts/backfill_closed_trade_evidence.py

Backfill and Synchronize All Closed Trades into MongoDB `strategy_evidence`.
Ensures every realized trade contains complete setup DNA, empirical outcome,
archetype classification, and actionable algorithmic takeaways.
"""

import os
import sys
from datetime import datetime, timezone
import pandas as pd
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

import importlib.util
spec = importlib.util.spec_from_file_location("streamlined_ipo_scanner", os.path.join(PROJECT_DIR, "streamlined_ipo_scanner.py"))
scanner_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner_module)
fetch_data = scanner_module.fetch_data

from core.strategy_evidence import build_trade_evidence_doc


def run_backfill():
    mongo_uri = os.getenv("MONGO_URI", "")
    if not mongo_uri:
        print("❌ MONGO_URI not configured.")
        return

    client = MongoClient(mongo_uri)
    db = client["ipo_scanner_v2"]

    positions_col = db["positions"]
    evidence_col = db["strategy_evidence"]

    # Query all closed clean-cohort positions (supports datetime or string entry_date)
    query = {
        "status": {"$in": ["CLOSED", "PAPER_CLOSED"]}
    }
    closed_positions = list(positions_col.find(query))

    print(f"\n{'═'*85}")
    print(f"🏛️ AlphaPulse — Backfilling Forensic Strategy Evidence for Closed Trades")
    print(f"{'═'*85}")
    print(f"Found {len(closed_positions)} clean-cohort closed positions in 'positions'.")

    backfilled_records = []

    for pos in closed_positions:
        sym = pos.get("symbol")
        entry_date = pos.get("entry_date")
        pnl = float(pos.get("pnl_pct", 0))
        days = float(pos.get("days_held", 0))
        reason = pos.get("exit_reason", "N/A")

        # Build granular evidence doc
        doc = build_trade_evidence_doc(pos, db=db, fetch_data_fn=fetch_data)

        evidence_col.update_one(
            {"evidence_id": doc["evidence_id"]},
            {"$set": doc},
            upsert=True
        )

        archetype = doc["forensics"]["archetype"]
        takeaway = doc["forensics"]["algo_takeaway"]

        backfilled_records.append({
            "Symbol": sym,
            "Entry Date": entry_date,
            "Days": int(days),
            "PnL %": f"{pnl:+.2f}%",
            "Exit Reason": reason[:22],
            "Archetype": archetype,
            "Takeaway": takeaway
        })

    # Display clean table
    if backfilled_records:
        df_summary = pd.DataFrame(backfilled_records)
        print("\n" + df_summary[['Symbol', 'Entry Date', 'Days', 'PnL %', 'Exit Reason', 'Archetype']].to_string(index=False))
        print(f"\n{'─'*85}")
        print("🔍 Detailed Forensic Learnings for Backfilled Trades:")
        for r in backfilled_records:
            print(f"\n• {r['Symbol']} ({r['PnL %']}, {r['Days']}d) — [{r['Archetype']}]:")
            print(f"  └─ {r['Takeaway']}")

    total_evidence_in_db = evidence_col.count_documents({})
    print(f"\n{'═'*85}")
    print(f"✅ Backfill complete. Total forensic documents in 'strategy_evidence': {total_evidence_in_db}")
    print(f"{'═'*85}\n")


if __name__ == "__main__":
    run_backfill()
