"""
analyze_db_quality_and_patterns.py
===================================
Deep DB quality check + winner pattern fingerprinting.
Pulls signals, positions (including closed), signals_v2 and surfaces:

  1. DB Quality Summary        — field coverage, data completeness
  2. Closed Trade Performance  — actual PnL, win rate, avg win/loss
  3. Grade Performance         — does grade A+ actually outperform B/C?
  4. Volume & Consolidation    — winner vs loser traits
  5. Market Context            — nifty_trend, nifty_slope in winners vs losers
  6. Scanner / Window          — which scan window (10/20/40/80) produces best setups
  7. Time Patterns             — month-of-year / day-of-week edge
"""

import os
import sys
import pandas as pd
import numpy as np
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    print("ERROR: MONGO_URI not set")
    sys.exit(1)

client = MongoClient(MONGO_URI, tz_aware=False)
db = client["ipo_scanner_v2"]

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 0: Load raw data
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "="*80)
print("  IPO Base Scanner — DB Quality & Winner Pattern Analysis")
print("  Run at:", datetime.now().strftime("%Y-%m-%d %H:%M IST"))
print("="*80)

signals_raw  = list(db.signals.find({}, {"_id": 0}))
positions_raw = list(db.positions.find({}, {"_id": 0}))
v2_raw       = list(db.signals_v2.find({}, {"_id": 0}))

df_sig  = pd.DataFrame(signals_raw)
df_pos  = pd.DataFrame(positions_raw)
df_v2   = pd.DataFrame(v2_raw)

print(f"\n[DB] signals    : {len(df_sig)} documents")
print(f"[DB] positions  : {len(df_pos)} documents")
print(f"[DB] signals_v2 : {len(df_v2)} documents")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: DB Quality — field coverage
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─"*80)
print("  SECTION 1: DATABASE QUALITY — Field Coverage")
print("─"*80)

CRITICAL_SIGNAL_FIELDS = [
    "symbol", "signal_date", "entry_price", "stop_loss", "target_price",
    "grade", "volume_ratio", "consolidation_range_pct", "scanner",
    "scanner_version", "status", "sector", "market_cap"
]

CRITICAL_POS_FIELDS = [
    "symbol", "entry_date", "entry_price", "stop_loss", "current_price",
    "status", "pnl_pct", "max_runup_pct", "days_held", "grade"
]

def coverage_report(df, fields, name):
    if df.empty:
        print(f"  {name}: NO DATA")
        return
    print(f"\n  {name} ({len(df)} docs):")
    for f in fields:
        if f not in df.columns:
            pct = 0.0
            null_count = len(df)
        else:
            null_count = df[f].isna().sum() + (df[f] == "").sum() + (df[f] == "Unknown").sum()
            pct = (1 - null_count / len(df)) * 100
        flag = "  ✅" if pct >= 90 else ("  ⚠️ " if pct >= 60 else "  ❌")
        print(f"{flag} {f:<35} {pct:>5.1f}% filled  ({int(len(df)-null_count)}/{len(df)})")

coverage_report(df_sig, CRITICAL_SIGNAL_FIELDS, "signals")
coverage_report(df_pos, CRITICAL_POS_FIELDS, "positions")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: Closed Trade Performance
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─"*80)
print("  SECTION 2: CLOSED TRADE PERFORMANCE")
print("─"*80)

if df_pos.empty:
    print("  No positions found.")
