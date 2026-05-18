#!/usr/bin/env python3
"""
streamlined_ipo_scanner.py

Optimized IPO breakout scanner:
- Dynamic symbol list (recent IPOs + active positions)
- Enhanced entry filters and grading
- Dynamic partial profit taking by grade
- SuperTrend trailing stops for winners
- Smart exit logic (stop-loss, persistent losers)
- Weekly and monthly summary commands
- Dry-run and heartbeat modes
"""

SCANNER_VERSION = "2.5.0"  # Behavioral Research Engine Upgrade
LOG_SCHEMA_VERSION = "2026-04-23.v1"

import os
import sys
import argparse
import pickle
import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from jugaad_data.nse.history import stock_raw
import pandas as pd
import threading

# Institutional Analytics & Enrichment (Phase 2.2 Upgrade)
try:
    from core.repository import MongoRepository
    from integration.signal_builder import SignalBuilder
    from lifecycle.tracker import LifecycleTracker
    from lifecycle.evaluator import evaluate_signal_outcome
    ANALYTICS_AVAILABLE = True
except ImportError as e:
    print(f"⚠️ Analytics modules not found: {e}")
    ANALYTICS_AVAILABLE = False

# Try to import yfinance, fallback if not available
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

class IntegrationBridge:
    """Bridge between legacy scanner loops and v2 institutional telemetry."""
    def __init__(self):
        try:
            from core.repository import MongoRepository
            from integration.signal_builder import SignalBuilder
            self.repo = MongoRepository()
            self.builder = SignalBuilder()
            self.active = True
        except Exception as e:
            # We don't want to crash the main scanner if bridge fails
            import logging
            logging.getLogger(__name__).warning(f"🏛️ Institutional Bridge failed to initialize: {e}")
            self.active = False

    def save_signal(self, raw_payload, candle=None, history=None, base_candles=None):
        """Builds and saves an institutional signal from raw scanner data."""
        if not self.active:
            return False
        try:
            # Fallback for data objects if not passed explicitly
            c = candle if candle is not None else raw_payload.get('_candle')
            h = history if history is not None else raw_payload.get('_history')
            b = base_candles if base_candles is not None else raw_payload.get('_base_candles', h)
            
            # Fetch sector/industry if missing
            if 'sector' not in raw_payload or raw_payload['sector'] == 'Unknown':
                try:
                    import yfinance as yf
                    ticker = yf.Ticker(f"{raw_payload['symbol']}.NS")
                    raw_payload['sector'] = ticker.info.get('sector', 'Unknown')
                    raw_payload['industry'] = ticker.info.get('industry', 'Unknown')
                except:
                    pass

            # Use global SCANNER_VERSION
            signal_v2 = self.builder.build_signal(raw_payload, c, h, b, SCANNER_VERSION)
            self.repo.save_signal(signal_v2)
            return True
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"🏛️ IntegrationBridge Save Failed: {e}")
            return False

# ── Research Metadata Helpers (Phase 2.3) ──────────────────────────────────
# Pattern archetypes are observational labels ONLY — not separate strategies.
PATTERN_IPO_DISCOVERY         = "PATTERN_IPO_DISCOVERY"
PATTERN_CONSOLIDATION_BREAKOUT = "PATTERN_CONSOLIDATION_BREAKOUT"
PATTERN_RUNAWAY_GAP           = "PATTERN_RUNAWAY_GAP"
PATTERN_EARLY_CONTINUATION    = "PATTERN_EARLY_CONTINUATION"
PATTERN_RECOVERY_BREAKOUT     = "PATTERN_RECOVERY_BREAKOUT"

# --- Phase 3: Model Validation Buckets (Scientific Research Refactor) ---
BUCKET_ALIGNED  = "ALIGNED"   # Meets all current heuristic alpha rules
BUCKET_EXTENDED = "EXTENDED"  # Structurally valid but Out-of-Sample (for research)
BUCKET_BROKEN   = "BROKEN"    # Structurally flawed or erratic action

# --- Reason Codes (Hard Fields for Analytics) ---
RC_OK                 = "OK"
RC_PRNG_LIMIT         = "PRNG_LIMIT"         # PRNG > 25.0
RC_VOL_LIMIT          = "VOL_LIMIT"          # Volume ratio < 1.2
RC_NON_IPO_CONTEXT    = "NON_IPO_CONTEXT"    # Days since listing > LISTING_MAX_DAYS_SINCE_LISTING
RC_STRUCTURE_FAILED   = "STRUCTURE_FAILED"   # Price broke base low during window
RC_ERRATIC_VOLATILITY = "ERRATIC_VOLATILITY" # PRNG > 45.0 (No longer a base)
RC_LIQUIDITY_TRAP     = "LIQUIDITY_TRAP"     # Low turnover or frequent circuits
RC_MICROCAP_PENALTY   = "MICROCAP_PENALTY"   # Market cap below floor

def categorize_signal_bucket(metrics: dict, days_since_listing: int) -> tuple:
    """
    Objective signal categorization into ALIGNED, EXTENDED, or BROKEN.
    Returns (bucket, reason_codes_list).
    """
    reasons = []
    prng = metrics.get("prng", 0)
    vol_ratio = metrics.get("vol_ratio", 0)
    
    # 1. Structural Checks (BROKEN)
    if prng > 45.0:
        reasons.append(RC_ERRATIC_VOLATILITY)
    if days_since_listing > LISTING_MAX_DAYS_SINCE_LISTING:
        reasons.append(RC_NON_IPO_CONTEXT)
    
    if reasons:
        return BUCKET_BROKEN, reasons

    # 2. Heuristic Alignment Checks (EXTENDED)
    if prng > 25.0:
        reasons.append(RC_PRNG_LIMIT)
    if vol_ratio < 1.2:
        reasons.append(RC_VOL_LIMIT)
    
    if reasons:
        return BUCKET_EXTENDED, reasons
    
    # 3. Strategy Alignment (EXTENDED)
    if days_since_listing < MIN_AGE_DAYS:
        reasons.append("ULTRA_FRESH_IPO")
    
    # 4. Institutional Liquidity Hardening (Phase 2.5)
    mcap = metrics.get("market_cap_cr", 0)
    turnover = metrics.get("avg_turnover_cr", 0)
    circuits = metrics.get("circuit_days_15", 0)
    
    if turnover > 0 and turnover < MIN_DAILY_TURNOVER_CR:
        reasons.append(RC_LIQUIDITY_TRAP)
    if circuits >= 3:
        reasons.append(f"{RC_LIQUIDITY_TRAP}_CIRCUITS")
    if mcap > 0 and mcap < MIN_MARKET_CAP_CR:
        reasons.append(RC_MICROCAP_PENALTY)
    
    if reasons:
        # If it's a liquidity trap or microcap, it's BROKEN (Avoid capital lock-in)
        if RC_LIQUIDITY_TRAP in reasons or f"{RC_LIQUIDITY_TRAP}_CIRCUITS" in reasons:
            return BUCKET_BROKEN, reasons
        return BUCKET_EXTENDED, reasons
    
    return BUCKET_ALIGNED, [RC_OK]

def classify_pattern_type(grade: str, days_since_listing: int, vol_ratio: float, prng: float) -> str:
    """Assign an observational pattern archetype. Classification ONLY — no live logic."""
    if grade == "LISTING_BREAKOUT" or days_since_listing <= 10:
        return PATTERN_IPO_DISCOVERY
    if vol_ratio >= 5.0 and prng < 8:
        return PATTERN_RUNAWAY_GAP
    if days_since_listing <= 30 and prng < 15:
        return PATTERN_EARLY_CONTINUATION
    if prng > 25:
        return PATTERN_RECOVERY_BREAKOUT
    return PATTERN_CONSOLIDATION_BREAKOUT

# Global cache for Nifty data to avoid redundant API calls
_nifty_regime_cache = {}

def get_market_regime(target_date=None):
    """
    Automated Nifty-based regime detection.
    Classification:
    - BULL: Price > 20-EMA (with buffer) and 20-EMA > 50-EMA
    - WEAK_BULL: Price > 50-EMA (with buffer) but below 20-EMA
    - RANGE: Price within 0.2% of either EMA (Transition zone)
    - CORRECTION: Price below 50-EMA (with buffer)
    - UNKNOWN: Data fetch failed
    """
    try:
        # Buffer for regime stability (0.2% tolerance)
        TOLERANCE = 0.002
        
        # Default to today if no date provided
        if target_date is None:
            target_date = datetime.today().date()
        elif hasattr(target_date, 'date'):
            target_date = target_date.date()
        
        # Check cache (key is date string)
        date_key = target_date.strftime('%Y-%m-%d')
        if date_key in _nifty_regime_cache:
            return _nifty_regime_cache[date_key]
            
        # Fetch Nifty data (^NSEI for Nifty 50)
        start_dt = target_date - timedelta(days=150)
        end_dt = target_date + timedelta(days=1)
        
        # Use yfinance with auto_adjust=False to avoid FutureWarnings
        df_nifty = yf.download("^NSEI", start=start_dt, end=end_dt, progress=False, auto_adjust=False)
        if df_nifty is None or df_nifty.empty:
            return "UNKNOWN"
            
        # Reset index if MultiIndex columns exist
        df_nifty.columns = [c[0] if isinstance(c, tuple) else c for c in df_nifty.columns]
        
        # Calculate EMAs
        df_nifty['EMA20'] = df_nifty['Close'].ewm(span=20, adjust=False).mean()
        df_nifty['EMA50'] = df_nifty['Close'].ewm(span=50, adjust=False).mean()
        
        latest = df_nifty.iloc[-1]
        price = float(latest['Close'])
        ema20 = float(latest['EMA20'])
        ema50 = float(latest['EMA50'])
        
        # Calculate distance from EMAs
        dist_20 = (price / ema20) - 1
        dist_50 = (price / ema50) - 1
        
        # Priority 1: Range/Neutral check
        if abs(dist_20) < TOLERANCE or abs(dist_50) < TOLERANCE:
            regime = "RANGE"
        # Priority 2: Directional trends
        elif price > ema20 and ema20 > ema50:
            regime = "BULL"
        elif price > ema50:
            regime = "WEAK_BULL"
        else:
            regime = "CORRECTION"
            
        # Cache for next time
        _nifty_regime_cache[date_key] = regime
        return regime
        
    except Exception as e:
        logger.warning(f"⚠️ Market regime detection failed: {e}")
        return "UNKNOWN"

# Global rate limiters for APIs
_upstox_last_request = 0.0
_upstox_lock = threading.Lock()
_yfinance_last_request = 0.0
_yfinance_lock = threading.Lock()
_yfinance_min_delay = 0.2  # 200ms minimum delay between yfinance requests

