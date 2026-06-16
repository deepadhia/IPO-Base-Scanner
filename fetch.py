import pandas as pd
import requests
from datetime import datetime, timedelta
from io import StringIO
import os

def fetch_recent_ipo_symbols(years_back=3):
    """Dynamic IPO symbol fetching with multiple fallback methods"""
    try:
        print(f"[*] Fetching recent IPO symbols for last {years_back} year(s)...")
        
        # Method 1: Try NSE API with retry
        for attempt in range(3):
            try:
                print(f"[Attempt {attempt + 1}/3] Fetching NSE equity list...")
                url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                session = requests.Session()
                session.headers.update(headers)
                
                # Establish session first
                session.get("https://www.nseindia.com", timeout=15)
                resp = session.get(url, timeout=45)
                resp.raise_for_status()
                
                print("[OK] NSE API connection successful")
                
                df = pd.read_csv(StringIO(resp.text))
                print(f"[Info] NSE EQUITY_L returned {len(df)} records")
                
                # Find the right columns
                date_col = None
                symbol_col = None
                name_col = None
                
                for col in df.columns:
                    col_upper = col.upper()
                    if 'DATE' in col_upper and 'LISTING' in col_upper:
                        date_col = col
                    elif 'SYMBOL' in col_upper:
                        symbol_col = col
                    elif 'NAME' in col_upper and 'COMPANY' in col_upper:
                        name_col = col
                
                if date_col and symbol_col:
                    df[date_col] = pd.to_datetime(df[date_col], errors='coerce', format='mixed')
                    cutoff = datetime.now() - timedelta(days=365 * years_back)
                    
                    # Filter for recent IPOs
                    recent_mask = df[date_col] > cutoff
                    recent_ipos = df[recent_mask]
                    
                    # Remove suspicious companies
                    suspicious_patterns = ['RNBDENIMS'] 
                    if name_col:
                        suspicious_mask = recent_ipos[name_col].str.contains('|'.join(suspicious_patterns), case=False, na=False)
                        recent_ipos = recent_ipos[~suspicious_mask]
                        
                    # Remove RE and SME shares
                    if symbol_col:
                        re_sme_mask = recent_ipos[symbol_col].str.contains('-RE|-SM|RE1', case=False, na=False)
                        recent_ipos = recent_ipos[~re_sme_mask]
                    
                    symbols = recent_ipos[symbol_col].tolist()
                    companies = recent_ipos[name_col].tolist() if name_col else symbols
                    dates = recent_ipos[date_col].dt.strftime('%Y-%m-%d').tolist()
                    
                    # --- Penny Stock Filtering (< Rs.25) via Bulk yfinance ---
                    print(f"[*] Bulk checking close prices for {len(symbols)} IPOs to filter out penny stocks (< Rs.25)...")
                    try:
                        import yfinance as yf
                        tickers_ns = [f"{s}.NS" for s in symbols]
                        
                        # Bulk download latest price (Group_by ticker)
                        prices_df = yf.download(tickers_ns, period="1d", progress=False)
                        
                        valid_symbols = []
                        valid_companies = []
                        valid_dates = []
                        
                        for idx_sym, sym in enumerate(symbols):
                            ticker = f"{sym}.NS"
                            close_price = None
                            try:
                                # yfinance MultiIndex check
                                if isinstance(prices_df.columns, pd.MultiIndex):
                                    if 'Close' in prices_df.columns.levels[0] and ticker in prices_df.columns.levels[1]:
                                        close_val = prices_df['Close'][ticker].iloc[-1]
                                        if pd.notna(close_val):
                                            close_price = float(close_val)
                                else:
                                    if 'Close' in prices_df.columns:
                                        close_val = prices_df['Close'][ticker].iloc[-1] if isinstance(prices_df['Close'], pd.DataFrame) else prices_df['Close'].iloc[-1]
                                        if pd.notna(close_val):
                                            close_price = float(close_val)
                            except Exception:
                                pass
                                
                            if close_price is not None and close_price < 25.0:
                                print(f"  [Skip] Filtering out penny stock from fetch: {sym} (Price Rs.{close_price:.2f} < Rs.25.00)")
                                continue
                                
                            valid_symbols.append(sym)
                            if name_col:
                                valid_companies.append(companies[idx_sym])
                            valid_dates.append(dates[idx_sym])
                            
                        symbols = valid_symbols
                        companies = valid_companies if name_col else symbols
                        dates = valid_dates
                        print(f"[OK] Filtered symbol list: {len(symbols)} symbols remain after price filter")
                    except Exception as e:
                        print(f"[Warning] Bulk price filter failed: {e}. Proceeding with all symbols.")
                    
                    print(f"[OK] NSE API: Found {len(symbols)} recent IPOs")
                    
                    df_symbols = pd.DataFrame({
                        'symbol': symbols,
                        'company': companies,
                        'listing_date': dates
                    })
                    
                    # No longer saving to CSV, using MongoDB exclusively

                    # MongoDB dual-write: upsert discovered IPOs
                    try:
                        from db import upsert_ipo, ensure_indexes
                        ensure_indexes()
                        for _, row in df_symbols.iterrows():
                            upsert_ipo(
                                symbol=row['symbol'],
                                listing_date=row['listing_date'],
                                name=row['company']
                            )
                        print(f"[MongoDB] Upserted {len(df_symbols)} IPO records")
                    except Exception as db_e:
                        print(f"[Warning] [MongoDB] IPO write FAILED (CSV write succeeded): {db_e}")
                        try:
                            from db import db_metrics
                            db_metrics["failures"] = db_metrics.get("failures", 0) + 1
                        except Exception:
                            pass

                    return df_symbols
                else:
                    print("[Warning] NSE API: Could not find required columns")
                    raise Exception("Column mapping failed")
                    
            except Exception as e:
                print(f"[Warning] NSE API attempt {attempt + 1} failed: {e}")
                if attempt == 2:  # Last attempt
                    print("[Error] All NSE API attempts failed")
                    break
                else:
                    print("[Info] Retrying in 5 seconds...")
                    import time
                    time.sleep(5)
        
        # Method 2: Fallback to MongoDB list
        print("[Info] Falling back to MongoDB records...")
        try:
            from db import ipos_col
            if ipos_col is not None:
                docs = list(ipos_col.find({}, {"_id": 0, "symbol": 1, "company": 1, "listing_date": 1}))
                if docs:
                    df_symbols = pd.DataFrame(docs)
                    print(f"[Info] MongoDB fallback: {len(df_symbols)} symbols")
                    return df_symbols
            print("[Error] No valid MongoDB records found")
        except Exception as db_fallback_error:
            print(f"[Warning] MongoDB fallback failed: {db_fallback_error}")
            print("[Info] Creating minimal fallback data...")
            
            # Method 3: Create minimal fallback
            fallback_symbols = [
                'SWIGGY', 'BLACKBUCK', 'STALLION', 'BHARATSE', 
                'NATCAPSUQ', 'MOSCHIP', 'TRAVELFOOD', 'OCCLLTD', 'GARUDA',
                'CEWATER', 'RACLGEAR', 'ORCHASP', 'OSWALPUMPS', 'IGIL',
                'VIKRAN', 'AFCONS', 'MOBIKWIK', 'MASTERTR', 'JAINREC',
                'DRAGARWQ', 'KOTIC', 'SCANSTL', 'IWP', 'NOVARTIND'
            ]
            
            df_symbols = pd.DataFrame({
                'symbol': fallback_symbols,
                'company': [f"{sym} Ltd" for sym in fallback_symbols],
                'listing_date': [datetime.now().strftime('%Y-%m-%d')] * len(fallback_symbols)
            })
            
            # Save to MongoDB
            try:
                from db import upsert_ipo
                for _, row in df_symbols.iterrows():
                    upsert_ipo(row['symbol'], row['listing_date'], row['company'])
            except:
                pass
            
            print(f"[Info] Created minimal fallback with {len(fallback_symbols)} symbols")
            return df_symbols
                
    except Exception as e:
        print(f"[Error] fetch_recent_ipo_symbols failed: {e}")
        return None
