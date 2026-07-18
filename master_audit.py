#!/usr/bin/env python3
"""
master_audit.py -- IPO-Base-Scanner System Integrity Audit
===========================================================
Run daily or weekly to verify system health across three layers:

  Section 1: Database Integrity
  Section 2: Telemetry / Log Quality
  Section 3: Strategy Consistency

Usage:
  python master_audit.py             # Full audit, human-readable output
  python master_audit.py --json      # Full audit, JSON output (for CI)
  python master_audit.py --section 1|2|3  # Run a single section

Exit codes:
  0 = PASS  (no issues found)
  1 = WARN  (review recommended)
  2 = FAIL  (action required)
"""

import os
import sys
import json
import argparse
import re
from datetime import datetime, timezone, timedelta
from collections import Counter
import pandas as pd

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Keep this in sync with streamlined_ipo_scanner.py SCANNER_VERSION.
# Section 3 will flag any drift automatically.
EXPECTED_VERSION = "3.4.0"

IST = timezone(timedelta(hours=5, minutes=30))

NSE_HOLIDAYS_2025_2026 = {
    # 2025
    "2025-01-26", "2025-02-26", "2025-03-14", "2025-04-10",
    "2025-04-14", "2025-04-18", "2025-05-01", "2025-08-15",
    "2025-08-27", "2025-10-02", "2025-10-24", "2025-10-28",
    "2025-11-05", "2025-11-15", "2025-12-25",
    # 2026
    "2026-01-26", "2026-02-19", "2026-03-03", "2026-03-19",
    "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
    "2026-05-01", "2026-05-28", "2026-06-26", "2026-08-15",
    "2026-08-26", "2026-09-14", "2026-10-02", "2026-10-20",
    "2026-11-10", "2026-11-24", "2026-12-25",
}

MAX_REALISTIC_PNL_PCT    = 150.0
MIN_REALISTIC_PNL_PCT    = -60.0
MAX_RUNUP_REALISTIC      = 200.0
MAX_ENTRY_ABOVE_BKT_PCT  = 8.0


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------
def get_db():
    uri = os.getenv("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI not set in environment.")
    return MongoClient(uri, tz_aware=True)["ipo_scanner_v2"]


def send_telegram_notification(msg):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return
    import requests
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
class AuditResult:
    def __init__(self):
        self.errors   = []
        self.warnings = []
        self.info     = []

    def error(self, msg): self.errors.append(str(msg))
    def warn(self,  msg): self.warnings.append(str(msg))
    def ok(self,    msg): self.info.append(str(msg))

    @property
    def exit_code(self):
        if self.errors:   return 2
        if self.warnings: return 1
        return 0

    def status_label(self):
        return "PASS" if self.exit_code == 0 else ("WARN" if self.exit_code == 1 else "FAIL")

    def print_report(self, section_name):
        print("\n" + "=" * 70)
        print("  " + section_name)
        print("-" * 70)
        for msg in self.info:
            print("  [OK]   " + msg)
        for msg in self.warnings:
            print("  [WARN] " + msg)
        for msg in self.errors:
            print("  [ERR]  " + msg)
        print("\n  [%s]  %d errors, %d warnings\n" % (
            self.status_label(), len(self.errors), len(self.warnings)))

    def to_dict(self, section_name):
        return {
            "section":  section_name,
            "status":   self.status_label(),
            "errors":   self.errors,
            "warnings": self.warnings,
            "info":     self.info,
        }


