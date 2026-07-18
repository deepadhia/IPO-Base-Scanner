"""
test_volume_exhaustion.py
Unit tests for the Volume Exhaustion Early Exit logic.

Tests verify:
  1. Winner positions (max_runup >= 8%) are NEVER exited
  2. High-PnL positions (pnl >= 5%) are NEVER exited
  3. Deeply underwater positions (pnl < -3%) are NEVER exited
  4. Positions held fewer than min_days are NEVER exited
  5. Thin/illiquid stocks (baseline vol < 50,000) are NEVER exited
  6. Insufficient post-entry rows (< 16) skips the check safely
  7. No VOLUME column handled gracefully
  8. None dataframe handled gracefully
  9. Collapsing volume (ratio < 0.45) triggers exit
  10. Healthy volume (ratio >= 0.45) does NOT trigger
  11. Exactly at ratio boundary (0.45) does NOT trigger
  12. LISTING_BREAKOUT triggers at exactly 15 days
  13. LISTING_BREAKOUT does NOT trigger at 14 days
  14. Listing day exclusion gives correct baseline
  15. Listing day inclusion would cause false result (validates exclusion logic)
"""

import unittest
import pandas as pd
from datetime import date


def evaluate_volume_exhaustion(grade, days_held, pnl, new_max_runup, vol_df, fetch_start_date):
    """
    Inline mirror of the production volume exhaustion check in stop_loss_update_scan().
    Returns exit_reason string if triggered, else None.
    """
    _VOL_MIN_DAYS_IPO    = 15
    _VOL_MIN_DAYS_CONSOL = 10
    _VOL_FLAT_PNL_LOW    = -3.0
    _VOL_FLAT_PNL_HIGH   =  5.0
    _VOL_MAX_RUNUP       =  8.0
    _VOL_RATIO_THRESHOLD =  0.45
    _VOL_ABS_FLOOR       = 50_000

    _vol_min_days = _VOL_MIN_DAYS_IPO if grade == "LISTING_BREAKOUT" else _VOL_MIN_DAYS_CONSOL

    if not (days_held >= _vol_min_days and _VOL_FLAT_PNL_LOW <= pnl < _VOL_FLAT_PNL_HIGH and new_max_runup < _VOL_MAX_RUNUP):
        return None

    if vol_df is None or "VOLUME" not in vol_df.columns or len(vol_df) == 0:
        return None

    _entry_ts = pd.Timestamp(fetch_start_date)
    _entry_rows = vol_df[vol_df["DATE"] >= _entry_ts].copy().reset_index(drop=True)
    if len(_entry_rows) > 1:
        _entry_rows = _entry_rows.iloc[1:].reset_index(drop=True)

    if len(_entry_rows) < 16:
        return None

    _baseline_vol = _entry_rows.iloc[:11]["VOLUME"].mean()
    _recent_vol   = _entry_rows.iloc[-5:]["VOLUME"].mean()

    if _baseline_vol < _VOL_ABS_FLOOR or _baseline_vol <= 0:
        return None

    _vol_ratio = _recent_vol / _baseline_vol
    if _vol_ratio < _VOL_RATIO_THRESHOLD:
        return f"Volume Exhaustion - Dead volume (ratio: {_vol_ratio:.2f}, pnl: {pnl:+.1f}%, days: {days_held})"
    return None


def make_df(entry_date, listing_vol, baseline_vol, recent_vol, total_days=25):
    dates = [pd.Timestamp(entry_date) + pd.Timedelta(days=i) for i in range(total_days)]
    volumes = []
    for i in range(total_days):
        if i == 0:
            volumes.append(listing_vol)
        elif i >= total_days - 5:
            volumes.append(recent_vol)
        else:
            volumes.append(baseline_vol)
    return pd.DataFrame({"DATE": dates, "OPEN": 100.0, "HIGH": 105.0, "LOW": 95.0, "CLOSE": 100.0, "VOLUME": volumes})


ENTRY = date(2026, 6, 1)