else:
    df_pos["pnl_pct"]     = pd.to_numeric(df_pos.get("pnl_pct", 0), errors="coerce").fillna(0)
    df_pos["max_runup_pct"] = pd.to_numeric(df_pos.get("max_runup_pct", pd.Series(dtype=float)), errors="coerce").fillna(0)
    df_pos["days_held"]   = pd.to_numeric(df_pos.get("days_held", 0), errors="coerce").fillna(0)

    closed = df_pos[df_pos.get("status", pd.Series(dtype=str)).str.upper().isin(["CLOSED", "TARGET_HIT", "STOPPED_OUT"])].copy() if "status" in df_pos.columns else pd.DataFrame()
    active = df_pos[df_pos.get("status", pd.Series(dtype=str)).str.upper() == "ACTIVE"].copy() if "status" in df_pos.columns else df_pos

    print(f"\n  Active positions : {len(active)}")
    print(f"  Closed positions : {len(closed)}")

    if not closed.empty:
        winners = closed[closed["pnl_pct"] > 0]
        losers  = closed[closed["pnl_pct"] <= 0]
        win_rate = len(winners) / len(closed) * 100

        avg_win  = winners["pnl_pct"].mean() if not winners.empty else 0
        avg_loss = losers["pnl_pct"].mean()  if not losers.empty else 0
        expectancy = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)
        rr = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

        print(f"\n  Win Rate         : {win_rate:.1f}%  ({len(winners)} W / {len(losers)} L)")
        print(f"  Avg Win          : +{avg_win:.2f}%")
        print(f"  Avg Loss         :  {avg_loss:.2f}%")
        print(f"  Reward:Risk      :  {rr:.2f}x")
        print(f"  Expectancy/trade : {expectancy:+.2f}%")
        print(f"  Max single gain  : +{closed['pnl_pct'].max():.2f}%")
        print(f"  Max single loss  :  {closed['pnl_pct'].min():.2f}%")
        print(f"  Avg days held    :  {closed['days_held'].mean():.1f} days")

        if not winners.empty and "max_runup_pct" in winners.columns:
            print(f"\n  [Winners] Avg max runup   : +{winners['max_runup_pct'].mean():.1f}%")
            print(f"  [Winners] Avg days held   :  {winners['days_held'].mean():.1f} days")
        if not losers.empty and "max_runup_pct" in losers.columns:
            print(f"  [Losers]  Avg max runup   : +{losers['max_runup_pct'].mean():.1f}%  (peak before stop)")
            print(f"  [Losers]  Avg days held   :  {losers['days_held'].mean():.1f} days")
    else:
        print("\n  No closed trades found — cannot compute closed-trade stats.")
        print("  (All positions may still be ACTIVE. Analysis below uses ACTIVE PnL snapshot.)")

        # Active PnL snapshot
        if not active.empty:
            print(f"\n  [ACTIVE SNAPSHOT] {len(active)} positions")
            print(f"  Avg unrealised PnL : {active['pnl_pct'].mean():+.2f}%")
            print(f"  Best performer     : {active.loc[active['pnl_pct'].idxmax(), 'symbol']} ({active['pnl_pct'].max():+.2f}%)")
            print(f"  Worst performer    : {active.loc[active['pnl_pct'].idxmin(), 'symbol']} ({active['pnl_pct'].min():+.2f}%)")
            pos_count = (active["pnl_pct"] > 0).sum()
            neg_count = (active["pnl_pct"] <= 0).sum()
            print(f"  Currently green    : {pos_count} / {len(active)}  ({pos_count/len(active)*100:.1f}%)")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3: Grade Performance
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─"*80)
print("  SECTION 3: GRADE PERFORMANCE BREAKDOWN")
print("─"*80)

if not df_pos.empty and "grade" in df_pos.columns:
    grade_df = df_pos.copy()
    grade_df["grade"] = grade_df["grade"].fillna("N/A")
    grade_df["pnl_pct"] = pd.to_numeric(grade_df["pnl_pct"], errors="coerce").fillna(0)

    grade_stats = grade_df.groupby("grade")["pnl_pct"].agg(
        count="count",
        avg_pnl="mean",
        median_pnl="median",
        max_pnl="max",
        min_pnl="min"
    ).sort_values("avg_pnl", ascending=False)

    print(f"\n  {'Grade':<8} {'Count':>5} {'Avg PnL':>9} {'Median':>8} {'Best':>8} {'Worst':>8}")
    print("  " + "-"*52)
    for grade, row in grade_stats.iterrows():
        print(f"  {grade:<8} {int(row['count']):>5} {row['avg_pnl']:>+8.2f}% {row['median_pnl']:>+7.2f}% {row['max_pnl']:>+7.2f}% {row['min_pnl']:>+7.2f}%")
else:
    print("  Grade data not available in positions.")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4: Volume & Consolidation — Winner DNA
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─"*80)
print("  SECTION 4: WINNER DNA — Volume & Consolidation Traits (signals)")
print("─"*80)

