#!/usr/bin/env python3
"""
utils.py

Utility functions for Upstox API integration
"""

import os
import time
import threading
import pandas as pd
import requests
import logging

logger = logging.getLogger(__name__)

# Global rate limiter for Upstox API
_upstox_last_request = 0.0
_upstox_lock = threading.Lock()

def fetch_from_upstox(symbol, start_date, end_date):
    """Fetch historical data from Upstox API with rate limiting"""
    try:
        # Load IPO mappings from MongoDB
        try:
            from db import get_instrument_key_mapping
            symbol_mapping = get_instrument_key_mapping()
        except Exception as e:
            logger.error(f"Error getting Upstox mapping from DB: {e}")
            return None

        if not symbol_mapping or symbol not in symbol_mapping:
            logger.warning(f"Symbol {symbol} not found in Upstox mapping (MongoDB)")
            return None
        
        instrument_key = symbol_mapping[symbol]

        
        # Get Upstox credentials
        access_token = os.getenv('UPSTOX_ACCESS_TOKEN')
        if not access_token:
            logger.warning("Upstox access token not found")
            return None
        
        # Prepare API request
        headers = {
            'Accept': 'application/json',
            'Api-Version': '2.0',
            'Authorization': f'Bearer {access_token}'
        }
        
        from_str = start_date.strftime('%Y-%m-%d')
        to_str = end_date.strftime('%Y-%m-%d')
        url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/day/{to_str}/{from_str}"
        
        # Global rate limiting: Ensure minimum 100ms between Upstox API requests
        global _upstox_last_request
        with _upstox_lock:
            current_time = time.time()
            time_since_last = current_time - _upstox_last_request
            if time_since_last < 0.1:  # 100ms minimum delay
                time.sleep(0.1 - time_since_last)
            _upstox_last_request = time.time()
        
        logger.info(f"🔄 Trying Upstox API for {symbol}")
        response = requests.get(url, headers=headers)
        
        # Handle rate limiting (429 Too Many Requests)
        if response.status_code == 429:
            logger.warning(f"⚠️ Rate limited for {symbol}, waiting 1 second...")
            time.sleep(1)
            response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            if 'data' in data and 'candles' in data['data']:
                candles = data['data']['candles']
                if candles:
                    # Convert to DataFrame
                    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close'])
                    
                    # Handle timestamp conversion - try different formats
                    try:
                        # Try Unix timestamp first
                        df['DATE'] = pd.to_datetime(df['timestamp'], unit='s')
                    except:
                        try:
                            # Try ISO format
                            df['DATE'] = pd.to_datetime(df['timestamp'])
                        except:
                            # Try string format
                            df['DATE'] = pd.to_datetime(df['timestamp'], format='%Y-%m-%d')
                    
                    df.columns = ['timestamp', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME', 'IGNORE', 'DATE']
                    
                    # Select required columns and add LTP column (use CLOSE as LTP)
                    df = df[['DATE', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']]
                    df['LTP'] = df['CLOSE']  # Add LTP column using CLOSE price
                    
                    # Ensure DATE is datetime (should already be, but verify for consistency)
                    if not pd.api.types.is_datetime64_any_dtype(df['DATE']):
                        df['DATE'] = pd.to_datetime(df['DATE'])
                    
                    # Sort by date ascending (oldest to newest) to ensure consistent ordering
                    df = df.sort_values('DATE').reset_index(drop=True)
                    
                    logger.info(f"✅ Upstox API: Got {len(df)} candles for {symbol}")
                    return df
        
        logger.warning(f"⚠️ Upstox API: No data for {symbol}")
        return None
        
    except Exception as e:
        logger.warning(f"⚠️ Upstox API error for {symbol}: {e}")
        return None

def fetch_nifty_from_upstox(start_date, end_date):
    """Fetch Nifty 50 index data from Upstox API."""
    import requests
    access_token = os.getenv('UPSTOX_ACCESS_TOKEN')
    if not access_token:
        logger.warning("Upstox access token not found for Nifty fetching")
        return None
    
    headers = {
        'Accept': 'application/json',
        'Api-Version': '2.0',
        'Authorization': f'Bearer {access_token}'
    }
    
    from_str = start_date.strftime('%Y-%m-%d')
    to_str = end_date.strftime('%Y-%m-%d')
    url = f"https://api.upstox.com/v2/historical-candle/NSE_INDEX|Nifty 50/day/{to_str}/{from_str}"
    
    try:
        logger.info("🔄 Trying Upstox API for Nifty 50 index")
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if 'data' in data and 'candles' in data['data']:
                candles = data['data']['candles']
                if candles:
                    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close'])
                    
                    try:
                        df['DATE'] = pd.to_datetime(df['timestamp'])
                    except Exception:
                        df['DATE'] = pd.to_datetime(df['timestamp'], format='%Y-%m-%d')
                    
                    df.columns = ['timestamp', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME', 'IGNORE', 'DATE']
                    df = df[['DATE', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']]
                    df['LTP'] = df['CLOSE']
                    
                    # Ensure DATE is timezone naive to match yfinance output style
                    df['DATE'] = pd.to_datetime(df['DATE']).dt.tz_localize(None)
                    df = df.sort_values('DATE').reset_index(drop=True)
                    df.set_index('DATE', inplace=True)
                    logger.info(f"✅ Upstox API: Got {len(df)} Nifty 50 candles")
                    return df
        else:
            logger.warning(f"Upstox Nifty index query failed with status code {response.status_code}")
    except Exception as e:
        logger.warning(f"⚠️ Upstox API error for Nifty: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic NSE Trading Holiday Management & Notification Deduplication
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime, date, timezone, timedelta
from typing import Optional, Union, Set

IST = timezone(timedelta(hours=5, minutes=30))

# Thread-safe in-memory cache: {year: (cached_at_datetime, set_of_dates)}
_NSE_HOLIDAYS_MEM_CACHE: dict = {}
_HOLIDAYS_MEM_LOCK = threading.Lock()

# Curated static baseline (safe offline fallback)
_STATIC_NSE_HOLIDAYS_BASELINE = {
    # 2025 holidays
    "2025-01-26", "2025-02-26", "2025-03-14", "2025-04-10", "2025-04-14",
    "2025-04-18", "2025-05-01", "2025-08-15", "2025-08-27", "2025-10-02",
    "2025-10-24", "2025-10-28", "2025-11-05", "2025-12-25",
    # 2026 holidays
    "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-08-15",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
}

def get_dynamic_nse_holidays(year: Optional[int] = None, force_refresh: bool = False) -> Set[str]:
    """
    Dynamically retrieve official NSE trading holidays for the given year.
    Hierarchy:
      1. Memory cache (if valid for 1 day)
      2. MongoDB cache (`market_holidays` collection, refreshed every 7 days)
      3. Live Upstox Market Holidays API (https://api.upstox.com/v2/market/holidays)
      4. Offline verified static baseline fallback
    """
    if year is None:
        year = datetime.now(IST).year

    # 1. Check in-memory cache
    with _HOLIDAYS_MEM_LOCK:
        if not force_refresh and year in _NSE_HOLIDAYS_MEM_CACHE:
            cached_time, holidays_set = _NSE_HOLIDAYS_MEM_CACHE[year]
            if (datetime.now(timezone.utc) - cached_time).total_seconds() < 86400:
                return holidays_set

    # 2. Check MongoDB cache
    try:
        from db import market_holidays_col
        if market_holidays_col is not None and not force_refresh:
            doc = market_holidays_col.find_one({"year": int(year)})
            if doc and "holidays" in doc and doc["holidays"]:
                updated_at = doc.get("updated_at")
                # If updated within 7 days, accept DB cache
                if updated_at:
                    if not updated_at.tzinfo:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
                    if age_seconds < 7 * 86400:
                        holidays_set = set(doc["holidays"])
                        with _HOLIDAYS_MEM_LOCK:
                            _NSE_HOLIDAYS_MEM_CACHE[year] = (datetime.now(timezone.utc), holidays_set)
                        return holidays_set
    except Exception as e:
        logger.debug(f"DB holiday cache lookup skipped: {e}")

    # 3. Attempt fetch from Upstox Market Holidays API
    fetched_holidays = set()
    try:
        access_token = os.getenv("UPSTOX_ACCESS_TOKEN")
        headers = {
            "Accept": "application/json",
            "Api-Version": "2.0"
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        api_url = "https://api.upstox.com/v2/market/holidays"
        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            res_json = resp.json()
            data_list = res_json.get("data", [])
            for item in data_list:
                h_date = item.get("date")  # YYYY-MM-DD
                closed_exchanges = item.get("closed_exchanges", [])
                h_type = item.get("holiday_type", "TRADING_HOLIDAY")
                
                # Check if exchange is closed for NSE equity trading
                is_nse_closed = (
                    "NSE" in closed_exchanges or
                    "NFO" in closed_exchanges or
                    "NSE_EQ" in closed_exchanges or
                    h_type == "TRADING_HOLIDAY"
                )
                
                if h_date and is_nse_closed:
                    if str(year) in h_date:
                        fetched_holidays.add(h_date)

            if fetched_holidays:
                logger.info(f"✅ Dynamically fetched {len(fetched_holidays)} NSE holidays for {year} from Upstox API.")
                # Upsert into MongoDB
                try:
                    from db import market_holidays_col
                    if market_holidays_col is not None:
                        market_holidays_col.update_one(
                            {"year": int(year)},
                            {"$set": {
                                "year": int(year),
                                "holidays": sorted(list(fetched_holidays)),
                                "updated_at": datetime.now(timezone.utc),
                                "source": "upstox_api"
                            }},
                            upsert=True
                        )
                except Exception as db_err:
                    logger.debug(f"Failed to cache holidays in DB: {db_err}")

                with _HOLIDAYS_MEM_LOCK:
                    _NSE_HOLIDAYS_MEM_CACHE[year] = (datetime.now(timezone.utc), fetched_holidays)
                return fetched_holidays
    except Exception as api_err:
        logger.warning(f"⚠️ Could not dynamically fetch NSE holidays from Upstox API: {api_err}")

    # 4. Fallback to MongoDB existing doc even if stale
    try:
        from db import market_holidays_col
        if market_holidays_col is not None:
            doc = market_holidays_col.find_one({"year": int(year)})
            if doc and "holidays" in doc and doc["holidays"]:
                holidays_set = set(doc["holidays"])
                with _HOLIDAYS_MEM_LOCK:
                    _NSE_HOLIDAYS_MEM_CACHE[year] = (datetime.now(timezone.utc), holidays_set)
                return holidays_set
    except Exception:
        pass

    # 5. Fallback to static baseline filtered by year
    year_prefix = f"{year}-"
    baseline = {d for d in _STATIC_NSE_HOLIDAYS_BASELINE if d.startswith(year_prefix)}
    with _HOLIDAYS_MEM_LOCK:
        _NSE_HOLIDAYS_MEM_CACHE[year] = (datetime.now(timezone.utc), baseline)
    return baseline


def is_market_day(check_date: Optional[Union[date, datetime, str]] = None) -> bool:
    """
    Check if a given date is an active NSE trading day (not a weekend, not an NSE holiday).
    Fails open (returns True) on unexpected error so live market trading is never blocked by a transient issue.
    """
    try:
        if check_date is None:
            target_date = datetime.now(IST).date()
        elif isinstance(check_date, str):
            target_date = datetime.strptime(check_date, "%Y-%m-%d").date()
        elif isinstance(check_date, datetime):
            target_date = check_date.date()
        elif isinstance(check_date, date):
            target_date = check_date
        else:
            return True

        # Check weekend: 5=Saturday, 6=Sunday
        if target_date.weekday() >= 5:
            return False

        # Check dynamic NSE holiday list
        holidays = get_dynamic_nse_holidays(target_date.year)
        date_str = target_date.strftime("%Y-%m-%d")
        if date_str in holidays:
            return False

        return True
    except Exception as e:
        logger.warning(f"⚠️ [is_market_day] Exception during market day check ({e}). Failing open.")
        return True


def get_last_trading_day(target_date: Optional[Union[date, datetime, str]] = None) -> date:
    """Calculate the most recent date that was NOT a weekend or an NSE holiday."""
    if target_date is None:
        target_date = datetime.now(IST).date()
    elif isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    elif isinstance(target_date, datetime):
        target_date = target_date.date()

    check_date = target_date - timedelta(days=1)
    for _ in range(30):  # safety walk-back limit
        if check_date.weekday() < 5 and not (check_date.strftime("%Y-%m-%d") in get_dynamic_nse_holidays(check_date.year)):
            return check_date
        check_date -= timedelta(days=1)

    return target_date - timedelta(days=1)


def send_holiday_notification_once(scanner_name: str, today_str: Optional[str] = None, send_telegram_fn=None) -> bool:
    """
    Send a market holiday Telegram notification exactly once per day across all scanners.
    Subsequent runs by any scanner on the same day will log cleanly and suppress duplicate messages.
    Returns:
        True if notification was sent.
        False if notification was suppressed because it was already sent today.
    """
    if not today_str:
        today_str = datetime.now(IST).strftime("%Y-%m-%d")

    # 1. Check MongoDB system_audits_col
    try:
        from db import system_audits_col
        if system_audits_col is not None:
            existing = system_audits_col.find_one({
                "audit_type": "HOLIDAY_NOTIFICATION_SENT",
                "date": today_str
            })
            if existing:
                triggered_by = existing.get("triggered_by", "another scanner")
                logger.info(f"📅 Market is closed today ({today_str}). Notification already sent for today (by {triggered_by}). Suppressing duplicate Telegram alert.")
                return False
    except Exception as db_err:
        logger.debug(f"DB check for holiday notification skipped: {db_err}")

    # 2. Check local filesystem log directory fallback
    marker_dir = os.path.join("logs", today_str)
    marker_file = os.path.join(marker_dir, "holiday_notification.sent")
    if os.path.exists(marker_file):
        logger.info(f"📅 Market is closed today ({today_str}). Local marker exists. Suppressing duplicate Telegram alert.")
        return False

    # 3. Format message and send notification
    skip_msg = (
        f"📅 <b>NSE Market Holiday / Non-Trading Day</b>\n\n"
        f"🗓 <b>Date:</b> {today_str}\n"
        f"⏸ <b>Status:</b> Market closed — automated scans paused.\n"
        f"✅ Scanners will resume on the next trading session."
    )

    logger.info(f"📅 Market is closed today ({today_str}). Sending single daily holiday notification (triggered by {scanner_name}).")

    if send_telegram_fn is not None:
        try:
            send_telegram_fn(skip_msg)
        except Exception as e:
            logger.error(f"Error sending holiday telegram: {e}")

    # 4. Record deduplication marker in MongoDB
    try:
        from db import system_audits_col
        if system_audits_col is not None:
            audit_id = f"holiday_alert_{today_str}"
            system_audits_col.update_one(
                {"audit_id": audit_id},
                {"$set": {
                    "audit_id": audit_id,
                    "audit_type": "HOLIDAY_NOTIFICATION_SENT",
                    "date": today_str,
                    "timestamp": datetime.now(timezone.utc),
                    "triggered_by": scanner_name
                }},
                upsert=True
            )
    except Exception as db_save_err:
        logger.debug(f"Could not persist holiday notification record in DB: {db_save_err}")

    # 5. Create local file marker
    try:
        os.makedirs(marker_dir, exist_ok=True)
        with open(marker_file, "w", encoding="utf-8") as f:
            f.write(f"Sent by {scanner_name} at {datetime.now(timezone.utc).isoformat()}\n")
    except Exception:
        pass

    return True



