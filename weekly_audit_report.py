#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weekly_audit_report.py

Weekly system health audit for the IPO Base Scanner.

Run this every week (or on-demand) to verify:
  1. DB Integrity     — positions vs signals are in sync
  2. Data Quality     — no orphaned, corrupt, or contradictory records
  3. Performance Gate — are active positions tracking toward the 1-2 month expectancy goal?
  4. Logic Integrity  — stop-loss math, trailing stop direction, shadow SL consistency
  5. Regime Coverage  — market_regime field populated correctly
  6. Exit Integrity   — closed trades have valid exit fields
  7. Signal Pipeline  — signals being generated and converted to positions at expected rates
  8. Stale Data Guard — positions lacking recent price updates

Usage:
    python weekly_audit_report.py                  # Full audit, print + save report
    python weekly_audit_report.py --save-only      # Suppress console, save to file only
    python weekly_audit_report.py --section db     # Run only the DB integrity section
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone, date

# Force UTF-8 on Windows consoles so emoji characters do not crash the script
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from collections import defaultdict

import pandas as pd

# ─── Constants ────────────────────────────────────────────────────────────────

REPORT_DIR = os.path.join(os.path.dirname(__file__), "audit_reports")
MAX_DAYS_WITHOUT_PRICE_UPDATE = 3   # Flag positions not updated in N trading days
MIN_WIN_RATE_GOAL             = 0.25 # 25% closed win rate (soft goal, forward test)
EXPECTED_EXPECTANCY_PCT       = 0.0  # >= 0% realized avg PnL on closed trades
MAX_STOP_VIOLATION_PCT        = 5.0  # Flag if current_price is more than 5% below stop
MAX_ORPHANED_SIGNALS          = 5    # Max allowed signals with no matching position
SHADOW_SL_LEVELS              = [0.08, 0.10, 0.12]

# Detect CI / GitHub Actions environment.
# When True: skip txt file, save to MongoDB, send Telegram.
# When False (local): save txt file, skip MongoDB, no Telegram.
IS_CI = os.getenv("GITHUB_ACTIONS") == "true"

# ─── Report State ─────────────────────────────────────────────────────────────

findings = []     # List[dict] — individual findings
errors   = []     # List[str]  — hard errors / critical issues
warnings = []     # List[str]  — soft warnings

def _find(level: str, section: str, message: str, detail: dict = None):
    """Record a finding."""
    findings.append({
        "level":   level,      # INFO | WARN | ERROR
        "section": section,
        "message": message,
        "detail":  detail or {}
    })
    if level == "ERROR":
        errors.append(f"[{section}] {message}")
    elif level == "WARN":
        warnings.append(f"[{section}] {message}")

def _ok(section: str, message: str):
    _find("INFO", section, f"✅ {message}")

def _warn(section: str, message: str, detail: dict = None):
    _find("WARN", section, f"⚠️  {message}", detail)

def _err(section: str, message: str, detail: dict = None):
    _find("ERROR", section, f"❌ {message}", detail)

# ─── DB Connection ────────────────────────────────────────────────────────────

def _load_db():
    """Load DB module; return (positions_col, signals_col, logs_col, system_audits_col) or raise."""
    try:
        from db import positions_col, signals_col, logs_col, system_audits_col
        if positions_col is None or signals_col is None:
            raise RuntimeError("DB collections are None — check MONGO_URI in .env")
        return positions_col, signals_col, logs_col, system_audits_col
    except ImportError as e:
        raise RuntimeError(f"Cannot import db.py: {e}") from e


# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram_alert(msg: str):
    """
    Send an HTML-formatted Telegram message.
    Only fires in CI (GITHUB_ACTIONS=true) — bypassed silently on local runs.
    """
    if not IS_CI:
        print("[Telegram] Bypassed — local run. Message not sent.")
        return

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        print("[Telegram] Disabled — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing.")
        return

    import requests
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id":             chat_id,
            "text":                msg,
            "parse_mode":          "HTML",
            "disable_notification": False,
        }, timeout=15)
        if resp.status_code == 200:
            print("[Telegram] Alert sent successfully.")
        else:
            print(f"[Telegram] API error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[Telegram] Communication error: {e}")

# ─── Section 1: DB Integrity ──────────────────────────────────────────────────

def audit_db_integrity(positions_col, signals_col):
    """
    Check that positions and signals collections are not diverged.
    Rules:
      - Every ACTIVE position should have a corresponding ACTIVE signal (by symbol)
      - Every signal with status=ACTIVE should have a position record
      - No symbol should appear twice as ACTIVE in positions
      - No symbol should appear twice as ACTIVE in signals
    """
    SEC = "DB_INTEGRITY"

    pos_docs  = list(positions_col.find({}, {"symbol": 1, "status": 1, "signal_id": 1, "_id": 0}))
    sig_docs  = list(signals_col.find({},  {"symbol": 1, "status": 1, "signal_id": 1, "_id": 0}))

    active_pos_symbols = [d["symbol"] for d in pos_docs if d.get("status") == "ACTIVE"]
    active_sig_symbols = [d["symbol"] for d in sig_docs
                          if d.get("status") in ("ACTIVE", "POSITION_ACTIVE")]

    # Duplicate check in positions
    seen = defaultdict(int)
    for s in active_pos_symbols:
        seen[s] += 1
    dupes = [s for s, c in seen.items() if c > 1]
    if dupes:
        _err(SEC, f"Duplicate ACTIVE positions found for {len(dupes)} symbol(s)", {"symbols": dupes})
    else:
        _ok(SEC, f"No duplicate ACTIVE positions ({len(active_pos_symbols)} active)")

    # Duplicate check in signals
    seen_sig = defaultdict(int)
    for s in active_sig_symbols:
        seen_sig[s] += 1
    dupes_sig = [s for s, c in seen_sig.items() if c > 1]
    if dupes_sig:
        _warn(SEC, f"Duplicate ACTIVE signals found for {len(dupes_sig)} symbol(s)", {"symbols": dupes_sig})
    else:
        _ok(SEC, f"No duplicate ACTIVE signals ({len(active_sig_symbols)} active)")

    # Positions without matching signals
    active_sig_set = set(active_sig_symbols)
    orphaned_pos   = [s for s in active_pos_symbols if s not in active_sig_set]
    if orphaned_pos:
        _warn(SEC, f"{len(orphaned_pos)} ACTIVE position(s) have no matching ACTIVE signal",
              {"symbols": orphaned_pos})
    else:
        _ok(SEC, "All ACTIVE positions have a matching ACTIVE signal")

    # Signals without matching positions
    active_pos_set   = set(active_pos_symbols)
    orphaned_signals = [s for s in active_sig_symbols if s not in active_pos_set]
    if len(orphaned_signals) > MAX_ORPHANED_SIGNALS:
        _warn(SEC, f"{len(orphaned_signals)} ACTIVE signal(s) have no matching position record",
              {"symbols": orphaned_signals})
    elif orphaned_signals:
        _find("INFO", SEC,
              f"ℹ️  {len(orphaned_signals)} ACTIVE signal(s) without position (within tolerance)",
              {"symbols": orphaned_signals})
    else:
        _ok(SEC, "All ACTIVE signals have a matching position record")

    return {
        "active_positions": len(active_pos_symbols),
        "active_signals":   len(active_sig_symbols),
        "orphaned_pos":     orphaned_pos,
        "orphaned_signals": orphaned_signals,
    }

