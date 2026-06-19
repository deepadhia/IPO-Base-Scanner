import os
import datetime
from pymongo import MongoClient
from dotenv import load_dotenv

def main():
    load_dotenv()
    MONGO_URI = os.getenv("MONGO_URI", "")
    client = MongoClient(MONGO_URI)
    db = client["ipo_scanner_v2"]
    
    today = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    print("Today local start:", today)
    
    # Naive query (matches MongoDB's UTC naive datetimes)
    count_logs = db["logs"].count_documents({"timestamp": {"$gte": today}})
    count_signals = db["signals"].count_documents({"created_at": {"$gte": today}})
    
    print(f"Logs written today: {count_logs}")
    print(f"Signals created today: {count_signals}")

    if count_logs > 0:
        print("\n--- SAMPLE LOGS FROM TODAY ---")
        for log in db["logs"].find({"timestamp": {"$gte": today}}).sort("timestamp", -1).limit(5):
            print(f"- {log.get('timestamp')} | {log.get('symbol')} | {log.get('action')} | {log.get('scanner')}")

if __name__ == "__main__":
    main()
