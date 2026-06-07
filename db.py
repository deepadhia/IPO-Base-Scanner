import os
import json
import hashlib
from datetime import datetime, timezone, timedelta, date
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "")
client = MongoClient(MONGO_URI, tz_aware=True) if MONGO_URI else None
db = client["ipo_scanner_v2"] if client else None

signals_col = db["signals"] if db is not None else None
positions_col = db["positions"] if db is not None else None
logs_col = db["logs"] if db is not None else None
instrument_keys_col = db["instrument_keys"] if db is not None else None
ipos_col = db["ipos"] if db is not None else None
listing_data_col = db["listing_data"] if db is not None else None
watchlist_col = db["watchlist"] if db is not None else None
system_audits_col = db["system_audits"] if db is not None else None

# In-process cache — avoids a DB round-trip on every data fetch
_instrument_key_cache: dict = {}

db_metrics = {
    "signals_generated": 0,
    "logs_written": 0,
    "rejections_logged": 0,
    "db_inserts": 0
}

# Versioning and Safeguards
SCANNER_VERSION = "3.3.0"  # v3.3.0: Volume floor, base-duration guard fix, 20-day patience stop, Limit Buy alerts
MAX_DAILY_REJECTIONS = 500
_rejection_guard_warned = False

IST = timezone(timedelta(hours=5, minutes=30))

def increment_metric(metric_name: str, count: int = 1):
    if metric_name in db_metrics:
        db_metrics[metric_name] += count

def make_utc(dt) -> datetime:
    """Ensure a datetime or date is timezone-aware and converted to UTC."""
    if not isinstance(dt, date):
        return dt
    if not isinstance(dt, datetime):
        dt = datetime.combine(dt, datetime.min.time(), tzinfo=timezone.utc)
    else:
        if dt.time() == datetime.min.time():
            # If it's representing local midnight (e.g. from Upstox or YFinance),
            # extract the local calendar date first and normalize to UTC midnight.
            dt = datetime.combine(dt.date(), datetime.min.time(), tzinfo=timezone.utc)
        elif dt.tzinfo is None:
            # Assume naive datetimes originating from the system are IST
            dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)

def generate_log_id(scanner: str, symbol: str, action: str, candle_timestamp, version: str, details: dict = None) -> str:
    """Generate deterministic dedupe hash for logs."""
    import json
    
    # 1. Standardize timestamp to DATE only for daily deduplication
    # This prevents duplicate logs if the scanner runs multiple times a day
    if isinstance(candle_timestamp, date):
        ts_str = candle_timestamp.strftime('%Y-%m-%d')
    else:
        ts_str = str(candle_timestamp)[:10] # Extract YYYY-MM-DD
        
    # 2. Stable Details: Remove volatile metrics (like live price/RSI) from dedupe hash
    # to ensure that "REJECTED_BREAKOUT" doesn't log 10 times if price moves 0.1%
    stable_details = {}
    if details:
        # Include structural metrics but exclude live price/time-sensitive ones
        skip_fields = {"current_price", "timestamp", "price_source", "rsi", "score", "candle_timestamp"}
        stable_details = {k: v for k, v in details.items() if k not in skip_fields}
        
    details_str = json.dumps(stable_details, sort_keys=True)
    raw = f"{scanner}_{symbol}_{action}_{ts_str}_{version}_{details_str}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def ensure_indexes():
    """Create necessary unique indexes for idempotency."""
    if db is None:
        return
    signals_col.create_index("signal_id", unique=True)
    positions_col.create_index("symbol", unique=True)
    logs_col.create_index("log_id", unique=True)
    logs_col.create_index("timestamp", expireAfterSeconds=2592000) # 30 days TTL
    logs_col.create_index("symbol")
    instrument_keys_col.create_index("ipo_symbol", unique=True)
    instrument_keys_col.create_index("isin")
    ipos_col.create_index("symbol", unique=True)
    listing_data_col.create_index("symbol", unique=True)
    watchlist_col.create_index("symbol", unique=True)
    system_audits_col.create_index("audit_id", unique=True)
    system_audits_col.create_index("timestamp")