# ===========================================================================
# SECTION 1: DATABASE INTEGRITY
# ===========================================================================
def audit_database_integrity(db):
    r = AuditResult()

    signals   = list(db.signals.find({},   {"_id": 0}))
    positions = list(db.positions.find({}, {"_id": 0}))

    # NOTE: signals can have status 'ACTIVE', 'CLOSED', or 'WATCH'.
    # Only 'ACTIVE' signals are expected to have matching positions.
    active_sig_syms = {s["symbol"] for s in signals if s.get("status") == "ACTIVE"}
    active_position_syms = {p["symbol"] for p in positions if p.get("status") == "ACTIVE"}
    all_position_syms   = {p["symbol"]: p.get("status") for p in positions}

    # 1a. Orphan ACTIVE signals -- ACTIVE signal but NO position record at all
    orphans_no_pos = active_sig_syms - set(all_position_syms.keys())
    if orphans_no_pos:
        r.error("ACTIVE signals with NO position record (crash mid-write?): %s" % sorted(orphans_no_pos))
    else:
        r.ok("All ACTIVE signals have at least one position record.")

    # 1b. ACTIVE signals where the position is CLOSED (signal status not synced after exit)
    sig_active_pos_closed = {
        sym for sym in active_sig_syms
        if all_position_syms.get(sym) == "CLOSED"
    }
    if sig_active_pos_closed:
        r.warn("Signal=ACTIVE but Position=CLOSED (exit not reflected in signals): %s\n"
               "       Run a sync script or close_signal_in_db() for these." % sorted(sig_active_pos_closed))
    else:
        r.ok("All ACTIVE signals have a matching ACTIVE position.")

    # 1b2. Position=ACTIVE but signal is CLOSED (backfill created position, signal later closed)
    pos_active_sig_closed = {
        sym for sym in active_position_syms
        if sym not in active_sig_syms and any(
            s.get("status") == "CLOSED" for s in signals if s.get("symbol") == sym
        )
    }
    if pos_active_sig_closed:
        r.warn("Position=ACTIVE but signal is CLOSED (position status not synced): %s" % sorted(pos_active_sig_closed))
    else:
        r.ok("No active positions with a closed signal (sync OK).")

    # 1c. Inverted stop-loss (exclude WATCH -- they have entry=stop=0 by design)
    bad = [s["symbol"] for s in signals
           if s.get("stop_loss", 0) >= s.get("entry_price", 1)
           and s.get("status") != "WATCH"]
    if bad:
        r.error("Signals with stop_loss >= entry_price (inverted): %s" % bad)
    else:
        r.ok("No inverted stop-loss values detected.")

    # 1d. Inverted target (exclude WATCH -- they have entry=target=0 by design)
    bad = [s["symbol"] for s in signals
           if s.get("target_price", 0) <= s.get("entry_price", 1)
           and s.get("status") != "WATCH"]
    if bad:
        r.error("Signals with target_price <= entry_price (inverted): %s" % bad)
    else:
        r.ok("No inverted target prices detected.")

    # 1e. Zero / negative entry price (exclude WATCH -- they intentionally have entry=0)
    bad = [s["symbol"] for s in signals
           if s.get("entry_price", 0) <= 0 and s.get("status") != "WATCH"]
    if bad:
        r.error("Signals with entry_price <= 0: %s" % bad)
    else:
        r.ok("All signals have a positive entry price.")

    # 1f. Duplicate signal_ids (WATCH signals accumulate one per day -- signal_ids should still be unique)
    ids     = [s.get("signal_id") for s in signals if s.get("signal_id")]
    dup_ids = [sid for sid, cnt in Counter(ids).items() if cnt > 1]
    if dup_ids:
        r.error("Duplicate signal_ids detected: %s" % dup_ids)
    else:
        watch_cnt  = sum(1 for s in signals if s.get("status") == "WATCH")
        trade_cnt  = sum(1 for s in signals if s.get("status") in ("ACTIVE", "CLOSED"))
        r.ok("All %d signal_ids are unique (%d trade signals, %d watchlist entries)." % (
            len(ids), trade_cnt, watch_cnt))

    # 1g. Unrealistic PnL on closed positions
    for p in positions:
        pnl = p.get("pnl_pct", 0)
        sym = p.get("symbol", "?")
        if p.get("status") == "CLOSED" and (
                pnl > MAX_REALISTIC_PNL_PCT or pnl < MIN_REALISTIC_PNL_PCT):
            r.warn("Position %s: pnl_pct=%.1f%% is outside realistic range "
                   "[%.0f%%, %.0f%%]. Check manually." % (
                       sym, pnl, MIN_REALISTIC_PNL_PCT, MAX_REALISTIC_PNL_PCT))

    # 1h. Entries on NSE holidays
    for p in positions:
        sym = p.get("symbol", "?")
        ed  = p.get("entry_date", "")
        if isinstance(ed, datetime):
            ed = ed.strftime("%Y-%m-%d")
        elif isinstance(ed, str):
            ed = ed[:10]
        if ed in NSE_HOLIDAYS_2025_2026:
            r.warn("Position %s: entry_date=%s is an NSE holiday. Verify." % (sym, ed))

    # 1i. IPO symbols in 'ipos' collection missing from 'listing_data' collection
    try:
        today_date = datetime.now().date()
        all_ipos = list(db.ipos.find({}, {"symbol": 1, "listing_date": 1, "_id": 0}))
        
        # Only check IPOs whose listing date has passed (listing_date < today).
        # Same-day or future listings cannot be backfilled until after market close EOD.
        ipo_symbols = set()
        for d in all_ipos:
            sym = d.get("symbol")
            if not sym: continue
            ld = d.get("listing_date")
            if ld:
                try:
                    ld_date = pd.to_datetime(ld).date()
                    if ld_date >= today_date:
                        continue  # Skip same-day or future listings
                except Exception:
                    pass
            ipo_symbols.add(sym)
        
        all_listings = list(db.listing_data.find({}, {"symbol": 1, "_id": 0}))
        listing_symbols = {d["symbol"] for d in all_listings if d.get("symbol")}
        
        missing_listings = ipo_symbols - listing_symbols
        # Filter out rights entitlements and SME segments that we ignore in the scanner
        missing_listings = {sym for sym in missing_listings if not ('-RE' in sym or sym.endswith('-SM') or 'RE1' in sym)}
        
        if missing_listings:
            r.warn("IPO symbols missing from 'listing_data' collection: %s. Attempting auto-backfill..." % sorted(missing_listings))
            try:
                from listing_day_breakout_scanner import update_listing_data_for_new_ipos
                update_listing_data_for_new_ipos()
                
                # Re-verify after backfill
                all_listings_post = list(db.listing_data.find({}, {"symbol": 1, "_id": 0}))
                listing_symbols_post = {d["symbol"] for d in all_listings_post if d.get("symbol")}
                missing_listings_post = ipo_symbols - listing_symbols_post
                missing_listings_post = {sym for sym in missing_listings_post if not ('-RE' in sym or sym.endswith('-SM') or 'RE1' in sym)}
                
                if missing_listings_post:
                    r.warn("IPO symbols still missing from 'listing_data' collection after backfill (rate-limited or Upstox fail): %s" % sorted(missing_listings_post))
                    err_msg = (
                        f"⚠️ <b>IPO Listing Data Sync Warning</b>\n\n"
                        f"The following IPO symbols are in <code>ipos</code> but missing from <code>listing_data</code>. "
                        f"They could not be auto-backfilled in GHA (likely due to yfinance cloud IP blocking):\n"
                        f"👉 <code>{sorted(list(missing_listings_post))}</code>\n\n"
                        f"💡 Please run <code>python manual_update_listing_data.py</code> locally to synchronize."
                    )
                    send_telegram_notification(err_msg)
                else:
                    r.ok("All active IPO symbols successfully backfilled and verified in 'listing_data' collection.")
            except Exception as backfill_err:
                r.warn("Failed during auto-backfill execution: %s" % backfill_err)
                err_msg = (
                    f"⚠️ <b>IPO Listing Data Sync Error</b>\n\n"
                    f"An error occurred during auto-backfill of missing listing records:\n"
                    f"<code>{backfill_err}</code>"
                )
                send_telegram_notification(err_msg)
        else:
            r.ok("All active IPO symbols have corresponding records in the 'listing_data' collection.")
    except Exception as e:
        r.error("Failed to audit missing listing data symbols: %s" % e)

    r.ok("Scanned %d signals and %d positions." % (len(signals), len(positions)))
    return r


