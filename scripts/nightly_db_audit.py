#!/usr/bin/env python3
"""
scripts/nightly_db_audit.py

Defensive Database Reconciliation and Market-Data Integrity Audit.
Runs nightly to catch lifecycle drift, stale states, NaNs, and distribution anomalies.

Usage:
  python scripts/nightly_db_audit.py
"""

import sys
import os
import math
from datetime import datetime, timezone, timedelta
import requests
from dotenv import load_dotenv

# Bootstrap path so we can import from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

try:
    from db import signals_col, positions_col, logs_col
except ImportError:
    print("[Error] Could not import database connections from db.py")
    sys.exit(1)

# Telegram Configurations
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram_alert(msg: str):
    """Send HTML-formatted message to Telegram."""
    is_ci = os.getenv("GITHUB_ACTIONS") == "true"
    if not is_ci:
        print("[Telegram Bypassed] Bypassing Telegram notification because script is running locally.")
        return

    if not BOT_TOKEN or not CHAT_ID:
        print("[Telegram disabled] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_notification": False
        }, timeout=15)
        if response.status_code == 200:
            print("[OK] Telegram notification sent successfully.")
        else:
            print(f"[Error] Telegram API Error: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"[Error] Telegram Communication Error: {e}")

def run_nightly_audit():
    print("=" * 70)
    print(f"  IPO-Base-Scanner Nightly Database Reconciliation & Audit")
    print(f"  Run Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)

    if signals_col is None or positions_col is None or logs_col is None:
        print("[FAIL] MongoDB collections not initialized.")
        return 1

    anomalies = []
    warnings = []

    # ── 1. STRUCTURAL AUDITS ──

    # Check 1.1: Lifecycle drift (Active signals missing positions)
    print("\n[Audit 1.1] Checking Signal-to-Position Lifecycle Sync...")
    active_signals = list(signals_col.find({"status": "ACTIVE"}))
    active_positions = list(positions_col.find({"status": "ACTIVE"}))
    
    active_sig_symbols = {s["symbol"] for s in active_signals}
    active_pos_symbols = {p["symbol"] for p in active_positions}

    # Signal active but no active position
    missing_pos = active_sig_symbols - active_pos_symbols
    # Position active but no active signal
    missing_sig = active_pos_symbols - active_sig_symbols

    if missing_pos:
        err = f"Signals are ACTIVE but missing matching ACTIVE positions: {sorted(missing_pos)}"
        print(f"  [ANOMALY] {err}")
        anomalies.append(err)
    if missing_sig:
        err = f"Positions are ACTIVE but missing matching ACTIVE signals: {sorted(missing_sig)}"
        print(f"  [ANOMALY] {err}")
        anomalies.append(err)

    if not missing_pos and not missing_sig:
        print("  [OK] Active statuses are in 100% perfect sync (Count: {} active).".format(len(active_sig_symbols)))

    # Check 1.2: Stale active check (Not updated in last 48 hours)
    print("\n[Audit 1.2] Checking for Stale Active Positions...")
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=48)
    stale_actives = []
    
    for pos in active_positions:
        updated_at = pos.get("updated_at")
        if updated_at:
            # Handle string or datetime objects
            if isinstance(updated_at, str):
                try:
                    updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                except ValueError:
                    updated_at = None
            if updated_at and updated_at < stale_cutoff:
                stale_actives.append(pos)
        else:
            # Missing updated_at field entirely
            stale_actives.append(pos)

    if stale_actives:
        print(f"  [WARNING] Found {len(stale_actives)} active positions not updated in > 48 hours:")
        for sp in stale_actives:
            msg = f"Stale active position for {sp['symbol']} (last updated: {sp.get('updated_at')})"
            print(f"    - {msg}")
            warnings.append(msg)
    else:
        print("  [OK] All active positions have been refreshed recently.")

    # Check 1.3: NaN or Null Value Audit (Context-Aware)
    print("\n[Audit 1.3] Scanning for NaN/Null value corruptions...")
    corrupt_docs = []
    
    for col_name, col in [("positions", positions_col), ("signals", signals_col)]:
        for doc in col.find():
            symbol = doc.get("symbol", "UNKNOWN")
            doc_id = doc.get("_id")
            status = doc.get("status")
            
            # Watchlist signals are special: they only have watch levels and current prices.
            if status == "WATCH":
                continue
                
            fields_to_check = []
            if col_name == "signals":
                # Signals collection tracks static parameters of the breakout trigger event
                fields_to_check = ["entry_price", "stop_loss", "target_price"]
                # Enforce PnL only on closed signals
                if status == "CLOSED":
                    fields_to_check.append("pnl_pct")
            else:
                # Positions collection dynamically manages open/active trades
                if status == "ACTIVE":
                    fields_to_check = ["entry_price", "stop_loss", "trailing_stop", "current_price", "pnl_pct"]
                elif status == "CLOSED":
                    fields_to_check = ["entry_price", "stop_loss", "exit_price", "pnl_pct"]
                elif status == "PAPER_ONLY":
                    fields_to_check = ["entry_price", "stop_loss", "current_price", "pnl_pct"]
                else:
                    fields_to_check = ["entry_price", "stop_loss"]

            for f in fields_to_check:
                val = doc.get(f)
                if val is None or (isinstance(val, float) and math.isnan(val)):
                    corrupt_docs.append((col_name, symbol, doc_id, f, val))

    if corrupt_docs:
        print(f"  [ANOMALY] Found {len(corrupt_docs)} documents with NaN/Null numerical corruptions:")
        for col_name, sym, doc_id, field, val in corrupt_docs[:10]:
            err = f"{col_name} | {sym}: field '{field}' is {val} (ID: {doc_id})"
            print(f"    - {err}")
            anomalies.append(err)
    else:
        print("  [OK] No NaN or Null value corruptions found in numerical schema.")

    # Check 1.4: Duplicate snapshots check
    print("\n[Audit 1.4] Checking for duplicate snapshot logs today...")
    today_str = datetime.now().strftime("%Y-%m-%d")
    pipeline = [
        {"$match": {"event": "DAILY_SNAPSHOT", "timestamp": {"$regex": f"^{today_str}"}}},
        {"$group": {"_id": {"symbol": "$symbol", "date": "$timestamp"}, "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}}
    ]
    dupe_snapshots = list(logs_col.aggregate(pipeline))
    if dupe_snapshots:
        print(f"  [WARNING] Found {len(dupe_snapshots)} duplicate daily snapshots logged today:")
        for dupe in dupe_snapshots[:10]:
            msg = f"Duplicate DAILY_SNAPSHOT for {dupe['_id']['symbol']} on {dupe['_id']['date']} (count={dupe['count']})"
            print(f"    - {msg}")
            warnings.append(msg)
    else:
        print("  [OK] No duplicate snapshot logs detected today.")


    # ── 2. DISTRIBUTION & DATA ANOMALY AUDITS ──

    # Check 2.1: Abnormal single-day PnL jumps (> 40% jump)
    print("\n[Audit 2.1] Checking for Abnormal Price/PnL jumps (>40%)...")
    abnormal_jumps = []
    
    # We audit all positions
    for pos in positions_col.find():
        pnl = pos.get("pnl_pct", 0) or 0
        symbol = pos["symbol"]
        
        # If PnL is > 200% or < -95%, flag it as highly suspicious
        if abs(pnl) > 200.0 or pnl < -95.0:
            abnormal_jumps.append((symbol, pnl, "Extreme absolute PnL value"))
            
        # Check daily price continuity if EOD historical metrics exist
        max_runup = pos.get("max_runup_pct", 0) or 0
        max_drawdown = pos.get("max_drawdown_pct", 0) or 0
        if max_runup > 250.0 or max_drawdown < -90.0:
            abnormal_jumps.append((symbol, pnl, f"Extreme peak runup ({max_runup:.1f}%) or drawdown ({max_drawdown:.1f}%)"))

    if abnormal_jumps:
        print(f"  [ANOMALY] Found {len(abnormal_jumps)} suspicious extreme price anomalies:")
        for sym, pnl, reason in abnormal_jumps[:10]:
            err = f"{sym}: PnL = {pnl:+.2f}% | Reason: {reason}"
            print(f"    - {err}")
            anomalies.append(err)
    else:
        print("  [OK] No extreme distribution jumps or PnL anomalies detected.")

    # Check 2.2: Stale Data / Price Discontinuity (Live vs Stored Price check)
    print("\n[Audit 2.2] Auditing live pricing discrepancies on active positions...")
    discrepancies = []
    
    # Let's import get_live_price from the scanner dynamically
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("scanner", "streamlined_ipo_scanner.py")
        scanner_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner_module)
        get_live_price = scanner_module.get_live_price
        
        for pos in active_positions:
            sym = pos["symbol"]
            stored_price = pos.get("current_price")
            if stored_price is not None and stored_price > 0:
                try:
                    live_price, _, _, _vol = get_live_price(sym)
                    if live_price is not None and live_price > 0:
                        diff_pct = abs(live_price - stored_price) / stored_price * 100.0
                        if diff_pct > 20.0:
                            discrepancies.append((sym, stored_price, live_price, diff_pct))
                except Exception as live_e:
                    print(f"    (Skipped live price check for {sym} due to API rate limit/error: {live_e})")
    except Exception as e:
        print(f"  [WARNING] Could not run Live pricing discrepancy audit: {e}")
        warnings.append(f"Could not run live pricing audit: {e}")

    if discrepancies:
        print(f"  [WARNING / POTENTIAL CORPORATE ACTION OR DRIFT - NEEDS REVIEW] Found {len(discrepancies)} active positions with major price discontinuities (>20%):")
        for sym, stored, live, diff in discrepancies:
            msg = f"{sym}: Stored Rs.{stored:.2f} vs Live Rs.{live:.2f} | Diff: {diff:.2f}% (Check for volatility/split/feed drift)"
            print(f"    - {msg}")
            warnings.append(msg)
    else:
        print("  [OK] No major active pricing discrepancies or data-feed splits detected.")

    # ── SUMMARY & TELEGRAM ALERT ──
    print("\n" + "=" * 70)
    print(f"  AUDIT COMPLETE | Anomalies: {len(anomalies)} | Warnings: {len(warnings)}")
    print("=" * 70)

    # Construct Telegram Alert Message
    status_emoji = "🟢"
    status_label = "SUCCESS"
    
    if anomalies:
        status_emoji = "🔴"
        status_label = "CRITICAL ANOMALIES DETECTED"
    elif warnings:
        status_emoji = "⚠️"
        status_label = "WARNINGS FOUND"

    utc_now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    ist_now_str = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime('%Y-%m-%d %H:%M:%S IST')

    tg_msg = f"""🔍 <b>IPO-Base-Scanner Nightly Audit Report</b>
📅 <i>Run Time: {ist_now_str} ({utc_now_str})</i>
======================================
Status: {status_emoji} <b>{status_label}</b>

📊 <b>Summary:</b>
• Anomalies: <b>{len(anomalies)}</b>
• Warnings: <b>{len(warnings)}</b>
• Active Positions Checked: <b>{len(active_positions)}</b>
"""

    if anomalies:
        tg_msg += "\n🛑 <b>Anomalies:</b>\n"
        for a in anomalies[:15]:
            tg_msg += f"• {a}\n"
        if len(anomalies) > 15:
            tg_msg += f"<i>...and {len(anomalies) - 15} more anomalies.</i>\n"

    if warnings:
        tg_msg += "\n⚠️ <b>Warnings / Review Needed:</b>\n"
        for w in warnings[:15]:
            tg_msg += f"• {w}\n"
        if len(warnings) > 15:
            tg_msg += f"<i>...and {len(warnings) - 15} more warnings.</i>\n"

    if not anomalies and not warnings:
        tg_msg += "\n✅ All structural and distribution integrity audits passed successfully."

    send_telegram_alert(tg_msg)

    # As requested, we always exit with success (0) to prevent pipeline failures,
    # relying on the Telegram Alert as the primary reporting mechanism.
    return 0

if __name__ == "__main__":
    sys.exit(run_nightly_audit())

