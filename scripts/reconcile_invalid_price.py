#!/usr/bin/env python3
"""
scripts/reconcile_invalid_price.py

Historical stop-loss replay for positions frozen in INVALID_PRICE status.

Logic per position:
  1. Fetch all candles from last_valid_date+1 → today
  2. Replay production trailing-stop logic day-by-day
     (grade-based %, same MIN_PNL_FOR_TRAIL / MIN_TRAIL_MOVE_PCT thresholds)
  3. Stop-breach check uses candle LOW (intraday reality)
     Trailing-stop update uses candle CLOSE (matches production EOD logic)
  4. If breach found  → close historically (BACKFILLED_STOP_HIT)
  5. If no breach     → revert to ACTIVE

Safe by design:
  - dry_run=True (default) — prints every decision, writes NOTHING
  - No Telegram alerts — never
  - outcome_source = "BACKFILLED" on all reconstructed exits
  - Idempotent: safe to re-run (upsert on symbol)

Usage:
    python scripts/reconcile_invalid_price.py            # dry-run preview
    python scripts/reconcile_invalid_price.py --execute  # write to MongoDB
"""

import sys
import os
import argparse
import logging
from datetime import datetime, timezone, timedelta, date

import pandas as pd

# ── Bootstrap path so we can import from project root ────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import positions_col, signals_col, upsert_position, insert_log, SCANNER_VERSION
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reconcile_invalid_price")

# ── Mirror production constants (must match streamlined_ipo_scanner.py) ───────
MIN_PNL_FOR_TRAIL = float(os.getenv("MIN_PNL_FOR_TRAIL", 5.0))
MIN_TRAIL_MOVE_PCT = float(os.getenv("MIN_TRAIL_MOVE_PCT", 1.0))

GRADE_STOP_PCTS = {
    "A+": 0.05,
    "A": 0.07,
    "B": 0.10,
    "C": 0.12,
    "D": 0.15,
    "LISTING_BREAKOUT": 0.10,  # explicit — mirrors production fix
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _grade_stop_pct(grade: str) -> float:
    """Return the trailing-stop percentage for a grade. Mirrors production logic."""
    return GRADE_STOP_PCTS.get(grade, 0.10)


def _classify_outcome(pnl: float, max_runup: float, max_drawdown: float, days: int) -> str:
    """Exact copy of production outcome classification."""
    if max_runup > 10.0 and days <= 5:
        return "FAST_WINNER"
    if max_runup > 10.0 and days > 5:
        return "SLOW_WINNER"
    if max_runup <= 3.0 and max_drawdown <= -3.0:
        return "FAILED_BREAKOUT"
    if max_runup < 1.0 and pnl < 0:
        return "IMMEDIATE_FAILURE"
    if max_runup > 3.0 and max_runup <= 8.0:
        return "NO_FOLLOW_THROUGH"
    return "NO_FOLLOW_THROUGH"


def _to_date(val) -> date:
    """Coerce any date-like value to a plain date object."""
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, pd.Timestamp):
        return val.date()
    if isinstance(val, str):
        return pd.to_datetime(val).date()
    raise TypeError(f"Cannot convert {type(val)} to date: {val!r}")


def fetch_candles(symbol: str, from_date: date) -> pd.DataFrame | None:
    """
    Fetch OHLCV candles for symbol from from_date to today.
    Mirrors production fetch_data(): Upstox → yfinance fallback.
    Returns DataFrame with columns: DATE, OPEN, HIGH, LOW, CLOSE, VOLUME
    or None on failure.
    """
    today = datetime.today().date()
    logger.info(f"  [{symbol}] Fetching candles {from_date} → {today}")

    # ── Upstox ────────────────────────────────────────────────────────────────
    try:
        from utils import fetch_from_upstox  # type: ignore
        df = fetch_from_upstox(symbol, from_date, today)
        if df is not None and not df.empty:
            logger.info(f"  [{symbol}] Upstox: {len(df)} candles")
            return df
    except Exception as e:
        logger.debug(f"  [{symbol}] Upstox failed: {e}")

    # ── yfinance fallback ─────────────────────────────────────────────────────
    try:
        import yfinance as yf
        df_yf = yf.download(
            f"{symbol}.NS",
            start=from_date,
            end=today + timedelta(days=1),
            progress=False,
            auto_adjust=False,
        )
        if df_yf is None or df_yf.empty:
            logger.warning(f"  [{symbol}] yfinance returned no data")
            return None

        # Normalise MultiIndex columns (yfinance ≥ 0.2 returns ticker-level multi-index)
        df_yf.columns = [
            c[0] if isinstance(c, tuple) else c for c in df_yf.columns
        ]
        df_yf = df_yf.rename(columns=str.capitalize)
        df_yf = df_yf.reset_index()

        # Align column names to production schema
        col_map = {"Date": "DATE", "Open": "OPEN", "High": "HIGH",
                   "Low": "LOW", "Close": "CLOSE", "Volume": "VOLUME"}
        df_yf = df_yf.rename(columns=col_map)

        needed = [c for c in ["DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"] if c in df_yf.columns]
        df_yf = df_yf[needed].copy()
        df_yf["DATE"] = pd.to_datetime(df_yf["DATE"])

        logger.info(f"  [{symbol}] yfinance: {len(df_yf)} candles")
        return df_yf
    except Exception as e:
        logger.error(f"  [{symbol}] yfinance failed: {e}")
        return None


