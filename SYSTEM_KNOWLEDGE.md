# IPO Breakout Qualification Engine — System Knowledge Map

> **Version:** v3.5.0
> **Last Updated:** 2026-07-21
> **Purpose:** Permanent reference document. Attach this file to any AI conversation before asking questions or requesting changes to this codebase.

---

## 1. What This System Is

A **behaviour-driven IPO momentum qualification engine** for Indian equities (NSE). It does **not** predict breakouts — it *validates* them after the market has already shown its hand through sustained price behaviour and volume expansion.

**Key principles:**
- Participate only in **confirmed breakouts** with structural + volume validation
- Filter aggressively — most symbols are rejected; only the highest-quality setups generate signals
- Log every rejection with a structured reason code so data can be analysed to continuously refine edge
- **No API order placement.** Human oversight is required for every execution — the engine only calculates, grades, and alerts via Telegram

---

## 2. Repository Layout

```
IPO-Base-Scanner/
│
├── streamlined_ipo_scanner.py       # MAIN: Consolidation breakout scanner (v3.5.0) — 4,900+ lines
├── listing_day_breakout_scanner.py  # Listing Day scanner (imports from main scanner) — 2,282 lines
├── hourly_breakout_scanner.py       # Intraday watchlist scanner
├── run_latest_rules_backtest.py     # Production rules backtesting engine & strategy audit
├── db.py                            # MongoDB persistence layer — 694 lines
├── fetch.py                         # Data acquisition (Upstox + YFinance fallback)
├── utils.py                         # Shared utilities (Nifty fetch from Upstox)
├── master_audit.py                  # System integrity audit (4 sections)
├── manage_db.py                     # Unified management CLI entrypoint (supports: python manage_db.py backtest)
│
├── core/                            # Immutable data models with Sector/Industry tracking
│   ├── models.py
│   └── repository.py
│
├── enrichment/                      # Feature store: point-in-time market/base context
│   ├── engine.py
│   ├── base.py
│   ├── breakout.py
│   └── market.py
│
├── lifecycle/                       # PnL evolution and synthetic outcome reconstruction
│   ├── tracker.py
│   └── evaluator.py
│
├── integration/                     # Cross-scanner bridge (Consolidation + Listing Day)
│   └── signal_builder.py
│
├── scripts/                         # Maintenance and migration scripts
│   ├── nightly_db_audit.py          # Nightly integrity guard (11 PM IST)
│   ├── reconcile_invalid_price.py   # Price reconciliation with full outcome reclassification
│   ├── migrate_data_model.py        # Schema migrations
│   ├── stamp_active_versions.py     # Backfill version fields on old documents
│   └── purge_penny_stocks.py        # Cleanup script
│
├── cloudflare-dispatcher/           # Precision cron dispatcher (Cloudflare Workers)
│   └── src/index.js                 # Dispatch schedule + GitHub API calls
│
├── .github/workflows/               # GitHub Actions workflows (CI/CD + automation)
│
├── analyze_30d_data.py              # 30-day quantitative analysis
├── analyze_db_quality_and_patterns.py  # Biweekly DB quality + winner pattern report
├── analyze_winning_traits.py        # Alpha trait discovery
├── reconstruct_outcomes.py          # Historical outcome reconstruction
├── weekly_audit_report.py           # Automated weekly audit report
├── visualize_signals.py             # Signal visualization
│
├── EXPERIMENT_CHANGELOG.md          # Strategy change log with clean-cohort cut points
├── README.md                        # Full public documentation
└── SYSTEM_KNOWLEDGE.md              # This file
```

---

## 3. The Two Core Scanner Strategies

### 3.1 Consolidation Breakout Scanner (`streamlined_ipo_scanner.py`)

**Target:** IPOs **10–200 days post-listing** that have built a proper base and are breaking out.

**Scan windows:** `10, 20` days (configurable via `CONSOL_WINDOWS`; narrowed 2026-07-05)

**The filter waterfall (in execution order):**

| Step | Filter | Threshold | Rejection Code |
|---|---|---|---|
| 1 | Active position check | Skip if already ACTIVE | silent |
| 2 | Listing Volume Floor | listing_day_volume >= 150,000 shares | `LISTING_VOLUME_BELOW_FLOOR` |
| 3 | Base formation range | Price within 8–35% below listing-day high | — |
| 4 | Base duration floor | Consolidation window w >= 3 days | `BASE_DURATION_BELOW_MINIMUM` |
| 5 | Consolidation range (PRNG) | <= 60% (MAX_PRNG=25.0 for ALIGNED bucket) | `loose_base` / `RC_PRNG_LIMIT` |
| 6 | Breakout close | Close must be above base high (no wick fakes) | — |
| 7 | Volume confirmation | One of: 2.5x avg burst, 1.2x (VOL_MULT) rolling, or >=3M absolute value | `RC_VOL_LIMIT` |
| 8 | Follow-through | Next candle holds base high ±2% OR >=80% continuation vol | `failed_follow_through` |
| 9 | Entry price | >= Rs 25.00 | `too_cheap` |
| 10 | Daily turnover | >= Rs 1.0 Cr (small-cap floor: Rs 0.75 Cr) | `LIQUIDITY_TRAP` |
| 11 | Market cap | >= Rs 500 Cr | `MICROCAP_PENALTY` |
| 12 | Circuit days | < 3 circuit days in 15 sessions | `LIQUIDITY_TRAP_CIRCUITS` |
| 13 | Grade minimum | Must meet MIN_LIVE_GRADE (default **B**) | `low_grade` |
| 14 | Risk:Reward | >= 1.3 (MIN_RISK_REWARD) | `poor_risk_reward` |
| 15 | Entry vs. breakout | Entry not > 8% above breakout level | `too_extended` |
| 16 | Stop loss risk | Stop not > 10% from entry | `excessive_stop_risk` |
| 17 | Breakout staleness | Breakout <= 10 days old | `stale_breakout` |
| 18 | Signal cooldown | >= 10 days since last signal for this symbol | `cooldown` |

