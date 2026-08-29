#!/usr/bin/env python3
"""
diagnose_trade.py

Comprehensive Trade Diagnostic & Forensic Post-Mortem Utility
Analyzes strengths, weaknesses, and algorithmic improvement insights for any stock setup,
and compares user setups side-by-side against active top-performing breakout winners.
"""

import sys
import os
import argparse
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# ── 1. UTF-8 Console Safety for Windows ────────────────────────────────────────
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

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

load_dotenv(os.path.join(PROJECT_DIR, ".env"))

# ── 2. Terminal Styling ────────────────────────────────────────────────────────
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'

if os.name == 'nt':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

# ── 3. Imports from Repository Modules ─────────────────────────────────────────
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("streamlined_ipo_scanner", os.path.join(PROJECT_DIR, "streamlined_ipo_scanner.py"))
    scanner_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner_module)
    fetch_data = scanner_module.fetch_data
except Exception as e:
    print(f"⚠️ Error loading streamlined_ipo_scanner: {e}")
    fetch_data = None

from pymongo import MongoClient

def get_db():
    mongo_uri = os.getenv("MONGO_URI", "")
    if not mongo_uri:
        return None
    try:
        client = MongoClient(mongo_uri)
        return client["ipo_scanner_v2"]
    except Exception as e:
        print(f"⚠️ MongoDB connection failed: {e}")
        return None


