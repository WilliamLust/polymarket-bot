"""
Weather Market Backtest — gopfan2 Strategy Kill Test

Strategy (from @seelffff / gopfan2):
1. Buy YES if price < $0.15
2. Buy NO if price > $0.45
3. Never bet more than $1 per position

Hypothesis: Retail systematically underprices tail probability in
temperature bucket markets. Tails (extreme hot/cold) trade at 10-12¢
but have ~18-20¢ real probability.

This is a KILL TEST — if the edge doesn't exist in historical data,
the X posts are BS and we stop building.

Test methodology:
- Load markets.parquet to identify weather/temperature markets
- Load quant.parquet (row groups) for trade data on those markets
- Apply the 3 rules to every trade
- Simulate: buy at trade price, hold to resolution, compare with outcome
- Compute: win rate, EV per trade, total P&L, Sharpe
- Apply 2% Polymarket fee on winnings
- Break down by entry price bucket, city, and time period

Assumptions:
- We can buy at the trade price (optimistic — real fills have slippage)
- Markets resolve correctly (no disputed resolutions)
- $1 per trade (as gopfan2 specifies)
- 2% fee on gross winnings only
"""

import sys
import json
import time
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

# ── Configuration ──────────────────────────────────────────────

MARKETS_PATH = "data/markets.parquet"
QUANT_PATH = "data/quant.parquet"
FEE_RATE = 0.02          # 2% Polymarket fee on winnings
MAX_POSITION = 1.0        # $1 per position (gopfan2 rule)
YES_THRESHOLD = 0.15      # Buy YES if price < this
NO_THRESHOLD = 0.45       # Buy NO if price > this

# ── Step 1: Identify weather markets ───────────────────────────

def load_weather_markets() -> pd.DataFrame:
    """Load markets.parquet and filter for temperature/weather markets."""
    print("Loading markets.parquet...")
    df = pd.read_parquet(MARKETS_PATH, columns=[
        "id", "question", "slug", "condition_id", "closed", "active",
        "outcome_prices", "volume", "event_title", "end_date", "neg_risk"
    ])
    
    # Filter for temperature/weather markets
    weather_mask = df["question"].str.contains(
        r"temperature|°F|°C|celsius|fahrenheit",
        case=False, na=False
    )
    weather = df[weather_mask].copy()
    print(f"Total markets: {len(df):,}")
    print(f"Weather markets: {len(weather):,}")
    
    # Only closed markets (we need resolved outcomes)
    weather_closed = weather[weather["closed"] == 1].copy()
    print(f"Closed weather markets: {len(weather_closed):,}")
    
    # Parse outcome prices to find resolution
    # Format: "['1', '0']" = YES won, "['0', '1']" = NO won
    def get_outcome(row):
        try:
            prices = row["outcome_prices"]
            if isinstance(prices, str):
                # Parse Python-style list strings: "['0', '1']"
                prices = prices.strip("[]").replace("'", "").split(", ")
                prices = [float(p) for p in prices]
            if isinstance(prices, list) and len(prices) >= 2:
                yes_price = float(prices[0])
                if yes_price > 0.5:
                    return "YES"
                else:
                    return "NO"
        except Exception:
            return "UNKNOWN"
    
    weather_closed["resolution"] = weather_closed.apply(get_outcome, axis=1)
    resolved = weather_closed[weather_closed["resolution"].isin(["YES", "NO"])].copy()
    print(f"Resolved weather markets: {len(resolved):,}")
    
    # Convert id to string for joining
    resolved["id"] = resolved["id"].astype(str)
    
    return resolved


# ── Step 2: Load trade data for weather markets ────────────────

