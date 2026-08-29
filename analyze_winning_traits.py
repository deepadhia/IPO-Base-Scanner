import os
import pandas as pd
import numpy as np
from pymongo import MongoClient
from dotenv import load_dotenv
from tabulate import tabulate
from datetime import datetime

load_dotenv()

def analyze_institutional_expectancy():
    """
    V2.5.0 Institutional Expectancy Analysis:
    Generates a Regime-Pattern matrix to identify high-probability alpha archetypes.
    """
    print("\n" + "="*80)
    print("           INSTITUTIONAL EXPECTANCY MATRIX (v2.5.0)")
    print("="*80)

    # 1. Load Data
    client = MongoClient(os.getenv("MONGO_URI"))
    db = client['ipo_scanner_v2']
    raw_signals = list(db.signals.find()) # Main collection enriched by backfill

    # Enforce clean-cohort filter (July 5, 2026 parameter tightening)
    CLEAN_COHORT_START = "2026-07-05"
    signals = [s for s in raw_signals if str(s.get("signal_date") or s.get("created_at") or "")[:10] >= CLEAN_COHORT_START]

    if not signals:
        print("X No clean-cohort signals found in MongoDB collection 'signals'.")
        return

    df = pd.DataFrame(signals)
    total_records = len(df)
    
    # 2. Cleanup & Processing
    # Handle status: SUCCESS/FAILURE/STOPPED_OUT/FAILED
    def derive_result(row):
        # We look at 'status' or PnL if status is not final
        s = str(row.get('status', '')).upper()
        pnl = float(row.get('pnl_pct', 0))
        
        if s in ['SUCCESS', 'TARGET_HIT']: return 1 # Win
        if s in ['STOPPED_OUT', 'FAILED', 'FAILURE']: return 0 # Loss
        
        # If status is ambiguous but we have an exit_price
        if row.get('exit_price', 0) > 0:
            return 1 if pnl > 0 else 0
        return None # Pending/Active

    df['is_win'] = df.apply(derive_result, axis=1)
    df['pnl_pct'] = pd.to_numeric(df['pnl_pct'], errors='coerce').fillna(0)
    
    # Ensure metadata fields exist
    if 'market_regime' not in df.columns:
        df['market_regime'] = 'UNKNOWN'
    else:
        df['market_regime'] = df['market_regime'].fillna('UNKNOWN')

    if 'pattern_type' not in df.columns:
        df['pattern_type'] = df.get('scanner', df.get('grade', 'LISTING_BREAKOUT'))
    else:
        df['pattern_type'] = df['pattern_type'].fillna('LISTING_BREAKOUT')

    if 'grade' not in df.columns:
        df['grade'] = 'N/A'
    else:
        df['grade'] = df['grade'].fillna('N/A')

    # Filter for closed/concluded trades for expectancy
    concluded = df[df['is_win'].notna()].copy()
    
    print(f"Total Signals:    {total_records}")
    print(f"Concluded Trades: {len(concluded)}")
    print(f"Active/Pending:   {total_records - len(concluded)}")

    if concluded.empty:
        print("\n[Warning] No concluded trades available for expectancy calculation.")
        return

    # 3. Regime-Pattern Expectancy Matrix
    print("\n[SECTION 1: REGIME-PATTERN WIN RATE MATRIX]")
    
    matrix_win_rate = pd.pivot_table(
        concluded, 
        values='is_win', 
        index='pattern_type', 
        columns='market_regime', 
        aggfunc='mean'
    ).fillna(0) * 100

    print(tabulate(matrix_win_rate, headers='keys', tablefmt='simple', floatfmt=".1f"))

    # 4. Net Expectancy Matrix (Avg P&L per Trade)
    print("\n[SECTION 2: EXPECTANCY MATRIX (Avg P&L % per Trade)]")
    
    matrix_expectancy = pd.pivot_table(
        concluded, 
        values='pnl_pct', 
        index='pattern_type', 
        columns='market_regime', 
        aggfunc='mean'
    ).fillna(0)

    print(tabulate(matrix_expectancy, headers='keys', tablefmt='simple', floatfmt=".2f"))

    # 5. Volume/Sample Size Check
    print("\n[SECTION 3: SAMPLE SIZE (Sample count per cell)]")
    matrix_counts = pd.pivot_table(
        concluded, 
        values='symbol', 
        index='pattern_type', 
        columns='market_regime', 
        aggfunc='count'
    ).fillna(0)
    print(tabulate(matrix_counts, headers='keys', tablefmt='simple', floatfmt=".0f"))

    # 6. Deep Insights
    print("\n" + "="*80)
    print("           FORENSIC INSIGHTS")
    print("="*80)

    # Best performing combination
    flat_expectancy = matrix_expectancy.unstack().sort_values(ascending=False)
    best_regime, best_pattern = flat_expectancy.index[0]
    best_val = flat_expectancy.iloc[0]
    
    # Worst performing combination
    worst_regime, worst_pattern = flat_expectancy.index[-1]
    worst_val = flat_expectancy.iloc[-1]

    print(f"[*] TOP ALPHA:  {best_pattern} in {best_regime} ({best_val:+.2f}% avg)")
    print(f"[!] DEAD ZONE:  {worst_pattern} in {worst_regime} ({worst_val:+.2f}% avg)")
    
    # Grade Analysis
    grade_perf = concluded.groupby('grade')['pnl_pct'].agg(['mean', 'count']).sort_values('mean', ascending=False)
    print("\n[GRADE PERFORMANCE]")
    print(tabulate(grade_perf, headers=['Grade', 'Avg P&L %', 'Count'], tablefmt='simple', floatfmt=".2f"))

    # Summary Conclusion
    overall_win_rate = concluded['is_win'].mean() * 100
    overall_expectancy = concluded['pnl_pct'].mean()
    
    print("\n" + "-"*40)
    print(f"OVERALL WIN RATE:  {overall_win_rate:.1f}%")
    print(f"OVERALL EXPECTANCY: {overall_expectancy:+.2f}% per trade")
    print("-"*40)
    
    if overall_expectancy < 0:
        print("[-] CAUTION: System-wide expectancy is currently negative. Tighten filters!")
    elif overall_expectancy > 5:
        print("[+] STRONG: System-wide expectancy is robust. Consider scaling!")
    else:
        print("[/] NEUTRAL: System is hovering near breakeven. Pattern pruning required.")

if __name__ == "__main__":
    # Ensure tabulate is installed or use fallback
    try:
        analyze_institutional_expectancy()
    except ImportError:
        print("\n[Error] 'tabulate' package not found. Please run: pip install tabulate")
    except Exception as e:
        print(f"\n[Error] Analysis failed: {e}")