# ===========================================================================
# SECTION 2: TELEMETRY / LOG QUALITY
# ===========================================================================
def audit_log_quality(db):
    r = AuditResult()
    today       = datetime.now(timezone.utc).date()
    today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    two_days_ago = today_start - timedelta(days=3)

    # 2a. SCAN_COMPLETED events today
    for scanner in ("consolidation", "listing_day", "positions", "watchlist"):
        count = db.logs.count_documents({
            "scanner": scanner,
            "action":  "SCAN_COMPLETED",
            "timestamp": {"$gte": today_start},
        })
        if scanner in ("consolidation", "listing_day") and count == 0:
            r.warn("No SCAN_COMPLETED log today for scanner='%s'. Did it run?" % scanner)
        elif count > 0:
            r.ok("'%s' SCAN_COMPLETED: %d event(s) today." % (scanner, count))

    # 2b. Rejection ratio today
    for scanner in ("consolidation", "listing_day"):
        total    = db.logs.count_documents({"scanner": scanner, "timestamp": {"$gte": today_start}})
        rejected = db.logs.count_documents({"scanner": scanner, "log_type": "REJECTED",
                                            "timestamp": {"$gte": today_start}})
        if total == 0:
            r.warn("'%s': No log entries at all today -- scanner may not have run." % scanner)
            continue
        ratio = rejected / total * 100
        if ratio > 97:
            r.warn("'%s' rejection ratio: %.1f%% (>97%%). "
                   "Filters may be too aggressive or data feed issue." % (scanner, ratio))
        elif ratio < 30:
            r.warn("'%s' rejection ratio: %.1f%% (<30%%). "
                   "Unusually few rejections -- verify scanner ran correctly." % (scanner, ratio))
        else:
            r.ok("'%s' rejection ratio: %.1f%% (%d/%d). Normal range." % (
                scanner, ratio, rejected, total))

    # 2c. Missing required fields in logs
    bad_docs = list(db.logs.find({
        "$or": [
            {"symbol":    {"$exists": False}},
            {"action":    {"$exists": False}},
            {"timestamp": {"$exists": False}},
        ]
    }, {"_id": 0, "log_id": 1, "scanner": 1}).limit(10))
    if bad_docs:
        r.error("Log documents missing required fields: %s" %
                [d.get("log_id", "?") for d in bad_docs])
    else:
        r.ok("All sampled log documents contain required fields.")

    # 2d. Version drift in today's logs
    wrong_ver = db.logs.count_documents({
        "timestamp": {"$gte": today_start},
        "version":   {"$ne": EXPECTED_VERSION},
    })
    if wrong_ver > 0:
        r.warn("%d log(s) today written with version != '%s'. "
               "Possible stale worker or partial deployment." % (wrong_ver, EXPECTED_VERSION))
    else:
        r.ok("All today's logs carry version='%s'." % EXPECTED_VERSION)

    # 2e. DAILY_SNAPSHOT coverage for active positions
    active_syms = [p["symbol"] for p in
                   db.positions.find({"status": "ACTIVE"}, {"symbol": 1, "_id": 0})]
    if active_syms:
        snapshotted = {
            doc["symbol"]
            for doc in db.logs.find({
                "action":    "DAILY_SNAPSHOT",
                "timestamp": {"$gte": today_start},
                "symbol":    {"$in": active_syms},
            }, {"symbol": 1, "_id": 0})
        }
        missing = set(active_syms) - snapshotted
        if missing:
            r.warn("Active positions missing DAILY_SNAPSHOT today: %s. "
                   "MTM tracker may not have run." % sorted(missing))
        else:
            r.ok("All %d active positions have a DAILY_SNAPSHOT today." % len(active_syms))

        for sym in active_syms:
            last = db.logs.find_one(
                {"action": "DAILY_SNAPSHOT", "symbol": sym},
                sort=[("timestamp", -1)]
            )
            if last:
                ts = last["timestamp"]
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < two_days_ago:
                    r.warn("Position %s: last DAILY_SNAPSHOT was %s (>2 business days ago)." % (
                        sym, last["timestamp"].strftime("%Y-%m-%d")))
    else:
        r.ok("No active positions -- DAILY_SNAPSHOT check skipped.")

    return r