# ─── Section 2: Data Quality ──────────────────────────────────────────────────

def audit_data_quality(positions_col, signals_col):
    """
    Check for missing, null, or contradictory field values in active and closed records.
    """
    SEC = "DATA_QUALITY"
    required_pos_fields  = ["symbol", "entry_price", "stop_loss", "status", "entry_date"]
    required_sig_fields  = ["symbol", "signal_id",  "signal_date", "grade", "status"]
    optional_but_flagged = ["market_regime", "next_day_open", "grade"]

    pos_docs = list(positions_col.find({}, {"_id": 0}))
    sig_docs = list(signals_col.find({},  {"_id": 0}))

    missing_pos_fields = defaultdict(list)
    null_entry_price   = []
    invalid_stop       = []   # stop_loss >= entry_price
    future_entry_date  = []
    no_regime          = []

    for doc in pos_docs:
        sym = doc.get("symbol", "UNKNOWN")
        for f in required_pos_fields:
            if f not in doc or doc[f] is None or doc[f] == "":
                missing_pos_fields[f].append(sym)

        ep = doc.get("entry_price")
        sl = doc.get("stop_loss")
        try:
            if ep is not None and (float(ep) <= 0):
                null_entry_price.append(sym)
            if ep is not None and sl is not None and float(sl) >= float(ep):
                invalid_stop.append(sym)
        except (TypeError, ValueError):
            null_entry_price.append(sym)

        ed = doc.get("entry_date")
        if ed is not None:
            try:
                ed_dt = pd.to_datetime(ed)
                if ed_dt.date() > date.today():
                    future_entry_date.append(sym)
            except Exception:
                pass

        mr = doc.get("market_regime")
        if not mr or mr == "UNKNOWN":
            if doc.get("status") == "ACTIVE":
                no_regime.append(sym)

    for field, syms in missing_pos_fields.items():
        if syms:
            _err(SEC, f"Missing required field '{field}' in {len(syms)} position record(s)",
                 {"symbols": syms[:10]})

    if null_entry_price:
        _err(SEC, f"{len(null_entry_price)} position(s) have zero/null entry_price",
             {"symbols": null_entry_price})
    else:
        _ok(SEC, "All positions have valid entry_price")

    if invalid_stop:
        _err(SEC, f"{len(invalid_stop)} position(s) have stop_loss >= entry_price (invalid)",
             {"symbols": invalid_stop})
    else:
        _ok(SEC, "All stop_loss values are below entry_price")

    if future_entry_date:
        _warn(SEC, f"{len(future_entry_date)} position(s) have entry_date in the future",
              {"symbols": future_entry_date})
    else:
        _ok(SEC, "No future-dated entry dates found")

    if no_regime:
        _warn(SEC, f"{len(no_regime)} ACTIVE position(s) missing market_regime",
              {"symbols": no_regime})
    else:
        _ok(SEC, "All ACTIVE positions have a market_regime label")

    # Signal quality
    missing_sig_fields = defaultdict(list)
    for doc in sig_docs:
        sym = doc.get("symbol", "UNKNOWN")
        for f in required_sig_fields:
            if f not in doc or doc[f] is None or doc[f] == "":
                missing_sig_fields[f].append(sym)

    for field, syms in missing_sig_fields.items():
        if syms:
            lvl = "ERROR" if field in ("signal_id", "signal_date") else "WARN"
            _find(lvl, SEC,
                  f"{'❌' if lvl=='ERROR' else '⚠️ '} Missing signal field '{field}' in {len(syms)} signal(s)",
                  {"symbols": syms[:10]})
    if not any(missing_sig_fields.values()):
        _ok(SEC, f"All {len(sig_docs)} signal records have required fields")

# ─── Section 3: Logic Integrity ───────────────────────────────────────────────