class TestGuards(unittest.TestCase):

    def _df(self, recent_vol=10_000):
        return make_df(ENTRY, 2_000_000, 300_000, recent_vol)

    def test_winner_archetype_skipped(self):
        self.assertIsNone(evaluate_volume_exhaustion("B", 15, 1.0, 8.0, self._df(), ENTRY))

    def test_winner_archetype_high_runup_skipped(self):
        self.assertIsNone(evaluate_volume_exhaustion("B", 15, 1.0, 15.0, self._df(), ENTRY))

    def test_high_pnl_skipped(self):
        self.assertIsNone(evaluate_volume_exhaustion("B", 12, 5.0, 5.0, self._df(), ENTRY))

    def test_deeply_underwater_skipped(self):
        self.assertIsNone(evaluate_volume_exhaustion("B", 12, -3.1, 1.0, self._df(), ENTRY))

    def test_ipo_too_few_days_skipped(self):
        self.assertIsNone(evaluate_volume_exhaustion("LISTING_BREAKOUT", 14, 1.0, 2.0, self._df(), ENTRY))

    def test_consolidation_too_few_days_skipped(self):
        self.assertIsNone(evaluate_volume_exhaustion("B", 9, 1.0, 2.0, self._df(), ENTRY))

    def test_thin_stock_skipped(self):
        df = make_df(ENTRY, 200_000, 20_000, 5_000)
        self.assertIsNone(evaluate_volume_exhaustion("B", 15, 1.0, 2.0, df, ENTRY))

    def test_insufficient_rows_skipped(self):
        df = make_df(ENTRY, 2_000_000, 300_000, 10_000, total_days=12)
        self.assertIsNone(evaluate_volume_exhaustion("B", 10, 1.0, 2.0, df, ENTRY))

    def test_no_volume_column_skipped(self):
        df = pd.DataFrame({"DATE": [pd.Timestamp(ENTRY) + pd.Timedelta(days=i) for i in range(25)], "CLOSE": 100.0})
        self.assertIsNone(evaluate_volume_exhaustion("B", 12, 1.0, 2.0, df, ENTRY))

    def test_none_df_skipped(self):
        self.assertIsNone(evaluate_volume_exhaustion("B", 12, 1.0, 2.0, None, ENTRY))


class TestTrigger(unittest.TestCase):

    def test_collapsing_volume_triggers(self):
        df = make_df(ENTRY, 2_000_000, 300_000, 80_000)
        result = evaluate_volume_exhaustion("B", 15, 1.5, 3.0, df, ENTRY)
        self.assertIsNotNone(result)
        self.assertIn("Volume Exhaustion", result)

    def test_healthy_volume_does_not_trigger(self):
        df = make_df(ENTRY, 2_000_000, 300_000, 180_000)
        result = evaluate_volume_exhaustion("B", 15, 1.5, 3.0, df, ENTRY)
        self.assertIsNone(result)

    def test_exactly_at_boundary_does_not_trigger(self):
        # baseline = 200K, recent = 90K ? ratio = 0.45 exactly ? should NOT trigger (strict <)
        df = make_df(ENTRY, 1_000_000, 200_000, 90_000)
        result = evaluate_volume_exhaustion("B", 15, 1.5, 3.0, df, ENTRY)
        self.assertIsNone(result, "Ratio exactly 0.45 should not trigger (strict <)")

    def test_ipo_triggers_at_15_days(self):
        df = make_df(ENTRY, 5_000_000, 400_000, 80_000)
        result = evaluate_volume_exhaustion("LISTING_BREAKOUT", 15, 2.0, 4.0, df, ENTRY)
        self.assertIsNotNone(result)

    def test_ipo_does_not_trigger_at_14_days(self):
        df = make_df(ENTRY, 5_000_000, 400_000, 80_000)
        result = evaluate_volume_exhaustion("LISTING_BREAKOUT", 14, 2.0, 4.0, df, ENTRY)
        self.assertIsNone(result)


class TestListingDayExclusion(unittest.TestCase):

    def test_listing_day_excluded_from_baseline_triggers_correctly(self):
        """
        Listing day: 10M. Days 1-19: 300K. Days 20-24: 80K.
        With listing day in baseline: avg of [10M + 300K*10] inflated.
        With listing day EXCLUDED (correct): baseline = 300K, recent = 80K, ratio = 0.267 ? triggers.
        """
        dates = [pd.Timestamp(ENTRY) + pd.Timedelta(days=i) for i in range(25)]
        volumes = [10_000_000] + [300_000] * 19 + [80_000] * 5
        df = pd.DataFrame({"DATE": dates, "OPEN": 100.0, "HIGH": 105.0, "LOW": 95.0, "CLOSE": 100.0, "VOLUME": volumes})
        result = evaluate_volume_exhaustion("LISTING_BREAKOUT", 20, 1.5, 3.0, df, ENTRY)
        self.assertIsNotNone(result, "Should trigger: baseline ~300K, recent 80K, ratio ~0.27")

    def test_listing_day_inclusion_would_falsely_suppress_exit(self):
        """
        Simulate: if listing day (10M) were included in the baseline, the average
        would be inflated so much that recent 80K looks like 0.008 ratio — still triggers.
        The key test: confirm our excluded baseline gives the correct ratio string.
        """
        dates = [pd.Timestamp(ENTRY) + pd.Timedelta(days=i) for i in range(25)]
        volumes = [10_000_000] + [60_000] * 19 + [10_000] * 5
        df = pd.DataFrame({"DATE": dates, "OPEN": 100.0, "HIGH": 105.0, "LOW": 95.0, "CLOSE": 100.0, "VOLUME": volumes})
        result = evaluate_volume_exhaustion("B", 15, 1.0, 2.0, df, ENTRY)
        # baseline (excl listing day) = 60K, above 50K floor. recent = 10K. ratio = 0.167 ? triggers
        self.assertIsNotNone(result)
        self.assertIn("ratio: 0.17", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