def auto_refresh_upstox_token():
    """Automatically refresh Upstox token before the scan runs if refresh token is present."""
    refresh_token = os.getenv('UPSTOX_REFRESH_TOKEN')
    client_id = os.getenv('UPSTOX_API_KEY') or os.getenv('UPSTOX_CLIENT_ID')
    client_secret = os.getenv('UPSTOX_API_SECRET') or os.getenv('UPSTOX_CLIENT_SECRET')
    redirect_uri = os.getenv('UPSTOX_REDIRECT_URI', 'https://127.0.0.1')
    
    if not refresh_token or not client_id or not client_secret:
        return
        
    url = 'https://api.upstox.com/v2/login/authorization/token'
    headers = {
        'accept': 'application/json',
        'Api-Version': '2.0',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'code': '',
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token
    }
    
    try:
        response = requests.post(url, headers=headers, data=data, timeout=10)
        if response.status_code == 200:
            token_data = response.json()
            new_access_token = token_data.get('access_token')
            if new_access_token:
                # Update environment variable for the current process
                os.environ['UPSTOX_ACCESS_TOKEN'] = new_access_token
                logger.info("✅ Successfully refreshed Upstox access token")
        else:
            logger.error(f"❌ Failed to refresh Upstox token: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"❌ Error refreshing Upstox token: {e}")

def stock_df(symbol, from_date, to_date, series="EQ"):
    """Custom stock_df function that handles column mapping correctly"""
    try:
        # Get raw data
        raw = stock_raw(symbol, from_date, to_date, series)
        
        if not raw:
            return pd.DataFrame()
        
        # Create DataFrame from raw data
        df = pd.DataFrame(raw)
        
        # Map old column names to new ones
        column_mapping = {
            'CH_TIMESTAMP': 'DATE',
            'CH_SERIES': 'SERIES',
            'CH_OPENING_PRICE': 'OPEN',
            'CH_TRADE_HIGH_PRICE': 'HIGH',
            'CH_TRADE_LOW_PRICE': 'LOW',
            'CH_PREVIOUS_CLS_PRICE': 'PREV. CLOSE',
            'CH_LAST_TRADED_PRICE': 'LTP',
            'CH_CLOSING_PRICE': 'CLOSE',
            'VWAP': 'VWAP',
            'CH_52WEEK_HIGH_PRICE': '52W H',
            'CH_52WEEK_LOW_PRICE': '52W L',
            'CH_TOT_TRADED_QTY': 'VOLUME',
            'CH_TOT_TRADED_VAL': 'VALUE',
            'CH_TOTAL_TRADES': 'NO OF TRADES',
            'CH_SYMBOL': 'SYMBOL'
        }
        
        # Rename columns (only rename columns that exist)
        existing_mapping = {k: v for k, v in column_mapping.items() if k in df.columns}
        df = df.rename(columns=existing_mapping)
        
        # Select only the columns we need (only if they exist)
        required_columns = ['DATE', 'SERIES', 'OPEN', 'HIGH', 'LOW', 'PREV. CLOSE', 'LTP', 'CLOSE', 'VWAP', '52W H', '52W L', 'VOLUME', 'VALUE', 'NO OF TRADES', 'SYMBOL']
        available_columns = [col for col in required_columns if col in df.columns]
        
        # Ensure we have at least the essential columns
        essential_columns = ['DATE', 'OPEN', 'HIGH', 'LOW', 'CLOSE']
        missing_essential = [col for col in essential_columns if col not in df.columns]
        if missing_essential:
            logger.error(f"Missing essential columns in stock_df for {symbol}: {missing_essential}")
            return pd.DataFrame()
        
        # Select available columns
        df = df[available_columns]
        
        # Add LTP if missing (use CLOSE as fallback)
        if 'LTP' not in df.columns and 'CLOSE' in df.columns:
            df['LTP'] = df['CLOSE']
        
        # Add VOLUME if missing (set to 0)
        if 'VOLUME' not in df.columns:
            df['VOLUME'] = 0
        
        # Convert DATE to datetime
        df['DATE'] = pd.to_datetime(df['DATE'])
        
        return df
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in custom stock_df for {symbol}: {error_msg}")
        if "Expecting value" in error_msg or "Max retries exceeded" in error_msg:
            return "FATAL_API_ERROR"
        return pd.DataFrame()
from fetch import fetch_recent_ipo_symbols
# Import only the functions we need, not the entire module
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Import functions without executing hybrid.py
def supertrend(df, p=10, m=3.0):
    hl = (df['HIGH']+df['LOW'])/2
    tr = pd.concat([df['HIGH']-df['LOW'],
                    abs(df['HIGH']-df['CLOSE'].shift()),
                    abs(df['LOW']-df['CLOSE'].shift())], axis=1).max(axis=1)
    atr = tr.rolling(p).mean()
    ub, lb = hl + m*atr, hl - m*atr
    # Explicit dtype avoids pandas FutureWarning for empty Series default dtype
    st = pd.Series(index=df.index, dtype="float64")
    for i in range(1, len(df)):
        if df['CLOSE'].iat[i] <= lb.iat[i]:
            st.iat[i] = ub.iat[i]
        elif df['CLOSE'].iat[i] >= ub.iat[i]:
            st.iat[i] = lb.iat[i]
        else:
            st.iat[i] = st.iat[i-1]
    return st

import pandas as pd
import numpy as np

def sanitize_metric(val):
    """Cast pandas/numpy variables to python natives for clean JSON logging"""
    if pd.isna(val) or val is None:
        return None
    try:
        if hasattr(val, 'item'):
            return val.item()
        if isinstance(val, (int, float, bool, str)):
            return val
        return float(val)
    except:
        return str(val)

def compute_grade_hybrid(df, idx, w, avg_vol):
    score=0
    low, high = df['LOW'].tail(w).min(), df['HIGH'].tail(w).max()
    prng = (high-low)/low*100
    if prng<=18: score+=1
    
    vol_ratio = df['VOLUME'].iat[idx]/avg_vol if avg_vol>0 else 0
    if df['VOLUME'].iat[idx]>=2.5*avg_vol and df['VOLUME'].iloc[idx-2:idx+1].sum()>=4*avg_vol: score+=1
    
    ret20 = (df['CLOSE'].iat[idx]/df['CLOSE'].iat[max(0,idx-20)]-1)
    percentile=np.percentile((df['CLOSE']-df['CLOSE'].shift(20))/df['CLOSE'].shift(20).fillna(0),85)
    rs_percentile_met = bool(ret20>=percentile)
    if rs_percentile_met: score+=1
    
    ema20,ema50 = df['CLOSE'].ewm(20).mean().iat[idx], df['CLOSE'].ewm(50).mean().iat[idx]
    macd = df['CLOSE'].ewm(12).mean().iat[idx] - df['CLOSE'].ewm(26).mean().iat[idx]
    sig = pd.Series(df['CLOSE'].ewm(12).mean()-df['CLOSE'].ewm(26).mean()).ewm(9).mean().iat[idx]
    rsi = 100-100/(1+(df['CLOSE'].diff().clip(lower=0).rolling(14).mean()/
                     df['CLOSE'].diff().clip(upper=0).abs().rolling(14).mean())).iat[idx]
    
    trend_alignment = bool(macd>sig and rsi>65 and ema20>ema50)
    if trend_alignment: score+=1
    if idx+1<len(df) and (df['OPEN'].iat[idx+1]/df['CLOSE'].iat[idx]-1)>=0.04: score+=1
    
    metrics_dict = {
        "metric_prng": sanitize_metric(prng),
        "metric_vol_ratio": sanitize_metric(vol_ratio),
        "metric_rsi": sanitize_metric(rsi),
        "metric_base_width": sanitize_metric(w),
        "metric_rs_percentile_met": sanitize_metric(rs_percentile_met),
        "metric_trend_alignment": sanitize_metric(trend_alignment)
    }

    return score, metrics_dict

def assign_grade(score):
    if score>=4: return 'A+'
    if score>=2: return 'B'
    if score>=1: return 'C'
    return 'D'

# Helper for live-grade filtering
GRADE_ORDER = ["D", "C", "B", "A", "A+"]

def is_live_grade_allowed(grade: str) -> bool:
    """Return True if grade meets MIN_LIVE_GRADE threshold for LIVE signals."""
    try:
        return GRADE_ORDER.index(grade) >= GRADE_ORDER.index(MIN_LIVE_GRADE)
    except ValueError:
        # Unknown grade: be conservative and reject
        return False

# NSE official trading holidays for 2025 and 2026
# Source: NSE India holiday calendar
NSE_HOLIDAYS = {
    # 2025 holidays
    "2025-01-26",  # Republic Day
    "2025-02-26",  # Mahashivratri
    "2025-03-14",  # Holi
    "2025-04-10",  # Id-Ul-Fitr (Ramadan Eid)
    "2025-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2025-04-18",  # Good Friday
    "2025-05-01",  # Maharashtra Day
    "2025-08-15",  # Independence Day
    "2025-08-27",  # Ganesh Chaturthi
    "2025-10-02",  # Mahatma Gandhi Jayanti
    "2025-10-02",  # Dussehra
    "2025-10-24",  # Diwali Laxmi Pujan
    "2025-10-28",  # Diwali Balipratipada
    "2025-11-05",  # Prakash Gurpurb Sri Guru Nanak Dev Ji
    "2025-11-15",  # ?
    "2025-12-25",  # Christmas
    # 2026 holidays
    "2026-01-26",  # Republic Day
    "2026-02-17",  # Mahashivratri
    "2026-03-03",  # Holi
    "2026-03-20",  # Id-Ul-Fitr (Ramadan Eid) - tentative
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
    "2026-08-21",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-12",  # Diwali Laxmi Pujan (tentative)
    "2026-11-13",  # Diwali Balipratipada (tentative)
    "2026-11-23",  # Prakash Gurpurb Sri Guru Nanak Dev Ji (tentative)
    "2026-12-25",  # Christmas
}

def is_market_day() -> bool:
    """Check if today is an NSE trading day (not a weekend, not a holiday).
    Returns True if the market is open today, False otherwise."""
    try:
        from datetime import timezone, timedelta as td
        ist = timezone(td(hours=5, minutes=30))
        now_ist = datetime.now(ist)
        today_str = now_ist.strftime("%Y-%m-%d")
        # Check weekend (0=Monday, 6=Sunday)
        if now_ist.weekday() >= 5:
            return False
        # Check NSE holiday list
        if today_str in NSE_HOLIDAYS:
            return False
        return True
    except Exception:
        # If check fails, assume market is open (fail-open)
        return True

def is_market_hours() -> bool:
    """Check if current IST time is within Indian market hours (9:15 AM - 3:30 PM IST)
    AND today is an NSE trading day (not a weekend or holiday).
    Returns True if within market hours on a trading day, False otherwise."""
    try:
        from datetime import timezone, timedelta as td
        ist = timezone(td(hours=5, minutes=30))
        now_ist = datetime.now(ist)
        # Check if today is a trading day first
        if not is_market_day():
            return False
        market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_open <= now_ist <= market_close
    except Exception:
        # If timezone calculation fails, assume market is open (fail-open)
        return True

def calculate_target_price(entry_price, consolidation_low, consolidation_high, grade):
    """Calculate target price based on pattern and grade"""
    # Calculate consolidation range
    consolidation_range = consolidation_high - consolidation_low
    
    # Base target multipliers by grade
    target_multipliers = {
        "A+": 1.5,  # 50% above consolidation high
        "A": 1.4,   # 40% above consolidation high
        "B": 1.3,   # 30% above consolidation high
        "C": 1.2,   # 20% above consolidation high
        "D": 1.15   # 15% above consolidation high
    }
    
    multiplier = target_multipliers.get(grade, 1.2)
    
    # Target = consolidation high + (range * multiplier)
    target = consolidation_high + (consolidation_range * multiplier)
    
    # Ensure minimum 10% return
    min_target = entry_price * 1.10
    target = max(target, min_target)
    
    return target

def calculate_grade_based_stop_loss(entry_price, consolidation_low, grade):
    """Calculate stop loss based on grade and IPO volatility"""
    # Grade-based stop loss percentages (more appropriate for IPO volatility)
    grade_stop_pcts = {
        "A+": 0.05,  # 5% - High confidence, tighter stop
        "A": 0.07,   # 7% - Good confidence
        "B": 0.10,   # 10% - Medium confidence, more room for volatility
        "C": 0.12,   # 12% - Lower confidence, more volatile
        "D": 0.15    # 15% - High risk, very volatile
    }
    
    stop_pct = grade_stop_pcts.get(grade, 0.10)  # Default 10% for unknown grades
    
    # Calculate stop below entry price
    stop_below_entry = entry_price * (1 - stop_pct)
    
    # Calculate stop below consolidation low (safer)
    stop_below_consolidation = consolidation_low * (1 - stop_pct)
    
    # Use the higher (safer) of the two
    stop_loss = max(stop_below_entry, stop_below_consolidation)
    
    # Ensure stop loss is not more than 20% below entry (maximum risk)
    max_risk_stop = entry_price * 0.80
    stop_loss = max(stop_loss, max_risk_stop)
    
    return stop_loss, stop_pct
import logging

# Load environment
load_dotenv()

# Helper functions for environment variables with robust defaults
def get_env_int(key, default):
    """Get environment variable as integer with fallback"""
    try:
        return int(os.getenv(key, default) or default)
    except (ValueError, TypeError):
        print(f"Warning: Invalid {key} value, using default: {default}")
        return default

def get_env_float(key, default):
    """Get environment variable as float with fallback"""
    try:
        return float(os.getenv(key, default) or default)
    except (ValueError, TypeError):
        print(f"Warning: Invalid {key} value, using default: {default}")
        return default

def get_liquidity_metrics(symbol, df):
    """
    Calculate turnover and detect circuit days from historical data.
    Returns (avg_turnover_cr, circuit_days_15, market_cap_cr)
    """
    global _yfinance_last_request
    try:
        # 1. Avg Turnover in Crores (Last 20 sessions)
        df_copy = df.copy()
        df_copy['turnover'] = df_copy['CLOSE'] * df_copy['VOLUME']
        avg_turnover = df_copy['turnover'].tail(20).mean()
        avg_turnover_cr = round(avg_turnover / 10000000, 2)
        
        # 2. Circuit Days in last 15 sessions
        # Heuristic: High == Low and Volume > 0
        df_copy['is_circuit'] = (df_copy['HIGH'] == df_copy['LOW']) & (df_copy['VOLUME'] > 0)
        circuit_days = int(df_copy['is_circuit'].tail(15).sum())
        
        # 3. Market Cap from yfinance
        mcap_cr = 0
        if YFINANCE_AVAILABLE:
            with _yfinance_lock:
                current_time = time.time()
                time_since_last = current_time - _yfinance_last_request
                if time_since_last < _yfinance_min_delay:
                    time.sleep(_yfinance_min_delay - time_since_last)
                
                try:
                    ticker = yf.Ticker(f"{symbol}.NS")
                    mcap = ticker.info.get('marketCap')
                    if mcap:
                        mcap_cr = round(mcap / 10000000, 2)
                except Exception as e:
                    logger.debug(f"Could not fetch market cap for {symbol}: {e}")
                
                _yfinance_last_request = time.time()
                
        return avg_turnover_cr, circuit_days, mcap_cr
    except Exception as e:
        logger.warning(f"⚠️ Error calculating liquidity metrics for {symbol}: {e}")
        return 0, 0, 0

def get_env_float(key, default):
    """Get environment variable as float with fallback"""
    try:
        return float(os.getenv(key, default) or default)
    except (ValueError, TypeError):
        print(f"Warning: Invalid {key} value, using default: {default}")
        return default

def get_env_list(key, default, separator=","):
    """Get environment variable as list with fallback"""
    try:
        value = os.getenv(key, default)
        return [int(x.strip()) for x in value.split(separator) if x.strip()]
    except (ValueError, TypeError):
        print(f"Warning: Invalid {key} value, using default: {default}")
        return [int(x.strip()) for x in default.split(separator)]

# Environment variables with robust defaults
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# Core configuration
# Global Scanner Constants
IPO_YEARS_BACK = get_env_int("IPO_YEARS_BACK", 3)
STOP_PCT = get_env_float("STOP_PCT", 0.07)  # Default 7% for IPO volatility

# Dynamic partial take per grade
PT_A_PLUS = get_env_float("PT_A_PLUS", 0.15)
PT_B = get_env_float("PT_B", 0.12)
PT_C = get_env_float("PT_C", 0.10)

# Trading parameters
CONSOL_WINDOWS = get_env_list("CONSOL_WINDOWS", "10,20,30,60,90,120")
MAX_PRNG = get_env_float("MAX_PRNG", 25.0)
VOL_MULT = get_env_float("VOL_MULT", 1.2)
ABS_VOL_MIN = get_env_int("ABS_VOL_MIN", 3000000)
LOOKAHEAD = get_env_int("LOOKAHEAD", 80)
MAX_DAYS = get_env_int("MAX_DAYS", 200)

# Institutional Universe Logic
LISTING_MAX_DAYS_SINCE_LISTING = get_env_int("LISTING_MAX_DAYS_SINCE_LISTING", 750)
MIN_AGE_DAYS = get_env_int("MIN_AGE_DAYS", 60)

# --- Phase 4: Institutional Liquidity Hardening (Phase 2.5) ---
MIN_DAILY_TURNOVER_CR = get_env_float("MIN_DAILY_TURNOVER_CR", 2.0)
MIN_MARKET_CAP_CR     = get_env_float("MIN_MARKET_CAP_CR", 500.0)
CIRCUIT_DAY_THRESHOLD = get_env_int("CIRCUIT_DAY_THRESHOLD", 3)

# Risk / reward and trailing configuration (tunable via .env)
# - MAX_ENTRY_ABOVE_BREAKOUT_PCT: max % above breakout/high we'll accept as entry
# - MIN_RISK_REWARD: minimum acceptable reward:risk ratio
# - MIN_PNL_FOR_TRAIL: minimum open P&L % before we start trailing the stop
# - MIN_TRAIL_MOVE_PCT: minimum % of entry price by which stop must improve to send an update
# - MIN_DAYS_BETWEEN_SIGNALS: cooldown between new signals for same symbol
# - MIN_LIVE_GRADE: minimum grade allowed for LIVE signals (D < C < B < A < A+)
MAX_ENTRY_ABOVE_BREAKOUT_PCT = get_env_float("MAX_ENTRY_ABOVE_BREAKOUT_PCT", 8.0)
MIN_RISK_REWARD = get_env_float("MIN_RISK_REWARD", 1.3)
MIN_PNL_FOR_TRAIL = get_env_float("MIN_PNL_FOR_TRAIL", 5.0)
MIN_TRAIL_MOVE_PCT = get_env_float("MIN_TRAIL_MOVE_PCT", 1.0)
MIN_DAYS_BETWEEN_SIGNALS = get_env_int("MIN_DAYS_BETWEEN_SIGNALS", 10)
MIN_LIVE_GRADE = os.getenv("MIN_LIVE_GRADE", "C") # Reset to C for Permissive base

# --- Meta-Observability: Config Drift Detection ──────────────────────────────
def get_last_trading_day(target_date=None):
    """Calculate the most recent date that was NOT a weekend or NSE holiday."""
    if target_date is None:
        target_date = datetime.today().date()
    
    check_date = target_date - timedelta(days=1)
    while True:
        # Check if weekend
        if check_date.weekday() >= 5: # 5=Sat, 6=Sun
            check_date -= timedelta(days=1)
            continue
        
        # Check if holiday
        if check_date.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
            check_date -= timedelta(days=1)
            continue
            
        return check_date

def get_last_expected_data_date():
    """
    Returns the date of the most recent trading session that should have EOD data.
    - If today is a trading day and it's after 6:30 PM IST, today is the last expected date.
    - Otherwise, it's the previous trading day.
    """
    from datetime import timezone, timedelta as td
    ist = timezone(td(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    today = now_ist.date()
    
    # After 6:30 PM IST, today's EOD data should be available on most providers
    if is_market_day() and (now_ist.hour > 18 or (now_ist.hour == 18 and now_ist.minute >= 30)):
        return today
    
    # Otherwise, return the previous trading day
    return get_last_trading_day(today)

def check_config_drift():
    """Warn if .env overrides are significantly diverging from institutional baselines."""
    recommendations = {
        "IPO_YEARS_BACK": (3, "Too short lookback blinds scanner to mature Stage-2 bases."),
        "LISTING_MAX_DAYS_SINCE_LISTING": (750, "Tight age filters kill institutional accumulation detection."),
        "MIN_AGE_DAYS": (60, "Scanning ultra-fresh IPOs (<60d) risks being trapped in post-listing distribution.")
    }
    
    # We check globals for the values established by get_env_int/float
    for key, (recommended, reason) in recommendations.items():
        val = globals().get(key)
        if val is not None and val < recommended:
            logger.warning(f"⚠️ [CONFIG DRIFT] {key} is set to {val}. Recommended is >= {recommended}.")
            logger.warning(f"   Reason: {reason}")

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# Initialize Config Audit
check_config_drift()
# ─────────────────────────────────────────────────────────────────────────────

# --- Cohort Definitions for Comparative Research ---
# These define the logical "buckets" for expectancy analysis.
RESEARCH_COHORTS = {
    "PERMISSIVE": {
        "max_prng": 60.0,
        "min_window": 10,
        "min_grade": "C",
        "vol_follow": 0.8
    },
    "STRICT": {
        "max_prng": 35.0,
        "min_window": 20,
        "min_grade": "C",
        "vol_follow": 1.0
    },
    "ULTRA_STRICT": {
        "max_prng": 25.0,
        "min_window": 30,
        "min_grade": "B",
        "vol_follow": 1.0
    }
}
# File paths
CACHE_FILE = os.getenv("CACHE_FILE", "ipo_cache.pkl")
# Metadata & State (Legacy CSVs removed, using MongoDB)

# System parameters
HEARTBEAT_RUNS = get_env_int("HEARTBEAT_RUNS", 0)

# Log yfinance availability after logger is initialized
if not YFINANCE_AVAILABLE:
    logger.warning("yfinance not available. Install with: pip install yfinance")

import json

def write_daily_log(scanner_name, symbol, action, details=None, candle_timestamp=None, log_type="ACCEPTED"):
    """Write scanner telemetry to MongoDB only (single-write path)."""
    try:
        from datetime import timezone, timedelta as td
        ist = timezone(td(hours=5, minutes=30))
        now_ist = datetime.now(ist)

        # DB-only write: use provided candle_timestamp if available, else fall back to now_ist
        try:
            from db import insert_log, db_metrics
            effective_candle_ts = candle_timestamp if candle_timestamp is not None else now_ist
            insert_log(
                scanner=scanner_name, symbol=symbol, action=action,
                candle_timestamp=effective_candle_ts,
                details=details or {}, version=SCANNER_VERSION, source="live",
                log_type=log_type
            )
        except Exception as db_e:
            logger.error(f"[MongoDB] log write FAILED for {symbol}/{action}: {db_e}")
            try:
                from db import db_metrics
                db_metrics["failures"] = db_metrics.get("failures", 0) + 1
            except Exception:
                pass

    except Exception as e:
        logger.debug(f"Could not write daily log: {e}")

def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning(f"[Telegram disabled] BOT_TOKEN: {'SET' if BOT_TOKEN else 'MISSING'}, CHAT_ID: {'SET' if CHAT_ID else 'MISSING'}")
        return
    
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    try:
        logger.info(f"Sending Telegram message to chat_id: {CHAT_ID}")
        response = requests.post(url, json={
            "chat_id": CHAT_ID, 
            "text": msg, 
            "parse_mode": "HTML",
            "disable_notification": False  # Force notification in group chats
        }, timeout=10)
        
        if response.status_code == 200:
            logger.info("✅ Telegram message sent successfully!")
            logger.info(f"Response: {response.json()}")
        else:
            logger.error(f"❌ Telegram API error: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"❌ Telegram error: {e}")

def format_signal_alert(symbol, grade, entry_price, stop_loss, target_price, score, date, consolidation_low=None, consolidation_high=None, breakout_price=None, data_source=None, current_price=None, price_source=None, breakout_close=None, entry_note=None, pattern_type=None, market_regime=None):
    """Format detailed IPO signal alert with comprehensive trading information"""
    # Calculate risk metrics
    risk_amount = entry_price - stop_loss
    risk_percentage = (risk_amount / entry_price) * 100
    reward_amount = target_price - entry_price
    reward_percentage = (reward_amount / entry_price) * 100
    risk_reward_ratio = reward_amount / risk_amount if risk_amount > 0 else 0
    
    # Calculate position sizing (assuming 1% risk per trade)
    position_size_percent = 1.0  # 1% of portfolio
    position_size_amount = (position_size_percent * 100000) / risk_amount if risk_amount > 0 else 0  # Assuming 1L portfolio
    
    # Determine win rate and confidence based on grade
    grade_info = {
        "A+": {"win_rate": "91%", "confidence": "Very High", "emoji": "⭐"},
        "A": {"win_rate": "85%", "confidence": "High", "emoji": "🔥"},
        "B": {"win_rate": "75%", "confidence": "Medium-High", "emoji": "🔥"},
        "C": {"win_rate": "65%", "confidence": "Medium", "emoji": "📈"},
        "D": {"win_rate": "60%", "confidence": "Low-Medium", "emoji": "📊"}
    }
    
    info = grade_info.get(grade, {"win_rate": "60%", "confidence": "Low", "emoji": "📊"})
    win_rate = info["win_rate"]
    confidence = info["confidence"]
    emoji = info["emoji"]
    
    # Format price information section
    # Bug 2 Fix: Always show BOTH breakout_close (fair reference) and entry_price
    # (actual live/execution price) so the alert is never ambiguous.
    price_info_section = ""
    if current_price is not None or breakout_close is not None:
        price_info_section = f"\n\n💰 <b>Price Information:</b>"
        if breakout_close is not None:
            price_info_section += f"\n• Breakout Close (Reference): ₹{breakout_close:,.2f}"
        if current_price is not None:
            price_info_section += f"\n• Current/Live Price: ₹{current_price:,.2f}"
        price_info_section += f"\n• Entry Price (Logged): ₹{entry_price:,.2f}"
        if price_source:
            price_info_section += f"\n• Price Source: {price_source}"
        if entry_note:
            note_labels = {
                "LIVE_INTRADAY": "⚡ Live intraday — execution price may differ from breakout close.",
                "NEXT_DAY_CLOSE": "📅 Based on next-day close.",
                "FALLBACK_CLOSE": "⚠️ Fallback close — live price unavailable."
            }
            price_info_section += f"\n• Entry Type: {note_labels.get(entry_note, entry_note)}"

    
    # Format the alert message with comprehensive information
    msg = f"""🎯 <b>IPO BREAKOUT SIGNAL</b>

📊 Symbol: <b>{symbol}</b>
{emoji} Grade: <b>{grade}</b> ({confidence} Confidence){price_info_section}
💰 Entry Price: ₹{entry_price:,.2f}
🛑 Stop Loss: ₹{stop_loss:,.2f} ({risk_percentage:.1f}% risk)
🎯 Target: ₹{target_price:,.2f} ({reward_percentage:.1f}% reward)
📊 Risk:Reward: 1:{risk_reward_ratio:.1f}
📈 Expected Return: {reward_percentage:.1f}% ({win_rate} win rate)

📋 <b>Pattern Details:</b>"""
    
    if consolidation_low and consolidation_high:
        msg += f"\n- Consolidation: Rs{consolidation_low:,.2f} - Rs{consolidation_high:,.2f}"
    
    if breakout_price:
        msg += f"\n- Breakout: Rs{breakout_price:,.2f}"
    
    msg += f"\n- Score: {score:.1f}/100"
    
    if pattern_type:
        msg += f"\n- Pattern: <b>{pattern_type}</b>"
    if market_regime:
        msg += f"\n- Regime: <b>{market_regime}</b>"

    # Add data source information
    if data_source:
        if data_source == 'Upstox API':
            msg += f"""
• Data Source: 🚀 Upstox API (Premium)"""
        elif data_source == 'NSE (Fallback)':
            msg += f"""
• Data Source: 📊 NSE (Fallback)"""
        else:
            msg += f"""
• Data Source: {data_source}"""

    msg += f"""

💼 <b>Position Sizing:</b>
• Risk per trade: {risk_percentage:.1f}%
• Suggested quantity: {int(position_size_amount):,} shares
• Capital at risk: ₹{int(risk_amount * position_size_amount):,}

📅 Signal Date: {date if isinstance(date, str) else date.strftime('%Y-%m-%d')}
⚠️ <b>Action Required:</b> Enter position at market open"""
    
    return msg

def format_exit_alert(symbol, exit_reason, exit_price, pnl_pct, days_held, entry_price):
    """Format detailed exit alert"""
    # Exit reason emojis
    exit_emojis = {
        "Stop Loss": "🛑",
        "Early Base Break": "⚡",
        "Time Stop -5%": "⏰",
        "Time Stop -8%": "⏰",
        "Partial Take": "💰"
    }
    emoji = exit_emojis.get(exit_reason, "📊")
    
    # PnL color
    pnl_color = "🟢" if pnl_pct > 0 else "🔴"
    
    msg = f"""{emoji} <b>POSITION EXIT</b>

📊 Symbol: <b>{symbol}</b>
📋 Reason: <b>{exit_reason}</b>
💰 Exit Price: ₹{exit_price:,.2f}
{pnl_color} P&L: {pnl_pct:+.1f}%
📅 Days Held: {days_held}
💵 Entry: ₹{entry_price:,.2f}

{datetime.now().strftime('%Y-%m-%d %H:%M')}"""
    return msg


def close_active_signal(symbol, exit_price, pnl_pct, days_held, exit_reason):
    """Mark the latest ACTIVE signal for symbol as CLOSED in MongoDB."""
    try:
        from db import close_signal_in_db
        close_signal_in_db(symbol, exit_price, pnl_pct, days_held, exit_reason)
    except Exception as e:
        logger.error(f"Error syncing signal close for {symbol}: {e}")

# initialize_csvs removed as system is fully on MongoDB


def cache_recent_ipos():
    df = None
    try:
        df = fetch_recent_ipo_symbols(years_back=IPO_YEARS_BACK)
        if df is not None and not df.empty:
            with open(CACHE_FILE, "wb") as f:
                pickle.dump(df, f)
    except Exception as e:
        print(f"Error fetching symbols: {e}")

    # Fallback to existing cache if fetch failed
    if df is None or df.empty:
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE,"rb") as f:
                    df = pickle.load(f)
            except:
                df = None
        
    # Final fallback to empty DF (CSV removed)
    if df is None or df.empty:
        df = pd.DataFrame(columns=["symbol","company","listing_date"])
            
    return df

def discover_listing_date(symbol):
    """Discovery mechanism for missing listing dates using yfinance max history."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(f"{symbol}.NS")
        hist = ticker.history(period="max")
        if not hist.empty:
            return hist.index[0].date()
    except Exception as e:
        logger.warning(f"  [Discovery] Failed for {symbol}: {e}")
    return None

def get_symbols_and_listing():
    ipo_df = cache_recent_ipos()
    recent = ipo_df["symbol"].tolist()
    listing_map = {}
    
    today = datetime.today().date()
    for _, row in ipo_df.iterrows():
        sym = row["symbol"]
        try:
            ld = pd.to_datetime(row["listing_date"]).date()
            # Safety: If date is today/yesterday, it might be a fetch.py fallback.
            # Verify via discovery to avoid treating old IPOs as fresh listings.
            if ld >= today - timedelta(days=1):
                actual_ld = discover_listing_date(sym)
                if actual_ld and actual_ld < ld:
                    ld = actual_ld
                    logger.info(f"🔄 Corrected listing date for {sym}: {ld}")
            listing_map[sym] = ld
        except Exception:
            # If date parsing fails, try discovery
            ld = discover_listing_date(sym)
            if ld:
                listing_map[sym] = ld
                logger.info(f"🔍 Discovered missing listing date for {sym}: {ld}")

    try:
        from db import get_active_symbols
        active = get_active_symbols()
    except Exception:
        active = []
    return list(set(recent + active)), listing_map


def fetch_from_upstox(symbol, start_date, end_date):
    """Fetch historical data from Upstox API with rate limiting"""
    import time
    
    try:
        # Convert dates to date objects if needed
        if isinstance(start_date, str):
            start_date = pd.to_datetime(start_date).date()
        elif hasattr(start_date, 'date'):
            start_date = start_date.date()
        elif isinstance(start_date, pd.Timestamp):
            start_date = start_date.date()
            
        if isinstance(end_date, str):
            end_date = pd.to_datetime(end_date).date()
        elif hasattr(end_date, 'date'):
            end_date = end_date.date()
        elif isinstance(end_date, pd.Timestamp):
            end_date = end_date.date()
        
        # Load IPO mappings from MongoDB
        try:
            from db import get_instrument_key_mapping
            mapping = get_instrument_key_mapping()
        except Exception as map_e:
            logger.warning(f"Could not load instrument key mapping: {map_e}")
            return None

        if symbol not in mapping:
            logger.warning(f"Symbol {symbol} not found in Upstox mapping")
            return None

        instrument_key = mapping[symbol]
        
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
        response = requests.get(url, headers=headers, timeout=10)
        
        # Handle rate limiting (429 Too Many Requests)
        if response.status_code == 429:
            logger.warning(f"⚠️ Rate limited for {symbol}, waiting 1 second...")
            time.sleep(1)
            response = requests.get(url, headers=headers, timeout=10)
        
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

def fetch_from_yfinance(symbol, start_date, end_date):
    """Tertiary fallback fetch from YFinance"""
    try:
        if not YFINANCE_AVAILABLE:
            return None
            
        yf_sym = f"{symbol}.NS"
        # yfinance download end date is exclusive, add 1 day
        end_dt = pd.to_datetime(end_date) + pd.Timedelta(days=1)
        
        # Rate limiting for yfinance
        global _yfinance_last_request
        with _yfinance_lock:
            current_time = time.time()
            time_since_last = current_time - _yfinance_last_request
            if time_since_last < _yfinance_min_delay:
                time.sleep(_yfinance_min_delay - time_since_last)
            _yfinance_last_request = time.time()
            
        df = yf.download(yf_sym, start=start_date, end=end_dt, progress=False)
        if df is None or df.empty:
            return None
            
        df = df.reset_index()
        # Handle MultiIndex columns in newer yfinance versions
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        
        col_map = {"Date": "DATE", "Open": "OPEN", "High": "HIGH",
                   "Low": "LOW", "Close": "CLOSE", "Volume": "VOLUME"}
        df = df.rename(columns=col_map)
        
        # Standardize columns to match scanner expectations
        required = ["DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
        if not all(col in df.columns for col in required):
            return None
            
        df["DATE"] = pd.to_datetime(df["DATE"])
        df["LTP"] = df["CLOSE"]
        
        return df[required + ["LTP"]]
    except Exception as e:
        logger.warning(f"⚠️ YFinance fallback failed for {symbol}: {e}")
        return None

def get_live_price_upstox(symbol):
    """Get live price from Upstox market quote API"""
    try:
        # Load IPO mappings from MongoDB
        try:
            from db import get_instrument_key_mapping
            mapping = get_instrument_key_mapping()
        except Exception:
            return None

        if symbol not in mapping:
            return None

        instrument_key = mapping[symbol]
        
        # Get Upstox credentials
        access_token = os.getenv('UPSTOX_ACCESS_TOKEN')
        if not access_token:
            return None
        
        # Prepare API request
        headers = {
            'Accept': 'application/json',
            'Api-Version': '2.0',
            'Authorization': f'Bearer {access_token}'
        }
        
        url = f'https://api.upstox.com/v2/market-quote/quotes?instrument_key={instrument_key}'
        
        import time
        global _upstox_last_request
        with _upstox_lock:
            current_time = time.time()
            time_since_last = current_time - _upstox_last_request
            if time_since_last < 0.2:  # 200ms minimum delay
                time.sleep(0.2 - time_since_last)
            _upstox_last_request = time.time()
            
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if 'data' in data:
                # Try both key formats
                quote_key = f'NSE_EQ:{symbol}'
                if quote_key in data['data']:
                    quote = data['data'][quote_key]
                    live_price = quote.get('last_price')
                    ohlc = quote.get('ohlc', {})
                    day_high = ohlc.get('high')
                    if live_price:
                        return float(live_price), (float(day_high) if day_high else float(live_price))
                elif instrument_key in data['data']:
                    quote = data['data'][instrument_key]
                    live_price = quote.get('last_price')
                    ohlc = quote.get('ohlc', {})
                    day_high = ohlc.get('high')
                    if live_price:
                        return float(live_price), (float(day_high) if day_high else float(live_price))
        
        return None
    except Exception as e:
        logger.warning(f"Error fetching live price from Upstox for {symbol}: {e}")
        return None

def get_live_price_yfinance(symbol):
    """Get live price from yfinance API with rate limiting"""
    if not YFINANCE_AVAILABLE:
        return None
    
    global _yfinance_last_request
    try:
        # Rate limiting: Ensure minimum delay between requests
        with _yfinance_lock:
            current_time = time.time()
            time_since_last = current_time - _yfinance_last_request
            if time_since_last < _yfinance_min_delay:
                time.sleep(_yfinance_min_delay - time_since_last)
            _yfinance_last_request = time.time()
        
        # NSE symbols need .NS suffix for yfinance
        ticker_symbol = f"{symbol}.NS"
        ticker = yf.Ticker(ticker_symbol)
        
        # Get current info (fastest method)
        info = ticker.fast_info
        if hasattr(info, 'lastPrice') and info.lastPrice:
            return float(info.lastPrice)
        
        # Fallback: Get latest quote
        data = ticker.history(period="1d", interval="1m")
        if not data.empty:
            last_price = float(data['Close'].iloc[-1])
            day_high = float(data['High'].max())
            return last_price, day_high
        
        return None
    except Exception as e:
        logger.warning(f"Error fetching live price from yfinance for {symbol}: {e}")
        return None



def get_live_price(symbol, prefer_source=None):
    """
    Get live price from multiple sources with fallback chain:
    1. Upstox API (if prefer_source='upstox' or None)
    2. yfinance (primary fallback - most reliable)
    3. jugaad-data (last resort only - not accurate, use sparingly)
    
    Returns: (price, source_name) or (None, None) if all fail
    """
    sources = []
    
    # Determine source priority
    if prefer_source == 'yfinance':
        sources = [('yfinance', get_live_price_yfinance), ('upstox', get_live_price_upstox)]
    else:
        # Default: Try Upstox first, then yfinance (most reliable)
        sources = [('upstox', get_live_price_upstox), ('yfinance', get_live_price_yfinance)]
    
    for source_name, fetch_func in sources:
        try:
            result = fetch_func(symbol)
            if result is not None:
                price, day_high = result
                if price > 0:
                    logger.info(f"✅ Got live price for {symbol} from {source_name}: Rs.{price:.2f} (High: Rs.{day_high:.2f})")
                    return price, source_name, day_high
        except Exception as e:
            logger.debug(f"Failed to get price from {source_name} for {symbol}: {e}")
            continue
    
    logger.warning(f"⚠️ Could not fetch live price for {symbol} from any source")
    return None, None, None

def fetch_data(symbol, start_date):
    """Fetch the most recent available data for a symbol using Upstox API with NSE fallback (jugaad-data)"""
    import time  # Import at the top of function
    
    # Skip RE (Real Estate Investment Trusts) shares as they're not suitable for IPO breakout patterns
    # Optimization: Ignore Rights Entitlements (-RE) and SME (-SM) segments
    if '-RE' in symbol or symbol.endswith('-SM') or 'RE1' in symbol:
        logger.warning(f"Skipping RE/SME share: {symbol}")
        return None
    
    try:
        # Convert start_date to date object if needed
        if isinstance(start_date, str):
            start_date = pd.to_datetime(start_date).date()
        elif hasattr(start_date, 'date'):
            start_date = start_date.date()
        elif isinstance(start_date, pd.Timestamp):
            start_date = start_date.date()
        
        today = datetime.today().date()
        
        # Try Upstox API first (if available)
        df = fetch_from_upstox(symbol, start_date, today)
        if df is not None and not df.empty:
            logger.info(f"✅ Upstox API: Got data for {symbol} ({len(df)} rows)")
            df.attrs['data_source'] = 'Upstox API'
            return df
        
        # Primary Fallback: Yahoo Finance (Simplified and Reliable)
        logger.warning(f"⚠️ Upstox API failed for {symbol}, trying YFinance fallback")
        df = fetch_from_yfinance(symbol, start_date, today)
        if df is not None and not df.empty:
            logger.info(f"✅ YFinance Fallback: Got data for {symbol} ({len(df)} rows)")
            df.attrs['data_source'] = 'Yahoo Finance'
            return df

        logger.warning(f"❌ No data found for {symbol} (Upstox & YFinance both failed)")
        return None
            
    except Exception as e:
        logger.error(f"Error fetching data for {symbol}: {e}")
        return None
    
def update_positions():
    from db import get_all_positions_df
    df_pos = get_all_positions_df()
    if df_pos.empty:
        logger.info("update_positions: no positions found in MongoDB")
        return
    
    # Initialize outcome tracking columns backward compatibility
    schema_cols = [
        "max_runup_pct",
        "max_drawdown_pct",
        "outcome_type",
        "holding_efficiency_pct",
        "time_to_failure_days",
        "time_to_failure_min",
    ]
    for c in schema_cols:
        if c not in df_pos.columns:
            df_pos[c] = None
    df_pos["max_runup_pct"] = df_pos["max_runup_pct"].fillna(0.0)
    df_pos["max_drawdown_pct"] = df_pos["max_drawdown_pct"].fillna(0.0)

    # Initialize Analytics (Phase 2.2)
    analytics_repo = None
    lifecycle_tracker = None
    if ANALYTICS_AVAILABLE:
        try:
            analytics_repo = MongoRepository()
            lifecycle_tracker = LifecycleTracker(analytics_repo)
        except Exception:
            pass

    for idx, pos in df_pos[df_pos["status"]=="ACTIVE"].iterrows():
        sym = pos["symbol"]
        
        # Handle entry_date - convert to date object
        start = pos["entry_date"]
        if isinstance(start, pd.Timestamp):
            start = start.date()
        elif isinstance(start, str):
            start = pd.to_datetime(start).date()
        elif hasattr(start, 'date'):
            start = start.date()
        else:
            logger.warning(f"Could not parse entry_date for {sym}: {start}")
            continue
        
        # Get current price - prefer LIVE price, fallback to latest historical close
        current_price = None
        price_source = "Historical Close"
        
        # Try to get live price first (more accurate for exit decisions)
        try:
            live_price, live_source, _ = get_live_price(sym)
            if live_price is not None and live_price > 0:
                current_price = live_price
                price_source = f"Live ({live_source})"
                logger.info(f"✅ Using live price for {sym}: ₹{current_price:.2f} from {live_source}")
        except Exception as e:
            logger.debug(f"Could not get live price for {sym}: {e}")
        
        # Fallback to historical data if live price unavailable
        if current_price is None:
            df = fetch_data(sym, start)
            if df is None or df.empty: 
                logger.warning(f"No data available for {sym}")
                continue
            
            # Get latest date from historical data
            latest_date = df['DATE'].max()
            if isinstance(latest_date, pd.Timestamp):
                latest_date = latest_date.date()
            elif hasattr(latest_date, 'date'):
                latest_date = latest_date.date()
            else:
                latest_date = pd.to_datetime(latest_date).date()
            
            # CRITICAL: Validate data is from a recent trading session
            # Use market-aware logic instead of calendar-day hacks
            last_trading_day = get_last_trading_day()
            
            # Allow data to be as old as the last valid trading day
            # If the market hasn't updated today yet, the data must be at least from the previous session
            if latest_date < last_trading_day:
                logger.error(f"❌ STALE DATA for {sym}: Latest data is {latest_date}. Last trading day was {last_trading_day}. Refusing exit decision with stale data!")
                continue
            
            # Data is fresh (today or yesterday) - safe to use
            current_price = float(df["CLOSE"].iloc[-1])
            latest_date_str = latest_date.strftime('%Y-%m-%d')
            price_source = f"Historical Close ({latest_date_str})"
            
            days_old = (datetime.today().date() - latest_date).days
            if days_old == 0:
                logger.info(f"✅ Using today's historical close for {sym}: ₹{current_price:.2f}")
            else:
                logger.warning(f"⚠️ Using yesterday's close for {sym}: ₹{current_price:.2f} (market may be closed)")
        
        # Calculate supertrend for trailing stop (need fresh data)
        # Only fetch if we don't already have df from above
        if current_price is None or 'df' not in locals():
            df = fetch_data(sym, start)
        if df is None or df.empty:
            logger.warning(f"Could not fetch data for supertrend calculation for {sym}")
            trailing = float(pos["stop_loss"])  # Fallback to original stop loss
        else:
            st = supertrend(df)
            trailing = max(float(pos["stop_loss"]), float(st.iloc[-1]))
        
        pnl = (current_price - float(pos["entry_price"]))/float(pos["entry_price"])*100
        
        # Calculate days held correctly
        today_date = datetime.today().date()
        days = (today_date - start).days
        
        if days < 0:
            logger.error(f"⚠️ Negative days for {sym}: entry_date={start}, today={today_date}")
            days = 0
            
        # Update peak metrics
        current_max_runup = float(pos.get("max_runup_pct", 0.0) or 0.0)
        current_max_drawdown = float(pos.get("max_drawdown_pct", 0.0) or 0.0)
        
        new_max_runup = max(current_max_runup, pnl)
        new_max_drawdown = min(current_max_drawdown, pnl)

        # --- Lifecycle Tracking (Phase 2.2) ---
        if ANALYTICS_AVAILABLE and analytics_repo and lifecycle_tracker:
            try:
                # Deterministic Signal ID reconstruction
                sig_id = analytics_repo.generate_deterministic_id(sym, start)
                lifecycle_tracker.record_daily_update(
                    signal_id=sig_id,
                    entry_price=float(pos["entry_price"]),
                    stop_price=float(pos["stop_loss"]),
                    current_price=float(current_price),
                    date=datetime.now()
                )
            except Exception as e:
                logger.error(f"❌ Failed to record lifecycle update for {sym}: {e}")

        
        # Enhanced exit strategies
        exit_reason = None
        
        # Tiered Time-Based Stops
        if days > 30 and current_price < pos["entry_price"] * 0.95:
            exit_reason = "Time Stop -5%"
        elif days > 60 and current_price < pos["entry_price"] * 0.92:
            exit_reason = "Time Stop -8%"
        
        # Traditional stop loss
        if current_price <= trailing:
            exit_reason = "Stop Loss"
        
        if exit_reason:
            # Outcome Classification
            outcome_type = "NO_FOLLOW_THROUGH"
            time_to_failure_days = None
            time_to_failure_min = None
            
            if new_max_runup > 10.0 and days <= 5:
                outcome_type = "FAST_WINNER"
            elif new_max_runup > 10.0 and days > 5:
                outcome_type = "SLOW_WINNER"
            elif new_max_runup <= 3.0 and new_max_drawdown <= -3.0:
                outcome_type = "FAILED_BREAKOUT"
                time_to_failure_days = days
            elif new_max_runup < 1.0 and exit_reason == "Stop Loss":
                outcome_type = "IMMEDIATE_FAILURE"
                time_to_failure_days = days
            elif new_max_runup > 3.0 and new_max_runup <= 8.0:
                outcome_type = "NO_FOLLOW_THROUGH"
            
            holding_efficiency_pct = None
            if new_max_runup >= 5.0:
                holding_efficiency_pct = round((pnl / new_max_runup) * 100.0, 2)

            # Derived analytic metric for faster failure diagnostics (no strategy impact)
            if time_to_failure_days is not None:
                time_to_failure_min = int(time_to_failure_days * 390)
                
            df_pos.loc[idx, ["status","exit_date","exit_price","pnl_pct","days_held", "max_runup_pct", "max_drawdown_pct", "outcome_type", "holding_efficiency_pct", "time_to_failure_days", "time_to_failure_min"]] = [
                "CLOSED", datetime.today().strftime("%Y-%m-%d"),
                float(current_price), float(pnl), int(days),
                new_max_runup, new_max_drawdown, outcome_type, holding_efficiency_pct, time_to_failure_days, time_to_failure_min
            ]
            close_active_signal(sym, current_price, pnl, days, exit_reason)
            
            # --- Outcome Evaluation (Phase 2.2) ---
            if ANALYTICS_AVAILABLE and analytics_repo:
                try:
                    sig_id = analytics_repo.generate_deterministic_id(sym, start)
                    evaluate_signal_outcome(analytics_repo, sig_id)
                except Exception as e:
                    logger.error(f"❌ Failed to evaluate outcome for {sym}: {e}")

            # Send detailed exit alert
            exit_msg = format_exit_alert(sym, exit_reason, current_price, pnl, days, pos["entry_price"])
            # Append outcome visually
            exit_msg += f"\n\n📊 <b>Outcome:</b> {outcome_type} (Peak: +{new_max_runup:.1f}%)"
            send_telegram(exit_msg)
        else:
            df_pos.loc[idx, ["current_price","trailing_stop","pnl_pct","days_held", "max_runup_pct", "max_drawdown_pct"]] = [
                float(current_price), float(trailing), float(pnl), int(days), new_max_runup, new_max_drawdown
            ]
    
    # Positions are already written row-by-row via upsert_position inside the loop above;
    # no batch CSV write needed.

def compute_rsi(close, period=14):
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def compute_macd(close):
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd_line = ema12 - ema26
    macd_signal = macd_line.ewm(span=9).mean()
    return macd_line, macd_signal

def dynamic_partial_take(grade):
    return PT_A_PLUS if grade=="A+" else PT_B if grade=="B" else PT_C

def smart_b_filters(df, entry_idx, avg_vol):
    close = df['CLOSE']
    rsi = compute_rsi(close)
    macd_line, macd_signal = compute_macd(close)

    # Require RSI >= 60 for a stronger momentum filter
    if rsi.iloc[entry_idx] < 60:
        return False

    # Require MACD line to be above signal by 0.1% of closing price
    if (macd_line.iloc[entry_idx] - macd_signal.iloc[entry_idx]) < 0.001 * close.iloc[entry_idx]:
        return False

    volume_ok = df['VOLUME'].iat[entry_idx] > 2.5*avg_vol
    ema20 = close.ewm(span=20).mean()
    trend_ok = close.iloc[entry_idx] > ema20.iloc[entry_idx]

    return volume_ok and trend_ok

def smart_c_filters(df, entry_idx, entry_price, w, avg_vol):
    c_score = 0
    low=df['LOW'][entry_idx-w+1:entry_idx+1].min()
    high=df['HIGH'][entry_idx-w+1:entry_idx+1].max()
    prn = (high-low)/low*100
    if prn<=25: c_score+=1
    if df['VOLUME'].iat[entry_idx]>=2*avg_vol: c_score+=1
    close = df['CLOSE']
    ema20 = close.ewm(span=20).mean()
    if close.iloc[entry_idx] > ema20.iloc[entry_idx]: c_score+=1
    for k in range(entry_idx+1, min(entry_idx+5, len(df))):
        if df['CLOSE'].iat[k] >= entry_price * 0.99:
            c_score+=1; break
    return c_score>=2

def reject_quick_losers(df, entry_idx, w, avg_vol):
    close = df['CLOSE']
    volume = df['VOLUME']
    red_flags = 0
    recent_avg_vol = volume.iloc[entry_idx-5:entry_idx].mean() if entry_idx >= 5 else avg_vol
    if volume.iat[entry_idx] < 1.5 * recent_avg_vol: red_flags += 1
    base_closes = close.iloc[entry_idx-w:entry_idx]
    if len(base_closes) >= 10:
        downtrend_days = sum(base_closes.diff() < 0) / len(base_closes)
        if downtrend_days > 0.6: red_flags += 1
    rsi = compute_rsi(close)
    if rsi.iloc[entry_idx] < 45: red_flags += 1
    recent_high = close.iloc[max(0,entry_idx-10):entry_idx].max()
    current_price = close.iloc[entry_idx]
    if current_price < recent_high * 0.92: red_flags += 1
    return red_flags >= 2

def detect_live_patterns(symbols, listing_map):
    """Detect LIVE FORMING patterns using proven backtest logic"""
    existing_signals_df = pd.DataFrame()
    signals_found = 0
    symbols_processed = 0
    processed_today = set()  # Track symbols processed today to prevent duplicates
    
    # Initialize Institutional Analytics (if available)
    analytics_repo = None
    signal_builder = None
    analytics_failures = 0
    v2_complete_count = 0
    v2_incomplete_count = 0
    v2_audit_samples = {
        "CLEAN_BREAKOUT": None, # Textbook case: low wick, high vol, tight base
        "HIGH_VOL": None,
        "HIGH_DELTA": None,
        "FIRST_INCOMPLETE": None,
        "RANDOM": None
    }
    if ANALYTICS_AVAILABLE:
        try:
            analytics_repo = MongoRepository()
            signal_builder = SignalBuilder()
            logger.info("🏛️ Institutional Analytics Engine Initialized (v2)")
        except Exception as e:
            logger.error(f"Failed to initialize analytics: {e}")
    
    for sym in symbols:
        symbols_processed += 1
        if symbols_processed % 20 == 0:
            logger.info(f"Processed {symbols_processed}/{len(symbols)} symbols...")
        
        try:
            from db import has_active_position
            if has_active_position(sym):
                continue
        except Exception:
            pass
        
        # Check if we already processed this symbol today
        today_key = f"{sym}_{datetime.today().strftime('%Y%m%d')}"
        if today_key in processed_today:
            continue
            
        ld = listing_map.get(sym)
        if not ld: continue
        df = fetch_data(sym, ld)
        if df is None or df.empty: continue
        
        # Check data freshness - reject signals with data older than expected for live trading
        latest_date = df['DATE'].max()
        if hasattr(latest_date, 'date'):
            latest_date = latest_date.date()
        
        last_expected = get_last_expected_data_date()
        if latest_date < last_expected:
            logger.warning(f"Skipping {sym} - data is stale (Latest: {latest_date}, Expected: {last_expected}). Too old for live trading!")
            continue
        
        lhigh = df["HIGH"].iloc[0]

        # Nested w/i loops can hit the same breakout many times — log each rejection reason once per symbol per run
        _consolidation_reject_logged = set()

        def _log_consolidation_reject_once(details: dict):
            r = details.get("reason", "unknown")
            if r in _consolidation_reject_logged:
                return
            _consolidation_reject_logged.add(r)
            
            # Determine failing metric name
            failing_metric_name = "unknown"
            for k in ["risk", "risk_pct", "ratio", "vol_ratio", "distance_pct", "days_old", "grade"]:
                if k in details:
                    failing_metric_name = k
                    break
                    
            actual_metric = details.get(
                "risk_pct", details.get(
                "risk",
                details.get("ratio", details.get("vol_ratio", details.get("distance_pct", details.get("days_old", details.get("grade", None))))))
            )
            required_metric = details.get(
                "max_allowed", details.get(
                "reward",
                details.get("min_required", None))
            )
            
            restructured_payload = {
                "symbol": sym,
                "stage": "post_confirm" if "days_old" in details else "pre_breakout",
                "rejection_reason": r,
                "failing_metric": failing_metric_name,
                "failing_value": actual_metric,
                "threshold": required_metric,
                "metrics": details.copy(),
                "ipo_age": ipo_age_for_log,
                "volume_ratio": details.get("vol_ratio", None),
                "source": "live",
                "log_type": "REJECTED"
            }
            write_daily_log("consolidation", sym, "REJECTED_BREAKOUT", restructured_payload, log_type="REJECTED")

        # listing_map values are typically datetime.date; older codepaths may pass dict-like objects.
        ipo_age_for_log = 0
        try:
            listing_date_val = None
            if isinstance(ld, dict):
                listing_date_val = ld.get("listingDate") or ld.get("listing_date")
            else:
                listing_date_val = ld
            if listing_date_val is not None and pd.notna(listing_date_val):
                listing_date_val = pd.to_datetime(listing_date_val).date()
                ipo_age_for_log = (datetime.today().date() - listing_date_val).days
        except Exception:
            ipo_age_for_log = 0
        # Standardized Rejection Telemetry for Analysis (Phase 2.2)
        _rejection_reasons = []
        _rejection_logged = False
        
        def _log_rejection_telemetry(reason: str, value: float, threshold: float, metrics: dict,
                                       potential_entry: float = None, potential_stop: float = None,
                                       potential_target: float = None, pattern_type: str = None,
                                       cohort: str = None):
            nonlocal _rejection_logged
            if reason not in _rejection_reasons:
                _rejection_reasons.append(reason)
            
            if _rejection_logged: return
            
            # Map reason string to objective reason codes for analytics
            reason_code_map = {
                "loose_base": RC_PRNG_LIMIT,
                "low_volume": RC_VOL_LIMIT,
                "failed_follow_through": RC_STRUCTURE_FAILED,
                "grade_d": "LOW_GRADE"
            }
            rc = reason_code_map.get(reason, reason.upper())
            
            # Determine Bucket
            bucket, bucket_reasons = categorize_signal_bucket(metrics, ipo_age_for_log)
            
            # Capture Cluster Index (Deduplication of correlated events)
            # Use j-th candle date as the cluster anchor
            candle_dt = df["DATE"].iat[j]
            cluster_idx = candle_dt.strftime('%Y-%m-%d') if hasattr(candle_dt, 'strftime') else str(candle_dt)
            
            # 1. Near-Miss Threshold Filter: Only log candidates that are "interesting"
            # Extended bucket items are ALWAYS interesting for research
            is_interesting = (
                bucket == BUCKET_EXTENDED or
                metrics.get("vol_ratio", 0) >= 0.8 or 
                metrics.get("prng", 999) <= 70 or
                metrics.get("rsi", 0) >= 55
            )
            if not is_interesting and bucket != BUCKET_EXTENDED: return

            _rejection_logged = True

            # ── Phase 3: Ghost PnL — freeze potential levels at rejection time ──
            ghost_pnl = {}
            if potential_entry is not None:
                ghost_pnl = {
                    "potential_entry":  round(potential_entry, 2),
                    "potential_stop":   round(potential_stop, 2)   if potential_stop  is not None else None,
                    "potential_target": round(potential_target, 2) if potential_target is not None else None,
                    "observation_window_days": 60,  
                    "ghost_status": "PENDING"        
                }

            payload = {
                "symbol": sym,
                "action": "MODEL_EXCLUSION",
                "log_type": "EXCLUDED",
                "bucket": bucket,
                "reason_codes": bucket_reasons,
                "cluster_index": cluster_idx,
                "failing_metric": reason,
                "failing_value": value,
                "threshold": threshold,
                "ipo_age": ipo_age_for_log,
                "metrics": {
                    "perf": metrics.get("perf"),
                    "prng": metrics.get("prng"),
                    "vol_ratio": metrics.get("vol_ratio"),
                    "rsi": metrics.get("rsi"),
                    "score": metrics.get("score"),
                    "window": metrics.get("window"),
                    "avg_vol": metrics.get("avg_vol", 0)
                },
                "pattern_type": pattern_type or "UNKNOWN",
                "cohort": cohort or "UNKNOWN",
                "market_regime": get_market_regime(candle_dt), 
                **ghost_pnl,
                "post_breakout_tracking": bucket in [BUCKET_ALIGNED, BUCKET_EXTENDED],
                "source": "live"
            }
            write_daily_log("consolidation", sym, "MODEL_EXCLUSION", payload, log_type="EXCLUDED")
            logger.debug(f"[Research] Logged {bucket} exclusion for {sym}: {bucket_reasons} | Entry={potential_entry}")
        
        # Use your proven backtest logic but check for LIVE patterns (recent breakouts)
        for w in CONSOL_WINDOWS[::-1]:  # Start with larger windows first
            if len(df) < w: continue
            
            # Check recent data for live patterns (last 10 days for better coverage)
            recent_start = max(w, len(df)-10)  # Check last 10 days for live patterns
            for j in range(recent_start, len(df)):
                # 1. Define immediate base O(N)
                base_window = df.iloc[j-w:j]
                if len(base_window) < w: continue
                
                low = base_window["LOW"].min()
                high2 = base_window["HIGH"].max()
                
                # 2. Context Rule (Base Formation Zone 8-35% below lhigh)
                perf = (low - lhigh) / lhigh
                if not (0.08 <= -perf <= 0.35): continue
                
                current_metrics = {"perf": round(perf, 4), "window": w}
                
                # 3. Base Tightness
                prng = round((high2 - low) / low * 100, 2)
                current_metrics["prng"] = prng
                
                # 4. Volume Checks
                avgv = base_window["VOLUME"].mean()
                if avgv <= 0: continue
                vol_ratio = round(df["VOLUME"].iat[j] / avgv, 2)
                current_metrics["vol_ratio"] = vol_ratio
                
                vol_ok = ((df["VOLUME"].iat[j] >= 2.5*avgv and df["VOLUME"].iloc[j-2:j+1].sum() >= 4*avgv) or
                         vol_ratio >= VOL_MULT or
                         (df["VOLUME"].iloc[j-2:j+1].sum() * df["CLOSE"].iat[j]) >= ABS_VOL_MIN)
                
                # 5. Breakout Confirmation
                is_live_breakout = False
                
                if j == len(df) - 1:
                    # This is the latest candle - check LIVE price for breakout
                    if is_market_hours():
                        live_price, _, _ = get_live_price(sym)
                    else:
                        live_price = float(df["CLOSE"].iloc[-1])
                    if live_price is not None and live_price > high2:
                        is_live_breakout = True
                else:
                    # Historical confirmed candle
                    if df["CLOSE"].iat[j] > high2 and df["CLOSE"].iat[j] > df["OPEN"].iat[j]:
                        is_live_breakout = True
                
                if not is_live_breakout:
                    continue

                # 6. Institutional Liquidity & Microcap Guardrails (Phase 2.5)
                # Fetch fresh liquidity metrics (Turnover, Circuits, Market Cap)
                avg_turnover_cr, circuit_days_15, mcap_cr = get_liquidity_metrics(sym, df)
                current_metrics["avg_turnover_cr"] = avg_turnover_cr
                current_metrics["circuit_days_15"] = circuit_days_15
                current_metrics["market_cap_cr"] = mcap_cr

                # 7. Bucket Validation (ALIGNED vs EXTENDED vs BROKEN)
                bucket, bucket_reasons = categorize_signal_bucket(current_metrics, ipo_age_for_log)
                
                # Use actual price for realistic research entry (close or high2, whichever is higher)
                realistic_entry = float(max(high2, df["CLOSE"].iat[j]))
                
                if bucket != BUCKET_ALIGNED:
                    # Log for research (Out-of-sample or Broken)
                    _ghost_entry  = realistic_entry
                    _ghost_stop   = round(_ghost_entry * 0.92, 2)
                    _ghost_target = round(_ghost_entry + (_ghost_entry - _ghost_stop) * 2, 2)
                    _pt = classify_pattern_type("UNKNOWN", ipo_age_for_log, vol_ratio, prng)
                    
                    # Map bucket reasons back to telemetry strings
                    primary_reason = "loose_base" if RC_PRNG_LIMIT in bucket_reasons else "other"
                    if bucket == BUCKET_BROKEN: 
                        if RC_LIQUIDITY_TRAP in bucket_reasons:
                            primary_reason = "liquidity_trap"
                        elif f"{RC_LIQUIDITY_TRAP}_CIRCUITS" in bucket_reasons:
                            primary_reason = "circuit_trap"
                        else:
                            primary_reason = "structurally_broken"
                    
                    _log_rejection_telemetry(primary_reason, prng, MAX_PRNG, current_metrics,
                                             potential_entry=realistic_entry,
                                             potential_stop=round(realistic_entry * 0.92, 2),
                                             potential_target=round(realistic_entry * 1.20, 2),
                                             pattern_type=classify_pattern_type("UNKNOWN", ipo_age_for_log, vol_ratio, prng))
                    continue

                # 7. Volume Validation for ALIGNED bucket
                if not vol_ok:
                    _ghost_entry  = realistic_entry
                    _ghost_stop   = round(_ghost_entry * 0.92, 2)
                    _ghost_target = round(_ghost_entry + (_ghost_entry - _ghost_stop) * 2, 2)
                    _pt = classify_pattern_type("UNKNOWN", ipo_age_for_log, vol_ratio, prng)
                    _log_rejection_telemetry("low_volume", vol_ratio, VOL_MULT, current_metrics,
                                             potential_entry=_ghost_entry,
                                             potential_stop=_ghost_stop,
                                             potential_target=_ghost_target,
                                             pattern_type=_pt)
                    continue
                
                # j is correctly defined, continue with follow-through
                
                # Continue with breakout validation
                # Follow-through filter (relaxed): next day should show conviction
                # Require EITHER close holds near base high OR decent volume continuation
                if j + 1 < len(df):
                    breakout_close = df["CLOSE"].iat[j]
                    breakout_volume = df["VOLUME"].iat[j]
                    next_day_close = df["CLOSE"].iat[j + 1]
                    next_day_volume = df["VOLUME"].iat[j + 1]
                    base_high = df["HIGH"][j-w+1:j+1].max()

                    close_holds = next_day_close > base_high * 0.98  # Allow 2% pullback
                    volume_confirms = next_day_volume >= 0.8 * breakout_volume  # 80% volume ok
                    if not close_holds and not volume_confirms:
                        _ghost_entry  = float(breakout_close)
                        _ghost_stop   = round(_ghost_entry * 0.92, 2)
                        _ghost_target = round(_ghost_entry + (_ghost_entry - _ghost_stop) * 2, 2)
                        _pt = classify_pattern_type("UNKNOWN", ipo_age_for_log, breakout_volume/avgv, prng)
                        _log_rejection_telemetry("failed_follow_through",
                                                 next_day_volume/breakout_volume, 0.8, current_metrics,
                                                 potential_entry=_ghost_entry,
                                                 potential_stop=_ghost_stop,
                                                 potential_target=_ghost_target,
                                                 pattern_type=_pt)
                        continue

                # Apply your proven filters
                if reject_quick_losers(df, j, w, avgv):
                    continue

                # --- Grade Assignment ---
                grade = assign_grade(score)
                
                # --- Institutional Grade Penalty (Phase 2.5) ---
                # Cap signals for microcaps to Grade C (Higher risk, smaller size)
                if mcap_cr > 0 and mcap_cr < 1000.0 and GRADE_ORDER.index(grade) > GRADE_ORDER.index("C"):
                    logger.info(f"⚠️ Downgrading {sym} from {grade} to C due to Microcap Penalty (< ₹1000Cr)")
                    grade = "C"
                
                # --- Cohort Validation (Multi-Bucket Research) ---
                valid_cohorts = []
                for name, config in RESEARCH_COHORTS.items():
                    # Window check
                    if w < config["min_window"]: continue
                    # Tightness check
                    if prng > config["max_prng"]: continue
                    # Grade check
                    if GRADE_ORDER.index(grade) < GRADE_ORDER.index(config["min_grade"]): continue
                    
                    # Follow-through (if available)
                    if j + 1 < len(df):
                        breakout_volume = df["VOLUME"].iat[j]
                        next_day_volume = df["VOLUME"].iat[j + 1]
                        if next_day_volume < config["vol_follow"] * breakout_volume:
                            continue
                    
                    valid_cohorts.append(name)

                if not valid_cohorts:
                    continue
                
                # Signal found - carry forward with valid_cohorts tagging
                current_metrics["valid_cohorts"] = valid_cohorts

                # This is a LIVE pattern - generate signal
                # Get the latest available data date (not system date to avoid future date issues)
                latest_data_date = df['DATE'].max()
                if isinstance(latest_data_date, pd.Timestamp):
                    latest_data_date = latest_data_date.date()
                elif hasattr(latest_data_date, 'date'):
                    latest_data_date = latest_data_date.date()
                
                # Use latest data date as entry date (not system date to avoid future dates)
                system_date = datetime.today().date()
                entry_date = latest_data_date
                
                # Validate entry_date is not in the future
                if entry_date > system_date:
                    logger.warning(f"⚠️ Entry date {entry_date} is in the future! Using latest data date instead.")
                    entry_date = latest_data_date
                
                # Cooldown: avoid spamming multiple signals for the same symbol in short time
                try:
                    from db import get_last_signal_date
                    last_signal_date = get_last_signal_date(sym)
                    if last_signal_date is not None:
                        if hasattr(last_signal_date, "date"):
                            last_signal_date = last_signal_date.date()
                        gap_days = (entry_date - last_signal_date).days
                        if gap_days < MIN_DAYS_BETWEEN_SIGNALS:
                            logger.info(
                                f"⏭️ Skipping {sym} - last signal {gap_days} days ago "
                                f"(< cooldown {MIN_DAYS_BETWEEN_SIGNALS} days)"
                            )
                            _log_consolidation_reject_once({"reason": "cooldown", "gap_days": gap_days})
                            continue
                except Exception as e:
                    logger.warning(f"Cooldown check failed for {sym}: {e}")

                # For live signals, ALWAYS use CURRENT market price as entry price
                # This ensures entry price matches what user would actually pay NOW
                # Try multiple sources: Upstox -> yfinance -> jugaad-data -> latest close
                live_price, price_source_name, _ = get_live_price(sym)
                if live_price is not None:
                    entry = live_price
                    source_emojis = {
                        'upstox': '🚀',
                        'yfinance': '📈',
                        'jugaad': '📊'
                    }
                    emoji = source_emojis.get(price_source_name, '💰')
                    price_source = f"{emoji} {price_source_name.title()} Live Price"
                    logger.info(f"✅ Using LIVE price from {price_source_name}: ₹{entry:.2f}")
                else:
                    # Fallback to latest available close price from historical data
                    entry = float(df["CLOSE"].iloc[-1])
                    latest_date = df['DATE'].iloc[-1]
                    if isinstance(latest_date, pd.Timestamp):
                        latest_date_str = latest_date.strftime('%Y-%m-%d')
                    else:
                        latest_date_str = str(latest_date)
                    price_source = f"📊 Latest Close ({latest_date_str})"
                    logger.warning(f"⚠️ No live price available, using latest close: ₹{entry:.2f} from {latest_date_str}")
                
                # --- PRICE FLOOR GUARDRAIL ---
                if entry < 20.0:
                    logger.info(f"⏭️ Skipping {sym} - Price ₹{entry:.2f} is below ₹20.00 floor")
                    _log_consolidation_reject_once({"reason": "price_below_floor", "price": round(entry, 2), "floor": 20.0, **metrics})
                    continue
                # -----------------------------

                # Entry date should be the next trading day after breakout
                if j + 1 < len(df):
                    entry_date = df['DATE'].iat[j + 1]
                    if isinstance(entry_date, pd.Timestamp):
                        entry_date = entry_date.date()
                    elif hasattr(entry_date, 'date'):
                        entry_date = entry_date.date()
                else:
                    # If no next day data, use latest available date
                    entry_date = latest_data_date
                
                # Final validation: ensure entry_date is not in the future
                if entry_date > system_date:
                    logger.warning(f"⚠️ Entry date {entry_date} is in the future! Using latest data date: {latest_data_date}")
                    entry_date = latest_data_date
                
                logger.info(f"Entry price for {sym}: ₹{entry:.2f} (from {price_source})")
                
                # Log detailed data for analysis
                logger.info(f"=== SIGNAL DATA FOR {sym} ===")
                logger.info(f"Pattern detected at index {j} (consolidation window: {w})")
                logger.info(f"Data range: {df['DATE'].min()} to {df['DATE'].max()}")
                logger.info(f"Breakout date: {df['DATE'].iat[j]}")
                logger.info(f"Entry date: {entry_date} (validated)")
                logger.info(f"Entry price: ₹{entry:.2f}")
                
                # Check data freshness
                latest_date = df['DATE'].max()
                if hasattr(latest_date, 'date'):
                    latest_date = latest_date.date()
                
                last_expected = get_last_expected_data_date()
                if latest_date < last_expected:
                    logger.warning(f"⚠️ DATA IS STALE! Latest: {latest_date}, Expected: {last_expected}")
                else:
                    logger.info(f"✅ Data is fresh: {latest_date}")
                
                logger.info(f"Breakout day data:")
                breakout_row = df.iloc[j]
                logger.info(f"  Date: {breakout_row['DATE']}, O={breakout_row['OPEN']:.2f} H={breakout_row['HIGH']:.2f} L={breakout_row['LOW']:.2f} C={breakout_row['CLOSE']:.2f}")
                if j + 1 < len(df):
                    next_row = df.iloc[j + 1]
                    logger.info(f"Entry day data (next day):")
                    logger.info(f"  Date: {next_row['DATE']}, O={next_row['OPEN']:.2f} H={next_row['HIGH']:.2f} L={next_row['LOW']:.2f} C={next_row['CLOSE']:.2f}")
                
                logger.info(f"Consolidation low: {low:.2f}")
                logger.info(f"Consolidation high: {high2:.2f}")
                logger.info(f"Breakout detected at index {j}: High={df['HIGH'].iat[j]:.2f} > Base High={high2:.2f}")
                logger.info(f"Grade: {grade} (Score: {score})")
                
                # Grade-based stop loss: More appropriate for IPO volatility
                stop, stop_pct = calculate_grade_based_stop_loss(entry, low, grade)
                risk_pct = (entry - stop) / entry * 100
                if risk_pct > 10.0:
                    logger.info(f"⏭️ Skipping {sym} - Stop risk {risk_pct:.2f}% exceeds hard 10% limit")
                    _log_consolidation_reject_once({"reason": "excessive_stop_risk", "risk_pct": round(risk_pct, 2), "max_allowed": 10.0, **current_metrics})
                    continue
                date = entry_date  # Use actual entry date from dataframe
                
                # Ensure date is a string in YYYY-MM-DD format for CSV
                if isinstance(date, pd.Timestamp):
                    date_str = date.strftime('%Y-%m-%d')
                elif hasattr(date, 'strftime'):
                    date_str = date.strftime('%Y-%m-%d')
                else:
                    date_str = str(date)
                
                # Bug 1 Fix: has_active_position MUST be checked first — it is the
                # unconditional business rule. signal_exists is secondary deduplication.
                try:
                    from db import has_active_position
                    if has_active_position(sym):
                        logger.info(f"⏭️ Skipping {sym} - already has active position")
                        _log_consolidation_reject_once({"reason": "active_position", "mode": "live", **current_metrics})
                        continue
                except Exception:
                    pass

                # Bug 4 Fix: include window size in signal ID to prevent collision
                # across different consolidation windows on the same date.
                sid = f"CONSOL_{sym}_{date_str.replace('-', '')}_{w}"
                try:
                    from db import signal_exists
                    if signal_exists(sid):
                        continue
                except Exception:
                    pass
                
                # Calculate target price using proper function based on consolidation pattern
                target = calculate_target_price(entry, low, high2, grade)

                # Validate reward:risk and extension above breakout level
                risk_amount = entry - stop
                reward_amount = target - entry
                if risk_amount <= 0 or reward_amount <= 0:
                    logger.info(f"⏭️ Skipping {sym} - invalid risk/reward (risk={risk_amount:.2f}, reward={reward_amount:.2f})")
                    _log_consolidation_reject_once({"reason": "invalid_risk_reward", "risk": round(risk_amount, 2), "reward": round(reward_amount, 2), **current_metrics})
                    continue
                risk_reward_ratio = reward_amount / risk_amount

                # Reject trades with poor risk/reward
                if risk_reward_ratio < MIN_RISK_REWARD:
                    logger.info(f"⏭️ Skipping {sym} - poor risk/reward 1:{risk_reward_ratio:.2f} (< {MIN_RISK_REWARD})")
                    _log_consolidation_reject_once({"reason": "poor_risk_reward", "ratio": round(risk_reward_ratio, 2), "min_required": MIN_RISK_REWARD, **current_metrics})
                    continue

                # Reject entries that are too extended above breakout level
                breakout_level = high2
                if breakout_level > 0:
                    distance_above = (entry / breakout_level - 1.0) * 100.0
                    if distance_above > MAX_ENTRY_ABOVE_BREAKOUT_PCT:
                        logger.info(
                            f"⏭️ Skipping {sym} - entry {distance_above:.2f}% above breakout "
                            f"(max allowed {MAX_ENTRY_ABOVE_BREAKOUT_PCT}%)"
                        )
                        _log_consolidation_reject_once({"reason": "too_extended", "distance_pct": round(distance_above, 2), "max_allowed": MAX_ENTRY_ABOVE_BREAKOUT_PCT, **current_metrics})
                        continue
                
                # Smart freshness filter: allow if stock is holding the breakout level
                # - Within 3 days: always allow
                # - 4-10 days: allow only if current price is still above breakout level
                # - >10 days: too old, skip
                breakout_date = df['DATE'].iat[j]
                if isinstance(breakout_date, pd.Timestamp):
                    breakout_date = breakout_date.date()
                elif hasattr(breakout_date, 'date'):
                    breakout_date = breakout_date.date()
                days_since_breakout = (datetime.today().date() - breakout_date).days
                
                if days_since_breakout > 10:
                    logger.info(f"⏭️ Skipping {sym} - breakout is {days_since_breakout} days old (>10 days, too stale)")
                    _log_consolidation_reject_once({"reason": "stale_breakout", "days_old": days_since_breakout, **current_metrics})
                    continue
                elif days_since_breakout > 3:
                    # Allow only if price is still holding above breakout level
                    if entry < high2:
                        logger.info(f"⏭️ Skipping {sym} - breakout {days_since_breakout} days old and price ₹{entry:.2f} has fallen below breakout level ₹{high2:.2f}")
                        _log_consolidation_reject_once({"reason": "stale_and_fallen", "days_old": days_since_breakout, "entry": round(entry, 2), "breakout_level": round(high2, 2), **current_metrics})
                        continue
                    else:
                        logger.info(f"✅ {sym} - breakout {days_since_breakout} days old but price ₹{entry:.2f} still holding above ₹{high2:.2f}")
                
                # Add to signals
                # Ensure date is a string in YYYY-MM-DD format
                if isinstance(date, pd.Timestamp):
                    date_str = date.strftime('%Y-%m-%d')
                elif hasattr(date, 'strftime'):
                    date_str = date.strftime('%Y-%m-%d')
                else:
                    date_str = str(date)

                # Lightweight score component decomposition for explainability logs
                vol_ratio_val = df["VOLUME"].iat[j] / avgv if avgv > 0 else 0.0
                tier_weight = 4.0 if grade == 'A+' else (3.0 if grade == 'A' else (2.0 if grade == 'B' else 1.0))
                volume_score = min(2.0, float(vol_ratio_val) / 2.0)
                base_score = 1.0
                momentum_score = 1.0
                total_score = min(10.0, tier_weight + volume_score + base_score + momentum_score)
                
                # Calculate tracking metrics
                ipo_age_days = 0
                try:
                    listing_date_val = None
                    if isinstance(ld, dict):
                        listing_date_val = ld.get("listingDate") or ld.get("listing_date")
                    else:
                        listing_date_val = ld
                    if listing_date_val is not None and pd.notna(listing_date_val):
                        listing_date_val = pd.to_datetime(listing_date_val).date()
                        ipo_age_days = (datetime.today().date() - listing_date_val).days
                except Exception:
                    ipo_age_days = 0
                dist_listing_high_pct = ((lhigh - entry) / lhigh * 100.0) if lhigh > 0 else 0.0
                vol_ratio_val = df["VOLUME"].iat[j] / avgv if avgv > 0 else 0.0
                
                tier_weight = 4.0 if grade == 'A+' else (3.0 if grade == 'A' else (2.0 if grade == 'B' else 1.0))
                volume_score = min(2.0, float(vol_ratio_val) / 2.0)
                base_score = 1.0
                momentum_score = 1.0
                total_score = min(10.0, tier_weight + volume_score + base_score + momentum_score)
                score_components = {
                    "tier_weight": round(tier_weight, 2),
                    "volume_score": round(volume_score, 2),
                    "base_score": round(base_score, 2),
                    "momentum_score": round(momentum_score, 2),
                    "total_score": round(total_score, 2)
                }
                
                # Bug 2 Fix: record breakout_close (fair reference price) separately
                # from entry (which may be a live intraday price). This prevents
                # look-ahead confusion in research and PnL attribution.
                breakout_close_ref = float(df["CLOSE"].iat[j])
                if live_price is not None and is_market_hours():
                    entry_note = "LIVE_INTRADAY"
                elif j + 1 < len(df):
                    entry_note = "NEXT_DAY_CLOSE"
                else:
                    entry_note = "FALLBACK_CLOSE"

                new_signal = {
                    "signal_id": sid,
                    "symbol": sym,
                    "signal_date": date_str,
                    "signal_time": datetime.now().strftime("%H:%M:%S"),
                    "entry_price": round(entry, 2),
                    # Bug 2: breakout_close = close of the candle that confirmed the breakout.
                    # This is the fair reference price for research; entry_price may differ
                    # if the scanner was live during market hours.
                    "breakout_close": round(breakout_close_ref, 2),
                    "entry_note": entry_note,
                    "consolidation_window": w,
                    "grade": grade,
                    "score": score,
                    "stop_loss": round(stop, 2),
                    "target_price": round(target, 2),
                    "status": "ACTIVE",
                    "exit_date": "",
                    "exit_price": 0,
                    "pnl_pct": 0,
                    "days_held": 0,
                    "signal_type": "CONSOLIDATION",
                    "version": SCANNER_VERSION,
                    "scanner": "consolidation_live",
                    # --- Tier fields (additive, backward-compatible) ---
                    "tier": grade,
                    "position_size_pct": 100 if grade == 'A+' else (60 if grade == 'A' else (40 if grade == 'B' else 20)),
                    "tier_rationale": "Consolidation Grade",
                    # --- Setup Quality & Behavioral Metrics ---
                    "ipo_age": ipo_age_days,
                    "distance_from_listing_high_pct": round(dist_listing_high_pct, 2),
                    "consolidation_range_pct": round(prng, 2),
                    "volume_ratio": round(vol_ratio_val, 2),
                    "volume_vs_listing_day": 0.0,
                    "risk_reward_ratio": round(risk_reward_ratio, 2) if 'risk_reward_ratio' in locals() else 0.0,
                    "confirmation_time_min": 0,
                    "max_extension_during_confirmation_pct": 0.0,
                    "rejection_depth_pct": 0.0,
                    "post_confirm_move_pct": round(distance_above, 2) if 'distance_above' in locals() else 0.0,
                    # --- Phase 2.5 Liquidity Hardening ---
                    "market_cap_cr": round(mcap_cr, 2),
                    "avg_turnover_cr": round(avg_turnover_cr, 2),
                    "circuit_days_15": int(circuit_days_15),
                    "did_hold_breakout_level": True,
                    "entry_vs_breakout_pct": round(distance_above, 2) if 'distance_above' in locals() else 0.0,
                    "signal_strength_score": score_components['total_score'],
                    # --- Score Components ---
                    "tier_weight": score_components.get("tier_weight", 0.0),
                    "volume_score": score_components.get("volume_score", 0.0),
                    "base_score": score_components.get("base_score", 0.0),
                    "momentum_score": score_components.get("momentum_score", 0.0),
                }
                
                # Write to daily log
                metrics["metric_ipo_age"] = sanitize_metric(ipo_age_days) if 'ipo_age_days' in locals() else None
                write_daily_log("consolidation", sym, "ACCEPTED_BREAKOUT", {**metrics, 
                    "grade": grade, "entry": round(entry, 2), "stop": round(stop, 2),
                    "target": round(target, 2), "score": score, "breakout_level": round(high2, 2),
                    "consolidation_window": w, "price_source": price_source,
                    "entry_vs_breakout_pct": round(distance_above, 2) if 'distance_above' in locals() else None,
                    "post_confirm_move_pct": round(distance_above, 2) if 'distance_above' in locals() else None,
                    "held_above_breakout_after_confirm": bool(entry >= high2),
                    "signal_strength_score": score_components.get("total_score", None),
                    "tier_weight": score_components.get("tier_weight", None),
                    "volume_score": score_components.get("volume_score", None),
                    "base_score": score_components.get("base_score", None),
                    "momentum_score": score_components.get("momentum_score", None),
                })
                
                # --- Institutional Snapshot Layer (Phase 2.2) ---
                if ANALYTICS_AVAILABLE and analytics_repo and signal_builder:
                    try:
                        # 1. Enrich (Breakout, Base, Market)
                        enriched = signal_builder.enricher.enrich_signal(
                            candle=df.iloc[j],
                            history=df.iloc[:j],
                            base_candles=df.iloc[max(0, j-w):j]
                        )
                        
                        # 2. Strict Binary Integrity Check
                        reasons = []
                        if not enriched.get("breakout") or "error" in enriched["breakout"]:
                            reasons.append("breakout_fingerprint_missing")
                        if not enriched.get("base") or "error" in enriched["base"]:
                            reasons.append("base_quality_missing")
                        if not enriched.get("market") or "error" in enriched["market"]:
                            reasons.append("market_context_missing")
                            
                        is_complete = len(reasons) == 0
                        
                        # 3. Build & Save
                        _pt = classify_pattern_type(grade, ipo_age_for_log, vol_ratio, prng)
                        _mr = get_market_regime(df["DATE"].iat[j])
                        _src = getattr(df, 'attrs', {}).get('data_source', 'unknown')
                        _snap = {
                            "pattern_type": _pt,
                            "market_regime": _mr,
                            "valid_cohorts": [c[0] for c in valid_cohorts] if 'valid_cohorts' in locals() else [],
                            "grade": grade,
                            "metrics_snapshot": current_metrics if 'current_metrics' in locals() else {},
                            "liquidity": {
                                "market_cap_cr": mcap_cr,
                                "avg_turnover_cr": avg_turnover_cr,
                                "circuit_days_15": circuit_days_15
                            },
                            "entry_at_signal": entry,
                            "stop_at_signal": stop,
                            "snapshot_ts": datetime.now(timezone.utc).isoformat()
                        }

                        inst_signal = signal_builder.build_signal(
                            raw_payload={**new_signal, 
                                         "log_id": locals().get('log_id', 'v1_link_missing'),
                                         "tier_weight": score_components.get("tier_weight"), 
                                         "volume_score": score_components.get("volume_score"),
                                         "base_score": score_components.get("base_score"),
                                         "momentum_score": score_components.get("momentum_score"),
                                         "signal_strength_score": score_components.get("total_score"),
                                         "pattern_type": _pt,
                                         "market_regime": _mr,
                                         "source_type": _src,
                                         "data_quality": "CONFIRMED" if _src == "Upstox API" else "FALLBACK",
                                         "decision_snapshot": _snap
                                         },
                            candle=df.iloc[j],
                            history=df.iloc[:j],
                            base_candles=df.iloc[max(0, j-w):j],
                            scanner_version=SCANNER_VERSION,
                            is_complete_snapshot=is_complete,
                            incomplete_reasons=reasons
                        )
                        
                        if analytics_repo.save_signal(inst_signal):
                            if is_complete:
                                v2_complete_count += 1
                                # Stratified Audit Logic
                                vol_z = inst_signal.breakout_fingerprint.get("volume_zscore", 0)
                                delta = abs(inst_signal.entry_price_delta_pct)
                                wick = inst_signal.breakout_fingerprint.get("upper_wick_pct", 1.0)
                                tightness = inst_signal.base_quality.get("tightness_index", 10.0)
                                
                                # Hunt for the "Textbook Case"
                                # Composite score: high vol, low wick, low tightness
                                quality_score = vol_z - (wick * 5) - (tightness / 2)
                                if not v2_audit_samples["CLEAN_BREAKOUT"] or quality_score > v2_audit_samples["CLEAN_BREAKOUT"]["metrics"]["quality"]:
                                    v2_audit_samples["CLEAN_BREAKOUT"] = {"id": inst_signal.signal_id, "metrics": {"quality": quality_score}}
                                    
                                if not v2_audit_samples["HIGH_VOL"] or vol_z > v2_audit_samples["HIGH_VOL"]["metrics"]["vol_z"]:
                                    v2_audit_samples["HIGH_VOL"] = {"id": inst_signal.signal_id, "metrics": {"vol_z": vol_z}}
                                if not v2_audit_samples["HIGH_DELTA"] or delta > v2_audit_samples["HIGH_DELTA"]["metrics"]["delta"]:
                                    v2_audit_samples["HIGH_DELTA"] = {"id": inst_signal.signal_id, "metrics": {"delta": delta}}
                                if not v2_audit_samples["RANDOM"]:
                                    v2_audit_samples["RANDOM"] = {"id": inst_signal.signal_id}
                            else:
                                v2_incomplete_count += 1
                                if not v2_audit_samples["FIRST_INCOMPLETE"]:
                                    v2_audit_samples["FIRST_INCOMPLETE"] = {"id": inst_signal.signal_id, "reasons": reasons}
                                logger.warning(f"⚠️ [Analytics] Incomplete snapshot for {sym}: {', '.join(reasons)}")
                                
                    except Exception as e:
                        analytics_failures += 1
                        v2_incomplete_count += 1
                        logger.error(f"❌ Failed to capture institutional snapshot for {sym}: {e}")
                        try:
                            # Save partial/failed signal for audit trail
                            fail_signal = signal_builder.build_signal(
                                raw_payload={**new_signal, "log_id": locals().get('log_id', 'v1_link_missing')},
                                candle=df.iloc[j],
                                history=df.iloc[:j],
                                base_candles=df.iloc[max(0, j-w):j],
                                scanner_version=SCANNER_VERSION,
                                is_complete_snapshot=False
                            )
                            analytics_repo.save_signal(fail_signal)
                            write_daily_log("analytics", sym, "ANALYTICS_FAILURE", {"error": str(e)}, log_type="ERROR")
                        except:
                            pass
                
                # Add to positions
                new_position = {
                    "symbol": sym,
                    "entry_date": date_str,
                    "entry_price": round(entry, 2),
                    "grade": grade,
                    "current_price": round(entry, 2),
                    "stop_loss": round(stop, 2),
                    "trailing_stop": round(stop, 2),
                    "pnl_pct": 0,
                    "days_held": 0,
                    "status": "ACTIVE"
                }
                
                # DB-only write: signal
                try:
                    from db import upsert_signal, db_metrics
                    upsert_signal(new_signal.copy())
                except Exception as db_e:
                    logger.error(f"[MongoDB] signal write FAILED for {sid}: {db_e}")
                    try:
                        from db import db_metrics
                        db_metrics["failures"] = db_metrics.get("failures", 0) + 1
                    except Exception:
                        pass
                
                # DB-only write: position
                try:
                    from db import upsert_position, db_metrics
                    upsert_position(new_position.copy())
                except Exception as db_e:
                    logger.error(f"[MongoDB] position write FAILED for {sym}: {db_e}")
                    try:
                        from db import db_metrics
                        db_metrics["failures"] = db_metrics.get("failures", 0) + 1
                    except Exception:
                        pass
                
                # Send Telegram notification with next day trading instructions
                days_old = (datetime.today().date() - latest_date).days
                if days_old > 1:
                    price_warning = f"⚠️ OLD DATA: {days_old} days old. Verify current price before trading!"
                else:
                    price_warning = f"✅ Fresh data - Ready for next day trading"
                
                # Get current/live price for verification (try to get fresh price)
                current_price_display = entry  # Entry price is the current/reference price
                try:
                    live_check, live_source, _ = get_live_price(sym)
                    if live_check is not None:
                        current_price_display = live_check
                        source_emojis = {
                            'upstox': '🚀',
                            'yfinance': '📈',
                            'jugaad': '📊'
                        }
                        emoji = source_emojis.get(live_source, '💰')
                        price_source_display = f"{emoji} Live: ₹{live_check:.2f} | Reference: ₹{entry:.2f}"
                    else:
                        price_source_display = f"📊 {price_source} | Reference: ₹{entry:.2f}"
                except:
                    price_source_display = f"📊 {price_source} | Reference: ₹{entry:.2f}"
                
                message = f"""🎯 <b>CONSOLIDATION BREAKOUT SIGNAL</b>

📊 Symbol: <b>{sym}</b>
📋 Signal Type: <b>Consolidation-Based Breakout</b>
{'🔥' if grade in ['A+', 'B'] else '📈'} Grade: <b>{grade}</b>

💰 <b>Price Information:</b>
• Current/Live Price: ₹{current_price_display:.2f}
• Entry Reference: ₹{entry:.2f} (Next Day Opening)
• Price Source: {price_source_display}

🛑 Stop Loss: ₹{stop:.2f}
📈 Target: ₹{target:.2f}
📅 Signal Date: {date_str}

🏗️ <b>Institutional Quality:</b>
• Market Cap: ₹{mcap_cr:.1f} Cr
• Avg Turnover: ₹{avg_turnover_cr:.2f} Cr/day
• Circuit Days (15d): {circuit_days_15}
{price_warning}

📋 <b>TRADING INSTRUCTIONS:</b>
• Enter at market open tomorrow
• Use ₹{entry:.2f} as reference price
• Set stop loss at ₹{stop:.2f}
• Target: ₹{target:.2f}
⚡ Consolidation pattern detected

🤖 Scanner v{SCANNER_VERSION} | {datetime.now().strftime('%Y-%m-%d %H:%M IST')}"""
                send_telegram(message)
                
                signals_found += 1
                processed_today.add(today_key)
                break  # break inner loop (over i) - found signal for this symbol
            if sym in processed_today or any(f"{sym}_" in k for k in processed_today): break  # break window loop for this symbol
        # DO NOT break the symbol loop — continue scanning other symbols
    
    logger.info(f"Live pattern scan complete: {signals_found} signals found from {symbols_processed} symbols")
    
    # Final Analytics Health Check (Forensic Audit Mode)
    if ANALYTICS_AVAILABLE and analytics_repo:
        v2_total = v2_complete_count + v2_incomplete_count
        trust_score = (v2_complete_count / signals_found) if signals_found > 0 else 1.0
        
        logger.info(f"🏛️ [Analytics] Forensic Audit Complete.")
        logger.info(f"   - Trust Score (Completeness): {trust_score:.2f}")
        logger.info(f"   - Signals (Total/Complete/Incomplete): {signals_found} / {v2_complete_count} / {v2_incomplete_count}")
        
        # Stratified Audit Blueprint
        logger.info(f"🔍 [Audit] FORENSIC BLUEPRINT: Verify these specific signals for correctness:")
        for category, sample in v2_audit_samples.items():
            if sample:
                msg = f"   👉 {category}: {sample['id']}"
                if category == "CLEAN_BREAKOUT": msg += " (Does this visually match your IDEAL setup?)"
                elif category == "HIGH_VOL": msg += f" (Check volume math, Z: {sample['metrics']['vol_z']})"
                elif category == "HIGH_DELTA": msg += f" (Check slippage logic, Delta: {sample['metrics']['delta']})"
                elif category == "FIRST_INCOMPLETE": msg += f" (Check why {', '.join(sample['reasons'])})"
                logger.info(msg)
        
        logger.info(f"⏰ [Audit] TIMEZONE CHECK: Verify candle_timestamp matches your chart's TZ (IST vs UTC).")
        
        if v2_total != signals_found:
            logger.error(f"❌ [Analytics] CRITICAL: Completeness Invariant Broken! Found {signals_found} but recorded {v2_total}.")
        
        if trust_score < 1.0:
            logger.warning(f"⚠️ [Analytics] Dataset Quality Degraded. Bias risk detected.")

    return signals_found

def detect_scan(symbols, listing_map):
    existing_signals_df = pd.DataFrame()
    signals_found = 0
    symbols_processed = 0
    processed_today = set()  # Track symbols processed today to prevent duplicates
    
    for sym in symbols:
        symbols_processed += 1
        if symbols_processed % 20 == 0:
            logger.info(f"Processed {symbols_processed}/{len(symbols)} symbols...")
        
        try:
            from db import has_active_position
            if has_active_position(sym):
                continue
        except Exception:
            pass
        
        # Check if we already processed this symbol today
        today_key = f"{sym}_{datetime.today().strftime('%Y%m%d')}"
        if today_key in processed_today:
            continue
            
        ld = listing_map.get(sym)
        if not ld: continue
        df = fetch_data(sym, ld)
        if df is None or df.empty: continue
        lhigh = df["HIGH"].iloc[0]

        _scan_reject_logged = set()

        def _log_scan_reject_once(details: dict):
            r = details.get("reason", "unknown")
            if r in _scan_reject_logged:
                return
            _scan_reject_logged.add(r)
            ipo_age_for_log = None
            try:
                listing_date_val = None
                if isinstance(ld, dict):
                    listing_date_val = ld.get("listingDate") or ld.get("listing_date")
                else:
                    listing_date_val = ld
                if listing_date_val is not None and pd.notna(listing_date_val):
                    listing_date_val = pd.to_datetime(listing_date_val).date()
                    ipo_age_for_log = (datetime.today().date() - listing_date_val).days
            except Exception:
                ipo_age_for_log = None
                
            # Determine failing metric name
            failing_metric_name = "unknown"
            for k in ["risk", "risk_pct", "ratio", "vol_ratio", "distance_pct", "days_old", "grade"]:
                if k in details:
                    failing_metric_name = k
                    break
                    
            actual_metric = details.get(
                "risk_pct", details.get(
                "risk",
                details.get("ratio", details.get("vol_ratio", details.get("distance_pct", details.get("days_old", details.get("grade", None))))))
            )
            required_metric = details.get(
                "max_allowed", details.get(
                "reward",
                details.get("min_required", None))
            )
            
            restructured_payload = {
                "symbol": sym,
                "stage": "post_confirm" if "days_old" in details else "pre_breakout",
                "rejection_reason": r,
                "failing_metric": failing_metric_name,
                "failing_value": actual_metric,
                "threshold": required_metric,
                "metrics": details.copy(),  # Ensures metrics field exists
                "ipo_age": ipo_age_for_log,
                "volume_ratio": details.get("vol_ratio", None),
                "original_details": details,
            }
            write_daily_log("consolidation", sym, "REJECTED_BREAKOUT", restructured_payload, log_type="REJECTED")

        for w in CONSOL_WINDOWS:
            if len(df) < w: continue
            for j in range(w, min(len(df), MAX_DAYS)):
                # 1. Define immediate base O(N)
                base_window = df.iloc[j-w:j]
                if len(base_window) < w: continue
                
                low = base_window["LOW"].min()
                high2 = base_window["HIGH"].max()
                
                # 2. Context Rule (Base Formation Zone)
                perf = (low - lhigh) / lhigh
                if not (0.08 <= -perf <= 0.35): continue
                
                # 3. Base Tightness
                prng = (high2 - low) / low * 100
                if prng > 60: continue
                
                # 4. Volume Checks
                avgv = base_window["VOLUME"].mean()
                if avgv <= 0: continue
                vol_ok = ((df["VOLUME"].iat[j] >= 2.5*avgv and df["VOLUME"].iloc[j-2:j+1].sum() >= 4*avgv) or
                         df["VOLUME"].iat[j]/avgv >= VOL_MULT or
                         (df["VOLUME"].iloc[j-2:j+1].sum() * df["CLOSE"].iat[j]) >= ABS_VOL_MIN)
                if not vol_ok: continue
                
                # 5. Breakout Confirmation
                is_live_breakout = False
                
                if j == len(df) - 1:
                    # Live candle
                    if is_market_hours():
                        live_price, _, _ = get_live_price(sym)
                    else:
                        live_price = float(df["CLOSE"].iloc[-1])
                    if live_price is not None and live_price > high2:
                        is_live_breakout = True
                        logger.info(f"🔥 LIVE breakout forming for {sym}: Live price ₹{live_price:.2f} > base high ₹{high2:.2f}")
                else:
                    # Historical confirmed candle
                    if df["CLOSE"].iat[j] > high2 and df["CLOSE"].iat[j] > df["OPEN"].iat[j]:
                        is_live_breakout = True
                
                if not is_live_breakout:
                    continue
                
                # Continue with breakout validation
                # Follow-through filter (relaxed): next day should show conviction
                if j + 1 < len(df):
                    breakout_close = df["CLOSE"].iat[j]
                    breakout_volume = df["VOLUME"].iat[j]
                    next_day_close = df["CLOSE"].iat[j + 1]
                    next_day_volume = df["VOLUME"].iat[j + 1]
                    base_high = df["HIGH"][j-w+1:j+1].max()

                    close_holds = next_day_close > base_high * 0.98  # Allow 2% pullback
                    volume_confirms = next_day_volume >= 0.8 * breakout_volume  # 80% volume ok
                    if not close_holds and not volume_confirms:
                        _log_scan_reject_once({"reason": "failed_follow_through", "close_holds": bool(close_holds), "volume_confirms": bool(volume_confirms)})
                        continue

                score, metrics = compute_grade_hybrid(df, j, w, avgv)
                grade = assign_grade(score)
                if grade == "D":
                    _log_scan_reject_once({"reason": "grade_d", "grade": grade, "mode": "scan", **metrics})
                    continue
                
                # This is a LIVE pattern - generate signal
                # Get the latest available data date (not system date to avoid future date issues)
                latest_data_date = df['DATE'].max()
                if isinstance(latest_data_date, pd.Timestamp):
                    latest_data_date = latest_data_date.date()
                elif hasattr(latest_data_date, 'date'):
                    latest_data_date = latest_data_date.date()
                
                # Use latest data date as entry date (not system date to avoid future dates)
                system_date = datetime.today().date()
                entry_date = latest_data_date
                
                # Validate entry_date is not in the future
                if entry_date > system_date:
                    logger.warning(f"⚠️ Entry date {entry_date} is in the future! Using latest data date instead.")
                    entry_date = latest_data_date
                
                # For live signals, ALWAYS use CURRENT market price as entry price
                # This ensures entry price matches what user would actually pay NOW
                # Try multiple sources: Upstox -> yfinance -> jugaad-data -> latest close
                live_price, price_source_name, _ = get_live_price(sym)
                if live_price is not None:
                    entry = live_price
                    source_emojis = {
                        'upstox': '🚀',
                        'yfinance': '📈',
                        'jugaad': '📊'
                    }
                    emoji = source_emojis.get(price_source_name, '💰')
                    price_source = f"{emoji} {price_source_name.title()} Live Price"
                    logger.info(f"✅ Using LIVE price from {price_source_name}: ₹{entry:.2f}")
                else:
                    # Fallback to latest available close price from historical data
                    entry = float(df["CLOSE"].iloc[-1])
                    latest_date = df['DATE'].iloc[-1]
                    if isinstance(latest_date, pd.Timestamp):
                        latest_date_str = latest_date.strftime('%Y-%m-%d')
                    else:
                        latest_date_str = str(latest_date)
                    price_source = f"📊 Latest Close ({latest_date_str})"
                    logger.warning(f"⚠️ No live price available, using latest close: ₹{entry:.2f} from {latest_date_str}")

                # --- PRICE FLOOR GUARDRAIL ---
                if entry < 20.0:
                    logger.info(f"⏭️ Skipping {sym} - Price ₹{entry:.2f} is below ₹20.00 floor")
                    _log_scan_reject_once({"reason": "price_below_floor", "price": round(entry, 2), "floor": 20.0, **metrics})
                    continue
                # -----------------------------
                
                # Entry date should be the next trading day after breakout
                if j + 1 < len(df):
                    entry_date = df['DATE'].iat[j + 1]
                    if isinstance(entry_date, pd.Timestamp):
                        entry_date = entry_date.date()
                    elif hasattr(entry_date, 'date'):
                        entry_date = entry_date.date()
                else:
                    # If no next day data, use latest available date
                    entry_date = latest_data_date
                
                # Final validation: ensure entry_date is not in the future
                if entry_date > system_date:
                    logger.warning(f"⚠️ Entry date {entry_date} is in the future! Using latest data date: {latest_data_date}")
                    entry_date = latest_data_date
                
                # Grade-based stop loss: More appropriate for IPO volatility
                stop, stop_pct = calculate_grade_based_stop_loss(entry, low, grade)
                risk_pct = (entry - stop) / entry * 100
                if risk_pct > 10.0:
                    logger.info(f"⏭️ Skipping {sym} - Stop risk {risk_pct:.2f}% exceeds hard 10% limit")
                    _log_scan_reject_once({"reason": "excessive_stop_risk", "risk_pct": round(risk_pct, 2), "max_allowed": 10.0, **metrics})
                    continue
                # Use actual entry date from dataframe
                date = entry_date
                
                # Bug 4 Fix: include window size in signal ID to prevent collision
                # across different consolidation windows on the same date.
                sid = f"CONSOL_{sym}_{date.strftime('%Y%m%d')}_{w}"

                # Bug 1 Fix: has_active_position MUST be the unconditional first guard.
                try:
                    from db import has_active_position, signal_exists
                    if has_active_position(sym):
                        logger.info(f"⏭️ Skipping {sym} - already has active position")
                        _log_scan_reject_once({"reason": "active_position", "mode": "scan", **metrics})
                        continue
                    if signal_exists(sid):
                        continue
                except Exception as e:
                    logger.error(f"Error checking DB for existing signal/position: {e}")
                    pass
                
                # Calculate better target price based on pattern
                target = calculate_target_price(entry, low, high2, grade)
                
                # Ensure date is a string in YYYY-MM-DD format
                if isinstance(date, pd.Timestamp):
                    date_str = date.strftime('%Y-%m-%d')
                elif hasattr(date, 'strftime'):
                    date_str = date.strftime('%Y-%m-%d')
                else:
                    date_str = str(date)
                
                # Bug 2 Fix: record breakout_close (fair reference) alongside entry
                breakout_close_ref_scan = float(df["CLOSE"].iat[j]) if j < len(df) else entry
                entry_note_scan = "LIVE_INTRADAY" if (live_price is not None and is_market_hours()) else ("NEXT_DAY_CLOSE" if j + 1 < len(df) else "FALLBACK_CLOSE")

                row = {
                    "signal_id": sid, "symbol": sym, "signal_date": date_str,
                    "signal_time": datetime.now().strftime("%H:%M:%S"),
                    "entry_price": entry,
                    "breakout_close": round(breakout_close_ref_scan, 2),
                    "entry_note": entry_note_scan,
                    "consolidation_window": w,
                    "grade": grade, "score": score,
                    "stop_loss": stop, "target_price": target,
                    "status": "ACTIVE", "exit_date": "", "exit_price": 0,
                    "pnl_pct": 0, "days_held": 0, "signal_type": "CONSOLIDATION",
                    "version": SCANNER_VERSION, "scanner": "consolidation_scan"
                }
                
                pos = {
                    "symbol": sym, "entry_date": date_str, "entry_price": entry,
                    "grade": grade, "current_price": entry, "stop_loss": stop,
                    "trailing_stop": stop, "pnl_pct": 0, "days_held": 0, "status": "ACTIVE"
                }
                
                # DB-only write
                try:
                    from db import upsert_signal, upsert_position, db_metrics
                    upsert_signal(row.copy())
                    upsert_position(pos.copy())
                except Exception as db_e:
                    logger.error(f"[MongoDB] DB write FAILED for {sym}: {db_e}")
                    try:
                        from db import db_metrics
                        db_metrics["failures"] = db_metrics.get("failures", 0) + 1
                    except Exception:
                        pass
                
                # Explainability components (local scope for logging)
                vol_ratio_val = df["VOLUME"].iat[j] / avgv if avgv > 0 else 0.0
                tier_weight = 4.0 if grade == 'A+' else (3.0 if grade == 'A' else (2.0 if grade == 'B' else 1.0))
                volume_score = min(2.0, float(vol_ratio_val) / 2.0)
                base_score = 1.0
                momentum_score = 1.0
                total_score = min(10.0, tier_weight + volume_score + base_score + momentum_score)

                # Write to daily log
                metrics["metric_ipo_age"] = sanitize_metric(ipo_age_days) if 'ipo_age_days' in locals() else None
                sig_doc = {
                    **metrics, 
                    "grade": grade, "entry": round(entry, 2), "stop": round(stop, 2),
                    "target": round(target, 2), "score": score, "breakout_level": round(high2, 2),
                    "consolidation_window": w, "mode": "scan", "price_source": price_source,
                    "entry_vs_breakout_pct": round((entry / high2 - 1.0) * 100.0, 2) if high2 > 0 else None,
                    "post_confirm_move_pct": round((entry / high2 - 1.0) * 100.0, 2) if high2 > 0 else None,
                    "held_above_breakout_after_confirm": bool(entry >= high2),
                    "signal_strength_score": round(total_score, 2),
                    "tier_weight": round(tier_weight, 2),
                    "volume_score": round(volume_score, 2),
                    "base_score": round(base_score, 2),
                    "momentum_score": round(momentum_score, 2),
                }
                
                write_daily_log("consolidation", sym, "ACCEPTED_BREAKOUT", sig_doc)
                
                # Get current/live price for verification (try to get fresh price)
                current_price_display = entry
                price_source_display = price_source
                try:
                    live_check, live_source, _ = get_live_price(sym)
                    if live_check is not None:
                        current_price_display = live_check
                        source_emojis = {
                            'upstox': '🚀',
                            'yfinance': '📈',
                            'jugaad': '📊'
                        }
                        emoji = source_emojis.get(live_source, '💰')
                        price_source_display = f"{emoji} {live_source.title()} Live Price"
                except Exception:
                    pass

                signal_msg = format_signal_alert(
                    sym, grade, entry, stop, target, score, date_str,
                    consolidation_low=low, consolidation_high=high2, breakout_price=entry,
                    data_source=data_source, current_price=current_price_display, price_source=price_source_display,
                    pattern_type=_pt if '_pt' in locals() else None, 
                    market_regime=_mr if '_mr' in locals() else None
                )
                # Add signal type and version to alert
                signal_msg = signal_msg.replace("🎯 <b>IPO BREAKOUT SIGNAL</b>", 
                                               "🎯 <b>CONSOLIDATION BREAKOUT SIGNAL</b>\n\n📋 <b>Signal Type:</b> Consolidation-Based Breakout")
                signal_msg += f"\n\n🤖 Scanner v{SCANNER_VERSION} | {datetime.now().strftime('%Y-%m-%d %H:%M IST')}"
                send_telegram(signal_msg)
                signals_found += 1
                logger.info(f"🎯 Signal found: {sym} - {grade} grade at {entry}")
                
                # Mark this symbol as processed today to prevent duplicates
                processed_today.add(today_key)
                break  # break inner loop (over i) - found signal for this symbol
            if sym in processed_today or any(f"{sym}_" in k for k in processed_today): break  # break window loop for this symbol
        # DO NOT break the symbol loop — continue scanning other symbols
    
    logger.info(f"📊 Scan complete: {signals_found} signals found from {symbols_processed} symbols processed")

    # LOG: Scan-level funnel summary — day-level dataset for tracking scanner activity
    try:
        from db import db_metrics
        db_stats = {
            "symbols_processed": symbols_processed,
            "signals_found": signals_found,
            "db_signals": db_metrics.get("signals_generated", 0),
            "db_logs": db_metrics.get("logs_written", 0),
            "db_failures": db_metrics.get("failures", 0),
            "version": SCANNER_VERSION
        }
    except Exception:
        db_stats = {"symbols_processed": symbols_processed, "signals_found": signals_found}

    write_daily_log("scanner", "SYSTEM", "SCAN_COMPLETED", db_stats)

    # Pre-calculate summary strings to avoid complex nested f-strings
    db_status = '✅ OK' if db_stats.get('db_failures', 0) == 0 else f"❌ {db_stats.get('db_failures')} FAILURES"
    detection_msg = '🎯 New signals detected! Check details above.' if signals_found > 0 else '✅ No new signals today - Market conditions normal.'
    
    # Send scan summary to Telegram
    summary_msg = f"""📊 <b>IPO Scanner Summary</b>
    
🔍 <b>Scan Results:</b>
• Symbols Processed: {symbols_processed}
• New Signals Found: {signals_found}
• Scan Date: {datetime.today().strftime('%Y-%m-%d %H:%M')}
• DB Status: {db_status}

{detection_msg}

📈 <b>Active Positions:</b> {get_active_positions_count()}"""
    
    send_telegram(summary_msg)
    return signals_found

def weekly_summary():
    """Generate detailed weekly summary with performance metrics"""
    from db import get_all_signals_df, get_all_positions_df
    df_signals = get_all_signals_df()
    df_positions = get_all_positions_df()
    
    # Weekly stats
    week_start = datetime.today() - timedelta(days=7)
    
    if not df_signals.empty and "signal_date" in df_signals.columns:
        # Ensure week_start is naive if signal_date is naive
        if df_signals["signal_date"].dt.tz is None and week_start.tzinfo is not None:
            week_start = week_start.replace(tzinfo=None)
        elif df_signals["signal_date"].dt.tz is not None and week_start.tzinfo is None:
            from db import IST
            week_start = week_start.replace(tzinfo=IST)
            
        weekly_signals = len(df_signals[df_signals["signal_date"] >= week_start])
    else:
        weekly_signals = 0
    if not df_positions.empty and "status" in df_positions.columns:
        active_positions = len(df_positions[df_positions["status"] == "ACTIVE"])
    else:
        active_positions = 0
    
    # Performance stats for active positions
    if active_positions > 0 and "pnl_pct" in df_positions.columns:
        active_df = df_positions[df_positions["status"] == "ACTIVE"]
        avg_pnl = active_df["pnl_pct"].mean()
        best_position = active_df.loc[active_df["pnl_pct"].idxmax()] if not active_df.empty else None
        worst_position = active_df.loc[active_df["pnl_pct"].idxmin()] if not active_df.empty else None
        
        performance_text = f"""
📈 <b>Performance Highlights:</b>
• Average P&L: {avg_pnl:.2f}%
• Best Position: {best_position['symbol']} ({best_position['pnl_pct']:.2f}%)
• Worst Position: {worst_position['symbol']} ({worst_position['pnl_pct']:.2f}%)"""
    else:
        performance_text = "\n📈 <b>Performance:</b> No active positions"
    
    msg = f"""📊 <b>Weekly Summary</b>
    
🔍 <b>This Week:</b>
• New Signals: {weekly_signals}
• Active Positions: {active_positions}
• Total Signals (All Time): {len(df_signals)}{performance_text}

📅 <b>Week Range:</b> {week_start.strftime('%Y-%m-%d')} to {datetime.today().strftime('%Y-%m-%d')}"""
    
    send_telegram(msg)

def monthly_review():
    """Generate detailed monthly review with comprehensive stats"""
    from db import get_all_signals_df, get_all_positions_df
    df_signals = get_all_signals_df()
    df_positions = get_all_positions_df()
    
    # Monthly stats
    month_start = datetime.today().replace(day=1)
    
    if not df_signals.empty and "signal_date" in df_signals.columns:
        # Ensure month_start is naive if signal_date is naive
        if df_signals["signal_date"].dt.tz is None and month_start.tzinfo is not None:
            month_start = month_start.replace(tzinfo=None)
        elif df_signals["signal_date"].dt.tz is not None and month_start.tzinfo is None:
            from db import IST
            month_start = month_start.replace(tzinfo=IST)
            
        monthly_signals = len(df_signals[df_signals["signal_date"] >= month_start])
    else:
        monthly_signals = 0
        
    total_signals = len(df_signals)
    
    # Grade distribution
    if total_signals > 0 and "grade" in df_signals.columns:
        grade_dist = df_signals["grade"].value_counts()
        grade_text = "\n".join([f"• {grade}: {count}" for grade, count in grade_dist.items()])
    else:
        grade_text = "• No signals yet"
    
    # Position stats
    if not df_positions.empty and "status" in df_positions.columns:
        active_positions = len(df_positions[df_positions["status"] == "ACTIVE"])
        closed_positions = len(df_positions[df_positions["status"] == "CLOSED"])
    else:
        active_positions = 0
        closed_positions = 0
    
    msg = f"""📊 <b>Monthly Review</b>
    
📈 <b>This Month ({month_start.strftime('%B %Y')}):</b>
• New Signals: {monthly_signals}
• Active Positions: {active_positions}
• Closed Positions: {closed_positions}

🎯 <b>All-Time Stats:</b>
• Total Signals: {total_signals}
• Grade Distribution:
{grade_text}

📅 <b>Review Period:</b> {month_start.strftime('%Y-%m-%d')} to {datetime.today().strftime('%Y-%m-%d')}"""
    
    send_telegram(msg)

def format_position_update_alert(symbol, current_price, entry_price, old_trailing, new_trailing, pnl_pct, days_held, grade):
    """Format position update alert"""
    pnl_emoji = "📈" if pnl_pct >= 0 else "📉"
    trailing_changed = "✅" if new_trailing > old_trailing else "➡️"
    
    msg = f"""🔄 <b>Position Update</b>

📊 Symbol: <b>{symbol}</b>
⭐ Grade: {grade}
💰 Current Price: ₹{current_price:,.2f}
💵 Entry Price: ₹{entry_price:,.2f}
{pnl_emoji} P&L: {pnl_pct:+.2f}%
📅 Days Held: {days_held}

🛑 Stop Loss:
• Old Trailing: ₹{old_trailing:,.2f}
• New Trailing: ₹{new_trailing:,.2f} {trailing_changed}

⏰ Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"""
    return msg

def stop_loss_update_scan():
    """Dedicated scan for updating stop losses on active positions"""
    logger.info("🔄 Starting stop-loss update scan...")
    from db import get_all_positions_df, upsert_position
    df_positions = get_all_positions_df()
    
    # Initialize outcome schema backward compatibility
    schema_cols = [
        "max_runup_pct",
        "max_drawdown_pct",
        "outcome_type",
        "holding_efficiency_pct",
        "time_to_failure_days",
        "time_to_failure_min",
    ]
    for c in schema_cols:
        if c not in df_positions.columns:
            df_positions[c] = None
    df_positions["max_runup_pct"] = df_positions["max_runup_pct"].fillna(0.0)
    df_positions["max_drawdown_pct"] = df_positions["max_drawdown_pct"].fillna(0.0)
    
    active_positions = df_positions[df_positions["status"] == "ACTIVE"]
    
    if active_positions.empty:
        send_telegram("📊 <b>Stop-Loss Update Scan</b>\n\n✅ No active positions to update.")
        return
    
    # Send pre-scan summary showing all positions that will be updated
    pre_scan_msg = f"""🔄 <b>Stop-Loss Update Scan Starting</b>

📊 <b>Active Positions to Update: {len(active_positions)}</b>

"""
    for idx, pos in active_positions.iterrows():
        sym = pos["symbol"]
        entry_price = pos["entry_price"]
        current_price = pos.get("current_price", entry_price)
        trailing_stop = pos.get("trailing_stop", pos.get("stop_loss", entry_price * 0.95))
        grade = pos.get("grade", "N/A")
        
        # Calculate days held
        try:
            entry_date = pos["entry_date"]
            if isinstance(entry_date, pd.Timestamp):
                entry_date = entry_date.date()
            elif hasattr(entry_date, 'date'):
                entry_date = entry_date.date()
            today_date = datetime.today().date()
            days_held = (today_date - entry_date).days
        except:
            days_held = "N/A"
        
        # Calculate PnL
        try:
            pnl = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
            pnl_emoji = "📈" if pnl >= 0 else "📉"
        except:
            pnl = 0
            pnl_emoji = "➡️"
        
        pre_scan_msg += f"""• <b>{sym}</b> ({grade})
  💰 Entry: ₹{entry_price:,.2f} | Current: ₹{current_price:,.2f}
  {pnl_emoji} P&L: {pnl:+.2f}% | 🛑 Stop: ₹{trailing_stop:,.2f}
  📅 Days: {days_held}

"""
    
    pre_scan_msg += f"\n⏰ <b>Scan Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    send_telegram(pre_scan_msg)
    
    updates_made = 0
    exits_triggered = 0
    failed_updates = []
    
    for idx, pos in active_positions.iterrows():
        sym = pos["symbol"]
        
        # Calculate days held first to skip 0-day positions
        try:
            entry_date = pos["entry_date"]
            if isinstance(entry_date, pd.Timestamp):
                entry_date = entry_date.date()
            elif isinstance(entry_date, str):
                entry_date = pd.to_datetime(entry_date).date()
            elif hasattr(entry_date, 'date'):
                entry_date = entry_date.date()
            
            today_date = datetime.today().date()
            days_held = (today_date - entry_date).days
            
            # Skip positions with 0 days held (just added today)
            if days_held <= 0:
                logger.info(f"⏭️ Skipping {sym} - position just added (0 days held). Will update tomorrow.")
                # Still persist the current price if available
                try:
                    p = pos.to_dict()
                    p["days_held"] = 0
                    upsert_position(p)
                except:
                    pass
                continue
        except Exception as e:
            logger.warning(f"Could not calculate days held for {sym}: {e}")
            # Continue anyway, but log the issue
        
        logger.info(f"Updating stop-loss for {sym}...")
        
        # Get current price
        try:
            # Use listing date as fallback if entry_date is invalid
            try:
                ipo_df = cache_recent_ipos()
                if not listing_map:
                    listing_map = {}
                listing_map = {
                    row["symbol"]: pd.to_datetime(row["listing_date"]).date()
                    for _, row in ipo_df.iterrows()
                }
                listing_date = listing_map.get(sym)
            except:
                listing_date = None
            
            # Use listing date if entry_date is in the future or invalid
            fetch_start_date = entry_date
            if entry_date > today_date or entry_date is None:
                if listing_date:
                    fetch_start_date = listing_date
                    logger.warning(f"⚠️ Entry date {entry_date} is invalid for {sym}, using listing date {listing_date}")
                else:
                    logger.error(f"❌ Cannot determine valid start date for {sym}")
                    failed_updates.append(sym)
                    continue
            
            # Get current price - prefer LIVE price, fallback to latest historical close
            current_price = None
            price_source = "Historical Close"
            current_data = None
            
            # Try to get live price first (more accurate for exit decisions)
            try:
                live_price, live_source, _ = get_live_price(sym)
                if live_price is not None and live_price > 0:
                    current_price = live_price
                    price_source = f"Live ({live_source})"
                    logger.info(f"✅ Using live price for {sym}: ₹{current_price:.2f} from {live_source}")
            except Exception as e:
                logger.debug(f"Could not get live price for {sym}: {e}")
            
            # Fallback to historical data if live price unavailable
            if current_price is None:
                current_data = fetch_data(sym, fetch_start_date)
                if current_data is None or current_data.empty:
                    logger.warning(f"Could not fetch data for {sym}")
                    failed_updates.append(sym)
                    # Send alert for failed update
                    failed_msg = f"""⚠️ <b>Position Update Failed</b>

📊 Symbol: <b>{sym}</b>
❌ Could not fetch current data
📅 Entry Date: {pos['entry_date']}
💰 Last Known Price: ₹{pos.get('current_price', pos['entry_price']):,.2f}

⏰ Scan Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}"""
                    send_telegram(failed_msg)
                    continue
                
                # Get latest date from historical data
                latest_date = current_data['DATE'].iloc[-1]
                if isinstance(latest_date, pd.Timestamp):
                    latest_date = latest_date.date()
                elif hasattr(latest_date, 'date'):
                    latest_date = latest_date.date()
                else:
                    latest_date = pd.to_datetime(latest_date).date()
                
                # CRITICAL: Validate data is from the latest expected trading session
                # Do NOT use stale data for exit decisions
                last_expected = get_last_expected_data_date()
                
                if latest_date < last_expected:
                    # Data is older than the last valid trading session
                    logger.error(f"❌ STALE DATA for {sym}: Latest data is {latest_date}, but expected {last_expected}. Cannot make exit decision with stale data!")
                    failed_updates.append(sym)
                    stale_msg = f"""⚠️ <b>Position Update Skipped - Stale Data</b>

📊 Symbol: <b>{sym}</b>
❌ Latest data: {latest_date}
⚠️ Expected: {last_expected}
⚠️ Cannot make exit decision with stale data
💰 Last Known Price: ₹{pos.get('current_price', pos['entry_price']):,.2f}
📅 Entry Date: {pos['entry_date']}

⏰ Scan Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}
💡 Will retry when live price or fresh data is available"""
                    send_telegram(stale_msg)
                    continue
                
                # Data is fresh (today or yesterday) - safe to use
                current_price = current_data["CLOSE"].iat[-1]
                latest_date_str = latest_date.strftime('%Y-%m-%d')
                price_source = f"Historical Close ({latest_date_str})"
                
                days_old = (datetime.today().date() - latest_date).days
                if days_old == 0:
                    logger.info(f"✅ Using today's historical close for {sym}: ₹{current_price:.2f}")
                else:
                    logger.warning(f"⚠️ Using yesterday's close for {sym}: ₹{current_price:.2f} (market may be closed)")
            
            entry_price = pos["entry_price"]
            old_trailing = pos["trailing_stop"]
            
            # Days held already calculated above, reuse it
            # But recalculate if entry_date was adjusted
            if days_held < 0:
                logger.error(f"⚠️ Negative days for {sym}: entry_date={entry_date}, today={today_date}")
                days_held = 0
                
            pnl = (current_price - entry_price) / entry_price * 100
            
            # Update peak metrics
            current_max_runup = float(pos.get("max_runup_pct", 0.0) or 0.0)
            current_max_drawdown = float(pos.get("max_drawdown_pct", 0.0) or 0.0)
            
            new_max_runup = max(current_max_runup, pnl)
            new_max_drawdown = min(current_max_drawdown, pnl)
            
            # Check for exits - use current price for exit decisions
            exit_reason = None
            is_winner_archetype = (new_max_runup >= 15.0)

            if current_price <= old_trailing:  # Use OLD trailing stop for exit check, not new one
                exit_reason = "Stop Loss"
            elif not is_winner_archetype:
                # Time-based stops only apply to non-winners (stale/dead trades)
                if days_held > 30 and current_price < entry_price * 0.95:
                    exit_reason = "Time Stop -5%"
                elif days_held > 60 and current_price < entry_price * 0.92:
                    exit_reason = "Time Stop -8%"
            
            if exit_reason:
                # Outcome Classification
                outcome_type = "NO_FOLLOW_THROUGH"
                time_to_failure_days = None
                time_to_failure_min = None
                
                if new_max_runup > 10.0 and days_held <= 5:
                    outcome_type = "FAST_WINNER"
                elif new_max_runup > 10.0 and days_held > 5:
                    outcome_type = "SLOW_WINNER"
                elif new_max_runup <= 3.0 and new_max_drawdown <= -3.0:
                    outcome_type = "FAILED_BREAKOUT"
                    time_to_failure_days = days_held
                elif new_max_runup < 1.0 and exit_reason == "Stop Loss":
                    outcome_type = "IMMEDIATE_FAILURE"
                    time_to_failure_days = days_held
                elif new_max_runup > 3.0 and new_max_runup <= 8.0:
                    outcome_type = "NO_FOLLOW_THROUGH"
                
                holding_efficiency_pct = None
                if new_max_runup >= 5.0:
                    holding_efficiency_pct = round((pnl / new_max_runup) * 100.0, 2)

                # Derived analytic metric for faster failure diagnostics (no strategy impact)
                if time_to_failure_days is not None:
                    time_to_failure_min = int(time_to_failure_days * 390)
                    
                # Close position - use current price (live or historical)
                df_positions.loc[idx, ["status", "exit_date", "exit_price", "pnl_pct", "days_held", "max_runup_pct", "max_drawdown_pct", "outcome_type", "holding_efficiency_pct", "time_to_failure_days", "time_to_failure_min"]] = [
                    "CLOSED", datetime.today().strftime("%Y-%m-%d"), current_price, pnl, days_held,
                    new_max_runup, new_max_drawdown, outcome_type, holding_efficiency_pct, time_to_failure_days, time_to_failure_min
                ]
                close_active_signal(sym, current_price, pnl, days_held, exit_reason)
                exits_triggered += 1
                
                # LOG: Position exit event — critical for month-end grade/exit-reason analysis
                write_daily_log("positions", sym, "POSITION_CLOSED", {
                    "exit_reason": exit_reason,
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(current_price, 2),
                    "pnl_pct": round(pnl, 2),
                    "days_held": days_held,
                    "grade": pos.get("grade", "?"),
                    "price_source": price_source,
                    "outcome_type": outcome_type,
                    "max_runup_pct": round(new_max_runup, 2),
                    "max_drawdown_pct": round(new_max_drawdown, 2),
                    "time_to_failure_days": time_to_failure_days,
                    "time_to_failure_min": time_to_failure_min,
                    "runup_before_drawdown_pct": round(new_max_runup, 2) if new_max_drawdown < 0 else None,
                })
                
                # Send exit alert
                exit_msg = format_exit_alert(sym, exit_reason, current_price, pnl, days_held, entry_price)
                exit_msg += f"\n\n📊 <b>Outcome:</b> {outcome_type} (Peak: +{new_max_runup:.1f}%)"
                send_telegram(exit_msg)
            else:
                # Update position and (optionally) trail stop-loss
                # Default: no change in trailing stop
                new_trailing = float(old_trailing)

                # Only start trailing once we have a reasonable profit cushion
                if pnl >= MIN_PNL_FOR_TRAIL:
                    # Calculate new candidate trailing stop from grade-based percentage
                    grade = pos.get("grade", "C")  # Default to C if grade not available
                    _, stop_pct = calculate_grade_based_stop_loss(entry_price, entry_price, grade)

                    candidate_trailing = current_price * (1 - stop_pct)

                    # Minimum absolute improvement required (as % of entry)
                    min_trail_move_abs = entry_price * (MIN_TRAIL_MOVE_PCT / 100.0)

                    if candidate_trailing > new_trailing and (candidate_trailing - new_trailing) >= min_trail_move_abs:
                        new_trailing = candidate_trailing

                # Persist updated position
                df_positions.loc[idx, ["current_price", "trailing_stop", "pnl_pct", "days_held", "max_runup_pct", "max_drawdown_pct"]] = [
                    current_price, new_trailing, pnl, days_held, new_max_runup, new_max_drawdown
                ]

                # LOG: Daily snapshot — every position every trading day (core dataset for tuning)
                write_daily_log("positions", sym, "DAILY_SNAPSHOT", {
                    "current_price": round(current_price, 2),
                    "entry_price": round(entry_price, 2),
                    "pnl_pct": round(pnl, 2),
                    "trailing_stop": round(new_trailing, 2),
                    "days_held": days_held,
                    "grade": pos.get("grade", "?"),
                    "price_source": price_source,
                    "max_runup_pct": round(new_max_runup, 2),
                    "max_drawdown_pct": round(new_max_drawdown, 2),
                })

                # Count only real trailing-stop improvements as "updates"
                if new_trailing > old_trailing:
                    updates_made += 1

                    # LOG: Trailing stop improvement — key for understanding stop-protection quality
                    write_daily_log("positions", sym, "TRAILING_STOP_UPDATED", {
                        "old_stop": round(old_trailing, 2),
                        "new_stop": round(new_trailing, 2),
                        "current_price": round(current_price, 2),
                        "pnl_pct": round(pnl, 2),
                        "days_held": days_held,
                        "grade": pos.get("grade", "?")
                    })

                    # Send position update alert only when stop-loss actually moves
                    update_msg = format_position_update_alert(
                        sym, current_price, entry_price, old_trailing, new_trailing, pnl, days_held, grade
                    )
                    send_telegram(update_msg)

                # Persist to DB (Critical Fix: was missing)
                try:
                    updated_pos = df_positions.loc[idx].to_dict()
                    # Clean up for MongoDB (remove NaN/None if any)
                    # Use a safer check that works with scalars and arrays
                    clean_pos = {}
                    for k, v in updated_pos.items():
                        try:
                            if pd.isna(v): continue
                        except:
                            pass # If v is an array, keep it
                        clean_pos[k] = v
                    upsert_position(clean_pos)
                except Exception as db_e:
                    logger.error(f"Failed to persist position update for {sym}: {db_e}")
        except Exception as e:
            logger.error(f"Error updating {sym}: {e}")
            failed_updates.append(sym)
            # Send alert for error
            error_msg = f"""❌ <b>Position Update Error</b>

📊 Symbol: <b>{sym}</b>
⚠️ Error: {str(e)}
📅 Entry Date: {pos['entry_date']}
💰 Last Known Price: ₹{pos.get('current_price', pos['entry_price']):,.2f}

⏰ Scan Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}"""
            send_telegram(error_msg)
            continue
    
    # Positions are written row-by-row via upsert_position inside the loop; no batch write needed.

    # Send summary
    summary_msg = f"""🔄 <b>Stop-Loss Update Scan Complete</b>
    