def audit_logic_integrity(positions_col):
    """
    Check stop-loss math and trailing stop direction.
    Rules:
      - trailing_stop must be >= stop_loss (trailing only moves up)
      - trailing_stop must be < entry_price (stop must be below entry)
      - current_price must not be severely below trailing_stop (missed exit)
      - shadow_sl fields must be < entry_price
    """
    SEC = "LOGIC_INTEGRITY"

    pos_docs = list(positions_col.find({"status": "ACTIVE"}, {"_id": 0}))
    if not pos_docs:
        _ok(SEC, "No ACTIVE positions to validate")
        return

    trailing_inverted   = []
    stop_above_entry    = []
    missed_exit         = []
    shadow_sl_issues    = []

    for doc in pos_docs:
        sym     = doc.get("symbol", "UNKNOWN")
        ep      = _safe_float(doc.get("entry_price"))
        sl      = _safe_float(doc.get("stop_loss"))
        ts      = _safe_float(doc.get("trailing_stop", sl))
        cp      = _safe_float(doc.get("current_price"))

        if None in (ep, sl, ts):
            continue

        # Trailing stop should not be below initial stop loss (only ratchets up)
        if ts < sl:
            trailing_inverted.append({"symbol": sym, "trailing_stop": ts, "stop_loss": sl})

        # Trailing stop above current price (true missed exit) — not same as above entry,
        # since for profitable positions trailing_stop > entry_price is correct and expected.
        if cp is not None and cp > 0 and ts > cp:
            stop_above_entry.append({
                "symbol":        sym,
                "trailing_stop": ts,
                "current_price": cp,
                "entry_price":   ep,
            })

        # Price severely below stop — should have been exited
        if cp is not None and cp > 0:
            breach_pct = (cp - ts) / ts * 100
            if breach_pct < -MAX_STOP_VIOLATION_PCT:
                missed_exit.append({
                    "symbol":       sym,
                    "current_price":cp,
                    "trailing_stop":ts,
                    "breach_pct":   round(breach_pct, 2)
                })

        # Shadow SL must be below entry
        for pct in [8, 10, 12]:
            skey = f"shadow_sl_{pct}pct"
            ssl  = _safe_float(doc.get(skey))
            if ssl is not None and ep is not None and ssl >= ep:
                shadow_sl_issues.append({"symbol": sym, "field": skey, "value": ssl, "entry": ep})

    if trailing_inverted:
        _err(SEC, f"{len(trailing_inverted)} ACTIVE position(s) have trailing_stop < stop_loss",
             {"items": trailing_inverted})
    else:
        _ok(SEC, "trailing_stop >= stop_loss for all ACTIVE positions")

    if stop_above_entry:
        _err(SEC, f"{len(stop_above_entry)} ACTIVE position(s) have trailing_stop > current_price (possible missed exit)",
             {"items": stop_above_entry})
    else:
        _ok(SEC, "No trailing stops exceed current price (no missed exits from stop breach)")

    if missed_exit:
        _err(SEC, f"{len(missed_exit)} position(s) are >{MAX_STOP_VIOLATION_PCT}% below their stop (possible missed exit)",
             {"items": missed_exit})
    else:
        _ok(SEC, "No missed exits detected (all prices above stops or within tolerance)")

    if shadow_sl_issues:
        _warn(SEC, f"{len(shadow_sl_issues)} shadow SL value(s) >= entry_price (invalid)",
              {"items": shadow_sl_issues})
    else:
        _ok(SEC, "All shadow SL values are below entry_price")

# ─── Section 4: Performance Gate ─────────────────────────────────────────────

def audit_performance_gate(positions_col, signals_col):
    """
    Evaluate whether system performance is on track with the 1-2 month forward test goal.
    Metrics checked:
      - Closed win rate (target >= 25%)
      - Closed avg realized PnL (target >= 0%)
      - Active position average unrealized PnL
      - Expectancy estimate: win_rate * avg_win + (1 - win_rate) * avg_loss
      - PAPER_ONLY signal rate (too many = capital constraint, not signal quality)
    """
    SEC = "PERFORMANCE_GATE"

    closed_docs = list(positions_col.find({"status": "CLOSED"}, {"_id": 0}))
    active_docs = list(positions_col.find({"status": "ACTIVE"},  {"_id": 0}))
    paper_count = positions_col.count_documents({"status": "PAPER_ONLY"})
    total_sigs  = signals_col.count_documents({})

    # ── Closed book ──
    closed_pnls  = []
    win_exits    = []
    loss_exits   = []

    for doc in closed_docs:
        pnl = _safe_float(doc.get("pnl_pct"))
        if pnl is None:
            continue
        closed_pnls.append(pnl)
        if pnl > 0:
            win_exits.append(pnl)
        else:
            loss_exits.append(pnl)

    n_closed   = len(closed_pnls)
    n_winners  = len(win_exits)
    win_rate   = n_winners / n_closed if n_closed > 0 else 0.0
    avg_pnl    = sum(closed_pnls) / n_closed if n_closed > 0 else 0.0
    avg_win    = sum(win_exits)  / n_winners  if win_exits  else 0.0
    avg_loss   = sum(loss_exits) / len(loss_exits) if loss_exits else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss if n_closed > 0 else None

    _find("INFO", SEC, f"ℹ️  Closed trades: {n_closed} | Winners: {n_winners} | Win rate: {win_rate:.1%}")
    _find("INFO", SEC, f"ℹ️  Avg realized PnL: {avg_pnl:+.2f}% | Avg win: {avg_win:+.2f}% | Avg loss: {avg_loss:+.2f}%")

    if n_closed >= 10:
        if win_rate < MIN_WIN_RATE_GOAL:
            _warn(SEC, f"Win rate {win_rate:.1%} is below soft goal of {MIN_WIN_RATE_GOAL:.0%}",
                  {"closed_trades": n_closed, "winners": n_winners})
        else:
            _ok(SEC, f"Win rate {win_rate:.1%} meets or exceeds {MIN_WIN_RATE_GOAL:.0%} soft goal")

        if avg_pnl < EXPECTED_EXPECTANCY_PCT:
            _warn(SEC, f"Avg realized PnL {avg_pnl:+.2f}% is negative on closed book",
                  {"avg_pnl": avg_pnl, "n_closed": n_closed})
        else:
            _ok(SEC, f"Avg realized PnL {avg_pnl:+.2f}% is non-negative")

        if expectancy is not None:
            _find("INFO", SEC,
                  f"ℹ️  Estimated expectancy (win_rate × avg_win + loss_rate × avg_loss): {expectancy:+.2f}%")
    else:
        _find("INFO", SEC,
              f"ℹ️  Only {n_closed} closed trades — performance gate requires ≥10 for meaningful stats")

    # ── Active book ──
    active_pnls = [_safe_float(d.get("pnl_pct")) for d in active_docs]
    active_pnls = [p for p in active_pnls if p is not None]
    n_active    = len(active_docs)
    avg_active  = sum(active_pnls) / n_active if n_active > 0 else None

    _find("INFO", SEC, f"ℹ️  Active positions: {n_active}" +
          (f" | Avg unrealized PnL: {avg_active:+.2f}%" if avg_active is not None else ""))

    # Full cohort view (closed + active unrealized)
    all_pnls     = closed_pnls + active_pnls
    cohort_avg   = sum(all_pnls) / len(all_pnls) if all_pnls else None
    if cohort_avg is not None:
        _find("INFO", SEC, f"ℹ️  Full cohort avg PnL (closed + active): {cohort_avg:+.2f}%")
        if cohort_avg < 0:
            _warn(SEC, f"Full cohort avg PnL {cohort_avg:+.2f}% is negative — active runners not yet compensating for closed losses")

    # ── PAPER_ONLY signal rate ──
    if total_sigs > 0:
        paper_rate = paper_count / total_sigs
        _find("INFO", SEC, f"ℹ️  PAPER_ONLY signals: {paper_count}/{total_sigs} ({paper_rate:.1%})")
        if paper_rate > 0.5:
            _warn(SEC, f"Over half of signals are PAPER_ONLY ({paper_rate:.1%}) — portfolio cap is blocking many setups")

    return {
        "n_closed":   n_closed,
        "win_rate":   win_rate,
        "avg_pnl":    avg_pnl,
        "expectancy": expectancy,
        "n_active":   n_active,
        "avg_active": avg_active,
        "cohort_avg": cohort_avg,
    }

