# Experiment Changelog

This file tracks analysis and logging cutovers so experiment windows stay comparable.

## Current Active Baseline

- `scanner_version`: `3.3.0`
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
| `2026-07-10` | `3.3.0` | **Corporate Action Guard:** Detects >25% single-day price drops and suspends database updates and exit checking for the position to prevent false stop-loss triggers.                                                           |
| `2026-07-05` | `3.3.0` | **Param tightening:** `CONSOL_WINDOWS` narrowed to `10,20` only; `MIN_LIVE_GRADE` raised from `C` to `B`. Based on 64-trade closed-trade analysis (Grade C avg -2.64%/median -5.55%; 40d avg -7.65%). New clean-cohort baseline. |
| `2026-06-07` | `3.3.0` | Listing volume floor (≥150k), base-duration guard fix, 20-day patience stop, Limit Buy alerts, `position_version` log field                                                                                                      |
| `2026-04-23` | `2.5.0` | MongoDB-only architecture, forensic audit, winner trait classification                                                                                                                                                           |
| `2026-04-21` | `2.4.x` | Granular telemetry integration for consolidation                                                                                                                                                                                 |
| `2026-04-15` | `2.4.0` | Lifecycle logging additions in positions pipeline                                                                                                                                                                                |
| `2026-04-01` | `2.3.0` | Institutional analytics research layer                                                                                                                                                                                           |

The next clean-cohort analysis run should use --start-date 2026-07-05 to isolate signals generated under these tightened parameters.
