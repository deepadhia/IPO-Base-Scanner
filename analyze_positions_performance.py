#!/usr/bin/env python3
"""
analyze_positions_performance.py
Analyzes active and closed positions from MongoDB, calculating win rate,
holding period, and exit reason distribution.
"""

import sys
import os
import pandas as pd
from datetime import datetime

# Force stdout/stderr to use UTF-8 on Windows to prevent UnicodeEncodeError for emojis
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if sys.stderr.encoding != 'utf-8':
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Set terminal color codes
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'

# Check if terminal supports color
if os.name == 'nt':
    # Enable ANSI escape characters on Windows
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

def main():
    try:
        from db import get_all_positions_df
    except ImportError:
        print("Error: Could not import db.py. Make sure you run this script from the project root.")
        sys.exit(1)
        
    print("=" * 80)
    print(f"{Colors.BOLD}{Colors.CYAN}🚀 IPO BREAKOUT SCANNER - PORTFOLIO PERFORMANCE SUMMARY{Colors.END}")
    print(f"Run Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST")
    print("=" * 80)
    
    df_pos = get_all_positions_df()
    if df_pos.empty:
        print("No positions found in MongoDB.")
        sys.exit(0)
        
    total_trades = len(df_pos)
    active_df = df_pos[df_pos["status"] == "ACTIVE"]
    closed_df = df_pos[df_pos["status"] == "CLOSED"]
    
    print(f"{Colors.BOLD}📊 Core Metrics:{Colors.END}")
    print(f"  • Total Trades Stored: {Colors.BOLD}{total_trades}{Colors.END}")
    print(f"  • Active Trades:      {Colors.BOLD}{Colors.BLUE}{len(active_df)}{Colors.END}")
    print(f"  • Closed Trades:      {Colors.BOLD}{Colors.YELLOW}{len(closed_df)}{Colors.END}")
    
    # ------------------ ACTIVE TRADES ------------------
    print("\n" + "-" * 80)
    print(f"{Colors.BOLD}{Colors.BLUE}📈 ACTIVE PORTFOLIO ANALYSIS ({len(active_df)} Trades){Colors.END}")
    print("-" * 80)
    
    if not active_df.empty:
        active_df = active_df.copy()
        active_df["pnl_pct"] = pd.to_numeric(active_df["pnl_pct"], errors='coerce').fillna(0.0)
        
        winners = active_df[active_df["pnl_pct"] > 0]
        losers = active_df[active_df["pnl_pct"] <= 0]
        win_rate = (len(winners) / len(active_df)) * 100
        avg_pnl = active_df["pnl_pct"].mean()
        
        print(f"  • Win Rate:      {Colors.BOLD}{Colors.GREEN if win_rate >= 50 else Colors.YELLOW}{win_rate:.1f}%{Colors.END} ({len(winners)} Win / {len(losers)} Loss)")
        print(f"  • Average PnL:   {Colors.BOLD}{Colors.GREEN if avg_pnl >= 0 else Colors.RED}{avg_pnl:+.2f}%{Colors.END}")
        print(f"  • Max Run:       {Colors.BOLD}{Colors.GREEN}+{active_df['pnl_pct'].max():.2f}%{Colors.END}")
        print(f"  • Min Drawdown:  {Colors.BOLD}{Colors.RED}{active_df['pnl_pct'].min():.2f}%{Colors.END}")
        
        print(f"\n{Colors.BOLD}{Colors.UNDERLINE}{'Symbol':<14} {'Entry Date':<12} {'Entry':<10} {'Current':<10} {'PnL %':<10} {'Held (Days)':<12} {'Grade':<8}{Colors.END}")
        
        active_sorted = active_df.sort_values(by="pnl_pct", ascending=False)
        for _, row in active_sorted.iterrows():
            pnl_val = row['pnl_pct']
            pnl_color = Colors.GREEN if pnl_val > 0 else (Colors.RED if pnl_val < 0 else '')
            pnl_str = f"{pnl_val:+.2f}%"
            
            entry_date_str = str(row['entry_date'])[:10]
            grade_str = row.get('grade', 'N/A')
            entry_price = float(row.get('entry_price', 0.0))
            current_price = float(row.get('current_price', entry_price))
            days_held = int(row.get('days_held', 0))
            
            print(f"{Colors.BOLD}{row['symbol']:<14}{Colors.END} {entry_date_str:<12} {entry_price:<10.2f} {current_price:<10.2f} {pnl_color}{pnl_str:<10}{Colors.END} {days_held:<12} {grade_str:<8}")
    else:
        print("No active trades currently.")
        
    # ------------------ CLOSED TRADES ------------------
    print("\n" + "-" * 80)
    print(f"{Colors.BOLD}{Colors.YELLOW}🚪 CLOSED PORTFOLIO ANALYSIS ({len(closed_df)} Trades){Colors.END}")
    print("-" * 80)
    
    if not closed_df.empty:
        closed_df = closed_df.copy()
        closed_df["pnl_pct"] = pd.to_numeric(closed_df["pnl_pct"], errors='coerce').fillna(0.0)
        
        winners_c = closed_df[closed_df["pnl_pct"] > 0]
        losers_c = closed_df[closed_df["pnl_pct"] <= 0]
        win_rate_c = (len(winners_c) / len(closed_df)) * 100
        avg_pnl_c = closed_df["pnl_pct"].mean()
        
        print(f"  • Realized Win Rate:  {Colors.BOLD}{Colors.GREEN if win_rate_c >= 40 else Colors.YELLOW}{win_rate_c:.1f}%{Colors.END} ({len(winners_c)} Win / {len(losers_c)} Loss)")
        print(f"  • Average Realized:   {Colors.BOLD}{Colors.GREEN if avg_pnl_c >= 0 else Colors.RED}{avg_pnl_c:+.2f}%{Colors.END}")
        
        if "exit_reason" in closed_df.columns:
            closed_df["exit_reason"] = closed_df["exit_reason"].fillna("Unknown / Historical")
            reason_counts = closed_df["exit_reason"].value_counts()
            print(f"\n{Colors.BOLD}🚪 Exit Reason Breakdown:{Colors.END}")
            for reason, count in reason_counts.items():
                print(f"  • {reason:<52} : {Colors.BOLD}{count}{Colors.END}")
    else:
        print("No closed trades recorded.")
        
    print("=" * 80)

if __name__ == "__main__":
    main()