📊 <b>Results:</b>
✅ Positions Updated: {updates_made}
🚪 Positions Closed: {exits_triggered}
⚠️ Failed Updates: {len(failed_updates)}
📈 Active Positions: {len(active_positions) - exits_triggered}"""
    
    if failed_updates:
        summary_msg += f"\n\n❌ <b>Failed Symbols:</b> {', '.join(failed_updates)}"
    
    summary_msg += f"\n\n⏰ <b>Scan Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    
    send_telegram(summary_msg)
    logger.info(f"Stop-loss update complete: {updates_made} updated, {exits_triggered} closed, {len(failed_updates)} failed")

def heartbeat():
    """Send heartbeat to confirm scanner is alive"""
    logger.info("💓 Sending heartbeat...")
    try:
        active_positions = get_active_positions_count()
        message = f"💓 <b>Scanner Heartbeat</b>\n\n⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n📈 Active Positions: {active_positions}"
        logger.info(f"Heartbeat message: {message}")
        send_telegram(message)
        logger.info("✅ Heartbeat sent successfully")
    except Exception as e:
        logger.error(f"❌ Heartbeat failed: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode",choices=["scan","weekly_summary","monthly_review","stop_loss_update","heartbeat","dry_run"],
                        nargs="?",default="scan")
    parser.add_argument("--bypass-holiday", action="store_true", help="Run scanner even if it's a holiday/weekend")
    args = parser.parse_args()

    # Show mode identification
    try:
        print("==========================================")
        print("IPO Scanner Started")
        print("==========================================")
    except UnicodeEncodeError:
        print("==========================================")
        print("IPO Scanner Started")
        print("==========================================")
    
    try:
        print(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
        print(f"Time: {datetime.now().strftime('%H:%M:%S')}")
        print(f"Mode: {args.mode.upper()}")
        print("==========================================")
    except UnicodeEncodeError:
        print(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
        print(f"Time: {datetime.now().strftime('%H:%M:%S')}")
        print(f"Mode: {args.mode.upper()}")
        print("==========================================")

    auto_refresh_upstox_token()

    # --- Market Holiday Guard ---
    # For scan and stop_loss_update: verify today is an NSE trading day.
    # Weekly/monthly summaries and heartbeat always run regardless of market day.
    scan_modes = {"scan", "stop_loss_update", "dry_run"}
    if args.mode in scan_modes and not is_market_day() and not args.bypass_holiday:
        from datetime import timezone, timedelta as td
        ist = timezone(td(hours=5, minutes=30))
        today_ist = datetime.now(ist).strftime("%Y-%m-%d")
        skip_msg = (
            f"📅 <b>Market Holiday / Non-Trading Day</b>\n\n"
            f"🗓 Date: {today_ist}\n"
            f"⏭ Scanner skipped — NSE is closed today.\n"
            f"✅ Next scan will run on the next trading day."
        )
        logger.info(f"📅 Market is closed today ({today_ist}). Skipping {args.mode} run.")
        send_telegram(skip_msg)
        sys.exit(0)
    # --- End Holiday Guard ---

    update_positions()
    symbols, listing_map = get_symbols_and_listing()
    

    def generate_daily_summary():
        try:
            today_str = datetime.today().strftime('%Y-%m-%d')
            today_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            todays_log_dir = os.path.join("logs", today_str)
            os.makedirs(todays_log_dir, exist_ok=True)
            
            summary = {
                "date": today_str,
                "total_signals": 0,
                "tier_distribution": {"A+": 0, "A": 0, "B": 0, "C": 0, "D": 0, "UNKNOWN": 0},
                "avg_signal_score": 0.0,
                "top_score": 0.0,
                "rejections": {}
            }
            
            # 1. Parse signals from MongoDB
            try:
                from db import get_all_signals_df
                sig_df = get_all_signals_df()
                if not sig_df.empty and "signal_date" in sig_df.columns:
                    sig_df["signal_date"] = sig_df["signal_date"].astype(str).str[:10]
                    todays_signals = sig_df[sig_df["signal_date"] == today_str]
                    summary["total_signals"] = len(todays_signals)

                    if len(todays_signals) > 0:
                        total_score = 0.0
                        for _, row in todays_signals.iterrows():
                            t = row.get("tier", "N/A")
                            if pd.isna(t) or str(t).strip() == "":
                                t = "UNKNOWN"
                            elif t not in summary["tier_distribution"]:
                                t = "UNKNOWN"
                            if t in summary["tier_distribution"]:
                                summary["tier_distribution"][t] += 1

                            score = float(row.get("signal_strength_score", 0.0) or 0.0)
                            total_score += score
                            summary["top_score"] = max(summary["top_score"], score)

                        summary["avg_signal_score"] = round(total_score / len(todays_signals), 2)
            except Exception as e:
                logger.error(f"Error parsing signals for summary: {e}")
            
            # 2. Parse rejections from MongoDB (Phase 2.5)
            try:
                from db import logs_col
                if logs_col is not None:
                    # Fetch all rejections for today from DB
                    rejection_logs = logs_col.find({
                        "timestamp": {"$gte": today_dt},
                        "action": {"$in": ["REJECTED_BREAKOUT", "PENDING_REJECTED"]}
                    })
                    for entry in rejection_logs:
                        details = entry.get("details", {}) or {}
                        reason = details.get("rejection_reason", details.get("reason", "unknown"))
                        summary["rejections"][reason] = summary["rejections"].get(reason, 0) + 1
                else:
                    logger.warning("logs_col is None, skipping rejection summary")
            except Exception as e:
                logger.error(f"Error parsing rejections from MongoDB: {e}")
            
            # Dump to JSON
            summary_file = os.path.join(todays_log_dir, "daily_summary.json")
            with open(summary_file, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)
            logger.info(f"Daily summary generated at {summary_file}")
            
        except Exception as e:
            logger.error(f"Failed to generate daily summary: {e}")


    if args.mode == "scan":
        signals_found = detect_live_patterns(symbols, listing_map)
        logger.info(f"✅ Live pattern scan completed successfully! Found {signals_found} signals.")
        generate_daily_summary()
    elif args.mode == "weekly_summary":
        weekly_summary()
    elif args.mode == "monthly_review":
        monthly_review()
    elif args.mode == "stop_loss_update":
        stop_loss_update_scan()
        generate_daily_summary()
    elif args.mode == "heartbeat":
        heartbeat()
    else:
        logger.info("Dry run complete (no writes or Telegram)")
