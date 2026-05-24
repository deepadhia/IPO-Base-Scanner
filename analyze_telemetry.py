import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from db import logs_col, db, SCANNER_VERSION
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def run_analysis(days=3):
    if logs_col is None:
        logger.error("❌ MongoDB connection unavailable. Cannot run analysis.")
        return

    logger.info(f"🚀 Starting Phase 4 Analysis (Data Version: {SCANNER_VERSION})")
    logger.info(f"📊 Analyzing last {days} days of telemetry...")
    
    # 1. Fetch Data
    # Filter by version 2.2.0 and recent dates
    since_date = datetime.now(timezone.utc) - timedelta(days=days)
    
    query = {
        "version": SCANNER_VERSION,
        "timestamp": {"$gte": since_date},
        "action": {"$in": ["ACCEPTED_BREAKOUT", "REJECTED_BREAKOUT"]}
    }
    
    cursor = logs_col.find(query)
    docs = list(cursor)
    
    if not docs:
        logger.warning(f"⚠️ No v{SCANNER_VERSION} logs found in the last {days} days.")
        return

    df = pd.DataFrame(docs).reset_index(drop=True)
    
    # Expand details into columns for analysis
    details_df = pd.json_normalize(df['details']).reset_index(drop=True)
    dup_cols = [c for c in details_df.columns if c in df.columns]
    if dup_cols:
        details_df = details_df.drop(columns=dup_cols)
    df = pd.concat([df.drop('details', axis=1, errors='ignore'), details_df], axis=1)
    
    # Ensure log_type exists (older 2.2.0 test logs might lack it)
    if 'log_type' not in df.columns:
        df['log_type'] = df['action'].apply(lambda x: 'ACCEPTED' if 'ACCEPTED' in x else 'REJECTED')

    # 2. System Health
    total_logs = len(df)
    accepted = df[df['log_type'] == 'ACCEPTED']
    rejected = df[df['log_type'] == 'REJECTED']
    
    logger.info("\n" + "="*40)
    logger.info("=== SYSTEM HEALTH ===")
    logger.info(f"Total Scans Recorded:  {total_logs}")
    logger.info(f"Accepted Signals:      {len(accepted)}")
    logger.info(f"Rejected Signals:      {len(rejected)}")
    logger.info(f"Conversion Rate:       {(len(accepted)/total_logs*100):.1f}%" if total_logs > 0 else "N/A")

    # 3. Rejection Reason Distribution
    if not rejected.empty and 'rejection_reason' in rejected.columns:
        logger.info("\n=== REJECTION REASONS ===")
        reasons = rejected['rejection_reason'].value_counts(normalize=True) * 100
        for reason, pct in reasons.items():
            logger.info(f"{reason:<20} : {pct:>5.1f}%")
        
        if reasons.iloc[0] > 70:
            logger.warning(f"⚠️ WARNING: {reasons.index[0]} is dominating rejections. Filter may be too strict.")

    # 4. Metric Distributions & Comparison
    metrics_to_analyze = ['metrics.vol_ratio', 'metrics.rsi', 'metrics.prng', 'metrics.perf']
    
    logger.info("\n=== METRIC COMPARISON (Accepted vs Rejected) ===")
    logger.info(f"{'Metric':<15} | {'Accepted (Avg)':<15} | {'Rejected (Avg)':<15} | {'Separation'}")
    logger.info("-" * 65)
    
    for m in metrics_to_analyze:
        if m in df.columns:
            # Drop NaNs for this specific metric
            m_acc = accepted[m].dropna()
            m_rej = rejected[m].dropna()
            
            avg_acc = m_acc.mean() if not m_acc.empty else 0
            avg_rej = m_rej.mean() if not m_rej.empty else 0
            diff = avg_acc - avg_rej
            
            logger.info(f"{m.replace('metrics.', ''):<15} | {avg_acc:>15.2f} | {avg_rej:>15.2f} | {diff:>10.2f}")

    # 5. Near-Miss Analysis (Failing by < 15%)
    if not rejected.empty and 'failing_value' in rejected.columns and 'threshold' in rejected.columns:
        logger.info("\n=== NEAR-MISS ANALYSIS (Within 15% of Threshold) ===")
        # Calculate how close they were: ratio of value to threshold
        # For vol_ratio/rsi, higher is better, so value/threshold < 1
        # For prng, lower is better, so threshold/value < 1
        
        def calculate_closeness(row):
            val = row['failing_value']
            thresh = row['threshold']
            if not val or not thresh or thresh == 0: return 0
            
            metric = row.get('failing_metric', '')
            if 'prng' in metric or 'loose_base' in metric:
                return thresh / val if val > thresh else 1.0
            else:
                return val / thresh if val < thresh else 1.0

        rejected['closeness'] = rejected.apply(calculate_closeness, axis=1)
        near_misses = rejected[rejected['closeness'] >= 0.85]
        
        logger.info(f"Near-Miss Candidates:  {len(near_misses)}")
        logger.info(f"Near-Miss Rate:        {(len(near_misses)/len(rejected)*100):.1f}% of rejections")
        
        if not near_misses.empty:
            top_near_misses = near_misses['symbol'].unique()[:5]
            logger.info(f"Top Near-Miss Symbols: {', '.join(top_near_misses)}")
        
        if (len(near_misses)/len(rejected)) > 0.4:
            logger.warning("⚠️ WARNING: Very high near-miss rate (>40%). Thresholds might be starving the system.")

    # 6. Grade Analysis
    if not rejected.empty and 'metrics.score' in rejected.columns:
        logger.info("\n=== POTENTIAL QUALITY OF REJECTIONS ===")
        # What would the grade have been?
        def assign_grade(score):
            if score >= 8: return 'A+'
            if score >= 6: return 'A'
            if score >= 4: return 'B'
            if score >= 2: return 'C'
            return 'D'
        
        rejected['potential_grade'] = rejected['metrics.score'].apply(assign_grade)
        grade_dist = rejected['potential_grade'].value_counts()
        for g in ['A+', 'A', 'B', 'C', 'D']:
            count = grade_dist.get(g, 0)
            logger.info(f"Grade {g:<3} rejections: {count}")
        
        high_quality_rejects = grade_dist.get('A+', 0) + grade_dist.get('A', 0)
        if high_quality_rejects > 0:
            logger.info(f"💡 Note: You rejected {high_quality_rejects} A/A+ setups due to secondary filters.")

    logger.info("\n" + "="*40)
    logger.info("✅ Analysis Complete.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Phase 4 Telemetry Analysis")
    parser.add_argument("--days", type=int, default=3, help="Number of days to analyze")
    args = parser.parse_args()
    
    run_analysis(days=args.days)
