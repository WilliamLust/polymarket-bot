"""
Realistic Slippage Backtest — Orderbook-Calibrated Fill Model

Uses live orderbook depth data to model realistic NO-fill prices per bucket.
Key finding: slippage is NOT uniform. Some buckets have negative slippage
(NO asks below theoretical), others have extreme positive slippage.

Calibrated from 39 live orderbook snapshots across 6 price buckets.

Usage:
    source venv/bin/activate
    python backtesting/realistic_slippage_backtest.py
    python backtesting/realistic_slippage_backtest.py --position-size 10
    python backtesting/realistic_slippage_backtest.py --position-size 100
"""

import sys
import json
import time
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

# ── Configuration ──────────────────────────────────────────────

MARKETS_PATH = "data/markets.parquet"
QUANT_PATH = "data/quant.parquet"
DEPTH_CALIBRATION_PATH = "backtesting/orderbook_depth_calibration.json"
FEE_RATE = 0.02
YES_THRESHOLD = 0.15
NO_THRESHOLD = 0.45

# Realistic fill model: NO_fill_cost = theoretical_cost * (1 + slippage_pct)
# Calibrated from live orderbook data. Negative = better than theoretical.
# At $1 position size (well within best ask for most markets).
BUCKET_SLIPPAGE = {
    # bucket: (low, high, avg_slippage_pct)
    # avg_slippage_pct = (NO_best_ask / (1-YES_price) - 1) * 100
    "45-55":  (0.45, 0.55,  0.167),   # 16.7% over theoretical
    "55-65":  (0.55, 0.65,  0.129),   # 12.9%
    "65-75":  (0.65, 0.75,  0.089),   # 8.9%
    "75-85":  (0.75, 0.85, -0.080),   # -8.0% (better than theoretical!)
    "85-95":  (0.85, 0.95,  0.638),   # 63.8% (terrible fills)
    "95-100": (0.95, 1.01, -0.651),   # -65.1% (much better than theoretical!)
}

def get_bucket(yes_price: float) -> str:
    for name, (low, high, _) in BUCKET_SLIPPAGE.items():
        if low <= yes_price < high:
            return name
    return "unknown"


# ── Step 1: Load markets ──────────────────────────────────────

def load_markets(category: str = None) -> pd.DataFrame:
    print("Loading markets.parquet...")
    df = pd.read_parquet(MARKETS_PATH, columns=[
        "id", "question", "slug", "condition_id", "closed", "active",
        "outcome_prices", "volume", "event_title", "end_date", "neg_risk"
    ])
    
    print(f"Total markets: {len(df):,}")
    
    def get_outcome(row):
        try:
            prices = row["outcome_prices"]
            if isinstance(prices, str):
                prices = prices.strip("[]").replace("'", "").split(", ")
                prices = [float(p) for p in prices]
            if isinstance(prices, list) and len(prices) >= 2:
                yes_price = float(prices[0])
                return "YES" if yes_price > 0.5 else "NO"
        except:
            return "UNKNOWN"
    
    df["resolution"] = df.apply(get_outcome, axis=1)
    resolved = df[df["resolution"].isin(["YES", "NO"])].copy()
    resolved["id"] = resolved["id"].astype(str)
    print(f"Resolved markets: {len(resolved):,}")
    
    # Categorize
    resolved["category"] = "other"
    category_patterns = {
        "weather": r"temperature|°F|°C|celsius|fahrenheit|weather|rain|snow|hurricane|tornado",
        "crypto": r"bitcoin|btc|eth|ethereum|crypto|solana|sol|dogecoin|doge|token|defi|nft|blockchain",
        "politics_us": r"trump|biden|harris|republican|democrat|congress|senate|president|governor|election|primary|supreme court",
        "politics_world": r"putin|russia|ukraine|china|xi|europe|eu|macron|zelensky|nato|iran|israel|hamas|uk",
        "sports": r"nfl|nba|mlb|nhl|soccer|football|basketball|baseball|hockey|super bowl|world cup|championship|playoff|game|match|score|win the",
        "economics": r"gdp|inflation|cpi|fed|interest rate|recession|unemployment|jobs report|fomc|treasury|bond",
        "tech": r"apple|google|microsoft|amazon|tesla|meta|ai|openai|gpt|launch|ipo|stock|share price|market cap",
        "science": r"spacex|nasa|moon|mars|rocket|launch|fusion|particle|discovery|research|study|climate",
        "entertainment": r"oscar|emmy|grammy|box office|movie|film|album|song|award|celebrity|show|series",
        "covid": r"covid|pandemic|vaccine|variant|cases|hospitalization|lockdown|mask",
    }
    for cat, pattern in category_patterns.items():
        mask = resolved["question"].str.contains(pattern, case=False, na=False)
        resolved.loc[mask & (resolved["category"] == "other"), "category"] = cat
    
    if category and category != "all":
        before = len(resolved)
        resolved = resolved[resolved["category"] == category].copy()
        print(f"Filtered to '{category}': {len(resolved):,} markets (from {before:,})")
    
    return resolved


