#!/usr/bin/env python3
"""
listing_day_breakout_scanner.py

IPO Listing Day Breakout Scanner:
- Tracks listing day high and low for each IPO
- Detects when stock breaks listing day high with volume
- Entry: When breaks listing day high with volume
- Stop Loss: Listing day low
- Target: Based on listing day range
"""

import os
import sys
import json
import pandas as pd
import numpy as np
import requests
import time
import argparse
from datetime import datetime, timedelta
from dotenv import load_dotenv
import logging
from datetime import time as dt_time

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except Exception:
    YFINANCE_AVAILABLE = False

# Add current directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Import from main scanner
import importlib.util
spec = importlib.util.spec_from_file_location("scanner", "streamlined_ipo_scanner.py")
scanner_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner_module)

# Import version and logging utilities from main scanner
SCANNER_VERSION = "2.5.0"
write_daily_log = getattr(scanner_module, 'write_daily_log', lambda *a, **k: None)

fetch_data = scanner_module.fetch_data
send_telegram = scanner_module.send_telegram
logger = scanner_module.logger
get_live_price = scanner_module.get_live_price
get_market_regime = scanner_module.get_market_regime
classify_pattern_type = scanner_module.classify_pattern_type
is_market_day = getattr(scanner_module, 'is_market_day', lambda: True)

# Load environment
load_dotenv()

# Metadata & State (Legacy CSVs removed, using MongoDB)

def log_rejected_signal(symbol, current_price, listing_high, days_since, vol_ratio, reason):
    """Log a simple rejection telemetry event to MongoDB."""
    try:
        write_daily_log("listing_day", symbol, "REJECTED_BREAKOUT", {
            "rejection_reason": reason,
            "failing_metric": "price_vs_listing_high",
            "failing_value": round(current_price, 2),
            "threshold": round(listing_high, 2),
            "metrics": {
                "age_days": days_since,
                "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
                "listing_high": round(listing_high, 2),
            },
        }, log_type="REJECTED")
    except Exception as e:
        logger.error(f"Failed to log rejection for {symbol}: {e}")



def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


# Quality mode: strict = only trades with full volume + freshness (default ON — "good trades only")
LISTING_STRICT_QUALITY = _env_bool("LISTING_STRICT_QUALITY", True)

# Configuration (stricter defaults when LISTING_STRICT_QUALITY is True)
# Legacy CSV paths removed

PENDING_BREAKOUTS_FILE = "listing_pending_breakouts.json"
MIN_VOLUME_MULTIPLIER = _env_float(
    "LISTING_MIN_VOLUME_MULT", 1.8 if LISTING_STRICT_QUALITY else 1.5
)
MAX_ENTRY_ABOVE_HIGH_PCT = _env_float(
    "LISTING_MAX_ENTRY_ABOVE_HIGH_PCT", 3.5 if LISTING_STRICT_QUALITY else 5.0
)
MIN_RISK_REWARD = _env_float(
    "LISTING_MIN_RISK_REWARD", 1.25 if LISTING_STRICT_QUALITY else 1.0
)
STOP_LOSS_PCT = _env_float("LISTING_STOP_LOSS_PCT", 8.0)
MIN_VOLUME_VS_LISTING_DAY = _env_float(
    "LISTING_MIN_VOL_VS_LISTING", 1.0 if LISTING_STRICT_QUALITY else 1.2
)
# Reject listing-high breakouts when IPO is too old (strategy is "listing day" edge)
MAX_DAYS_SINCE_LISTING_FOR_BREAKOUT = _env_int(
    "LISTING_MAX_DAYS_SINCE_LISTING", 500
)
# If listing-day volume in CSV is 0 / missing, require this multiple of recent avg volume
MIN_VOL_MULT_WHEN_NO_LISTING_VOL = _env_float(
    "LISTING_MIN_VOL_MULT_WHEN_NO_LISTING_VOL", 2.0 if LISTING_STRICT_QUALITY else 1.5
)
LISTING_CONFIRMATION_MINUTES = _env_int(
    "LISTING_CONFIRMATION_MINUTES", 60
)
LISTING_MIN_LEADER_SCORE = _env_int(
    "LISTING_MIN_LEADER_SCORE", 5
)
# Watchlist = attention layer only; in strict mode use same strategy lens as breakouts (fresh IPO + real volume).
LISTING_WATCHLIST_MAX_DAYS_SINCE_LISTING = _env_int(
    "LISTING_WATCHLIST_MAX_DAYS_SINCE_LISTING", 90 if LISTING_STRICT_QUALITY else 365
)
# Hard cap: beyond this, not an “IPO base” setup at all (normal stock)
LISTING_WATCHLIST_ABSOLUTE_MAX_AGE_DAYS = _env_int(
    "LISTING_WATCHLIST_ABSOLUTE_MAX_AGE_DAYS", 120 if LISTING_STRICT_QUALITY else 365
)
# 0 = disabled. Use with base-quality filter: dry-up is allowed when structure is tight.
LISTING_WATCHLIST_MIN_VOLUME_MULT = _env_float(
    "LISTING_WATCHLIST_MIN_VOLUME_MULT", 0.0
)
# 0 = disabled. E.g. 1.05 = current vol must be ≤ 1.05× recent avg (dry-up cap).
LISTING_WATCHLIST_MAX_VOL_VS_AVG = _env_float(
    "LISTING_WATCHLIST_MAX_VOL_VS_AVG", 0.0
)
LISTING_WATCHLIST_MIN_LEADER_SCORE = _env_int(
    "LISTING_WATCHLIST_MIN_LEADER_SCORE", LISTING_MIN_LEADER_SCORE
)
# Structured base (strict watchlist): volume dry-up only counts if price is tight, not “dead chop”
LISTING_WATCHLIST_BASE_LOOKBACK = _env_int(
    "LISTING_WATCHLIST_BASE_LOOKBACK", 7 if LISTING_STRICT_QUALITY else 5
)
LISTING_WATCHLIST_MAX_BASE_RANGE_PCT = _env_float(
    "LISTING_WATCHLIST_MAX_BASE_RANGE_PCT", 9.0 if LISTING_STRICT_QUALITY else 20.0
)
LISTING_WATCHLIST_AUTO_REJECT_WIDE_RANGE_PCT = _env_float(
    "LISTING_WATCHLIST_AUTO_REJECT_WIDE_RANGE_PCT", 12.0 if LISTING_STRICT_QUALITY else 100.0
)
LISTING_WATCHLIST_MIN_CLOSE_IN_RANGE_PCT = _env_float(
    "LISTING_WATCHLIST_MIN_CLOSE_IN_RANGE_PCT", 45.0 if LISTING_STRICT_QUALITY else 0.0
)
LISTING_WATCHLIST_REQUIRE_RANGE_CONTRACTION = _env_bool(
    "LISTING_WATCHLIST_REQUIRE_RANGE_CONTRACTION", LISTING_STRICT_QUALITY
)
# Proximity band (listing high): max distance already implied by near-breakout rule (~5%)
LISTING_WATCHLIST_MIN_DISTANCE_FROM_HIGH_PCT = _env_float(
    "LISTING_WATCHLIST_MIN_DISTANCE_FROM_HIGH_PCT", 0.0 if not LISTING_STRICT_QUALITY else 3.0
)
LISTING_WATCHLIST_MAX_DISTANCE_FROM_HIGH_PCT = _env_float(
    "LISTING_WATCHLIST_MAX_DISTANCE_FROM_HIGH_PCT", 5.0
)
# Volume dry-up: recent avg vol vs prior window (only after tight base checks)
LISTING_WATCHLIST_VOL_DRYUP_RECENT_DAYS = _env_int(
    "LISTING_WATCHLIST_VOL_DRYUP_RECENT_DAYS", 5 if LISTING_STRICT_QUALITY else 5
)
LISTING_WATCHLIST_VOL_DRYUP_PRIOR_DAYS = _env_int(
    "LISTING_WATCHLIST_VOL_DRYUP_PRIOR_DAYS", 10 if LISTING_STRICT_QUALITY else 10
)
LISTING_WATCHLIST_VOL_DRYUP_MAX_RATIO = _env_float(
    "LISTING_WATCHLIST_VOL_DRYUP_MAX_RATIO", 0.8 if LISTING_STRICT_QUALITY else 1.0
)
LISTING_WATCHLIST_REQUIRE_VOL_DRYUP = _env_bool(
    "LISTING_WATCHLIST_REQUIRE_VOL_DRYUP", LISTING_STRICT_QUALITY
)
# Support: lows tagging bottom of the lookback range
LISTING_WATCHLIST_MIN_SUPPORT_TOUCHES = _env_int(
    "LISTING_WATCHLIST_MIN_SUPPORT_TOUCHES", 2 if LISTING_STRICT_QUALITY else 0
)
LISTING_WATCHLIST_SUPPORT_TOUCH_TOLERANCE_PCT_OF_RANGE = _env_float(
    "LISTING_WATCHLIST_SUPPORT_TOUCH_TOLERANCE_PCT_OF_RANGE", 1.5 if LISTING_STRICT_QUALITY else 0.0
)
# Upper wicks / supply at highs
LISTING_WATCHLIST_MAX_UPPER_WICK_OF_HIGH_PCT = _env_float(
    "LISTING_WATCHLIST_MAX_UPPER_WICK_OF_HIGH_PCT", 4.0 if LISTING_STRICT_QUALITY else 100.0
)
LISTING_WATCHLIST_MAX_UPPER_WICK_OF_BAR_FRAC = _env_float(
    "LISTING_WATCHLIST_MAX_UPPER_WICK_OF_BAR_FRAC", 0.55 if LISTING_STRICT_QUALITY else 1.0
)
# Repeated intraday rejection when trading near listing high
LISTING_WATCHLIST_NEAR_HIGH_ZONE_PCT = _env_float(
    "LISTING_WATCHLIST_NEAR_HIGH_ZONE_PCT", 1.5 if LISTING_STRICT_QUALITY else 0.0
)
LISTING_WATCHLIST_NEAR_HIGH_DUMP_PCT = _env_float(
    "LISTING_WATCHLIST_NEAR_HIGH_DUMP_PCT", 3.5 if LISTING_STRICT_QUALITY else 100.0
)
LISTING_WATCHLIST_MAX_NEAR_HIGH_DUMP_DAYS = _env_int(
    "LISTING_WATCHLIST_MAX_NEAR_HIGH_DUMP_DAYS", 2 if LISTING_STRICT_QUALITY else 99
)
# Auto-reject: weak price + weak volume together
LISTING_WATCHLIST_REJECT_FALLING_VOL_AND_PRICE = _env_bool(
    "LISTING_WATCHLIST_REJECT_FALLING_VOL_AND_PRICE", LISTING_STRICT_QUALITY
)
# Choppy structure: too many direction changes
LISTING_WATCHLIST_MAX_SIGN_FLIPS = _env_int(
    "LISTING_WATCHLIST_MAX_SIGN_FLIPS", 4 if LISTING_STRICT_QUALITY else 99
)

# ---------------------------------------------------------------------------
# Tiered strategy: A+ / A / B / controlled-fallback A / Reject
# ---------------------------------------------------------------------------
# Tier A+: perfect base + listing high breakout + volume + fresh IPO → 100% size
LISTING_TIER_APLUS_MIN_VOLUME_MULT  = _env_float("LISTING_TIER_APLUS_MIN_VOLUME_MULT", 1.8)
LISTING_TIER_APLUS_MAX_AGE_DAYS     = _env_int("LISTING_TIER_APLUS_MAX_AGE_DAYS", 365)

# Tier A: pure momentum — strong volume, no base required, NOT perfect base → 60% size
LISTING_TIER_A_MIN_VOLUME_MULT = _env_float("LISTING_TIER_A_MIN_VOLUME_MULT", 2.0)
LISTING_TIER_A_MAX_AGE_DAYS    = _env_int("LISTING_TIER_A_MAX_AGE_DAYS", 365)