def load_weather_trades(weather_market_ids: set) -> pd.DataFrame:
    """Scan quant.parquet row groups and extract trades for weather markets."""
    print(f"\nScanning quant.parquet for trades in {len(weather_market_ids):,} weather markets...")
    pf = pq.ParquetFile(QUANT_PATH)
    num_rg = pf.metadata.num_row_groups
    total_rows = pf.metadata.num_rows
    
    weather_trades = []
    trades_found = 0
    rg_processed = 0
    
    for i in range(num_rg):
        rg = pf.metadata.row_group(i)
        rg_rows = rg.num_rows
        
        # Read only the columns we need to save memory
        table = pf.read_row_group(i, columns=["market_id", "price", "usd_amount", "side", "timestamp"])
        df = table.to_pandas()
        
        # Filter for weather markets
        df["market_id"] = df["market_id"].astype(str)
        mask = df["market_id"].isin(weather_market_ids)
        matches = df[mask]
        
        if len(matches) > 0:
            weather_trades.append(matches)
            trades_found += len(matches)
        
        rg_processed += 1
        if rg_processed % 50 == 0:
            pct = rg_processed / num_rg * 100
            print(f"  RG {rg_processed}/{num_rg} ({pct:.0f}%) — {trades_found:,} weather trades found")
        
        del df, table
    
    if not weather_trades:
        print("No weather trades found!")
        return pd.DataFrame()
    
    result = pd.concat(weather_trades, ignore_index=True)
    print(f"Total weather trades: {len(result):,}")
    return result


# ── Step 3: Apply gopfan2 strategy ─────────────────────────────

def apply_strategy(trades: pd.DataFrame, markets: pd.DataFrame) -> pd.DataFrame:
    """Apply the gopfan2 3-rule strategy and compute P&L."""
    print("\nApplying gopfan2 strategy...")
    
    # Merge trades with market resolution data
    markets_slim = markets[["id", "resolution", "question", "event_title"]].rename(columns={"id": "market_id"})
    merged = trades.merge(markets_slim, on="market_id", how="left")
    
    # Drop trades without resolution
    merged = merged[merged["resolution"].isin(["YES", "NO"])].copy()
    print(f"Trades with resolution: {len(merged):,}")
    
    # Apply strategy rules
    # Rule 1: Buy YES if price < 0.15
    buy_yes = merged["price"] < YES_THRESHOLD
    # Rule 2: Buy NO if price > 0.45
    buy_no = merged["price"] > NO_THRESHOLD
    
    # Create signal column
    merged["signal"] = "SKIP"
    merged.loc[buy_yes, "signal"] = "BUY_YES"
    merged.loc[buy_no, "signal"] = "BUY_NO"
    
    # Count signals
    n_buy_yes = (merged["signal"] == "BUY_YES").sum()
    n_buy_no = (merged["signal"] == "BUY_NO").sum()
    n_skip = (merged["signal"] == "SKIP").sum()
    print(f"BUY_YES signals: {n_buy_yes:,}")
    print(f"BUY_NO signals: {n_buy_no:,}")
    print(f"SKIP: {n_skip:,}")
    
    # Compute P&L for each signal
    def compute_pnl(row):
        if row["signal"] == "SKIP":
            return 0.0
        
        cost = MAX_POSITION * row["price"]  # What we paid
        
        if row["signal"] == "BUY_YES":
            # We bought YES shares at row["price"]
            # If resolution is YES: we get $1 per share, profit = $1 - cost - fee
            # If resolution is NO: we lose cost
            if row["resolution"] == "YES":
                gross_win = MAX_POSITION  # $1 per share
                fee = gross_win * FEE_RATE
                return gross_win - cost - fee
            else:
                return -cost
        
        elif row["signal"] == "BUY_NO":
            # We bought NO at (1 - row["price"]) effective cost
            # If resolution is NO: we get $1 per share
            # If resolution is YES: we lose the NO cost
            no_cost = MAX_POSITION * (1 - row["price"])
            if row["resolution"] == "NO":
                gross_win = MAX_POSITION
                fee = gross_win * FEE_RATE
                return gross_win - no_cost - fee
            else:
                return -no_cost
    
    merged["pnl"] = merged.apply(compute_pnl, axis=1)
    
    return merged