# ── Core replay ───────────────────────────────────────────────────────────────

def replay_position(pos: dict, candles: pd.DataFrame) -> dict:
    """
    Walk every candle in chronological order and replay production stop-loss logic.

    Uses:
      - candle LOW  for stop-breach detection  (intraday reality)
      - candle CLOSE for trailing-stop updates  (mirrors EOD production logic)

    Returns a result dict:
        outcome      : "STOP_HIT" | "TIME_STOP" | "STILL_ACTIVE"
        exit_date    : date | None
        exit_price   : float | None
        exit_reason  : str | None
        final_trailing_stop : float
        final_pnl    : float
        max_runup    : float
        max_drawdown : float
        days_held    : int
        outcome_type : str | None
        replay_log   : list[dict]   (day-by-day audit trail)
    """
    symbol      = pos["symbol"]
    entry_price = float(pos["entry_price"])
    grade       = pos.get("grade", "LISTING_BREAKOUT")
    entry_date  = _to_date(pos["entry_date"])

    # Seed state from the last valid snapshot
    trailing_stop = float(pos.get("trailing_stop") or pos.get("stop_loss"))
    max_runup     = float(pos.get("max_runup_pct",  0.0) or 0.0)
    max_drawdown  = float(pos.get("max_drawdown_pct", 0.0) or 0.0)

    stop_pct = _grade_stop_pct(grade)

    replay_log = []
    today_date = datetime.today().date()

    # Sort candles chronologically (defensive)
    candles = candles.sort_values("DATE").reset_index(drop=True)

    for _, row in candles.iterrows():
        candle_date  = _to_date(row["DATE"])
        candle_close = float(row["CLOSE"])
        candle_low   = float(row["LOW"])
        days_held    = (candle_date - entry_date).days

        pnl          = (candle_close - entry_price) / entry_price * 100.0
        max_runup    = max(max_runup,  pnl)
        max_drawdown = min(max_drawdown, pnl)

        old_trailing = trailing_stop  # used for exit check

        # ── 1. Trailing stop update (use CLOSE — mirrors production) ──────────
        if pnl >= MIN_PNL_FOR_TRAIL:
            candidate  = candle_close * (1.0 - stop_pct)
            min_move   = entry_price * (MIN_TRAIL_MOVE_PCT / 100.0)
            if candidate > trailing_stop and (candidate - trailing_stop) >= min_move:
                trailing_stop = candidate

        # ── 2. Stop-breach check (use LOW — intraday reality) ─────────────────
        exit_reason = None
        if candle_low <= old_trailing:
            # Stop hit: exit price is the trailing stop (worst realistic fill)
            # In practice a gap-down open could be worse, but we don't have
            # intraday data; using old_trailing is the conservative choice.
            exit_price = min(candle_close, old_trailing)
            exit_reason = "BACKFILLED_STOP_HIT"

        # ── 3. Time-based stop (mirrors production, winner archetype guard) ───
        if exit_reason is None:
            is_winner = max_runup >= 15.0
            if not is_winner:
                if days_held > 30 and candle_close < entry_price * 0.95:
                    exit_reason = "BACKFILLED_TIME_STOP_5PCT"
                    exit_price  = candle_close
                elif days_held > 60 and candle_close < entry_price * 0.92:
                    exit_reason = "BACKFILLED_TIME_STOP_8PCT"
                    exit_price  = candle_close

        log_entry = {
            "date":         candle_date.isoformat(),
            "close":        round(candle_close, 4),
            "low":          round(candle_low, 4),
            "pnl":          round(pnl, 3),
            "trailing":     round(trailing_stop, 4),
            "max_runup":    round(max_runup, 3),
            "max_drawdown": round(max_drawdown, 3),
            "exit_reason":  exit_reason,
        }
        replay_log.append(log_entry)

        if exit_reason:
            final_pnl    = (exit_price - entry_price) / entry_price * 100.0
            outcome_type = _classify_outcome(final_pnl, max_runup, max_drawdown, days_held)
            return {
                "outcome":               "STOP_HIT" if "STOP_HIT" in exit_reason else "TIME_STOP",
                "exit_date":             candle_date,
                "exit_price":            round(exit_price, 4),
                "exit_reason":           exit_reason,
                "final_trailing_stop":   round(trailing_stop, 4),
                "final_pnl":             round(final_pnl, 3),
                "max_runup":             round(max_runup, 3),
                "max_drawdown":          round(max_drawdown, 3),
                "days_held":             days_held,
                "outcome_type":          outcome_type,
                "replay_log":            replay_log,
            }

    # No breach across all candles → still alive
    final_pnl    = (candle_close - entry_price) / entry_price * 100.0
    days_held    = (today_date - entry_date).days
    return {
        "outcome":               "STILL_ACTIVE",
        "exit_date":             None,
        "exit_price":            None,
        "exit_reason":           None,
        "final_trailing_stop":   round(trailing_stop, 4),
        "final_pnl":             round(final_pnl, 3),
        "max_runup":             round(max_runup, 3),
        "max_drawdown":          round(max_drawdown, 3),
        "days_held":             days_held,
        "outcome_type":          None,
        "replay_log":            replay_log,
    }


