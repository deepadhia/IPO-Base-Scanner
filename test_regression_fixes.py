import unittest
from unittest.mock import patch, MagicMock, mock_open
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

    @patch('streamlined_ipo_scanner.get_last_trading_day')
    @patch('streamlined_ipo_scanner.fetch_data')
    @patch('streamlined_ipo_scanner.get_live_price')
    @patch('db.get_all_positions_df')
    @patch('db.upsert_position')
    @patch('db.signals_col')
    def test_next_day_open_resolution(self, mock_signals_col, mock_upsert, mock_get_pos, mock_live_price, mock_fetch, mock_last_trading_day):
        # Mock database calls
        mock_last_trading_day.return_value = date(2026, 6, 2)
        mock_signals_col.update_one = MagicMock()
        mock_upsert.return_value = None
        mock_live_price.return_value = (None, None, None, 0.0) # Force fallback to fetch_data

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

    @patch('streamlined_ipo_scanner.get_last_trading_day')
    @patch('streamlined_ipo_scanner.fetch_data')
    @patch('streamlined_ipo_scanner.get_live_price')
    @patch('db.get_all_positions_df')
    @patch('db.upsert_position')
    @patch('db.signals_col')
    def test_next_day_open_holiday_gap(self, mock_signals_col, mock_upsert, mock_get_pos, mock_live_price, mock_fetch, mock_last_trading_day):
        # Mock database calls
        mock_last_trading_day.return_value = date(2026, 6, 2)
        mock_signals_col.update_one = MagicMock()
        mock_upsert.return_value = None
        mock_live_price.return_value = (None, None, None, 0.0)

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

    @patch('streamlined_ipo_scanner.requests.get')
    @patch('streamlined_ipo_scanner.os.getenv')
    @patch('db.get_instrument_key_mapping')
    def test_upstox_quote_instrument_token_matching(self, mock_mapping, mock_getenv, mock_get):
        from streamlined_ipo_scanner import get_live_price_upstox
        
        # Setup mocks
        mock_mapping.return_value = {"TEST": "NSE_EQ|INE123"}
        mock_getenv.return_value = "fake_token"
        
        # Mock API response with key mismatch (key is NSE_EQ:TEST but instrument_token inside is NSE_EQ|INE123)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "success",
            "data": {
                "NSE_EQ:TEST": {
                    "instrument_token": "NSE_EQ|INE123",
                    "last_price": 125.5,
                    "ohlc": {
                        "high": 128.0
                    }
                }
            }
        }
        mock_get.return_value = mock_resp
        
        # Test fetching
        result = get_live_price_upstox("TEST")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 125.5)
        self.assertEqual(result[1], 128.0)

    @patch('streamlined_ipo_scanner.get_last_expected_data_date')
    @patch('streamlined_ipo_scanner.get_live_price')
    @patch('db.get_last_signal_date')
    @patch('streamlined_ipo_scanner.RESEARCH_COHORTS')
    @patch('streamlined_ipo_scanner.reject_quick_losers')
    @patch('streamlined_ipo_scanner.compute_grade_hybrid')
    @patch('streamlined_ipo_scanner.assign_grade')
    @patch('streamlined_ipo_scanner.get_liquidity_metrics')
    @patch('streamlined_ipo_scanner.classify_pattern_type')
    def test_stale_fallback_price_gate(self, mock_pattern, mock_liq, mock_assign, mock_grade, mock_reject, mock_cohorts, mock_last_sig, mock_live_price, mock_expected):
        # Imports
        from streamlined_ipo_scanner import detect_live_patterns
        
        from datetime import timedelta
        today = date.today()
        # Setup mocks
        mock_expected.return_value = today
        mock_last_sig.return_value = None
        mock_reject.return_value = False
        mock_grade.return_value = (85.0, {})
        mock_assign.return_value = "A"
        mock_liq.return_value = (50.0, 0, 5000.0)
        mock_pattern.return_value = "CONSOLIDATION"
        
        # Set cohort so that it passes
        mock_cohorts.items.return_value = [
            ("TEST_COHORT", {"min_window": 3, "max_prng": 15.0, "min_grade": "C", "vol_follow": 0.5})
        ]
        
        # df with breakout candle at the end (index 9)
        dates = pd.date_range(end=today, periods=10)
        df = pd.DataFrame({
            'DATE': dates,
            'OPEN': [100.0]*10,
            'HIGH': [110.0] + [108.0]*8 + [120.0], # Breakout today!
            'LOW': [95.0]*10,
            'CLOSE': [100.0]*9 + [115.0],
            'VOLUME': [200000]*9 + [500000]
        })
        
        # Case A: Live price fails (None, None, None, 0.0) -> price_is_live is False
        mock_live_price.return_value = (None, None, None, 0.0)
        
        listing_map = {"TESTSTOCK": today - timedelta(days=70)}
        
        # Run detect_live_patterns - since price_is_live is False, it should block and return 0 signals
        with patch('streamlined_ipo_scanner.fetch_data') as mock_fetch:
            mock_fetch.return_value = df
            signals_found = detect_live_patterns(["TESTSTOCK"], listing_map)
            self.assertEqual(signals_found, 0)
        
        # Case B: Live price succeeds -> price_is_live is True
        mock_live_price.return_value = (115.0, 'upstox', 120.0, 1000.0)
        
        # We need to mock Mongo calls inside detect_live_patterns when saving signals
        with patch('db.upsert_signal') as mock_sig, patch('db.upsert_position') as mock_pos, patch('streamlined_ipo_scanner.MongoRepository') as mock_repo, patch('streamlined_ipo_scanner.LifecycleTracker') as mock_tracker, patch('streamlined_ipo_scanner.fetch_data') as mock_fetch:
            mock_repo_inst = MagicMock()
            mock_repo.return_value = mock_repo_inst
            mock_repo_inst.save_signal.return_value = True
            mock_fetch.return_value = df
            
            signals_found = detect_live_patterns(["TESTSTOCK"], listing_map)
            # Should proceed because price is live
            self.assertEqual(signals_found, 1)
            self.assertTrue(mock_sig.called)
            self.assertTrue(mock_pos.called)

    def test_audit_logic_trailing_shadow_sl(self):
        import weekly_audit_report
        # Reset findings
        weekly_audit_report.findings = []
        weekly_audit_report.errors = []
        weekly_audit_report.warnings = []
        
        mock_positions_col = MagicMock()
        # Mock positions database call with:
        # 1. Valid trailing stop (TIMEX - entry 337.95, current 455.95, max_runup_pct 36.36, shadow_sl_8pct 423.98)
        # 2. Invalid stop (TESTFAIL - entry 100, current 100, max_runup_pct 0, shadow_sl_8pct 98 but status is ACTIVE and expected is 92)
        mock_positions_col.find.return_value = [
            {
                "symbol": "TIMEX",
                "status": "ACTIVE",
                "entry_price": 337.95,
                "current_price": 455.95,
                "max_runup_pct": 36.36,
                "stop_loss": 310.91,
                "trailing_stop": 310.91,
                "shadow_sl_8pct": 423.98,
                "shadow_status_8pct": "ACTIVE"
            },
            {
                "symbol": "TESTFAIL",
                "status": "ACTIVE",
                "entry_price": 100.0,
                "current_price": 100.0,
                "max_runup_pct": 0.0,
                "stop_loss": 92.0,
                "trailing_stop": 92.0,
                "shadow_sl_8pct": 98.0, # Expected is 92.0, so this exceeds expected
                "shadow_status_8pct": "ACTIVE"
            }
        ]
        
        # Run audit_logic_integrity
        weekly_audit_report.audit_logic_integrity(mock_positions_col)
        
        # We should find that TIMEX is valid, but TESTFAIL is flagged with error/warning
        issues_found = [f for f in weekly_audit_report.findings if f["level"] == "ERROR" and "TESTFAIL" in str(f.get("detail"))]
        timex_issues = [f for f in weekly_audit_report.findings if f["level"] == "ERROR" and "TIMEX" in str(f.get("detail"))]
        
        self.assertEqual(len(timex_issues), 0)
        self.assertEqual(len(issues_found), 1)

    @patch('streamlined_ipo_scanner.get_last_expected_data_date')
    @patch('streamlined_ipo_scanner.get_live_price')
    @patch('db.get_last_signal_date')
    @patch('streamlined_ipo_scanner.RESEARCH_COHORTS')
    @patch('streamlined_ipo_scanner.reject_quick_losers')
    @patch('streamlined_ipo_scanner.compute_grade_hybrid')
    @patch('streamlined_ipo_scanner.assign_grade')
    @patch('streamlined_ipo_scanner.get_liquidity_metrics')
    @patch('streamlined_ipo_scanner.classify_pattern_type')
    @patch('db.positions_col')
    def test_paper_only_position_persistence(self, mock_positions_col, mock_pattern, mock_liq, mock_assign, mock_grade, mock_reject, mock_cohorts, mock_last_sig, mock_live_price, mock_expected):
        from streamlined_ipo_scanner import detect_live_patterns
        import streamlined_ipo_scanner
        
        from datetime import timedelta
        today = date.today()
        # Setup mocks
        mock_expected.return_value = today
        mock_last_sig.return_value = None
        mock_reject.return_value = False
        mock_grade.return_value = (85.0, {})
        mock_assign.return_value = "A"
        mock_liq.return_value = (50.0, 0, 5000.0)
        mock_pattern.return_value = "CONSOLIDATION"
        
        # Force active count above hard cap (portfolio_full is True) but has_active_position(TESTSTOCK) is False
        def mock_count_docs(query, **kwargs):
            if query.get("symbol") == "TESTSTOCK":
                return 0
            if query.get("status") == "ACTIVE":
                return 10
            return 0
        mock_positions_col.count_documents.side_effect = mock_count_docs
        
        mock_cohorts.items.return_value = [
            ("TEST_COHORT", {"min_window": 3, "max_prng": 15.0, "min_grade": "C", "vol_follow": 0.5})
        ]
        
        # df with breakout candle at the end
        dates = pd.date_range(end=today, periods=10)
        df = pd.DataFrame({
            'DATE': dates,
            'OPEN': [100.0]*10,
            'HIGH': [110.0] + [108.0]*8 + [120.0],
            'LOW': [95.0]*10,
            'CLOSE': [100.0]*9 + [115.0],
            'VOLUME': [200000]*9 + [500000]
        })
        
        # Live price succeeds
        mock_live_price.return_value = (115.0, 'upstox', 120.0, 1000.0)
        from streamlined_ipo_scanner import detect_scan
        listing_map = {"TESTSTOCK": today - timedelta(days=70)}
        
        with patch('db.upsert_signal') as mock_sig, patch('db.upsert_position') as mock_pos, patch('streamlined_ipo_scanner.MongoRepository') as mock_repo, patch('streamlined_ipo_scanner.LifecycleTracker') as mock_tracker, patch('streamlined_ipo_scanner.fetch_data') as mock_fetch:
            mock_repo_inst = MagicMock()
            mock_repo.return_value = mock_repo_inst
            mock_repo_inst.save_signal.return_value = True
            mock_fetch.return_value = df
            
            detect_scan(["TESTSTOCK"], listing_map)
            
            # Position should still be upserted even though portfolio_full was True
            self.assertTrue(mock_pos.called)
            
            # Verify position arguments: status must be PAPER_ONLY and shadow stops ACTIVE
            pos_args = mock_pos.call_args[0][0]
            self.assertEqual(pos_args["status"], "PAPER_ONLY")
            self.assertEqual(pos_args["shadow_status_8pct"], "ACTIVE")

    def test_dynamic_nse_holidays_and_market_day(self):
        """Test dynamic holiday fetching, weekend detection, and market day checking."""
        from utils import get_dynamic_nse_holidays, is_market_day, get_last_trading_day
        
        # 1. Republic day (Jan 26) is a known holiday
        holidays_2026 = get_dynamic_nse_holidays(2026)
        self.assertIn("2026-01-26", holidays_2026)
        self.assertFalse(is_market_day("2026-01-26"))

        # 2. Weekend check (2026-08-29 is Saturday, 2026-08-30 is Sunday)
        self.assertFalse(is_market_day("2026-08-29"))
        self.assertFalse(is_market_day("2026-08-30"))

        # 3. Regular trading day (2026-08-26 Wednesday is open)
        self.assertTrue(is_market_day("2026-08-26"))

        # 4. get_last_trading_day walking back
        last_td = get_last_trading_day("2026-08-31") # Monday
        self.assertEqual(last_td.strftime("%Y-%m-%d"), "2026-08-28") # Should be Friday

    def test_holiday_notification_deduplication(self):
        """Test that send_holiday_notification_once only alerts once per day."""
        from utils import send_holiday_notification_once
        
        mock_telegram = MagicMock()
        test_date = "2026-12-25" # Christmas
        
        with patch('db.system_audits_col') as mock_audits, patch('os.path.exists', return_value=False), patch('builtins.open', mock_open()):
            # First call: no existing record in DB or on disk
            mock_audits.find_one.return_value = None
            
            sent_first = send_holiday_notification_once("scanner_1", today_str=test_date, send_telegram_fn=mock_telegram)
            self.assertTrue(sent_first)
            self.assertEqual(mock_telegram.call_count, 1)
            self.assertTrue(mock_audits.update_one.called)

            # Second call (e.g. hourly scanner next hour): existing record found
            mock_audits.find_one.return_value = {
                "audit_type": "HOLIDAY_NOTIFICATION_SENT",
                "date": test_date,
                "triggered_by": "scanner_1"
            }
            
            mock_telegram.reset_mock()
            sent_second = send_holiday_notification_once("hourly_scanner", today_str=test_date, send_telegram_fn=mock_telegram)
            self.assertFalse(sent_second)
            self.assertEqual(mock_telegram.call_count, 0) # Suppressed!

    def test_position_and_exit_alert_color_formatting(self):
        """Test broker-grade green/red color styling for positive and negative returns."""
        from streamlined_ipo_scanner import format_position_update_alert, format_exit_alert

        # 1. Positive Return Position Update
        pos_msg = format_position_update_alert(
            symbol="TESTPROFIT",
            current_price=120.0,
            entry_price=100.0,
            old_trailing=95.0,
            new_trailing=105.0,
            pnl_pct=20.0,
            days_held=10,
            grade="A"
        )
        self.assertIn("🟢", pos_msg)
        self.assertIn("+20.00%", pos_msg)
        self.assertIn("▲ +₹20.00/sh", pos_msg)
        self.assertIn("🔺 Stop Raised", pos_msg)

        # 2. Negative Return Position Update
        loss_msg = format_position_update_alert(
            symbol="TESTLOSS",
            current_price=90.0,
            entry_price=100.0,
            old_trailing=85.0,
            new_trailing=85.0,
            pnl_pct=-10.0,
            days_held=5,
            grade="B"
        )
        self.assertIn("🔴", loss_msg)
        self.assertIn("-10.00%", loss_msg)
        self.assertIn("▼ ₹-10.00/sh", loss_msg)
        self.assertIn("🔹 Maintained", loss_msg)

        # 3. Exit Alert Positive
        exit_pos_msg = format_exit_alert("TESTPROFIT", "Partial Take", 150.0, 50.0, 20, 100.0)
        self.assertIn("🟢", exit_pos_msg)
        self.assertIn("+50.00%", exit_pos_msg)

        # 4. Exit Alert Negative
        exit_loss_msg = format_exit_alert("TESTLOSS", "Stop Loss", 92.0, -8.0, 12, 100.0)
        self.assertIn("🔴", exit_loss_msg)
        self.assertIn("-8.00%", exit_loss_msg)

if __name__ == '__main__':
    unittest.main()

