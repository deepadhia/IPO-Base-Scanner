# 🤖 AGENTS.md — Development Guidelines & System Architecture

This file defines the core engineering standards, domain knowledge, and operational protocols for any AI agent or engineer working in the **IPO Base Scanner** repository.

---

## 🏛️ 1. Repository Purpose & Architecture

The **IPO Base Scanner** is a behavior-driven quantitative trading system that identifies, validates, and manages momentum breakouts in recent Indian IPOs (Mainboard NSE/BSE).

### Core Engines & Scanners
1. **Listing Day Breakout Engine (`listing_day_breakout_scanner.py`):**
   - Targets IPOs from listing day up to 730 calendar days (2 years).
   - Validates initial breaks of Day-1 listing high or tight local bases.
   - Enforces 60-minute holding observation (`PENDING` state) to reject intraday wick traps.
   - Hard 12% risk stop-loss cap dynamically buffered by 3% below the 15-day swing low.
2. **Consolidation Breakout Engine (`streamlined_ipo_scanner.py`):**
   - Targets IPOs 10–200 days post-listing breaking out of tight multi-day bases.
   - Scan windows: 10 & 20 days (narrowed from 40/80/120 on 2026-07-05).
   - Currently operating in forced **Out-of-Sample Forward Testing Mode (`PAPER_ONLY`)**.
3. **Watchlist Hourly Scanner (`hourly_breakout_scanner.py`):**
   - Intraday alerting engine for active watchlist symbols.
   - Requires ≥1.5x breakout-bar volume; never overwrites open daily positions.
4. **Trade Diagnostics & Strategy Evidence Engine (`diagnose_trade.py` & `core/strategy_evidence.py`):**
   - Self-diagnosing intelligence layer that evaluates trade DNA (volume surges, base PRNG %, upper wick exhaustion) against real trade outcomes to continuously uncover proven strengths and eliminate strategy leaks.

---

## 🛡️ 2. Clean-Cohort Baseline & Statistical Integrity

> [!IMPORTANT]
> **CLEAN COHORT CUTOFF: `2026-07-05`**
> On 2026-07-05 (v3.3.0 parameter tightening), flawed legacy rules were eliminated:
> 1. Grade C setups were permanently disqualified.
> 2. Wide 40/80/120-day consolidation windows were removed.
> 3. Strict volume floors (≥150,000 shares) and turnover thresholds (≥ Rs 1 Cr) were made mandatory.

### Rules for All Analytics & Audit Scripts
* **Never include pre-July-5, 2026 data in active strategy statistics.**
* Any new analytical or statistical script **MUST** filter `entry_date >= '2026-07-05'` and `signal_date >= '2026-07-05'`.
* Pre-July-5 historical records are safely preserved in:
  - `positions_legacy_archive` (MongoDB)
  - `signals_legacy_archive` (MongoDB)
* Active collections `positions` and `signals` must remain 100% clean-cohort data only.

---

## 💾 3. Database Schema & Conventions (MongoDB)

* **Database Name:** `ipo_scanner_v2`
* **Key Collections:**
  - `positions`: Active, paper, and clean closed trades.
  - `signals`: High-conviction breakout signals.
  - `strategy_evidence`: Granular setup DNA paired with empirical trade outcomes.
  - `logs`: Daily scanner execution and telemetry logs.
  - `daily_candles_cache`: Fast local cache for Upstox daily OHLCV candles.
  - `instrument_keys`: Upstox instrument key mappings.
  - `positions_legacy_archive` & `signals_legacy_archive`: Isolated historical archives.

### Status Field Conventions
- `ACTIVE`: Live capital allocated trade.
- `PAPER_ONLY`: Forward-testing / portfolio-cap overflow paper trade.
- `CLOSED`: Realized live trade.
- `PAPER_CLOSED`: Realized paper trade.

---

## 🔍 4. Forensic Diagnostics & System Evaluation Tools

When diagnosing setups, investigating failures, or auditing strategy health, use the built-in CLI tools:

```powershell
# 1. System-Level Self-Diagnosis (Strengths, Weaknesses, Hypotheses & Edge Directives)
python diagnose_trade.py --system

# 2. Sync all trade forensics into MongoDB collection 'strategy_evidence'
python diagnose_trade.py --sync-evidence

# 3. Diagnose specific symbols with Strengths, Weaknesses, and Algo Takeaways
python diagnose_trade.py KUSUMGAR CMRGREEN --vs-winners

# 4. Diagnose all currently active portfolio positions
python diagnose_trade.py --active-only

# 5. Unified MongoDB management entrypoint
python manage_db.py diagnose --system
python manage_db.py diagnose --symbols KUSUMGAR CMRGREEN --vs-winners
```

---

## 🚦 5. Quantitative & Risk Management Rules (v3.5.0 Standards)

1. **Upper 50% Candle Body Confirmation Rule:**
   - Any breakout attempt must close in the upper 50% of its total daily range: `(CLOSE - LOW) / (HIGH - LOW) >= 0.50`.
   - Rejects long shooting stars and upper supply traps (e.g. `KUSUMGAR`) structurally without curve-fitting narrow wick thresholds.
2. **14-Day Portfolio Velocity Speed Gate:**
   - Positions held `≥ 14 days` with flat/negative PnL ($PnL \le 0\%$) and decaying volume ($< 50\%$ of entry baseline) are closed early.
   - Cohort testing proves 81% of underwater 14-day trades continue to bleed (avg -13.16% drawdown), making this an essential alpha preservation rule.
3. **Anti-Chasing 8% Max Extension Guard:**
   - Breakout entries $> 8.0\%$ extended above the base breakout pivot or listing high are rejected to prevent buying overheated tops.
4. **Immediate Base Peak Re-Entry Trigger:**
   - Closed setups stored on the re-entry watchlist immediately re-trigger the moment price breaks above `peak_price_during_trade` with volume surge, capturing momentum ignition (e.g., `URBANCO` at ₹152) with zero opportunity loss.
5. **Volume Surge Multiplier:**
   - High-conviction Tier A setups require `≥ 3.0x` volume surge over the 20-day average.
   - Setups with volume spike `< 1.5x` or volume `< 150,000` shares are automatically rejected.
6. **Base Tightness (PRNG):**
   - 10-day price range (PRNG) must be `≤ 15%` to ensure coiled volatility and tight risk stops.
7. **Backtest & Quant Integrity:**
   - Never alter trading rules or backtesting logic silently.
   - Backtest integrity and statistical rigor take precedence over code aesthetics.

---

## 📋 6. Agent Workflow & Safety Checklist

Before delivering code or modifying the repository:
1. **Minimal Blast Radius:** Prefer targeted, additive, modular changes over broad refactors.
2. **Verify Imports & Types:** Ensure all imports and references exist.
3. **Preserve Immature Hypotheses:** Do not enable experimental gates on small sample sizes (see `EXPERIMENT_CHANGELOG.md`).
4. **Validate Against Clean Cohort:** Test queries against `positions` and `strategy_evidence` using `2026-07-05` cutoff.
5. **Never Destructively Overwrite DB Collections:** Always use upsert or archival tables.