if not df_sig.empty:
    df_sig["pnl_pct"] = pd.to_numeric(df_sig.get("pnl_pct", 0), errors="coerce").fillna(0)
    df_sig["volume_ratio"] = pd.to_numeric(df_sig.get("volume_ratio", np.nan), errors="coerce")
    df_sig["consolidation_range_pct"] = pd.to_numeric(df_sig.get("consolidation_range_pct", np.nan), errors="coerce")
    df_sig["days_since_ipo"] = pd.to_numeric(df_sig.get("days_since_ipo", np.nan), errors="coerce")

    # Classify closed signals by outcome
    def classify(row):
        s = str(row.get("status", "")).upper()
        if s in ["CLOSED", "TARGET_HIT", "SUCCESS"]:
            return "WIN" if row.get("pnl_pct", 0) > 0 else "LOSS"
        return "ACTIVE"

    df_sig["outcome"] = df_sig.apply(classify, axis=1)
    concluded_sig = df_sig[df_sig["outcome"].isin(["WIN", "LOSS"])]

    if not concluded_sig.empty:
        for metric in ["volume_ratio", "consolidation_range_pct", "days_since_ipo"]:
            if metric in concluded_sig.columns and concluded_sig[metric].notna().sum() > 0:
                w_mean = concluded_sig[concluded_sig["outcome"] == "WIN"][metric].mean()
                l_mean = concluded_sig[concluded_sig["outcome"] == "LOSS"][metric].mean()
                print(f"\n  {metric}:")
                print(f"    Winners avg : {w_mean:.2f}")
                print(f"    Losers  avg : {l_mean:.2f}")
                delta = w_mean - l_mean
                sign = "higher" if delta > 0 else "lower"
                print(f"    → Winners have {abs(delta):.2f} {sign} {metric}")
    else:
        # All active — still show distributions across all signals
        print("\n  All signals are still ACTIVE — showing full-cohort distributions:")
        for metric in ["volume_ratio", "consolidation_range_pct", "days_since_ipo"]:
            if metric in df_sig.columns and df_sig[metric].notna().sum() > 0:
                print(f"\n  {metric}:")
                print(f"    Mean  : {df_sig[metric].mean():.2f}")
                print(f"    Median: {df_sig[metric].median():.2f}")
                print(f"    P25   : {df_sig[metric].quantile(0.25):.2f}")
                print(f"    P75   : {df_sig[metric].quantile(0.75):.2f}")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5: Market Context in signals_v2
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─"*80)
print("  SECTION 5: MARKET CONTEXT — Nifty Regime & Slope (signals_v2)")
print("─"*80)

if not df_v2.empty:
    # Flatten market_context if nested
    if "market_context" in df_v2.columns:
        mc = pd.json_normalize(df_v2["market_context"].dropna())
        df_v2_flat = pd.concat([df_v2.drop(columns=["market_context"]).reset_index(drop=True),
                                 mc.reset_index(drop=True)], axis=1)
    else:
        df_v2_flat = df_v2

    # nifty_trend distribution
    if "nifty_trend" in df_v2_flat.columns:
        trend_counts = df_v2_flat["nifty_trend"].value_counts()
        print(f"\n  Nifty Trend at signal time (n={len(df_v2_flat)}):")
        for trend, cnt in trend_counts.items():
            print(f"    {trend:<12} : {cnt} signals ({cnt/len(df_v2_flat)*100:.1f}%)")

    if "nifty_trend_slope" in df_v2_flat.columns:
        df_v2_flat["nifty_trend_slope"] = pd.to_numeric(df_v2_flat["nifty_trend_slope"], errors="coerce")
        print(f"\n  Nifty Trend Slope at signal time:")
        print(f"    Mean   : {df_v2_flat['nifty_trend_slope'].mean():.4f}")
        print(f"    Median : {df_v2_flat['nifty_trend_slope'].median():.4f}")
        bullish = (df_v2_flat["nifty_trend_slope"] > 0).sum()
        print(f"    Bullish slope (>0) : {bullish}/{len(df_v2_flat)} ({bullish/len(df_v2_flat)*100:.1f}%)")

    if "nifty_rsi" in df_v2_flat.columns:
        df_v2_flat["nifty_rsi"] = pd.to_numeric(df_v2_flat["nifty_rsi"], errors="coerce")
        print(f"\n  Nifty RSI at signal time:")
        print(f"    Mean   : {df_v2_flat['nifty_rsi'].mean():.1f}")
        overbought = (df_v2_flat["nifty_rsi"] > 60).sum()
        print(f"    RSI>60 (momentum) : {overbought}/{len(df_v2_flat)} ({overbought/len(df_v2_flat)*100:.1f}%)")
else:
    print("  No signals_v2 data available.")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6: Scan Window Performance
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─"*80)
print("  SECTION 6: SCAN WINDOW BREAKDOWN")
print("─"*80)

if not df_sig.empty and "consol_window" in df_sig.columns:
    df_sig["consol_window"] = pd.to_numeric(df_sig["consol_window"], errors="coerce")
    window_stats = df_sig.groupby("consol_window")["pnl_pct"].agg(
        count="count", avg_pnl="mean", win_rate=lambda x: (x > 0).mean() * 100
    ).sort_values("avg_pnl", ascending=False)
    print(f"\n  {'Window':<8} {'Count':>6} {'Avg PnL':>9} {'WinRate':>9}")
    print("  " + "-"*36)
    for window, row in window_stats.iterrows():
        print(f"  {int(window):<8} {int(row['count']):>6} {row['avg_pnl']:>+8.2f}% {row['win_rate']:>8.1f}%")
