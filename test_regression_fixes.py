import unittest
from unittest.mock import patch, MagicMock
import pandas as pd
from datetime import datetime, date
import sys
import os

# Add workspace path to system path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from streamlined_ipo_scanner import get_market_regime, update_positions

class TestRegressionFixes(unittest.TestCase):

    def setUp(self):
        # Clear cache before each test
        import streamlined_ipo_scanner
        streamlined_ipo_scanner._nifty_regime_cache = {}

    @patch('streamlined_ipo_scanner.yf.download')
    @patch('utils.fetch_nifty_from_upstox')
    def test_nifty_regime_normalization(self, mock_upstox, mock_yf):
        # Disable Upstox first to test yfinance fallback
        mock_upstox.side_effect = Exception("Upstox failed")

        # 1. Test yfinance-style DatetimeIndex with name 'Date' and MultiIndex columns
        dates = pd.date_range(start='2025-10-01', periods=100, freq='D')
        multi_cols = pd.MultiIndex.from_tuples([
            ('Adj Close', '^NSEI'),
            ('Close', '^NSEI'),
            ('High', '^NSEI'),
            ('Low', '^NSEI'),
            ('Open', '^NSEI'),
            ('Volume', '^NSEI')
        ])
        
        # Create a trending price series to generate BULL/CORRECTION regimes
        # First 50 days trend down, last 50 days trend up
        prices = []
        p = 22000
        for i in range(100):
            if i < 50:
                p -= 50
            else:
                p += 150
            prices.append(p)

        data = []
        for p in prices:
            # Let's populate open, high, low, close as same
            data.append([p, p, p, p, p, 100000])

        df_yf_style = pd.DataFrame(data, index=dates, columns=multi_cols)
        df_yf_style.index.name = 'Date'
        mock_yf.return_value = df_yf_style

        # Run get_market_regime - should run without raising KeyError
        regime = get_market_regime(target_date=dates[-1].date())
        self.assertNotEqual(regime, "UNKNOWN")
        self.assertIn(regime, ["BULL", "WEAK_BULL", "RANGE", "CORRECTION"])

        # Clear cache for next case
        import streamlined_ipo_scanner
        streamlined_ipo_scanner._nifty_regime_cache = {}

        # 2. Test Upstox-style index name 'DATE' with single columns
        mock_yf.side_effect = Exception("yfinance failed")
        mock_upstox.side_effect = None  # Reset side effect!
        
        df_upstox_style = pd.DataFrame({
            'OPEN': prices,
            'HIGH': prices,
            'LOW': prices,
            'CLOSE': prices,
            'VOLUME': [100000]*100
        }, index=dates)
        df_upstox_style.index.name = 'DATE'
        mock_upstox.return_value = df_upstox_style

        # Run get_market_regime - should run without raising KeyError
        regime_upstox = get_market_regime(target_date=dates[-1].date())
        self.assertNotEqual(regime_upstox, "UNKNOWN")
        self.assertIn(regime_upstox, ["BULL", "WEAK_BULL", "RANGE", "CORRECTION"])

    @patch('streamlined_ipo_scanner.fetch_data')
    @patch('streamlined_ipo_scanner.get_live_price')
    @patch('db.get_all_positions_df')
    @patch('db.upsert_position')
    @patch('db.signals_col')
    def test_next_day_open_resolution(self, mock_signals_col, mock_upsert, mock_get_pos, mock_live_price, mock_fetch):
        # Mock database calls
        mock_signals_col.update_one = MagicMock()
        mock_upsert.return_value = None
        mock_live_price.return_value = (None, None, None) # Force fallback to fetch_data

        # Positions:
        # 1. Friday position (2026-05-29)
        # 2. Weekend position (stamped Saturday 2026-05-30)
        df_positions = pd.DataFrame([
            {
                "symbol": "TESTSTOCK",
                "entry_date": "2026-05-29",  # Friday (stored as string in DB)
                "entry_price": 100.0,
                "grade": "B",
                "stop_loss": 90.0,
                "trailing_stop": 90.0,
                "status": "ACTIVE",
                "next_day_open": None,
                "signal_id": "sig1",
                "max_runup_pct": 0.0,
                "max_drawdown_pct": 0.0
            },
            {
                "symbol": "TESTSTOCK",
                "entry_date": "2026-05-30",  # Saturday weekend-stamped
                "entry_price": 100.0,
                "grade": "B",
                "stop_loss": 90.0,
                "trailing_stop": 90.0,
                "status": "ACTIVE",
                "next_day_open": None,
                "signal_id": "sig2",
                "max_runup_pct": 0.0,
                "max_drawdown_pct": 0.0
            }
        ])
        mock_get_pos.return_value = df_positions

        # Historical candle data around 2026-05-29
        # Friday is 2026-05-29. Monday is 2026-06-01. Tuesday is 2026-06-02.
        df_candles = pd.DataFrame({
            'DATE': [
                datetime(2026, 5, 28), # Thursday
                datetime(2026, 5, 29), # Friday
                datetime(2026, 6, 1),  # Monday
                datetime(2026, 6, 2)   # Tuesday
            ],
            'OPEN': [98.0, 99.0, 105.0, 112.0],
            'HIGH': [102.0, 101.0, 111.0, 116.0],
            'LOW': [97.0, 98.0, 104.0, 111.0],
            'CLOSE': [100.0, 100.0, 110.0, 115.0],
            'VOLUME': [1000, 1000, 2000, 1500]
        })
        mock_fetch.return_value = df_candles

        # Call update_positions
        update_positions()

        # Check call arguments for upsert_position to see what was resolved
        # The positions are updated in-place on df_positions, but wait!
        # In update_positions(), the loop iterates over rows in get_all_positions_df()
        # and upserts them. Let's inspect the mock_upsert calls!
        self.assertTrue(mock_upsert.called)
        
        # Extract the resolved next_day_open values passed to upsert_position
        upserted_args = [call.args[0] for call in mock_upsert.call_args_list]
        
        # Both positions should resolve to Monday's open: 105.0
        # Position 0 (Friday entry) -> Next trading day is Monday (105.0)
        # Position 1 (Saturday entry) -> Next trading day is Monday (105.0)
        
        self.assertEqual(len(upserted_args), 2)
        self.assertEqual(upserted_args[0]["symbol"], "TESTSTOCK")
        self.assertEqual(upserted_args[0]["next_day_open"], 105.0) # Monday open
        self.assertEqual(upserted_args[1]["next_day_open"], 105.0) # Monday open (not Tuesday!)

    @patch('streamlined_ipo_scanner.fetch_data')
    @patch('streamlined_ipo_scanner.get_live_price')
    @patch('db.get_all_positions_df')
    @patch('db.upsert_position')
    @patch('db.signals_col')
    def test_next_day_open_holiday_gap(self, mock_signals_col, mock_upsert, mock_get_pos, mock_live_price, mock_fetch):
        # Mock database calls
        mock_signals_col.update_one = MagicMock()
        mock_upsert.return_value = None
        mock_live_price.return_value = (None, None, None)

        # Positions:
        # Thursday entry (before a Friday holiday)
        # Friday entry (on the actual holiday)
        df_positions = pd.DataFrame([
            {
                "symbol": "TESTSTOCK",
                "entry_date": "2026-05-28",  # Thursday
                "entry_price": 100.0,
                "grade": "B",
                "stop_loss": 90.0,
                "trailing_stop": 90.0,
                "status": "ACTIVE",
                "next_day_open": None,
                "signal_id": "sig1",
                "max_runup_pct": 0.0,
                "max_drawdown_pct": 0.0
            },
            {
                "symbol": "TESTSTOCK",
                "entry_date": "2026-05-29",  # Friday holiday
                "entry_price": 100.0,
                "grade": "B",
                "stop_loss": 90.0,
                "trailing_stop": 90.0,
                "status": "ACTIVE",
                "next_day_open": None,
                "signal_id": "sig2",
                "max_runup_pct": 0.0,
                "max_drawdown_pct": 0.0
            }
        ])
        mock_get_pos.return_value = df_positions

        # Candle data: Friday 2026-05-29 is a holiday, so no candle exists.
        df_candles = pd.DataFrame({
            'DATE': [
                datetime(2026, 5, 27), # Wednesday
                datetime(2026, 5, 28), # Thursday
                datetime(2026, 6, 1),  # Monday (Next trading session after Thursday/Friday)
                datetime(2026, 6, 2)   # Tuesday
            ],
            'OPEN': [98.0, 99.0, 108.0, 112.0],
            'HIGH': [102.0, 101.0, 111.0, 116.0],
            'LOW': [97.0, 98.0, 107.0, 111.0],
            'CLOSE': [100.0, 100.0, 110.0, 115.0],
            'VOLUME': [1000, 1000, 2000, 1500]
        })
        mock_fetch.return_value = df_candles

        # Call update_positions
        update_positions()

        # Both should resolve to Monday's open: 108.0
        self.assertTrue(mock_upsert.called)
        upserted_args = [call.args[0] for call in mock_upsert.call_args_list]
        self.assertEqual(len(upserted_args), 2)
        self.assertEqual(upserted_args[0]["next_day_open"], 108.0) # Monday open (post-holiday)
        self.assertEqual(upserted_args[1]["next_day_open"], 108.0) # Monday open (post-holiday)

if __name__ == '__main__':
    unittest.main()