# ── Persistence helpers ───────────────────────────────────────────────────────

def _persist_closed(pos: dict, result: dict, dry_run: bool):
    """Write a backfilled closed position to positions_col."""
    symbol = pos["symbol"]
    doc = {
        "symbol":                 symbol,
        "status":                 "CLOSED",
        "exit_date":              result["exit_date"].isoformat(),
        "exit_price":             result["exit_price"],
        "exit_reason":            result["exit_reason"],
        "pnl_pct":                result["final_pnl"],
        "days_held":              result["days_held"],
        "max_runup_pct":          result["max_runup"],
        "max_drawdown_pct":       result["max_drawdown"],
        "trailing_stop":          result["final_trailing_stop"],
        "outcome_type":           result["outcome_type"],
        "outcome_source":         "BACKFILLED",
        "reconciliation_version": SCANNER_VERSION,
        "updated_at":             datetime.now(timezone.utc),
        # Preserve original entry data
        "entry_price":            float(pos["entry_price"]),
        "entry_date":             pos["entry_date"],
        "grade":                  pos.get("grade"),
        "stop_loss":              float(pos.get("stop_loss", 0)),
    }
    logger.info(
        f"  [{symbol}] → CLOSE | exit={result['exit_date']} "
        f"reason={result['exit_reason']} pnl={result['final_pnl']:.2f}% "
        f"outcome={result['outcome_type']}"
    )
    if not dry_run:
        upsert_position(doc)
        # Also close the matching signal
        if signals_col is not None:
            signals_col.update_many(
                {"symbol": symbol, "$or": [
                    {"status": "ACTIVE"},
                    {"lifecycle_state": "POSITION_ACTIVE"},
                ]},
                {"$set": {
                    "status":                 "CLOSED",
                    "lifecycle_state":        "CLOSED",
                    "exit_date":              datetime.combine(result["exit_date"],
                                                               datetime.min.time()).replace(tzinfo=timezone.utc),
                    "exit_price":             result["exit_price"],
                    "pnl_pct":                result["final_pnl"],
                    "days_held":              result["days_held"],
                    "exit_reason":            result["exit_reason"],
                    "outcome_type":           result["outcome_type"],
                    "outcome_source":         "BACKFILLED",
                    "updated_at":             datetime.now(timezone.utc),
                }}
            )


def _persist_reactivated(pos: dict, result: dict, dry_run: bool):
    """Revert INVALID_PRICE → ACTIVE with refreshed trailing stop and metrics."""
    symbol = pos["symbol"]
    doc = {
        "symbol":                 symbol,
        "status":                 "ACTIVE",
        "trailing_stop":          result["final_trailing_stop"],
        "pnl_pct":                result["final_pnl"],
        "days_held":              result["days_held"],
        "max_runup_pct":          result["max_runup"],
        "max_drawdown_pct":       result["max_drawdown"],
        "reconciliation_version": SCANNER_VERSION,
        "updated_at":             datetime.now(timezone.utc),
    }
    logger.info(
        f"  [{symbol}] → REACTIVATE | trailing={result['final_trailing_stop']:.4f} "
        f"pnl={result['final_pnl']:.2f}% days={result['days_held']}"
    )
    if not dry_run:
        upsert_position(doc)


