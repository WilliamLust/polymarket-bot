
## v7 Changes (Deployed 2026-05-06)

1. **METAR-latency tracker** — `_computeModelFreshness()` estimates model data age from GFS/ECMWF run schedules. FRESH (<30min) = 1.2x boost, STALE (>120min) = 0.9x dampener.
2. **City tier weighting** — 14 secondary cities (tier 2) get 1.3x position boost due to slower market repricing. 18 major hubs (tier 1) at 1.0x.
3. **YES+NO rebalancing arb** — Scans for askYes+askNo < 0.97, buys both legs for guaranteed profit after fees. Max $5/trade, 10/day, 3/scan.
4. **Whale dual-filter refresh** — Removes WR>95% wallets with 50+ trades (arb bots) and <10 trade wallets. Runs on startup. Removed 27 bots from 5145 -> 5118.
5. **Category-specific whale tracking** — `checkMarket(conditionId, category)` now tracks per-category trade counts. Wallets with 3+ trades in a category get +0.5 bonus to effective count.

### New KL-Weather Return Fields
- `freshness`: "FRESH" / "RECENT" / "STALE"
- `freshnessAgeMinutes`: minutes since latest model data became available
- `freshnessBoost`: 1.2 / 1.0 / 0.9
- `cityTier`: 1 (major) or 2 (secondary)
- `secondaryCityBoost`: 1.0 or 1.3

### New Trader Parameters
| Parameter | Value | Notes |
|-----------|-------|-------|
| ARB_ENABLED | true | YES+NO rebalancing arb |
| ARB_MIN_SPREAD | 0.03 | Need askYes+askNo <= 0.97 |
| ARB_MAX_POSITION | $5 | Max per arb trade |
| ARB_DAILY_LIMIT | 10 | Max arb trades per day |
| Tier 2 city boost | 1.3x | Secondary cities |
| FRESH boost | 1.2x | Model data <30 min old |
| STALE dampener | 0.9x | Model data >120 min old |