# Tier A — controlled fallback: outside A windows but still valid → 50% size
LISTING_TIER_A_FALLBACK_MAX_AGE_DAYS       = _env_int("LISTING_TIER_A_FALLBACK_MAX_AGE_DAYS", 365)
LISTING_TIER_A_FALLBACK_POSITION_SIZE_PCT  = _env_int("LISTING_TIER_A_FALLBACK_POSITION_SIZE_PCT", 50)

# Tier B: base breakout below listing high (accumulation-driven) → 40% size
LISTING_TIER_B_ENABLED                    = _env_bool("LISTING_TIER_B_ENABLED", True)
LISTING_TIER_B_MAX_DISTANCE_FROM_HIGH_PCT = _env_float("LISTING_TIER_B_MAX_DISTANCE_FROM_HIGH_PCT", 20.0)
LISTING_TIER_B_MAX_AGE_DAYS               = _env_int("LISTING_TIER_B_MAX_AGE_DAYS", 365)
LISTING_TIER_B_POSITION_SIZE_PCT          = _env_int("LISTING_TIER_B_POSITION_SIZE_PCT", 40)

# Post-confirm move: minimum % above breakout reference after confirmation (kills dead trades)
LISTING_MIN_POST_CONFIRM_MOVE_PCT = _env_float("LISTING_MIN_POST_CONFIRM_MOVE_PCT", 1.5)


