# Listing Day Breakout Scanner - Logic Summary (v2.5.0)

## Strict Quality Mode (Default — “Good Trades Only”)

The Listing Day Breakout Scanner runs in strict quality mode (`LISTING_STRICT_QUALITY=true` by default) to filter out market noise and capture only pristine institutional setups.

| Check | Default Setting (Strict Mode) |
| :--- | :--- |
| **Max Days Since Listing** | ≤ `LISTING_MAX_DAYS_SINCE_LISTING` (**730 days / 2 years**) |
| **Volume vs 10d Avg** | ≥ `LISTING_MIN_VOLUME_MULT` (**1.8×** to **2.0×** depending on Tier) |
| **Volume vs Listing Day** | ≥ `LISTING_MIN_VOL_VS_LISTING` (**1.0×**) when listing volume > 0 |
| **Volume (Listing Vol Missing)**| Today's volume ≥ `LISTING_MIN_VOL_MULT_WHEN_NO_LISTING_VOL` (**2.0×** avg) |
| **Entry Above Listing High** | ≤ `LISTING_MAX_ENTRY_ABOVE_HIGH_PCT` (**3.5%**) |
| **Min Risk/Reward Ratio** | ≥ `LISTING_MIN_RISK_REWARD` (**1.25**) |
| **Leader Score Threshold** | ≥ `LISTING_MIN_LEADER_SCORE` (**5 / 8**) |

---

## Deployed Strategy Rules (Final v2.5.0 Specification):

### 1. Entry & Breakout Levels
* **Watchlist Proximity:** Tracks and alerts when a stock is within **5% below** its listing day high.
* **Breakout Confirmations:** Triggers a breakout when current price/high breaks above the listing day high.
* **Intraday Confirmation Gate:** If market is open, breakouts on IPOs less than 5 days old are held in `PENDING` state for **60 minutes** to prevent intraday whipsaw wicks.
* **Turnover & Liquidity Floor:** Rejects signals with average daily turnover **< 1.0 Cr** (micro-caps) or circuit days **≥ 3 in 15 sessions**.

### 2. Upgraded Stop Loss (15-Day Local Swing Low + 12% Risk Cap)
* The stop loss is anchored to the **15-day local swing low** (including the breakout candle low) with a 3% buffer:
  `struct_stop = support_local * 0.97`
* To prevent excessive risk, individual trade risk is **capped at 12.0%**:
  `max_risk_stop = entry_price * 0.88`
* The final stop is the tighter of the two:
  `stop_loss = max(struct_stop, max_risk_stop)`
* **Fallback Stop:** Falls back to a flat **8% stop loss** (`entry_price * 0.92`) if the calculated structural stop is invalid or above the entry price.

### 3. Listing Age Limits (1-2 Years Max)
* IPOs are eligible for scanning and breakouts for up to **730 calendar days (2 years)** post-listing.
* This allows the scanner to capture breakouts that occur after a long consolidation or accumulation base formation (e.g. 1 year) while filtering out stale/mature listings that are no longer in their high-velocity IPO phase.

### 4. Risk/Reward and Targets
* Target calculations are anchored to entry price plus a multiplier of the listing day range:
  * Entry ≤2% above listing high → 100% of listing day range.
  * Entry 2-5% above listing high → 75% of listing day range.
  * Entry >5% above listing high → 50% of listing day range.
* A minimum risk/reward ratio of **1:1.25** is enforced. Setups where the potential range expansion target does not justify the stop loss size are rejected.

---

## Example Scenarios:

### Scenario 1: Tight 15-day Swing Low Breakout (Accepted)
* Listing Day High: ₹100
* 15-day Swing Low: ₹95 (3% buffer stop at ₹92.15)
* Entry: ₹102 (2% above listing high)
* Days Since Listing: 45 days
* Risk: `(102 - 92.15) / 102 = 9.65%` (below 12% cap)
* Result: **✅ Accepted** - Stop loss placed at ₹92.15.

### Scenario 2: Wide Base Capped Breakout (Accepted with Cap)
* Listing Day High: ₹100
* 15-day Swing Low: ₹85 (3% buffer stop at ₹82.45)
* Entry: ₹103 (3% above listing high)
* Days Since Listing: 320 days (approx. 1 year base)
* Raw Risk: `(103 - 82.45) / 103 = 19.95%` (above 12% cap)
* Capped Stop Loss: ₹90.64 (12% cap applied)
* Result: **✅ Accepted** - Stop loss placed at ₹90.64 (12% risk limit).

### Scenario 3: Stale IPO (Rejected)
* Listing Day High: ₹100
* Entry: ₹102
* Days Since Listing: 780 days (> 2 years old)
* Result: **❌ Rejected** - Too old to qualify as an IPO breakout setup.
