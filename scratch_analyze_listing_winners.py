from db import signals_col
import pandas as pd
import numpy as np

# Load all signals
all_sigs = list(signals_col.find({"grade": "LISTING_BREAKOUT"}, {"_id": 0}))
df = pd.DataFrame(all_sigs)

if df.empty:
    print("No LISTING_BREAKOUT signals found!")
    exit(1)

# Enforce proper types
df["pnl_pct"] = pd.to_numeric(df.get("pnl_pct", 0), errors="coerce").fillna(0)
df["entry_price"] = pd.to_numeric(df.get("entry_price", np.nan), errors="coerce")
df["w"] = df["metrics"].apply(lambda m: float(m.get("w", np.nan)) if isinstance(m, dict) else np.nan)
df["market_regime"] = df["market_regime"].fillna("UNKNOWN")

closed_df = df[df["status"] == "CLOSED"].copy()
super_winners = closed_df[closed_df["pnl_pct"] >= 20.0].copy()

print(f"Total Closed Signals: {len(closed_df)}")
print(f"Total Super Winners: {len(super_winners)}")

# 1. Test Filter: Market Regime in BULL / CORRECTION
print("\n=== FILTER TEST 1: Market Regime in [BULL, CORRECTION] ===")
f1 = closed_df[closed_df["market_regime"].isin(["BULL", "CORRECTION"])]
w1 = f1[f1["pnl_pct"] > 0]
sw1 = f1[f1["pnl_pct"] >= 20.0]
print(f"Trades Taken : {len(f1)} / {len(closed_df)} ({len(f1)/len(closed_df)*100:.1f}%)")
print(f"Win Rate     : {len(w1)/len(f1)*100:.1f}%")
print(f"Avg PnL      : {f1['pnl_pct'].mean():+.2f}%")
print(f"Super Winners: {len(sw1)} / {len(super_winners)} ({len(sw1)/len(super_winners)*100:.1f}%)")

# 2. Test Filter: Entry Price >= 150
print("\n=== FILTER TEST 2: Nominal Entry Price >= 150 ===")
f2 = closed_df[closed_df["entry_price"] >= 150.0]
w2 = f2[f2["pnl_pct"] > 0]
sw2 = f2[f2["pnl_pct"] >= 20.0]
print(f"Trades Taken : {len(f2)} / {len(closed_df)} ({len(f2)/len(closed_df)*100:.1f}%)")
print(f"Win Rate     : {len(w2)/len(f2)*100:.1f}%")
print(f"Avg PnL      : {f2['pnl_pct'].mean():+.2f}%")
print(f"Super Winners: {len(sw2)} / {len(super_winners)} ({len(sw2)/len(super_winners)*100:.1f}%)")

# 3. Test Filter: Breakout Age w <= 15
print("\n=== FILTER TEST 3: Breakout Age (w) <= 15 days ===")
f3 = closed_df[closed_df["w"] <= 15.0]
w3 = f3[f3["pnl_pct"] > 0]
sw3 = f3[f3["pnl_pct"] >= 20.0]
print(f"Trades Taken : {len(f3)} / {len(closed_df)} ({len(f3)/len(closed_df)*100:.1f}%)")
print(f"Win Rate     : {len(w3)/len(f3)*100:.1f}%")
print(f"Avg PnL      : {f3['pnl_pct'].mean():+.2f}%")
print(f"Super Winners: {len(sw3)} / {len(super_winners)} ({len(sw3)/len(super_winners)*100:.1f}%)")

# 4. Joint Filter: Regime [BULL, CORRECTION] AND Entry Price >= 150 AND Age <= 15
print("\n=== JOINT FILTER: REGIME + PRICE >= 150 + AGE <= 15 ===")
f4 = closed_df[
    (closed_df["market_regime"].isin(["BULL", "CORRECTION"])) &
    (closed_df["entry_price"] >= 150.0) &
    (closed_df["w"] <= 15.0)
]
w4 = f4[f4["pnl_pct"] > 0]
sw4 = f4[f4["pnl_pct"] >= 20.0]
print(f"Trades Taken : {len(f4)} / {len(closed_df)} ({len(f4)/len(closed_df)*100:.1f}%)")
print(f"Win Rate     : {len(w4)/len(f4)*100:.1f}%")
print(f"Avg PnL      : {f4['pnl_pct'].mean():+.2f}%")
print(f"Super Winners: {len(sw4)} / {len(super_winners)} ({len(sw4)/len(super_winners)*100:.1f}%)")

# 5. Joint Filter with wider 12% Stop Loss simulation
# Since we didn't change the database records, how would a 12% SL look under this joint filter?
# Let's write a simple simulation: if original pnl_pct < 0, let's see if widening the SL would have saved it.
# Actually, the 12% simulation from before was already run. Let's see what happens if we just use the joint filter on the 12% SL simulation results!
# Let's inspect the results we have.
