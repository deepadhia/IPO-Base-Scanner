"""
backtest_reentry_logic.py
Quantitative Backtest & Simulation Script: Testing Listing Day High Re-Entry Logic

Simulates 4 variations across historical IPO candle data from MongoDB:
  1. Baseline: Standard exits (SL, Speed Gates, Stagnant Guard, Volume Exhaustion Exit) - NO Re-Entry.
  2. Var A: Re-Entry within 5% of Listing Day High - NO Volume Filter.
  3. Var B: Re-Entry within 5% of Listing Day High - WITH 1.2x Volume Confirmation.
  4. Var C: Re-Entry within 5% of Listing Day High - WITH 1.5x Volume Confirmation.

Outputs a comparative performance table:
  - Total Trades & Re-Entry Count
  - Win Rate (%)
  - Avg Return per Trade (%)
  - Profit Factor (Gross Wins / Gross Losses)
  - Whipsaw Rate (%) (Trades failing within 3 days with PnL < -3%)
  - Total Strategy Return (%)
"""

import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

from db import db, listing_data_col, daily_candles_col


def load_all_ipo_candles():
    """Load cached daily candles for all IPO symbols in MongoDB."""
    if db is None or daily_candles_col is None:
        logger.error("MongoDB not connected!")
        return {}

    logger.info("Loading cached daily candles from MongoDB...")
    docs = list(daily_candles_col.find({}, {"_id": 0, "symbol": 1, "candles": 1, "listing_date": 1}))
    
    symbol_data = {}
    for doc in docs:
        sym = doc.get("symbol")
        candles = doc.get("candles")
        if not sym or not candles or len(candles) < 20:
            continue
        
        df = pd.DataFrame(candles)
        df["DATE"] = pd.to_datetime(df["DATE"])
        for col in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        
        df = df.sort_values("DATE").reset_index(drop=True)
        symbol_data[sym] = df

    logger.info(f"Loaded valid OHLCV candle data for {len(symbol_data)} symbols.")
    return symbol_data


def simulate_trade_pipeline(df, symbol, var_name, vol_mult_threshold=None, allow_reentry=False):
    """
    Simulates trades on a single symbol's OHLCV dataframe.
    """
    trades = []
    
    if len(df) < 25:
        return trades

    # Identify Listing High (Day 0 high or max of first 3 days)
    listing_high = df["HIGH"].iloc[:3].max()
    
    in_trade = False
    entry_price = 0.0
    entry_idx = 0
    entry_date = None
    stop_loss = 0.0
    max_runup = 0.0
    last_exit_idx = -100
    is_reentry_trade = False
    
    for i in range(5, len(df)):
        current_date = df["DATE"].iat[i]
        close = df["CLOSE"].iat[i]
        high = df["HIGH"].iat[i]
        low = df["LOW"].iat[i]
        vol = df["VOLUME"].iat[i]
        
        # Calculating 20-day rolling avg volume for volume filter
        avg_vol_20 = df["VOLUME"].iloc[max(0, i-20):i].mean() if i >= 20 else df["VOLUME"].iloc[:i].mean()
        
        if not in_trade:
            # Check for initial entry condition (Fresh breakout: Close > listing_high)
            is_initial_breakout = (close > listing_high) and (last_exit_idx == -100)
            
            # Check for re-entry condition (if enabled and previously exited within 45 days)
            is_reentry_candidate = False
            if allow_reentry and (last_exit_idx > 0) and ((i - last_exit_idx) <= 45) and ((i - last_exit_idx) >= 5):
                # Within 5% of listing high OR breaking above it
                price_in_reentry_zone = (close >= 0.95 * listing_high)
                
                # Volume condition check
                if vol_mult_threshold is not None:
                    vol_condition = (avg_vol_20 > 0) and (vol >= vol_mult_threshold * avg_vol_20)
                else:
                    vol_condition = True
                
                is_reentry_candidate = price_in_reentry_zone and vol_condition

            if is_initial_breakout or is_reentry_candidate:
                in_trade = True
                entry_price = close
                entry_idx = i
                entry_date = current_date
                stop_loss = entry_price * 0.90  # Standard 10% stop loss
                max_runup = 0.0
                is_reentry_trade = is_reentry_candidate
                continue

        if in_trade:
            days_held = (i - entry_idx)
            pnl_pct = (close - entry_price) / entry_price * 100.0
            current_runup = (high - entry_price) / entry_price * 100.0
            max_runup = max(max_runup, current_runup)
            
            exit_reason = None
            
            # 1. Stop Loss check
            if low <= stop_loss:
                exit_reason = "Stop Loss"
                exit_price = stop_loss
            
            # 2. Volume Exhaustion Exit check (Day 10+, PnL -3% to 5%, max_runup < 8%)
            elif (days_held >= 10) and (-3.0 <= pnl_pct < 5.0) and (max_runup < 8.0):
                post_entry_vols = df["VOLUME"].iloc[entry_idx+1:i+1]
                if len(post_entry_vols) >= 10:
                    baseline_vol = post_entry_vols.iloc[:7].mean()
                    recent_vol = post_entry_vols.iloc[-3:].mean()
                    if baseline_vol >= 50000:
                        vol_ratio = recent_vol / baseline_vol
                        if vol_ratio < 0.45:
                            exit_reason = "Volume Exhaustion"
                            exit_price = close

            # 3. Dead-Money Speed Gate (Day 20, max_runup < 4%)
            elif (days_held >= 20) and (max_runup < 4.0):
                exit_reason = "Speed Gate 20d"
                exit_price = close

            # 4. Stagnant Position Guard (Day 40, pnl < 10%)
            elif (days_held >= 40) and (pnl_pct < 10.0):
                exit_reason = "Stagnant Guard 40d"
                exit_price = close

            if exit_reason:
                final_pnl = (exit_price - entry_price) / entry_price * 100.0
                is_whipsaw = (days_held <= 3) and (final_pnl < -3.0)
                
                trades.append({
                    "symbol": symbol,
                    "variation": var_name,
                    "entry_date": entry_date,
                    "exit_date": current_date,
                    "days_held": days_held,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_pct": final_pnl,
                    "max_runup_pct": max_runup,
                    "exit_reason": exit_reason,
                    "is_reentry": is_reentry_trade,
                    "is_whipsaw": is_whipsaw,
                    "win": final_pnl > 0
                })
                
                in_trade = False
                last_exit_idx = i

    return trades


