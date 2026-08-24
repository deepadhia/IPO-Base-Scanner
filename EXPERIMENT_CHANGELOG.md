# Experiment Changelog

This file tracks analysis and logging cutovers so experiment windows stay comparable.

## Current Active Baseline

- `scanner_version`: `3.4.0`
- `log_schema_version`: `2026-04-23.v1`
- recommended clean analysis start: `2026-07-05` (param tightening: CONSOL_WINDOWS=10,20, MIN_LIVE_GRADE=B)

## Why this exists

- Strategy and logging logic evolve over time.
- Comparing pre-change and post-change rows in one bucket can pollute results.
- This changelog provides explicit cut points for analysis filters.

## Analysis command (clean cohort)

```bash
python analyze_30d_data.py --start-date 2026-06-07 --version 3.3.0 --clean-cohort
```

`--clean-cohort` excludes:

- `signal_type == WATCHLIST`
- grades containing `LOW_VOL`

## Querying mixed-version position logs

After the v3.3.0 bump, all new log entries carry `version = "3.3.0"` in the logs collection.
Positions that were opened under `2.5.0` will still emit `3.3.0` log entries — but the log
payload now includes `position_version` so you can cleanly separate cohorts:

```python
# MongoDB — only 3.3.0 positions in daily snapshots
db.logs.find({"action": "DAILY_SNAPSHOT", "details.position_version": "3.3.0"})

# MongoDB — legacy 2.5.0 positions still running under new scanner
db.logs.find({"action": "DAILY_SNAPSHOT", "details.position_version": "2.5.0"})
```

## Notable milestones

| Date         | Version | Change                                                                                                                                                                                                                           |
| ------------ | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `2026-08-25` | `3.4.0` | **Critical signal-book fixes:** (1) listing/re-entry positions now persist `grade=LISTING_BREAKOUT` so IPO exit/trail gates apply; (2) hourly never overwrites open IPO rows + respects soft cap; (3) listing `has_active_position` + soft/hard cap parity; (4) strict mode volume spike required for `BASE_BREAKOUT`; (5) hourly volume tied to breakout bar (no prior-bar borrow); (6) live consol enforces `MIN_LIVE_GRADE`. Docs aligned. |
| `2026-08-25` | `3.4.0` | **Hourly volume gate:** Intraday alerts now hard-require ≥1.5x volume (skip incomplete/zero last candle); rejects `no_volume_confirmation` instead of AZAD-style 0.0x price+RSI alerts. **Weekly perf gate:** soft goals use edge cohort only (`ex-INTRADAY`); intraday stats reported as info. |
| `2026-08-24` | `3.4.0` | **Weekly audit false CRITICAL:** `upsert_position` no longer `$unset`s live open metrics (`max_runup_pct`/`pnl_pct`/`days_held`). Shadow audit uses peak fallback; weekly `--fix` restores wiped metrics, lifts below-floor shadows only when peak is reliable, and closes duplicate ACTIVE intraday signals. |
| `2026-08-08` | `3.4.0` | **exit_reason hygiene:** Open positions (`ACTIVE`/`PAPER_ONLY`) no longer persist sticky `exit_reason`; field is set only on close. Shadow reasons stay in `shadow_exit_reason_*`. No strategy/PnL change. |
| `2026-07-18` | `3.4.0` | **Volume Exhaustion Exit:** Added early exit for flat stagnant positions (`-3% <= PnL < +5%`, `runup < 8%`) when post-entry volume decays `< 45%` vs 11-day baseline (excluding Day 0 listing volume). **Trailing Dead Zone Closure:** Lowered `MIN_PNL_FOR_TRAIL` to 4% (3% for `LISTING_BREAKOUT`). **Modular Backtest Engine:** Integrated `run_latest_rules_backtest.py` (`python manage_db.py backtest`) with rule isolation CLI flags (`--disable-vol-exit`, `--disable-stagnant-guard`, `--disable-speed-gates`, `--vol-ratio`). Quant audit across 675 IPOs confirmed +1.18% avg return per trade, 1.25 Profit Factor, and proved naive 5% Re-entry causes 335% trade churn (rejected). |
| `2026-07-11` | `3.4.0` | **Re-Entry Breakouts:** Added tracking for `peak_price_during_trade`. Allows stopped-out valid setups to trigger a Re-Entry if they cross the peak again within 30 days. Re-entries bypass DNA filters but enforce liquidity thresholds, yielding +76.8% absolute port return in backtests. Implemented `PAPER_ONLY` caps for re-entries. |
| `2026-07-10` | `3.3.0` | **Corporate Action Guard:** Suspends exits on >25% drops to prevent false stop triggers. **Breakout Volume Floor:** Volume floor (≥150k) now checks breakout-day volume instead of Day 0. **Stagnant Position Guard:** Exits trades held ≥40d with PnL <10%. |
| `2026-07-05` | `3.3.0` | **Param tightening:** `CONSOL_WINDOWS` narrowed to `10,20` only; `MIN_LIVE_GRADE` raised from `C` to `B`. Based on 64-trade closed-trade analysis (Grade C avg -2.64%/median -5.55%; 40d avg -7.65%). New clean-cohort baseline. |
| `2026-06-07` | `3.3.0` | Listing volume floor (≥150k), base-duration guard fix, 20-day patience stop, Limit Buy alerts, `position_version` log field                                                                                                      |
| `2026-04-23` | `2.5.0` | MongoDB-only architecture, forensic audit, winner trait classification                                                                                                                                                           |
| `2026-04-21` | `2.4.x` | Granular telemetry integration for consolidation                                                                                                                                                                                 |
| `2026-04-15` | `2.4.0` | Lifecycle logging additions in positions pipeline                                                                                                                                                                                |
| `2026-04-01` | `2.3.0` | Institutional analytics research layer                                                                                                                                                                                           |

## Parking lot (deferred — needs larger N)

Do **not** enable these on thin post-exit samples (~5 realized / ~22 dead-money / ~9 trail-winner paths). Prefer missed edge over a backfired rule.

| Idea | Why deferred |
|---|---|
| Underwater dead-money (day 12 / −6%) | Can cut recoveries; needs full-history backtest + shadow |
| Peak chandelier trail for runup ≥15% | n=9 trail winners; risk of larger givebacks |
| Consol HQ → ACTIVE | Capital risk; keep force-paper OOS |
| Retune 20d/21d dead-money | Already looks good; do not retune on thin data |
| New Early Base Break logic | Legacy label; not in live exit code |

The next clean-cohort analysis run should use --start-date 2026-07-05 to isolate signals generated under these tightened parameters.