# ── Step 2: Load deduped trades ───────────────────────────────

def load_trades_deduped(market_ids: set, dedup_mode: str = "first") -> pd.DataFrame:
    print(f"\nScanning quant.parquet for trades in {len(market_ids):,} markets (dedup={dedup_mode})...")
    pf = pq.ParquetFile(QUANT_PATH)
    num_rg = pf.metadata.num_row_groups
    keep = "first" if dedup_mode == "first" else "last"
    skip_filter = len(market_ids) > 100_000
    
    rg_chunks = []
    trades_seen = 0
    t0 = time.time()
    import gc
    
    for i in range(num_rg):
        table = pf.read_row_group(i, columns=["market_id", "price", "usd_amount", "side", "timestamp"])
        df = table.to_pandas()
        df["market_id"] = df["market_id"].astype(str)
        trades_seen += len(df)
        
        if not skip_filter:
            df = df[df["market_id"].isin(market_ids)]
        
        if len(df) > 0:
            df = df.sort_values("timestamp")
            df = df.drop_duplicates(subset="market_id", keep=keep)
            rg_chunks.append(df)
        
        if len(rg_chunks) >= 100:
            merged_chunk = pd.concat(rg_chunks, ignore_index=True)
            merged_chunk = merged_chunk.sort_values("timestamp")
            merged_chunk = merged_chunk.drop_duplicates(subset="market_id", keep=keep)
            rg_chunks = [merged_chunk]
            gc.collect()
        
        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            pct = (i + 1) / num_rg * 100
            chunk_rows = sum(len(c) for c in rg_chunks)
            eta = elapsed / (i + 1) * (num_rg - i - 1)
            print(f"  RG {i+1}/{num_rg} ({pct:.0f}%) — {trades_seen:,} raw, {chunk_rows:,} deduped — {elapsed:.0f}s, ETA {eta:.0f}s", flush=True)
        
        del df, table
    
    if not rg_chunks:
        print("No trades found!")
        return pd.DataFrame()
    
    print(f"Merging {len(rg_chunks)} chunks...", flush=True)
    combined = pd.concat(rg_chunks, ignore_index=True)
    del rg_chunks
    combined = combined.sort_values("timestamp")
    combined = combined.drop_duplicates(subset="market_id", keep=keep)
    
    if skip_filter:
        before = len(combined)
        combined = combined[combined["market_id"].isin(market_ids)]
        print(f"  Filtered: {before:,} → {len(combined):,}")
    
    print(f"Scan complete: {trades_seen:,} raw → {len(combined):,} deduped positions")
    return combined.reset_index(drop=True)


# ── Step 3: Apply strategy with realistic fills ───────────────