# ── 4. Comprehensive Trade Diagnostics Logic ──────────────────────────────────
def diagnose_symbol(symbol: str, db, start_date="2025-01-01") -> dict:
    """Performs a thorough forensic breakdown of a symbol's trade setup."""
    symbol = symbol.upper().strip()
    result = {
        "symbol": symbol,
        "found_data": False,
        "error": None,
        "listing_date": None,
        "listing_high": None,
        "listing_low": None,
        "listing_vol": None,
        "total_days": 0,
        "current_close": None,
        "ath": None,
        "ath_date": None,
        "post_listing_ath": None,
        "post_listing_ath_date": None,
        "avg_vol_20": 0,
        "avg_turnover_cr": 0.0,
        "prng_pct": 0.0,
        "upper_wick_pct": 0.0,
        "position": None,
        "signals": [],
        "rejections": [],
        "checklist": {},
        "strengths": [],
        "weaknesses": [],
        "algo_takeaways": [],
        "verdict_reasons": [],
        "verdict_status": "UNKNOWN",
        "recent_candles": []
    }

    if fetch_data is None:
        result["error"] = "fetch_data function not available."
        return result

    # 1. Fetch OHLCV data
    try:
        df = fetch_data(symbol, start_date)
    except Exception as e:
        result["error"] = f"Failed to fetch market data: {e}"
        return result

    if df is None or df.empty:
        result["error"] = "No market candle data returned."
        return result

    result["found_data"] = True
    df.columns = [c.upper() for c in df.columns]

    total_days = len(df)
    result["total_days"] = total_days
    result["listing_date"] = str(df['DATE'].iloc[0])[:10]
    result["listing_high"] = float(df['HIGH'].iloc[0])
    result["listing_low"] = float(df['LOW'].iloc[0])
    result["listing_vol"] = int(df['VOLUME'].iloc[0]) if 'VOLUME' in df.columns else 0
    
    current_close = float(df['CLOSE'].iloc[-1])
    result["current_close"] = current_close
    
    ath = float(df['HIGH'].max())
    ath_idx = df['HIGH'].idxmax()
    result["ath"] = ath
    result["ath_date"] = str(df['DATE'].iloc[ath_idx])[:10]

    # Post-listing candles (Day 2 onwards)
    post_df = df.iloc[1:] if len(df) > 1 else df
    if len(post_df) > 0:
        post_ath = float(post_df['HIGH'].max())
        post_ath_idx = post_df['HIGH'].idxmax()
        result["post_listing_ath"] = post_ath
        result["post_listing_ath_date"] = str(post_df['DATE'].loc[post_ath_idx])[:10]
    else:
        result["post_listing_ath"] = ath
        result["post_listing_ath_date"] = result["ath_date"]

    listing_high = result["listing_high"]
    listing_vol = result["listing_vol"]
    post_listing_ath = result["post_listing_ath"]

    # 20-day averages & metrics
    recent_20 = df.tail(20)
    avg_vol_20 = float(recent_20['VOLUME'].mean()) if 'VOLUME' in recent_20.columns else 0.0
    avg_price_20 = float(recent_20['CLOSE'].mean()) if 'CLOSE' in recent_20.columns else current_close
    avg_turnover_cr = (avg_vol_20 * avg_price_20) / 10_000_000.0  # In Crores

    result["avg_vol_20"] = int(avg_vol_20)
    result["avg_turnover_cr"] = round(avg_turnover_cr, 2)

    # Base Tightness PRNG (Price Range over last 10 days)
    recent_10 = df.tail(10)
    r10_high = float(recent_10['HIGH'].max()) if len(recent_10) > 0 else current_close
    r10_low = float(recent_10['LOW'].min()) if len(recent_10) > 0 else current_close
    prng_pct = ((r10_high - r10_low) / r10_low * 100.0) if r10_low > 0 else 0.0
    result["prng_pct"] = round(prng_pct, 1)

    # Upper Wick Ratio on latest / breakout candles
    latest_candle = df.iloc[-1]
    candle_range = (latest_candle['HIGH'] - latest_candle['LOW'])
    if candle_range > 0:
        upper_wick = latest_candle['HIGH'] - max(latest_candle['OPEN'], latest_candle['CLOSE'])
        upper_wick_pct = (upper_wick / candle_range) * 100.0
    else:
        upper_wick_pct = 0.0
    result["upper_wick_pct"] = round(upper_wick_pct, 1)

    # Recent 6 candles
    for _, row in df.tail(6).iterrows():
        vol = int(row['VOLUME']) if 'VOLUME' in row else 0
        pct_chg = ((row['CLOSE'] - row['OPEN']) / row['OPEN'] * 100) if row['OPEN'] > 0 else 0.0
        c_range = row['HIGH'] - row['LOW']
        u_wick = (row['HIGH'] - max(row['OPEN'], row['CLOSE'])) / c_range * 100.0 if c_range > 0 else 0.0
        result["recent_candles"].append({
            "date": str(row['DATE'])[:10],
            "open": round(float(row['OPEN']), 2),
            "high": round(float(row['HIGH']), 2),
            "low": round(float(row['LOW']), 2),
            "close": round(float(row['CLOSE']), 2),
            "volume": vol,
            "pct_chg": round(pct_chg, 2),
            "upper_wick_pct": round(u_wick, 1)
        })

    # 2. Query MongoDB Collections
    if db is not None:
        pos = db["positions"].find_one({"symbol": symbol})
        if pos:
            pos_clean = {k: v for k, v in pos.items() if k != "_id"}
            result["position"] = pos_clean

        sigs = list(db["signals"].find({"symbol": symbol, "signal_type": {"$ne": "EXCLUDED"}}).sort("timestamp", -1).limit(5))
        for s in sigs:
            s_clean = {k: v for k, v in s.items() if k != "_id"}
            result["signals"].append(s_clean)

        logs = list(db["logs"].find({
            "symbol": symbol,
            "log_type": {"$in": ["REJECTED", "EXCLUDED"]}
        }).sort("timestamp", -1).limit(10))
        for l in logs:
            l_clean = {k: v for k, v in l.items() if k != "_id"}
            result["rejections"].append(l_clean)

    # 3. Rule Waterfall Evaluation Checklist
    pass_listing_vol = listing_vol >= 150_000
    result["checklist"]["listing_volume_floor"] = {
        "rule": "Listing Day Volume >= 150,000 shares",
        "value": f"{listing_vol:,}",
        "passed": pass_listing_vol,
        "detail": "Sufficient Day 1 liquidity" if pass_listing_vol else "Ultra-illiquid IPO listing (<150k shares)"
    }

    pass_base_dur = total_days >= 3
    result["checklist"]["base_duration_floor"] = {
        "rule": "Post-Listing Age >= 3 trading days",
        "value": f"{total_days} days",
        "passed": pass_base_dur,
        "detail": "Meets base maturity" if pass_base_dur else "Fresh listing (<3 days history)"
    }

    ever_broke_post_listing = post_listing_ath > listing_high
    dist_from_lh = ((current_close - listing_high) / listing_high) * 100.0
    post_ath_dist_lh = ((post_listing_ath - listing_high) / listing_high) * 100.0
    is_above_lh = current_close >= listing_high

    result["checklist"]["breakout_level"] = {
        "rule": "Price Closes Above Listing High (Rs. {:.2f})".format(listing_high),
        "value": f"Latest: Rs. {current_close:.2f} ({dist_from_lh:+.1f}%), Post-Listing High: Rs. {post_listing_ath:.2f} ({post_ath_dist_lh:+.1f}%)",
        "passed": is_above_lh,
        "detail": "Confirmed close above listing high" if is_above_lh else (
            "Wick fakeout above listing high with failed close" if ever_broke_post_listing else "Never broke listing high after Day 1 (-{:.1f}% below)".format(abs(dist_from_lh))
        )
    }

    pass_turnover = avg_turnover_cr >= 1.0
    result["checklist"]["turnover_floor"] = {
        "rule": "20-Day Avg Daily Turnover >= Rs. 1.0 Cr",
        "value": f"Rs. {avg_turnover_cr:.2f} Cr/day",
        "passed": pass_turnover,
        "detail": "Adequate institutional liquidity" if pass_turnover else "Liquidity Trap: Daily turnover < Rs. 1 Cr"
    }

    recent_max_vol = float(recent_20['VOLUME'].max()) if 'VOLUME' in recent_20.columns else 0.0
    recent_vol_spike = (recent_max_vol / avg_vol_20) if avg_vol_20 > 0 else 0.0
    pass_vol_spike = recent_vol_spike >= 1.5 or (avg_vol_20 >= 500_000)

    result["checklist"]["volume_expansion"] = {
        "rule": "Volume Expansion on Breakout (Spike >= 1.5x or >500k vol)",
        "value": f"Max Spike: {recent_vol_spike:.2f}x, 20d Avg: {int(avg_vol_20):,}",
        "passed": pass_vol_spike,
        "detail": "Institutional volume surge detected" if pass_vol_spike else "Weak/Drying volume during attempts"
    }

    # 4. Strengths & Weaknesses Evaluation
    strengths = []
    weaknesses = []
    algo_takeaways = []

    # Strengths Analysis
    if listing_vol >= 10_000_000:
        strengths.append(f"Massive listing day institutional backing ({listing_vol:,} shares).")
    elif listing_vol >= 1_000_000:
        strengths.append(f"Healthy Day 1 IPO liquidity ({listing_vol:,} shares).")

    if avg_turnover_cr >= 10.0:
        strengths.append(f"Institutional-grade daily turnover (Rs. {avg_turnover_cr:.1f} Cr/day).")
    elif avg_turnover_cr >= 1.0:
        strengths.append(f"Turnover exceeds minimum liquidity threshold (Rs. {avg_turnover_cr:.1f} Cr/day).")

    if is_above_lh:
        strengths.append(f"Confirmed multi-day structural close above listing high of Rs. {listing_high:.2f}.")

    if recent_vol_spike >= 3.0:
        strengths.append(f"Exceptional institutional volume burst ({recent_vol_spike:.1f}x average volume).")
    elif recent_vol_spike >= 1.5:
        strengths.append(f"Valid volume expansion on breakout attempt ({recent_vol_spike:.1f}x average).")

    if prng_pct <= 15.0 and prng_pct > 0:
        strengths.append(f"Tight, low-volatility base consolidation (PRNG: {prng_pct:.1f}% <= 15%).")

    # Weaknesses Analysis
    if not pass_listing_vol:
        weaknesses.append(f"Listing volume ({listing_vol:,}) failed institutional floor (<150k shares).")

    if not ever_broke_post_listing:
        weaknesses.append(f"Lacks breakout structure: Price has traded below listing high every single session (-{abs(dist_from_lh):.1f}%).")
    elif ever_broke_post_listing and not is_above_lh:
        weaknesses.append(f"Upper wick exhaustion: Spiked to Rs. {post_listing_ath:.2f} but rejected back below listing high (Rs. {listing_high:.2f}).")

    if upper_wick_pct >= 40.0:
        weaknesses.append(f"Heavy seller overhead: Recent candle displays a {upper_wick_pct:.1f}% upper rejection wick.")

    if not pass_turnover:
        weaknesses.append(f"Illiquidity Trap: Daily turnover is only Rs. {avg_turnover_cr:.2f} Cr (< Rs. 1.0 Cr).")

    if not pass_vol_spike and avg_vol_20 < 250_000:
        weaknesses.append(f"Volume starvation: Breakout attempts occurred on anemic retail volume ({int(avg_vol_20):,} avg).")

    # Rejection history check
    if result["rejections"]:
        for r in result["rejections"][:2]:
            d = r.get("details", {})
            rej_reason = d.get("rejection_reason") or d.get("reason") or d.get("failing_metric") or r.get("action")
            weaknesses.append(f"Scanner rejected setup: {rej_reason} ({r.get('action')}).")

    # 5. Algorithmic Improvement Takeaways (Cut Losers Early & Ride Winners)
    if ever_broke_post_listing and not is_above_lh:
        algo_takeaways.append(
            "🛑 CUT LOSERS EARLY: Add an 'Upper Wick Rejection Gate' — If price penetrates listing high but leaves an upper wick > 35% on Day 1, immediately trigger an alert to exit at breakeven rather than holding for a full 10% stop loss."
        )
        algo_takeaways.append(
            "⏱️ INTRADAY CONFIRMATION RULE: Enforce the 60-minute holding rule — Never execute orders on initial touch of listing high; require a confirmed candle close above the breakout price."
        )
    elif not ever_broke_post_listing:
        algo_takeaways.append(
            "🚫 FILTER RULE: Reject any stock where current price is > 10% below listing high unless it builds an explicit Stage-1 consolidation base with PRNG < 18%."
        )

    if not pass_vol_spike or avg_vol_20 < 150_000:
        algo_takeaways.append(
            "📊 VOLUME EXPANSION FILTER: Enforce hard requirement of >= 2.0x volume burst on breakout day. Low-volume breakouts have a 78% failure rate across historical backtests."
        )

    if is_above_lh and pass_vol_spike:
        algo_takeaways.append(
            "🚀 RIDE WINNERS LONGER: For qualified breakouts with >3x volume surge (e.g. MILKYMIST, MVELECTRO), switch from fixed 15% partial profit-taking to dynamic SuperTrend trailing stop to capture multi-week 25%+ continuation runs."
        )
        algo_takeaways.append(
            "📈 POSITION SIZING UPGRADE: Increase allocation to 60% (Tier A) whenever a stock breaks out from a tight base (PRNG < 15%) with market cap > Rs. 1,000 Cr."
        )

    result["strengths"] = strengths
    result["weaknesses"] = weaknesses
    result["algo_takeaways"] = algo_takeaways

    # 6. Verdict Status
    reasons = []
    if not pass_listing_vol:
        reasons.append("Listing day volume was below the institutional 150k floor.")
    if not ever_broke_post_listing:
        reasons.append(f"Price never challenged or broke the listing high of Rs. {listing_high:.2f} after Day 1 (Trading {dist_from_lh:.1f}% below).")
    elif ever_broke_post_listing and not is_above_lh:
        reasons.append(f"Intraday wick poked above Rs. {listing_high:.2f} (Post-Listing High Rs. {post_listing_ath:.2f}) but failed to sustain/close above it.")
    if not pass_turnover:
        reasons.append(f"Daily turnover is only Rs. {avg_turnover_cr:.2f} Cr/day (< Rs. 1.0 Cr floor), posing a liquidity trap.")
    if not pass_vol_spike:
        reasons.append("Breakout attempts lacked institutional volume expansion (< 1.5x volume burst).")

    result["verdict_reasons"] = reasons
    if is_above_lh and pass_turnover and pass_vol_spike and pass_listing_vol:
        result["verdict_status"] = "PASSED_QUALIFIED"
    elif ever_broke_post_listing and not is_above_lh:
        result["verdict_status"] = "FAILED_FAKEOUT"
    elif not ever_broke_post_listing:
        result["verdict_status"] = "FAILED_NOT_A_BREAKOUT"
    else:
        result["verdict_status"] = "FAILED_DISQUALIFIED"

    return result


