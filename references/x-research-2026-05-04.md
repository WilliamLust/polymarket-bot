# X/Twitter Research — Polymarket Bot Ecosystem (May 2026)

Analysis of 10 posts from May 2-3, 2026. Rated on substance vs hype.
Integration value assessed for our 5-layer stack.

---

## RATINGS LEGEND
- ★★★ = High value, integrate or emulate
- ★★ = Medium value, borrow specific pieces
- ★ = Low value, mostly hype
- ✗ = Not useful / deceptive

---

## 1. @Crypto_Jargon — "Anthropic prediction market bot" ★

**Post:** Claims "ANTHROPIC JUST DROPPED A PREDICTION MARKET TRADING BOT STRUCTURE"
**Reality:** A Skill.md file with infographic, not an Anthropic product. No code, no repo.

6-step pipeline conceptually matches our stack (Scan → Research → Predict → Risk → Execute → Compound). The formulas shown are standard (Kelly, VaR, Sharpe). The SKILL.md structure with triggers and rules is identical to what Hermes already does natively.

**Integration value:** None. We already have all of this built. Confirms our architecture is standard, which is reassuring but not novel.

---

## 2. @ridark_eth — "Math trap / Frank-Wolfe Algorithm" ✗

**Post:** Claims 41% of Polymarket conditions are "mathematically broken," median logic deviation $0.60/dollar. Name-drops Bregman Projection and Frank-Wolfe Algorithm.

**Reality:** Zero code, zero methodology, zero proof. The 41% and $0.60 figures are unsubstantiated. Frank-Wolfe is a real optimization algorithm but the post provides no implementation. Community calls it out as engagement bait.

**Integration value:** None. Frank-Wolfe optimization *could* theoretically be applied to portfolio allocation on correlated markets, but this post gives nothing to work with. File under "maybe explore later."

---

## 3. @sopersone — "Quant formula, $20M claim" ✗

**Post:** Claims a single formula made $20M. Shows a complex equation with 7 free parameters.

**Reality:** The formula is a manually-tuned heuristic evaluation function, not a model. 7 free parameters (λ₁, λ₂, λ₃, γ₀, φ₀, κ, 0.7 threshold) = classic overfit risk. Community critique is sharp: no Kelly derivation, no martingale justification, internal contradiction (y penalized and boosted simultaneously), and the $20M claim is unverifiable at Polymarket's volume without massive slippage.

**Integration value:** None. The "parlay" concept (stacking correlated positions) is interesting in theory but doesn't exist natively on Polymarket. If we ever do correlation arb, we'd derive it properly, not copy this.

---

## 4. @0xTrackmind — "Jane Street trader story" ★★

**Post:** Narrative about a Jane Street trader who pivoted to Polymarket. $11,400 in 19 days, Sharpe 2.31, 214 trades.

**Key signal — the 3-agent voting system:**
- Agent 1: News lag detector
- Agent 2: Whale flow tracker
- Agent 3: Crowd bias analyzer
- Two agree → full Kelly size. One alone → half size. All disagree → no trade.

This is a specific, implementable version of multi-agent debate that's more practical than TradingAgents' full bull/bear debate. Lighter weight, faster execution, clear voting rules.

**Other details:** Scraped 86M trades, filtered 500 markets → 35 survivors, exits at 85% capture or 3x volume spike, never holds to settlement. These are concrete, borrowable rules.

**Integration value:** ★★ for the 3-agent voting architecture. We should implement this as a lighter alternative to the full TradingAgents multi-agent debate. The exit rules (85% capture, 3x volume spike) are also worth backtesting.

---

## 5. @zostaff — poly-trading-bot (MIT) ★★★

**Post:** Open-source Polymarket trading bot, MIT license, Python 3.12+.

**Repo:** https://github.com/zostaff/poly-trading-bot

**This is the most valuable find.** A complete, working toolkit:

Three strategies:
1. AI Directional — LLM-powered via OpenRouter (any model)
2. Safe Compounder — Pure edge math, no LLM
3. Beast Mode — Aggressive (included for comparison)