def apply_strategy_realistic(trades: pd.DataFrame, markets: pd.DataFrame, position_size: float = 1.0) -> pd.DataFrame:
    print(f"\nApplying strategy with REALISTIC fills (position_size=${position_size})...")
    print("Fill model: orderbook-calibrated slippage per bucket")
    for name, (low, high, slip) in BUCKET_SLIPPAGE.items():
        sign = "+" if slip >= 0 else ""
        print(f"  {name}¢: {sign}{slip*100:.1f}% slippage")
    
    # Merge
    markets_slim = markets[["id", "resolution", "question", "event_title", "category"]].rename(columns={"id": "market_id"})
    merged = trades.merge(markets_slim, on="market_id", how="left")
    merged = merged[merged["resolution"].isin(["YES", "NO"])].copy()
    print(f"Positions with resolution: {len(merged):,}")
    
    # Signals
    buy_yes = merged["price"] < YES_THRESHOLD
    buy_no = merged["price"] > NO_THRESHOLD
    merged["signal"] = "SKIP"
    merged.loc[buy_yes, "signal"] = "BUY_YES"
    merged.loc[buy_no, "signal"] = "BUY_NO"
    
    n_buy_yes = (merged["signal"] == "BUY_YES").sum()
    n_buy_no = (merged["signal"] == "BUY_NO").sum()
    print(f"BUY_YES: {n_buy_yes:,}")
    print(f"BUY_NO:  {n_buy_no:,}")
    
    # Compute bucket for each position
    merged["bucket"] = merged["price"].apply(get_bucket)
    
    # Compute P&L with realistic fills
    merged["pnl"] = 0.0
    merged["fill_price"] = 0.0
    
    # BUY_YES: use trade price (conservative — no slippage data for YES side)
    yes_mask = merged["signal"] == "BUY_YES"
    if yes_mask.any():
        fill = merged.loc[yes_mask, "price"]
        cost = position_size * fill
        win_payout = position_size - position_size * FEE_RATE - cost
        lose_payout = -cost
        resolved_yes = merged.loc[yes_mask, "resolution"] == "YES"
        merged.loc[yes_mask, "pnl"] = np.where(resolved_yes, win_payout, lose_payout)
        merged.loc[yes_mask, "fill_price"] = fill
    
    # BUY_NO: apply bucket-specific slippage
    no_mask = merged["signal"] == "BUY_NO"
    if no_mask.any():
        no_sub = merged.loc[no_mask]
        theoretical_cost = (1 - no_sub["price"])  # e.g. YES=0.95 → NO cost=0.05
        slippage_mult = no_sub["bucket"].map(lambda b: BUCKET_SLIPPAGE.get(b, (0, 0, 0))[2])
        
        # Realistic fill cost = theoretical * (1 + slippage_pct)
        # Negative slippage means CHEAPER fills (better for us)
        actual_fill_cost = theoretical_cost * (1 + slippage_mult)
        actual_fill_cost = actual_fill_cost.clip(lower=0.001, upper=0.99)
        
        no_cost = position_size * actual_fill_cost
        win_payout = position_size - position_size * FEE_RATE - no_cost
        lose_payout = -no_cost
        resolved_no = merged.loc[no_mask, "resolution"] == "NO"
        merged.loc[no_mask, "pnl"] = np.where(resolved_no, win_payout, lose_payout)
        merged.loc[no_mask, "fill_price"] = actual_fill_cost
    
    return merged


# ── Step 4: Analyze and print ─────────────────────────────────