**Main scan function:** `detect_scan()` at line 3186
**Live scan function:** `detect_live_patterns()` at line 2138

---

### 3.2 Listing Day Breakout Scanner (`listing_day_breakout_scanner.py`)

**Target:** IPOs from listing day up to **730 calendar days (2 years)** post-listing.

**Key rules:**
- Age constraint: IPOs older than 730 days are rejected
- Breakout Volume Floor: IPOs with < 150,000 breakout-day shares are rejected (Day 0 / actual listing day is exempt — volume may not be settled in DB yet)
- Base Duration Floor: requires >= 3 trading days of post-listing history
- Stop Loss: 15-day local swing low, buffered by 3%, **hard-capped at 12% max risk**
- Observation State: IPOs **≥ 5 days** old entering breakout levels during market hours go into `PENDING` for 60 minutes. IPOs **< 5 days** old are downgraded to `WATCHLIST` (alert only).
- Tier B (Base Breakout): stocks **5–20% below** listing high that break a tight local base qualify as `BASE_BREAKOUT` (40% size) without crossing listing high first; under strict quality, requires the same Day-2+ **≥1.8×** volume spike as `BREAKOUT`
- Listing/re-entry positions persist `grade="LISTING_BREAKOUT"` (winner traits stay in `winner_label` / `winner_score`) so IPO exit gates apply
- Watchlist proximity (strict mode): **3–5% below** listing high (not 0–5%)
- Volume floor: avg daily turnover < Rs 1.0 Cr or >= 3 circuit days in 15 sessions → rejected
- Limit Buy Price instruction: `Listing Day High x 1.035` (capped at 3.5% above listing high)
- Grading uses a **Tier** system (A+/B/C) rather than the consolidation scanner's `Grade`

**Architecture note:** `listing_day_breakout_scanner.py` imports `streamlined_ipo_scanner.py` via `importlib` and reuses `fetch_data`, `send_telegram`, `get_live_price`, `get_market_regime`, `write_daily_log`, and `classify_pattern_type` from the main scanner module.

---

### 3.3 Re-Entry Breakout Module (v3.4.0)

**Target:** `CLOSED` or `PAPER_CLOSED` trades that were stopped out within the last 30 days but re-break above their `peak_price_during_trade`.

**Core Philosophy:** A shakeout below the stop-loss is often a structural reset for major winners. Re-entries capitalize on valid structural breakouts that simply required a deeper volatility cushion.

**Key rules:**
- **Trigger:** Current price or day high crosses above the `peak_price_during_trade` (highest price achieved while the parent trade was active).
- **Time Constraint:** The parent trade must have closed within the last 30 days.
- **Liquidity Check:** Enforces a basic floor (>150,000 volume, >1Cr turnover).
- **DNA Filter Bypass:** Re-entries intentionally bypass the strict 1.5x volume spike and entry extension rules required for primary breakouts, as testing proved these filters degrade re-entry yield by artificially suppressing valid "legacy base" breakouts.
- **Re-Entry Cap:** Maximum of 1 re-entry per parent trade (`reentry_count`).
- **Portfolio Handling:** Re-entries consume portfolio slots just like normal trades. If the cap is full, they trigger as `PAPER_ONLY`.

---

## 4. Grading System

### 4.1 `compute_grade_hybrid()` (Consolidation Scanner)
**Location:** `streamlined_ipo_scanner.py:524`
**Returns:** `(score: int, metrics_dict: dict)` — score 0–5

| Score | Grade | Position Bias |
|---|---|---|
| 4–5 | A+ | Full size |
| 2–3 | B | Reduced + smart filters |
| 1 | C | Minimum size |
| 0 | D | Never traded |

**The 5 scoring criteria:**
1. PRNG (consolidation range) <= 18% — tight base, institutional accumulation
2. Breakout day volume >= 2.5x avg AND 3-day sum >= 4x avg — massive volume
3. 20-day return in top **85th percentile** of historical returns — momentum rank
4. `MACD > signal AND RSI > 65 AND EMA20 > EMA50` — technical trend alignment
5. Next open >= 4% above breakout close — gap-up confirmation

**Microcap Cap:** Any symbol with market cap < Rs 1,000 Cr is automatically **capped at Grade C** regardless of technical score.

**`assign_grade(score)`** at line 559: `score >= 4` → A+, `score >= 2` → B, `score == 1` → C, `score == 0` → D