Key infrastructure:
- Streamlit dashboard for real-time monitoring
- SQLite telemetry on every trade (critical for post-mortems)
- Fallback chain across LLM models
- Circuit breaker at 15% drawdown
- Quarter-Kelly default position sizing
- Paper trading mode by default
- Category scorer for filtering market types

Architecture: `src/clients/`, `src/agents/`, `src/strategies/` — very similar to ours.

**Integration value:** ★★★ Clone and study. The SQLite telemetry, circuit breaker, category scorer, and paper trading mode are all directly applicable. The multi-agent ensemble (forecaster, bull/bear researcher, risk manager, trader, news analyst) maps to our TradingAgents tertiary strategy. We should port the telemetry and circuit breaker patterns into our execution layer.

---

## 6. @bl888m — "CarbonCopy" ✗

**Post:** Copy-trading product called CarbonCopy. Claims $94K from $600 in 8 weeks.

**Reality:** Copy trading product with marketing narrative. The "copy timing, not wallets" angle is interesting conceptually (top wallets exit at 91% of max move, retail waits for 100% and watches it reverse), but this is a paid product, not open-source. Copy trading is already rejected in our stack (no edge, pure parasitic).

**Integration value:** ✗ for the product. The exit discipline insight (exit at 85-91% capture, cut losses at -12%) aligns with the @0xTrackmind exit rules and is worth noting for our own exit logic.

---

## 7. @AlterEgo_eth — "Hermes Agent tutorial" ★★

**Post:** 7-step tutorial for building a Polymarket bot using Hermes Agent.

**Key technical finds:**
1. **CLOB v2 migration** — legacy `py_clob_client` → `py_clob_client_v2`. This is critical if we ever go live. Config: `use_server_time=True`, `retry_on_error=True`.
2. **Three repos referenced:**
   - JLowo/gengar_polymarket_bot — Oracle lag scalper, Brownian motion model
   - joicodev/polymarket-bot — Black-Scholes + EWMA volatility
   - djienne/Polymarket-bot — Gabagool arb + Smart Ape momentum

The "Hermes self-learning" claim is marketing fluff — no concrete mechanism shown.

**Integration value:** ★★ for the repo references and CLOB v2 migration warning. The gengar bot is particularly valuable (see below).

---

## 8. @Dipper_pol — "99.3% win rate quant bot" ★

**Post:** Claims $2,500 → $855,000, 99.3% win rate, 29,304 predictions.

**Key insight:** The 99.3% win rate comes from refusing every trade where EV ≤ 0, not from prediction accuracy. This is essentially what our Safe Compounder does — extreme selectivity. Quarter-Kelly sizing confirmed again.

**Reality check:** $855K from $2,500 is a 34,200% return. Even at quarter-Kelly with high selectivity, this implies either enormous volume, survivorship bias (only winners shown), or fabrication.

**Integration value:** ★ for confirming the "skip bad trades" philosophy. We already have this in our EV filter.

---

## 9. @seelffff — "gopfan2 weather strategy" ★★

**Post:** Reverse-engineered a trader who made $343K trading exclusively weather markets.

**The 3-rule strategy:**
1. Buy YES if price < $0.15
2. Buy NO if price > $0.45
3. Never bet more than $1 per position

**Why it works:** Retail bets on obvious temperature ranges. Tail buckets (extreme hot/cold) are priced at 10-12¢ but have ~18-20¢ real probability. Buy that gap thousands of times. No weather model needed — just check if a bucket is underpriced.

This is **structural edge** — not speed, not LLM, just recognizing that retail misprices low-probability outcomes systematically.

**Integration value:** ★★ This is a concrete, testable strategy. We can backtest it immediately on our 568M trade dataset. Filter for weather markets, apply the 3 rules, compute returns. If it still works, it becomes our simplest strategy — zero LLM cost, zero GPU, pure structural edge. Also links to an open-source version.

---

## 10. @0x_Discover — "Pair-sum arbitrage bot" ★

**Post:** Pair-sum arbitrage bot making $27K/day, $743K in 35 days. Survived Polymarket fee changes.

**Strategy:** Pair-sum arbitrage on crypto Up/Down markets (BTC, ETH, SOL, XRP). Only enters when total price is low enough that fees still leave profit.

