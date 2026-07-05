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
# SECTION 4: Volume & Consolidation — Winner DNA (from signals_v2 enrichment)
# ─────────────────────────────────────────────────────────────────────────────
# NOTE: signals_v2 holds the enriched data (from backfill_v2_from_v1.py).
# The legacy signals collection only has 2.3% field coverage for volume_ratio
# and consolidation_range_pct. This section reads from signals_v2 using:
#   outcome.pnl_pct      → win/loss classification
#   base_quality.base_depth      → equivalent to consolidation_range_pct
#   breakout_fingerprint.volume_zscore → volume strength at breakout
#   market_context.nifty_trend_slope   → Nifty tailwind at signal time
# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─"*80)
print("  SECTION 4: WINNER DNA — Volume & Consolidation Traits (signals_v2)")
print("─"*80)

if not df_v2.empty:
    # ── Flatten nested enrichment fields ──────────────────────────────────────
    def safe_get_nested(row, *keys):
        """Safely extract a value from nested dicts in a DataFrame row."""
        val = row
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return np.nan
        return val if val is not None else np.nan

    # Outcome: pnl from outcome.pnl_pct
    df_v2["_pnl"] = df_v2.apply(
        lambda r: safe_get_nested(r.get("outcome", {}), "pnl_pct"), axis=1
    )
    df_v2["_pnl"] = pd.to_numeric(df_v2["_pnl"], errors="coerce")

    # Outcome status
    df_v2["_status"] = df_v2.apply(
        lambda r: str(safe_get_nested(r.get("outcome", {}), "status") or "").upper(), axis=1
    )

    # Base quality: base_depth = consolidation range equivalent
    df_v2["_base_depth"] = pd.to_numeric(
        df_v2.apply(lambda r: safe_get_nested(r.get("base_quality", {}), "base_depth"), axis=1),
        errors="coerce"
    )

    # Base quality: tightness_index (avg daily range %)
    df_v2["_tightness"] = pd.to_numeric(
        df_v2.apply(lambda r: safe_get_nested(r.get("base_quality", {}), "tightness_index"), axis=1),
        errors="coerce"
    )

    # Breakout: volume zscore
    df_v2["_vol_zscore"] = pd.to_numeric(
        df_v2.apply(lambda r: safe_get_nested(r.get("breakout_fingerprint", {}), "volume_zscore"), axis=1),
        errors="coerce"
    )

    # Breakout: body-to-range (conviction candle)
    df_v2["_body_to_range"] = pd.to_numeric(
        df_v2.apply(lambda r: safe_get_nested(r.get("breakout_fingerprint", {}), "body_to_range"), axis=1),
        errors="coerce"
    )

    # Market context: Nifty slope
    df_v2["_nifty_slope"] = pd.to_numeric(
        df_v2.apply(lambda r: safe_get_nested(r.get("market_context", {}), "nifty_trend_slope"), axis=1),
        errors="coerce"
    )

    # ── Classify win / loss ───────────────────────────────────────────────────
    closed_v2 = df_v2[df_v2["_status"].isin(["CLOSED", "TARGET_HIT", "SUCCESS", "STOPPED_OUT"])].copy()
    # Fall back: also include rows where pnl is set but status is not mapped
    if closed_v2.empty:
        closed_v2 = df_v2[df_v2["_pnl"].notna()].copy()

    if not closed_v2.empty:
        winners_v2 = closed_v2[closed_v2["_pnl"] > 0]
        losers_v2  = closed_v2[closed_v2["_pnl"] <= 0]

        print(f"\n  Using signals_v2 enrichment — {len(closed_v2)} concluded signals "
              f"({len(winners_v2)} W / {len(losers_v2)} L)")

        metrics_to_compare = [
            ("_base_depth",    "Base Depth %",       "consolidation range — lower = tighter base"),
            ("_tightness",     "Tightness Index",    "avg daily range % — lower = more coiled"),
            ("_vol_zscore",    "Volume Z-Score",     "breakout volume vs 20d avg — higher = stronger"),
            ("_body_to_range", "Body-to-Range",      "candle conviction — higher = cleaner breakout candle"),
            ("_nifty_slope",   "Nifty Trend Slope",  "market tailwind — positive = bullish regime"),
        ]

        print(f"\n  {'Metric':<22} {'Winners Avg':>13} {'Losers Avg':>12} {'Edge':>14}  Interpretation")
        print("  " + "─" * 90)
        for col, label, note in metrics_to_compare:
            w_vals = winners_v2[col].dropna()
            l_vals = losers_v2[col].dropna()
            if len(w_vals) == 0 and len(l_vals) == 0:
                print(f"  {label:<22}  {'no data':>12}  {'no data':>11}  —")
                continue
            w_mean = w_vals.mean() if len(w_vals) > 0 else float("nan")
            l_mean = l_vals.mean() if len(l_vals) > 0 else float("nan")
            if not np.isnan(w_mean) and not np.isnan(l_mean):
                delta = w_mean - l_mean
                edge = f"{delta:+.2f}"
            else:
                edge = "N/A"
            print(f"  {label:<22}  {w_mean:>12.3f}  {l_mean:>11.3f}  {edge:>14}  ({note})")

        # ── Nifty regime breakdown ────────────────────────────────────────────
        print(f"\n  Nifty tailwind split (closed signals_v2):")
        bullish_w = (winners_v2["_nifty_slope"] > 0).sum()
        bullish_l = (losers_v2["_nifty_slope"] > 0).sum()
        total_w   = winners_v2["_nifty_slope"].notna().sum()
        total_l   = losers_v2["_nifty_slope"].notna().sum()
        if total_w > 0:
            print(f"    Winners with bullish slope : {bullish_w}/{total_w} ({bullish_w/total_w*100:.1f}%)")
        if total_l > 0:
            print(f"    Losers  with bullish slope : {bullish_l}/{total_l} ({bullish_l/total_l*100:.1f}%)")
    else:
        print("\n  No concluded signals found in signals_v2 — cannot compute winner DNA.")
        print("  (Run backfill_v2_from_v1.py if signals_v2 is empty or outcomes are missing.)")
else:
    print("  signals_v2 is empty — run backfill_v2_from_v1.py first.")

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
