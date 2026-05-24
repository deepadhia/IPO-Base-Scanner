from db import signals_col
import pandas as pd

sigs = list(signals_col.find({}, {"_id": 0}))
df = pd.DataFrame(sigs)

# Ensure numeric types
df["pnl_pct"] = pd.to_numeric(df.get("pnl_pct", 0), errors="coerce").fillna(0)

# Filter for Listing Day / IPO Discovery signals
df_list = df[
    (df["grade"] == "LISTING_BREAKOUT") | 
    (df["signal_type"] == "LISTING_DAY_BREAKOUT") | 
    (df["scanner"] == "listing_day")
].copy()

super_winners = df_list[df_list["pnl_pct"] > 25.0].copy()

print(f"Total Super Winners: {len(super_winners)}")

# Sort by PnL
super_winners = super_winners.sort_values("pnl_pct", ascending=False)

# Let's inspect fields that are populated (not all null)
print("\n=== POPULATED FIELDS FOR SUPER WINNERS ===")
for col in super_winners.columns:
    non_null_count = super_winners[col].notna().sum()
    if non_null_count > 0:
        # Get a few sample values
        samples = super_winners[col].dropna().head(3).tolist()
        print(f"Field: {col:<30} | Non-Null: {non_null_count}/{len(super_winners)} | Samples: {samples}")

print("\n=== COMPLETE LIST OF 64 SUPER WINNERS ===")
cols_to_print = ["symbol", "pnl_pct", "signal_date", "market_regime", "status", "exit_reason"]
print(super_winners[cols_to_print].to_string(index=False))
