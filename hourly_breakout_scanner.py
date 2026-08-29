#!/usr/bin/env python3
"""
hourly_breakout_scanner.py

Hourly intraday breakout scanner for watchlist symbols:
- Reads symbols from watchlist.csv
- Fetches intraday data (5-minute candles)
- Detects real-time breakouts
- Sends alerts for immediate action
"""

import os
import sys
import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
import logging

# Load environment
load_dotenv()

# Force UTF-8 encoding on standard output for Windows console compatibility
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# Configuration
# Metadata & State (Legacy CSVs removed, using MongoDB)
INTRADAY_INTERVAL = "5minute"  # 1minute, 5minute, 15minute, 30minute, 60minute
LOOKBACK_DAYS = 5  # Days of intraday data to fetch
MIN_VOLUME_MULTIPLIER = 1.5  # Minimum volume spike for breakout

# Telegram configuration
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Add current directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Import from main scanner
import importlib.util
spec = importlib.util.spec_from_file_location("scanner", "streamlined_ipo_scanner.py")
scanner_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner_module)

# Import shared utilities
get_market_regime = scanner_module.get_market_regime
classify_pattern_type = scanner_module.classify_pattern_type
get_live_price = scanner_module.get_live_price
is_market_day = getattr(scanner_module, 'is_market_day', lambda: True)
send_holiday_notification_once = getattr(scanner_module, 'send_holiday_notification_once', None)
if send_holiday_notification_once is None:
    from utils import send_holiday_notification_once
write_daily_log = scanner_module.write_daily_log  # Use shared writer — prevents version drift

SCANNER_VERSION = "3.5.0"  # v3.5.0: Upper 50% Candle Body Gate, 14-Day Velocity Gate, Anti-Chasing Extension Guard

# write_daily_log is now imported from scanner_module above (shared writer).

