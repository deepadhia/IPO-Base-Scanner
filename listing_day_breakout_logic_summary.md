# Listing Day Breakout Scanner - Logic Summary (v3.4.0)

## Strict Quality Mode (Default — "Good Trades Only")

The Listing Day Breakout Scanner runs in strict quality mode (`LISTING_STRICT_QUALITY=true` by default) to filter out market noise and capture only pristine institutional setups.

| Check | Default Setting (Strict Mode) |
| :--- | :--- |
| **Max Days Since Listing** | ≤ `LISTING_MAX_DAYS_SINCE_LISTING` (**730 days / 2 years**) |
| **Breakout Volume Floor (v3.3.0)** | Breakout-day traded volume ≥ **150,000 shares** (Day 0 exempt) |
| **Base History Floor (v3.3.0)** | At least **3 trading days** of post-listing data required |
| **Volume vs 10d Avg (Day 2+ Baseline)** | ≥ `LISTING_MIN_VOLUME_MULT` (**1.8×** to **2.0×** depending on Tier). Baseline calculated from Day 2 onwards — listing day (Day 0) and Day 1 are excluded to avoid spike inflation. |
| **Volume (Listing Vol Missing)**| Today's volume ≥ `LISTING_MIN_VOL_MULT_WHEN_NO_LISTING_VOL` (**2.0×** avg) — safety net only when listing day volume was never recorded |
| **Entry Above Listing High** | ≤ `LISTING_MAX_ENTRY_ABOVE_HIGH_PCT` (**3.5%**) |
| **Min Risk/Reward Ratio** | ≥ `LISTING_MIN_RISK_REWARD` (**1.25**) |
| **Leader Score Threshold** | ≥ `LISTING_MIN_LEADER_SCORE` (**5 / 8**) |

---

## Deployed Strategy Rules (Final v3.4.0 Specification):

### 1. Entry & Breakout Levels
* **Breakout Volume Floor:** IPOs whose **breakout-day** volume is `< 150,000 shares` are rejected from Day 1 onwards. Day 0 is exempt since volume may not be fully settled in the DB yet.
* **Base History Floor:** A minimum of **3 completed trading days** post-listing is required before the scanner will consider a breakout signal. This prevents false reads from listing-day intraday spikes with no base context.
* **Watchlist Proximity:** Tracks and alerts when a stock is **3–5% below** its listing day high (strict mode band; hardcoded near-breakout trigger is within 5%).
* **Breakout Confirmations:** Triggers a breakout when current high breaks **above** the listing day high.
* **Tier B (Base Breakout):** Stocks **5–20% below** listing high that break a tight local base qualify as `BASE_BREAKOUT` (40% position size) without crossing listing high first.
* **Intraday Confirmation Gate:** During market hours, IPOs **≥ 5 days** old that break listing high enter `PENDING` for **60 minutes** to verify sustained hold. IPOs **< 5 days** old are downgraded to `WATCHLIST` (alert only).
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

### 4. Tier Assignment
| Tier | Conditions | Size |
| :--- | :--- | :--- |
| **A+** | Perfect base + listing-high breakout + vol ≥ 1.8× + age ≤ 730d | 100% |
| **A** | Pure momentum (no perfect base) + vol ≥ 2.0× + age ≤ 730d | 60% |
| **A (fallback)** | vol ≥ 1.8× + age ≤ 730d, outside primary windows | 50% |
| **B** | `BASE_BREAKOUT` below listing high (≤20% below) | 40% |

### 5. Risk/Reward and Targets
* Target calculations are anchored to entry price plus a multiplier of the listing day range:
  * Entry ≤2% above listing high → 100% of listing day range.
  * Entry 2-5% above listing high → 75% of listing day range.
  * Entry >5% above listing high → 50% of listing day range.
* A **minimum profit target floor** of **20%** above entry price is enforced (`LISTING_MIN_TARGET_RETURN_PCT=0.20`). When the listing day range is very narrow (tight consolidation), the floor activates to prevent the calculated target from being too close to entry and failing the R:R filter. The floor is tunable via env var.
* A minimum risk/reward ratio of **1:1.25** is enforced (`LISTING_MIN_RISK_REWARD`). Setups where the potential range expansion target does not justify the stop loss size are rejected.

### 6. Limit Buy Order Instruction (v3.3.0)
* Every Telegram alert now includes a **Limit Buy Price** calculated as:
  `limit_buy_price = listing_day_high × 1.035`
* This caps execution at 3.5% above the listing high, preventing runaway chasing.
* The price is clearly labelled in the alert: `Place a Limit Buy Order at ₹{limit_buy_price}`.

---

## Example Scenarios:

### Scenario 1: Tight 15-day Swing Low Breakout (Accepted)
* Listing Day High: ₹100
* Breakout Day Volume: 500,000 shares (≥ 150k floor ✅)
* Days Since Listing: 45 days (≥ 3 days ✅)
* 15-day Swing Low: ₹95 (3% buffer stop at ₹92.15)
* Entry: ₹102 (2% above listing high)
* Risk: `(102 - 92.15) / 102 = 9.65%` (below 12% cap)
* Limit Buy Price: ₹103.50 (3.5% above listing high)
* Result: **✅ Accepted** - Stop loss placed at ₹92.15.

### Scenario 2: Wide Base Capped Breakout (Accepted with Cap)
* Listing Day High: ₹100
* Breakout Day Volume: 200,000 shares (≥ 150k floor ✅)
* 15-day Swing Low: ₹85 (3% buffer stop at ₹82.45)
* Entry: ₹103 (3% above listing high)
* Days Since Listing: 320 days (approx. 1 year base)
* Raw Risk: `(103 - 82.45) / 103 = 19.95%` (above 12% cap)
* Capped Stop Loss: ₹90.64 (12% cap applied)
* Result: **✅ Accepted** - Stop loss placed at ₹90.64 (12% risk limit).

### Scenario 3: Illiquid Breakout Day (Rejected by v3.3.0 Volume Floor)
* Listing Day High: ₹100
* Breakout Day Volume: 80,000 shares (< 150k floor ❌)
* Days Since Listing: 15 days
* Result: **❌ Rejected** - `BREAKOUT_VOLUME_BELOW_FLOOR`. Too illiquid to sustain momentum.

### Scenario 4: Stale IPO (Rejected)
* Listing Day High: ₹100
* Entry: ₹102
* Days Since Listing: 780 days (> 2 years old)
* Result: **❌ Rejected** - Too old to qualify as an IPO breakout setup.