**Reality:** This is the speed/execution game we already rejected. Sub-100ms bots capture 73% of simple arb. The bot survived fees by having better infrastructure, not better strategy.

**Integration value:** ★ for confirming our rejection of simple arbitrage. The "fees kill thin edges" lesson reinforces our 15% divergence threshold.

---

## CROSSLINKED REPOS — DEEPER ANALYSIS

### gengar_polymarket_bot (JLowo) ★★★

Oracle lag scalper for Polymarket's 5-minute BTC Up/Down markets.

**Architecture is exceptional:**
- Brownian motion probability model (vol calibrated at 0.12 from backtesting)
- Three-layer entry filter: model ≥80% confidence + margin of safety (price ≤ prob × 0.85) + Kelly sizing
- Balance-verified buys (snapshot USDC, wait, verify 3 rounds, ghost fill detection)
- Circuit breakers: CLOB health check, daily loss limit, minimum notional guard
- **No stops** — 5-min window too short for stops (data showed stops cost $35.45 across 5 fires, 4/5 stopped trades would have won)
- Tor proxy for geo-restrictions (relevant if we ever go live from US)
- **Float precision bug** — py-clob-client uses IEEE 754 float math, producing `0.29000000000000004` which violates 4-decimal rule. Fix: use `create_order(OrderArgs(price=round(price, 2), size=float(int(shares))))`

**Integration value:** ★★★ The balance verification, ghost fill detection, and float precision fix are production-critical patterns. The no-stops-for-short-windows finding changes exit strategy design. The calibration history (vol 0.08 → 0.12) demonstrates proper backtest-driven parameter tuning.

### joicodev/polymarket-bot ★★

Black-Scholes + EWMA volatility for 5-min BTC markets.

**Key differentiator:** 7-condition abstention system:
1. Insufficient data (< 50 ticks)
2. Dead zone (baseProb within 10% of 50%)
3. Volatility too low (sigma < 0.0001)
4. Volatility too high (sigma > 0.01)
5. Time remaining too short (< 30s)
6. Edge too small (p - q < 0.05)
7. Drawdown exceeded

Logit-space combination of Black-Scholes + momentum + mean reversion.

**Integration value:** ★★ The 7-condition abstention system is a cleaner version of our entry filter. We should adopt this pattern for the stacking ensemble strategy. Logit-space probability combination is mathematically superior to naive averaging.

---

## SYNTHESIS: WHAT TO INTEGRATE

### Immediate (next session)
1. **SQLite telemetry** (from zostaff/poly-trading-bot) — log every signal, every trade decision, every cost. Essential for post-mortems and edge decay detection.
2. **Circuit breaker** (from zostaff + gengar) — 15% drawdown halt, CLOB health check, daily loss limit.
3. **7-condition abstention system** (from joicodev) — adapt for our stacking ensemble's entry filter.
4. **gopfan2 weather strategy** — backtest the 3-rule approach on our 568M trade dataset.
5. **Float precision fix** (from gengar) — use integer shares + 2-decimal prices with py-clob-client.

### Medium-term (architecture)
6. **3-agent voting** (from @0xTrackmind) — implement as lightweight alternative to full TradingAgents debate. News lag + whale flow + crowd bias → vote → size.
7. **Exit rules** — 85% capture OR 3x volume spike → exit. Never hold to settlement (from @0xTrackmind + @bl888m).
8. **Category scorer** (from zostaff) — classify markets by type (crypto, politics, weather, sports) and apply strategy-specific parameters.
9. **Paper trading mode** (from zostaff) — signal against live data without executing.

### Not now
10. **CLOB v2 migration** — note it, but we're backtesting-only. Apply when/if we go live.
11. **Tor proxy** — relevant for US execution, but that's a legal question, not a technical one.
12. **Black-Scholes / Brownian motion** — only applies to 5-min BTC markets. Our strategies target multi-day resolution markets where these models don't apply.

### Rejected
- CarbonCopy (copy trading) — no edge, parasitic
- @ridark_eth Frank-Wolfe — no implementation provided
- @sopersone quant formula — overfitted heuristic
- Simple pair-sum arbitrage — dead, speed game we can't win
