from db import signals_col, positions_col
import pandas as pd
import numpy as np

# Load all signals
all_sigs = list(signals_col.find({}, {"_id": 0}))
df = pd.DataFrame(all_sigs)

if df.empty:
    print("No signals found!")
    exit(1)

# Ensure numeric types
df["pnl_pct"] = pd.to_numeric(df.get("pnl_pct", 0), errors="coerce").fillna(0)
df["volume_ratio"] = pd.to_numeric(df.get("volume_ratio", np.nan), errors="coerce")
df["consolidation_range_pct"] = pd.to_numeric(df.get("consolidation_range_pct", np.nan), errors="coerce")
df["market_cap_cr"] = pd.to_numeric(df.get("market_cap_cr", np.nan), errors="coerce")
df["entry_price"] = pd.to_numeric(df.get("entry_price", np.nan), errors="coerce")
df["score"] = pd.to_numeric(df.get("score", np.nan), errors="coerce")

# Filter for Listing Day / IPO Discovery signals
# These are tagged with grade == "LISTING_BREAKOUT" or signal_type == "LISTING_DAY_BREAKOUT" or scanner == "listing_day"
df_list = df[
    (df["grade"] == "LISTING_BREAKOUT") | 
    (df["signal_type"] == "LISTING_DAY_BREAKOUT") | 
    (df["scanner"] == "listing_day")
].copy()

print(f"Total Listing Day Breakout Signals: {len(df_list)}")

# Inspect OMNI specifically
df_omni = df[df["symbol"] == "OMNI"]
if not df_omni.empty:
    print("\n=== OMNI SIGNAL PROFILE ===")
    for idx, row in df_omni.iterrows():
        print(f"Symbol: {row.get('symbol')} | PnL: {row.get('pnl_pct'):+.2f}% | Status: {row.get('status')}")
        print(f"  Signal Date : {row.get('signal_date')} | Time: {row.get('signal_time')}")
        print(f"  Entry Price : {row.get('entry_price')} | Stop Loss: {row.get('stop_loss')} | Target: {row.get('target_price')}")
        print(f"  Vol Ratio   : {row.get('volume_ratio')} | Base Range: {row.get('consolidation_range_pct')}%")
        print(f"  Market Cap  : {row.get('market_cap_cr')} Cr | Nifty Slope: {row.get('nifty_trend_slope')}")
        print(f"  Regime      : {row.get('market_regime')} | Score: {row.get('score')} | Leader Score: {row.get('leader_score')}")
        print(f"  Notes       : {row.get('notes') or row.get('entry_note')}")
else:
    print("\n[Warn] OMNI not found in signals collection.")

# Let's segregate Listing Day breakouts into:
# 1. Super Winners (PnL > 25%)
# 2. Average/Losing setups (PnL <= 0% and CLOSED)
super_winners = df_list[df_list["pnl_pct"] > 25.0].copy()
losers = df_list[(df_list["pnl_pct"] <= 0.0) & (df_list["status"] == "CLOSED")].copy()

print(f"\n=== COMPARATIVE STUDY ===")
print(f"Super Winners (PnL > 25%) : {len(super_winners)}")
print(f"Concluded Losers (PnL <= 0%) : {len(losers)}")

def print_metric_comparison(col_name, label):
    if col_name in df_list.columns:
        w_vals = super_winners[col_name].dropna()
        l_vals = losers[col_name].dropna()
        
        w_mean = w_vals.mean() if not w_vals.empty else np.nan
        l_mean = l_vals.mean() if not l_vals.empty else np.nan
        
        w_str = f"{w_mean:.4f}" if isinstance(w_mean, (float, int)) and not pd.isna(w_mean) else str(w_mean)
        l_str = f"{l_mean:.4f}" if isinstance(l_mean, (float, int)) and not pd.isna(l_mean) else str(l_mean)
        
        print(f"\n* {label} ({col_name}):")
        print(f"  - Super Winners Mean : {w_str}")
        print(f"  - Losers Mean        : {l_str}")
        if not w_vals.empty and not l_vals.empty and not pd.isna(w_mean) and not pd.isna(l_mean):
            diff = w_mean - l_mean
            sign = "higher" if diff > 0 else "lower"
            print(f"  → Difference: {abs(diff):.4f} ({sign} in Super Winners)")

print_metric_comparison("volume_ratio", "Volume Ratio at Breakout")
print_metric_comparison("consolidation_range_pct", "Consolidation Base Range %")
print_metric_comparison("market_cap_cr", "Market Capitalization (Cr)")
print_metric_comparison("score", "Scanner Match Score (out of 5)")
print_metric_comparison("leader_score", "Listing Day Leader Score")

# Let's inspect Nifty Trend Slope & Regime distributions
if "market_regime" in df_list.columns:
    print("\n* Market Regime Distribution:")
    print("  Super Winners:")
    w_reg = super_winners["market_regime"].value_counts(normalize=True) * 100
    for reg, pct in w_reg.items():
        print(f"    - {reg:<12} : {pct:.1f}% ({super_winners['market_regime'].value_counts()[reg]} trades)")
    
    print("  Losers:")
    l_reg = losers["market_regime"].value_counts(normalize=True) * 100
    for reg, pct in l_reg.items():
        print(f"    - {reg:<12} : {pct:.1f}% ({losers['market_regime'].value_counts()[reg]} trades)")

if "nifty_trend_slope" in df_list.columns:
    df_list["nifty_trend_slope"] = pd.to_numeric(df_list["nifty_trend_slope"], errors="coerce")
    super_winners["nifty_trend_slope"] = pd.to_numeric(super_winners["nifty_trend_slope"], errors="coerce")
    losers["nifty_trend_slope"] = pd.to_numeric(losers["nifty_trend_slope"], errors="coerce")
    
    print("\n* Nifty Trend Slope at Breakout:")
    print(f"  - Super Winners Mean : {super_winners['nifty_trend_slope'].mean():.6f}")
    print(f"  - Losers Mean        : {losers['nifty_trend_slope'].mean():.6f}")

# Sample study of specific columns in Super Winners to see if we can find any absolute invariants
print("\n=== TOP 10 INDIVIDUAL SUPER WINNERS DETAILS ===")
cols_to_show = ["symbol", "pnl_pct", "signal_date", "market_regime", "market_cap_cr", "volume_ratio", "leader_score"]
existing_cols = [c for c in cols_to_show if c in df_list.columns]
print(super_winners.sort_values("pnl_pct", ascending=False)[existing_cols].head(15).to_string(index=False))
