# 🚀 IPO Breakout Qualification Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Version](https://img.shields.io/badge/version-2.5.0-orange.svg)](https://github.com/Deep-Adhia/IPO-Base-Scanner)
[![Automated](https://img.shields.io/badge/automation-GitHub%20Actions-green.svg)](https://github.com/features/actions)

This is **not** a simple breakout scanner.

It is a behavior-driven IPO momentum qualification system that participates **only in confirmed breakouts** with structural and volume validation. The system ruthlessly filters out market noise, grading high-quality setups while explicitly tracking every rejection — so the data can be analysed 30 days later to continuously refine the edge.

---

## 🧭 Strategy Philosophy

This system **does not predict breakouts — it validates them.**

It only participates in moves where demand is already proven through sustained price behaviour and volume expansion. By forcing the market to show its hand first, false signals are eliminated structurally rather than by intuition.

---

## 🎯 Scanner Architecture

The system runs **two core scanners** plus an intraday watchlist scanner.

### 1. 📅 Listing Day Breakout Scanner (`listing_day_breakout_scanner.py`)

Targets the **listing day** and the days immediately following, when momentum is freshest and institutional footprints are most visible.

**Flow:**
1. **Symbol detected on listing day** → enters `PENDING` observation state.
2. **Behaviour observation** → system monitors price action for 45–60 minutes post-breakout.
3. **Rejection** → terminated instantly if price falls back below breakout level or rejection tail exceeds the mathematical threshold.
4. **`CONFIRMED` execution** → only mathematically confirmed breakouts generate actionable signals.

> **Time Decay Filter**: If a confirmed breakout fails to sustain ≥1.5% away from the breakout level within a defined window (60–90 min), it is treated as a "dead breakout" and silently rejected.

---

### 2. 🔁 Consolidation Breakout Scanner (`streamlined_ipo_scanner.py`)

Targets IPOs **10–200 days post-listing** that have built a proper base structure and are breaking out of that base.

**Scan windows:** `10, 20, 40, 80, 120` days (configurable)

**Flow:**
1. Symbol must be within `8–35%` below listing-day high (base formation range).
2. Consolidation range must be `≤60%` (tight base, not chop).
3. Breakout candle must **close** above the base high (no wick fakes).
4. Volume must confirm via one of: `2.5x avg burst`, `VOL_MULT (1.2x)` rolling, or absolute `3M+ value`.
5. Follow-through filter: next candle must hold base high ±2% **or** show 80%+ continuation volume.
6. Grade and R:R checks filter further before signal emission.

---

### 3. ⏱️ Watchlist Hourly Scanner (`hourly_breakout_scanner.py`)

Monitors active watchlist symbols intraday and emits fast breakout alerts during market hours.
It is a tactical alerting layer and writes structured JSONL logs to the same daily log path.

---

## 🚫 Rejection Logic (Critical Filters)

The system rejects aggressively. A setup is terminated at the first failing condition:

| Filter | Reason Logged |
|---|---|
| Price below `₹20.00` | `too_cheap` — avoid penny stock manipulation |
| Daily Turnover `< ₹2.0 Cr` | `LIQUIDITY_TRAP` — avoid capital lock-in |
| Market Cap `< ₹500 Cr` | `MICROCAP_PENALTY` — high manipulation risk |
| `3+` Circuit Days in 15 sessions | `LIQUIDITY_TRAP_CIRCUITS` — prevent trap reversals |
| Price outside `8%–35%` of listing high | Outside base formation range |
| Consolidation range `>60%` | `loose_base` — chop, not accumulation |
| Failed follow-through | `failed_follow_through` |
| Grade below minimum (`C` by default) | `low_grade` |
| Risk:Reward ratio `< 1.3` | `poor_risk_reward` |
| Entry `>8%` above breakout level | `too_extended` |
| Stop Loss `>10.0%` risk from entry | `excessive_stop_risk` |
| Breakout `>10` days old | `stale_breakout` |
| Cooldown (`<10` days since last signal) | `cooldown` |
| Symbol already in active position | Silent skip (no duplicate positions) |
| Market holiday (NSE calendar) | Scanner exits cleanly |

*Most symbols are rejected. Only the highest-quality setups generate signals.*

---

## 📊 Grading System

Grades are assigned by the `compute_grade_hybrid()` scoring function (5 criteria, max score 5):

**Note on terminology**
- `Grade` (consolidation scanner) and `Tier` (listing breakout engine) are independent scoring systems.
- `Grade` measures consolidation/base quality.
- `Tier` measures breakout quality and position sizing allocation.

| Grade | Score | Min Confidence | Position Bias |
|---|---|---|---|
| **A+** | 4–5 | Very High (91%) | Full size |
| **B** | 2–3 | Medium-High (75%) | Reduced + smart filters |
| **C** | 1 | Medium (65%) | Min size — monitor closely |
| **D** | 0 | Rejected | ❌ Never traded |

> **Microcap Penalty (Phase 2.5)**: Any symbol with a Market Cap < ₹1000 Crore is automatically capped at **Grade C**, regardless of technical base quality, to enforce strict risk management on smaller counters.

**5 scoring criteria:**
1. Consolidation range `≤18%` (tight base = institutional accumulation)
2. Massive volume — breakout day `≥2.5x` avg + 3-day sum `≥4x` avg
3. Momentum percentile — 20-day return in top 85th percentile
4. Technical alignment — MACD bullish + RSI `>65` + EMA20 above EMA50
5. Gap-up confirmation — next open `≥4%` above breakout close

---

## 🔁 Learning & Feedback Loop

Every rejection and signal is written to a **structured daily JSONL log**, building the dataset for algorithm tuning:

```
logs/
  YYYY-MM-DD/
    consolidation.jsonl    ← REJECTED_BREAKOUT + ACCEPTED_BREAKOUT events
    listing_day.jsonl      ← PENDING / CONFIRMED / BREAKOUT_SIGNAL events
    watchlist.jsonl        ← Hourly watchlist SIGNAL_GENERATED + REJECTED_BREAKOUT + SCAN_COMPLETED
    positions.jsonl        ← POSITION_CLOSED + DAILY_SNAPSHOT + TRAILING_STOP_UPDATED
```

Each JSONL entry is structured containing a flattened, Pandas-ready snapshot of all technical components:
```json
{
  "timestamp": "2026-05-08 14:14:00 IST",
  "version": "2.5.0",
  "log_schema_version": "2026-04-23.v1",
  "scanner": "consolidation",
  "symbol": "INOXINDIA",
  "action": "REJECTED_BREAKOUT",
  "log_type": "REJECTED",
  "details": {
    "rejection_reason": "low_volume",
    "failing_metric": "vol_ratio",
    "failing_value": 0.95,
    "threshold": 1.2,
    "metrics": {
      "perf": -0.12,
      "prng": 15.5,
      "vol_ratio": 0.95,
      "rsi": 62.4,
      "score": 3
    }
  }
}
```

*Because every log includes a standardized `metrics` snapshot and explicit `failing_metric` attribution, you can build high-fidelity distributions of near-misses to mathematically optimize your filters.*

Daily summary snapshots may also be generated as:

```text
logs/YYYY-MM-DD/daily_summary.json
```

This file is **derived output**, not a source-of-truth input. The source remains JSONL logs.

After 30 days this dataset allows answering:
- Which grades actually hit their targets (win rate per grade)?
- Are we exiting too early (trailing stop too tight)?
- Which rejection reason filters out the most candidates?
- Do low-volume breakouts (`LISTING_BREAKOUT_LOW_VOL`) underperform full confirms?

---

## 📂 Data Infrastructure

```text
IPO-Base-Scanner/
├── streamlined_ipo_scanner.py       # Consolidation breakout scanner (v2.5.0)
├── listing_day_breakout_scanner.py  # Listing day breakout scanner
├── hourly_breakout_scanner.py       # Intraday watchlist scanner
│
├── db.py                            # Core MongoDB persistence layer (v2.5.0)
├── fetch.py                         # Data acquisition (Upstox + YFinance)
├── master_audit.py                  # System integrity audit (Section 1/2/3)
├── manage_db.py                     # Unified management entrypoint
│
├── [Legacy — DELETED/DEPRECATED]
│   ├── *.csv                        # All CSV files removed for MongoDB-only operation
│   ├── *.pkl                        # Legacy pickle caches removed
│
└── logs/                            # derive output summary files (optional)
```

**⚠️ ARCHITECTURAL FREEZE**: As of v2.5.0, the system is strictly **MongoDB-only**. All CSV fallback paths have been purged to ensure a stationary, high-fidelity quant baseline.
```

- **Phase 3**: 3-Day Live Validation (Zero Failure Cutover).
- **Phase 4**: Data Intelligence & Edge Extraction (Filter Optimization).

---

## 🧠 Institutional Analytics & Forensic Research

Starting with **v2.3.0** the system grew a dedicated research layer. **v2.5.0** adds a self-auditing infrastructure layer on top.

### 🏛️ The Modular Architecture (v2.4.x)

| Component | Path | Responsibility |
|---|---|---|
| **Core** | `core/` | Immutable data models with Sector/Industry tracking. |
| **Enrichment** | `enrichment/` | Feature Store: Point-in-time Market context, Breakout & Base character. |
| **Lifecycle** | `lifecycle/` | PnL evolution and Synthetic Outcome Reconstruction. |
| **Integration** | `integration/` | Cross-scanner Bridge (Consolidation + Listing Day). |
| **Research** | `analyze_winning_traits.py` | Alpha Trait Discovery & Pattern Fingerprinting. |

### 🧪 Alpha Research & Trait Discovery
Starting with **v2.4.0**, the system enables forensic backtesting of historical signals to identify the "DNA" of winning setups.

1.  **Synthetic Reconstruction (`reconstruct_outcomes.py`)**: Walks forward through historical data to objectively calculate Max Run-up and Drawdown for past signals.
2.  **Point-in-Time Enrichment**: Ensures historical signals are enriched with the *actual* market context (Nifty slope, RSI) from the date of the trade, not current data.
3. **Sector Decoupling Analysis**: Tracks performance by Industry Group to identify "Oversold Decoupling" — setups that thrive even during market stress.

---

### 🔍 System Integrity Audit (`master_audit.py`)

Added in **v2.5.0** — a standalone daily/weekly audit with three sections:

| Section | What it checks |
|---|---|
| **1: DB Integrity** | Orphan signals, inverted stops/targets, zero entry prices, duplicate signal IDs, unrealistic PnL |
| **2: Log Quality** | SCAN_COMPLETED heartbeats, rejection ratios, version drift in logs, DAILY_SNAPSHOT coverage |
| **3: Strategy Consistency** | Version alignment across all files, sector population, entry-vs-breakout guard, enrichment completeness |
| **4: Price Existence** | Validates entry price against historical candle data (supports next-day execution) |

```bash
python master_audit.py             # Full audit
python master_audit.py --section 1 # DB integrity only
python master_audit.py --json      # JSON output for CI
```

Exit codes: `0` = PASS · `1` = WARN · `2` = FAIL

> The audit is aware of all three signal statuses (`ACTIVE`, `CLOSED`, `WATCH`) and excludes watchlist
> candidates from checks that only apply to executed trade signals.

### 🏆 Winner Trait Classification (`winner_label` field)

Added in **v2.5.0** — every new consolidation breakout signal is automatically tagged with a `winner_label` derived from empirical DB analysis (marked as experimental due to small sample size):

| Label | Criteria met | Meaning |
|---|---|---|
| `POSSIBLE_WINNER_EXPERIMENTAL` | 4–5 out of 5 | Matches the fingerprint of top-performing setups (experimental - verify manually) |
| `STANDARD` | 2–3 out of 5 | Meets minimum bar, trade with normal sizing |
| `WATCHLIST_ONLY` | 0–1 out of 5 | Weak setup — paper trade only |

**The 5 winner criteria** (derived from 2026-05-21 DB analysis):
1. Grade B or better
2. Consolidation window 10 or 20 days
3. Volume ratio ≥ 1.5x at breakout
4. Tight base (consolidation range < 18%)
5. Nifty trend slope > 0 (bullish regime at signal time)

The label appears in the MongoDB `signals` document and is highlighted in the Telegram alert:
```
🎯 CONSOLIDATION BREAKOUT SIGNAL
🏆 POSSIBLE WINNER (experimental — based on thin sample, verify manually) — Matches 5/5 winner criteria
   ✅ grade_B_or_better, window_10_or_20, volume_ratio_gte_1_5, tight_base_lt_18pct, nifty_bullish_slope
```

### 📊 Biweekly DB Quality & Winner Pattern Analysis (`analyze_db_quality_and_patterns.py`)

Runs every 1st and 15th of the month via GitHub Actions. Generates a report covering:
- Field coverage and data completeness across all collections
- Closed-trade performance: win rate, avg win/loss, reward:risk, expectancy
- Grade performance breakdown (A+/B/C)
- Market context at signal time (Nifty regime, slope distribution)
- Scan window performance (10/20/40/80 day comparison)
- Active positions risk snapshot (% to stop-loss)

```bash
python analyze_db_quality_and_patterns.py  # Run locally at any time
```

---

### 🔍 Forensic Audit Workflow

Every scan now concludes with a **Forensic Blueprint** in the terminal. This provides a **Trust Score** and specific Signal IDs for manual "Ground Truth" validation:
- **`CLEAN_BREAKOUT`**: Textbook case for baseline validation.
- **`HIGH_VOL` / `HIGH_DELTA`**: Edge cases for math and slippage verification.
- **`FIRST_INCOMPLETE`**: Failure attribution for systematic error detection.

---

---

## 🛠️ Installation & Configuration

### 1. Setup Environment
```bash
git clone https://github.com/Deep-Adhia/IPO-Base-Scanner.git
cd IPO-Base-Scanner
pip install -r requirements.txt
cp .env.template .env
```

### 2. Configure Database
Create a free cluster on [MongoDB Atlas](https://www.mongodb.com/cloud/atlas) and add your connection string to `.env`:
```bash
MONGO_URI=mongodb+srv://user:pass@cluster.mongodb.net/?appName=stock-tracker
```
Verify connectivity:
```bash
python manage_db.py test
```

### 3. Configure Data Sources

⚠️ **IMPORTANT**: The Upstox API is **required** for live price confirmation during market hours. Fallback sources (NSE/YFinance) are used only for historical data outside market hours.

```bash
# .env — Upstox Analytics token (permanent, no daily login required)
UPSTOX_ACCESS_TOKEN=your_analytics_token_here

# Telegram alerts
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Tunable parameters (see .env.template for all options)
MIN_LIVE_GRADE=C         # Minimum grade for signal emission (D/C/B/A/A+)
MIN_RISK_REWARD=1.3      # Minimum R:R ratio
MIN_DAYS_BETWEEN_SIGNALS=10   # Cooldown window per symbol
CONSOL_WINDOWS=10,20,40,80,120
```

### 4. Run Manually
```bash
# Run system integrity audit
python master_audit.py             # Full audit (DB + logs + strategy)
python master_audit.py --section 1 # DB integrity only
python master_audit.py --json      # JSON output for CI

# Run consolidation scan
python streamlined_ipo_scanner.py scan

# Run infrastructure tasks
python manage_db.py test           # Check MongoDB connectivity
python manage_db.py backup         # Export MongoDB to local JSON
python manage_db.py quality        # Analyze log structural quality
python manage_db.py analyze        # Run Phase 4 Data Intelligence

# Update stop-losses on active positions
python streamlined_ipo_scanner.py stop_loss_update

# Weekly / monthly summaries (Telegram)
python streamlined_ipo_scanner.py weekly_summary
python streamlined_ipo_scanner.py monthly_review
```

### 5. Automation Deployment (GitHub Actions)
Add to GitHub Repository Secrets: 
- `UPSTOX_ACCESS_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `MONGO_URI` (Atlas connection string)

**Infrastructure Health:**
Every workflow run now includes a **"Check MongoDB Connection"** step. If this fails, check your Atlas IP Whitelist (allow `0.0.0.0/0` for GitHub runners).

Primary workflows:
- `ipo-scanner-v2.yml` — consolidation scanner (`scan`, `stop_loss_update`, weekly/monthly summaries)
- `listing-day-breakout.yml` — listing-day breakout scanner
- `watchlist-hourly-scanner.yml` — hourly watchlist breakout scanner
- `master-audit-and-verification.yml` — daily system integrity audit and verification suite
- `biweekly-db-quality-analysis.yml` — biweekly DB quality & winner pattern analysis report

Automated schedules (IST):
| Job | Time | Cron (UTC) |
|---|---|---|
| Daily scan + stop-loss update | 2:15 PM weekdays | `45 08 * * 1-5` |
| Weekly summary | Sunday 2:45 PM | `15 09 * * 0` |
| Monthly review | 1st of month 2:45 PM | `15 09 1 * *` |
| Daily system integrity audit & verifications | Daily at 11:00 PM | `30 17 * * *` |
| Biweekly DB quality & winner pattern analysis | 1st & 15th at 8:00 PM | `30 14 1,15 * *` |

> **NSE Holiday Guard**: The scanner automatically skips NSE public holidays (full 2025–2026 calendar enforced in code). A Telegram notification is sent when a day is skipped.

---

## 📈 Quantitative Analysis (30-Day)

Run:

```bash
python analyze_30d_data.py
```

Recommended for clean-window analysis (non-destructive filters):

```bash
python analyze_30d_data.py --start-date 2026-04-24 --version 2.1.0 --clean-cohort
```

The analysis script now uses a resilient read order for rejection metrics:

1. Prefer `logs/YYYY-MM-DD/daily_summary.json` when available.
2. If missing/empty (common on fresh local pull), automatically fallback to parsing:
   - `logs/YYYY-MM-DD/consolidation.jsonl`
   - `logs/YYYY-MM-DD/listing_day.jsonl`
   - `logs/YYYY-MM-DD/watchlist.jsonl`

This means you can run analysis locally even if CI-generated summary files are not present in your branch.
It also supports optional `--start-date`, `--version`, `--rejection-days`, and `--clean-cohort` filters so old rows are excluded without deleting historical data.

For experiment cutovers and baseline tracking, see `EXPERIMENT_CHANGELOG.md`.

---

## 📱 Alert Format (Telegram)

```text
🎯 IPO BREAKOUT SIGNAL

📊 Symbol: SAATVIKGL
⭐ Grade: A (High Confidence)

💰 Price Information:
• Breakout Close (Reference): ₹446.90
• Entry Price (Logged): ₹464.00
• Price Source: Upstox Live
• Entry Type: LIVE_INTRADAY — execution price may differ from breakout close.

🛑 Stop Loss: ₹408.32 (12.0% risk)
🎯 Target: ₹589.58 (27.1% reward)
📊 Risk:Reward: 1:2.3

📋 Pattern Details:
• Consolidation: ₹408.32 – ₹446.90
• Breakout: ₹446.90
• Score: 1.0/5
• Consolidation Window: 80 days

🤖 Scanner v2.5.0 | 2026-05-08 14:15 IST
```

---

## ⚠️ System Discipline & Compliance

- **1 stock = 1 position**: If a symbol already has an active position, new signals for that symbol are ignored at the scan level.
- **NSE holiday-aware**: Will not scan on market holidays — no stale-price signals.
- **Manual execution only**: The engine calculates, grades, and alerts. It does **not** place API orders. Human oversight is required for every execution.
- **Educational/Analytical Tool**: Not financial or investment advice.

---

<sub>Built for systematic IPO momentum trading | v2.5.0 | Automated via GitHub Actions | MongoDB Atlas Infrastructure | Data-Driven Filter Optimization</sub>