### 4.2 Grade-Based Stop Loss Percentages
**Location:** `streamlined_ipo_scanner.py:680` — `calculate_grade_based_stop_loss()`
Used both for **initial stop placement** and **trailing stop percentage**.

| Grade | Stop % from Entry |
|---|---|
| A+ | 5% |
| A | 7% |
| B | 10% |
| C | 12% |
| D | 15% |
| LISTING_BREAKOUT | 10% |

Stop = `max(entry_price * (1 - stop_pct), consolidation_low * (1 - stop_pct))`, hard-floored at `entry_price * 0.80` (no more than 20% max risk).

### 4.3 Target Price Calculation
**Location:** `streamlined_ipo_scanner.py:655` — `calculate_target_price()`
`target = consolidation_high + (consolidation_range x multiplier)`

| Grade | Multiplier |
|---|---|
| A+ | 1.5 |
| A | 1.4 |
| B | 1.3 |
| C | 1.2 |

Minimum target enforced: `entry_price * 1.10`.

---

## 5. Winner Trait Classification

**Location:** `streamlined_ipo_scanner.py:191` — `classify_winner_traits()`

Every new consolidation signal is scored against 5 empirical winner criteria (from DB analysis of 2024–2026 Mainboard IPOs):

| Criterion | Test |
|---|---|
| 1. Grade B or better | grade in ('A+', 'A', 'B') |
| 2. Preferred window | consolidation_window in (10, 20) |
| 3. Strong volume | volume_ratio >= 1.5 |
| 4. Tight base | consolidation_range_pct < 18.0% |
| 5. Bullish Nifty | nifty_slope > 0 |

| Score | Label |
|---|---|
| 4–5 | `POSSIBLE_WINNER_EXPERIMENTAL` |
| 2–3 | `STANDARD` |
| 0–1 | `WATCHLIST_ONLY` |

Stored in MongoDB `signals` doc as `winner_label`. Marked **experimental** — sample size is small.

### 5.2 Listing Breakout Winner Classifier

**Location:** `listing_day_breakout_scanner.py` — `classify_listing_winner_traits()`

Every new listing day breakout signal is scored against 5 empirical winner criteria derived from the 2024–2026 historical backtest (which evaluated 486 IPOs and analyzed the common traits of trades achieving $\ge 20\%$ PnL):

| Criterion | Test | Rationale / Empirical Insight |
|---|---|---|
| 1. Early Breakout | `days_since_listing <= 35` | 75% of historical super-winners broke out within 35 trading days post listing. |
| 2. Volatility Cushion | `listing_range_pct >= 5.0%` | Listing day high-to-low range $\ge 5\%$ filters out dead/flat listing days. |
| 3. Volume Spike | `volume_ratio_vs_avg >= 1.5` | Breakout day volume must be $\ge 1.5\text{x}$ the clean trailing 10-day average (which automatically excludes Day 0/1 launch volume). |
| 4. Clean Entry | `entry_above_high_pct <= 4.0%` | Breakout close must be close to the listing high to minimize entry chasing slippage. |
| 5. Circuit Free | `circuit_days_15 == 0` | Zero locked upper/lower circuits in the last 15 sessions. |

| Score | Label |
|---|---|
| 4–5 | `POSSIBLE_WINNER` |
| 2–3 | `STANDARD` |
| 0–1 | `WATCHLIST_ONLY` |

Stored in MongoDB `signals` doc as `winner_label` and displayed as a high-probability badge on Telegram alerts.

---

## 6. Market Regime Detection

**Location:** `streamlined_ipo_scanner.py:257` — `get_market_regime()`

**Data source:** Nifty 50 (^NSEI) — fetched via Upstox, falling back to yfinance.

**Classification logic (priority order):**

| Regime | Rule |
|---|---|
| RANGE | Price within ±0.2% of either EMA20 or EMA50 |
| BULL | Price > EMA20 > EMA50 |
| WEAK_BULL | Price > EMA50 but below EMA20 |
| CORRECTION | Price below EMA50 |

**Stabilization:** A regime shift is only confirmed after persisting for **3 consecutive trading days** (reduces historical whipsaws from 72% to 7.9%).

**Regime sizing weights** (tunable via `.env`):

| Regime | Default Size Weight |
|---|---|
| BULL | 1.0x |
| WEAK_BULL | 0.75x |
| CORRECTION | 0.5x |
| RANGE | 0.5x |
| UNKNOWN | 0.5x |

Stored on every signal/position document as `position_size_weight`.

---

## 7. Portfolio and Position Management

### 7.1 Portfolio Cap Guard
- `MAX_ACTIVE_POSITIONS` (default: **5**) = soft cap — signals exceeding this are written with `status: "PAPER_ONLY"` (position row is still upserted)
- `HARD_ACTIVE_POSITIONS` (default: `MAX_ACTIVE_POSITIONS + 2` = **7**) = hard cap
- Consolidation production path forces `PAPER_ONLY` (OOS forward test); listing/re-entry/hourly honor soft/hard caps
- Hourly never overwrites an existing `ACTIVE`/`PAPER_ONLY` row for the same symbol
- `PAPER_ONLY` signals are included in **strategy expectancy analytics** but excluded from **live capital P&L calculations**

### 7.2 Trailing Stop Update Logic
**Location:** `stop_loss_update_scan()` at line 3919 — runs at **6:30 PM IST** daily.