# ===========================================================================
# SECTION 3: STRATEGY CONSISTENCY
# ===========================================================================
def audit_strategy_consistency(db):
    r = AuditResult()
    base_dir = os.path.dirname(os.path.abspath(__file__))

    def extract_ver(filepath, pattern):
        try:
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
            m = re.search(pattern, content)
            return m.group(1) if m else None
        except FileNotFoundError:
            return None

    # 3a. Version consistency across files
    versions = {
        "streamlined_ipo_scanner.py": extract_ver(
            os.path.join(base_dir, "streamlined_ipo_scanner.py"),
            r'SCANNER_VERSION\s*=\s*["\']([^"\']+)["\']'),
        "db.py": extract_ver(
            os.path.join(base_dir, "db.py"),
            r'SCANNER_VERSION\s*=\s*["\']([^"\']+)["\']'),
        "README badge": extract_ver(
            os.path.join(base_dir, "README.md"),
            r'badge/version-([0-9.]+)-orange'),
        "README footer": extract_ver(
            os.path.join(base_dir, "README.md"),
            r'systematic IPO momentum trading \| v([0-9]+\.[0-9]+\.[0-9]+) \|'),
    }

    drift = [(name, ver) for name, ver in versions.items()
             if ver is not None and ver != EXPECTED_VERSION]
    if drift:
        for name, ver in drift:
            r.error("Version drift: %s has '%s', expected '%s'." % (
                name, ver, EXPECTED_VERSION))
    else:
        r.ok("All version strings match '%s' across scanner, db.py, and README." % EXPECTED_VERSION)

    # 3b. V2 signals with missing sector
    missing_sector = db.signals_v2.count_documents({
        "$or": [
            {"sector": {"$in": ["Unknown", None, ""]}},
            {"sector": {"$exists": False}},
        ]
    })
    if missing_sector:
        r.warn("%d V2 signal(s) have sector='Unknown' or missing. "
               "Re-run backfill_v2_from_v1.py to enrich these." % missing_sector)
    else:
        r.ok("All V2 signals have sector populated.")

    # 3c. V2 signals with null nifty_trend_slope
    missing_slope = db.signals_v2.count_documents({
        "$or": [
            {"market_context.nifty_trend_slope": {"$exists": False}},
            {"market_context.nifty_trend_slope": None},
        ]
    })
    if missing_slope:
        r.warn("%d V2 signal(s) have null nifty_trend_slope. "
               "Point-in-time enrichment may be incomplete." % missing_slope)
    else:
        r.ok("All V2 signals have nifty_trend_slope populated.")

    # 3d. Unrealistic max_runup in V2 outcomes
    unrealistic = list(db.signals_v2.find(
        {"outcome.max_runup_pct": {"$gt": MAX_RUNUP_REALISTIC}},
        {"symbol": 1, "outcome.max_runup_pct": 1, "_id": 0}
    ))
    if unrealistic:
        r.warn("%d V2 signal(s) with max_runup_pct >%.0f%% (possible data error): %s" % (
            len(unrealistic), MAX_RUNUP_REALISTIC,
            [(d["symbol"], d["outcome"]["max_runup_pct"]) for d in unrealistic]))
    else:
        r.ok("No V2 signals with unrealistic runup (>%.0f%%)." % MAX_RUNUP_REALISTIC)

    # 3e. Entry price vs breakout level -- validates the MAX_ENTRY_ABOVE_BREAKOUT_PCT guard
    v1_sigs = list(db.signals.find(
        {"breakout_level": {"$gt": 0}, "entry_price": {"$gt": 0}},
        {"symbol": 1, "entry_price": 1, "breakout_level": 1, "_id": 0}
    ))
    too_extended = [
        s for s in v1_sigs
        if (s["entry_price"] / s["breakout_level"] - 1) * 100 > MAX_ENTRY_ABOVE_BKT_PCT
    ]
    if too_extended:
        r.warn("%d signal(s) have entry >%.0f%% above breakout level "
               "(guard may not have fired): %s" % (
                   len(too_extended), MAX_ENTRY_ABOVE_BKT_PCT,
                   [s["symbol"] for s in too_extended[:5]]))
    else:
        r.ok("All signals have entry within %.0f%% of breakout level." % MAX_ENTRY_ABOVE_BKT_PCT)

    # 3f. Legacy ACTIVE signals missing entry_note (pre-v2.4.1 -- expected)
    missing_note = db.signals.count_documents({
        "status": "ACTIVE", "entry_note": {"$exists": False}
    })
    if missing_note:
        r.ok("Note: %d ACTIVE signal(s) lack 'entry_note' (pre-v2.4.1 records -- expected)." % missing_note)

    return r