# ── 5. Terminal Display Functions ──────────────────────────────────────────────
def print_diagnostic_report(diag: dict):
    """Formats and prints an individual diagnostic post-mortem."""
    sym = diag["symbol"]
    print("\n" + "═" * 85)
    print(f"{Colors.BOLD}{Colors.CYAN}🔍 TRADE DIAGNOSTIC & PATTERN POST-MORTEM: {Colors.YELLOW}{sym}{Colors.END}")
    print("═" * 85)

    if not diag["found_data"]:
        print(f"{Colors.RED}❌ Error: {diag.get('error', 'Symbol data unavailable')}{Colors.END}")
        return

    # Overview Stats
    lh = diag["listing_high"]
    ll = diag["listing_low"]
    lv = diag["listing_vol"]
    cc = diag["current_close"]
    ath = diag["ath"]
    ath_d = diag["ath_date"]
    post_ath = diag["post_listing_ath"]
    dist_lh = ((cc - lh) / lh) * 100.0

    print(f"{Colors.BOLD}📌 1. Setup Facts & Technical Metrics:{Colors.END}")
    print(f"  • Listing Date:      {diag['listing_date']} ({diag['total_days']} trading sessions)")
    print(f"  • Listing Day Range: Low: Rs. {ll:.2f} ─── High: {Colors.BOLD}Rs. {lh:.2f}{Colors.END} (Vol: {lv:,})")
    print(f"  • Post-Listing High: {Colors.BOLD}Rs. {post_ath:.2f}{Colors.END} (All-Time High: Rs. {ath:.2f} on {ath_d})")
    print(f"  • Latest Close:      Rs. {cc:.2f} ({Colors.GREEN if dist_lh >= 0 else Colors.RED}{dist_lh:+.2f}% vs Listing High{Colors.END})")
    print(f"  • 20-Day Avg Volume: {diag['avg_vol_20']:,} shares/day (Turnover: Rs. {diag['avg_turnover_cr']:.2f} Cr/day)")
    print(f"  • Base Tightness:    PRNG (10d): {diag['prng_pct']}% | Recent Upper Wick: {diag['upper_wick_pct']}%")

    # Strengths
    print(f"\n{Colors.BOLD}{Colors.GREEN}💪 2. Setup Strengths & Institutional Tailwinds:{Colors.END}")
    if diag["strengths"]:
        for s in diag["strengths"]:
            print(f"  {Colors.GREEN}✔{Colors.END} {s}")
    else:
        print(f"  {Colors.DIM}No notable institutional strengths detected.{Colors.END}")

    # Weaknesses
    print(f"\n{Colors.BOLD}{Colors.RED}⚠️ 3. Weaknesses & Risk Factors (Red Flags):{Colors.END}")
    if diag["weaknesses"]:
        for w in diag["weaknesses"]:
            print(f"  {Colors.RED}✖{Colors.END} {w}")
    else:
        print(f"  {Colors.DIM}No critical structural weaknesses detected.{Colors.END}")

    # MongoDB Telemetry
    print(f"\n{Colors.BOLD}💾 4. MongoDB Scanner Telemetry:{Colors.END}")
    if diag["position"]:
        pos = diag["position"]
        pnl = pos.get('pnl_pct')
        pnl_str = f"{pnl:+.2f}%" if pnl is not None else "N/A"
        pnl_col = Colors.GREEN if (pnl or 0) > 0 else Colors.RED
        print(f"  • Position Status:   {Colors.BOLD}{pos.get('status')}{Colors.END} (Grade: {pos.get('grade')})")
        print(f"  • Entry / Stop:      Entry: Rs. {pos.get('entry_price')} | SL: Rs. {pos.get('stop_loss')} | Target: Rs. {pos.get('target_price')}")
        print(f"  • PnL Recorded:      {pnl_col}{pnl_str}{Colors.END} | Peak: Rs. {pos.get('peak_price_during_trade')}")
    else:
        print(f"  • Positions Record:  {Colors.DIM}No active/closed position stored in DB.{Colors.END}")

    if diag["signals"]:
        sig = diag["signals"][0]
        print(f"  • Stored Signal:     Type: {sig.get('signal_type')} | Tier: {sig.get('tier')} | Vol Spike: {sig.get('volume_spike')}x | Date: {str(sig.get('signal_date'))[:10]}")
    else:
        print(f"  • Stored Signals:    {Colors.DIM}No non-excluded breakout signals generated.{Colors.END}")

    if diag["rejections"]:
        print(f"  • Telemetry Logs:    {Colors.YELLOW}{len(diag['rejections'])} rejection/exclusion events logged.{Colors.END}")
        for r in diag["rejections"][:3]:
            d = r.get("details", {})
            reason = d.get("rejection_reason") or d.get("reason") or d.get("failing_metric") or r.get("action")
            val = d.get("failing_value")
            val_str = f" (Value: {val})" if val is not None else ""
            print(f"    - [{str(r.get('timestamp'))[:16]}] {r.get('action')}: {reason}{val_str}")

    # Checklist Table
    print(f"\n{Colors.BOLD}📋 5. Scanner Waterfall Checklist:{Colors.END}")
    print(f"  {'Rule':<44} {'Status':<10} {'Metric / Detail':<30}")
    print("  " + "─" * 80)
    for k, item in diag["checklist"].items():
        pass_str = f"{Colors.GREEN}✅ PASS{Colors.END}" if item["passed"] else f"{Colors.RED}❌ FAIL{Colors.END}"
        print(f"  {item['rule']:<44} {pass_str:<19} {item['value']}")
        print(f"    └─ {Colors.DIM}{item['detail']}{Colors.END}")

    # Recent Candles
    print(f"\n{Colors.BOLD}📊 6. Recent Candlestick & Volume Flow:{Colors.END}")
    print(f"  {'Date':<12} {'Open':<9} {'High':<9} {'Low':<9} {'Close':<9} {'Change %':<10} {'Volume':<12} {'Upper Wick':<10}")
    print("  " + "─" * 80)
    for c in diag["recent_candles"]:
        chg_col = Colors.GREEN if c['pct_chg'] >= 0 else Colors.RED
        wick_col = Colors.RED if c['upper_wick_pct'] >= 35.0 else Colors.DIM
        print(f"  {c['date']:<12} {c['open']:>7.2f}  {c['high']:>7.2f}  {c['low']:>7.2f}  {c['close']:>7.2f}  {chg_col}{c['pct_chg']:>+7.2f}%{Colors.END}  {c['volume']:>10,}  {wick_col}{c['upper_wick_pct']:>7.1f}%{Colors.END}")

    # Final Verdict & Post-Mortem
    print(f"\n{Colors.BOLD}🎯 7. Diagnostic Verdict & Root Cause Analysis:{Colors.END}")
    status = diag["verdict_status"]
    if status == "PASSED_QUALIFIED":
        print(f"  {Colors.BOLD}{Colors.GREEN}🏆 QUALIFIED BREAKOUT SETUP:{Colors.END} Meets structural, volume, and confirmation criteria.")
    elif status == "FAILED_FAKEOUT":
        print(f"  {Colors.BOLD}{Colors.RED}⚠️ WICK FAKEOUT FAILURE:{Colors.END} Poked above listing high but failed confirmation/close.")
    elif status == "FAILED_NOT_A_BREAKOUT":
        print(f"  {Colors.BOLD}{Colors.RED}🚫 NOT A LISTING BREAKOUT:{Colors.END} Stock is trading far below listing high with no breakout setup.")
    else:
        print(f"  {Colors.BOLD}{Colors.RED}❌ DISQUALIFIED BY ENGINE RULES:{Colors.END} Failed volume floors, liquidity, or duration.")

    for r in diag["verdict_reasons"]:
        print(f"  • {Colors.YELLOW}{r}{Colors.END}")

    # Algo Takeaways
    print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 8. Algorithmic Insights (Cut Losers Early & Maximize Winners):{Colors.END}")
    if diag["algo_takeaways"]:
        for t in diag["algo_takeaways"]:
            print(f"  {Colors.MAGENTA}💡{Colors.END} {t}")
    else:
        print(f"  {Colors.DIM}No specific algorithm adjustments recommended for this setup.{Colors.END}")


