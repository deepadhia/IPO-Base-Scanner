import pandas as pd
import json
import os
import argparse
from datetime import datetime, timedelta
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

def _aggregate_rejections_from_jsonl(day_dir, version_filter=None, include_watchlist=True):
    """Fallback parser: aggregate rejection reasons from daily JSONL logs."""
    rejection_counts = {}
    total = 0
    parsed_entries = 0
    files = ["consolidation.jsonl", "listing_day.jsonl"]
    if include_watchlist:
        files.append("watchlist.jsonl")
    for file_name in files:
        file_path = os.path.join(day_dir, file_name)
        if not os.path.exists(file_path):
            continue
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if version_filter and str(entry.get("version", "")) != str(version_filter):
                        continue

                    action = entry.get("action")
                    if action not in ("REJECTED_BREAKOUT", "PENDING_REJECTED"):
                        continue

                    details = entry.get("details", {}) or {}
                    reason = details.get("rejection_reason", details.get("reason", "unknown"))
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    total += 1
                    parsed_entries += 1
        except Exception:
            continue
    return rejection_counts, total, parsed_entries

def run_analysis(start_date=None, version_filter=None, rejection_days=10, clean_cohort=False):
    print("===========================================")
    print(" IPO Scanner: 30-Day Quantitative Analysis ")
    print("===========================================")

    # ── Load from MongoDB (v2.5.0+ architecture) ─────────────────────────────
    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        print(" Error: MONGO_URI not set in environment")
        return

    try:
        client = MongoClient(mongo_uri, tz_aware=False)
        db = client["ipo_scanner_v2"]
        positions_raw = list(db.positions.find({}, {"_id": 0}))
        signals_raw   = list(db.signals.find({}, {"_id": 0}))
    except Exception as e:
        print(f" Error: Could not connect to MongoDB: {e}")
        return

    if not positions_raw:
        print(" Error: No positions found in MongoDB (ipo_scanner_v2.positions)")
        return

    df_pos_all = pd.DataFrame(positions_raw)
    df_pos = df_pos_all.copy()
    df_sig = pd.DataFrame(signals_raw) if signals_raw else None

    # Normalise date columns
    if "entry_date" in df_pos.columns:
        df_pos["entry_date"] = pd.to_datetime(df_pos["entry_date"], errors="coerce")
    if df_sig is not None and "signal_date" in df_sig.columns:
        df_sig["signal_date"] = pd.to_datetime(df_sig["signal_date"], errors="coerce")

    # ── Apply filters (same logic as the original CSV version) ────────────────
    if start_date:
        if "entry_date" in df_pos.columns:
            df_pos = df_pos[df_pos["entry_date"].dt.date >= start_date].copy()
        else:
            print(" Warning: entry_date not found in positions, start-date filter skipped.")
        if df_sig is not None and "signal_date" in df_sig.columns:
            df_sig = df_sig[df_sig["signal_date"].dt.date >= start_date].copy()

    if version_filter:
        if "version" in df_pos.columns:
            df_pos = df_pos[df_pos["version"].astype(str) == str(version_filter)].copy()
        if df_sig is not None and "version" in df_sig.columns:
            df_sig = df_sig[df_sig["version"].astype(str) == str(version_filter)].copy()

    if clean_cohort and df_sig is not None:
        if "signal_type" in df_sig.columns:
            df_sig = df_sig[df_sig["signal_type"].fillna("").astype(str) != "WATCHLIST"].copy()
        if "grade" in df_sig.columns:
            df_sig = df_sig[~df_sig["grade"].fillna("").astype(str).str.contains("LOW_VOL", na=False)].copy()
        eligible_symbols = set(df_sig["symbol"].dropna().astype(str).tolist()) if "symbol" in df_sig.columns else set()
        if eligible_symbols:
            df_pos = df_pos[df_pos["symbol"].astype(str).isin(eligible_symbols)].copy()
        else:
            df_pos = df_pos.iloc[0:0].copy()

    print(f"\n FILTERS:")
    print(f"   Start Date: {start_date if start_date else 'None'}")
    print(f"   Version: {version_filter if version_filter else 'None'}")
    print(f"   Clean Cohort: {'ON (exclude WATCHLIST + LOW_VOL)' if clean_cohort else 'OFF'}")
    print(f"   Positions in scope: {len(df_pos)}/{len(df_pos_all)}")
    if df_sig is not None:
        print(f"   Signals in scope: {len(df_sig)}")
    
    # 1. Base Win Rates
    total = len(df_pos)
    if total == 0:
        print("No positions found in filtered scope.")
        return
        
    closed = df_pos[df_pos['status'] == 'CLOSED']
    active = df_pos[df_pos['status'] == 'ACTIVE']
    
    print(f"\n TOTAL POSITIONS TAKEN: {total}")
    print(f"   Currently Active: {len(active)}")
    print(f"   Closed: {len(closed)}")
    
    if len(closed) == 0:
        print("\n Not enough closed positions for deep outcome analysis yet.")
        return
        
    winners = closed[closed['pnl_pct'] > 0]
    losers = closed[closed['pnl_pct'] <= 0]
    
    win_rate = len(winners) / len(closed) * 100
    print(f"\n OVERALL WIN RATE: {win_rate:.1f}% ({len(winners)}W / {len(losers)}L)")
    
    # 2. Outcome Type Distribution
    if 'outcome_type' in closed.columns:
        print("\n OUTCOME CLASSIFICATIONS:")
        outcomes = closed['outcome_type'].value_counts()
        for classification, count in outcomes.items():
            if pd.isna(classification) or classification == "":
                classification = "UNCLASSIFIED"
            pct = count / len(closed) * 100
            print(f"   - {classification}: {count} ({pct:.1f}%)")
            
    # 3. Holding Efficiency
    if 'holding_efficiency_pct' in closed.columns:
        valid_eff = closed[pd.notna(closed['holding_efficiency_pct'])]
        if len(valid_eff) > 0:
            avg_eff = valid_eff['holding_efficiency_pct'].mean()
            print(f"\n AVG HOLDING EFFICIENCY: {avg_eff:.1f}% (For winning runs >5%)")

    # 3b. Failure speed diagnostics (if tracked)
    if 'time_to_failure_min' in closed.columns:
        failed = closed[pd.notna(closed['time_to_failure_min'])]
        if len(failed) > 0:
            avg_fail_min = failed['time_to_failure_min'].mean()
            print(f" AVG TIME TO FAILURE: {avg_fail_min:.0f} min")
    elif 'time_to_failure_days' in closed.columns:
        failed = closed[pd.notna(closed['time_to_failure_days'])]
        if len(failed) > 0:
            avg_fail_days = failed['time_to_failure_days'].mean()
            print(f" AVG TIME TO FAILURE: {avg_fail_days:.1f} days")
            
    # 4. Tie to signals (Tier Analysis)
    if df_sig is not None:
        # Merge signals into positions to get tier and scores
        merged = closed.merge(df_sig, on='symbol', suffixes=('_pos', '_sig'))
        
        if 'tier' in merged.columns or 'tier_sig' in merged.columns:
            tier_col = 'tier_sig' if 'tier_sig' in merged.columns else 'tier'
            print("\n WIN RATE BY TIER:")
            tiers = merged[tier_col].dropna().unique()
            for t in sorted(tiers):
                t_df = merged[merged[tier_col] == t]
                if len(t_df) > 0:
                    t_win = len(t_df[t_df['pnl_pct_pos'] > 0]) / len(t_df) * 100
                    print(f"   - Tier {t}: {t_win:.1f}% ({len(t_df[t_df['pnl_pct_pos'] > 0])}W / {len(t_df[t_df['pnl_pct_pos'] <= 0])}L)")
        
        # Breakdown by Signal Score if available
        score_col = None
        if 'signal_strength_score' in merged.columns:
            score_col = 'signal_strength_score'
        elif 'score_sig' in merged.columns:
            score_col = 'score_sig'
            
        if score_col:
            high_score = merged[merged[score_col] >= 8.0]
            if len(high_score) > 0:
                high_win = len(high_score[high_score['pnl_pct_pos'] > 0]) / len(high_score) * 100
                print(f"\n HIGH SCORE (>= 8.0) PERFORMANCE: {high_win:.1f}% Win Rate")

    # 5. Rejection Log Scan
    print("\n REJECTION ANALYSIS (Last 10 Days)")
    logs_dir = "logs"
    rejection_reasons = {}
    total_rejections = 0
    summary_days_used = 0
    jsonl_fallback_days_used = 0
    
    if os.path.exists(logs_dir):
        if start_date:
            cutoff_date = start_date
        else:
            cutoff_date = (datetime.today() - timedelta(days=rejection_days)).date()
        for date_str in os.listdir(logs_dir):
            try:
                date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                if date_obj.date() >= cutoff_date:
                    day_dir = os.path.join(logs_dir, date_str)
                    summary_file = os.path.join(day_dir, "daily_summary.json")
                    day_used = False

                    # Prefer daily summary (fast path)
                    # If version filter is enabled, skip summary shortcut and parse JSONL directly.
                    if os.path.exists(summary_file) and not version_filter and not clean_cohort:
                        try:
                            with open(summary_file, "r", encoding="utf-8") as f:
                                data = json.load(f)
                            day_rejections = data.get("rejections", {}) or {}
                            if day_rejections:
                                for reason, count in day_rejections.items():
                                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + int(count)
                                    total_rejections += int(count)
                                summary_days_used += 1
                                day_used = True
                        except Exception:
                            pass

                    # Fallback: parse JSONL logs directly when summary is missing/empty
                    if not day_used:
                        day_counts, day_total, parsed_entries = _aggregate_rejections_from_jsonl(
                            day_dir,
                            version_filter=version_filter,
                            include_watchlist=(not clean_cohort),
                        )
                        if parsed_entries > 0:
                            for reason, count in day_counts.items():
                                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + int(count)
                            total_rejections += int(day_total)
                            jsonl_fallback_days_used += 1
            except:
                continue
                
        if total_rejections > 0:
            print(f"   Total explicit rejections: {total_rejections}")
            sorted_rejections = sorted(rejection_reasons.items(), key=lambda x: x[1], reverse=True)
            for reason, count in sorted_rejections[:5]:
                print(f"   - {reason}: {count} ({count/total_rejections*100:.1f}%)")
            print(f"   Source: daily_summary.json days={summary_days_used}, JSONL fallback days={jsonl_fallback_days_used}")
        else:
            print("   No recent rejection entries found in daily summaries or JSONL logs.")
            
    print("\n===========================================")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run quantitative IPO scanner analysis with non-destructive filters.")
    parser.add_argument("--start-date", type=str, default=None, help="Include rows from this date onward (YYYY-MM-DD).")
    parser.add_argument("--version", type=str, default=None, help="Optional version filter, e.g. 2.1.0.")
    parser.add_argument("--rejection-days", type=int, default=10, help="Lookback days for rejection analysis when start-date is not provided.")
    parser.add_argument("--clean-cohort", action="store_true", help="Exclude WATCHLIST and LOW_VOL signal cohorts from analysis scope.")
    args = parser.parse_args()

    parsed_start = None
    if args.start_date:
        try:
            parsed_start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        except ValueError:
            print(" Error: --start-date must be YYYY-MM-DD")
            raise SystemExit(2)

    run_analysis(
        start_date=parsed_start,
        version_filter=args.version,
        rejection_days=args.rejection_days,
        clean_cohort=args.clean_cohort,
    )
