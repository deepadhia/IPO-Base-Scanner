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