def insert_log(scanner: str, symbol: str, action: str, candle_timestamp, details: dict, version: str = SCANNER_VERSION, source: str = "live", log_type: str = "ACCEPTED"):
    global _rejection_guard_warned
    if logs_col is None:
        return
        
    # Safeguard: Prevent runaway rejection logs on free tier
    if log_type == "REJECTED":
        if db_metrics["rejections_logged"] >= MAX_DAILY_REJECTIONS:
            if not _rejection_guard_warned:
                logger.warning(f"⚠️ [Telemetry] Daily rejection limit ({MAX_DAILY_REJECTIONS}) reached. Further rejections will not be logged.")
                _rejection_guard_warned = True
            return
        increment_metric("rejections_logged")
        
    if isinstance(candle_timestamp, str):
        try:
            candle_timestamp = datetime.fromisoformat(candle_timestamp.replace('Z', '+00:00'))
        except ValueError:
            pass

    candle_timestamp = make_utc(candle_timestamp)
    
    log_id = generate_log_id(scanner, symbol, action, candle_timestamp, version, details)
    doc = {
        "log_id": log_id,
        "timestamp": datetime.now(timezone.utc),
        "candle_timestamp": candle_timestamp,
        "symbol": symbol,
        "action": action,
        "scanner": scanner,
        "version": version,
        "log_type": log_type,
        "source": source,
        "details": details,
        "created_at": datetime.now(timezone.utc)
    }
    try:
        logs_col.insert_one(doc)
        increment_metric("db_inserts")
        increment_metric("logs_written")
    except DuplicateKeyError:
        pass # Ignore duplicates to preserve immutability
    except Exception as e:
        logger.error(f"Failed to insert log into MongoDB: {e}")

def upsert_signal(signal_doc: dict):
    if signals_col is None:
        return
        
    if isinstance(signal_doc.get("signal_date"), str):
        try:
            signal_doc["signal_date"] = datetime.strptime(signal_doc["signal_date"], "%Y-%m-%d")
        except ValueError:
            pass
            
    if isinstance(signal_doc.get("signal_date"), date):
        signal_doc["signal_date"] = make_utc(signal_doc["signal_date"])
        
    if isinstance(signal_doc.get("exit_date"), str) and signal_doc.get("exit_date"):
        try:
            signal_doc["exit_date"] = datetime.strptime(signal_doc["exit_date"], "%Y-%m-%d")
        except ValueError:
            pass
            
    if isinstance(signal_doc.get("exit_date"), date):
        signal_doc["exit_date"] = make_utc(signal_doc["exit_date"])
            
    signal_id = signal_doc.get("signal_id")
    if not signal_id:
        return
        
    doc = signal_doc.copy()
    doc.pop("created_at", None)
    doc["updated_at"] = datetime.now(timezone.utc)
    
    try:
        signals_col.update_one(
            {"signal_id": signal_id},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True
        )
        increment_metric("db_inserts")
        increment_metric("signals_generated")
    except Exception as e:
        logger.error(f"Failed to upsert signal {signal_id} into MongoDB: {e}")

def upsert_position(position_doc: dict):
    if positions_col is None:
        return
        
    if isinstance(position_doc.get("entry_date"), str):
        try:
            position_doc["entry_date"] = datetime.strptime(position_doc["entry_date"], "%Y-%m-%d")
        except ValueError:
            pass
            
    if isinstance(position_doc.get("entry_date"), date):
        position_doc["entry_date"] = make_utc(position_doc["entry_date"])
        
    if isinstance(position_doc.get("exit_date"), str) and position_doc.get("exit_date"):
        try:
            position_doc["exit_date"] = datetime.strptime(position_doc["exit_date"], "%Y-%m-%d")
        except ValueError:
            pass
            
    if isinstance(position_doc.get("exit_date"), date):
        position_doc["exit_date"] = make_utc(position_doc["exit_date"])
            
    symbol = position_doc.get("symbol")
    if not symbol:
        return
        
    position_doc["updated_at"] = datetime.now(timezone.utc)
    
    try:
        positions_col.update_one(
            {"symbol": symbol},
            {"$set": position_doc},
            upsert=True
        )
        increment_metric("db_inserts")
    except Exception as e:
        logger.error(f"Failed to upsert position for {symbol} into MongoDB: {e}")

def signal_exists(signal_id: str) -> bool:
    """Return True if a signal_id already exists in MongoDB."""
    if signals_col is None or not signal_id:
        return False
    try:
        return signals_col.count_documents({"signal_id": signal_id}, limit=1) > 0
    except Exception as e:
        logger.error(f"Failed signal_exists lookup for {signal_id}: {e}")
        return False

def has_active_position(symbol: str) -> bool:
    """Return True if the symbol already has an ACTIVE position."""
    if positions_col is None or not symbol:
        return False
    try:
        return positions_col.count_documents({"symbol": symbol, "status": "ACTIVE"}, limit=1) > 0
    except Exception as e:
        logger.error(f"Failed has_active_position lookup for {symbol}: {e}")
        return False