# ─── Section 5: Exit Integrity ─────────────────────────────────────────────────

def audit_exit_integrity(positions_col):
    """
    Verify that all closed positions have valid exit fields and exit reasoning.
    Flags:
      - exit_price is zero, null, or negative
      - exit_date is missing or is before entry_date
      - exit_reason is missing or 'Unknown'
      - pnl_pct does not reconcile with entry_price and exit_price
    """
    SEC = "EXIT_INTEGRITY"

    closed_docs = list(positions_col.find({"status": "CLOSED"}, {"_id": 0}))

    if not closed_docs:
        _ok(SEC, "No closed positions found — nothing to validate")
        return

    missing_exit_price  = []
    missing_exit_date   = []
    missing_exit_reason = []
    date_before_entry   = []
    pnl_mismatch        = []

    for doc in closed_docs:
        sym  = doc.get("symbol", "UNKNOWN")
        ep   = _safe_float(doc.get("entry_price"))
        xp   = _safe_float(doc.get("exit_price"))
        pnl  = _safe_float(doc.get("pnl_pct"))
        xd   = doc.get("exit_date")
        ed   = doc.get("entry_date")
        xr   = doc.get("exit_reason")

        if xp is None or xp <= 0:
            missing_exit_price.append(sym)

        if not xd:
            missing_exit_date.append(sym)
        else:
            try:
                xd_dt = pd.to_datetime(xd)
                ed_dt = pd.to_datetime(ed)
                if xd_dt < ed_dt:
                    date_before_entry.append(sym)
            except Exception:
                pass

        if not xr or str(xr).lower() in ("", "unknown", "historical", "none"):
            missing_exit_reason.append(sym)

        # PnL reconciliation: expected_pnl = (exit_price - entry_price) / entry_price * 100
        if ep and xp and ep > 0 and xp > 0 and pnl is not None:
            expected_pnl = (xp - ep) / ep * 100
            diff         = abs(expected_pnl - pnl)
            if diff > 1.0:   # Allow 1% tolerance for rounding
                pnl_mismatch.append({
                    "symbol":       sym,
                    "stored_pnl":   round(pnl, 2),
                    "computed_pnl": round(expected_pnl, 2),
                    "diff":         round(diff, 2),
                })

    total = len(closed_docs)

    if missing_exit_price:
        _err(SEC, f"{len(missing_exit_price)}/{total} closed positions missing valid exit_price",
             {"symbols": missing_exit_price[:10]})
    else:
        _ok(SEC, f"All {total} closed positions have valid exit_price")

    if missing_exit_date:
        _warn(SEC, f"{len(missing_exit_date)}/{total} closed positions missing exit_date",
              {"symbols": missing_exit_date[:10]})
    else:
        _ok(SEC, "All closed positions have exit_date")

    if missing_exit_reason:
        _warn(SEC, f"{len(missing_exit_reason)}/{total} closed positions have missing/Unknown exit_reason",
              {"symbols": missing_exit_reason[:10]})
    else:
        _ok(SEC, "All closed positions have a valid exit_reason")

    if date_before_entry:
        _err(SEC, f"{len(date_before_entry)} position(s) have exit_date before entry_date",
             {"symbols": date_before_entry})
    else:
        _ok(SEC, "No exit dates precede entry dates")

    if pnl_mismatch:
        _warn(SEC, f"{len(pnl_mismatch)} closed position(s) have pnl_pct that does not reconcile with exit_price",
              {"items": pnl_mismatch[:5]})
    else:
        _ok(SEC, "PnL values reconcile with entry/exit prices for all closed positions")

# ─── Section 6: Stale Data Guard ──────────────────────────────────────────────

def audit_stale_data(positions_col):
    """
    Flag ACTIVE positions that have not had their price updated recently.
    Uses 'updated_at' field from MongoDB.
    """
    SEC = "STALE_DATA"

    now      = datetime.now(timezone.utc)
    cutoff   = now - timedelta(days=MAX_DAYS_WITHOUT_PRICE_UPDATE)
    stale    = []
    no_ts    = []

    docs = list(positions_col.find({"status": "ACTIVE"}, {"symbol": 1, "updated_at": 1, "_id": 0}))
    for doc in docs:
        sym  = doc.get("symbol", "UNKNOWN")
        upd  = doc.get("updated_at")
        if upd is None:
            no_ts.append(sym)
            continue
        try:
            upd_dt = pd.to_datetime(upd, utc=True)
            if upd_dt < pd.Timestamp(cutoff):
                days_old = (now - upd_dt.to_pydatetime()).days
                stale.append({"symbol": sym, "last_updated": str(upd_dt.date()), "days_old": days_old})
        except Exception:
            no_ts.append(sym)

    if stale:
        _warn(SEC, f"{len(stale)} ACTIVE position(s) not updated in >{MAX_DAYS_WITHOUT_PRICE_UPDATE} days",
              {"items": stale})
    else:
        _ok(SEC, f"All ACTIVE positions updated within {MAX_DAYS_WITHOUT_PRICE_UPDATE} days")

    if no_ts:
        _warn(SEC, f"{len(no_ts)} ACTIVE position(s) have no 'updated_at' timestamp",
              {"symbols": no_ts})

# ─── Section 7: Signal Pipeline Health ────────────────────────────────────────