else:
    # Try signal_id pattern to infer window
    if not df_sig.empty and "signal_id" in df_sig.columns:
        def extract_window(sid):
            try:
                parts = str(sid).split("_")
                return int(parts[-1])
            except:
                return None
        df_sig["inferred_window"] = df_sig["signal_id"].apply(extract_window)
        valid_windows = df_sig[df_sig["inferred_window"].isin([10, 20, 40, 80, 120])]
        if not valid_windows.empty:
            window_stats = valid_windows.groupby("inferred_window")["pnl_pct"].agg(
                count="count", avg_pnl="mean"
            ).sort_values("avg_pnl", ascending=False)
            print(f"\n  {'Window':<8} {'Count':>6} {'Avg PnL':>9}")
            for window, row in window_stats.iterrows():
                print(f"  {int(window):<8} {int(row['count']):>6} {row['avg_pnl']:>+8.2f}%")
        else:
            print("  consol_window field not available in signals — skipping.")
    else:
        print("  consol_window field not available — skipping.")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7: Active Positions Risk Snapshot
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─"*80)
print("  SECTION 7: ACTIVE POSITIONS RISK SNAPSHOT")
print("─"*80)

if not df_pos.empty and "status" in df_pos.columns:
    active_pos = df_pos[df_pos["status"].str.upper() == "ACTIVE"].copy()
    if not active_pos.empty:
        active_pos["pnl_pct"]   = pd.to_numeric(active_pos["pnl_pct"], errors="coerce").fillna(0)
        active_pos["days_held"] = pd.to_numeric(active_pos["days_held"], errors="coerce").fillna(0)
        if "stop_loss" in active_pos.columns and "current_price" in active_pos.columns:
            active_pos["stop_loss"]     = pd.to_numeric(active_pos["stop_loss"], errors="coerce")
            active_pos["current_price"] = pd.to_numeric(active_pos["current_price"], errors="coerce")
            active_pos["pct_to_stop"]   = ((active_pos["current_price"] - active_pos["stop_loss"]) / active_pos["current_price"] * 100)

        # Sort by PnL
        active_pos = active_pos.sort_values("pnl_pct", ascending=False)
        cols = ["symbol", "pnl_pct", "days_held"] + (["pct_to_stop"] if "pct_to_stop" in active_pos.columns else [])
        print(f"\n  {'Symbol':<15} {'PnL%':>8} {'Days':>6} {'%ToStop':>9}")
        print("  " + "-"*42)
        for _, row in active_pos[cols].iterrows():
            stop_str = f"{row.get('pct_to_stop', float('nan')):>+8.1f}%" if "pct_to_stop" in row and not pd.isna(row.get("pct_to_stop")) else "       N/A"
            print(f"  {row['symbol']:<15} {row['pnl_pct']:>+7.2f}% {int(row['days_held']):>5}d {stop_str}")
        print(f"\n  Positions in profit: {(active_pos['pnl_pct']>0).sum()} / {len(active_pos)}")
        print(f"  At-risk (<3% to stop): {(active_pos.get('pct_to_stop', pd.Series(dtype=float)) < 3).sum() if 'pct_to_stop' in active_pos.columns else 'N/A'}")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8: Winner Pattern Summary
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─"*80)
print("  SECTION 8: WINNER PATTERN CONCLUSION")
print("─"*80)

print("""
  Based on the data above, here are the key structural observations:

  [WHAT TO LOOK FOR IN WINNERS]
  • Higher volume_ratio at breakout  → sustained demand, not a 1-day spike
  • Tighter consolidation_range_pct  → <18% base = institutional accumulation
  • Bullish Nifty context (slope>0)  → market tailwind at signal time
  • Nifty RSI > 60                   → momentum market = breakouts sustain
  • Scan window 20–40 days           → best quality bases (not too short/too long)
  • Grade A/A+                       → higher structural quality threshold

  [WHAT TO AVOID]
  • Signals in UNKNOWN market regime (Nifty data missing) — skip or reduce size
  • Low volume_ratio (<1.2x)         → weak institutional conviction
  • Very wide bases (>40%)           → chop, not accumulation
  • Listing-day signals in downtrend → fade risk is very high

  [ACTIONABLE NEXT STEPS]
  • Run reconstruct_outcomes.py after 30 days to get objective max-runup data
  • Use backfill_v2_from_v1.py to fill missing sector/nifty context fields
  • Use outcome_analytics.py once we have ≥50 concluded trades for falsification
""")

print("="*80)
print("  Analysis complete.")
print("="*80 + "\n")
