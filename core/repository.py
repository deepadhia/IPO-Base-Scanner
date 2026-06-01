import os
import hashlib
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
import logging
from .models import Signal

# Import existing connection logic if possible, or re-initialize safely
try:
    from db import db as existing_db
    db = existing_db
except ImportError:
    import os
    from pymongo import MongoClient
    from dotenv import load_dotenv
    load_dotenv()
    MONGO_URI = os.getenv("MONGO_URI", "")
    client = MongoClient(MONGO_URI, tz_aware=True) if MONGO_URI else None
    db = client["ipo_scanner_v2"] if client else None

logger = logging.getLogger(__name__)

DATA_VERSION = "v2"

class MongoRepository:
    def __init__(self):
        self.signals_v2 = db["signals_v2"] if db is not None else None
        self.signal_updates = db["signal_updates"] if db is not None else None
        self.signal_outcomes = db["signal_outcomes"] if db is not None else None
        self._ensure_indexes()

    def _ensure_indexes(self):
        """Lock the data contract with mandatory unique indexes."""
        if self.signals_v2 is not None:
            # Deterministic ID is our primary key
            self.signals_v2.create_index([("signal_id", ASCENDING)], unique=True)
            self.signals_v2.create_index([("symbol", ASCENDING), ("signal_date", DESCENDING)])
            self.signals_v2.create_index([("created_at", DESCENDING)])

        if self.signal_updates is not None:
            # Prevent duplicate updates for the same signal on the same day
            self.signal_updates.create_index(
                [("signal_id", ASCENDING), ("date", ASCENDING)], 
                unique=True
            )

        if self.signal_outcomes is not None:
            self.signal_outcomes.create_index([("signal_id", ASCENDING)], unique=True)

    def generate_deterministic_id(self, symbol: str, breakout_date: datetime) -> str:
        """symbol_YYYYMMDD format ensures idempotency."""
        ds = breakout_date.strftime("%Y%m%d")
        return f"{symbol}_{ds}"

    def save_signal(self, signal: Signal) -> bool:
        """Immutable snapshot: Save only if it doesn't exist."""
        if self.signals_v2 is None:
            return False

        doc = signal.to_dict()
        doc["data_version"] = DATA_VERSION
        doc["created_at"] = datetime.now(timezone.utc)

        try:
            self.signals_v2.insert_one(doc)
            logger.info(f"✅ [Repo] Signal saved: {signal.signal_id}")
            return True
        except DuplicateKeyError:
            logger.warning(f"⚠️ [Repo] Duplicate signal ignored: {signal.signal_id}")
            return False
        except Exception as e:
            logger.error(f"❌ [Repo] Failed to save signal {signal.signal_id}: {e}")
            return False

    def save_update(self, update_doc: dict) -> bool:
        """Append-only lifecycle updates."""
        if self.signal_updates is None:
            return False

        update_doc["data_version"] = DATA_VERSION
        update_doc["logged_at"] = datetime.now(timezone.utc)

        try:
            self.signal_updates.insert_one(update_doc)
            return True
        except DuplicateKeyError:
            # This is fine: it means we already updated this signal today
            return False
        except Exception as e:
            logger.error(f"❌ [Repo] Failed to save update for {update_doc.get('signal_id')}: {e}")
            return False

    def save_outcome(self, outcome_doc: dict) -> bool:
        """Final summary snapshot."""
        if self.signal_outcomes is None:
            return False
            
        outcome_doc["data_version"] = DATA_VERSION
        outcome_doc["updated_at"] = datetime.now(timezone.utc)
        
        try:
            self.signal_outcomes.replace_one(
                {"signal_id": outcome_doc["signal_id"]},
                outcome_doc,
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"❌ [Repo] Failed to save outcome for {outcome_doc.get('signal_id')}: {e}")
            return False
