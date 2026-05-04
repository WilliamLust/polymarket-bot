# Polymarket Bot — Restart Prompt

We're building a Polymarket prediction market trading bot at ~/polymarket-bot/ (git repo, venv, 5 commits pushed to github.com:WilliamLust/polymarket-bot).

## What we just completed

Ran the weather backtest kill test (backtesting/weather_backtest.py). Results in backtesting/weather_backtest_results.json.

**Critical finding**: The gopfan2 X poster's claim is BACKWARDS.
- Buy YES at tails (<15c): NEGATIVE edge (-$3,291 on 4.85M trades, 3.1% WR)
- Buy NO at favorites (>45c): POSITIVE edge (+$41,260 on 1.14M trades, 26.3% WR)
- Retail OVERprices tail risk, doesn't underprice it
- Best bucket: 95-100c YES price (buy NO at ≤5c), 4.8% WR but +$12,697 P&L — rare wins pay 20:1
- Total P&L: +$37,969 but 7.5% overall WR = extreme variance, max drawdown $10,880
- Late 2024 was terrible, 2025-2026 recovered strongly

## What to do next

The edge exists but it's opposite to what was claimed. Next steps in priority order:

1. **Add slippage/fill simulation** — current backtest assumes we trade at observed prices. Real fills have spread. If 5-10c slippage on NO fills at 95c+ markets, the edge may evaporate.
2. **Kelly sizing on the NO-favorites edge** — quarter Kelly on the >45c bucket. Compute optimal position sizing given the 4.8-26% win rates.
3. **Deduplicate trades** — current backtest counts every trade as a signal. In reality we'd take one position per market, not 50 trades on the same market. Need to aggregate by market_id and take only the first/last trade.
4. **Run the same backtest on ALL markets (not just weather)** — does this NO-favorites edge hold across politics, crypto, sports? If so, the opportunity is much larger.
5. **Stacking ensemble training** — train the 5-model stack (XGBoost+LightGBM+HistGBT+ExtraTrees+RF) on resolved market features to predict resolution.
6. **AI prob arb refinement** — test Qwen 3.6:27b on historical weather markets where we know the outcome. Compare LLM estimates to market prices.

## Key files
- `backtesting/weather_backtest.py` — the kill test script
- `backtesting/weather_backtest_results.json` — full results
- `backtesting/load_data.py` — PyArrow row-group reader (avoids OOM on 36GB file)
- `strategies/ai_prob_arb.py` — LLM probability estimation via Ollama
- `strategies/stacking_ensemble.py` — 5-model stack + Kelly sizing
- `execution/market_data.py` — read-only Polymarket API client
- `config/config.yaml` — thresholds and model config
- `data/quant.parquet` — 36GB, 568.5M trades, MUST use PyArrow row groups
- `data/markets.parquet` — 735K markets with resolution data

## Known gotchas
- quant.parquet is 36GB — pandas read_parquet = OOM. PyArrow row-group iteration only.
- Qwen 3.x requires "think": false in Ollama API calls or output goes to message.thinking.
- outcome_prices in markets.parquet are Python-style list strings: "['1', '0']" = YES won.
- No GitHub PAT — SSH works for push but can't create repos via API.
- US-based — Polymarket ToS prohibits trading. Backtesting and analysis only.

Load the polymarket-trading-bot skill before continuing work on this project.
