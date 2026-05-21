import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

def compute_market_context(market_data: pd.DataFrame = None, end_date: datetime = None) -> dict:
    """
    Fetch and analyze Nifty 50 context.
    If market_data is not provided, it fetches the last 60 days up to end_date.
    """
    try:
        if isinstance(end_date, str):
            end_date = pd.to_datetime(end_date).to_pydatetime()
            
        if market_data is None:
            ref_end_date = end_date if end_date else datetime.now()
            start_date = ref_end_date - timedelta(days=90) # Sufficient buffer for 60d MA
            
            try:
                from utils import fetch_nifty_from_upstox
                market_data = fetch_nifty_from_upstox(start_date, ref_end_date + timedelta(days=1))
            except Exception as ex:
                logger.warning(f"Could not import or use fetch_nifty_from_upstox: {ex}")
                market_data = None
                
            if market_data is None or market_data.empty:
                logger.warning("Upstox fetching for Nifty failed or returned no data, falling back to yfinance...")
                # Fetch Nifty 50 (^NSEI)
                nifty = yf.Ticker("^NSEI")
                if end_date:
                    market_data = nifty.history(start=start_date.strftime('%Y-%m-%d'), 
                                               end=(end_date + timedelta(days=1)).strftime('%Y-%m-%d'))
                else:
                    market_data = nifty.history(period="90d") # Increased period for 20DMA stability
            
        if market_data is None or market_data.empty:
            return {"error": "No market data available"}
            
        # Ensure column names are standard
        market_data.columns = [c.upper() for c in market_data.columns]
        
        # If we have more than 60 rows, slice to the last 60 for consistency
        if len(market_data) > 60:
            market_data = market_data.tail(60)
            
        latest_candle_date = market_data.index[-1].strftime('%Y-%m-%d')
        
        # 1. Nifty Daily Return
        latest_close = market_data['CLOSE'].iloc[-1]
        prev_close = market_data['CLOSE'].iloc[-2]
        nifty_return = (latest_close / prev_close - 1) * 100.0
        
        # 2. Distance from 20DMA
        ma20 = market_data['CLOSE'].rolling(window=20).mean()
        dist_20ma = (latest_close / ma20.iloc[-1] - 1) * 100.0
        
        # 3. Trend Slope (Last 5 days) using simple linear regression
        y = market_data['CLOSE'].tail(5).values
        x = np.arange(len(y))
        
        n = len(x)
        sum_x = np.sum(x)
        sum_y = np.sum(y)
        sum_xx = np.sum(x**2)
        sum_xy = np.sum(x * y)
        
        denominator = (n * sum_xx - sum_x**2)
        if denominator == 0:
            slope = 0
        else:
            slope = (n * sum_xy - sum_x * sum_y) / denominator
            
        trend_slope = (slope / latest_close) * 100.0 # Normalized as %
        
        # 4. Market State Label (Derived)
        if latest_close > ma20.iloc[-1] and trend_slope > 0:
            state = "BULL_CONFIRMED"
        elif latest_close < ma20.iloc[-1] and trend_slope < 0:
            state = "BEAR_CONFIRMED"
        elif latest_close > ma20.iloc[-1] and trend_slope < 0:
            state = "DISTRIBUTION"
        else:
            state = "ACCUMULATION"
            
        return {
            "nifty_return": round(float(nifty_return), 2),
            "nifty_20ma_dist": round(float(dist_20ma), 2),
            "nifty_trend_slope": round(float(trend_slope), 3),
            "market_state": state,
            "nifty_close": round(float(latest_close), 2),
            "market_data_date": latest_candle_date,
            "market_cache_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        logger.error(f"Error computing market context: {e}")
        return {"error": str(e)}
