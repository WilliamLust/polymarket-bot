# Polymarket Bot Improvement Plan — Next Session Prompt

Copy everything below the line into a new Hermes session.

---

Load the `polymarket-trading-bot` skill first. Read `~/polymarket-bot/STRATEGY.md` and `~/polymarket-bot/RESTART.md`.

## Current State

Our Polymarket BUY_NO bot is live on a Lithuanian VPS (76.13.251.154, user=polymarket, screen session `trader`, Node 22 via nvm). It scans every 5 minutes for markets where YES ≥ 95¢ and buys the NO side at $1/position on a $49.39 bankroll. We've backtested 568M trades, paper-traded, and deployed with category exposure caps (3/cat). Zero live trades so far — the 95-99¢ window is thin at current market conditions.

The edge is real but thin: 9% of 95-99¢ YES markets flip to NO resolution, yielding 20-100x returns. But live slippage averages +15% (worse than backtest), narrowing our margin. The strategy survives up to ~40% slippage, but we're already at 15%.

## Mission

Implement the highest-impact improvements to widen the edge. I've ranked these by expected value (impact × probability of success / effort):

### Priority 1: Exit Strategy (HIGHEST IMPACT)
**Why #1:** Right now we hold NO until resolution, which means we only profit when the market fully flips (9% of the time). But if a market's YES price drops from 97¢ to 85¢ after we buy NO at 3¢, our NO is now worth 15¢ — a 5x gain we could lock in. We don't need the market to fully flip to profit; we just need significant price movement, which happens far more often than full flips. This could transform our effective win rate from 9% to 30-50%.

**Approach:**
- Monitor open positions' current YES prices each scan cycle
- Sell NO when profit capture hits a threshold (e.g., ≥5x cost, or YES drops below 90¢)
- Compare hold-to-resolution EV vs sell-early EV on historical data first
- Implement as a new `checkExits()` function in live_trader_node.js that runs before `scanAndTrade()`

### Priority 2: Orderbook Depth Screening (HIGH IMPACT, LOW EFFORT)
**Why #2:** Our +15% average slippage is the biggest drag on edge. If we can skip thin-book markets (where we get terrible fills) and only trade markets with deep NO-side liquidity, we reduce slippage and directly increase profitability. This is a quick filter addition.

**Approach:**
- Before placing an order, query the CLOB orderbook for the NO token
- Calculate available shares within 10% of mid-price (our "executable depth")
- Skip markets where executable depth < some threshold (e.g., < 50 shares)
- Log depth data for each market to build a model over time

### Priority 3: Per-Category Flip Rates (MEDIUM-HIGH IMPACT, LOW EFFORT)
**Why #3:** The 9% overall flip rate masks huge variance by category. If weather flips 15% and politics 5%, we should be loading up on weather and avoiding politics. We have 568M rows of historical data — this is a straightforward SQL/groupby analysis that directly informs both filtering and position sizing.

**Approach:**
- Query `quant.parquet` + `markets.parquet` for resolution outcomes by category
- Compute per-category flip rate for 95-99¢ YES bucket
- Use these rates to weight position sizing (Kelly) and optionally filter low-flip categories
- Integrate as a `categoryFlipRates.json` config file the trader loads at startup

### Priority 4: Position Sizing by Confidence (MEDIUM IMPACT, LOW EFFORT — after #3)
**Why #4:** Once we have per-category flip rates, we can apply Kelly criterion sizing. A weather market at 95¢ with 15% flip rate deserves a bigger position than a politics market at 95¢ with 5% flip rate. This is a simple multiplier on position size.

**Approach:**
- Kelly fraction = (p × b - q) / b where p = flip rate, q = 1-p, b = NO payout ratio
- Cap at some max (e.g., 2x base position size) to avoid overconcentration
- Implement as a `getPositionSize(market)` function that returns adjusted size

### Priority 5: Better Entry Signals (HIGH IMPACT, HIGH EFFORT — research project)
**Why #5:** Not all 95¢ markets are equal. A weather market where the forecast is already locked in is genuinely near-certain. A crypto price target at 95¢ might be genuinely uncertain. Distinguishing these would dramatically improve win rate, but requires understanding *why* the market is priced where it is — which needs NLP, news monitoring, or other complex signals. Worth prototyping but not building yet.

**Approach (prototype only):**
- Pull question text for all 95-99¢ markets over past 6 months
- Label resolved ones as flipped/not-flipped
- Look for linguistic patterns (e.g., "Will X be between Y and Z" vs "Will X happen by date") that correlate with flip probability
- If signal exists, build a classifier; if not, shelve

### Priority 6: Whale Watching (LOW-MEDIUM IMPACT, MODERATE EFFORT)
**Why #6:** On-chain wallet tracking sounds cool but has a latency problem — by the time we see a large NO purchase, the price has already moved. More useful as a sentiment indicator than an execution signal. Could complement entry signals if we identify consistently profitable wallets.

### Priority 7: News/Time Lag (UNCERTAIN IMPACT, HIGH EFFORT — shelf)
**Why #7:** Monitoring RSS/X for events not yet priced in is a full research project with uncertain payoff. The signal is real (markets do lag news) but building a reliable pipeline is complex. Revisit after simpler improvements are deployed.

## What to Build First

Start with **Priority 2 (orderbook depth screening)** — it's the lowest effort and directly addresses our biggest pain point (slippage). Then move to **Priority 3 (per-category flip rates)** — analytical work using data we already have. Then tackle **Priority 1 (exit strategy)** — the biggest impact but needs more design work and careful backtesting to avoid selling winners too early. Priorities 4-7 are sequential follow-ups or shelved for later.

## Key Files
- `~/polymarket-bot/live/live_trader_node.js` — the live trader (Node.js, CLOB v2 SDK, POLY_1271)
- `~/polymarket-bot/live/shadow_data/positions.json` — all positions with categories
- `~/polymarket-bot/STRATEGY.md` — full strategy synopsis
- `~/polymarket-bot/RESTART.md` — deployment status and restart instructions
- VPS: `ssh polymarket@76.13.251.154`, always `source ~/.nvm/nvm.sh && nvm use 22`, NO apt
- Data: `~/polymarket-bot/data/quant.parquet` (568M rows) + `markets.parquet` (734K markets)
- Long-running processes MUST use screen, not SSH backgrounding

## Constraints
- $49.39 bankroll, $1 positions, conservative until edge confirmed
- Bot uses limit orders only, never market orders
- CLOB /balances endpoint behind Cloudflare — use getBalanceAllowance()
- polymarket.com endpoints serve Vercel challenge to Node.js — use curl via child_process
- SSH agent forwarding for git push from VPS: `ssh -A polymarket@76.13.251.154`
- Deposit wallet: 0xf277e98adFE6DD4670c2Bb871941DF628A8E0932
- EOA: 0x82d4...7b3