def run_comparative_backtest():
    """Run all 4 variations across all IPO datasets in MongoDB and print performance report."""
    symbol_data = load_all_ipo_candles()
    if not symbol_data:
        print("No candle data available to backtest.")
        return

    variations = [
        {"name": "Baseline (No Re-Entry)", "allow_reentry": False, "vol_mult": None},
        {"name": "Var A: Re-Entry (No Vol Filter)", "allow_reentry": True, "vol_mult": None},
        {"name": "Var B: Re-Entry (1.2x Vol Filter)", "allow_reentry": True, "vol_mult": 1.2},
        {"name": "Var C: Re-Entry (1.5x Vol Filter)", "allow_reentry": True, "vol_mult": 1.5},
    ]

    results_summary = []

    for var in variations:
        all_trades = []
        for sym, df in symbol_data.items():
            t_list = simulate_trade_pipeline(
                df, sym, var["name"], vol_mult_threshold=var["vol_mult"], allow_reentry=var["allow_reentry"]
            )
            all_trades.extend(t_list)

        if not all_trades:
            continue

        tdf = pd.DataFrame(all_trades)
        total_trades = len(tdf)
        reentry_trades = tdf["is_reentry"].sum()
        wins = tdf["win"].sum()
        win_rate = (wins / total_trades) * 100.0 if total_trades > 0 else 0
        
        avg_return = tdf["pnl_pct"].mean()
        total_return = tdf["pnl_pct"].sum()
        
        gross_wins = tdf[tdf["pnl_pct"] > 0]["pnl_pct"].sum()
        gross_losses = abs(tdf[tdf["pnl_pct"] < 0]["pnl_pct"].sum())
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else np.nan
        
        whipsaws = tdf["is_whipsaw"].sum()
        whipsaw_rate = (whipsaws / total_trades) * 100.0 if total_trades > 0 else 0
        
        results_summary.append({
            "Variation": var["name"],
            "Total Trades": total_trades,
            "Re-Entries": int(reentry_trades),
            "Win Rate (%)": round(win_rate, 2),
            "Avg Return (%)": round(avg_return, 2),
            "Profit Factor": round(profit_factor, 2) if not np.isnan(profit_factor) else "Inf",
            "Whipsaw Rate (%)": round(whipsaw_rate, 2),
            "Total Return (%)": round(total_return, 2)
        })

    summary_df = pd.DataFrame(results_summary)
    
    print("\n" + "=" * 100)
    print("      QUANTITATIVE BACKTEST REPORT: LISTING DAY HIGH RE-ENTRY LOGIC")
    print("=" * 100)
    print(summary_df.to_string(index=False))
    print("=" * 100 + "\n")

    return summary_df


if __name__ == "__main__":
    run_comparative_backtest()