def _evaluate_watchlist_perfect_base(
    df: pd.DataFrame,
    lookback: int,
    listing_day_high: float,
    current_high: float,
    proximity_check: bool = True,
) -> tuple[bool, str, dict]:
    """
    IPO “perfect base” checklist for strict watchlist (quiet tight base near listing high).
    Returns (ok, rejection_reason, checklist dict).
    """
    checklist: dict = {}
    if df is None or df.empty or lookback < 3:
        return False, "Strict watchlist: insufficient history for base checklist", checklist

    need = max(
        lookback,
        LISTING_WATCHLIST_VOL_DRYUP_RECENT_DAYS + LISTING_WATCHLIST_VOL_DRYUP_PRIOR_DAYS + 1
        if LISTING_WATCHLIST_REQUIRE_VOL_DRYUP
        else lookback,
    )
    if len(df) < need:
        return False, f"Strict watchlist: need ≥{need} bars for base checklist", checklist

    tail = df.tail(lookback).copy()
    cols = ["HIGH", "LOW", "CLOSE"]
    if "OPEN" not in df.columns:
        tail["OPEN"] = tail["CLOSE"]
    else:
        cols.append("OPEN")
    if "VOLUME" not in df.columns:
        return False, "Strict watchlist: missing VOLUME for base checklist", checklist
    cols.append("VOLUME")
    for col in cols:
        if col in tail.columns:
            tail[col] = pd.to_numeric(tail[col], errors="coerce")
    tail = tail.dropna(subset=["HIGH", "LOW", "CLOSE", "VOLUME"])
    if len(tail) < lookback:
        return False, "Strict watchlist: insufficient clean bars for base checklist", checklist

    rh = float(tail["HIGH"].max())
    rl = float(tail["LOW"].min())
    avg_c = float(tail["CLOSE"].mean())
    if avg_c <= 0 or rh <= 0 or listing_day_high <= 0:
        return False, "Strict watchlist: invalid prices for base checklist", checklist

    base_range_pct = (rh - rl) / avg_c * 100.0
    span = rh - rl if rh > rl else 1e-9
    last_close = float(tail["CLOSE"].iloc[-1])
    close_pct_in_range = (last_close - rl) / span * 100.0

    checklist["tight_range_pct"] = round(base_range_pct, 2)
    checklist["close_pct_up_from_range_low"] = round(close_pct_in_range, 2)

    if base_range_pct >= LISTING_WATCHLIST_AUTO_REJECT_WIDE_RANGE_PCT:
        return (
            False,
            f"Strict watchlist: auto-reject wide chop (range {base_range_pct:.1f}% ≥ {LISTING_WATCHLIST_AUTO_REJECT_WIDE_RANGE_PCT}%)",
            checklist,
        )

    if base_range_pct > LISTING_WATCHLIST_MAX_BASE_RANGE_PCT:
        return (
            False,
            f"Strict watchlist: base too loose ({base_range_pct:.1f}% > max {LISTING_WATCHLIST_MAX_BASE_RANGE_PCT}%)",
            checklist,
        )

    dist_from_high_pct = (listing_day_high - current_high) / listing_day_high * 100.0
    checklist["distance_from_listing_high_pct"] = round(dist_from_high_pct, 2)
    if proximity_check:
        if LISTING_WATCHLIST_MAX_DISTANCE_FROM_HIGH_PCT > 0:
            if dist_from_high_pct > LISTING_WATCHLIST_MAX_DISTANCE_FROM_HIGH_PCT + 1e-6:
                return (
                    False,
                    f"Strict watchlist: too far below listing high ({dist_from_high_pct:.1f}% > {LISTING_WATCHLIST_MAX_DISTANCE_FROM_HIGH_PCT}%)",
                    checklist,
                )
        if LISTING_WATCHLIST_MIN_DISTANCE_FROM_HIGH_PCT > 0:
            if dist_from_high_pct < LISTING_WATCHLIST_MIN_DISTANCE_FROM_HIGH_PCT - 1e-6:
                return (
                    False,
                    f"Strict watchlist: too tight to listing high ({dist_from_high_pct:.1f}% < min {LISTING_WATCHLIST_MIN_DISTANCE_FROM_HIGH_PCT}% band)",
                    checklist,
                )

    if LISTING_WATCHLIST_MIN_CLOSE_IN_RANGE_PCT > 0 and close_pct_in_range < LISTING_WATCHLIST_MIN_CLOSE_IN_RANGE_PCT:
        return (
            False,
            f"Strict watchlist: not upper-base ({close_pct_in_range:.0f}% up from range low, need ≥{LISTING_WATCHLIST_MIN_CLOSE_IN_RANGE_PCT}%)",
            checklist,
        )

    # Support touches: lows near bottom of the same tight range
    tol = span * (LISTING_WATCHLIST_SUPPORT_TOUCH_TOLERANCE_PCT_OF_RANGE / 100.0)
    support_line = rl
    touches = int((tail["LOW"] <= support_line + tol).sum())
    checklist["support_touches"] = touches
    if LISTING_WATCHLIST_MIN_SUPPORT_TOUCHES > 0 and touches < LISTING_WATCHLIST_MIN_SUPPORT_TOUCHES:
        return (
            False,
            f"Strict watchlist: support not held ({touches} touches at range low, need ≥{LISTING_WATCHLIST_MIN_SUPPORT_TOUCHES})",
            checklist,
        )

    # Upper wicks / supply
    bad_wick_bar = False
    max_uw_high = 0.0
    for _, row in tail.iterrows():
        h = float(row["HIGH"])
        l = float(row["LOW"])
        o = float(row["OPEN"])
        c = float(row["CLOSE"])
        body_top = max(o, c)
        upper = max(0.0, h - body_top)
        if h > 0:
            uw_pct = upper / h * 100.0
            max_uw_high = max(max_uw_high, uw_pct)
            if uw_pct > LISTING_WATCHLIST_MAX_UPPER_WICK_OF_HIGH_PCT:
                bad_wick_bar = True
                break
        bar_rng = h - l
        if bar_rng > 1e-9 and upper / bar_rng > LISTING_WATCHLIST_MAX_UPPER_WICK_OF_BAR_FRAC:
            bad_wick_bar = True
            break
    checklist["max_upper_wick_pct_of_high"] = round(max_uw_high, 2)
    checklist["clean_upper_wicks"] = not bad_wick_bar
    if bad_wick_bar:
        return (
            False,
            "Strict watchlist: heavy upper wicks / supply near highs",
            checklist,
        )

    # Near listing high: repeated close well off the high
    if LISTING_WATCHLIST_NEAR_HIGH_ZONE_PCT > 0 and LISTING_WATCHLIST_MAX_NEAR_HIGH_DUMP_DAYS < 90:
        zone_low = listing_day_high * (1.0 - LISTING_WATCHLIST_NEAR_HIGH_ZONE_PCT / 100.0)
        dump_days = 0
        for _, row in tail.iterrows():
            h = float(row["HIGH"])
            c = float(row["CLOSE"])
            if h >= zone_low and h > 0:
                dump_pct = (h - c) / h * 100.0
                if dump_pct > LISTING_WATCHLIST_NEAR_HIGH_DUMP_PCT:
                    dump_days += 1
        checklist["near_high_dump_days"] = dump_days
        if dump_days >= LISTING_WATCHLIST_MAX_NEAR_HIGH_DUMP_DAYS:
            return (
                False,
                f"Strict watchlist: repeated rejection near highs ({dump_days} days)",
                checklist,
            )

    # Volatility contraction (first vs second half of window)
    half = max(2, lookback // 2)
    first = tail.iloc[:half]
    second = tail.iloc[half:]

    def _mean_daily_range_pct(block: pd.DataFrame) -> float | None:
        if block.empty or len(block) < 1:
            return None
        close_safe = block["CLOSE"].replace(0, np.nan)
        r = ((block["HIGH"] - block["LOW"]) / close_safe * 100.0).mean()
        return float(r) if pd.notna(r) else None

    r_prior = _mean_daily_range_pct(first)
    r_recent = _mean_daily_range_pct(second)
    contraction_ok: bool | None = None
    if r_prior is not None and r_recent is not None and r_prior > 0:
        contraction_ok = r_recent <= r_prior
    checklist["prior_half_avg_range_pct"] = round(r_prior, 2) if r_prior is not None else None
    checklist["recent_half_avg_range_pct"] = round(r_recent, 2) if r_recent is not None else None
    checklist["volatility_contraction"] = contraction_ok

    if LISTING_WATCHLIST_REQUIRE_RANGE_CONTRACTION:
        if contraction_ok is None:
            return False, "Strict watchlist: could not measure volatility contraction", checklist
        if not contraction_ok:
            return (
                False,
                "Strict watchlist: no volatility contraction",
                checklist,
            )

    # Volume dry-up vs prior window
    if LISTING_WATCHLIST_REQUIRE_VOL_DRYUP:
        r_d = LISTING_WATCHLIST_VOL_DRYUP_RECENT_DAYS
        p_d = LISTING_WATCHLIST_VOL_DRYUP_PRIOR_DAYS
        vol_all = pd.to_numeric(df["VOLUME"], errors="coerce")
        recent_mean = float(vol_all.tail(r_d).mean())
        prior_slice = vol_all.iloc[-(r_d + p_d) : -r_d]
        prior_mean = float(prior_slice.mean()) if len(prior_slice) > 0 else 0.0
        ratio = (recent_mean / prior_mean) if prior_mean > 0 else None
        checklist["vol_dryup_ratio_recent_vs_prior"] = round(ratio, 3) if ratio is not None else None
        if ratio is None or prior_mean <= 0:
            return False, "Strict watchlist: cannot measure volume dry-up (prior volume)", checklist
        if ratio > LISTING_WATCHLIST_VOL_DRYUP_MAX_RATIO:
            return (
                False,
                f"Strict watchlist: no volume dry-up ({ratio:.2f} > max {LISTING_WATCHLIST_VOL_DRYUP_MAX_RATIO} recent vs prior)",
                checklist,
            )

    # Falling price + falling volume together
    if LISTING_WATCHLIST_REJECT_FALLING_VOL_AND_PRICE and len(tail) >= 5:
        closes = tail["CLOSE"].astype(float)
        vols = tail["VOLUME"].astype(float)
        p_weak = closes.iloc[-1] < closes.iloc[0] and closes.iloc[-3:].mean() < closes.iloc[:3].mean()
        v_weak = vols.iloc[-1] < vols.iloc[0] and vols.iloc[-3:].mean() < vols.iloc[:3].mean()
        checklist["falling_price_and_volume"] = bool(p_weak and v_weak)
        if p_weak and v_weak:
            return (
                False,
                "Strict watchlist: falling price with falling volume (distribution)",
                checklist,
            )
    else:
        checklist["falling_price_and_volume"] = False

    # Choppy structure
    chg = tail["CLOSE"].pct_change().dropna()
    if len(chg) >= 2:
        signs = np.sign(chg.to_numpy(dtype=float))
        flips = int((np.abs(np.diff(signs)) > 0).sum())
        checklist["direction_flips"] = flips
        if flips > LISTING_WATCHLIST_MAX_SIGN_FLIPS:
            return (
                False,
                f"Strict watchlist: choppy structure ({flips} direction flips, max {LISTING_WATCHLIST_MAX_SIGN_FLIPS})",
                checklist,
            )

    checklist["perfect_base"] = True
    return True, "", checklist


def _assign_breakout_tier(
    signal_type: str,
    confirmed: bool,
    perfect_base: bool,
    volume_ratio: float,
    days_since_listing: int,
    post_confirm_move_pct: float = 0.0,
) -> tuple:
    """
    Assign quality tier + position size to a confirmed breakout signal.
    Returns (tier, position_size_pct, rationale).
    Returns (None, None, reason) when no tier qualifies — caller must reject.

    Tier rules (mutually exclusive, evaluated top-down):
        A+          → perfect base + listing-high breakout + vol ≥ 1.8× + age ≤ 60d  → 100%
        A           → pure momentum, vol ≥ 2.0×, age ≤ 45d, NOT perfect base         → 60%
        B           → BASE_BREAKOUT type only                                          → 40%
        Fallback A  → vol ≥ 1.8×, age ≤ 75d (controlled edge-extender)               → 50%
        Reject      → anything else, or unconfirmed, or post-confirm move too small

    WATCHLIST is never a trade: returns (None, None, ...) always.
    """
    if not confirmed:
        return None, None, "No tier: signal not yet confirmed (PENDING state)"

    # Post-confirm weak-move filter: kills slow/dead breakouts
    if LISTING_MIN_POST_CONFIRM_MOVE_PCT > 0 and post_confirm_move_pct < LISTING_MIN_POST_CONFIRM_MOVE_PCT:
        return (
            None, None,
            f"Weak breakout: post-confirm move {post_confirm_move_pct:.2f}% "
            f"< required {LISTING_MIN_POST_CONFIRM_MOVE_PCT}%"
        )

    if signal_type == "BREAKOUT":
        # ── A+: perfect structure + listing-high breakout + volume + fresh ────────
        if (
            perfect_base
            and volume_ratio >= LISTING_TIER_APLUS_MIN_VOLUME_MULT
            and days_since_listing <= LISTING_TIER_APLUS_MAX_AGE_DAYS
        ):
            return (
                "A+", 100,
                "Perfect base + listing-high breakout + volume — trail aggressively, hold longer"
            )

        # ── A: pure momentum — strong volume, explicitly NOT perfect base ─────────
        if (
            not perfect_base
            and volume_ratio >= LISTING_TIER_A_MIN_VOLUME_MULT
            and days_since_listing <= LISTING_TIER_A_MAX_AGE_DAYS
        ):
            return (
                "A", 60,
                "Pure momentum breakout — smaller position, faster exit, don't expect long trend"
            )

        # ── B: base breakout below listing high ───────────────────────────────────
        # (handled below via signal_type == "BASE_BREAKOUT" branch)

        # ── Controlled fallback A: valid breakout outside primary tier windows ────
        if (
            volume_ratio >= LISTING_TIER_APLUS_MIN_VOLUME_MULT   # minimum 1.8× bar
            and days_since_listing <= LISTING_TIER_A_FALLBACK_MAX_AGE_DAYS
        ):
            return (
                "A", LISTING_TIER_A_FALLBACK_POSITION_SIZE_PCT,
                f"Controlled fallback breakout (age {days_since_listing}d ≤ {LISTING_TIER_A_FALLBACK_MAX_AGE_DAYS}d) — opportunistic, reduced size"
            )

        return None, None, "Breakout confirmed but outside all tier age/volume windows — no trade"

    if signal_type == "BASE_BREAKOUT":
        return (
            "B", LISTING_TIER_B_POSITION_SIZE_PCT,
            "Accumulation base breakout below listing high — medium conviction, normal swing trade"
        )

    # WATCHLIST is never a trade
    return None, None, f"Signal type '{signal_type}' does not qualify for tier assignment"


# Legacy CSV initialization removed

def load_listing_data():
    """Load listing day data from MongoDB listing_data collection."""
    try:
        from db import listing_data_col
        if listing_data_col is None:
            logger.warning("[DB] listing_data_col unavailable - returning empty DataFrame")
            return pd.DataFrame()
        docs = list(listing_data_col.find({}, {"_id": 0}))
        if not docs:
            return pd.DataFrame()
        df = pd.DataFrame(docs)
        if 'listing_date' in df.columns and not df.empty:
            df['listing_date'] = pd.to_datetime(df['listing_date'], utc=True, errors='coerce').dt.tz_localize(None).dt.date
        return df
    except Exception as e:
        logger.error(f"Error loading listing data from MongoDB: {e}")
        return pd.DataFrame()

def save_listing_data(df):
    """Save listing data to MongoDB only."""
    try:
        from db import upsert_listing_data
        for _, row in df.iterrows():
            upsert_listing_data(row['symbol'], row.to_dict())
    except Exception as e:
        logger.error(f"Error saving listing data to MongoDB: {e}")


def initialize_watchlist_data_csv():
    """No-op: watchlist signals are written to MongoDB. Kept for call-site compatibility."""
    pass


def load_pending_breakouts():
    """Load pending breakout confirmation state from disk."""
    if not os.path.exists(PENDING_BREAKOUTS_FILE):
        return {}
    try:
        with open(PENDING_BREAKOUTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_pending_breakouts(data):
    """Persist pending breakout confirmation state."""
    try:
        with open(PENDING_BREAKOUTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Could not save pending breakouts: {e}")


def _now_ist():
    # Keep one IST source for consistent timestamps
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _market_is_open_ist():
    now = _now_ist()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt_time(9, 15) <= t <= dt_time(15, 30)


def _get_intraday_bars(symbol, interval="5m", period="1d"):
    """Fetch intraday bars from yfinance for confirmation logic."""
    if not YFINANCE_AVAILABLE:
        return pd.DataFrame()
    try:
        df = yf.Ticker(f"{symbol}.NS").history(period=period, interval=interval)
        if df is None or df.empty:
            return pd.DataFrame()
        # Normalize columns to expected shape
        out = df.reset_index().rename(columns={
            "Datetime": "DATE", "Open": "OPEN", "High": "HIGH", "Low": "LOW", "Close": "CLOSE", "Volume": "VOLUME"
        })
        cols = [c for c in ["DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"] if c in out.columns]
        return out[cols]
    except Exception:
        return pd.DataFrame()


def _leader_score(entry_above_high_pct, volume_spike, risk_reward, current_price, listing_high, listing_range_pct):
    """
    Simple leader score (0-8 with extension penalty) for selection quality.
    """
    score = 0
    # Volume strength (0-2)
    if volume_spike >= 2.0:
        score += 2
    elif volume_spike >= 1.5:
        score += 1
    # Breakout strength (0-2)
    if current_price >= listing_high * 1.01:
        score += 2
    elif current_price >= listing_high:
        score += 1
    # Structure quality proxy (0-2) tighter range preferred
    if listing_range_pct <= 18:
        score += 2
    elif listing_range_pct <= 28:
        score += 1
    # Close/risk quality (0-2)
    if risk_reward >= 1.8:
        score += 2
    elif risk_reward >= 1.25:
        score += 1
    # Extension penalty (-2 to 0)
    if entry_above_high_pct > 4.0:
        score -= 2
    elif entry_above_high_pct > 2.5:
        score -= 1
    return score

def get_listing_day_data(symbol, listing_date):
    """Fetch and extract listing day high/low from historical data"""
    try:
        logger.info(f"📊 Fetching listing day data for {symbol} (listing date: {listing_date})")
        
        # Convert listing_date to date object if needed (before calling fetch_data)
        if isinstance(listing_date, str):
            listing_date = pd.to_datetime(listing_date).date()
        elif hasattr(listing_date, 'date'):
            listing_date = listing_date.date()
        elif isinstance(listing_date, pd.Timestamp):
            listing_date = listing_date.date()
        
        # Fetch data starting from listing date
        df = fetch_data(symbol, listing_date)
        
        if df is None or df.empty:
            logger.warning(f"⚠️ No data available for {symbol} on listing date")
            return None
        
        # Find listing day data
        df['DATE'] = pd.to_datetime(df['DATE']).dt.date
        listing_day_data = df[df['DATE'] == listing_date]
        
        if listing_day_data.empty:
            # Try to get first day of data (might be listing day)
            listing_day_data = df.iloc[[0]]
            logger.info(f"Using first available day as listing day for {symbol}")
        
        if listing_day_data.empty:
            logger.warning(f"⚠️ Could not find listing day data for {symbol}")
            return None
        
        # Extract listing day metrics
        listing_day_high = float(listing_day_data['HIGH'].iloc[0])
        listing_day_low = float(listing_day_data['LOW'].iloc[0])
        listing_day_close = float(listing_day_data['CLOSE'].iloc[0])
        listing_day_volume = float(listing_day_data['VOLUME'].iloc[0])
        actual_listing_date = listing_day_data['DATE'].iloc[0]
        
        logger.info(f"✅ Listing day data for {symbol}:")
        logger.info(f"   High: ₹{listing_day_high:.2f}")
        logger.info(f"   Low: ₹{listing_day_low:.2f}")
        logger.info(f"   Close: ₹{listing_day_close:.2f}")
        logger.info(f"   Volume: {listing_day_volume:,.0f}")
        
        return {
            'symbol': symbol,
            'listing_date': actual_listing_date,
            'listing_day_high': listing_day_high,
            'listing_day_low': listing_day_low,
            'listing_day_close': listing_day_close,
            'listing_day_volume': listing_day_volume,
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'status': 'ACTIVE'
        }
    
    except Exception as e:
        logger.error(f"Error getting listing day data for {symbol}: {e}")
        return None

def update_listing_data_for_new_ipos():
    """Check for new IPOs in MongoDB ipos_col and add their listing day data."""
    try:
        # Load recent IPOs from MongoDB
        from db import ipos_col
        if ipos_col is None:
            logger.warning("[DB] ipos_col unavailable — cannot update listing data")
            return

        docs = list(ipos_col.find({}, {"_id": 0, "symbol": 1, "listing_date": 1}))
        if not docs:
            logger.warning("[DB] ipos_col is empty — no recent IPOs to process")
            return

        recent_ipos = pd.DataFrame(docs)
        recent_ipos['listing_date'] = pd.to_datetime(
            recent_ipos['listing_date'], utc=True, errors='coerce'
        ).dt.tz_localize(None).dt.date

        # Load existing listing data symbols from MongoDB
        listing_data = load_listing_data()
        existing_symbols = set(listing_data['symbol'].tolist()) if not listing_data.empty else set()

        new_ipos = 0

        for _, row in recent_ipos.iterrows():
            symbol = row['symbol']
            listing_date = row['listing_date']

            # Skip RE/SME noise
            if pd.isna(symbol) or '-RE' in str(symbol) or str(symbol).endswith('-SM') or 'RE1' in str(symbol):
                continue

            # Skip future listings (NSE archives only update EOD)
            if pd.isna(listing_date) or listing_date >= datetime.now().date():
                continue

            # Skip if already exists
            if symbol in existing_symbols:
                continue

            listing_info = get_listing_day_data(symbol, listing_date)

            if listing_info:
                new_row = pd.DataFrame([listing_info])
                listing_data = new_row if listing_data.empty else pd.concat(
                    [listing_data, new_row], ignore_index=True
                )
                new_ipos += 1
                logger.info(f"✅ Added listing data for {symbol}")
                time.sleep(0.5)
            else:
                logger.warning(f"⚠️ Could not get listing data for {symbol} - skipping")

        if new_ipos > 0:
            save_listing_data(listing_data)
            logger.info(f"✅ Updated listing data: {new_ipos} new IPOs added")
        else:
            logger.info("✅ No new IPOs to add")

    except Exception as e:
        logger.error(f"Error updating listing data: {e}")

def calculate_signal_score_components(tier, volume_ratio, perfect_base, post_confirm_pct):
    tier_weight = 4.0 if tier == 'A+' else (3.0 if tier == 'A' else (2.0 if tier == 'B' else 1.0))
    volume_score = min(2.0, float(volume_ratio) / 2.0) if volume_ratio else 0.0
    base_score = 2.0 if perfect_base else 0.5
    momentum_score = min(2.0, float(post_confirm_pct) / 2.0) if post_confirm_pct else 0.0
    
    total_score = min(10.0, tier_weight + volume_score + base_score + momentum_score)
    return {
        "tier_weight": round(tier_weight, 2),
        "volume_score": round(volume_score, 2),
        "base_score": round(base_score, 2),
        "momentum_score": round(momentum_score, 2),
        "total_score": round(total_score, 2)
    }

def check_listing_day_breakout(symbol, listing_info, pending_breakouts=None):
    """Check if symbol has broken listing day high with volume"""
    try:
        listing_day_high = listing_info['listing_day_high']
        listing_day_low = listing_info['listing_day_low']
        listing_day_close = listing_info.get('listing_day_close', listing_day_high)  # Fallback to high if close not available
        listing_day_volume = float(listing_info.get('listing_day_volume', 0))  # CRITICAL: was missing, caused NameError
        listing_date = listing_info['listing_date']
        last_updated = listing_info.get('last_updated', 'N/A')  # When listing data was captured
        
        # Convert listing_date to date object if needed (from CSV it might be string)
        if isinstance(listing_date, str):
            listing_date = pd.to_datetime(listing_date).date()
        elif hasattr(listing_date, 'date'):
            listing_date = listing_date.date()
        elif isinstance(listing_date, pd.Timestamp):
            listing_date = listing_date.date()
        
        # Standardized Rejection Telemetry (Phase 2.2)
        _rejection_logged = False
        def _log_listing_rejection(reason: str, value: float, threshold: float, metrics: dict):
            nonlocal _rejection_logged
            if _rejection_logged: return
            
            # Near-miss filter for listing day: within 10% of high
            is_interesting = (
                current_high >= listing_day_high * 0.90
            )
            if not is_interesting: return

            _rejection_logged = True
            payload = {
                "symbol": symbol,
                "action": "REJECTED_BREAKOUT",
                "log_type": "REJECTED",
                "rejection_reason": reason,
                "failing_metric": reason,
                "failing_value": round(value, 2),
                "threshold": round(threshold, 2),
                "base_zone_passed": True,
                "metrics": {
                    "perf": metrics.get("perf", None),
                    "prng": metrics.get("prng", None),
                    "vol_ratio": metrics.get("vol_ratio", None),
                    "rsi": metrics.get("rsi", None),
                    "score": metrics.get("score", None)
                },
                "source": "live"
            }
            write_daily_log("listing_day", symbol, "REJECTED_BREAKOUT", payload, log_type="REJECTED")
            logger.debug(f"[Telemetry] Logged listing rejection for {symbol}: {reason}")

        # Fetch current data
        df = fetch_data(symbol, listing_date)
        
        if df is None or df.empty:
            return None
        
        # Get latest data from historical
        latest = df.iloc[-1]
        historical_high = float(latest['HIGH'])
        current_volume = float(latest['VOLUME'])
        current_date = latest['DATE']
        
        # Validate data freshness - check if latest data is from today
        latest_date = latest['DATE']
        if isinstance(latest_date, pd.Timestamp):
            latest_date = latest_date.date()
        elif hasattr(latest_date, 'date'):
            latest_date = latest_date.date()
        else:
            latest_date = pd.to_datetime(latest_date).date()
        
        today_date = datetime.today().date()
        days_old = (today_date - latest_date).days
        
        # CRITICAL: Get LIVE price FIRST for accurate breakout detection
        current_price = None
        current_high = None
        price_source = "Historical Close"
        
        try:
            live_price, live_source, day_high = get_live_price(symbol)
            if live_price is not None and live_price > 0:
                current_price = live_price
                current_high = day_high if day_high else live_price
                price_source = f"Live ({live_source})"
                logger.info(f"✅ Using live price for {symbol} breakout detection: ₹{current_price:.2f} (High: ₹{current_high:.2f}) from {live_source}")
        except Exception as e:
            logger.debug(f"Could not get live price for {symbol}: {e}")
        
        # --- INSTITUTIONAL LIQUIDITY GUARDRAIL (Phase 2.5) ---
        avg_turnover_cr, circuit_days_15, mcap_cr = scanner_module.get_liquidity_metrics(symbol, df)
        
        # We need these metrics in the payload/log
        liquidity_metrics = {
            "avg_turnover_cr": avg_turnover_cr,
            "circuit_days_15": circuit_days_15,
            "market_cap_cr": mcap_cr
        }

        if avg_turnover_cr > 0 and avg_turnover_cr < scanner_module.MIN_DAILY_TURNOVER_CR:
            logger.info(f"⏭️ Skipping {symbol} - Turnover ₹{avg_turnover_cr:.2f}Cr is below ₹{scanner_module.MIN_DAILY_TURNOVER_CR}Cr floor")
            _log_listing_rejection("LIQUIDITY_TRAP", avg_turnover_cr, scanner_module.MIN_DAILY_TURNOVER_CR, liquidity_metrics)
            return None
        if circuit_days_15 >= scanner_module.CIRCUIT_DAY_THRESHOLD:
            logger.info(f"⏭️ Skipping {symbol} - Frequent circuits detected ({circuit_days_15} days)")
            _log_listing_rejection("LIQUIDITY_TRAP_CIRCUITS", circuit_days_15, scanner_module.CIRCUIT_DAY_THRESHOLD, liquidity_metrics)
            return None
        if mcap_cr > 0 and mcap_cr < scanner_module.MIN_MARKET_CAP_CR:
            logger.info(f"⏭️ Skipping {symbol} - Market Cap ₹{mcap_cr:.1f}Cr is below ₹{scanner_module.MIN_MARKET_CAP_CR}Cr floor")
            _log_listing_rejection("MICROCAP_PENALTY", mcap_cr, scanner_module.MIN_MARKET_CAP_CR, liquidity_metrics)
            return None
        # ---------------------------------------------------
        
        # Fallback to historical data if live price unavailable
        if current_price is None:
            current_price = float(latest['CLOSE'])
            current_high = historical_high  # Use historical high
            price_source = f"Historical Close ({latest_date.strftime('%Y-%m-%d')})"
            
            # Warn if data is stale
            if days_old > 1:
                logger.warning(f"⚠️ Using stale data for {symbol}: {days_old} days old ({latest_date})")
            elif days_old == 0:
                logger.info(f"✅ Using today's historical close for {symbol}: ₹{current_price:.2f}")
            else:
                logger.info(f"⚠️ Using yesterday's close for {symbol}: ₹{current_price:.2f} (market may be closed)")

        # --- PRICE FLOOR GUARDRAIL (Second Check) ---
        if current_price is not None and current_price < scanner_module.MIN_ENTRY_PRICE_RS:
            logger.info(f"⏭️ Skipping {symbol} - Price ₹{current_price:.2f} is below ₹{scanner_module.MIN_ENTRY_PRICE_RS:.2f} floor")
            return None
        # --------------------------------------------
        
        # Log breakout level comparison
        logger.info(f"📊 {symbol} Breakout Level Check:")
        logger.info(f"   Listing Day High: ₹{listing_day_high:.2f}")
        logger.info(f"   Current High: ₹{current_high:.2f} ({price_source})")
        logger.info(f"   Breakout Required: Current High > ₹{listing_day_high:.2f}")
        
        # Calculate average volume (last 10 days excluding listing day)
        if len(df) > 1:
            recent_df = df.tail(10)
            avg_volume = recent_df['VOLUME'].mean()
        else:
            avg_volume = current_volume
        
        # Check for breakout
        is_breakout = False
        breakout_conditions = []
        rejection_reason = None
        volume_warnings = []  # Track volume-related warnings
        
        if current_high > listing_day_high:
            is_breakout = True
            signal_type = 'BREAKOUT'
            breakout_conditions.append(f"Price broke listing day high ({current_high:.2f} > {listing_day_high:.2f})")
        elif current_high >= listing_day_high * 0.95:
            # Watchlist condition: Within 5% of listing high
            is_breakout = True  # We set this to True to proceed with calculations, but mark type as WATCHLIST
            signal_type = 'WATCHLIST'
            breakout_conditions.append(f"Near Breakout: {current_high:.2f} is within 5% of {listing_day_high:.2f}")
            logger.info(f"👀 {symbol}: Detected as WATCHLIST candidate (High: {current_high:.2f}, Trigger: {listing_day_high:.2f})")
        elif LISTING_TIER_B_ENABLED:
            # Tier B candidate: > 5% below listing high — validate tight base below
            is_breakout = True
            signal_type = 'BASE_BREAKOUT'
            logger.info(f"📦 {symbol}: Checking Tier B base breakout (live high {current_high:.2f}, listing high {listing_day_high:.2f})")
        else:
            rejection_reason = f"Price ({current_high:.2f}) below listing day high ({listing_day_high:.2f})"
            _log_listing_rejection("below_listing_high", current_high, listing_day_high, {"current_high": current_high, "listing_high": listing_day_high})
        
        # Condition 2: Volume confirmation (now a warning, not a rejection)
        volume_spike = current_volume >= avg_volume * MIN_VOLUME_MULTIPLIER
        if volume_spike:
            breakout_conditions.append(f"Volume spike ({current_volume:,.0f} vs avg {avg_volume:,.0f})")
        elif is_breakout:
            # Price broke but volume insufficient - add warning instead of rejecting
            volume_warnings.append(f"Low volume spike: {current_volume:,.0f} vs avg {avg_volume:,.0f} (need {MIN_VOLUME_MULTIPLIER}x)")
        
        # Proceed if price broke listing day high OR is watchlist
        if is_breakout:
            # Tracking vars for tier classification (populated below per path)
            base_range_high: float = 0.0
            perfect_base_ok: bool = False

            # Calculate entry, stop loss, and target
            # For Watchlist, use Listing High as the hypothetical entry price
            if signal_type == 'WATCHLIST':
                entry_price = listing_day_high
            else:
                entry_price = current_price  # For confirmed breakout, use current price
            
            # CRITICAL FIX: Calculate target based on ENTRY price, not listing day high
            # This ensures target is always above entry price
            listing_range = listing_day_high - listing_day_low
            listing_range_pct = (listing_range / listing_day_high * 100) if listing_day_high > 0 else 0
            
            # Note: Listing day range is not used for rejection - listing day low is last support level
            # Stop loss is purely percentage-based (8% below entry), not based on listing day low
            
            # Calculate how far above listing high the entry is
            entry_above_high = entry_price - listing_day_high
            entry_above_high_pct = (entry_above_high / listing_day_high * 100) if listing_day_high > 0 else 0
            
            # FILTER 2: Only generate signals if entry is within reasonable distance of listing high
            # This prevents generating signals when breakout happened long ago
            # Only apply for actual BREAKOUTs, not WATCHLIST
            if signal_type == 'BREAKOUT' and entry_above_high_pct > MAX_ENTRY_ABOVE_HIGH_PCT:
                rejection_reason = f"Entry ({entry_price:.2f}) is {entry_above_high_pct:.1f}% above listing high ({listing_day_high:.2f}) - too far from breakout level"
                logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                return None
            
            # Days since listing (used for display + strict watchlist / breakout gates)
            today_date = datetime.today().date()
            if isinstance(listing_date, str):
                listing_date_obj = pd.to_datetime(listing_date).date()
            elif hasattr(listing_date, 'date'):
                listing_date_obj = listing_date.date()
            else:
                listing_date_obj = listing_date
            
            days_since_listing = (today_date - listing_date_obj).days
            vol_vs_avg = (current_volume / avg_volume) if avg_volume > 0 else 0.0

            # --- Strict watchlist gate: same strategic lens as IPO momentum (no stale / dead-volume radar) ---
            if LISTING_STRICT_QUALITY and signal_type == 'WATCHLIST':
                if days_since_listing > LISTING_WATCHLIST_ABSOLUTE_MAX_AGE_DAYS:
                    rejection_reason = (
                        f"Strict watchlist: IPO age {days_since_listing}d > absolute max "
                        f"{LISTING_WATCHLIST_ABSOLUTE_MAX_AGE_DAYS}d (not IPO-base regime)"
                    )
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                    return None
                if days_since_listing > LISTING_WATCHLIST_MAX_DAYS_SINCE_LISTING:
                    rejection_reason = (
                        f"Strict watchlist: {days_since_listing}d since listing "
                        f"(max {LISTING_WATCHLIST_MAX_DAYS_SINCE_LISTING}d)"
                    )
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                    return None
                if LISTING_WATCHLIST_MIN_VOLUME_MULT > 0 and vol_vs_avg < LISTING_WATCHLIST_MIN_VOLUME_MULT:
                    rejection_reason = (
                        f"Strict watchlist: volume {vol_vs_avg:.2f}x avg "
                        f"(need ≥{LISTING_WATCHLIST_MIN_VOLUME_MULT}x)"
                    )
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                    return None
                if LISTING_WATCHLIST_MAX_VOL_VS_AVG > 0 and vol_vs_avg > LISTING_WATCHLIST_MAX_VOL_VS_AVG:
                    rejection_reason = (
                        f"Strict watchlist: volume {vol_vs_avg:.2f}x avg "
                        f"(need ≤{LISTING_WATCHLIST_MAX_VOL_VS_AVG}x for dry-up)"
                    )
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                    return None

                base_ok, base_reason, base_metrics = _evaluate_watchlist_perfect_base(
                    df,
                    LISTING_WATCHLIST_BASE_LOOKBACK,
                    float(listing_day_high),
                    float(current_high),
                )
                if not base_ok:
                    rejection_reason = base_reason
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason} {base_metrics}")
                    return None
                perfect_base_ok = True  # watchlist passed — track for debug (WATCHLIST is not a trade)

            # --- Tier B gate: validate base breakout below listing high ---
            if signal_type == 'BASE_BREAKOUT':
                dist_below_high_pct = (
                    (listing_day_high - current_high) / listing_day_high * 100.0
                    if listing_day_high > 0 else 0.0
                )
                if days_since_listing > LISTING_TIER_B_MAX_AGE_DAYS:
                    rejection_reason = (
                        f"Tier B: IPO age {days_since_listing}d > max {LISTING_TIER_B_MAX_AGE_DAYS}d"
                    )
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                    return None
                if dist_below_high_pct > LISTING_TIER_B_MAX_DISTANCE_FROM_HIGH_PCT:
                    rejection_reason = (
                        f"Tier B: {dist_below_high_pct:.1f}% below listing high "
                        f"(max {LISTING_TIER_B_MAX_DISTANCE_FROM_HIGH_PCT}%)"
                    )
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                    return None
                # Base quality (skip proximity — intentionally below listing high)
                base_ok_b, base_reason_b, _ = _evaluate_watchlist_perfect_base(
                    df,
                    LISTING_WATCHLIST_BASE_LOOKBACK,
                    float(listing_day_high),
                    float(current_high),
                    proximity_check=False,
                )
                if not base_ok_b:
                    rejection_reason = f"Tier B base check: {base_reason_b}"
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                    return None
                # Confirm current_high actually broke above the recent base range high
                _tail_b = df.tail(LISTING_WATCHLIST_BASE_LOOKBACK).copy()
                _tail_b["HIGH"] = pd.to_numeric(_tail_b["HIGH"], errors="coerce")
                _tail_b = _tail_b.dropna(subset=["HIGH"])
                # exclude today's bar when computing historical base high
                base_range_high = (
                    float(_tail_b["HIGH"].iloc[:-1].max())
                    if len(_tail_b) > 1
                    else float(_tail_b["HIGH"].max())
                )
                if current_high <= base_range_high:
                    rejection_reason = (
                        f"Tier B: current high {current_high:.2f} ≤ base high {base_range_high:.2f}"
                    )
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                    return None
                perfect_base_ok = True
                breakout_conditions.append(
                    f"Base breakout: high {current_high:.2f} > base range high {base_range_high:.2f} "
                    f"({dist_below_high_pct:.1f}% below listing high)"
                )
                logger.info(
                    f"✅ {symbol}: Tier B BASE_BREAKOUT validated "
                    f"(base {base_range_high:.2f} → {current_high:.2f}, listing high {listing_day_high:.2f})"
                )

            volume_vs_listing_day = current_volume / listing_day_volume if listing_day_volume > 0 else 0
            if volume_vs_listing_day < MIN_VOLUME_VS_LISTING_DAY:
                volume_warnings.append(f"Low volume vs listing day: {volume_vs_listing_day:.1f}x (need {MIN_VOLUME_VS_LISTING_DAY:.1f}x)")
                if signal_type == 'BREAKOUT' and not LISTING_STRICT_QUALITY:
                    logger.warning(f"⚠️ {symbol}: Low volume vs listing day ({volume_vs_listing_day:.1f}x, need {MIN_VOLUME_VS_LISTING_DAY:.1f}x) - sending signal with caution")

            # --- Strict quality gate (default): only persist / alert full-quality breakouts ---
            if LISTING_STRICT_QUALITY and signal_type == 'BREAKOUT':
                if days_since_listing > MAX_DAYS_SINCE_LISTING_FOR_BREAKOUT:
                    rejection_reason = (
                        f"Strict: {days_since_listing}d since listing (max {MAX_DAYS_SINCE_LISTING_FOR_BREAKOUT}d)"
                    )
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                    return None
                if not volume_spike:
                    rejection_reason = (
                        f"Strict: volume spike required (current {current_volume:,.0f} vs avg {avg_volume:,.0f}, need {MIN_VOLUME_MULTIPLIER}x)"
                    )
                    logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                    return None
                if listing_day_volume > 0:
                    if volume_vs_listing_day < MIN_VOLUME_VS_LISTING_DAY:
                        rejection_reason = (
                            f"Strict: volume vs listing day {volume_vs_listing_day:.2f}x < {MIN_VOLUME_VS_LISTING_DAY}x"
                        )
                        logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                        _log_listing_rejection("low_volume_vs_listing", volume_vs_listing_day, MIN_VOLUME_VS_LISTING_DAY, {"vol_ratio": volume_vs_listing_day})
                        return None
                else:
                    if current_volume < avg_volume * MIN_VOL_MULT_WHEN_NO_LISTING_VOL:
                        rejection_reason = (
                            f"Strict: listing day volume missing/0 — need current vol ≥ {MIN_VOL_MULT_WHEN_NO_LISTING_VOL}x avg ({avg_volume:,.0f})"
                        )
                        logger.info(f"⏭️ Skipping {symbol}: {rejection_reason}")
                        return None
                # Passed strict checks — treat as high-quality (no LOW_VOL grade)
                volume_warnings = []
            
            # Stop loss % below entry (configurable via LISTING_STOP_LOSS_PCT)
            stop_loss_pct = STOP_LOSS_PCT / 100.0
            
            # Calculate stop loss purely based on entry price percentage
            stop_loss = entry_price * (1 - stop_loss_pct)
            
            # Target calculation: Use entry price + percentage of listing range
            if entry_above_high_pct <= 2.0:
                target_multiplier = 1.0
            elif entry_above_high_pct <= 5.0:
                target_multiplier = 0.75
            else:
                target_multiplier = 0.5
            
            target_price = entry_price + (listing_range * target_multiplier)
            
            # Risk/Reward
            risk = entry_price - stop_loss
            reward = target_price - entry_price
            risk_reward = reward / risk if risk > 0 else 0
            
            # FILTER: Minimum risk/reward ratio (reward must be at least equal to risk)
            if risk_reward < MIN_RISK_REWARD:
                rejection_reason = f"Risk/Reward ratio ({risk_reward:.2f}) below minimum ({MIN_RISK_REWARD:.1f})"
                logger.info(f"⏭️ Skipping {symbol}: Risk/Reward ratio ({risk_reward:.2f}) is below minimum ({MIN_RISK_REWARD:.1f})")
                return None

            # Leader score gate (selection quality)
            leader_score = _leader_score(
                entry_above_high_pct=entry_above_high_pct,
                volume_spike=(current_volume / avg_volume) if avg_volume > 0 else 0,
                risk_reward=risk_reward,
                current_price=current_price,
                listing_high=listing_day_high,
                listing_range_pct=listing_range_pct
            )
            min_leader_for_signal = None
            if signal_type == 'BREAKOUT':
                min_leader_for_signal = LISTING_MIN_LEADER_SCORE
            elif LISTING_STRICT_QUALITY and signal_type == 'WATCHLIST':
                min_leader_for_signal = LISTING_WATCHLIST_MIN_LEADER_SCORE
            if min_leader_for_signal is not None and leader_score < min_leader_for_signal:
                logger.info(
                    f"⏭️ Skipping {symbol}: Leader score {leader_score} < {min_leader_for_signal} "
                    f"({'breakout' if signal_type == 'BREAKOUT' else 'watchlist'})"
                )
                _log_listing_rejection("low_leader_score", leader_score, min_leader_for_signal, {"leader_score": leader_score})
                return None

            # --- Detect perfect base for BREAKOUT signals (used for A+ tier eligibility) ---
            if signal_type == 'BREAKOUT':
                _pb_ok, _, _pb_metrics = _evaluate_watchlist_perfect_base(
                    df,
                    LISTING_WATCHLIST_BASE_LOOKBACK,
                    float(listing_day_high),
                    float(current_high),
                    proximity_check=False,  # price is above listing high — proximity irrelevant
                )
                perfect_base_ok = _pb_ok
                if _pb_ok:
                    logger.info(f"✅ {symbol}: BREAKOUT has tight base — A+ tier eligible")
                    write_daily_log("listing_day", symbol, "PERFECT_BASE_DETECTED", _pb_metrics)

            # Intraday confirmation engine: PENDING -> CONFIRMED -> ENTER
            if signal_type == 'BREAKOUT' and LISTING_CONFIRMATION_MINUTES > 0 and _market_is_open_ist():
                if pending_breakouts is None:
                    pending_breakouts = {}
                state = pending_breakouts.get(symbol)
                now_ts = _now_ist()
                now_iso = now_ts.isoformat()
                if not state:
                    pending_breakouts[symbol] = {
                        "started_at": now_iso,
                        "breakout_level": float(listing_day_high),
                        "max_price_seen": float(current_price),
                        "last_price": float(current_price)
                    }
                    logger.info(f"⏳ {symbol}: breakout moved to PENDING for {LISTING_CONFIRMATION_MINUTES}m confirmation")
                    write_daily_log("listing_day", symbol, "PENDING_STARTED", {
                        "breakout_level": float(listing_day_high),
                        "confirm_minutes": LISTING_CONFIRMATION_MINUTES,
                        "price": round(float(current_price), 2),
                        "leader_score": int(leader_score),
                    })
                    return {
                        "symbol": symbol,
                        "type": "PENDING",
                        "current_price": round(current_price, 2),
                        "listing_day_high": listing_day_high,
                        "confirm_minutes": LISTING_CONFIRMATION_MINUTES,
                    }
                # update state
                started = datetime.fromisoformat(state["started_at"])
                state["max_price_seen"] = max(float(state.get("max_price_seen", current_price)), float(current_price))
                state["last_price"] = float(current_price)
                pending_breakouts[symbol] = state

                # rejection filter during observation
                max_seen = float(state["max_price_seen"])
                rejection_pct = ((max_seen - current_price) / max_seen * 100) if max_seen > 0 else 0
                if current_price < listing_day_high or rejection_pct > 2.5:
                    rej_reason = "below_breakout" if current_price < listing_day_high else "rejection_from_high"
                    pending_breakouts.pop(symbol, None)
                    logger.info(f"⏭️ {symbol}: pending confirmation rejected (price hold/rejection failed)")
                    write_daily_log("listing_day", symbol, "PENDING_REJECTED", {
                        "reason": rej_reason,
                        "current_price": round(float(current_price), 2),
                        "breakout_level": float(listing_day_high),
                        "rejection_pct": round(float(rejection_pct), 2),
                        "max_price_seen": round(float(max_seen), 2),
                        "elapsed_minutes": int((now_ts - started).total_seconds() // 60),
                    }, log_type="REJECTED")
                    return None

                elapsed_min = int((now_ts - started).total_seconds() // 60)
                if elapsed_min < LISTING_CONFIRMATION_MINUTES:
                    logger.info(f"⏳ {symbol}: pending {elapsed_min}/{LISTING_CONFIRMATION_MINUTES}m confirmed hold")
                    return {
                        "symbol": symbol,
                        "type": "PENDING",
                        "current_price": round(current_price, 2),
                        "listing_day_high": listing_day_high,
                        "confirm_minutes": LISTING_CONFIRMATION_MINUTES,
                    }
                # Confirmed
                pending_breakouts.pop(symbol, None)
                breakout_conditions.append(f"Confirmed hold {LISTING_CONFIRMATION_MINUTES}m above breakout")
                write_daily_log("listing_day", symbol, "PENDING_CONFIRMED", {
                    "breakout_level": float(listing_day_high),
                    "confirm_minutes": LISTING_CONFIRMATION_MINUTES,
                    "entry_reference": round(float(current_price), 2),
                    "leader_score": int(leader_score),
                    "elapsed_minutes": elapsed_min,
                })
            
            # Calculate gain from listing day close
            gain_from_listing_close = (
                (current_price - listing_day_close) / listing_day_close * 100
            ) if listing_day_close > 0 else 0

            # Post-confirm move: % price has moved beyond its breakout reference level
            if signal_type == 'BASE_BREAKOUT':
                post_confirm_move_pct = (
                    (current_high - base_range_high) / base_range_high * 100.0
                    if base_range_high > 0 else 0.0
                )
                # Target for BASE_BREAKOUT: the listing high is the natural objective
                if listing_day_high > entry_price:
                    target_price = listing_day_high
                    reward = target_price - entry_price
                    risk_reward = reward / risk if risk > 0 else 0
            else:
                post_confirm_move_pct = float(entry_above_high_pct)

            # --- Tier assignment (WATCHLIST always returns None — never a trade) ---
            vol_ratio_for_tier = (current_volume / avg_volume) if avg_volume > 0 else 0.0
            tier, position_size_pct, tier_rationale = _assign_breakout_tier(
                signal_type=signal_type,
                confirmed=True,  # PENDING returns early above; here we are always confirmed
                perfect_base=perfect_base_ok,
                volume_ratio=vol_ratio_for_tier,
                days_since_listing=days_since_listing,
                post_confirm_move_pct=post_confirm_move_pct,
            )
            if tier is None and signal_type != 'WATCHLIST':
                logger.info(f"⏭️ {symbol}: No tier assigned — {tier_rationale}")
                return None
            
            # --- Institutional Grade Penalty (Phase 2.5) ---
            # Cap signals for microcaps to Grade C (Higher risk, smaller size)
            if mcap_cr > 0 and mcap_cr < 1000.0 and tier in ["A+", "A", "B"]:
                logger.info(f"⚠️ Downgrading Listing Signal {symbol} from {tier} to C due to Microcap Penalty (< ₹1000Cr)")
                tier = "C"
                position_size_pct = 20 # Standard size for C tier

            # --- Analytics & Score Components ---
            if signal_type == 'BASE_BREAKOUT':
                breakout_level_for_calc = base_range_high
                consolidation_range_pct = (base_range_high - df['LOW'].min()) / df['LOW'].min() * 100.0 if df['LOW'].min() > 0 else None
            else:
                breakout_level_for_calc = listing_day_high
                consolidation_range_pct = None

            entry_vs_breakout_pct = ((entry_price - breakout_level_for_calc) / breakout_level_for_calc * 100.0) if breakout_level_for_calc > 0 else 0.0
            
            # Retrieve components computed during PENDING
            state = pending_breakouts.get(symbol) if pending_breakouts else None
            confirmation_time_min = 0
            max_extension_during_confirmation_pct = 0.0
            rejection_depth_pct = 0.0
            did_hold_breakout_level = True
            
            if state and signal_type == 'BREAKOUT':
                started = datetime.fromisoformat(state.get("started_at", _now_ist().isoformat()))
                confirmation_time_min = int((_now_ist() - started).total_seconds() // 60)
                max_seen = float(state.get("max_price_seen", current_price))
                max_extension_during_confirmation_pct = ((max_seen - listing_day_high) / listing_day_high * 100.0) if listing_day_high > 0 else 0.0
                rejection_depth_pct = ((max_seen - current_price) / max_seen * 100.0) if max_seen > 0 else 0.0
                
            score_comps = calculate_signal_score_components(tier, vol_ratio_for_tier, perfect_base_ok, post_confirm_move_pct)

            # --- Sustained Check (New Logic) ---
            is_sustained = current_price >= listing_day_high
            pullback_from_high_pct = ((current_high - current_price) / current_high * 100.0) if current_high > 0 else 0.0
            max_extension_pct = ((current_high - breakout_level_for_calc) / breakout_level_for_calc * 100.0) if breakout_level_for_calc > 0 else 0.0

            return {
                'symbol': symbol,
                'listing_date': listing_date,
                'listing_day_high': listing_day_high,
                'listing_day_low': listing_day_low,
                'listing_day_close': listing_day_close,
                'current_price': current_price,
                'current_high': current_high,
                'entry_price': round(entry_price, 2),
                'stop_loss': round(stop_loss, 2),
                'target_price': round(target_price, 2),
                'volume_spike': round(current_volume / avg_volume, 2),
                'volume_vs_listing_day': round(volume_vs_listing_day, 2),
                'listing_range_pct': round(listing_range_pct, 2),
                'risk_reward': round(risk_reward, 2),
                'breakout_date': current_date,
                'breakout_conditions': ' | '.join(breakout_conditions),
                'price_source': price_source,
                'days_since_listing': days_since_listing,
                'gain_from_listing_close': round(gain_from_listing_close, 2),
                'entry_above_high_pct': round(entry_above_high_pct, 2),
                'target_multiplier': round(target_multiplier, 2),
                'last_updated': last_updated,
                'volume_warnings': volume_warnings,
                'has_volume_caution': len(volume_warnings) > 0,
                'leader_score': int(leader_score),
                'type': signal_type,
                # --- Sustained Status ---
                'is_sustained': is_sustained,
                'pullback_from_high_pct': round(pullback_from_high_pct, 2),
                'max_extension_pct': round(max_extension_pct, 2),
                # --- Institutional Research Metadata (v2.5.0) ---
                'pattern_type': classify_pattern_type("LISTING_BREAKOUT" if signal_type == "BREAKOUT" else "CONSOLIDATION", days_since_listing, vol_ratio_for_tier, listing_range_pct),
                'market_regime': get_market_regime(current_date),
                # --- Tier fields ---
                'tier': tier,
                'position_size_pct': position_size_pct,
                'tier_rationale': tier_rationale,
                'perfect_base': perfect_base_ok,
                'post_confirm_move_pct': round(post_confirm_move_pct, 2),
                'base_range_high': round(base_range_high, 2) if base_range_high > 0 else None,
                # --- Analytics Tracking Fields ---
                'ipo_age': days_since_listing,
                'distance_from_listing_high_pct': round(((listing_day_high - current_price) / listing_day_high * 100) if listing_day_high > 0 else 0, 2),
                'consolidation_range_pct': round(consolidation_range_pct, 2) if consolidation_range_pct is not None else None,
                'volume_ratio': round(vol_ratio_for_tier, 2),
                'confirmation_time_min': confirmation_time_min,
                'max_extension_during_confirmation_pct': round(max_extension_during_confirmation_pct, 2),
                'rejection_depth_pct': round(rejection_depth_pct, 2),
                'did_hold_breakout_level': did_hold_breakout_level,
                'entry_vs_breakout_pct': round(entry_vs_breakout_pct, 2),
                'signal_strength_score': score_comps['total_score'],
                'score_components': score_comps,
                # --- Institutional Liquidity Hardening (Phase 2.5) ---
                'market_cap_cr': round(mcap_cr, 2),
                'avg_turnover_cr': round(avg_turnover_cr, 2),
                'circuit_days_15': int(circuit_days_15),
                # --- Enrichment Data (Institutional V2) ---
                '_candle': latest,
                '_history': df,
                '_base_candles': df,
            }

        
        # Log rejection reason if available
        if rejection_reason:
            logger.info(f"⏭️ {symbol}: Breakout rejected - {rejection_reason}")
        
        return None
    
    except Exception as e:
        logger.error(f"Error checking breakout for {symbol}: {e}")
        return None

def format_listing_breakout_alert(breakout_data):
    """Format listing day breakout alert"""
    symbol = breakout_data['symbol']
    entry = breakout_data['entry_price']
    stop = breakout_data['stop_loss']
    target = breakout_data['target_price']
    listing_high = breakout_data['listing_day_high']
    current_high = breakout_data['current_high']
    current_price = breakout_data['current_price']
    vol_spike = breakout_data['volume_spike']
    rr = breakout_data['risk_reward']
    conditions = breakout_data['breakout_conditions']
    days_since_listing = breakout_data.get('days_since_listing', 0)
    gain_from_listing = breakout_data.get('gain_from_listing_close', 0)
    price_source = breakout_data.get('price_source', 'Historical Close')
    entry_above_high_pct = breakout_data.get('entry_above_high_pct', 0)
    target_multiplier = breakout_data.get('target_multiplier', 0.5)
    volume_vs_listing_day = breakout_data.get('volume_vs_listing_day', 0)
    has_volume_caution = breakout_data.get('has_volume_caution', False)
    
    msg = f"""🎯 <b>LISTING DAY HIGH BREAKOUT!</b>

📊 <b>{symbol}</b>
📋 Signal Type: Listing Day Breakout

🏆 <b>TIER: {breakout_data.get('tier', 'A')}  |  💰 Position Size: {breakout_data.get('position_size_pct', 60)}%</b>
📌 <i>{breakout_data.get('tier_rationale', '')}</i>

⏰ <b>Context & Timing:</b>
• Age: {days_since_listing} days old
• Breakout Status: <b>{'✅ SUSTAINED' if breakout_data.get('is_sustained') else '❌ FAILED_TO_SUSTAIN'}</b>
• Pullback from High: {breakout_data.get('pullback_from_high_pct', 0):.1f}%
• Max Extension: {breakout_data.get('max_extension_pct', 0):+.1f}%
{'• <b>✅ Perfect Base Detected</b>' if breakout_data.get('perfect_base') else ''}

💰 <b>Trade Details:</b>
• Current Price: ₹{current_price:,.2f} <i>({price_source})</i>
• Entry Target: ₹{entry:,.2f}
• Stop Loss: ₹{stop:,.2f} (-{STOP_LOSS_PCT:.0f}%)
• Target Obj: ₹{target:,.2f}
• Risk:Reward: 1:{rr:.1f}

📈 <b>Metrics:</b>
• Listing Day High: ₹{listing_high:,.2f} (<b>BROKEN!</b>)
• Base High: ₹{breakout_data.get('base_range_high', listing_high):,.2f}

📊 <b>Confirmation:</b>
• Volume Spike: {vol_spike:.1f}x avg
• Vol vs Listing: {volume_vs_listing_day:.1f}x {'✅' if not has_volume_caution else '⚠️'}
• Pattern: <b>{breakout_data.get('pattern_type', 'N/A')}</b>
• Regime: <b>{breakout_data.get('market_regime', 'N/A')}</b>
• {conditions}"""

    if has_volume_caution:
        msg += f"""

⚠️ <b>VOLUME CAUTION:</b>
• Signal sent for tracking - volume filters disabled
• Review performance later to validate filters"""

    msg += f"""

⚡ <b>Action Required:</b> Consider entry based on tier size.

🤖 <i>Scanner v{SCANNER_VERSION} | {datetime.now().strftime('%H:%M IST')}</i>"""
    return msg


def format_base_breakout_alert(breakout_data):
    """Format Tier B base breakout alert (actionable trade, distinct from WATCHLIST)."""
    symbol = breakout_data['symbol']
    entry = breakout_data['entry_price']
    stop = breakout_data['stop_loss']
    target = breakout_data['target_price']
    listing_high = breakout_data['listing_day_high']
    current_price = breakout_data['current_price']
    vol_spike = breakout_data.get('volume_spike', 0)
    rr = breakout_data['risk_reward']
    days_since = breakout_data.get('days_since_listing', 0)
    base_range_high = breakout_data.get('base_range_high') or 0
    dist_below_high = ((listing_high - current_price) / listing_high * 100) if listing_high > 0 else 0
    post_move = breakout_data.get('post_confirm_move_pct', 0)
    position_size = breakout_data.get('position_size_pct', 40)
    tier_rationale = breakout_data.get('tier_rationale', '')
    price_source = breakout_data.get('price_source', 'Live')
    conditions = breakout_data.get('breakout_conditions', '')
    symbol = breakout_data['symbol']
    is_sustained = breakout_data.get('is_sustained', False)
    pullback = breakout_data.get('pullback_from_high_pct', 0)

    listing_date = breakout_data['listing_date']
    listing_date_str = (
        listing_date.strftime('%Y-%m-%d')
        if hasattr(listing_date, 'strftime') else str(listing_date)
    )

    return f"""📦 <b>TIER B — BASE BREAKOUT: {symbol}</b>

{'='*35}
🥉 <b>TIER: B  |  💰 Position Size: {position_size}%</b>
📌 {tier_rationale}
{'='*35}

🎯 <b>Setup:</b> Stock broke above its accumulation base while still
   <b>{dist_below_high:.1f}% below</b> listing high — next stop: listing high.

💰 <b>Trade Plan:</b>
• Entry: ₹{entry:,.2f}  ({price_source})
• Stop Loss: ₹{stop:,.2f}  ({STOP_LOSS_PCT:.0f}% below entry)
• Target: ₹{target:,.2f}  (Listing Day High = natural target)
• Risk:Reward: 1:{rr:.1f}
• Post-Breakout Move: {post_move:+.2f}% above base high

📈 <b>Context:</b>
• Base High Broken: ₹{base_range_high:,.2f}
• Listing Day High (target): ₹{listing_high:,.2f}
• Volume Spike: {vol_spike:.1f}x avg
• IPO Age: {days_since}d  |  Listed: {listing_date_str}
• Pattern: <b>{breakout_data.get('pattern_type', 'N/A')}</b>
• Regime: <b>{breakout_data.get('market_regime', 'N/A')}</b>
• Status: <b>{'✅ SUSTAINED' if is_sustained else f'❌ PULLED BACK ({pullback:.1f}%)'}</b>
• {conditions}

🤖 Scanner v{SCANNER_VERSION} | {datetime.now().strftime('%Y-%m-%d %H:%M IST')}
"""


def format_watchlist_alert(breakout_data):
    """Format watchlist alert for near-breakout candidates"""
    symbol = breakout_data['symbol']
    current_price = breakout_data['current_price']
    listing_high = breakout_data['listing_day_high']
    listing_date = breakout_data['listing_date']
    price_source = breakout_data.get('price_source', 'Live')
    days_since = breakout_data.get('days_since_listing', 0)
    vol_spike = breakout_data.get('volume_spike', 0)
    
    # Calculate distance to breakout
    distance_amt = listing_high - current_price
    distance_pct = (distance_amt / listing_high * 100)
    
    # Format listing date
    if hasattr(listing_date, 'strftime'):
        listing_date_str = listing_date.strftime('%Y-%m-%d')
    else:
        listing_date_str = str(listing_date)
    
    # Volume trend assessment
    vol_status = "Building Up 🟢" if vol_spike > 1.0 else "Normal 🟡"
    if vol_spike > 2.0:
        vol_status = "Very High 💥"
    
    msg = f"""👀 <b>WATCHLIST ALERT: {symbol}</b>
    
🚀 <b>Approaching Breakout Level!</b>
The stock is within <b>{distance_pct:.1f}%</b> of its Listing Day High.

📊 <b>Status:</b>
• Current Price: ₹{current_price:,.2f} ({price_source})
• Breakout Level: ₹{listing_high:,.2f}
• Distance: {distance_pct:.1f}% away

📉 <b>Volume Trend:</b>
• Volume: {vol_spike:.1f}x avg ({vol_status})
• Pre-breakout buildup detected

📅 <b>Listing Context:</b>
• Listed on: {listing_date_str}
• Age: {days_since} days old
• Pattern: <b>{breakout_data.get('pattern_type', 'N/A')}</b>
• Regime: <b>{breakout_data.get('market_regime', 'N/A')}</b>

💡 <b>Actionable Advice:</b>
Keep {symbol} on your radar. A close above ₹{listing_high:.2f} with volume triggers a valid entry.

🤖 Scanner v{SCANNER_VERSION} | {datetime.now().strftime('%Y-%m-%d %H:%M IST')}
"""
    return msg

def save_breakout_signal(breakout_data):
    """Save breakout signal to MongoDB (DB-only)."""
    try:
        now = datetime.now()
        today = now.date()
        signal_id = f"LISTING_{breakout_data['symbol']}_{today.strftime('%Y%m%d')}"
        try:
            from db import signal_exists, has_active_position
            if signal_exists(signal_id):
                logger.info(f"Listing day breakout signal already exists for {breakout_data['symbol']} today")
                return False
            if has_active_position(breakout_data['symbol']):
                logger.info(f"⏭️ Skipping {breakout_data['symbol']} - already has active position")
                return False
        except Exception:
            pass
        
        # Create new signal
        # Add note about volume caution if applicable
        volume_note = ""
        if breakout_data.get('has_volume_caution', False):
            volume_warnings = breakout_data.get('volume_warnings', [])
            volume_note = f"VOLUME_CAUTION: {'; '.join(volume_warnings)}"
        
        new_signal = {
            "signal_id": signal_id,
            "symbol": breakout_data['symbol'],
            "signal_date": now, # Use full datetime for MongoDB compatibility
            "signal_time": now.strftime("%H:%M:%S"),
            "entry_price": breakout_data['entry_price'],
            "grade": "LISTING_BREAKOUT" + ("_LOW_VOL" if breakout_data.get('has_volume_caution', False) else ""),
            "score": 100 if not breakout_data.get('has_volume_caution', False) else 80,
            "stop_loss": breakout_data['stop_loss'],
            "target_price": breakout_data['target_price'],
            "status": "ACTIVE",
            "exit_date": "",
            "exit_price": 0,
            "pnl_pct": 0,
            "days_held": 0,
            "signal_type": "LISTING_DAY_BREAKOUT",
            "notes": volume_note,
            "version": SCANNER_VERSION,
            "scanner": "listing_day",
            "leader_score": int(breakout_data.get("leader_score", 0)),
            # --- Tier fields (additive, backward-compatible) ---
            "tier": breakout_data.get("tier", ""),
            "position_size_pct": breakout_data.get("position_size_pct", ""),
            "tier_rationale": breakout_data.get("tier_rationale", ""),
            # --- Setup Quality & Behavioral Metrics ---
            "ipo_age": breakout_data.get("ipo_age", None),
            "distance_from_listing_high_pct": breakout_data.get("distance_from_listing_high_pct", None),
            "consolidation_range_pct": breakout_data.get("consolidation_range_pct", None),
            "volume_ratio": breakout_data.get("volume_ratio", None),
            "volume_vs_listing_day": breakout_data.get("volume_vs_listing_day", None),
            "risk_reward_ratio": breakout_data.get("risk_reward", None),
            "confirmation_time_min": breakout_data.get("confirmation_time_min", None),
            "max_extension_during_confirmation_pct": breakout_data.get("max_extension_during_confirmation_pct", None),
            "rejection_depth_pct": breakout_data.get("rejection_depth_pct", None),
            "post_confirm_move_pct": breakout_data.get("post_confirm_move_pct", None),
            "did_hold_breakout_level": breakout_data.get("did_hold_breakout_level", True),
            "entry_vs_breakout_pct": breakout_data.get("entry_vs_breakout_pct", None),
            "signal_strength_score": breakout_data.get("signal_strength_score", None),
            # --- Score Components ---
            "tier_weight": breakout_data.get("score_components", {}).get("tier_weight", None),
            "volume_score": breakout_data.get("score_components", {}).get("volume_score", None),
            "base_score": breakout_data.get("score_components", {}).get("base_score", None),
            "momentum_score": breakout_data.get("score_components", {}).get("momentum_score", None),
        }
        
        # Write to daily log — pass breakout candle time for market-correct deduplication
        write_daily_log("listing_day", breakout_data['symbol'], "BREAKOUT_SIGNAL", {
            "entry": breakout_data['entry_price'],
            "stop_loss": breakout_data['stop_loss'],
            "target": breakout_data['target_price'],
            "listing_high": breakout_data.get('listing_day_high', 0),
            "volume_caution": breakout_data.get('has_volume_caution', False),
            "tier": breakout_data.get('tier', ''),
            "position_size_pct": breakout_data.get('position_size_pct', ''),
            "tier_rationale": breakout_data.get('tier_rationale', ''),
            "perfect_base": breakout_data.get('perfect_base', False),
            "post_confirm_move_pct": breakout_data.get('post_confirm_move_pct', 0),
            "entry_vs_breakout_pct": breakout_data.get("entry_vs_breakout_pct", None),
            "held_above_breakout_after_confirm": breakout_data.get("did_hold_breakout_level", True),
            "signal_strength_score": breakout_data.get("signal_strength_score", None),
            "tier_weight": breakout_data.get("score_components", {}).get("tier_weight", None),
            "volume_score": breakout_data.get("score_components", {}).get("volume_score", None),
            "base_score": breakout_data.get("score_components", {}).get("base_score", None),
            "momentum_score": breakout_data.get("score_components", {}).get("momentum_score", None),
        }, candle_timestamp=breakout_data.get('candle_timestamp') or breakout_data.get('timestamp'))
        
        # --- NEW: V2 Institutional Telemetry Integration ---
        try:
            IntegrationBridge = getattr(scanner_module, 'IntegrationBridge', None)
            if IntegrationBridge:
                bridge = IntegrationBridge()
                # Transform data for SignalBuilder
                enriched_payload = breakout_data.copy()
                enriched_payload['scanner'] = 'listing_day'
                enriched_payload['entry_price'] = breakout_data.get('entry_price')
                enriched_payload['stop_loss'] = breakout_data.get('stop_loss')
                enriched_payload['target_price'] = breakout_data.get('target_price')
                enriched_payload['candle_timestamp'] = breakout_data.get('candle_timestamp') or datetime.now()
                
                success_v2 = bridge.save_signal(enriched_payload)
                if success_v2:
                    logger.info(f"🏛️  V2 Institutional Snapshot saved for {breakout_data['symbol']}")
        except Exception as v2_e:
            logger.warning(f"[Telemetry] V2 bridge failed (falling back to V1 only): {v2_e}")

        # DB-only write: signal (V1 Legacy Collection)
        try:
            from db import upsert_signal
            upsert_signal(new_signal.copy())
        except Exception as db_e:
            logger.error(f"[MongoDB] signal write FAILED for {signal_id}: {db_e}")
            try:
                from db import db_metrics
                db_metrics["failures"] = db_metrics.get("failures", 0) + 1
            except Exception:
                pass
        
        logger.info(f"✅ Saved legacy signal for {breakout_data['symbol']}")
        return True
    
    except Exception as e:
        logger.error(f"Error saving signal: {e}")
        return False

def save_watchlist_signal(breakout_data):
    """Save watchlist signal to prevent duplicate alerts"""
    try:
        today = datetime.now().date()
        # Use WATCHLIST prefix to distinguish from actual breakouts
        signal_id = f"WATCHLIST_{breakout_data['symbol']}_{today.strftime('%Y%m%d')}"
        
        # Check if signal already exists
        try:
            from db import signal_exists
            if signal_exists(signal_id):
                logger.info(f"Watchlist alert already sent for {breakout_data['symbol']} today")
                return False
        except Exception:
            pass
            
        distance_pct = ((breakout_data['listing_day_high'] - breakout_data['current_price']) / breakout_data['listing_day_high'] * 100) if breakout_data['listing_day_high'] > 0 else 0

        # Create new signal record (watchlist-only storage)
        new_signal = {
            "signal_id": signal_id,
            "symbol": breakout_data['symbol'],
            "signal_date": today,
            "signal_time": _now_ist().strftime("%H:%M:%S"),
            "status": "WATCH",
            "watch_level": breakout_data['listing_day_high'],
            "current_price": breakout_data['current_price'],
            "distance_pct": round(distance_pct, 2),
            "notes": "Within 5% of listing high",
            "version": SCANNER_VERSION,
            "scanner": "listing_day"
        }
        
        # DB-only write: signal
        try:
            from db import upsert_signal
            upsert_signal(new_signal.copy())
        except Exception as db_e:
            logger.error(f"[MongoDB] watchlist signal write FAILED for {signal_id}: {db_e}")
            try:
                from db import db_metrics
                db_metrics["failures"] = db_metrics.get("failures", 0) + 1
            except Exception:
                pass

        logger.info(f"✅ Saved watchlist signal for {breakout_data['symbol']}")
        return True
        
    except Exception as e:
        logger.error(f"Error saving watchlist signal: {e}")
        return False

def add_position(breakout_data):
    """Add position to MongoDB (DB-only)."""
    try:
        today = datetime.now().date()
        try:
            from db import has_active_position
            if has_active_position(breakout_data['symbol']):
                logger.info(f"⏭️ Position already exists for {breakout_data['symbol']}")
                return False
        except Exception:
            pass
        
        # Create new position
        new_position = {
            "symbol": breakout_data['symbol'],
            "entry_date": today,
            "entry_price": breakout_data['entry_price'],
            "grade": "LISTING_BREAKOUT",
            "current_price": breakout_data['entry_price'],
            "stop_loss": breakout_data['stop_loss'],
            "trailing_stop": breakout_data['stop_loss'],
            "pnl_pct": 0,
            "days_held": 0,
            "status": "ACTIVE"
        }
        
        # DB-only write: position
        try:
            from db import upsert_position
            upsert_position(new_position.copy())
        except Exception as db_e:
            logger.error(f"[MongoDB] position write FAILED for {breakout_data['symbol']}: {db_e}")
            try:
                from db import db_metrics
                db_metrics["failures"] = db_metrics.get("failures", 0) + 1
            except Exception:
                pass
        
        logger.info(f"✅ Added position for {breakout_data['symbol']}")
        return True
    
    except Exception as e:
        logger.error(f"Error adding position: {e}")
        return False

def update_listing_status(symbol, status):
    """Update status of listing data entry"""
    try:
        listing_data = load_listing_data()
        if not listing_data.empty and symbol in listing_data['symbol'].tolist():
            listing_data.loc[listing_data['symbol'] == symbol, 'status'] = status
            listing_data.loc[listing_data['symbol'] == symbol, 'last_updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_listing_data(listing_data)
    except Exception as e:
        logger.error(f"Error updating listing status: {e}")

def scan_listing_day_breakouts():
    """Main function to scan for listing day breakouts"""
    logger.info("🚀 Starting Listing Day Breakout Scan...")
    if LISTING_STRICT_QUALITY:
        logger.info(
            f"⚙️ Quality: STRICT — full volume + freshness, min R:R {MIN_RISK_REWARD}, "
            f"max {MAX_ENTRY_ABOVE_HIGH_PCT}% above listing high, breakout max {MAX_DAYS_SINCE_LISTING_FOR_BREAKOUT}d since listing; "
            f"watchlist age ≤{LISTING_WATCHLIST_MAX_DAYS_SINCE_LISTING}d (abs ≤{LISTING_WATCHLIST_ABSOLUTE_MAX_AGE_DAYS}d), "
            f"vol vs avg: min {LISTING_WATCHLIST_MIN_VOLUME_MULT or '—'}× / max {LISTING_WATCHLIST_MAX_VOL_VS_AVG or '—'}×, "
            f"perfect-base {LISTING_WATCHLIST_BASE_LOOKBACK}d: range≤{LISTING_WATCHLIST_MAX_BASE_RANGE_PCT}% "
            f"(auto-reject ≥{LISTING_WATCHLIST_AUTO_REJECT_WIDE_RANGE_PCT}%), "
            f"dist {LISTING_WATCHLIST_MIN_DISTANCE_FROM_HIGH_PCT or '—'}–{LISTING_WATCHLIST_MAX_DISTANCE_FROM_HIGH_PCT}%, "
            f"vol dry-up ≤{LISTING_WATCHLIST_VOL_DRYUP_MAX_RATIO}, "
            f"watchlist leader ≥{LISTING_WATCHLIST_MIN_LEADER_SCORE}"
        )
    else:
        logger.info("⚙️ Quality: RELAXED — low-vol breakouts may be sent with caution")
    logger.info("=" * 60)
    
    # Step 0: Prep MongoDB (Legacy CSV initialization removed)
    
    # Update listing data for new IPOs
    logger.info("📊 Step 1: Updating listing data for new IPOs...")
    update_listing_data_for_new_ipos()
    
    # Load active listings
    logger.info("\n📊 Step 2: Scanning for breakouts...")
    listing_data = load_listing_data()
    
    if listing_data.empty:
        logger.warning("No listing data available")
        return
    
    # Filter for active listings
    active_listings = listing_data[listing_data['status'] == 'ACTIVE']
    
    if active_listings.empty:
        logger.info("No active listings to monitor")
        return
    
    logger.info(f"📋 Monitoring {len(active_listings)} active listings...")
    
    breakouts_found = 0
    pending_breakouts = load_pending_breakouts()
    
    for idx, listing_info in active_listings.iterrows():
        symbol = listing_info['symbol']
        logger.info(f"\n🔍 Checking {symbol}...")
        
        try:
            # Check for breakout
            breakout = check_listing_day_breakout(symbol, listing_info, pending_breakouts)
            
            if breakout:
                signal_type = breakout.get('type', 'BREAKOUT')
                
                if signal_type == 'BREAKOUT':
                    logger.info(f"🎯 BREAKOUT DETECTED for {symbol}! [Tier {breakout.get('tier', '?')} | {breakout.get('position_size_pct', '?')}% size]")
                    logger.info(f"   Entry: ₹{breakout['entry_price']:.2f}")
                    logger.info(f"   Stop Loss: ₹{breakout['stop_loss']:.2f}")
                    logger.info(f"   Target: ₹{breakout['target_price']:.2f}")
                    logger.info(f"   Tier rationale: {breakout.get('tier_rationale', '')}")

                    # Save signal
                    if save_breakout_signal(breakout):
                        # Add position
                        add_position(breakout)

                        # Update listing status
                        update_listing_status(symbol, 'BREAKOUT')

                        # Send alert
                        alert_msg = format_listing_breakout_alert(breakout)
                        send_telegram(alert_msg)

                        breakouts_found += 1

                elif signal_type == 'BASE_BREAKOUT':
                    logger.info(f"📦 TIER B BASE BREAKOUT for {symbol}! [{breakout.get('position_size_pct', 40)}% size]")
                    logger.info(f"   Entry: ₹{breakout['entry_price']:.2f}  Target (listing high): ₹{breakout['target_price']:.2f}")

                    if save_breakout_signal(breakout):
                        add_position(breakout)
                        update_listing_status(symbol, 'BASE_BREAKOUT')
                        alert_msg = format_base_breakout_alert(breakout)
                        send_telegram(alert_msg)
                        breakouts_found += 1

                elif signal_type == 'WATCHLIST':
                    # Save watchlist signal (returns False if duplicate)
                    if save_watchlist_signal(breakout):
                        logger.info(f"👀 Sending WATCHLIST alert for {symbol}")
                        alert_msg = format_watchlist_alert(breakout)
                        send_telegram(alert_msg)
                elif signal_type == 'PENDING':
                    logger.info(f"⏳ {symbol}: pending confirmation in progress")

                
                # Small delay
                time.sleep(0.5)
            else:
                # Breakout was checked but not confirmed - rejection reason already logged in check_listing_day_breakout
                pass
            
            # Rate limiting
            time.sleep(0.3)
        
        except Exception as e:
            logger.error(f"Error scanning {symbol}: {e}")
            continue
    
    logger.info(f"\n{'='*60}")
    logger.info(f"✅ Scan complete: {breakouts_found} breakouts found")
    save_pending_breakouts(pending_breakouts)
    
    try:
        from db import db_metrics
        db_stats = {
            "listings_monitored": len(active_listings),
            "signals_found": breakouts_found,
            "db_signals": db_metrics.get("signals_generated", 0),
            "db_logs": db_metrics.get("logs_written", 0),
            "db_failures": db_metrics.get("failures", 0)
        }
    except Exception:
        db_stats = {"listings_monitored": len(active_listings), "signals_found": breakouts_found}

    write_daily_log("listing_day", "SYSTEM", "SCAN_COMPLETED", db_stats)
    
    # Send summary
    if breakouts_found > 0:
        summary = f"""📊 <b>Listing Day Breakout Scan Summary</b>

🔍 Listings Monitored: {len(active_listings)}
🎯 Breakouts Found: {breakouts_found}
⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🧯 DB Status: {'✅ OK' if db_stats.get('db_failures', 0) == 0 else f"❌ {db_stats.get('db_failures')} FAILURES"}

{'🎉 New breakouts detected! Check alerts above.' if breakouts_found > 0 else '✅ No breakouts at this time.'}"""
        send_telegram(summary)

def main():
    """Main function"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--bypass-holiday", action="store_true", help="Run scanner even if it's a holiday/weekend")
    args = parser.parse_args()

    try:
        print("IPO Listing Day Breakout Scanner")
        print("=" * 60)
        print(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
        print(f"Time: {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 60)
    except:
        print("IPO Listing Day Breakout Scanner")
        print("=" * 60)

    # --- Market Holiday Guard ---
    if not is_market_day() and not args.bypass_holiday:
        from datetime import timezone, timedelta as td
        ist = timezone(td(hours=5, minutes=30))
        today_ist = datetime.now(ist).strftime("%Y-%m-%d")
        skip_msg = (
            f"📅 <b>Listing Day: Market Holiday</b>\n\n"
            f"🗓 Date: {today_ist}\n"
            f"⏭ Scanner skipped — NSE is closed today.\n"
            f"✅ Use --bypass-holiday to force run."
        )
        logger.info(f"📅 Market is closed today ({today_ist}). Skipping listing day scan.")
        send_telegram(skip_msg)
        sys.exit(0)

    scan_listing_day_breakouts()

if __name__ == "__main__":
    main()

