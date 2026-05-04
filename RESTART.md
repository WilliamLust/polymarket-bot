# Polymarket Bot — RESTART.md

## Project Status: Enhanced Backtest Complete

The enhanced backtest with deduplication, slippage simulation, and all-market expansion is **complete**. Key script: `backtesting/enhanced_backtest.py`.

## Backtest Results Summary

### Baseline (dedup=first, slippage=0, all markets)

| Metric | Value |
|--------|-------|
| Raw trades | 568,590,741 |
| Deduped positions | 615,111 |
| BUY_YES (<15¢) | 169,081 positions, 5.1% WR, +$2,177 |
| BUY_NO (>45¢) | 347,311 positions, 43.2% WR, **+$17,391** |
| Total P&L | +$19,567 |
| Max drawdown | $98 |

### BUY_NO by Price Bucket (slippage=0)

| Bucket | Positions | Win Rate | P&L |
|--------|-----------|----------|-----|
| 45-55¢ | 214,842 | 53.8% | +$6,084 |
| 55-65¢ | 29,517 | 48.8% | +$1,894 |
| 65-75¢ | 16,778 | 38.9% | +$1,235 |
| 75-85¢ | 13,472 | 31.8% | +$1,388 |
| 85-95¢ | 13,349 | 29.3% | +$2,498 |
| 95-100¢ | 59,353 | 9.0% | +$4,292 |

### Slippage Sensitivity

| Slippage | BUY_NO P&L | Overall P&L | Max DD | Verdict |
|----------|------------|-------------|--------|---------|
| 0¢ | +$17,391 | +$19,567 | $98 | ★ STRONG EDGE |
| 3¢ | +$6,971 | +$4,076 | $5,016 | ★ STRONG EDGE (but 45-55¢ bucket flips negative: -$361) |
| 5¢ | +$25 | -$6,252 | $13,171 | ◆ MARGINAL — edge destroyed |

**Critical insight**: At 3¢ slippage, the 45-55¢ bucket goes negative (-$361). At 5¢, the entire BUY_NO edge collapses to +$25. The edge is extremely slippage-sensitive, concentrated in the high-YES-price buckets where NO fill costs are tiny (5-15¢) but slippage is a large % of cost.

### BUY_NO P&L by Category (slippage=0)

| Category | Positions | WR | P&L |
|----------|-----------|-----|-----|
| other | 150,029 | 43.6% | +$9,655 |
| crypto | 127,703 | 49.6% | +$2,744 |
| sports | 39,816 | 23.3% | +$1,979 |
| politics_us | 6,679 | 41.9% | +$886 |
| tech | 10,306 | 38.3% | +$789 |
| politics_world | 5,768 | 40.8% | +$505 |
| weather | 2,852 | 47.6% | +$407 |
| entertainment | 2,195 | 29.5% | +$235 |
| science | 1,537 | 45.5% | +$102 |
| economics | 351 | 53.3% | +$81 |
| covid | 75 | 42.7% | +$8 |

**All categories profitable.** "Other" category dominates volume/P&L. Crypto has highest WR (49.6%). Sports has lowest WR (23.3%) but still profitable because the 95-100¢ bucket (5.5% WR, +$892) is huge volume (22K positions).

### Weather-Only (dedup=first, slippage=0)

| Metric | Value |
|--------|-------|
| Positions | 2,850 BUY_NO |
| WR | 47.6% |
| P&L | +$405 |
| 95-100¢ bucket | 568 pos, 18.0% WR, +$90 |

Compared to original weather_backtest (5.99M trades, no dedup): the edge survives dedup at +$405 instead of +$37,969. The 324x reduction in trade count is the key — we went from counting every price update as a separate trade to one position per market.

### Sports-Only (dedup=first, slippage=0)

| Metric | Value |
|--------|-------|
| Positions | 39,816 BUY_NO |
| WR | 23.3% |
| P&L | +$1,979 |
| 95-100¢ bucket | 22,126 pos (!), 5.5% WR, +$892 |

Sports is dominated by the 95-100¢ bucket — 22K of 40K positions. Very low WR (5.5%) but still net positive because the payout asymmetry (5¢ cost → $0.93 net win) compensates.

### Crypto-Only (dedup=first, slippage=0)

| Metric | Value |
|--------|-------|
| Positions | 127,703 BUY_NO |
| WR | 49.6% |
| P&L | +$2,744 |
| 45-55¢ bucket | 115,273 pos (!), 52.1% WR, +$1,393 |

Crypto is the cleanest edge — near-coin-flip markets where buying NO at >45¢ gives 49.6% WR. Most positions are in the 45-55¢ range (near 50/50 odds).

## Key Findings

1. **Dedup kills the headline number but the edge survives**: +$37,969 (weather, no dedup) → +$405 (weather, deduped). +$17,391 (all markets, deduped). The original 5.99M "trades" were 324x overcounted.

2. **The edge is real across all categories**: Every category is net positive at 0 slippage. "Other" (uncategorized markets) dominates P&L.

3. **Slippage is the existential threat**: At 3¢ slippage, total P&L drops from +$19.6K to +$4.1K. At 5¢, it's gone. The 45-55¢ bucket flips negative at just 3¢. The 95-100¢ bucket is the most resilient (+$2,512 at 3¢, +$1,325 at 5¢) but can't carry the full strategy alone.

4. **The 95-100¢ bucket is where the asymmetry lives**: 59K positions, 9% WR, but each win pays ~19x the cost. This is the "long tail" trade — buy NO at 5¢, win $0.93. At 3¢ slippage (8¢ cost), you still net +$0.85 on wins but lose more on losses.

5. **Max drawdown is tiny at 0 slippage ($98) but explodes with slippage**: $5K at 3¢, $13K at 5¢. This is because slippage turns many marginal wins into losses.

## Data Pipeline

- **quant.parquet**: 568.6M rows, 577 row groups, ~36GB. Uses `drop_duplicates` per row group with periodic merging (every 100 RGs) to bound memory.
- **markets.parquet**: 734,790 total markets, 734,521 resolved. 11 categories via regex on market question/slug.
- **Scan time**: ~57-74s for all-market dedup. ~73s for weather-only.

## Scripts

- `backtesting/enhanced_backtest.py` — Main script with `--dedup`, `--slippage`, `--category` flags
- `backtesting/weather_backtest.py` — Original weather-only (no dedup)
- `backtesting/load_data.py` — Shared data loading utilities

## Next Steps

1. **Slippage modeling**: Current slippage is uniform (additive). Real slippage depends on orderbook depth and market liquidity. Need orderbook data for realistic fills.
2. **Position sizing**: Current model uses $1 per trade. Kelly criterion or fractional sizing would improve risk-adjusted returns.
3. **Time-based analysis**: The edge is concentrated in recent months (2026-01 through 2026-03 = +$14.6K of +$19.6K total). Is this a regime change or growing pains?
4. **Live API integration**: The Polymarket CLOB API for real-time pricing and orderbook depth.
5. **Category filtering**: Sports 95-100¢ bucket (5.5% WR) vs crypto 45-55¢ bucket (52% WR) — different risk profiles. Strategy could filter by category + bucket.