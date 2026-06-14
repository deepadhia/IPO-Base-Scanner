#!/usr/bin/env python3
"""
Verify Signals Performance - Run after 1 month to analyze signal accuracy

This script analyzes all signals and positions to:
1. Check if entry prices matched live prices at signal time
2. Verify if stop loss/target were hit correctly
3. Calculate actual P&L vs expected
4. Generate performance report
5. Identify any issues with stale data or incorrect prices
"""

import pandas as pd
import sys
import os
from datetime import datetime, timedelta
import json

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

def safe_float(v, default=0.0):
    import math
    if v is None or pd.isna(v) or (isinstance(v, float) and math.isnan(v)):
        return default
    return float(v)

def safe_int(v, default=0):
    import math
    if v is None or pd.isna(v) or (isinstance(v, float) and math.isnan(v)):
        return default
    return int(v)

def safe_str(v, default=""):
    if v is None or pd.isna(v):
        return default
    return str(v)

# Import from main scanner
import importlib.util
spec = importlib.util.spec_from_file_location("scanner", "streamlined_ipo_scanner.py")
scanner_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner_module)

get_live_price = scanner_module.get_live_price
logger = scanner_module.logger

# Metadata & State (Legacy CSVs removed, using MongoDB)
from db import get_all_signals_df, get_all_positions_df
REPORT_FILE = "signals_performance_report.json"

def analyze_signals(version=None):
    """Analyze all signals for performance"""
    print("📊 Analyzing Signals Performance...")
    print("=" * 80)
    
    # Load signals from MongoDB
    signals_df = get_all_signals_df()
    if signals_df.empty:
        print("❌ No signals found!")
        return None
        
    if version:
        if 'strategy_version' in signals_df.columns:
            # Handle float/string comparison safely
            signals_df = signals_df[signals_df['strategy_version'].astype(str) == str(version)]
        elif 'version' in signals_df.columns:
            signals_df = signals_df[signals_df['version'].astype(str) == str(version)]
    
    print(f"\n📈 Found {len(signals_df)} signals to analyze\n")
    
    results = {
        'total_signals': len(signals_df),
        'active_signals': 0,
        'closed_signals': 0,
        'signals_analysis': [],
        'summary': {
            'total_pnl': 0,
            'winning_signals': 0,
            'losing_signals': 0,
            'avg_pnl': 0,
            'max_profit': 0,
            'max_loss': 0
        }
    }
    
    for idx, signal in signals_df.iterrows():
        symbol = signal['symbol']
        entry_price = signal['entry_price']
        stop_loss = signal['stop_loss']
        target_price = signal['target_price']
        signal_date = pd.to_datetime(signal['signal_date']).date()
        status = signal.get('status', 'ACTIVE')
        exit_price = signal.get('exit_price', 0)
        exit_date = signal.get('exit_date', '')
        pnl_pct = signal.get('pnl_pct', 0)
        days_held = signal.get('days_held', 0)
        grade = signal.get('grade', 'N/A')
        signal_type = signal.get('signal_type', 'UNKNOWN')
        
        today = datetime.today().date()
        days_since_signal = (today - signal_date).days
        
        print(f"\n{'='*80}")
        print(f"📊 Signal: {symbol} ({signal_type})")
        print(f"   Grade: {grade} | Status: {status}")
        print(f"   Signal Date: {signal_date} ({days_since_signal} days ago)")
        print(f"   Entry: ₹{entry_price:.2f} | Stop: ₹{stop_loss:.2f} | Target: ₹{target_price:.2f}")
        
        # Get current live price
        current_price = None
        price_source = "Unknown"
        try:
            current_price, price_source, _ = get_live_price(symbol)
            if current_price:
                print(f"   Current Price: ₹{current_price:.2f} ({price_source})")
        except Exception as e:
            print(f"   ⚠️ Could not fetch current price: {e}")
        
        signal_analysis = {
            'symbol': symbol,
            'signal_date': safe_str(signal_date),
            'signal_type': safe_str(signal_type),
            'grade': safe_str(grade),
            'status': safe_str(status),
            'entry_price': safe_float(entry_price),
            'stop_loss': safe_float(stop_loss),
            'target_price': safe_float(target_price),
            'days_since_signal': days_since_signal,
            'current_price': safe_float(current_price) if current_price else None,
            'price_source': safe_str(price_source),
            'exit_price': safe_float(exit_price),
            'exit_date': safe_str(exit_date),
            'pnl_pct': safe_float(pnl_pct),
            'days_held': safe_int(days_held),
            'issues': []
        }
        
        # Analyze performance
        if status == 'CLOSED' and exit_price > 0:
            results['closed_signals'] += 1
            actual_pnl = ((exit_price - entry_price) / entry_price) * 100
            signal_analysis['actual_pnl'] = actual_pnl
            signal_analysis['expected_pnl'] = float(pnl_pct)
            
            # Check if stop loss or target was hit
            if exit_price <= stop_loss * 1.01:  # Allow 1% tolerance
                signal_analysis['exit_reason'] = 'Stop Loss Hit'
                print(f"   🛑 Exit: Stop Loss Hit at ₹{exit_price:.2f}")
            elif exit_price >= target_price * 0.99:  # Allow 1% tolerance
                signal_analysis['exit_reason'] = 'Target Hit'
                print(f"   🎯 Exit: Target Hit at ₹{exit_price:.2f}")
            else:
                signal_analysis['exit_reason'] = 'Manual/Other'
                print(f"   📊 Exit: ₹{exit_price:.2f} (Manual/Other)")
            
            print(f"   P&L: {actual_pnl:.2f}% (Expected: {pnl_pct:.2f}%)")
            
            if actual_pnl > 0:
                results['summary']['winning_signals'] += 1
            else:
                results['summary']['losing_signals'] += 1
            
            results['summary']['total_pnl'] += actual_pnl
            results['summary']['max_profit'] = max(results['summary']['max_profit'], actual_pnl)
            results['summary']['max_loss'] = min(results['summary']['max_loss'], actual_pnl)
            
        else:
            results['active_signals'] += 1
            if current_price:
                current_pnl = ((current_price - entry_price) / entry_price) * 100
                signal_analysis['current_pnl'] = current_pnl
                print(f"   📈 Current P&L: {current_pnl:.2f}%")
                
                # Check if stop loss or target would be hit
                if current_price <= stop_loss * 1.01:
                    signal_analysis['status_check'] = 'Near Stop Loss'
                    print(f"   ⚠️ Near Stop Loss!")
                elif current_price >= target_price * 0.99:
                    signal_analysis['status_check'] = 'Near Target'
                    print(f"   🎯 Near Target!")
                else:
                    signal_analysis['status_check'] = 'In Range'
        
        # Check for issues
        if current_price:
            price_diff = abs(current_price - entry_price)
            price_diff_pct = (price_diff / entry_price * 100) if entry_price > 0 else 0
            
            # If signal is old and price difference is significant, flag it
            if days_since_signal > 7 and price_diff_pct > 10:
                signal_analysis['issues'].append(f'Large price difference ({price_diff_pct:.1f}%) after {days_since_signal} days')
                print(f"   ⚠️ ISSUE: Large price difference: {price_diff_pct:.1f}%")
        
        results['signals_analysis'].append(signal_analysis)
    
    # Calculate averages
    if results['closed_signals'] > 0:
        results['summary']['avg_pnl'] = results['summary']['total_pnl'] / results['closed_signals']
    
    return results