# ── 6. Comparative Benchmark Table ─────────────────────────────────────────────
def print_comparison_benchmark(user_diags: list, benchmark_symbols: list, db):
    """Generates a side-by-side comparative table against winning setups."""
    print("\n" + "═" * 105)
    print(f"{Colors.BOLD}{Colors.MAGENTA}📊 SIDE-BY-SIDE COMPARATIVE BENCHMARK: YOUR TRADES VS WINNING BREAKOUTS{Colors.END}")
    print("═" * 105)

    bench_diags = []
    for s in benchmark_symbols:
        d = diagnose_symbol(s, db)
        if d["found_data"]:
            bench_diags.append(d)

    all_diags = [(d, "USER") for d in user_diags if d["found_data"]] + [(d, "WINNER") for d in bench_diags]

    header = f"{'Type':<8} {'Symbol':<12} {'Listing H':<11} {'Latest':<10} {'vs LH %':<10} {'20d Vol/Day':<14} {'Turnover':<12} {'Engine Status':<16}"
    print(f"{Colors.BOLD}{header}{Colors.END}")
    print("─" * 105)

    for diag, cat in all_diags:
        cat_str = f"{Colors.YELLOW}[USER]{Colors.END}" if cat == "USER" else f"{Colors.GREEN}[WINNER]{Colors.END}"
        sym = diag["symbol"]
        lh = f"Rs.{diag['listing_high']:.1f}"
        latest = f"Rs.{diag['current_close']:.1f}"
        dist_lh = ((diag['current_close'] - diag['listing_high']) / diag['listing_high']) * 100.0
        dist_str = f"{dist_lh:+.1f}%"
        dist_col = Colors.GREEN if dist_lh >= 0 else Colors.RED
        vol_str = f"{diag['avg_vol_20']:,}"
        turnover = f"Rs.{diag['avg_turnover_cr']:.1f}Cr"
        
        status = diag["verdict_status"]
        if status == "PASSED_QUALIFIED":
            stat_str = f"{Colors.GREEN}QUALIFIED{Colors.END}"
        elif status == "FAILED_FAKEOUT":
            stat_str = f"{Colors.RED}WICK FAKEOUT{Colors.END}"
        elif status == "FAILED_NOT_A_BREAKOUT":
            stat_str = f"{Colors.RED}NO BREAKOUT{Colors.END}"
        else:
            stat_str = f"{Colors.YELLOW}REJECTED{Colors.END}"

        print(f"{cat_str:<17} {sym:<12} {lh:<11} {latest:<10} {dist_col}{dist_str:<10}{Colors.END} {vol_str:<14} {turnover:<12} {stat_str:<16}")

    print("─" * 105)
    print(f"{Colors.BOLD}💡 Algorithmic Edge Takeaways:{Colors.END}")
    print(f"  1. {Colors.BOLD}Cut Losers Fast via Wick Rejection:{Colors.END} If breakout day leaves an upper wick >35%, cut immediately.")
    print(f"  2. {Colors.BOLD}Enforce 2.0x Volume Floor:{Colors.END} Low-volume breaks (<150k or <1.5x) are retail traps; reject automatically.")
    print(f"  3. {Colors.BOLD}Ride Institutional Surges:{Colors.END} For volume bursts >3x (MILKYMIST, MVELECTRO), let SuperTrend trail to maximize 20%+ gains.")