def audit_signal_pipeline(signals_col, positions_col, logs_col):
    """
    Assess the signal generation funnel:
      - Signals generated in last 30 days
      - How many converted to ACTIVE vs PAPER_ONLY
      - Recent day(s) with zero signals (might indicate scanner not running)
    """
    SEC = "SIGNAL_PIPELINE"

    now       = datetime.now(timezone.utc)
    d30_ago   = now - timedelta(days=30)
    d7_ago    = now - timedelta(days=7)

    recent_sigs = list(signals_col.find(
        {"signal_date": {"$gte": d30_ago}},
        {"symbol": 1, "status": 1, "signal_date": 1, "grade": 1, "_id": 0}
    ))

    n_30d        = len(recent_sigs)
    n_active_30d = sum(1 for s in recent_sigs if s.get("status") == "ACTIVE")
    n_paper_30d  = sum(1 for s in recent_sigs if s.get("status") == "PAPER_ONLY")
    n_closed_30d = sum(1 for s in recent_sigs if s.get("status") == "CLOSED")

    _find("INFO", SEC, f"ℹ️  Last 30 days: {n_30d} signals | "
          f"ACTIVE={n_active_30d} | PAPER_ONLY={n_paper_30d} | CLOSED={n_closed_30d}")

    # Grade distribution of recent signals
    grade_counts = defaultdict(int)
    for s in recent_sigs:
        g = s.get("grade") or "UNKNOWN"
        grade_counts[g] += 1
    if grade_counts:
        dist_str = " | ".join(f"{g}:{c}" for g, c in sorted(grade_counts.items()))
        _find("INFO", SEC, f"ℹ️  Grade distribution (30d): {dist_str}")

    # Check last 7 days for scan activity using logs collection
    scan_days = set()
    if logs_col is not None:
        try:
            scan_logs = logs_col.find(
                {"action": "SCAN_COMPLETED", "timestamp": {"$gte": d7_ago}},
                {"timestamp": 1, "_id": 0}
            )
            for log in scan_logs:
                ts = log.get("timestamp")
                if ts:
                    day = pd.to_datetime(ts, utc=True).date()
                    scan_days.add(day)
        except Exception as e:
            _warn(SEC, f"Could not query scan logs: {e}")

    expected_trading_days = _estimate_trading_days_in_range(d7_ago.date(), now.date())
    if scan_days:
        _find("INFO", SEC,
              f"ℹ️  Scanner ran on {len(scan_days)}/{expected_trading_days} estimated trading days in last 7 days")
        if len(scan_days) < expected_trading_days - 1:
            _warn(SEC, f"Scanner may have missed {expected_trading_days - len(scan_days)} trading day(s) in the last week")
    else:
        _warn(SEC, "No SCAN_COMPLETED log entries found in the last 7 days — scanner may not be running")

    # Conversion rate (signals → active positions)
    if n_30d > 0:
        conversion_rate = (n_active_30d + n_closed_30d) / n_30d
        _find("INFO", SEC, f"ℹ️  Signal-to-trade conversion rate (30d): {conversion_rate:.1%}")
        if conversion_rate < 0.1 and n_30d > 5:
            _warn(SEC, f"Very low conversion rate {conversion_rate:.1%} — many signals are PAPER_ONLY (portfolio cap?)")

# ─── Section 8: Regime Coverage ───────────────────────────────────────────────