# ===========================================================================
# SECTION 4: PRICE EXISTENCE & MARKET INTEGRITY
# ===========================================================================
def audit_price_existence(db):
    r = AuditResult()
    
    # 1. Fetch trade signals (ACTIVE or CLOSED)
    signals = list(db.signals.find({"status": {"$in": ["ACTIVE", "CLOSED"]}}))
    r.ok(f"Found {len(signals)} total trade signals in database.")
    
    # 2. Filter out already PASSED signals
    to_audit = [s for s in signals if s.get("price_audit_status") != "PASSED"]
    r.ok(f"Skipping {len(signals) - len(to_audit)} already PASSED signals. {len(to_audit)} signals need auditing.")
    
    if not to_audit:
        r.ok("All signals have already passed the price existence audit.")
        return r

    # Cache map for instrument keys mapping (Upstox)
    try:
        from db import get_instrument_key_mapping
        instrument_mapping = get_instrument_key_mapping()
    except Exception as e:
        instrument_mapping = {}
        r.warn(f"Could not load Upstox instrument mapping: {e}")

    import pandas as pd
    import yfinance as yf
    from utils import fetch_from_upstox
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning)

    success_count = 0
    fail_count = 0
    skip_count = 0

    for s in to_audit:
        symbol = s["symbol"]
        sig_id = s.get("signal_id", "unknown")
        entry_price = s.get("entry_price")
        
        if entry_price is None or entry_price <= 0:
            r.warn(f"Signal {sig_id}: Invalid or missing entry_price: {entry_price}")
            continue

        # Determine signal date and created date
        sig_date_raw = s.get("signal_date")
        created_raw = s.get("created_at")
        
        # Helper to convert to date
        def to_date_obj(dt_raw):
            if not dt_raw:
                return None
            if isinstance(dt_raw, str):
                try:
                    return pd.to_datetime(dt_raw).date()
                except Exception:
                    return None
            elif isinstance(dt_raw, datetime):
                if dt_raw.tzinfo is None:
                    dt_raw = dt_raw.replace(tzinfo=timezone.utc)
                return dt_raw.astimezone(IST).date()
            return dt_raw

        sig_date = to_date_obj(sig_date_raw) or to_date_obj(created_raw)
        created_date = to_date_obj(created_raw) or sig_date
        
        if not sig_date:
            r.error(f"Signal {sig_id}: Missing signal_date and created_at fields.")
            continue

        # Set a window around both dates to handle timezone/weekend offsets
        min_date = min(sig_date, created_date)
        max_date = max(sig_date, created_date)
        start_date = datetime.combine(min_date - timedelta(days=3), datetime.min.time())
        end_date = datetime.combine(max_date + timedelta(days=3), datetime.max.time())
        
        df = None
        source = None
        
        # Try Upstox if token and mapping are available
        upstox_token = os.getenv("UPSTOX_ACCESS_TOKEN")
        if upstox_token and instrument_mapping and symbol in instrument_mapping:
            try:
                df = fetch_from_upstox(symbol, start_date, end_date)
                if df is not None and not df.empty:
                    source = "Upstox"
            except Exception as e:
                pass
                
        # Fallback to YFinance
        if df is None or df.empty:
            try:
                ticker = f"{symbol}.NS"
                end_dt_yf = end_date + timedelta(days=1)
                yf_df = yf.download(ticker, start=start_date.strftime("%Y-%m-%d"), end=end_dt_yf.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
                if not yf_df.empty:
                    yf_df = yf_df.reset_index()
                    yf_df.columns = [c[0] if isinstance(c, tuple) else c for c in yf_df.columns]
                    yf_df.rename(columns={"Date": "DATE", "Open": "OPEN", "High": "HIGH", "Low": "LOW", "Close": "CLOSE", "Volume": "VOLUME"}, inplace=True)
                    yf_df["DATE"] = pd.to_datetime(yf_df["DATE"]).dt.date
                    df = yf_df
                    source = "YFinance"
            except Exception as e:
                pass

        if df is None or df.empty:
            r.warn(f"Signal {sig_id}: Could not fetch historical data from Upstox or YFinance for {symbol}")
            db.signals.update_one(
                {"signal_id": sig_id},
                {"$set": {
                    "price_audit_status": "SKIPPED",
                    "price_audit_error": "Could not fetch historical data"
                }}
            )
            skip_count += 1
            continue

        # Look for matching EOD candle
        # Convert df DATE column to date objects if not already
        df["DATE_ONLY"] = pd.to_datetime(df["DATE"]).dt.date
        
        price_valid = False
        candle_date = None
        low = 0.0
        high = 0.0
        
        # 1. Try to check price validity on signal breakout date (sig_date)
        matching_rows = df[df["DATE_ONLY"] == sig_date]
        if not matching_rows.empty:
            candle = matching_rows.iloc[0]
            candle_date = candle["DATE_ONLY"]
            low = float(candle["LOW"])
            high = float(candle["HIGH"])
            price_valid = (low * 0.995) <= entry_price <= (high * 1.005)
            
        # 2. If not valid on sig_date, try checking on position execution date (created_date)
        if not price_valid and created_date != sig_date:
            created_rows = df[df["DATE_ONLY"] == created_date]
            if not created_rows.empty:
                candle = created_rows.iloc[0]
                candle_date = candle["DATE_ONLY"]
                low = float(candle["LOW"])
                high = float(candle["HIGH"])
                price_valid = (low * 0.995) <= entry_price <= (high * 1.005)
                
        # 3. Fallback to closest date within 2 days of sig_date if no exact match or valid candle found
        if not price_valid and matching_rows.empty:
            df["day_diff"] = df["DATE_ONLY"].apply(lambda x: abs((x - sig_date).days))
            closest_rows = df[df["day_diff"] <= 2].sort_values("day_diff")
            if not closest_rows.empty:
                candle = closest_rows.iloc[0]
                candle_date = candle["DATE_ONLY"]
                low = float(candle["LOW"])
                high = float(candle["HIGH"])
                price_valid = (low * 0.995) <= entry_price <= (high * 1.005)
                
        if not candle_date:
            r.warn(f"Signal {sig_id}: No trading candle found within 2 days of {sig_date} for {symbol}")
            db.signals.update_one(
                {"signal_id": sig_id},
                {"$set": {
                    "price_audit_status": "SKIPPED",
                    "price_audit_error": f"No candle found near {sig_date}"
                }}
            )
            skip_count += 1
            continue
        
        if price_valid:
            db.signals.update_one(
                {"signal_id": sig_id},
                {"$set": {
                    "price_audit_status": "PASSED",
                    "price_audit_source": source,
                    "price_audit_candle_date": datetime.combine(candle_date, datetime.min.time()),
                    "price_audit_range": f"Rs.{low:.2f} - Rs.{high:.2f}"
                }}
            )
            success_count += 1
        else:
            if entry_price < low:
                pct_diff = (low - entry_price) / low * 100
                err_msg = f"Entry price Rs.{entry_price:.2f} is BELOW candle Low Rs.{low:.2f} by {pct_diff:.2f}%"
            else:
                pct_diff = (entry_price - high) / high * 100
                err_msg = f"Entry price Rs.{entry_price:.2f} is ABOVE candle High Rs.{high:.2f} by {pct_diff:.2f}%"
                
            r.error(f"Signal {sig_id} ({symbol}): {err_msg} on {candle_date} (Source: {source})")
            db.signals.update_one(
                {"signal_id": sig_id},
                {"$set": {
                    "price_audit_status": "FAILED",
                    "price_audit_error": err_msg,
                    "price_audit_source": source,
                    "price_audit_candle_date": datetime.combine(candle_date, datetime.min.time()),
                    "price_audit_range": f"Rs.{low:.2f} - Rs.{high:.2f}"
                }}
            )
            fail_count += 1
            
    r.ok(f"Audit completed: {success_count} passed, {fail_count} failed, {skip_count} skipped.")
    return r


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="IPO-Base-Scanner master audit")
    parser.add_argument("--json",    action="store_true", help="Output as JSON")
    parser.add_argument("--section", type=int, choices=[1, 2, 3, 4],
                        help="Run only one section")
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  IPO-Base-Scanner -- Master System Audit")
    print("  Run at: %s IST" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("  Expected version: %s" % EXPECTED_VERSION)
    print("=" * 70)

    try:
        db = get_db()
    except RuntimeError as e:
        print("\n[ERR] Cannot connect to MongoDB: %s" % e)
        sys.exit(2)

    sections = {
        1: ("Section 1: Database Integrity",      lambda: audit_database_integrity(db)),
        2: ("Section 2: Telemetry / Log Quality", lambda: audit_log_quality(db)),
        3: ("Section 3: Strategy Consistency",    lambda: audit_strategy_consistency(db)),
        4: ("Section 4: Price Existence & Market Integrity", lambda: audit_price_existence(db)),
    }

    run_nums = [args.section] if args.section else [1, 2, 3, 4]

    results   = {}
    worst_ext = 0
    for num in run_nums:
        name, fn = sections[num]
        result   = fn()
        results[num] = (name, result)
        if not args.json:
            result.print_report(name)
        if result.exit_code > worst_ext:
            worst_ext = result.exit_code

    if args.json:
        output = {
            "audit_time":       datetime.now().isoformat(),
            "expected_version": EXPECTED_VERSION,
            "sections":         [res.to_dict(name) for name, res in results.values()],
            "overall_status":   "PASS" if worst_ext == 0 else ("WARN" if worst_ext == 1 else "FAIL"),
        }
        print(json.dumps(output, indent=2))
    else:
        status = "PASS" if worst_ext == 0 else ("WARN" if worst_ext == 1 else "FAIL")
        print("\n" + "=" * 70)
        print("  Overall: [%s]" % status)
        print("=" * 70 + "\n")

    sys.exit(worst_ext)


if __name__ == "__main__":
    main()