- Trailing starts when `pnl >= trail_threshold`:
  - `MIN_PNL_FOR_TRAIL` (default: **4.0%**, lowered from 5% to eliminate the dead zone vs speed gates)
  - `LISTING_BREAKOUT` grade threshold: **3.0%** (sits below the 4% speed gate)
- New trailing = `current_price x (1 - stop_pct)` where `stop_pct` is grade-based
- Trailing stop only moves **forward** (ratchet — never loosens)
- Minimum improvement required = `entry_price x (MIN_TRAIL_MOVE_PCT / 100)` (default: **1%** of entry)

### 7.3 Exit Conditions (for ACTIVE positions)
**Location:** `stop_loss_update_scan()` in `streamlined_ipo_scanner.py`.

**Safety Guard:** Exit checking is suspended if the **Corporate Action Guard** detects an extreme single-day drop (>25% compared to the previous day's recorded price), preventing false triggers from stock splits, bonus share adjustments, or data errors.

**Exit Trigger 1: Trailing Stop Loss**
```
current_price <= trailing_stop  →  exit_reason = "Stop Loss"
```

**Exit Trigger 2: Dead-Money Patience Stops** (applied only when `max_runup_pct < 15%` — not yet winner archetype):

| Archetype | Patience Period | Runup Required | Exit Reason |
|---|---|---|---|
| IPO Discovery (grade == "LISTING_BREAKOUT") | >= 20 days | < 4.0% | `Time Stop - IPO Dead Money` |
| Consolidation | >= 21 days | < 5.0% | `Time Stop - Consolidation Dead Money` |
| Other (fallback) | > 30 days AND price < entry x 0.95 | — | `Time Stop -5%` |
| Other (fallback) | > 60 days AND price < entry x 0.92 | — | `Time Stop -8%` |

**Winner Archetype Exempt:** `max_runup_pct >= 15.0%` ➔ standard patience stops are **never applied**.

**Exit Trigger 3: Volume Exhaustion Early Exit (v3.4.0)**
Exits flat, stagnant positions before day 40 if volume collapses relative to the post-entry baseline, freeing capital for fresh breakouts or re-entries:
- **Condition:** `pnl` between **-3.0% and +5.0%**, `max_runup_pct < 8.0%`, `days_held >= min_days`.
- **Minimum Days:** **15 trading days** for `LISTING_BREAKOUT`, **10 trading days** for Consolidation.
- **Volume Ratio Threshold:** Recent 5-day average volume `< 45%` (`0.45`) of the 11-day post-entry baseline.
- **Liquidity Floor:** Requires baseline volume $\ge 50,000$ shares/day (skips thin/illiquid stocks to avoid noise).
- **Listing Day Exclusion:** Row 0 (entry/listing day) is excluded from baseline calculations to remove structurally inflated volume.

```
volume_ratio < 0.45 AND days_held >= min_days AND -3% <= pnl < 5% AND max_runup < 8%
➔ exit_reason = "Volume Exhaustion - Dead volume (ratio: X.XX, pnl: +Y.Y%, days: N)"
```

**Exit Trigger 4: Secondary Stagnant Position Guard (Global Portfolio Efficiency)**
Regardless of early peak runups or winner archetype status, if a position is held for **$\ge 40$ days** and its **current PnL is $< 10.0\%$**, it is exited to prevent capital lock-in:
```
days_held >= 40 AND current_pnl < 10.0%  ➔  exit_reason = "Time Stop - Stagnant Position (40d)"
```

**`exit_reason` hygiene:** `exit_reason` is set **only on close** (`CLOSED` / `PAPER_CLOSED`). Open positions (`ACTIVE` / `PAPER_ONLY`) must not carry a non-null `exit_reason`; shadow time-stops use `shadow_exit_reason_*` fields only. `upsert_position` clears sticky `exit_reason` on open-status writes.

All thresholds configurable via `.env`:
```
MIN_PNL_FOR_TRAIL=4.0
DEAD_MONEY_DAYS_IPO=20
DEAD_MONEY_RUNUP_IPO=4.0
DEAD_MONEY_DAYS_CONSOL=21
DEAD_MONEY_RUNUP_CONSOL=5.0
DEAD_MONEY_DAYS_OTHER=30
DEAD_MONEY_RUNUP_OTHER=5.0
```

### 7.5 Modular Strategy Backtesting Engine (v3.4.0)
**Location:** `run_latest_rules_backtest.py` (CLI entrypoints: `python manage_db.py backtest` or `python streamlined_ipo_scanner.py backtest`).

Simulates current v3.4.0 production rules across all 675 Mainboard IPO candle datasets in MongoDB to audit strategy health, trade frequency, profit factor, and exit reason distribution.

**Supported CLI Toggles for Isolated Rule Testing:**
```bash
# Test without Volume Exhaustion Early Exit
python run_latest_rules_backtest.py --disable-vol-exit

# Test without 40-day Stagnant Guard
python run_latest_rules_backtest.py --disable-stagnant-guard

# Test without 20d/21d Patience Speed Gates
python run_latest_rules_backtest.py --disable-speed-gates

# Custom Trailing Activation PnL thresholds (IPO / Consolidation)
python run_latest_rules_backtest.py --trail-pnl-ipo 5.0 --trail-pnl-consol 6.0

# Custom Volume Ratio threshold
python run_latest_rules_backtest.py --vol-ratio 0.35
```

---

## 8. Outcome Classification Taxonomy

**Location:** `streamlined_ipo_scanner.py:4167–4182` and `scripts/reconcile_invalid_price.py:67–79`

When a position is closed, it is assigned an `outcome_type`:

| Outcome | Condition |
|---|---|
| `FAST_WINNER` | max_runup_pct > 10% AND days_held <= 5 |
| `SLOW_WINNER` | max_runup_pct > 10% AND days_held > 5 |
| `FAILED_BREAKOUT` | max_runup_pct <= 3% AND max_drawdown_pct <= -3% |
| `IMMEDIATE_FAILURE` | max_runup_pct < 1% AND exited via stop loss |
| `NO_FOLLOW_THROUGH` | max_runup_pct > 3% AND <= 8% — or default fallback |

Additional analytic fields stored on close:
- `holding_efficiency_pct`: `(pnl / max_runup) x 100` — how much of the peak move was captured (only stored when max_runup >= 5%)
- `time_to_failure_days`: calendar days held (for FAILED_BREAKOUT / IMMEDIATE_FAILURE)
- `time_to_failure_min`: `time_to_failure_days x 390` (approximate intraday minutes)

**Shadow stops:** The system runs parallel shadow simulations at 8%, 10%, and 12% stop-loss levels (`shadow_status_8pct`, `shadow_status_10pct`, `shadow_status_12pct`) for research comparison — these do not affect live trades.

---

## 9. Signal Pattern Archetypes (Research Labels)

**Location:** `streamlined_ipo_scanner.py:96–105` and `classify_pattern_type()` at line 168.

Observational labels only — not separate strategies. Stored on signals for analytics.

| Archetype Constant | Meaning |
|---|---|
| `PATTERN_IPO_DISCOVERY` | Listing-day or very early breakout (< 30 days) |
| `PATTERN_CONSOLIDATION_BREAKOUT` | Classic base-building after listing |
| `PATTERN_RUNAWAY_GAP` | Large gap-up confirmation |
| `PATTERN_EARLY_CONTINUATION` | Breakout continuing from a prior base |
| `PATTERN_RECOVERY_BREAKOUT` | Recovering from a significant drawdown |

**Signal Buckets** (research quality filter):

| Bucket | Meaning |
|---|---|
| `ALIGNED` | Meets all current heuristic alpha rules |
| `EXTENDED` | Structurally valid but out-of-sample (for research) |
| `BROKEN` | Structurally flawed or erratic price action |

---

## 10. MongoDB Data Model

**Database:** `ipo_scanner_v2`

### Collections

| Collection | Purpose | Key Unique Index |
|---|---|---|
| `signals` | Every accepted breakout signal | `signal_id` (SHA256 hash) |
| `positions` | Live/closed trade positions | `symbol` (one position per symbol max) |
| `logs` | All scan events (30-day TTL auto-expire) | `log_id` (SHA256 hash) |
| `ipos` | Master IPO registry | `symbol` |
| `listing_data` | Listing-day metadata per symbol | `symbol` |
| `instrument_keys` | Upstox API instrument keys | `ipo_symbol`, `isin` |
| `watchlist` | Intraday watchlist symbols | — |
| `system_audits` | Audit results and reports | — |
| `daily_candles_cache` | Cached OHLCV data | — |

### Position Document Schema (Key Fields)

```json
{
  "symbol": "INOXINDIA",
  "status": "ACTIVE",
  "entry_price": 464.00,
  "entry_date": "2026-06-07",
  "trailing_stop": 417.60,
  "stop_loss": 408.32,
  "target_price": 589.58,
  "grade": "B",
  "pnl_pct": 5.2,
  "days_held": 12,
  "max_runup_pct": 8.1,
  "max_drawdown_pct": -2.1,
  "outcome_type": null,
  "holding_efficiency_pct": null,
  "time_to_failure_days": null,
  "time_to_failure_min": null,
  "position_size_weight": 0.75,
  "version": "3.3.0",
  "strategy_version": "3.3.0-consolidation",
  "winner_label": "STANDARD",
  "shadow_status_8pct": "ACTIVE",
  "shadow_status_10pct": "ACTIVE",
  "shadow_status_12pct": "CLOSED"
}
```

### Log Events Written to `logs` Collection

| Action | Scanner | When |
|---|---|---|
| `REJECTED_BREAKOUT` | consolidation / listing_day | Every filter rejection |
| `ACCEPTED_BREAKOUT` | consolidation | Signal passes all filters |
| `PENDING` | listing_day | Symbol enters observation window |
| `BREAKOUT_SIGNAL` | listing_day | Signal confirmed |
| `POSITION_CLOSED` | positions | Trade exits |
| `DAILY_SNAPSHOT` | positions | Every active position, every trading day |
| `TRAILING_STOP_UPDATED` | positions | When stop actually moves up |
| `SIGNAL_GENERATED` | watchlist | Intraday watchlist signal |
| `SCAN_COMPLETED` | all | Heartbeat — end of each scan run |

### Querying by Version (Cohort Separation)

```python
# Only 3.3.0 positions in daily snapshots:
db.logs.find({"action": "DAILY_SNAPSHOT", "details.position_version": "3.3.0"})

# Legacy 2.5.0 positions still running under new scanner:
db.logs.find({"action": "DAILY_SNAPSHOT", "details.position_version": "2.5.0"})
```

---

## 11. Data Sources and Fetching

**Primary:** Upstox API (via `UPSTOX_ACCESS_TOKEN` — permanent analytics token, no daily login)
**Fallback:** yfinance

**Live price resolution order** (`get_live_price()` at line 1515):
1. Upstox live price (`get_live_price_upstox()`)
2. yfinance live price (`get_live_price_yfinance()`)

**Historical data** (`fetch_data()` at line 1673):
1. Check `daily_candles_cache` collection in MongoDB
2. Fetch from Upstox API (`fetch_from_upstox()`)
3. Fall back to yfinance (`fetch_from_yfinance()`)

**Stale data guard:** Before making any exit decision, the system validates that the latest data date is >= `get_last_expected_data_date()`. If stale, the position update is skipped and a Telegram alert is sent.

**Corporate Action Guard:** Because daily price drops on the NSE/BSE mainboards are strictly limited by daily price bands (typically 20% max), any drop of **>25%** in a single day compared to the previous day's recorded price indicates a corporate action (stock split, bonus issue, extraordinary dividend) or a severe data print issue. When triggered, the system:
1. Skips updating position statistics in MongoDB during `update_positions()` to avoid recording false drawdowns.
2. Suspends trailing-stop/time-stop exit checking during `stop_loss_update_scan()`.
3. Dispatches a high-priority alert to Telegram requesting manual database adjustment.

**Nifty regime data:** Fetched via `utils.fetch_nifty_from_upstox()`, falling back to yfinance `^NSEI`.

---

## 12. Automation Infrastructure

### Scheduling Architecture (Two-Layer)

GitHub's `on: schedule` is delayed by 30 min–3+ hours under load — unsuitable for intraday scanning.

**Layer 1 — Cloudflare Worker** (`cloudflare-dispatcher/src/index.js`):
- Runs every 15 minutes on Cloudflare free tier (Mon–Sat, 03:00–11:00 UTC)
- Calls GitHub `workflow_dispatch` API at exact scheduled times
- Worker URL: `https://ipo-scanner-workflow-dispatcher.mysmarttv558.workers.dev`
- Secret: `GITHUB_PAT` stored as Cloudflare Worker secret (never in code)

**Layer 2 — GitHub Actions:**
- Receives `workflow_dispatch` events and runs the actual Python scanners
- Secrets required: `UPSTOX_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `MONGO_URI`

### Dispatch Schedule

| UTC Time | IST Time | Workflows Dispatched |
|---|---|---|
| 03:45 | 09:15 (market open) | `listing-day-breakout.yml`, `watchlist-hourly-scanner.yml` |
| 04:15–10:45 | Every 30 min, market hours | listing-day + watchlist |
| 08:45 | 14:15 | + `ipo-scanner-v2.yml` (daily consolidation scan) |
| 13:00 | 18:30 | `ipo-scanner-v2.yml` with `mode=stop_loss_update` |

### GitHub Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `listing-day-breakout.yml` | Cloudflare → dispatch | Run listing day scanner |
| `watchlist-hourly-scanner.yml` | Cloudflare → dispatch | Intraday watchlist scan |
| `ipo-scanner-v2.yml` | Cloudflare → dispatch | Daily consolidation scan + stop-loss update |
| `ipo-scanner-v2.yml` (weekly) | GitHub cron (Sun 2:45 PM IST) | Weekly summary |
| `ipo-scanner-v2.yml` (monthly) | GitHub cron (1st 2:45 PM IST) | Monthly review |
| `master-audit-and-verification.yml` | GitHub cron (11:00 PM IST daily) | DB + log integrity audit |
| `biweekly-db-quality-analysis.yml` | GitHub cron (1st & 15th 8:00 PM IST) | DB quality + winner pattern report |

**NSE Holiday Guard:** Complete 2025–2026 holiday calendar hardcoded in `streamlined_ipo_scanner.py`. On holidays, scanner exits cleanly and sends a Telegram notification.

---

## 13. Scanner Entry Points and CLI Commands

```bash
# Main consolidation scanner
python streamlined_ipo_scanner.py scan                    # Daily consolidation scan
python streamlined_ipo_scanner.py stop_loss_update        # EOD stop-loss update pass
python streamlined_ipo_scanner.py weekly_summary          # Weekly performance report (Telegram)
python streamlined_ipo_scanner.py monthly_review          # Monthly performance report (Telegram)
python streamlined_ipo_scanner.py heartbeat               # Health check

# System integrity
python master_audit.py                                    # Full audit (DB + logs + strategy)
python master_audit.py --section 1                        # DB integrity only
python master_audit.py --json                             # JSON output for CI

# DB management
python manage_db.py test                                  # MongoDB connectivity
python manage_db.py backup                                # Export to local JSON
python manage_db.py quality                               # Log structural quality
python manage_db.py analyze                               # Phase 4 data intelligence

# Analytics
python analyze_30d_data.py --start-date 2026-06-07 --version 3.3.0 --clean-cohort
python analyze_db_quality_and_patterns.py
```

---

## 14. Key Configuration Constants (Tunable via `.env`)

All constants use `get_env_int()` / `get_env_float()` helpers with hardcoded defaults.

| Variable | Default | Purpose |
|---|---|---|
| `CONSOL_WINDOWS` | `10,20` | Consolidation window lengths to scan |
| `MAX_PRNG` | 25.0 | Max price range % for ALIGNED bucket |
| `VOL_MULT` | 1.2 | Rolling volume multiplier threshold |
| `ABS_VOL_MIN` | 3,000,000 | Absolute volume minimum |
| `MAX_DAYS` | 200 | Max days post-listing to scan |
| `LISTING_MAX_DAYS_SINCE_LISTING` | 730 | Max IPO age for listing-day scanner |
| `MIN_AGE_DAYS` | 60 | Min post-listing age before consolidation scan |
| `MIN_DAILY_TURNOVER_CR` | 1.0 | Min daily turnover in Crore |
| `MIN_DAILY_TURNOVER_SMALL_CAP_CR` | 0.75 | Small-cap turnover floor |
| `SMALL_CAP_THRESHOLD_CR` | 3,000 | Threshold to classify as small-cap |
| `MIN_MARKET_CAP_CR` | 500 | Min market cap in Crore |
| `CIRCUIT_DAY_THRESHOLD` | 3 | Max circuit days in 15 sessions |
| `MIN_ENTRY_PRICE_RS` | 25.0 | Min entry price in Rs |
| `MAX_ENTRY_ABOVE_BREAKOUT_PCT` | 8.0 | Max % above breakout to accept entry |
| `MIN_RISK_REWARD` | 1.3 | Min risk:reward ratio |
| `MIN_PNL_FOR_TRAIL` | 4.0 | Min P&L % before trailing starts (3.0% for `LISTING_BREAKOUT`) |
| `MIN_TRAIL_MOVE_PCT` | 1.0 | Min % improvement to count as a trail update |
| `MIN_DAYS_BETWEEN_SIGNALS` | 10 | Signal cooldown per symbol |
| `MIN_LIVE_GRADE` | B | Minimum grade for live signal emission |
| `MAX_ACTIVE_POSITIONS` | 5 | Soft portfolio cap |
| `HARD_ACTIVE_POSITIONS` | 7 | Hard portfolio cap |
| `DEAD_MONEY_DAYS_IPO` | 20 | Patience stop days for listing breakouts |
| `DEAD_MONEY_RUNUP_IPO` | 4.0 | Min runup % to avoid patience stop (IPO) |
| `DEAD_MONEY_DAYS_CONSOL` | 21 | Patience stop days for consolidation |
| `DEAD_MONEY_RUNUP_CONSOL` | 5.0 | Min runup % to avoid patience stop (consolidation) |
| `DEAD_MONEY_DAYS_OTHER` | 30 | Fallback patience stop days |
| `PT_A_PLUS` | 0.15 | Partial take level for A+ grade |
| `PT_B` | 0.12 | Partial take level for B grade |
| `PT_C` | 0.10 | Partial take level for C grade |
| `REGIME_SIZE_MULT_BULL` | 1.0 | Position size weight in BULL regime |
| `REGIME_SIZE_MULT_WEAK_BULL` | 0.75 | Position size weight in WEAK_BULL |
| `REGIME_SIZE_MULT_CORRECTION` | 0.5 | Position size weight in CORRECTION |
| `REGIME_SIZE_MULT_RANGE` | 0.5 | Position size weight in RANGE |

---

## 15. Modular Analytics Layer

### IntegrationBridge (`streamlined_ipo_scanner.py:50`)
Bridge between the main scanner loop and the v2 institutional telemetry layer (`core/`, `enrichment/`, `integration/`). If the bridge fails to import, it silently degrades — the main scanner continues without institutional telemetry.

### Enrichment Layer (`enrichment/`)
Provides point-in-time feature enrichment:
- `base.py` — Base character metrics (PRNG, base width, structure)
- `breakout.py` — Breakout quality metrics (volume ratio, gap-up %)
- `market.py` — Market context (Nifty slope, RSI, sector state)
- `engine.py` — Orchestrates enrichment pipeline

### Lifecycle Layer (`lifecycle/`)
- `tracker.py` — Tracks PnL evolution of positions over time
- `evaluator.py` — `evaluate_signal_outcome()` — synthetic outcome reconstruction from historical data

---

## 16. System Integrity and Audit Layer

### Master Audit (`master_audit.py`)
Run daily at 11 PM IST. Four sections:

| Section | Checks |
|---|---|
| 1: DB Integrity | Orphan signals, inverted stops/targets, zero entry prices, duplicate signal IDs, unrealistic P&L |
| 2: Log Quality | SCAN_COMPLETED heartbeats, rejection ratios, version drift, DAILY_SNAPSHOT coverage |
| 3: Strategy Consistency | Version alignment across all files, sector population, entry-vs-breakout guard, enrichment completeness |
| 4: Price Existence | Validates entry price against historical candle data |

Exit codes: `0` = PASS, `1` = WARN, `2` = FAIL

### Nightly DB Audit (`scripts/nightly_db_audit.py`)
Runs at 11 PM IST:
- Status sync: active signals vs active positions (1-to-1 verification)
- Numerical schema: scans for NaN/Null corruptions
- Stale positions: flags ACTIVE positions not updated in > 48 hours
- Snapshot duplication detection
- PnL discontinuity: flags single-day price jumps > 40%

---

## 17. Version History and Experiment Baselines

| Version | Date | Key Changes |
|---|---|---|
| **v3.4.0** | 2026-07-18 | Re-entry breakouts (`peak_price_during_trade`), volume exhaustion exit, trailing dead-zone closure (`MIN_PNL_FOR_TRAIL` 4%), Tier B base breakouts, doc/workflow parameter alignment |
| **v3.3.0** | 2026-06-07 | Listing volume floor (>=150k shares), base-duration floor (>=3d), 20-day patience stop, Limit Buy alerts, `position_version` log field for cohort separation |
| **v3.3.0** *(param update)* | 2026-07-05 | `CONSOL_WINDOWS` narrowed to `10,20` only (40/80/120d avg -7.65% across 64-trade history); `MIN_LIVE_GRADE` raised from `C` to `B` (Grade C avg -2.64%, median -5.55%) |
| **v2.5.0** | 2026-04-23 | MongoDB-only architecture, winner trait classification, forensic audit mode, master_audit.py |
| **v2.4.0** | 2026-04-15 | Modular enrichment layer, lifecycle PnL reconstruction |
| **v2.3.0** | 2026-04-01 | Institutional analytics research layer |

**Clean analysis baseline (recommended):**
```bash
python analyze_30d_data.py --start-date 2026-06-07 --version 3.3.0 --clean-cohort
```
`--clean-cohort` excludes `signal_type == WATCHLIST` and grades containing `LOW_VOL`.

`EXPERIMENT_CHANGELOG.md` tracks all analysis cutover points.

---

## 18. Algorithm Performance State

> As of 2026-07-05, the system is in early live-running phase under v3.3.0 (launched 2026-06-07).
> The local `signals_performance_report.json` and `monthly_system_report.json` files show all-zero values — live performance data lives exclusively in **MongoDB Atlas**.

The system measures its own performance via:
- `analyze_db_quality_and_patterns.py` — biweekly report: win rate by grade, R:R, expectancy, scan window comparison
- `analyze_30d_data.py` — 30-day rolling quantitative analysis
- `weekly_audit_report.py` — automated weekly audit report
- `holding_efficiency_pct` on every closed position — measures how much of peak move was captured
- Shadow stop simulations (8/10/12%) — parallel research into tighter/looser stop configurations

**Designed feedback questions (answerable after 30+ closed trades):**
- Win rate per grade (A+/B/C)?
- Is the trailing stop too tight? (look at `holding_efficiency_pct` < 40% as warning)
- Which rejection filter eliminates the most candidates?
- Do low-volume breakouts (`LISTING_BREAKOUT_LOW_VOL`) underperform full confirms?
- Which scan windows (10d/20d/40d) perform best?

---

## 19. Development Conventions

### Adding a New Entry Filter
1. Add rejection to the waterfall in `detect_scan()` (line 3186) or `detect_live_patterns()` (line 2138)
2. Log it: `write_daily_log("consolidation", sym, "REJECTED_BREAKOUT", {"reason": "NEW_REASON_CODE"}, log_type="REJECTED")`
3. Add the reason to the rejection table in `README.md`
4. Update `master_audit.py` section 3 if the filter is strategy-critical

### Adding a New Position Field
1. Add the field to `upsert_position()` in `db.py` (line 193)
2. Add schema backward compatibility in `stop_loss_update_scan()` (schema_cols initialization at line 3892)
3. Add to `DAILY_SNAPSHOT` log payload so the field is tracked over time
4. Run `scripts/stamp_active_versions.py` to backfill existing documents if needed

### Changing a Stop-Loss or Timing Threshold
1. Add or update the `.env` variable at the top of `streamlined_ipo_scanner.py` using `get_env_int()` / `get_env_float()` — no hardcoded magic numbers
2. Update the default in the function call
3. Update `README.md` configuration table
4. Add an entry to `EXPERIMENT_CHANGELOG.md` with the date — this creates a new analysis cohort boundary

### Running a One-Time Backfill or Migration
Use scripts in `scripts/`:
- `scripts/stamp_active_versions.py` — backfill version fields on existing documents
- `scripts/migrate_data_model.py` — reconstruct `outcome_type` for old closed positions
- `scripts/reconcile_invalid_price.py` — re-fetch prices and reclassify outcomes

### Changing the Dispatch Schedule
Edit `cloudflare-dispatcher/src/index.js` → `SCHEDULE` object, then deploy:
```bash
cd cloudflare-dispatcher
npx wrangler deploy
```

---

## 20. Required Secrets and Environment Variables

| Variable | Required? | Purpose |
|---|---|---|
| `MONGO_URI` | Required | MongoDB Atlas connection string |
| `UPSTOX_ACCESS_TOKEN` | Required for live trading | Permanent analytics token |
| `TELEGRAM_BOT_TOKEN` | Required for alerts | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Required for alerts | Target chat/channel ID |
| `GITHUB_PAT` | Required in Cloudflare | GitHub Personal Access Token (workflow scope) |

All other variables have safe defaults and are optional overrides.
