# Polymarket Bot v5 Upgrade Plan

**Date:** 2026-05-05
**Status:** In Progress
**Research Source:** references/v5-research-2026-05-05.md

## Overview

Six changes ranked by edge potential x effort. One immediate, five this week.

## IMMEDIATE

### 1. Kill Profit-Lock Exit -> Hold to Resolution

**Why:** Wenth autopsy (Mar 2026, 34 configurations): 50% profit-lock on $0.20 NO position captures 12.5% of max gain, retains 100% of max loss. Only 4/34 configs profitable -- all hold-to-resolution. Highest WR config (70.2%, profit-lock at 30%) produced 2nd-largest loss (-$92).

**Current code:** EXIT_PROFIT_PCT = 0.50, EXIT_MAX_HOLD_HOURS = 6, EXIT_CHECK_ENABLED = true

**Change:** Set EXIT_CHECK_ENABLED = false. Function becomes no-op. Existing "exited" positions in positions.json unchanged.

**Risk:** Zero. Only stops selling winners early.

## THIS WEEK

### 2. Skip Markets Closing <8h (3 lines)

**Why:** Edge evaporates as resolution approaches. Late movers compress pricing. Markets closing <8h have near-zero edge.

**Change:** After category cap check, parse end_date_iso, skip if <8h from now.

### 3. Spatial Correlation Caps -- Weather Regions (30 min)

**Why:** Weather positions are NOT independent. Cold front hits Northeast -> NYC, Boston, Philly move simultaneously. Category cap (3/weather) doesn't protect against regional correlation.

**Change:** Add weatherRegion(city) mapper. Add MAX_PER_WEATHER_REGION = 2. Count open positions per region in scan loop. Skip if cap hit.

Regions: northeast, southeast, midwest, southwest, pacific, europe, asia, oceania

### 4. Multi-Model Convergence Gate (2-3 hr, kl_weather.js)

**Why:** polymarketweather.com documents 4 structural edges. We only exploit 1 (NWS forecast -> D_KL). Missing: convergence gate, airport delta, model-update timing.

Changes to kl_weather.js:
- Add Open-Meteo API fetch (ECMWF + GFS ensemble, free)
- Add convergence gate: spread >5F -> CAUTION, 3-5F -> reduced boost, <2F -> normal
- Add airport station delta table (static JSON, seasonal offsets)
- Keep existing D_KL computation as-is

### 5. Cron-Triggered Weather Scan (1 hr)

**Why:** GFS updates at 00Z/06Z/12Z/18Z. Edge window 5-15 min after publish. Our 5-min scan catches some but isn't synchronized.

**Change:** Add --weather-urgent flag to trader (scan weather only, single pass). VPS crontab:
3 0,6,12,18 * * * bash -l -c "cd ~/polymarket-bot && node live/live_trader_node.js --live --weather-urgent"

### 6. Verify Kelly Fraction (5 min, no code change)

**Current:** KELLY_FRACTION = 0.25 (quarter-Kelly)
**Research:** 0.25-0.5x recommended. We're at conservative end.
**Verdict:** No change needed. Already correct.

## NEXT SPRINT (not this commit)

- Single-condition rebalancing arb (YES+NO != $1.00)
- Behavioral fade signal (volume surge without price move)
- Verify whale_checker.js direction inference data source

## DEFERRED

- Resolution-source latency arb (needs closer VPS)
- Combinatorial arb (low liquidity at $47 bankroll)
- Cross-platform arb (can't access Kalshi from Lithuania)
- Market making (wrong capital size/time horizon)
