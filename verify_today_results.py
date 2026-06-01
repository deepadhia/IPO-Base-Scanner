import os
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient
from dotenv import load_dotenv

def main():
    """
    Monitoring script to verify the integrity of the daily scan results.
    Run this after every scan to check your 'Trust Score'.
    """
    load_dotenv()
    MONGO_URI = os.getenv("MONGO_URI", "")
    if not MONGO_URI:
        print("Error: MONGO_URI not found in .env file.")
        return

    client = MongoClient(MONGO_URI)
    db = client["ipo_scanner_v2"]
    signals_v2 = db["signals"]
    signal_updates = db["positions"]
    
    # Today's range in UTC based on IST day boundaries (Match logic of the daily scanner)
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    today_start_ist = datetime.combine(now_ist.date(), datetime.min.time(), tzinfo=ist)
    today = today_start_ist.astimezone(timezone.utc)
    tomorrow = today + timedelta(days=1)
    
    # 1. Fetch Signals for Today
    query = {"created_at": {"$gte": today, "$lt": tomorrow}}
    signals = list(signals_v2.find(query))
    
    total_signals = len(signals)
    complete_signals = sum(1 for s in signals if s.get("is_complete_snapshot"))
    incomplete_signals = total_signals - complete_signals
    
    print("-" * 50)
    print(f"[AUDIT] FORENSIC AUDIT REPORT: {datetime.now().strftime('%Y-%m-%d')}")
    print("-" * 50)
    print(f"Total Signals Detected:   {total_signals}")
    print(f"Complete Snapshots:       {complete_signals}")
    print(f"Incomplete Snapshots:     {incomplete_signals}")
    
    if total_signals > 0:
        trust_score = complete_signals / total_signals
        print(f"SYSTEM TRUST SCORE:      {trust_score:.2%}")
        
        if trust_score < 1.0:
            print("\n[!] DETECTION GAPS (Reasons for Incompleteness):")
            reasons_count = {}
            for s in signals:
                if not s.get("is_complete_snapshot"):
                    for r in s.get("incomplete_reasons", []):
                        reasons_count[r] = reasons_count.get(r, 0) + 1
            for r, count in reasons_count.items():
                print(f"  - {r}: {count} signals")
    else:
        print("\n(No signals were detected during today's scan window)")

    # 2. Check Lifecycle Updates (Position Tracking)
    updates_query = {"logged_at": {"$gte": today, "$lt": tomorrow}}
    updates_count = signal_updates.count_documents(updates_query)
    print(f"\n[UPDATES] Lifecycle Updates:    {updates_count} positions updated")
    
    # 3. Print the Audit Blueprint (Stratified Samples)
    if total_signals > 0:
        print("\n[SAMPLES] STRATIFIED AUDIT SAMPLES (Verify these in MongoDB):")
        # Show a few examples to manually verify
        for s in signals[:5]:
            status = "COMPLETE" if s.get("is_complete_snapshot") else "INCOMPLETE"
            print(f"  - {s['symbol']} ({s['signal_id']}) | {status}")
            if s.get('entry_price') == 0:
                print("    [!] Warning: entry_price is 0.0 - check builder logic.")
    
    print("-" * 50)

if __name__ == "__main__":
    main()