def analyze_positions(version=None):
    """Analyze all positions for performance"""
    print("\n\n" + "=" * 80)
    print("📊 Analyzing Positions Performance...")
    print("=" * 80)
    
    # Load positions from MongoDB
    positions_df = get_all_positions_df()
    if positions_df.empty:
        print("❌ No positions found!")
        return None
        
    if version:
        if 'strategy_version' in positions_df.columns:
            # Handle float/string comparison safely
            positions_df = positions_df[positions_df['strategy_version'].astype(str) == str(version)]
        elif 'version' in positions_df.columns:
            positions_df = positions_df[positions_df['version'].astype(str) == str(version)]
    
    print(f"\n💰 Found {len(positions_df)} positions to analyze\n")
    
    results = {
        'total_positions': len(positions_df),
        'active_positions': 0,
        'closed_positions': 0,
        'positions_analysis': [],
        'summary': {
            'total_pnl': 0,
            'winning_positions': 0,
            'losing_positions': 0,
            'avg_pnl': 0
        }
    }
    
    for idx, pos in positions_df.iterrows():
        symbol = pos['symbol']
        entry_price = pos['entry_price']
        entry_date = pd.to_datetime(pos['entry_date']).date()
        status = pos.get('status', 'ACTIVE')
        current_price = pos.get('current_price', entry_price)
        stop_loss = pos.get('stop_loss', 0)
        trailing_stop = pos.get('trailing_stop', 0)
        pnl_pct = pos.get('pnl_pct', 0)
        days_held = pos.get('days_held', 0)
        exit_price = pos.get('exit_price', 0)
        exit_date = pos.get('exit_date', '')
        grade = pos.get('grade', 'N/A')
        
        today = datetime.today().date()
        days_since_entry = (today - entry_date).days
        
        print(f"\n{'='*80}")
        print(f"💰 Position: {symbol} ({status})")
        print(f"   Grade: {grade}")
        print(f"   Entry Date: {entry_date} ({days_since_entry} days ago)")
        print(f"   Entry: ₹{entry_price:.2f} | Stop: ₹{stop_loss:.2f} | Trailing: ₹{trailing_stop:.2f}")
        
        # Get current live price
        live_price = None
        price_source = "Unknown"
        try:
            live_price, price_source, _ = get_live_price(symbol)
            if live_price:
                print(f"   Current Live Price: ₹{live_price:.2f} ({price_source})")
                print(f"   Stored Price: ₹{current_price:.2f}")
        except Exception as e:
            print(f"   ⚠️ Could not fetch live price: {e}")
        
        position_analysis = {
            'symbol': symbol,
            'entry_date': safe_str(entry_date),
            'grade': safe_str(grade),
            'status': safe_str(status),
            'entry_price': safe_float(entry_price),
            'stop_loss': safe_float(stop_loss),
            'trailing_stop': safe_float(trailing_stop),
            'days_since_entry': days_since_entry,
            'stored_current_price': safe_float(current_price),
            'live_price': safe_float(live_price) if live_price else None,
            'price_source': safe_str(price_source),
            'exit_price': safe_float(exit_price),
            'exit_date': safe_str(exit_date),
            'pnl_pct': safe_float(pnl_pct),
            'days_held': safe_int(days_held),
            'issues': []
        }
        
        if status == 'CLOSED':
            results['closed_positions'] += 1
            if exit_price > 0:
                actual_pnl = ((exit_price - entry_price) / entry_price) * 100
                position_analysis['actual_pnl'] = actual_pnl
                print(f"   🏁 Exit: ₹{exit_price:.2f} | P&L: {actual_pnl:.2f}%")
                
                if actual_pnl > 0:
                    results['summary']['winning_positions'] += 1
                else:
                    results['summary']['losing_positions'] += 1
                
                results['summary']['total_pnl'] += actual_pnl
        else:
            results['active_positions'] += 1
            if live_price:
                current_pnl = ((live_price - entry_price) / entry_price) * 100
                position_analysis['current_pnl'] = current_pnl
                print(f"   📈 Current P&L: {current_pnl:.2f}%")
                
                # Check price accuracy
                stored_diff = abs(live_price - current_price)
                stored_diff_pct = (stored_diff / current_price * 100) if current_price > 0 else 0
                if stored_diff_pct > 3:
                    position_analysis['issues'].append(f'Stored price differs by {stored_diff_pct:.1f}% from live')
                    print(f"   ⚠️ ISSUE: Stored price differs by {stored_diff_pct:.1f}%")
        
        results['positions_analysis'].append(position_analysis)
    
    # Calculate averages
    if results['closed_positions'] > 0:
        results['summary']['avg_pnl'] = results['summary']['total_pnl'] / results['closed_positions']
    
    return results