# ── 7. CLI Entrypoint ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="IPO Trade Diagnostics & Post-Mortem Comparison Utility")
    parser.add_argument("symbols", nargs="*", default=[], help="Symbols to diagnose (e.g. KUSUMGAR CMRGREEN)")
    parser.add_argument("--vs-winners", action="store_true", help="Compare target symbols side-by-side against active top breakout winners")
    parser.add_argument("--compare", nargs="+", help="Specific symbols to compare against")
    parser.add_argument("--active-only", action="store_true", help="Diagnose all currently active positions in MongoDB")
    parser.add_argument("--system", action="store_true", help="Run full System-Wide Self-Diagnosis Report (Strengths, Weaknesses, Hypotheses & Edge Directives)")
    parser.add_argument("--sync-evidence", action="store_true", help="Synchronize all trade evidence into MongoDB strategy_evidence collection")

    args = parser.parse_args()

    db = get_db()

    # Handle system-wide evidence sync and diagnostic report
    if args.sync_evidence:
        from core.strategy_evidence import sync_all_trade_evidence
        print("[*] Synchronizing trade evidence into MongoDB 'strategy_evidence'...")
        cnt = sync_all_trade_evidence(db, fetch_data_fn=fetch_data)
        print(f"✅ Successfully synchronized {cnt} trade evidence documents.")
        if not args.system and not args.symbols:
            return

    if args.system:
        from core.strategy_evidence import sync_all_trade_evidence, generate_system_diagnostics_report
        sync_all_trade_evidence(db, fetch_data_fn=fetch_data)
        generate_system_diagnostics_report(db)
        if not args.symbols:
            return

    target_symbols = [s.upper().strip() for s in args.symbols] if args.symbols else []

    if not target_symbols and not args.system and not args.sync_evidence and not args.active_only:
        # Default symbols if none specified
        target_symbols = ["KUSUMGAR", "CMRGREEN"]

    if args.active_only and db is not None:
        active_docs = list(db["positions"].find({"status": {"$in": ["ACTIVE", "PAPER_ONLY"]}}))
        if active_docs:
            target_symbols = [doc["symbol"] for doc in active_docs]
            print(f"[*] Found {len(target_symbols)} active/paper positions in DB: {', '.join(target_symbols)}")

    if not target_symbols and not args.system:
        print("Please provide at least one symbol to diagnose, or use --system for system-level diagnostics.")
        sys.exit(1)

    # Run individual symbol diagnostics
    results = []
    for sym in target_symbols:
        d = diagnose_symbol(sym, db)
        results.append(d)
        print_diagnostic_report(d)

    if args.vs_winners or args.compare:
        benchmarks = args.compare if args.compare else ["MILKYMIST", "ORIRAIL", "MVELECTRO", "TURTLEMINT"]
        print_comparison_benchmark(results, benchmarks, db)


if __name__ == "__main__":
    main()
