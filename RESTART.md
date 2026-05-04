# Polymarket Bot — RESTART.md

## Project Status: Strategy Optimized with Realistic Fill Model

**Last updated**: Session with orderbook-calibrated slippage integration
**Git**: main branch, commit 2b08312 (+ uncommitted realistic slippage work)

---

## Key Finding: The Strategy Works, But Only at 95+¢

The flat slippage model was wrong. Orderbook-calibrated fills reveal that slippage varies wildly by bucket:

| YES Bucket | Slippage | Effect | Playable? |
|-----------|----------|--------|-----------|
| 45-55¢    | +16.7%   | Pay 58¢ for 50¢ NO — kills edge | NO |
| 55-65¢    | +12.9%   | Moderate overpayment | Marginal |
| 65-75¢    | +8.9%    | Slight overpayment | Marginal |
| 75-85¢    | -8.0%    | Fills BELOW theoretical | YES |
| 85-95¢    | +63.8%   | Pay 16¢ for 10¢ NO — but 29% WR beats 17% breakeven | YES |
| 95-100¢   | -65.1%   | Pay 0.56¢ for 1.58¢ NO — huge advantage | YES |

### Why negative slippage at 95-100¢?
YES and NO trade on separate orderbooks. At 95¢ YES, the NO ask sits at ~0.56¢ — below the implied 5¢ (1 - 0.95). This happens because YES market makers leave a spread, and NO liquidity providers compete at very tight levels. You get filled at essentially zero cost.

---

## Final Strategy: BUY_NO at YES >= 95¢ Only

| Metric | Value |
|--------|-------|
| Positions | 59,353 |
| Win rate | 9.0% |
| Avg fill | 0.56¢ (theoretical: 1.58¢) |
| Slippage advantage | +1.02¢ per position vs theoretical |
| P&L at $1/pos | +$4,898 |
| P&L at $10/pos | +$48,980 |
| P&L at $50/pos | +$244,901 |
| Max drawdown at $10/pos | $2,868 |
| Sharpe ratio | 0.295 |

### By Category (95-100¢ bucket, $10/position):
| Category | Count | WR | P&L |
|----------|-------|----|----|
| other | 30,690 | 10.3% | +$29,379 |
| sports | 19,605 | 5.3% | +$9,270 |
| crypto | 5,445 | 9.1% | +$4,524 |
| tech | 2,377 | 12.7% | +$2,778 |
| politics_us | 602 | 32.4% | +$1,857 |
| weather | 565 | 17.9% | +$954 |
| economics | 69 | 33.3% | +$220 |

### Entry Threshold Optimization:
| Min YES | Positions | WR | P&L $10 | MaxDD $10 |
|---------|-----------|----|---------|-----------|
| 0.45 | 350K | 43.4% | -$27,372 | $725K |
| 0.55 | 132K | 26.0% | +$92,498 | $140K |
| 0.65 | 103K | 19.5% | +$89,336 | $70K |
| 0.75 | 86K | 15.7% | +$81,586 | $36K |
| 0.85 | 73K | 12.7% | +$65,456 | $18K |
| 0.95 | 59K | 9.0% | +$48,980 | $3K |
| 0.99 | 38K | 5.5% | +$19,253 | $1K |

Higher threshold = lower P&L but MUCH lower drawdown. 95¢ is the sweet spot.

---

## Other Buckets Worth Playing

**75-85¢ bucket**: Negative slippage (-8%), fills at 19.2¢ vs 20.9¢ theoretical. Breakeven WR 19.6%, actual WR 31.8%. Comfortable edge. 13,472 positions.

**85-95¢ bucket**: Despite 63.8% slippage (paying 16.4¢ for 10¢ NO), breakeven WR is only 16.7% and actual WR is 29.3%. Still profitable, but thin. 13,349 positions.

---

## Data Sources

- **quant.parquet**: 36GB, 568.6M trades, 577 row groups. Deduped to 615K (1/market, keep=first)
- **markets.parquet**: 735K markets, 734K resolved (YES/NO)
- **orderbook_depth_calibration.json**: 39 live CLOB snapshots, 6 price buckets
- Slippage model: `actual_no_cost = theoretical * (1 + bucket_slippage_pct)`

---

## Files

| File | Purpose |
|------|---------|
| `backtesting/enhanced_backtest.py` | Dedup + flat slippage + all-category |
| `backtesting/realistic_slippage_backtest.py` | Orderbook-calibrated fill model |
| `backtesting/strategy_optimizer.py` | Entry threshold optimization |
| `backtesting/fetch_orderbook_depth.py` | Live orderbook sampler |
| `backtesting/orderbook_depth_calibration.json` | 39 calibration snapshots |
| `backtesting/weather_backtest.py` | Original (overcounted) weather backtest |

---

## Caveats & Risks

1. **Calibration sample is small** — only 39 orderbook snapshots. 36/75 failed (resolved/inactive markets). Should re-sample with active markets only.
2. **No historical orderbook data** — current live slippage applied to 4+ years of historical trades. Market microstructure may have changed.
3. **Survivorship bias** — we only see resolved markets. Cancelled/voided markets excluded.
4. **US residents can't trade** — ToS restriction. Backtesting only.
5. **The $100/pos P&L of $490K is unrealistic** — at $100/position, you eat through the orderbook. The $98K depth at best ask evaporates fast with size.
6. **Negative slippage is suspicious** — paying less than theoretical sounds great but may be a sampling artifact. Need more data points.
7. **Strategy is well-known** — other bots likely already arbing this edge. Live performance will be worse.

---

## Next Steps

1. **Re-sample orderbooks with active markets only** — filter `active=true` before querying CLOB API
2. **Build live paper-trading bot** — deploy with $1 positions to validate fill assumptions
3. **Size-aware slippage model** — model fill price as function of order size × bucket depth
4. **Combine 75-85 + 95-100 buckets** — both have favorable slippage; blended strategy
5. **Time-of-day analysis** — slippage may vary with market hours/liquidity
6. **Void market handling** — some resolved markets get voided (refund). Need to handle these.
