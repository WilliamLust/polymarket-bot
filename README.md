# Polymarket Trading Bot

ML-driven trading bot for Polymarket prediction markets. Backtesting-first development — no capital at risk until edge is proven.

## 5-Layer Stack

```
LAYER 1 — DATA           Gamma API + CLOB WebSocket + HuggingFace (1.1B trades) + xurl sentiment
LAYER 2 — BACKTESTING    prediction-market-backtesting (NautilusTrader) + Optuna TPE
LAYER 3 — STRATEGY       AI Prob Arb (Qwen 3.6:27b) + Stacking Ensemble + TradingAgents
LAYER 4 — EXECUTION      py-clob-client (v0.34.6) + EIP-712 signing on Polygon
LAYER 5 — GPU            Local LLM inference + ML training (RTX 3090 24GB)
```

## US Jurisdiction

Polymarket ToS prohibits US persons from trading. This project starts with **backtesting and data analysis only** — legal everywhere, no auth needed. Trade execution code exists but is disabled by default.

## Quick Start

```bash
cd ~/polymarket-bot
source venv/bin/activate

# Download the 21GB dataset (first time only — takes ~30-60 min)
huggingface-cli download SII-WANGZJ/Polymarket_data quant.parquet --repo-type dataset --local-dir data/

# Explore the data
python backtesting/load_data.py --sample 100000

# Check live markets
python -c "from execution.market_data import PolymarketClient; c=PolymarketClient(); print(c.list_events(limit=5))"

# Test AI probability estimation (requires Ollama running)
python -c "from strategies.ai_prob_arb import AIProbArb, check_ollama_available; print('Ollama ready:', check_ollama_available())"
```

## Project Structure

```
polymarket-bot/
├── backtesting/
│   ├── load_data.py          # Load and explore quant.parquet
│   └── (backtesting engine from cloned repo)
├── strategies/
│   ├── stacking_ensemble.py  # Navnoor Bawa's 5-model stack + Kelly sizing
│   └── ai_prob_arb.py       # LLM probability estimation → arb signals
├── execution/
│   └── market_data.py        # Polymarket API client (read-only)
├── config/
│   └── default.yaml          # Configuration template
├── data/                     # quant.parquet lives here (21GB, gitignored)
├── notebooks/                # Jupyter exploration
├── references/               # Strategy research, API docs, tool evaluations
├── scripts/                  # Utility scripts
├── prediction-market-backtesting/  # Cloned from evan-kolberg
├── polymarket-agents/             # Cloned from Polymarket/agents
├── venv/                          # Python virtualenv
├── .env.example                   # Environment variables template
└── .gitignore
```

## Strategies

### Primary: AI Probability Arbitrage
News → local LLM (Qwen 3.6:27b) estimates true probability → trade when market price diverges >15%. This is where the RTX 3090 matters — ~15 tok/sec inference is fast enough to beat human traders in the 30s-5min window.

### Secondary: Stacking Ensemble
5 base models (XGBoost, LightGBM, HistGradientBoosting, ExtraTrees, RF) → LogisticRegression meta-learner. Platt scaling calibration. 93-95% CV accuracy, Brier 0.022. 10 features.

### Tertiary: TradingAgents
Multi-agent LLM debate (fundamentals/sentiment/news/tech analysts → bull/bear debate → trader → risk mgmt). 65.4K stars, supports Ollama locally.

### Position Sizing
Fractional Kelly Criterion — quarter Kelly, max 5% bankroll per position.

## What We Rejected
- MiroFish — narrative reports, not quantitative signals. Financial features "coming soon".
- Simple arbitrage — dead (2.7s windows, sub-100ms bots capture 73%)
- Market making — thin margins, adverse selection, gas eats profits

## Hardware

- RTX 3090 (24GB VRAM) — runs Qwen 3.6:27b locally
- Ollama with think:false (required for Qwen 3.x)
- Linux Mint

## Key References

- SII-WANGZJ/Polymarket_data — 1.1B trades, 107GB on HuggingFace
- evan-kolberg/prediction-market-backtesting — NautilusTrader-based backtesting
- Polymarket/agents — official AI agent framework (3.4K stars)
- py-clob-client v0.34.6 — official Python trading client