def audit_regime_coverage(positions_col, signals_col):
    """
    Verify that market_regime field is populated correctly.
    Checks:
      - Active positions should not have UNKNOWN regime
      - Recent signals should not have UNKNOWN regime
      - Regime distribution should not be 100% one label (possible stale cache)
    """
    SEC = "REGIME_COVERAGE"

    all_pos  = list(positions_col.find({}, {"symbol": 1, "status": 1, "market_regime": 1, "_id": 0}))
    all_sigs = list(signals_col.find({},   {"symbol": 1, "status": 1, "market_regime": 1, "_id": 0}))

    # Active positions with UNKNOWN regime
    unknown_active = [d["symbol"] for d in all_pos
                      if d.get("status") == "ACTIVE" and
                      (not d.get("market_regime") or d.get("market_regime") == "UNKNOWN")]
    if unknown_active:
        _warn(SEC, f"{len(unknown_active)} ACTIVE position(s) have UNKNOWN market_regime",
              {"symbols": unknown_active})
    else:
        _ok(SEC, "All ACTIVE positions have a non-UNKNOWN market_regime")

    # Regime distribution across all signals
    regime_counts = defaultdict(int)
    for d in all_sigs:
        r = d.get("market_regime") or "UNKNOWN"
        regime_counts[r] += 1

    total = sum(regime_counts.values())
    if total > 0:
        dist_str = " | ".join(f"{r}:{c}({c/total:.0%})" for r, c in sorted(regime_counts.items()))
        _find("INFO", SEC, f"ℹ️  Signal regime distribution: {dist_str}")

        unknown_pct = regime_counts.get("UNKNOWN", 0) / total
        if unknown_pct > 0.3:
            _warn(SEC, f"{unknown_pct:.0%} of signals have UNKNOWN regime — check Nifty data fetch reliability")

        # Stale cache detection: 100% same label on recent signals
        recent_regimes = [d.get("market_regime") for d in all_sigs[-20:]
                          if d.get("market_regime") and d.get("market_regime") != "UNKNOWN"]
        if recent_regimes and len(set(recent_regimes)) == 1 and len(recent_regimes) > 5:
            _warn(SEC, f"Last 20 signals are all in regime '{recent_regimes[0]}' — possible stale regime cache")
        else:
            _ok(SEC, "Regime labels show expected variety in recent signals")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(val):
    """Return float or None."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None

def _estimate_trading_days_in_range(start: date, end: date) -> int:
    """Rough estimate: Mon–Fri only (no Indian holidays)."""
    count = 0
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon–Fri
            count += 1
        d += timedelta(days=1)
    return max(count - 1, 0)   # Subtract 1 (today may not be done yet)

# ─── Safe Data Fixes ──────────────────────────────────────────────────────────

def fix_shadow_sl_above_entry(positions_col) -> list:
    """
    Fix #1: Shadow SL fields that are >= entry_price.

    Root cause: shadow_sl was computed from the original signal price before
    entry_price was updated to the next-day MOO price. This recomputes them
    from the actual stored entry_price using the same -8% / -10% / -12% factors.

    Safe: additive correction only. No position status, trailing stop, or PnL
    fields are touched. Shadow SL fields are research metadata — not live risk.
    """
    applied = []
    docs = list(positions_col.find({}, {"symbol": 1, "entry_price": 1,
                                        "shadow_sl_8pct": 1, "shadow_sl_10pct": 1,
                                        "shadow_sl_12pct": 1, "_id": 0}))
    for doc in docs:
        sym = doc.get("symbol", "UNKNOWN")
        ep  = _safe_float(doc.get("entry_price"))
        if ep is None or ep <= 0:
            continue

        updates = {}
        for pct_label, factor in [("8pct", 0.92), ("10pct", 0.90), ("12pct", 0.88)]:
            field = f"shadow_sl_{pct_label}"
            stored = _safe_float(doc.get(field))
            if stored is not None and stored >= ep:
                correct = round(ep * factor, 2)
                updates[field] = correct

        if updates:
            try:
                positions_col.update_one(
                    {"symbol": sym},
                    {"$set": updates}
                )
                desc = f"[FIX:SHADOW_SL] {sym}: recomputed {list(updates.keys())} from entry_price={ep}"
                applied.append(desc)
                print(f"  {desc}")
            except Exception as e:
                print(f"  [FIX:SHADOW_SL] ERROR updating {sym}: {e}")
    return applied


def fix_missing_market_regime(positions_col, signals_col) -> list:
    """
    Fix #2: Backfill market_regime for ACTIVE positions that have UNKNOWN or null regime.

    Calls get_market_regime(entry_date) for each affected position.
    Safe: only sets market_regime where it is currently missing/UNKNOWN.
    Does not alter any financial field.
    """
    applied = []
    docs = list(positions_col.find(
        {"status": "ACTIVE"},
        {"symbol": 1, "market_regime": 1, "entry_date": 1, "signal_id": 1, "_id": 0}
    ))

    needs_fix = [
        d for d in docs
        if not d.get("market_regime") or d.get("market_regime") == "UNKNOWN"
    ]

    if not needs_fix:
        return applied

    # Import lazily to avoid circular dependency at module level
    try:
        from streamlined_ipo_scanner import get_market_regime
    except ImportError as e:
        print(f"  [FIX:REGIME] Cannot import get_market_regime: {e}")
        return applied

    for doc in needs_fix:
        sym        = doc.get("symbol", "UNKNOWN")
        entry_date = doc.get("entry_date")
        sig_id     = doc.get("signal_id")

        try:
            regime = get_market_regime(entry_date)
            if not regime or regime == "UNKNOWN":
                print(f"  [FIX:REGIME] {sym}: get_market_regime returned UNKNOWN — skipping")
                continue

            positions_col.update_one(
                {"symbol": sym},
                {"$set": {"market_regime": regime}}
            )
            # Also sync to signals collection if signal_id is present
            if sig_id and signals_col is not None:
                signals_col.update_one(
                    {"signal_id": sig_id},
                    {"$set": {"market_regime": regime}}
                )

            desc = f"[FIX:REGIME] {sym}: set market_regime={regime} (from entry_date={entry_date})"
            applied.append(desc)
            print(f"  {desc}")
        except Exception as e:
            print(f"  [FIX:REGIME] ERROR for {sym}: {e}")

    return applied


def fix_pnl_mismatch(positions_col) -> list:
    """
    Fix #3: Recompute pnl_pct for CLOSED positions where stored value disagrees
    with (exit_price - entry_price) / entry_price * 100 by more than 1%.

    Safe: only touches pnl_pct on CLOSED positions where the mathematical
    recomputation is unambiguous. Does not modify any ACTIVE position.
    The corrected value is logged before writing.
    """
    applied = []
    closed_docs = list(positions_col.find(
        {"status": "CLOSED"},
        {"symbol": 1, "entry_price": 1, "exit_price": 1, "pnl_pct": 1, "_id": 0}
    ))

    for doc in closed_docs:
        sym  = doc.get("symbol", "UNKNOWN")
        ep   = _safe_float(doc.get("entry_price"))
        xp   = _safe_float(doc.get("exit_price"))
        pnl  = _safe_float(doc.get("pnl_pct"))

        if None in (ep, xp, pnl) or ep <= 0 or xp <= 0:
            continue

        expected = round((xp - ep) / ep * 100, 4)
        diff     = abs(expected - pnl)

        if diff > 1.0:
            try:
                positions_col.update_one(
                    {"symbol": sym, "status": "CLOSED"},
                    {"$set": {"pnl_pct": expected}}
                )
                desc = (f"[FIX:PNL] {sym}: corrected pnl_pct "
                        f"{pnl:+.2f}% → {expected:+.2f}% "
                        f"(entry={ep}, exit={xp})")
                applied.append(desc)
                print(f"  {desc}")
            except Exception as e:
                print(f"  [FIX:PNL] ERROR updating {sym}: {e}")

    return applied


# ─── Report Generation ────────────────────────────────────────────────────────

def build_report(perf_data: dict, fixes_applied: list = None) -> str:
    """Compile all findings into a structured text report."""
    lines = []
    now   = datetime.now()

    lines.append("=" * 72)
    lines.append(f"  IPO BASE SCANNER — WEEKLY AUDIT REPORT")
    lines.append(f"  Generated: {now.strftime('%Y-%m-%d %H:%M:%S IST')}")
    lines.append("=" * 72)

    # Summary header
    n_err  = len(errors)
    n_warn = len(warnings)
    status = "PASS ✅" if n_err == 0 else "FAIL ❌"
    lines.append(f"\n  OVERALL STATUS: {status}  |  Errors: {n_err}  |  Warnings: {n_warn}")
    lines.append("")

    # Performance snapshot
    if perf_data:
        lines.append("─" * 72)
        lines.append("  PERFORMANCE SNAPSHOT")
        lines.append("─" * 72)
        lines.append(f"  Closed trades     : {perf_data.get('n_closed', 'N/A')}")
        wr = perf_data.get("win_rate")
        lines.append(f"  Win rate          : {wr:.1%}" if wr is not None else "  Win rate          : N/A")
        ap = perf_data.get("avg_pnl")
        lines.append(f"  Avg realized PnL  : {ap:+.2f}%" if ap is not None else "  Avg realized PnL  : N/A")
        ex = perf_data.get("expectancy")
        lines.append(f"  Estimated expectancy: {ex:+.2f}%" if ex is not None else "  Estimated expectancy: N/A (need ≥10 closed)")
        lines.append(f"  Active positions  : {perf_data.get('n_active', 'N/A')}")
        ca = perf_data.get("cohort_avg")
        lines.append(f"  Full cohort avg   : {ca:+.2f}%" if ca is not None else "  Full cohort avg   : N/A")
        lines.append("")

    # Findings by section
    sections = sorted(set(f["section"] for f in findings))
    for sec in sections:
        sec_findings = [f for f in findings if f["section"] == sec]
        lines.append("─" * 72)
        lines.append(f"  {sec.replace('_', ' ')}")
        lines.append("─" * 72)
        for f in sec_findings:
            lines.append(f"  {f['message']}")
            if f["detail"]:
                detail_str = json.dumps(f["detail"], indent=2, default=str)
                for dl in detail_str.splitlines():
                    lines.append(f"    {dl}")
        lines.append("")

    # Summary of issues
    if errors:
        lines.append("─" * 72)
        lines.append("  CRITICAL ERRORS (action required)")
        lines.append("─" * 72)
        for e in errors:
            lines.append(f"  ❌ {e}")
        lines.append("")

    if warnings:
        lines.append("─" * 72)
        lines.append("  WARNINGS (investigate)")
        lines.append("─" * 72)
        for w in warnings:
            lines.append(f"  ⚠️  {w}")
        lines.append("")

    # Fixes applied (if any)
    if fixes_applied:
        lines.append("─" * 72)
        lines.append("  FIXES APPLIED THIS RUN")
        lines.append("─" * 72)
        for fx in fixes_applied:
            lines.append(f"  ✔ {fx}")
        lines.append("")

    lines.append("=" * 72)
    lines.append("  END OF REPORT")
    lines.append("=" * 72)

    return "\n".join(lines)


def save_report(report_text: str, audit_id: str) -> str:
    """Save report to audit_reports/ directory. Only called on local runs."""
    os.makedirs(REPORT_DIR, exist_ok=True)
    fpath = os.path.join(REPORT_DIR, f"{audit_id}.txt")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(report_text)
    return fpath


def _build_telegram_message(
    audit_id: str,
    perf_data: dict,
    fixes_log: list,
    n_errors: int,
    n_warnings: int,
) -> str:
    """Build HTML Telegram summary from audit results."""
    from datetime import timezone
    ist_now = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M IST")

    if n_errors > 0:
        status_icon  = "\U0001f534"   # red circle
        status_label = "CRITICAL ERRORS"
    elif n_warnings > 0:
        status_icon  = "\u26a0\ufe0f"
        status_label = "WARNINGS"
    else:
        status_icon  = "\U0001f7e2"   # green circle
        status_label = "ALL CLEAR"

    wr  = perf_data.get("win_rate")
    ap  = perf_data.get("avg_pnl")
    ca  = perf_data.get("cohort_avg")
    n_c = perf_data.get("n_closed", "?")
    n_a = perf_data.get("n_active", "?")

    msg = (
        f"\U0001f4cb <b>Weekly System Audit</b>\n"
        f"\U0001f4c5 <i>{ist_now}</i>\n"
        f"<code>{audit_id}</code>\n"
        f"{'=' * 36}\n"
        f"Status: {status_icon} <b>{status_label}</b>\n"
        f"Errors: <b>{n_errors}</b>  |  Warnings: <b>{n_warnings}</b>\n"
        f"\n"
        f"\U0001f4c8 <b>Performance Snapshot</b>\n"
        f"\u2022 Closed trades  : <b>{n_c}</b>\n"
        f"\u2022 Win rate       : <b>{wr:.1%}</b>\n" if wr is not None else
        f"\u2022 Win rate       : N/A\n"
    )
    # rebuild cleanly to avoid f-string nesting issues
    lines = [
        f"\U0001f4cb <b>Weekly System Audit</b>",
        f"\U0001f4c5 <i>{ist_now}</i>",
        f"<code>{audit_id}</code>",
        "=" * 34,
        f"Status: {status_icon} <b>{status_label}</b>",
        f"Errors: <b>{n_errors}</b>  |  Warnings: <b>{n_warnings}</b>",
        "",
        "\U0001f4c8 <b>Performance Snapshot</b>",
        f"\u2022 Closed trades  : <b>{n_c}</b>",
    ]
    if wr is not None:
        lines.append(f"\u2022 Win rate       : <b>{wr:.1%}</b>")
    if ap is not None:
        lines.append(f"\u2022 Avg closed PnL : <b>{ap:+.2f}%</b>")
    if ca is not None:
        lines.append(f"\u2022 Full cohort avg: <b>{ca:+.2f}%</b>")
    lines.append(f"\u2022 Active positions: <b>{n_a}</b>")

    # Error + warning summary
    error_findings = [f for f in findings if f["level"] == "ERROR"]
    warn_findings  = [f for f in findings if f["level"] == "WARN"]

    if error_findings:
        lines.append("")
        lines.append("\U0001f6d1 <b>Critical Errors:</b>")
        for f in error_findings[:8]:
            # Strip emoji prefix for cleaner Telegram look
            clean = f["message"].replace("\u274c ", "").replace("\u26a0\ufe0f  ", "")
            lines.append(f"\u2022 {clean}")
        if len(error_findings) > 8:
            lines.append(f"<i>...and {len(error_findings) - 8} more errors.</i>")

    if warn_findings:
        lines.append("")
        lines.append("\u26a0\ufe0f <b>Warnings:</b>")
        for f in warn_findings[:8]:
            clean = f["message"].replace("\u26a0\ufe0f  ", "").replace("\u274c ", "")
            lines.append(f"\u2022 {clean}")
        if len(warn_findings) > 8:
            lines.append(f"<i>...and {len(warn_findings) - 8} more warnings.</i>")

    if not error_findings and not warn_findings:
        lines.append("")
        lines.append("\u2705 All integrity checks passed.")

    # Fixes applied
    if fixes_log:
        lines.append("")
        lines.append(f"\U0001f527 <b>Fixes Applied ({len(fixes_log)}):</b>")
        for fx in fixes_log[:6]:
            # Shorten for Telegram
            short = fx.split(":", 2)[-1].strip()[:80]
            lines.append(f"\u2022 {short}")
        if len(fixes_log) > 6:
            lines.append(f"<i>...and {len(fixes_log) - 6} more fixes.</i>")

    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weekly IPO Scanner Audit Report")
    parser.add_argument("--save-only", action="store_true",
                        help="Do not print to console — save report file only")
    parser.add_argument("--fix", action="store_true",
                        help="Apply safe data fixes after audit (shadow SL, regime, PnL reconciliation)")
    parser.add_argument("--section", choices=[
        "db", "data_quality", "logic", "performance", "exits", "stale", "pipeline", "regime"
    ], help="Run only a specific audit section")
    args = parser.parse_args()

    audit_id  = f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    fixes_log = []  # Track what was fixed this run

    mode_label = "CI (GitHub Actions)" if IS_CI else "LOCAL"
    print(f"[AUDIT] Starting Weekly Audit — {audit_id} [{mode_label}]")
    if IS_CI:
        print("[AUDIT] CI mode: MongoDB + Telegram. No txt file generated.")
    else:
        print("[AUDIT] Local mode: txt file only. MongoDB and Telegram skipped.")

    try:
        positions_col, signals_col, logs_col, system_audits_col = _load_db()
    except RuntimeError as e:
        print(f"[AUDIT] FATAL: Cannot connect to database: {e}")
        sys.exit(1)

    # Which sections to run
    run_all  = args.section is None
    perf_data = {}

    try:
        if run_all or args.section == "db":
            print("  [1/8] DB Integrity...")
            audit_db_integrity(positions_col, signals_col)

        if run_all or args.section == "data_quality":
            print("  [2/8] Data Quality...")
            audit_data_quality(positions_col, signals_col)

        if run_all or args.section == "logic":
            print("  [3/8] Logic Integrity...")
            audit_logic_integrity(positions_col)

        if run_all or args.section == "performance":
            print("  [4/8] Performance Gate...")
            perf_data = audit_performance_gate(positions_col, signals_col)

        if run_all or args.section == "exits":
            print("  [5/8] Exit Integrity...")
            audit_exit_integrity(positions_col)

        if run_all or args.section == "stale":
            print("  [6/8] Stale Data Guard...")
            audit_stale_data(positions_col)

        if run_all or args.section == "pipeline":
            print("  [7/8] Signal Pipeline Health...")
            audit_signal_pipeline(signals_col, positions_col, logs_col)

        if run_all or args.section == "regime":
            print("  [8/8] Regime Coverage...")
            audit_regime_coverage(positions_col, signals_col)

    except Exception as e:
        _err("RUNTIME", f"Unexpected error during audit: {e}")

    # ── Auto-fix safe issues ────────────────────────────────────────────────
    if args.fix and run_all:
        print("\n[AUDIT] Applying safe data fixes...")

        sl_fixes = fix_shadow_sl_above_entry(positions_col)
        fixes_log.extend(sl_fixes)
        if sl_fixes:
            print(f"  Shadow SL: fixed {len(sl_fixes)} field group(s)")
        else:
            print("  Shadow SL: no corrections needed")

        regime_fixes = fix_missing_market_regime(positions_col, signals_col)
        fixes_log.extend(regime_fixes)
        if regime_fixes:
            print(f"  Regime:    backfilled {len(regime_fixes)} position(s)")
        else:
            print("  Regime:    no backfill needed")

        pnl_fixes = fix_pnl_mismatch(positions_col)
        fixes_log.extend(pnl_fixes)
        if pnl_fixes:
            print(f"  PnL:       reconciled {len(pnl_fixes)} closed position(s)")
        else:
            print("  PnL:       no reconciliation needed")

        if fixes_log:
            print(f"\n[AUDIT] {len(fixes_log)} fix(es) applied.")
        else:
            print("[AUDIT] No fixes were needed.")
    elif args.fix and not run_all:
        print("[AUDIT] --fix only runs with a full audit (omit --section).")

    # ── Compile report text ─────────────────────────────────────────────────
    print("\n  Compiling report...")
    report_text = build_report(perf_data, fixes_applied=fixes_log if fixes_log else None)

    n_err  = len(errors)
    n_warn = len(warnings)

    if IS_CI:
        # ── CI: MongoDB + Telegram only, no txt ─────────────────────────────
        print("[AUDIT] CI: Saving results to MongoDB...")
        try:
            from db import save_audit_to_db
            db_saved = save_audit_to_db(
                audit_id             = audit_id,
                overall_status       = "FAIL" if n_err > 0 else "PASS",
                n_errors             = n_err,
                n_warnings           = n_warn,
                findings             = findings,
                performance_snapshot = perf_data,
                fixes_applied        = fixes_log,
                report_file          = None,  # No txt file in CI
            )
            print("[AUDIT] Saved to MongoDB." if db_saved else "[AUDIT] Warning: MongoDB save returned False.")
        except Exception as db_err:
            print(f"[AUDIT] Warning: MongoDB save failed: {db_err}")

        print("[AUDIT] CI: Sending Telegram alert...")
        tg_msg = _build_telegram_message(audit_id, perf_data, fixes_log, n_err, n_warn)
        send_telegram_alert(tg_msg)

        # Always print to GitHub Actions log as well
        if not args.save_only:
            print()
            print(report_text)

    else:
        # ── Local: txt file only, no MongoDB, no Telegram ───────────────────
        if not args.save_only:
            print()
            print(report_text)

        saved_path = save_report(report_text, audit_id)
        print(f"\n[AUDIT] Report saved to: {saved_path}")
        print("[AUDIT] Local run: MongoDB and Telegram skipped.")

    # ── Exit code ────────────────────────────────────────────────────────────
    if n_err > 0:
        print(f"\n[AUDIT] FAILED: {n_err} error(s) found. Investigate immediately.")
        sys.exit(1)
    elif n_warn > 0:
        print(f"\n[AUDIT] WARNINGS: {n_warn} warning(s). Review before next scan.")
        sys.exit(0)
    else:
        print("\n[AUDIT] PASS: Audit completed cleanly. System is healthy.")
        sys.exit(0)


if __name__ == "__main__":
    main()