# ── Step 4: Analyze results ────────────────────────────────────

def analyze_results(df: pd.DataFrame) -> dict:
    """Compute detailed strategy statistics."""
    signals = df[df["signal"] != "SKIP"].copy()
    
    if len(signals) == 0:
        return {"error": "No signals generated"}
    
    results = {}
    
    # Overall stats
    results["total_signals"] = len(signals)
    results["buy_yes_count"] = (signals["signal"] == "BUY_YES").sum()
    results["buy_no_count"] = (signals["signal"] == "BUY_NO").sum()
    
    # Win rate
    wins = (signals["pnl"] > 0).sum()
    losses = (signals["pnl"] < 0).sum()
    results["wins"] = int(wins)
    results["losses"] = int(losses)
    results["win_rate"] = wins / len(signals) if len(signals) > 0 else 0
    
    # P&L
    results["total_pnl"] = float(signals["pnl"].sum())
    results["avg_pnl_per_trade"] = float(signals["pnl"].mean())
    results["median_pnl"] = float(signals["pnl"].median())
    
    # EV per dollar risked
    buy_yes_trades = signals[signals["signal"] == "BUY_YES"]
    buy_no_trades = signals[signals["signal"] == "BUY_NO"]
    
    if len(buy_yes_trades) > 0:
        yes_total_risked = (buy_yes_trades["price"] * MAX_POSITION).sum()
        results["buy_yes_pnl"] = float(buy_yes_trades["pnl"].sum())
        results["buy_yes_ev_per_dollar"] = float(buy_yes_trades["pnl"].sum() / yes_total_risked) if yes_total_risked > 0 else 0
        results["buy_yes_win_rate"] = float((buy_yes_trades["pnl"] > 0).mean())
        results["buy_yes_count"] = len(buy_yes_trades)
    
    if len(buy_no_trades) > 0:
        no_total_risked = ((1 - buy_no_trades["price"]) * MAX_POSITION).sum()
        results["buy_no_pnl"] = float(buy_no_trades["pnl"].sum())
        results["buy_no_ev_per_dollar"] = float(buy_no_trades["pnl"].sum() / no_total_risked) if no_total_risked > 0 else 0
        results["buy_no_win_rate"] = float((buy_no_trades["pnl"] > 0).mean())
        results["buy_no_count"] = len(buy_no_trades)
    
    # Breakdown by entry price bucket
    results["by_price_bucket"] = {}
    for bucket_name, low, high in [
        ("penny_0-5", 0, 0.05),
        ("deep_tail_5-10", 0.05, 0.10),
        ("tail_10-15", 0.10, 0.15),
        ("mid_15-30", 0.15, 0.30),
        ("center_30-50", 0.30, 0.50),
        ("fav_50-70", 0.50, 0.70),
        ("heavy_fav_70-85", 0.70, 0.85),
        ("dominant_85-95", 0.85, 0.95),
        ("near_certain_95-100", 0.95, 1.01),
    ]:
        bucket = signals[(signals["price"] >= low) & (signals["price"] < high)]
        if len(bucket) > 0:
            results["by_price_bucket"][bucket_name] = {
                "count": len(bucket),
                "win_rate": float((bucket["pnl"] > 0).mean()),
                "avg_pnl": float(bucket["pnl"].mean()),
                "total_pnl": float(bucket["pnl"].sum()),
                "avg_price": float(bucket["price"].mean()),
            }
    
    # Breakdown by BUY_YES entry price
    results["buy_yes_by_price"] = {}
    for bucket_name, low, high in [
        ("0-5c", 0, 0.05),
        ("5-10c", 0.05, 0.10),
        ("10-15c", 0.10, 0.15),
    ]:
        bucket = buy_yes_trades[(buy_yes_trades["price"] >= low) & (buy_yes_trades["price"] < high)]
        if len(bucket) > 0:
            results["buy_yes_by_price"][bucket_name] = {
                "count": len(bucket),
                "win_rate": float((bucket["pnl"] > 0).mean()),
                "total_pnl": float(bucket["pnl"].sum()),
                "avg_pnl": float(bucket["pnl"].mean()),
            }
    
    # Breakdown by BUY_NO entry price
    results["buy_no_by_price"] = {}
    for bucket_name, low, high in [
        ("45-55c", 0.45, 0.55),
        ("55-65c", 0.55, 0.65),
        ("65-75c", 0.65, 0.75),
        ("75-85c", 0.75, 0.85),
        ("85-95c", 0.85, 0.95),
        ("95-100c", 0.95, 1.01),
    ]:
        bucket = buy_no_trades[(buy_no_trades["price"] >= low) & (buy_no_trades["price"] < high)]
        if len(bucket) > 0:
            results["buy_no_by_price"][bucket_name] = {
                "count": len(bucket),
                "win_rate": float((bucket["pnl"] > 0).mean()),
                "total_pnl": float(bucket["pnl"].sum()),
                "avg_pnl": float(bucket["pnl"].mean()),
            }
    
    # Time-based analysis
    signals["datetime"] = pd.to_datetime(signals["timestamp"], unit="s")
    signals["month"] = signals["datetime"].dt.to_period("M")
    monthly = signals.groupby("month").agg(
        trades=("pnl", "count"),
        pnl=("pnl", "sum"),
        win_rate=("pnl", lambda x: (x > 0).mean()),
    )
    results["monthly_summary"] = {
        str(k): {"trades": int(v["trades"]), "pnl": round(float(v["pnl"]), 2), "win_rate": round(float(v["win_rate"]), 3)}
        for k, v in monthly.to_dict("index").items()
    }
    
    # Simulated bankroll growth
    signals_sorted = signals.sort_values("timestamp")
    signals_sorted["cumulative_pnl"] = signals_sorted["pnl"].cumsum()
    results["final_bankroll"] = float(signals_sorted["cumulative_pnl"].iloc[-1])
    results["max_drawdown"] = float((signals_sorted["cumulative_pnl"].cummax() - signals_sorted["cumulative_pnl"]).max())
    
    return results