def send_telegram(msg):
    """Send Telegram notification"""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning(f"[Telegram disabled] BOT_TOKEN: {'SET' if BOT_TOKEN else 'MISSING'}, CHAT_ID: {'SET' if CHAT_ID else 'MISSING'}")
        return
    
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    try:
        response = requests.post(url, json={
            "chat_id": CHAT_ID, 
            "text": msg, 
            "parse_mode": "HTML",
            "disable_notification": False
        }, timeout=10)
        
        if response.status_code == 200:
            logger.info("✅ Telegram message sent successfully!")
        else:
            logger.error(f"❌ Telegram API error: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"❌ Telegram error: {e}")

def load_watchlist():
    """Load active symbols from MongoDB watchlist collection."""
    try:
        from db import watchlist_col
        if watchlist_col is None:
            logger.warning("[DB] watchlist_col unavailable")
            return []
        
        docs = list(watchlist_col.find({"status": "ACTIVE"}, {"symbol": 1, "_id": 0}))
        active_symbols = [d["symbol"] for d in docs if d.get("symbol")]
        
        logger.info(f"📋 Loaded {len(active_symbols)} active symbols from MongoDB watchlist")
        return active_symbols
    
    except Exception as e:
        logger.error(f"Error loading watchlist from MongoDB: {e}")
        return []

# Try to import yfinance for intraday data
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

def fetch_intraday_data_yfinance(symbol, interval=INTRADAY_INTERVAL):
    """Fetch intraday data from yfinance API with rate limiting"""
    if not YFINANCE_AVAILABLE:
        return None
    
    try:
        # Rate limiting: 200ms minimum delay
        time.sleep(0.2)
        
        # Map interval to yfinance format
        interval_map = {
            '1minute': '1m',
            '5minute': '5m',
            '15minute': '15m',
            '30minute': '30m',
            '60minute': '1h'
        }
        yf_interval = interval_map.get(interval, '5m')
        
        # NSE symbols need .NS suffix
        ticker_symbol = f"{symbol}.NS"
        ticker = yf.Ticker(ticker_symbol)
        
        # Fetch intraday data (max 7 days for intraday)
        period = min(LOOKBACK_DAYS, 7)
        try:
            scanner_module.network_call_made = True
        except Exception:
            pass
        df = ticker.history(period=f"{period}d", interval=yf_interval)
        
        if df.empty:
            return None
        
        # Rename columns to match expected format
        df = df.reset_index()
        date_col = next((c for c in df.columns if 'date' in str(c).lower() or 'time' in str(c).lower()), df.columns[0])
        df = df.rename(columns={
            date_col: 'DATE',
            'Open': 'OPEN',
            'High': 'HIGH',
            'Low': 'LOW',
            'Close': 'CLOSE',
            'Volume': 'VOLUME'
        })
        df = df[['DATE', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']]
        df['LTP'] = df['CLOSE']
        
        # Ensure DATE is datetime (should already be from yfinance, but verify)
        if not pd.api.types.is_datetime64_any_dtype(df['DATE']):
            df['DATE'] = pd.to_datetime(df['DATE'])
        
        # Sort by date (ascending - oldest to newest)
        df = df.sort_values('DATE').reset_index(drop=True)
        
        logger.info(f"✅ Got {len(df)} intraday candles from yfinance for {symbol}")
        return df
        
    except Exception as e:
        logger.warning(f"⚠️ yfinance error for {symbol}: {e}")
        return None

def fetch_intraday_data_upstox(symbol, interval=INTRADAY_INTERVAL):
    """Fetch intraday data from Upstox API"""
    try:
        # Load IPO mappings
        try:
            from db import get_instrument_key_mapping
            mapping = get_instrument_key_mapping()
            instrument_key = mapping.get(symbol)
            
            if not instrument_key:
                logger.warning(f"Symbol {symbol} not found in Upstox mapping (MongoDB)")
                return None
        except Exception as e:
            logger.warning(f"Error getting Upstox mapping from MongoDB for {symbol}: {e}")
            return None
        
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
        
        # Calculate date range
        end_date = datetime.now()
        start_date = end_date - timedelta(days=LOOKBACK_DAYS)
        
        # Format dates for Upstox API
        from_str = start_date.strftime('%Y-%m-%d')
        to_str = end_date.strftime('%Y-%m-%d')
        
        # Upstox intraday API endpoint
        url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/{interval}/{to_str}/{from_str}"
        
        logger.info(f"🔄 Fetching intraday data for {symbol} ({interval})...")
        try:
            scanner_module.network_call_made = True
        except Exception:
            pass
        response = requests.get(url, headers=headers, timeout=30)
        
        # Handle rate limiting
        if response.status_code == 429:
            logger.warning(f"⚠️ Rate limited for {symbol}, waiting 1 second...")
            time.sleep(1)
            response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            if 'data' in data and 'candles' in data['data']:
                candles = data['data']['candles']
                if candles:
                    # P0-7 Fix: Upstox returns 7 fields [ts, open, high, low, close, volume, oi].
                    # Previous code had duplicate 'close' column causing ValueError on rename.
                    # Use safe rename pattern that is robust to field count changes.
                    raw_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi']
                    # Pad or trim to match actual number of columns returned
                    actual_cols = raw_cols[:len(candles[0])] if candles else raw_cols
                    df = pd.DataFrame(candles, columns=actual_cols)
                    
                    # Handle timestamp conversion
                    try:
                        df['DATE'] = pd.to_datetime(df['timestamp'], unit='s')
                    except Exception:
                        try:
                            df['DATE'] = pd.to_datetime(df['timestamp'])
                        except Exception:
                            df['DATE'] = pd.to_datetime(df['timestamp'], format='%Y-%m-%d %H:%M:%S')
                    
                    # Rename OHLCV columns explicitly — safe regardless of extra cols
                    df = df.rename(columns={
                        'open': 'OPEN', 'high': 'HIGH', 'low': 'LOW',
                        'close': 'CLOSE', 'volume': 'VOLUME'
                    })
                    df = df[['DATE', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']]
                    df['LTP'] = df['CLOSE']
                    
                    # Ensure DATE is datetime
                    if not pd.api.types.is_datetime64_any_dtype(df['DATE']):
                        df['DATE'] = pd.to_datetime(df['DATE'])
                    
                    # Sort ascending (oldest to newest)
                    df = df.sort_values('DATE').reset_index(drop=True)
                    
                    logger.info(f"✅ Got {len(df)} intraday candles for {symbol}")
                    return df
        
        logger.warning(f"⚠️ No intraday data for {symbol}")
        return None
        
    except Exception as e:
        logger.warning(f"⚠️ Upstox API error for {symbol}: {e}")
        return None

def fetch_intraday_data(symbol, interval=INTRADAY_INTERVAL):
    """
    Fetch intraday data from multiple sources with fallback:
    1. Upstox API (if available)
    2. yfinance (fallback)
    
    Returns DataFrame or None
    """
    # Try Upstox first
    df = fetch_intraday_data_upstox(symbol, interval)
    if df is not None and not df.empty:
        return df
    
    # Fallback to yfinance
    logger.info(f"⚠️ Upstox failed, trying yfinance for {symbol}...")
    df = fetch_intraday_data_yfinance(symbol, interval)
    if df is not None and not df.empty:
        return df
    
    logger.warning(f"⚠️ Could not fetch intraday data for {symbol} from any source")
    return None

def compute_rsi(close, period=14):
    """Calculate RSI"""
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def detect_intraday_breakout(df, symbol, bulk_prices=None):
    """Detect intraday breakout patterns using LIVE prices for accurate detection"""
    if df is None or len(df) < 20:
        return None
    
    try:
        # Get recent data (last 2 days of intraday candles)
        # For 5-minute candles: 2 days = ~96 candles (assuming 9:15 AM to 3:30 PM = 6.25 hours = 75 candles/day)
        recent_df = df.tail(150)  # Last 150 candles (~2 days)
        
        if len(recent_df) < 20:
            return None
        
        # Calculate consolidation levels from historical data (exclude last candle for accurate range)
        historical_df = recent_df.iloc[:-1] if len(recent_df) > 1 else recent_df
        
        # Calculate recent high and low from historical data (excluding current candle)
        recent_high = historical_df['HIGH'].max() if len(historical_df) > 0 else recent_df['HIGH'].max()
        recent_low = historical_df['LOW'].min() if len(historical_df) > 0 else recent_df['LOW'].min()
        
        # Calculate consolidation range (last 50 candles, excluding current)
        consolidation_df = historical_df.tail(50) if len(historical_df) >= 50 else historical_df
        consolidation_low = consolidation_df['LOW'].min() if len(consolidation_df) > 0 else recent_low
        consolidation_high = consolidation_df['HIGH'].max() if len(consolidation_df) > 0 else recent_high
        
        # Volume confirmation must come from the *same* decision bar as price,
        # not an older non-zero spike while live LTP only just crossed.
        # recent_high is computed from historical_df (excludes last candle).
        last_bar_volume = float(recent_df["VOLUME"].fillna(0).astype(float).iloc[-1])
        last_bar_high = float(recent_df["HIGH"].iloc[-1])
        hist_vols = historical_df["VOLUME"].fillna(0).astype(float) if len(historical_df) > 0 else recent_df["VOLUME"].fillna(0).astype(float)
        hist_nonzero = hist_vols[hist_vols > 0]
        avg_volume = float(hist_nonzero.mean()) if len(hist_nonzero) > 0 else 0.0
        current_volume = last_bar_volume
        volume_ratio = (current_volume / avg_volume) if avg_volume > 0 else 0.0
        volume_ok = avg_volume > 0 and current_volume >= avg_volume * MIN_VOLUME_MULTIPLIER
        
        # CRITICAL: Get LIVE price for accurate breakout detection.
        live_price = None
        live_source = "Historical"
        if bulk_prices and symbol in bulk_prices:
            quote = bulk_prices[symbol]
            live_price = quote[0] if hasattr(quote, "__getitem__") else getattr(quote, "close", None)
            live_source = "Upstox-Bulk"
            logger.info(f"✅ Using live price for {symbol} breakout detection: ₹{live_price:.2f} (Upstox-Bulk)")
        else:
            try:
                live_price, live_source, _, _vol = get_live_price(symbol)
                if live_price is not None and live_price > 0:
                    logger.info(f"✅ Using live price for {symbol} breakout detection: ₹{live_price:.2f} ({live_source})")
            except Exception as e:
                logger.debug(f"Could not get live price for {symbol}: {e}")
        
        # Use live price if available, otherwise use latest historical close
        if live_price is not None:
            current_price = live_price
            current_high = live_price  # For breakout detection, use live price as current high
        else:
            current_price = float(recent_df['CLOSE'].iloc[-1])
            current_high = float(recent_df['HIGH'].iloc[-1])
            live_source = "Historical"  # Ensure source is set to Historical when using historical data
            logger.warning(f"⚠️ Using historical price for {symbol}: ₹{current_price:.2f}")
        
        # Breakout conditions:
        # 1. Current price/high breaks above recent high (using LIVE price if available)
        # 2. Volume spike on the breakout bar (required — at least 1.5x average)
        # 3. RSI momentum confirmation (optional strength point)
        is_breakout = False
        breakout_strength = 0
        
        # Check if price breaks above recent high
        if current_high > recent_high:
            is_breakout = True
            breakout_strength += 1
            logger.info(f"🔥 {symbol}: Price broke above recent high! ({current_high:.2f} > {recent_high:.2f}) [Using: {live_source}]")
        
        # If live LTP crossed but the last candle itself never broke the range,
        # last-bar volume is not confirmation of *this* breakout — reject.
        if is_breakout and volume_ok and last_bar_high <= recent_high:
            logger.info(
                f"⏭️ {symbol}: Live breakout without breakout-bar volume "
                f"(last_bar_high={last_bar_high:.2f} <= recent_high={recent_high:.2f}, "
                f"vol={current_volume:,.0f})"
            )
            volume_ok = False
            volume_ratio = (current_volume / avg_volume) if avg_volume > 0 else 0.0

        # Volume confirmation (mandatory for alert — price+RSI alone caused AZAD 0.0x alerts)
        if volume_ok:
            breakout_strength += 1
            logger.info(f"📊 {symbol}: Volume spike detected! ({current_volume:,.0f} vs avg {avg_volume:,.0f}, {volume_ratio:.2f}x)")
        elif is_breakout:
            logger.info(
                f"⏭️ {symbol}: Price breakout without volume "
                f"(vol={current_volume:,.0f}, avg={avg_volume:,.0f}, ratio={volume_ratio:.2f}x)"
            )
        
        # Calculate RSI for momentum confirmation
        rsi = compute_rsi(recent_df['CLOSE'])
        current_rsi = rsi.iloc[-1] if len(rsi) > 0 else 50
        
        if current_rsi > 60:  # Strong momentum
            breakout_strength += 1
        
        # Price broke but volume missing/insufficient — do not alert (AZAD-class false signals)
        if is_breakout and not volume_ok:
            return {
                'rejected': True,
                'reason': 'no_volume_confirmation',
                'failing_metric': 'volume_ratio',
                'failing_value': round(volume_ratio, 2),
                'threshold': f'>={MIN_VOLUME_MULTIPLIER}x on breakout bar (no prior-bar volume borrow)',
                'metrics': {
                    'current_volume': round(current_volume, 0),
                    'avg_volume': round(avg_volume, 0),
                    'volume_ratio': round(volume_ratio, 2),
                    'breakout_strength': breakout_strength,
                    'rsi': round(float(current_rsi), 2) if current_rsi is not None else None,
                },
                'volume_ratio': round(volume_ratio, 2),
            }

        # Require price breakout + volume spike; RSI may add a 3rd strength point
        if is_breakout and volume_ok and breakout_strength >= 2:
            # Guard: Reject flat / circuit-locked consolidation bases.
            # Placed INSIDE the breakout block so that symbols with no breakout
            # signal return None (not a rejected dict) and do not generate
            # spurious REJECTED_BREAKOUT log entries.
            consolidation_range = consolidation_high - consolidation_low
            min_consolidation_pct = 0.0025  # 0.25% floor
            if consolidation_range <= 0 or (consolidation_range / consolidation_low) < min_consolidation_pct:
                logger.info(f"⏭️ Rejecting {symbol} - Flat consolidation base detected (range: ₹{consolidation_range:.2f}, Low: ₹{consolidation_low:.2f})")
                return {
                    'rejected': True,
                    'reason': 'flat_consolidation',
                    'failing_metric': 'consolidation_range_pct',
                    'failing_value': round((consolidation_range / consolidation_low * 100) if consolidation_low > 0 else 0, 4),
                    'threshold': f'>={min_consolidation_pct * 100}%',
                    'metrics': {
                        'consolidation_low': round(consolidation_low, 2),
                        'consolidation_high': round(consolidation_high, 2),
                        'consolidation_range': round(consolidation_range, 2),
                    },
                    'volume_ratio': round(volume_ratio, 2)
                }

            # Entry price: Use LIVE price if available, otherwise current price
            entry_price = current_price
            
            # Stop loss (below consolidation low)
            stop_loss_1 = consolidation_low * 0.98   # 2% below consolidation low
            stop_loss_2 = consolidation_low - (consolidation_range * 0.05)  # 5% of range below
            # P1-6 Fix: Apply hard 10% risk cap to prevent excessive stop distances
            # on wide consolidation ranges. Consistent with consolidation scanner cap.
            max_risk_stop = entry_price * 0.90
            stop_loss = max(min(stop_loss_1, stop_loss_2), max_risk_stop)
            
            # Target (based on consolidation range) - add 50% of range above consolidation high
            target_price = consolidation_high + (consolidation_range * 0.5)
            
            # P0-1 Safety Guard: target must be at least entry_price * 1.05 (minimum 5% profit objective for intraday)
            min_target_price = entry_price * 1.05
            if target_price < min_target_price:
                logger.info(f"🛡️ Adjusting target for {symbol} to minimum 5% profit objective: ₹{min_target_price:.2f} (was ₹{target_price:.2f})")
                target_price = min_target_price
            
            # Risk/Reward
            risk = entry_price - stop_loss
            reward = target_price - entry_price
            
            if risk <= 0:
                logger.info(f"⏭️ Rejecting {symbol} - Invalid risk <= 0 (entry: ₹{entry_price:.2f}, stop: ₹{stop_loss:.2f})")
                return {
                    'rejected': True,
                    'reason': 'invalid_risk',
                    'failing_metric': 'risk',
                    'failing_value': round(risk, 2),
                    'threshold': '>0',
                    'metrics': {
                        'entry_price': round(entry_price, 2),
                        'stop_loss': round(stop_loss, 2),
                    },
                    'volume_ratio': round(volume_ratio, 2)
                }
                
            risk_reward = reward / risk
            MIN_RISK_REWARD = getattr(scanner_module, 'MIN_RISK_REWARD', 1.3)
            
            if risk_reward < MIN_RISK_REWARD:
                logger.info(f"⏭️ Rejecting {symbol} - poor risk/reward 1:{risk_reward:.2f} (< {MIN_RISK_REWARD:.2f})")
                return {
                    'rejected': True,
                    'reason': 'poor_risk_reward',
                    'failing_metric': 'risk_reward_ratio',
                    'failing_value': round(risk_reward, 2),
                    'threshold': f'>={MIN_RISK_REWARD:.2f}',
                    'metrics': {
                        'entry_price': round(entry_price, 2),
                        'stop_loss': round(stop_loss, 2),
                        'target_price': round(target_price, 2),
                        'risk': round(risk, 2),
                        'reward': round(reward, 2),
                        'risk_reward': round(risk_reward, 2),
                    },
                    'volume_ratio': round(volume_ratio, 2)
                }
            
            logger.info(f"📊 {symbol} Breakout Levels:")
            logger.info(f"   Consolidation: ₹{consolidation_low:.2f} - ₹{consolidation_high:.2f}")
            logger.info(f"   Recent High: ₹{recent_high:.2f}")
            logger.info(f"   Entry: ₹{entry_price:.2f} ({live_source})")
            logger.info(f"   Stop Loss: ₹{stop_loss:.2f}")
            logger.info(f"   Target: ₹{target_price:.2f}")
            logger.info(f"   Risk:Reward: 1:{risk_reward:.2f}")
            
            return {
                'symbol': symbol,
                'entry_price': round(entry_price, 2),
                'stop_loss': round(stop_loss, 2),
                'target_price': round(target_price, 2),
                'current_price': round(current_price, 2),
                'recent_high': round(recent_high, 2),
                'consolidation_low': round(consolidation_low, 2),
                'consolidation_high': round(consolidation_high, 2),
                'volume_spike': round(volume_ratio, 2),
                'rsi': round(current_rsi, 2),
                'risk_reward': round(risk_reward, 2),
                'breakout_strength': breakout_strength,
                'price_source': live_source,
                'entry_vs_breakout_pct': round(((entry_price / recent_high) - 1.0) * 100.0, 2) if recent_high > 0 else None,
                'post_confirm_move_pct': round(((current_price / recent_high) - 1.0) * 100.0, 2) if recent_high > 0 else None,
                'held_above_breakout_after_confirm': bool(current_price >= recent_high),
                'signal_strength_score': round(float(breakout_strength) * 3.33, 2),
                'tier_weight': None,
                'volume_score': round(min(2.0, volume_ratio / 2.0), 2) if avg_volume > 0 else None,
                'base_score': 1.0,
                'momentum_score': round(min(2.0, max(0.0, (current_rsi - 50.0) / 10.0)), 2) if current_rsi is not None else None,
                'pattern_type': classify_pattern_type("INTRADAY", 30, breakout_strength/2.0, 10),
                'market_regime': get_market_regime(),
                'timestamp': datetime.now()
            }
        
        return None
    
    except Exception as e:
        logger.error(f"Error detecting breakout for {symbol}: {e}")
        return None

def format_intraday_alert(breakout_data):
    """Format intraday breakout alert"""
    symbol = breakout_data['symbol']
    entry = breakout_data['entry_price']
    stop = breakout_data['stop_loss']
    target = breakout_data['target_price']
    current = breakout_data['current_price']
    rsi = breakout_data['rsi']
    vol_spike = breakout_data['volume_spike']
    rr = breakout_data['risk_reward']
    strength = breakout_data['breakout_strength']
    price_source = breakout_data.get('price_source', 'Historical')
    
    # Add emoji for price source
    source_emojis = {
        'upstox': '🚀',
        'yfinance': '📈',
        'jugaad': '📊',
        'Historical': '📊'
    }
    emoji = source_emojis.get(price_source.lower(), '💰')
    
    msg = f"""⚡ <b>INTRADAY BREAKOUT DETECTED</b>

📊 Symbol: <b>{symbol}</b>
💰 Current Price: ₹{current:,.2f} ({emoji} {price_source})
🎯 Entry: ₹{entry:,.2f}
🛑 Stop Loss: ₹{stop:,.2f}
📈 Target: ₹{target:,.2f}
📊 Risk:Reward: 1:{rr:.1f}

📊 <b>Breakout Metrics:</b>
• RSI: {rsi:.1f}
• Volume Spike: {vol_spike:.1f}x
• Pattern: <b>{breakout_data.get('pattern_type', 'N/A')}</b>
• Regime: <b>{breakout_data.get('market_regime', 'N/A')}</b>
• Breakout Strength: {strength}/3

⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

⚠️ <b>Action Required:</b> Review immediately for entry opportunity"""
    
    return msg

def save_breakout_signal(breakout_data):
    """Save breakout signal to MongoDB. Never clobber an existing open IPO book row."""
    try:
        # Get actual candle timestamp for determinism — use market event time, not system time
        candle_ts = breakout_data.get('timestamp', datetime.now())
        if not hasattr(candle_ts, 'strftime'):
            candle_ts = datetime.now()
        candle_time = candle_ts.strftime('%H%M')
        
        # signal_date tied to market candle date, not execution date
        signal_date = candle_ts.date() if hasattr(candle_ts, 'date') else datetime.now().date()
        symbol = breakout_data['symbol']
        signal_id = f"INTRADAY_{symbol}_{signal_date.strftime('%Y%m%d')}_{candle_time}"
        
        # P1-5 Fix: Use signal_exists(signal_id) for deduplication — consistent with other
        # scanners and avoids cross-type collisions (e.g. WATCHLIST blocking INTRADAY).
        try:
            from db import signal_exists
            if signal_exists(signal_id):
                logger.info(f"Signal already exists for {symbol} today ({signal_id})")
                return False
        except Exception as e:
            logger.warning(f"Error checking MongoDB for existing signal: {e}")

        # Book isolation: upsert_position keys on symbol only — never overwrite
        # ACTIVE/PAPER_ONLY listing or consolidation rows with INTRADAY.
        try:
            from db import has_open_position
            if has_open_position(symbol):
                logger.warning(
                    f"⏭️ Skipping INTRADAY position for {symbol} — open position already exists "
                    f"(signal saved as ALERT_ONLY; position write blocked)"
                )
                new_signal = {
                    "signal_id": signal_id,
                    "symbol": symbol,
                    "signal_date": signal_date.strftime('%Y-%m-%d') if hasattr(signal_date, 'strftime') else str(signal_date),
                    "entry_price": breakout_data['entry_price'],
                    "grade": "INTRADAY",
                    "score": breakout_data['breakout_strength'] * 10,
                    "stop_loss": breakout_data['stop_loss'],
                    "target_price": breakout_data['target_price'],
                    "status": "ALERT_ONLY",
                    "exit_date": "",
                    "exit_price": 0,
                    "pnl_pct": 0,
                    "days_held": 0,
                    "signal_type": "INTRADAY",
                    "scanner": "hourly_breakout_scanner",
                    "skip_reason": "open_position_exists",
                }
                from db import upsert_signal
                upsert_signal(new_signal.copy())
                return True
        except Exception as e:
            logger.warning(f"Open-position guard failed for {symbol}: {e}")

        # Portfolio caps (same soft/hard as IPO scanners) — hourly must not ignore the book.
        soft_cap = getattr(scanner_module, 'MAX_ACTIVE_POSITIONS', 5)
        hard_cap = getattr(scanner_module, 'HARD_ACTIVE_POSITIONS', soft_cap + 2)
        active_count = 0
        portfolio_full = False
        try:
            from db import positions_col
            if positions_col is not None:
                active_count = positions_col.count_documents({"status": "ACTIVE"})
            if active_count >= soft_cap:
                portfolio_full = True
                logger.info(
                    f"🔒 Hourly cap reached ({active_count} active; soft {soft_cap}/hard {hard_cap}) — "
                    f"{symbol} saved as PAPER_ONLY"
                )
        except Exception as cap_e:
            logger.warning(f"Hourly portfolio cap check failed for {symbol}: {cap_e}")
            # Fail closed for capital: do not open uncapped ACTIVE on cap-check errors
            portfolio_full = True

        position_status = "PAPER_ONLY" if portfolio_full else "ACTIVE"
        
        new_signal = {
            "signal_id": signal_id,
            "symbol": symbol,
            "signal_date": signal_date.strftime('%Y-%m-%d') if hasattr(signal_date, 'strftime') else str(signal_date),
            "entry_price": breakout_data['entry_price'],
            "grade": "INTRADAY",
            "score": breakout_data['breakout_strength'] * 10,
            "stop_loss": breakout_data['stop_loss'],
            "target_price": breakout_data['target_price'],
            "status": position_status,
            "exit_date": "",
            "exit_price": 0,
            "pnl_pct": 0,
            "days_held": 0,
            "signal_type": "INTRADAY",
            "scanner": "hourly_breakout_scanner"
        }
        
        new_position = {
            "signal_id": signal_id,
            "symbol": symbol,
            "entry_date": signal_date.strftime('%Y-%m-%d') if hasattr(signal_date, 'strftime') else str(signal_date),
            "entry_price": breakout_data['entry_price'],
            "grade": "INTRADAY",
            "current_price": breakout_data['entry_price'],
            "stop_loss": breakout_data['stop_loss'],
            "trailing_stop": breakout_data['stop_loss'],
            "pnl_pct": 0,
            "days_held": 0,
            "status": position_status,
            "next_day_open": None,
            "version": SCANNER_VERSION,
            "strategy_version": SCANNER_VERSION,
            "exit_version": SCANNER_VERSION,
            "execution_version": "3.3.0-single-writer",
            "risk_model_version": "3.3.0-archetype-velocity",
        }
        
        try:
            from db import upsert_signal, upsert_position
            upsert_signal(new_signal.copy())
            upsert_position(new_position.copy())
        except Exception as db_e:
            logger.error(f"[MongoDB] DB write FAILED for {signal_id}: {db_e}")
            try:
                from db import db_metrics
                db_metrics["failures"] = db_metrics.get("failures", 0) + 1
            except Exception:
                pass
            return False

        logger.info(
            f"✅ Saved INTRADAY {position_status} for {symbol} "
            f"(active book {active_count}/soft {soft_cap}/hard {hard_cap})"
        )
        return True
    
    except Exception as e:
        logger.error(f"Error saving signal: {e}")
        return False

def scan_watchlist():
    """Scan all symbols in watchlist for breakouts"""
    logger.info("🚀 Starting hourly intraday breakout scan...")
    logger.info("=" * 60)
    
    # Load watchlist
    symbols = load_watchlist()
    
    if not symbols:
        logger.warning("No active symbols in watchlist")
        return
    
    logger.info(f"📋 Scanning {len(symbols)} symbols...")
    
    # Pre-fetch live prices in bulk from Upstox to minimize sequential API calls
    bulk_prices = {}
    try:
        bulk_prices = getattr(scanner_module, 'get_bulk_live_prices_upstox', lambda x: {})(symbols)
        if bulk_prices:
            logger.info(f"⚡ Pre-fetched live prices in bulk for {len(bulk_prices)}/{len(symbols)} symbols")
    except Exception as bulk_err:
        logger.warning(f"Failed to pre-fetch bulk live prices: {bulk_err}")
        
    breakouts_found = 0
    
    for i, symbol in enumerate(symbols, 1):
        logger.info(f"\n[{i}/{len(symbols)}] Scanning {symbol}...")
        
        # Reset network call flag for this symbol iteration
        if hasattr(scanner_module, 'network_call_made'):
            scanner_module.network_call_made = False
            
        try:
            # Fetch intraday data (tries Upstox first, then yfinance)
            df = fetch_intraday_data(symbol)
            
            if df is None or df.empty:
                logger.warning(f"⚠️ No data for {symbol}")
                continue
            
            # Detect breakout
            breakout = detect_intraday_breakout(df, symbol, bulk_prices)
            
            if breakout and not breakout.get('rejected'):
                logger.info(f"🎯 BREAKOUT DETECTED for {symbol}!")
                write_daily_log("watchlist", symbol, "SIGNAL_GENERATED", {
                    "entry": breakout.get("entry_price"),
                    "stop_loss": breakout.get("stop_loss"),
                    "target": breakout.get("target_price"),
                    "breakout_level": breakout.get("recent_high"),
                    "entry_vs_breakout_pct": breakout.get("entry_vs_breakout_pct"),
                    "post_confirm_move_pct": breakout.get("post_confirm_move_pct"),
                    "held_above_breakout_after_confirm": breakout.get("held_above_breakout_after_confirm"),
                    "signal_strength_score": breakout.get("signal_strength_score"),
                    "tier_weight": breakout.get("tier_weight"),
                    "volume_score": breakout.get("volume_score"),
                    "base_score": breakout.get("base_score"),
                    "momentum_score": breakout.get("momentum_score"),
                    "volume_ratio": breakout.get("volume_spike"),
                    "risk_reward_ratio": breakout.get("risk_reward"),
                    "price_source": breakout.get("price_source"),
                })
                
                # Save signal
                if save_breakout_signal(breakout):
                    # Send alert
                    alert_msg = format_intraday_alert(breakout)
                    send_telegram(alert_msg)
                    breakouts_found += 1
                
                # Small delay to avoid rate limiting
                time.sleep(0.5)
            elif breakout and breakout.get('rejected'):
                logger.info(f"⏭️ {symbol}: Breakout rejected - {breakout.get('reason')}")
                write_daily_log("watchlist", symbol, "REJECTED_BREAKOUT", {
                    "rejection_reason": breakout.get("reason"),
                    "failing_metric": breakout.get("failing_metric"),
                    "failing_value": breakout.get("failing_value"),
                    "threshold": breakout.get("threshold"),
                    "metrics": breakout.get("metrics"),
                    "volume_ratio": breakout.get("volume_ratio"),
                }, log_type="REJECTED")
            else:
                logger.info(f"✅ {symbol}: No breakout detected")
                write_daily_log("watchlist", symbol, "REJECTED_BREAKOUT", {
                    "rejection_reason": "no_intraday_breakout",
                    "failing_metric": "breakout",
                    "failing_value": 0,
                    "threshold": "breakout_strength>=2 and price>recent_high",
                    "metrics": {"breakout": None},
                    "volume_ratio": None,
                }, log_type="REJECTED")
            
            # Rate limiting between symbols - only sleep if network request made
            if getattr(scanner_module, 'network_call_made', False):
                time.sleep(0.3)
        
        except Exception as e:
            logger.error(f"Error scanning {symbol}: {e}")
            continue
    
    logger.info(f"\n{'='*60}")
    logger.info(f"✅ Scan complete: {breakouts_found} breakouts found")
    try:
        from db import db_metrics
        db_stats = {
            "symbols_scanned": len(symbols),
            "signals_found": breakouts_found,
            "db_signals": db_metrics.get("signals_generated", 0),
            "db_logs": db_metrics.get("logs_written", 0),
            "db_failures": db_metrics.get("failures", 0)
        }
    except Exception:
        db_stats = {"symbols_scanned": len(symbols), "signals_found": breakouts_found}

    write_daily_log("watchlist", "SYSTEM", "SCAN_COMPLETED", db_stats)
    
    # Send summary
    if breakouts_found > 0:
        summary = f"""📊 <b>Hourly Breakout Scan Summary</b>

🔍 Symbols Scanned: {len(symbols)}
🎯 Breakouts Found: {breakouts_found}
⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🧯 DB Status: {'✅ OK' if db_stats.get('db_failures', 0) == 0 else f"❌ {db_stats.get('db_failures')} FAILURES"}

{'🎉 New breakouts detected! Check alerts above.' if breakouts_found > 0 else '✅ No new breakouts at this time.'}"""
        send_telegram(summary)

def main():
    """Main function"""
    print("⚡ Hourly Intraday Breakout Scanner")
    print("=" * 60)
    print(f"📅 Date: {datetime.now().strftime('%Y-%m-%d')}")
    print(f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 60)

    # P1-4 Fix: Skip on NSE holidays/weekends — avoids wasted API calls.
    # Consistent with consolidation and listing scanner guards.
    if not is_market_day():
        from datetime import timezone, timedelta as td
        ist = timezone(td(hours=5, minutes=30))
        today_ist = datetime.now(ist).strftime("%Y-%m-%d")
        logger.info(f"📅 Market is closed today ({today_ist}). Skipping hourly scan.")
        send_holiday_notification_once("hourly_breakout_scanner", today_ist, send_telegram)
        return

    scan_watchlist()

if __name__ == "__main__":
    main()