def _log_reconciliation(pos: dict, result: dict, dry_run: bool):
    """Insert a structured reconciliation audit entry into logs_col."""
    if dry_run:
        return
    symbol = pos["symbol"]
    details = {
        "outcome":               result["outcome"],
        "exit_reason":           result["exit_reason"],
        "exit_date":             result["exit_date"].isoformat() if result["exit_date"] else None,
        "exit_price":            result["exit_price"],
        "final_pnl":             result["final_pnl"],
        "max_runup":             result["max_runup"],
        "max_drawdown":          result["max_drawdown"],
        "days_held":             result["days_held"],
        "outcome_type":          result["outcome_type"],
        "outcome_source":        "BACKFILLED",
        "candles_replayed":      len(result["replay_log"]),
        "replay_summary":        result["replay_log"][-5:],  # last 5 days for audit trail
    }
    insert_log(
        scanner="reconcile_invalid_price",
        symbol=symbol,
        action="INVALID_PRICE_RECONCILED",
        candle_timestamp=datetime.now(timezone.utc),
        details=details,
        version=SCANNER_VERSION,
        source="migration",
        log_type="ACCEPTED",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = True):
    if dry_run:
        logger.info("=" * 60)
        logger.info("DRY-RUN MODE — no writes will be made")
        logger.info("=" * 60)
    else:
        logger.info("=" * 60)
        logger.info("EXECUTE MODE — changes WILL be written to MongoDB")
        logger.info("=" * 60)

    stuck = list(positions_col.find({"status": "INVALID_PRICE"}))
    if not stuck:
        logger.info("No INVALID_PRICE positions found. Nothing to do.")
        return

    logger.info(f"Found {len(stuck)} INVALID_PRICE positions: "
                f"{[p['symbol'] for p in stuck]}")

    for pos in stuck:
        symbol = pos["symbol"]
        logger.info(f"\n{'─'*55}")
        logger.info(f"Processing: {symbol}")
        logger.info(
            f"  Entry: ₹{pos['entry_price']} | "
            f"Grade: {pos.get('grade')} | "
            f"Frozen trailing: ₹{pos.get('trailing_stop')}"
        )

        # Determine replay start: day AFTER the last known valid update
        last_update = pos.get("updated_at") or pos.get("created_at")
        if last_update is None:
            logger.error(f"  [{symbol}] Cannot determine last_update date — skipping")
            continue

        # The position was updated by stop_loss_update_scan on May 13.
        # Start replay from May 14 (the day after).
        last_update_date = _to_date(last_update)
        replay_from      = last_update_date + timedelta(days=1)
        today            = datetime.today().date()

        if replay_from > today:
            logger.info(f"  [{symbol}] Last update was today — no candles to replay")
            continue

        # ── Fetch candles ──────────────────────────────────────────────────────
        candles = fetch_candles(symbol, replay_from)
        if candles is None or candles.empty:
            logger.error(
                f"  [{symbol}] Could not fetch candles for replay period "
                f"{replay_from} → {today}. Skipping to preserve integrity."
            )
            continue

        logger.info(f"  [{symbol}] Replaying {len(candles)} candles "
                    f"({replay_from} → {_to_date(candles['DATE'].iloc[-1])})")

        # ── Replay ────────────────────────────────────────────────────────────
        result = replay_position(pos, candles)

        # ── Print day-by-day audit trail ──────────────────────────────────────
        logger.info(f"  [{symbol}] Replay log ({len(result['replay_log'])} days):")
        for day in result["replay_log"]:
            flag = " ← EXIT" if day["exit_reason"] else ""
            logger.info(
                f"    {day['date']}  close={day['close']:.4f}  low={day['low']:.4f}  "
                f"trail={day['trailing']:.4f}  pnl={day['pnl']:+.2f}%"
                f"  runup={day['max_runup']:+.2f}%{flag}"
            )

        # ── Decision & persistence ────────────────────────────────────────────
        logger.info(f"  [{symbol}] Outcome → {result['outcome']}")

        if result["outcome"] in ("STOP_HIT", "TIME_STOP"):
            _persist_closed(pos, result, dry_run)
        else:
            # STILL_ACTIVE: safe to reactivate — no breach found
            _persist_reactivated(pos, result, dry_run)

        _log_reconciliation(pos, result, dry_run)

    logger.info(f"\n{'='*60}")
    logger.info("Reconciliation complete." if not dry_run else
                "Dry-run complete. Run with --execute to apply changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reconcile INVALID_PRICE positions via historical stop-loss replay"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Write results to MongoDB. Default is dry-run (read-only preview).",
    )
    args = parser.parse_args()
    run(dry_run=not args.execute)