def analyze_and_print(df: pd.DataFrame, position_size: float) -> None:
    signals = df[df["signal"] != "SKIP"].copy()
    
    print("\n" + "=" * 70)
    print("REALISTIC SLIPPAGE BACKTEST — ORDERBOOK-CALIBRATED")
    print("=" * 70)
    print(f"Position size: ${position_size}")
    print(f"Fill model: bucket-specific slippage from live orderbook data")
    
    print(f"\nTotal positions: {len(signals):,}")
    buy_no = signals[signals["signal"] == "BUY_NO"]
    buy_yes = signals[signals["signal"] == "BUY_YES"]
    
    print(f"\n{'─' * 70}")
    print("OVERALL")
    print(f"{'─' * 70}")
    total_pnl = signals["pnl"].sum()
    wr = (signals["pnl"] > 0).mean()
    print(f"Win rate:       {wr:.1%}")
    print(f"Total P&L:      ${total_pnl:,.2f}")
    
    sorted_signals = signals.sort_values("timestamp")
    cum = sorted_signals["pnl"].cumsum()
    max_dd = (cum.cummax() - cum).max()
    print(f"Max drawdown:   ${max_dd:,.2f}")
    
    print(f"\n{'─' * 70}")
    print("BUY_NO (with realistic fills)")
    print(f"{'─' * 70}")
    if len(buy_no) > 0:
        print(f"Count:       {len(buy_no):,}")
        print(f"Win rate:    {(buy_no['pnl'] > 0).mean():.1%}")
        print(f"P&L:         ${buy_no['pnl'].sum():,.2f}")
        
        # Per bucket
        print(f"\n  {'Bucket':<12} {'Count':>8} {'WR':>7} {'Avg Fill':>10} {'Theo Cost':>10} {'P&L':>12} {'vs 0-slip':>10}")
        print(f"  {'─'*65}")
        for name, (low, high, slip) in BUCKET_SLIPPAGE.items():
            bucket = buy_no[(buy_no["price"] >= low) & (buy_no["price"] < high)]
            if len(bucket) == 0:
                continue
            avg_fill = bucket["fill_price"].mean()
            avg_theo = (1 - bucket["price"]).mean()
            pnl = bucket["pnl"].sum()
            wr = (bucket["pnl"] > 0).mean()
            # Compare to 0-slippage P&L for same bucket
            no_cost_0slip = (1 - bucket["price"]) * position_size
            win_0 = position_size - position_size * FEE_RATE - no_cost_0slip
            lose_0 = -no_cost_0slip
            resolved_no = bucket["resolution"] == "NO"
            pnl_0slip = np.where(resolved_no, win_0, lose_0).sum()
            diff = pnl - pnl_0slip
            sign = "+" if slip >= 0 else ""
            print(f"  {name:<12} {len(bucket):>8,} {wr:>6.1%} {avg_fill*100:>8.2f}c  {avg_theo*100:>8.2f}c  ${pnl:>10,.2f} {diff:>+9,.0f}")
    
    print(f"\n{'─' * 70}")
    print("BUY_YES")
    print(f"{'─' * 70}")
    if len(buy_yes) > 0:
        print(f"Count:       {len(buy_yes):,}")
        print(f"Win rate:    {(buy_yes['pnl'] > 0).mean():.1%}")
        print(f"P&L:         ${buy_yes['pnl'].sum():,.2f}")
    
    # Category breakdown
    print(f"\n{'─' * 70}")
    print("BY CATEGORY (BUY_NO only)")
    print(f"{'─' * 70}")
    for cat in sorted(signals["category"].unique()):
        cat_no = buy_no[buy_no["category"] == cat]
        if len(cat_no) > 0:
            pnl = cat_no["pnl"].sum()
            flag = " ★" if pnl > 0 else " ✗"
            print(f"  {cat:20s}: {len(cat_no):>6,} pos  WR={(cat_no['pnl'] > 0).mean():.1%}  P&L=${pnl:>10,.2f}{flag}")
    
    # Verdict
    print(f"\n{'=' * 70}")
    print("VERDICT")
    print(f"{'=' * 70}")
    no_pnl = buy_no["pnl"].sum() if len(buy_no) > 0 else 0
    no_wr = (buy_no["pnl"] > 0).mean() if len(buy_no) > 0 else 0
    
    if no_pnl > 5000 and no_wr > 0.15:
        print(f"★ STRONG EDGE — ${no_pnl:,.2f} BUY_NO profit, {no_wr:.1%} WR")
        print(f"  Max DD ${max_dd:,.2f}. Orderbook-calibrated fills confirm edge.")
    elif no_pnl > 0:
        print(f"◆ MARGINAL EDGE — ${no_pnl:,.2f} BUY_NO profit, {no_wr:.1%} WR")
    else:
        print(f"✗ NO EDGE — ${no_pnl:,.2f} BUY_NO result")
    
    # Save results
    results = {
        "position_size": position_size,
        "total_pnl": float(total_pnl),
        "buy_no_pnl": float(no_pnl),
        "buy_no_wr": float(no_wr),
        "max_drawdown": float(max_dd),
        "fill_model": "orderbook-calibrated",
        "slippage_by_bucket": {name: slip for name, (_, _, slip) in BUCKET_SLIPPAGE.items()},
    }
    output_path = "backtesting/realistic_slippage_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {output_path}")


# ── Main ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Realistic slippage backtest with orderbook-calibrated fills")
    parser.add_argument("--position-size", type=float, default=1.0,
                        help="Position size in dollars (default: $1)")
    parser.add_argument("--dedup", choices=["first", "last"], default="first")
    parser.add_argument("--category", default=None)
    args = parser.parse_args()
    
    start_time = time.time()
    
    print("Realistic Slippage Backtest")
    print(f"  Position size: ${args.position_size}")
    print(f"  Dedup: {args.dedup}")
    print(f"  Category: {args.category or 'all'}")
    
    markets = load_markets(category=args.category)
    if len(markets) == 0:
        print("No markets found.")
        sys.exit(1)
    
    market_ids = set(markets["id"].tolist())
    trades = load_trades_deduped(market_ids, dedup_mode=args.dedup)
    if len(trades) == 0:
        print("No trades found.")
        sys.exit(1)
    
    signals = apply_strategy_realistic(trades, markets, position_size=args.position_size)
    analyze_and_print(signals, args.position_size)
    
    elapsed = time.time() - start_time
    print(f"\nBacktest completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