def generate_report(signals_results, positions_results):
    """Generate comprehensive performance report"""
    print("\n\n" + "=" * 80)
    print("📊 GENERATING PERFORMANCE REPORT")
    print("=" * 80)
    
    report = {
        'report_date': str(datetime.today().date()),
        'signals': signals_results,
        'positions': positions_results
    }
    
    # Save to JSON
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n✅ Report saved to: {REPORT_FILE}")
    
    # Print summary
    print("\n" + "=" * 80)
    print("📈 SUMMARY")
    print("=" * 80)
    
    if signals_results:
        print(f"\n📊 SIGNALS:")
        print(f"   Total Signals: {signals_results['total_signals']}")
        print(f"   Active: {signals_results['active_signals']}")
        print(f"   Closed: {signals_results['closed_signals']}")
        
        if signals_results['closed_signals'] > 0:
            print(f"   Winning: {signals_results['summary']['winning_signals']}")
            print(f"   Losing: {signals_results['summary']['losing_signals']}")
            print(f"   Average P&L: {signals_results['summary']['avg_pnl']:.2f}%")
            print(f"   Max Profit: {signals_results['summary']['max_profit']:.2f}%")
            print(f"   Max Loss: {signals_results['summary']['max_loss']:.2f}%")
    
    if positions_results:
        print(f"\n💰 POSITIONS:")
        print(f"   Total Positions: {positions_results['total_positions']}")
        print(f"   Active: {positions_results['active_positions']}")
        print(f"   Closed: {positions_results['closed_positions']}")
        
        if positions_results['closed_positions'] > 0:
            print(f"   Winning: {positions_results['summary']['winning_positions']}")
            print(f"   Losing: {positions_results['summary']['losing_positions']}")
            print(f"   Average P&L: {positions_results['summary']['avg_pnl']:.2f}%")
    
    print("\n" + "=" * 80)
    print("✅ Analysis complete! Check the JSON report for detailed information.")
    print("=" * 80)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Verify signals and positions performance")
    parser.add_argument("--version", "-v", type=str, help="Filter strategy version (e.g. 3.3.0)")
    args = parser.parse_args()
    
    print("🔍 SIGNALS PERFORMANCE VERIFICATION")
    print("=" * 80)
    print("This script analyzes all signals and positions to verify performance.")
    print("Run this after 1 month to check signal accuracy and identify issues.\n")
    if args.version:
        print(f"Filtering by strategy version: {args.version}")
        
    signals_results = analyze_signals(version=args.version)
    positions_results = analyze_positions(version=args.version)
    
    if signals_results or positions_results:
        generate_report(signals_results, positions_results)
    else:
        print("\n❌ No data to analyze!")