def get_last_signal_date(symbol: str):
    """Fetch the latest signal_date for a symbol from MongoDB."""
    if signals_col is None or not symbol:
        return None
    try:
        doc = signals_col.find_one(
            {"symbol": symbol},
            {"signal_date": 1, "_id": 0},
            sort=[("signal_date", -1)],
        )
        return doc.get("signal_date") if doc else None
    except Exception as e:
        logger.error(f"Failed get_last_signal_date lookup for {symbol}: {e}")
        return None

def get_active_positions_count() -> int:
    """Return count of active positions from MongoDB."""
    if positions_col is None:
        return 0
    try:
        return positions_col.count_documents({"status": "ACTIVE"})
    except Exception as e:
        logger.error(f"Failed get_active_positions_count: {e}")
        return 0


def upsert_instrument_key(ipo_symbol: str, instrument_key: str, isin: str = None,
                          name: str = None, match_type: str = "exact", exchange: str = "NSE"):
    """Upsert a symbol → instrument_key mapping into the instrument_keys collection."""
    if instrument_keys_col is None:
        return
    doc = {
        "ipo_symbol": ipo_symbol,
        "instrument_key": instrument_key,
        "isin": isin,
        "name": name or ipo_symbol,
        "match_type": match_type,
        "exchange": exchange,
        "updated_at": datetime.now(timezone.utc),
    }
    try:
        instrument_keys_col.update_one(
            {"ipo_symbol": ipo_symbol},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True
        )
        # Invalidate local cache entry so next fetch picks up the new value
        _instrument_key_cache.pop(ipo_symbol, None)
    except Exception as e:
        logger.error(f"Failed to upsert instrument key for {ipo_symbol}: {e}")


def get_instrument_key_mapping() -> dict:
    """Load symbol to instrument_key mapping from MongoDB instrument_keys collection."""
    global _instrument_key_cache
    if _instrument_key_cache:
        return _instrument_key_cache

    if instrument_keys_col is not None:
        try:
            docs = instrument_keys_col.find({}, {"ipo_symbol": 1, "instrument_key": 1, "_id": 0})
            _instrument_key_cache = {d["ipo_symbol"]: d["instrument_key"] for d in docs}
            logger.info(f"[InstrumentKeys] Loaded {len(_instrument_key_cache)} mappings from MongoDB")
            return _instrument_key_cache
        except Exception as e:
            logger.error(f"[InstrumentKeys] MongoDB error: {e}")

    return _instrument_key_cache

def upsert_ipo(symbol: str, listing_date=None, name: str = None, **kwargs):
    """Upsert an IPO symbol record into the ipos collection."""
    if ipos_col is None:
        return
    
    if isinstance(listing_date, str) and listing_date:
        try:
            listing_date = datetime.strptime(listing_date, "%Y-%m-%d")
        except ValueError:
            pass
            
    if isinstance(listing_date, date):
        listing_date = make_utc(listing_date)
        
    doc = {
        "symbol": symbol,
        "listing_date": listing_date,
        "name": name or symbol,
        "updated_at": datetime.now(timezone.utc),
        **kwargs
    }
    doc.pop("created_at", None)
    try:
        ipos_col.update_one(
            {"symbol": symbol},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True
        )
        increment_metric("db_inserts")
    except Exception as e:
        logger.error(f"Failed to upsert IPO {symbol}: {e}")

def upsert_listing_data(symbol: str, data: dict):
    """Upsert listing day metrics into the listing_data collection."""
    if listing_data_col is None:
        return
        
    doc = data.copy()
    doc.pop("created_at", None)
    doc["symbol"] = symbol
    doc["updated_at"] = datetime.now(timezone.utc)
    
    # Cast dates if present
    for date_field in ["listing_date", "last_updated"]:
        if isinstance(doc.get(date_field), str) and doc.get(date_field):
            try:
                # Try common formats
                for fmt in ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S"]:
                    try:
                        doc[date_field] = datetime.strptime(doc[date_field], fmt)
                        break
                    except ValueError:
                        continue
            except Exception:
                pass
        
        if isinstance(doc.get(date_field), date):
            doc[date_field] = make_utc(doc[date_field])
            
    try:
        listing_data_col.update_one(
            {"symbol": symbol},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True
        )
        increment_metric("db_inserts")
    except Exception as e:
        logger.error(f"Failed to upsert listing data for {symbol}: {e}")