def print_results(results: dict) -> None:
    """Print formatted backtest results."""
    if "error" in results:
        print(f"\nERROR: {results['error']}")
        return
    
    print("\n" + "=" * 70)
    print("GOPFAN2 WEATHER STRATEGY — KILL TEST RESULTS")
    print("=" * 70)
    
    print(f"\nTotal signals: {results['total_signals']:,}")
    print(f"  BUY_YES: {results.get('buy_yes_count', 0):,}")
    print(f"  BUY_NO:  {results.get('buy_no_count', 0):,}")
    
    print(f"\n{'─' * 70}")
    print("OVERALL PERFORMANCE")
    print(f"{'─' * 70}")
    print(f"Win rate:         {results['win_rate']:.1%}")
    print(f"Wins:             {results['wins']:,}")
    print(f"Losses:           {results['losses']:,}")
    print(f"Total P&L:        ${results['total_pnl']:,.2f}")
    print(f"Avg P&L per trade: ${results['avg_pnl_per_trade']:.4f}")
    print(f"Max drawdown:     ${results['max_drawdown']:,.2f}")
    
    if "buy_yes_ev_per_dollar" in results:
        print(f"\n{'─' * 70}")
        print("BUY_YES (price < 15¢)")
        print(f"{'─' * 70}")
        print(f"Trades:           {results['buy_yes_count']:,}")
        print(f"Win rate:         {results['buy_yes_win_rate']:.1%}")
        print(f"Total P&L:        ${results['buy_yes_pnl']:,.2f}")
        print(f"EV per $ risked:  ${results['buy_yes_ev_per_dollar']:.4f}")
        
        for bucket, data in results.get("buy_yes_by_price", {}).items():
            print(f"  {bucket}: {data['count']:,} trades, {data['win_rate']:.1%} WR, ${data['total_pnl']:,.2f} P&L")
    
    if "buy_no_ev_per_dollar" in results:
        print(f"\n{'─' * 70}")
        print("BUY_NO (price > 45¢)")
        print(f"{'─' * 70}")
        print(f"Trades:           {results['buy_no_count']:,}")
        print(f"Win rate:         {results['buy_no_win_rate']:.1%}")
        print(f"Total P&L:        ${results['buy_no_pnl']:,.2f}")
        print(f"EV per $ risked:  ${results['buy_no_ev_per_dollar']:.4f}")
        
        for bucket, data in results.get("buy_no_by_price", {}).items():
            print(f"  {bucket}: {data['count']:,} trades, {data['win_rate']:.1%} WR, ${data['total_pnl']:,.2f} P&L")
    
    print(f"\n{'─' * 70}")
    print("PRICE BUCKET BREAKDOWN (all signals)")
    print(f"{'─' * 70}")
    for bucket, data in results.get("by_price_bucket", {}).items():
        wr = data['win_rate']
        ev = data['avg_pnl']
        flag = " ★ EDGE" if ev > 0.01 else (" ◆ MARGINAL" if ev > 0 else " ✗ NEGATIVE")
        print(f"  {bucket:20s}: {data['count']:>8,} trades  WR={wr:.1%}  avg=${ev:.4f}{flag}")
    
    # Monthly trend
    monthly = results.get("monthly_summary", {})
    if monthly:
        print(f"\n{'─' * 70}")
        print("MONTHLY P&L TREND")
        print(f"{'─' * 70}")
        for month, data in sorted(monthly.items()):
            wr = data['win_rate']
            pnl = data['pnl']
            trades = data['trades']
            bar = "█" * max(1, int(pnl / 10)) if pnl > 0 else "░" * max(1, int(-pnl / 10))
            print(f"  {month}: {trades:>6,} trades  WR={wr:.1%}  P&L=${pnl:>8,.2f} {bar}")
    
    # Verdict
    print(f"\n{'=' * 70}")
    print("VERDICT")
    print(f"{'=' * 70}")
    if results['total_pnl'] > 0 and results['win_rate'] > 0.5:
        print(f"✓ EDGE DETECTED — ${results['total_pnl']:,.2f} profit on {results['total_signals']:,} trades")
        print(f"  Win rate: {results['win_rate']:.1%}")
        print(f"  Avg profit per trade: ${results['avg_pnl_per_trade']:.4f}")
        print(f"  Strategy survives kill test. Proceed to paper trading.")
    elif results['total_pnl'] > 0:
        print(f"◆ MARGINAL — ${results['total_pnl']:,.2f} profit but {results['win_rate']:.1%} win rate")
        print(f"  Edge may not survive slippage and execution costs. Investigate further.")
    else:
        print(f"✗ NO EDGE — ${results['total_pnl']:,.2f} loss on {results['total_signals']:,} trades")
        print(f"  Strategy fails kill test. Do not proceed to live trading.")


# ── Main ───────────────────────────────────────────────────────

def main():
    start_time = time.time()
    
    # Step 1: Load weather markets
    weather_markets = load_weather_markets()
    if len(weather_markets) == 0:
        print("No weather markets found. Exiting.")
        sys.exit(1)
    
    weather_market_ids = set(weather_markets["id"].tolist())
    
    # Step 2: Load trade data
    weather_trades = load_weather_trades(weather_market_ids)
    if len(weather_trades) == 0:
        print("No weather trades found. Exiting.")
        sys.exit(1)
    
    # Step 3: Apply strategy
    signals = apply_strategy(weather_trades, weather_markets)
    
    # Step 4: Analyze
    results = analyze_results(signals)
    print_results(results)
    
    # Save results
    output_path = "backtesting/weather_backtest_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {output_path}")
    
    elapsed = time.time() - start_time
    print(f"Backtest completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