def upsert_watchlist(symbol: str, data: dict = None):
    """Upsert a symbol into the watchlist collection."""
    if watchlist_col is None:
        return
    doc = data.copy() if data else {}
    doc.pop("created_at", None)
    doc["symbol"] = symbol
    doc["status"] = doc.get("status", "ACTIVE")
    doc["updated_at"] = datetime.now(timezone.utc)
    try:
        watchlist_col.update_one(
            {"symbol": symbol},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Failed to upsert watchlist for {symbol}: {e}")


def get_all_positions_df(status: str = None):
    """Return all positions from MongoDB as a pandas DataFrame.

    Args:
        status: Optional filter, e.g. 'ACTIVE' or 'CLOSED'. None returns all.

    Returns:
        pd.DataFrame with position rows, empty DataFrame if unavailable.
    """
    import pandas as pd
    if positions_col is None:
        logger.warning("[DB] positions_col is None — returning empty DataFrame")
        return pd.DataFrame()
    try:
        query = {}
        if status:
            query["status"] = status
        
        # 1. Get from positions collection
        pos_docs = list(positions_col.find(query, {"_id": 0}))
        
        # 2. Get from signals collection (Phase 2.2 migration path)
        # We look for records where lifecycle_state is POSITION_ACTIVE or status is ACTIVE
        sig_query = {}
        if status == "ACTIVE":
            sig_query = {"$or": [{"status": "ACTIVE"}, {"lifecycle_state": "POSITION_ACTIVE"}]}
        elif status == "CLOSED":
            sig_query = {"$or": [{"status": "CLOSED"}, {"lifecycle_state": "CLOSED"}]}
        else:
            sig_query = {}
            
        sig_docs = list(signals_col.find(sig_query, {"_id": 0}))
        
        # Tag sources to prioritize positions_col records during deduplication
        for d in pos_docs:
            d["_doc_source"] = "position"
        for d in sig_docs:
            d["_doc_source"] = "signal"

        # Combine them
        docs = pos_docs + sig_docs
        
        if not docs:
            return pd.DataFrame(columns=["symbol", "status", "entry_date", "exit_date", "pnl_pct", "grade"])
        
        df = pd.DataFrame(docs)
        
        # Explicit Schema Guarantee & Mapping
        if "lifecycle_state" in df.columns:
            if "status" not in df.columns:
                df["status"] = df["lifecycle_state"]
            else:
                df["status"] = df["status"].fillna(df["lifecycle_state"])
            df["status"] = df["status"].apply(lambda x: "ACTIVE" if x == "POSITION_ACTIVE" else x)
        
        # Deduplicate combined rows by symbol, prioritizing positions collection record
        if not df.empty and "symbol" in df.columns and "_doc_source" in df.columns:
            df["_source_order"] = df["_doc_source"].map({"position": 0, "signal": 1}).fillna(2)
            df = df.sort_values(["symbol", "_source_order"])
            df = df.drop_duplicates(subset=["symbol"], keep="first")
            df = df.drop(columns=["_doc_source", "_source_order"], errors="ignore")
        
        if "signal_date" in df.columns:
            if "entry_date" not in df.columns:
                df["entry_date"] = df["signal_date"]
            else:
                df["entry_date"] = df["entry_date"].combine_first(df["signal_date"])
        
        # Fallback to created_at (v2 schema)
        if "created_at" in df.columns:
            if "entry_date" not in df.columns:
                df["entry_date"] = df["created_at"]
            else:
                df["entry_date"] = df["entry_date"].combine_first(df["created_at"])
        
        # Final safety for missing columns
        if "status" not in df.columns: df["status"] = None
        if "entry_date" not in df.columns: 
            # Deep fallback for backfilled signals missing top-level dates
            if "decision_snapshot" in df.columns:
                def extract_snapshot_ts(x):
                    if isinstance(x, dict) and "snapshot_ts" in x:
                        return x["snapshot_ts"]
                    return None
                df["entry_date"] = df["decision_snapshot"].apply(extract_snapshot_ts)
            else:
                df["entry_date"] = None
            
        # Normalise entry_date to timezone-naive date so callers don't break
        for col in ["entry_date", "exit_date"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], utc=True, errors="coerce").dt.tz_localize(None)
        return df
    except Exception as e:
        logger.error(f"[DB] get_all_positions_df failed: {e}")
        return pd.DataFrame()


def get_all_signals_df(status: str = None):
    """Return all signals from MongoDB as a pandas DataFrame.

    Args:
        status: Optional filter, e.g. 'ACTIVE' or 'CLOSED'. None returns all.

    Returns:
        pd.DataFrame with signal rows, empty DataFrame if unavailable.
    """
    import pandas as pd
    if signals_col is None:
        logger.warning("[DB] signals_col is None — returning empty DataFrame")
        return pd.DataFrame()
    try:
        query = {}
        if status:
            query["status"] = status
        docs = list(signals_col.find(query, {"_id": 0}))
        if not docs:
            return pd.DataFrame(columns=["symbol", "status", "signal_date", "exit_date", "pnl_pct", "grade"])
        df = pd.DataFrame(docs)
        # Normalise signal_date to timezone-naive datetime for callers
        for col in ["signal_date", "exit_date"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], utc=True, errors="coerce").dt.tz_localize(None)
        return df
    except Exception as e:
        logger.error(f"[DB] get_all_signals_df failed: {e}")
        return pd.DataFrame()


def close_signal_in_db(symbol: str, exit_price: float, pnl_pct: float, days_held: int, exit_reason: str):
    """Mark the most-recent ACTIVE signal for a symbol as CLOSED in MongoDB."""
    if signals_col is None:
        return
    try:
        signals_col.update_many(
            {"symbol": symbol, "$or": [
                {"status": "ACTIVE"},
                {"lifecycle_state": "POSITION_ACTIVE"},
            ]},
            {"$set": {
                "status": "CLOSED",
                "lifecycle_state": "CLOSED",
                "exit_date": datetime.now(timezone.utc),
                "exit_price": float(exit_price),
                "pnl_pct": float(pnl_pct),
                "days_held": int(days_held),
                "exit_reason": exit_reason,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        # SYNC: Also close the record in positions collection if it exists
        if positions_col is not None:
            positions_col.update_one(
                {"symbol": symbol, "status": "ACTIVE"},
                {"$set": {
                    "status": "CLOSED",
                    "lifecycle_state": "CLOSED",
                    "exit_date": datetime.now(timezone.utc),
                    "exit_price": float(exit_price),
                    "pnl_pct": float(pnl_pct),
                    "days_held": int(days_held),
                    "exit_reason": exit_reason,
                    "updated_at": datetime.now(timezone.utc),
                }}
            )
    except Exception as e:
        logger.error(f"[DB] close_signal_in_db failed for {symbol}: {e}")


def get_active_symbols() -> list:
    """Return list of symbols that have an ACTIVE position in MongoDB."""
    if positions_col is None:
        return []
    try:
        docs = positions_col.find({"status": "ACTIVE"}, {"symbol": 1, "_id": 0})
        return [d["symbol"] for d in docs]
    except Exception as e:
        logger.error(f"[DB] get_active_symbols failed: {e}")
        return []


def save_audit_to_db(
    audit_id: str,
    overall_status: str,
    n_errors: int,
    n_warnings: int,
    findings: list,
    performance_snapshot: dict,
    fixes_applied: list = None,
    report_file: str = None,
) -> bool:
    """
    Persist a weekly audit run to the system_audits collection.

    Args:
        audit_id:             Unique identifier e.g. 'audit_20260531_121817'
        overall_status:       'PASS' or 'FAIL'
        n_errors:             Count of ERROR-level findings
        n_warnings:           Count of WARN-level findings
        findings:             Full list of finding dicts from the audit run
        performance_snapshot: Dict with win_rate, avg_pnl, cohort_avg, etc.
        fixes_applied:        List of fix descriptions applied in this run
        report_file:          Absolute path to the saved .txt report file

    Returns:
        True on success, False on failure.
    """
    if system_audits_col is None:
        logger.warning("[DB] system_audits_col is None — audit not saved to DB")
        return False

    doc = {
        "audit_id":            audit_id,
        "timestamp":           datetime.now(timezone.utc),
        "overall_status":      overall_status,
        "n_errors":            n_errors,
        "n_warnings":          n_warnings,
        "findings":            findings,
        "performance_snapshot": performance_snapshot or {},
        "fixes_applied":       fixes_applied or [],
        "report_file":         report_file,
    }

    try:
        system_audits_col.update_one(
            {"audit_id": audit_id},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        logger.info(f"[DB] Audit {audit_id} saved to system_audits collection")
        return True
    except Exception as e:
        logger.error(f"[DB] save_audit_to_db failed for {audit_id}: {e}")
        return False
