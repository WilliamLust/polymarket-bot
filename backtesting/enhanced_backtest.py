"""
Enhanced Backtest — Dedup + Slippage + All-Market Expansion

Improvements over weather_backtest.py:
1. Deduplication: one position per market_id (first trade signals entry, no double-counting)
2. Slippage simulation: configurable spread model for real-fill approximation
3. All-market expansion: test the NO-favorites edge across ALL market categories

The gopfan2 claim is BACKWARDS. The real edge is:
- Buy NO when YES price > 45¢ (retail overprices favorites)
- Best bucket: 95-100¢ YES price, buy NO at ≤5¢, 4.8% WR but +$12,697 P&L
- This script tests whether that edge survives dedup, slippage, and holds across categories.

Usage:
    source venv/bin/activate
    python backtesting/enhanced_backtest.py
    python backtesting/enhanced_backtest.py --slippage 0.03
    python backtesting/enhanced_backtest.py --category weather
    python backtesting/enhanced_backtest.py --dedup last   # use last trade per market
"""

import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

# ── Configuration ──────────────────────────────────────────────

MARKETS_PATH = "data/markets.parquet"
QUANT_PATH = "data/quant.parquet"
FEE_RATE = 0.02          # 2% Polymarket fee on winnings
MAX_POSITION = 1.0        # $1 per position
YES_THRESHOLD = 0.15      # Buy YES if price < this
NO_THRESHOLD = 0.45       # Buy NO if price > this

# Slippage model: for YES price at X, buy NO costs (1-X) + slippage
# Slippage is additive to the cost side. Default 0 = no slippage.
DEFAULT_SLIPPAGE = 0.0

# ── Step 1: Load and categorize markets ────────────────────────

def load_markets(category: str = None) -> pd.DataFrame:
    """Load markets.parquet, parse resolution, optionally filter by category."""
    print("Loading markets.parquet...")
    df = pd.read_parquet(MARKETS_PATH, columns=[
        "id", "question", "slug", "condition_id", "closed", "active",
        "outcome_prices", "volume", "event_title", "end_date", "neg_risk"
    ])
    
    print(f"Total markets: {len(df):,}")
    
    # Parse outcome prices to find resolution
    def get_outcome(row):
        try:
            prices = row["outcome_prices"]
            if isinstance(prices, str):
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
    
    df["resolution"] = df.apply(get_outcome, axis=1)
    resolved = df[df["resolution"].isin(["YES", "NO"])].copy()
    resolved["id"] = resolved["id"].astype(str)
    print(f"Resolved markets: {len(resolved):,}")
    
    # Categorize markets by topic
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
    
    # Print category counts
    cat_counts = resolved["category"].value_counts()
    print(f"\nMarket categories:")
    for cat, count in cat_counts.items():
        print(f"  {cat:20s}: {count:>6,}")
    
    # Filter by requested category
    if category:
        if category == "all":
            pass  # use all resolved markets
        else:
            before = len(resolved)
            resolved = resolved[resolved["category"] == category].copy()
            print(f"\nFiltered to '{category}': {len(resolved):,} markets (from {before:,})")
    
    return resolved


# ── Step 2: Load trade data with dedup ────────────────────────

def load_trades_deduped(market_ids: set, dedup_mode: str = "first") -> pd.DataFrame:
    """
    Scan quant.parquet row groups and extract trades for target markets.
    
    Uses drop_duplicates (hash-based, O(n)) instead of groupby+sort for speed.
    For dedup=first/last: per-row-group drop_duplicates, then final merge.
    For all-market scans: skip the isin() filter entirely.
    """
    print(f"\nScanning quant.parquet for trades in {len(market_ids):,} markets (dedup={dedup_mode})...")
    pf = pq.ParquetFile(QUANT_PATH)
    num_rg = pf.metadata.num_row_groups
    
    if dedup_mode in ("first", "last"):
        keep = "first" if dedup_mode == "first" else "last"
        skip_filter = len(market_ids) > 100_000
        
# Collect per-RG deduped chunks, then final merge
        rg_chunks = []
        trades_seen = 0
        t0 = time.time()
        last_print = 0
        
        for i in range(num_rg):
            table = pf.read_row_group(i, columns=["market_id", "price", "usd_amount", "side", "timestamp"])
            df = table.to_pandas()
            df["market_id"] = df["market_id"].astype(str)
            trades_seen += len(df)
            
            # Filter to target markets (skip for all-market — filter at end)
            if not skip_filter:
                df = df[df["market_id"].isin(market_ids)]
            
            if len(df) > 0:
                # Fast dedup: sort by timestamp, then drop_duplicates keeps first/last per market
                df = df.sort_values("timestamp")
                df = df.drop_duplicates(subset="market_id", keep=keep)
                rg_chunks.append(df)
            
            # Periodic merge to bound memory (every 100 row groups)
            if len(rg_chunks) >= 100:
                merged_chunk = pd.concat(rg_chunks, ignore_index=True)
                merged_chunk = merged_chunk.sort_values("timestamp")
                merged_chunk = merged_chunk.drop_duplicates(subset="market_id", keep=keep)
                rg_chunks = [merged_chunk]
                import gc; gc.collect()
            
            now = time.time()
            if (i + 1) % 25 == 0 or (now - last_print > 30):
                elapsed = now - t0
                pct = (i + 1) / num_rg * 100
                chunk_rows = sum(len(c) for c in rg_chunks)
                eta = elapsed / (i + 1) * (num_rg - i - 1)
                print(f"  RG {i+1}/{num_rg} ({pct:.0f}%) — {trades_seen:,} raw rows, {chunk_rows:,} deduped — {elapsed:.0f}s elapsed, ETA {eta:.0f}s", flush=True)
                last_print = now
            
            del df, table
        
        if not rg_chunks:
            print("No trades found for target markets!")
            return pd.DataFrame()
        
        print(f"Merging {len(rg_chunks)} chunks...", flush=True)
        combined = pd.concat(rg_chunks, ignore_index=True)
        del rg_chunks
        
        # Final dedup across all row groups
        combined = combined.sort_values("timestamp")
        combined = combined.drop_duplicates(subset="market_id", keep=keep)
        
        # Filter to resolved markets (for all-market scans)
        if skip_filter:
            before = len(combined)
            combined = combined[combined["market_id"].isin(market_ids)]
            print(f"  Filtered to resolved markets: {before:,} → {len(combined):,}")
        
        print(f"Scan complete: {trades_seen:,} raw rows → {len(combined):,} deduped positions (mode={dedup_mode})")
        combined = combined.reset_index(drop=True)
        return combined
    
    else:
        # 'none' or 'median_price' — must store all trades (memory-intensive)
        market_trades = defaultdict(list)
        trades_found = 0
        rg_processed = 0
        
        for i in range(num_rg):
            table = pf.read_row_group(i, columns=["market_id", "price", "usd_amount", "side", "timestamp"])
            df = table.to_pandas()
            df["market_id"] = df["market_id"].astype(str)
            
            mask = df["market_id"].isin(market_ids)
            matches = df[mask]
            
            if len(matches) > 0:
                for _, row in matches.iterrows():
                    market_trades[row["market_id"]].append({
                        "timestamp": row["timestamp"],
                        "price": row["price"],
                        "side": row["side"],
                        "usd_amount": row["usd_amount"],
                    })
                trades_found += len(matches)
            
            rg_processed += 1
            if rg_processed % 50 == 0:
                pct = rg_processed / num_rg * 100
                print(f"  RG {rg_processed}/{num_rg} ({pct:.0f}%) — {trades_found:,} raw trades, {len(market_trades):,} unique markets", flush=True)
            
            del df, table
        
        if not market_trades:
            print("No trades found for target markets!")
            return pd.DataFrame()
        
        print(f"Raw trades found: {trades_found:,} across {len(market_trades):,} markets")
        
        if dedup_mode == "none":
            rows = []
            for mid, trades in market_trades.items():
                for t in trades:
                    rows.append({"market_id": mid, **t})
            result = pd.DataFrame(rows)
            print(f"Returning all {len(result):,} trades (no dedup)")
            return result
        
        # median_price dedup
        deduped = []
        for mid, trades in market_trades.items():
            prices = [t["price"] for t in trades]
            median_p = np.median(prices)
            best = min(trades, key=lambda t: abs(t["price"] - median_p))
            deduped.append({"market_id": mid, **best})
        
        result = pd.DataFrame(deduped)
        print(f"Deduped: {len(result):,} positions (one per market, mode={dedup_mode})")
        return result


# ── Step 3: Apply strategy with slippage ───────────────────────

def apply_strategy(trades: pd.DataFrame, markets: pd.DataFrame, slippage: float = 0.0) -> pd.DataFrame:
    """Apply the revised gopfan2 strategy (BUY_NO at favorites) with slippage simulation."""
    print(f"\nApplying strategy (slippage={slippage}¢)...")
    
    # Merge trades with market resolution data
    markets_slim = markets[["id", "resolution", "question", "event_title", "category"]].rename(columns={"id": "market_id"})
    merged = trades.merge(markets_slim, on="market_id", how="left")
    
    # Drop trades without resolution
    merged = merged[merged["resolution"].isin(["YES", "NO"])].copy()
    print(f"Positions with resolution: {len(merged):,}")
    
    # Apply strategy rules
    buy_yes = merged["price"] < YES_THRESHOLD
    buy_no = merged["price"] > NO_THRESHOLD
    
    merged["signal"] = "SKIP"
    merged.loc[buy_yes, "signal"] = "BUY_YES"
    merged.loc[buy_no, "signal"] = "BUY_NO"
    
    n_buy_yes = (merged["signal"] == "BUY_YES").sum()
    n_buy_no = (merged["signal"] == "BUY_NO").sum()
    n_skip = (merged["signal"] == "SKIP").sum()
    print(f"BUY_YES: {n_buy_yes:,}")
    print(f"BUY_NO:  {n_buy_no:,}")
    print(f"SKIP:    {n_skip:,}")
    
    # Compute P&L with slippage (vectorized — no iterrows)
    # BUY_YES: fill_price = min(price + slippage, 0.99), cost = fill_price
    #   WIN  (res=YES): payout = 1 - fee*1 - cost = 1 - 0.02 - cost
    #   LOSE (res=NO):  payout = -cost
    # BUY_NO: no_fill_cost = min((1-price) + slippage, 0.99)
    #   WIN  (res=NO):  payout = 1 - fee*1 - no_fill_cost
    #   LOSE (res=YES): payout = -no_fill_cost
    yes_mask = merged["signal"] == "BUY_YES"
    no_mask  = merged["signal"] == "BUY_NO"
    
    merged["pnl"] = 0.0
    
    # BUY_YES positions
    if yes_mask.any():
        fill = (merged.loc[yes_mask, "price"] + slippage).clip(upper=0.99)
        cost = MAX_POSITION * fill
        win_payout = MAX_POSITION - MAX_POSITION * FEE_RATE - cost
        lose_payout = -cost
        resolved_yes = merged.loc[yes_mask, "resolution"] == "YES"
        merged.loc[yes_mask, "pnl"] = np.where(resolved_yes, win_payout, lose_payout)
    
    # BUY_NO positions
    if no_mask.any():
        no_fill = ((1 - merged.loc[no_mask, "price"]) + slippage).clip(upper=0.99)
        no_cost = MAX_POSITION * no_fill
        win_payout = MAX_POSITION - MAX_POSITION * FEE_RATE - no_cost
        lose_payout = -no_cost
        resolved_no = merged.loc[no_mask, "resolution"] == "NO"
        merged.loc[no_mask, "pnl"] = np.where(resolved_no, win_payout, lose_payout)
    
    return merged


# ── Step 4: Analyze results ────────────────────────────────────

def analyze_results(df: pd.DataFrame) -> dict:
    """Compute detailed strategy statistics with per-category breakdown."""
    signals = df[df["signal"] != "SKIP"].copy()
    
    if len(signals) == 0:
        return {"error": "No signals generated"}
    
    results = {}
    
    # Overall stats
    results["total_signals"] = len(signals)
    results["buy_yes_count"] = int((signals["signal"] == "BUY_YES").sum())
    results["buy_no_count"] = int((signals["signal"] == "BUY_NO").sum())
    
    wins = (signals["pnl"] > 0).sum()
    losses = (signals["pnl"] < 0).sum()
    results["wins"] = int(wins)
    results["losses"] = int(losses)
    results["win_rate"] = float(wins / len(signals))
    
    results["total_pnl"] = float(signals["pnl"].sum())
    results["avg_pnl_per_trade"] = float(signals["pnl"].mean())
    results["median_pnl"] = float(signals["pnl"].median())
    
    # Per-signal-type stats
    for sig_type in ["BUY_YES", "BUY_NO"]:
        sub = signals[signals["signal"] == sig_type]
        if len(sub) > 0:
            if sig_type == "BUY_YES":
                risked = (sub["price"] * MAX_POSITION).sum()
            else:
                risked = ((1 - sub["price"]) * MAX_POSITION).sum()
            
            results[f"{sig_type.lower()}_pnl"] = float(sub["pnl"].sum())
            results[f"{sig_type.lower()}_ev_per_dollar"] = float(sub["pnl"].sum() / risked) if risked > 0 else 0
            results[f"{sig_type.lower()}_win_rate"] = float((sub["pnl"] > 0).mean())
            results[f"{sig_type.lower()}_count"] = len(sub)
    
    # Price bucket breakdown (BUY_NO only — that's where the edge is)
    results["buy_no_by_price"] = {}
    buy_no_trades = signals[signals["signal"] == "BUY_NO"]
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
    
    # Per-category breakdown
    results["by_category"] = {}
    for cat in sorted(signals["category"].unique()):
        cat_df = signals[signals["category"] == cat]
        if len(cat_df) > 0:
            cat_no = cat_df[cat_df["signal"] == "BUY_NO"]
            results["by_category"][cat] = {
                "total_signals": len(cat_df),
                "buy_no_count": len(cat_no),
                "buy_no_pnl": float(cat_no["pnl"].sum()) if len(cat_no) > 0 else 0,
                "buy_no_win_rate": float((cat_no["pnl"] > 0).mean()) if len(cat_no) > 0 else 0,
                "total_pnl": float(cat_df["pnl"].sum()),
                "win_rate": float((cat_df["pnl"] > 0).mean()),
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


def print_results(results: dict, slippage: float, dedup_mode: str, category: str) -> None:
    """Print formatted backtest results."""
    if "error" in results:
        print(f"\nERROR: {results['error']}")
        return
    
    print("\n" + "=" * 70)
    print("ENHANCED BACKTEST — NO-FAVORITES EDGE")
    print("=" * 70)
    print(f"Slippage: {slippage}¢  |  Dedup: {dedup_mode}  |  Category: {category or 'all'}")
    
    print(f"\nTotal positions: {results['total_signals']:,}")
    print(f"  BUY_YES (<15¢): {results.get('buy_yes_count', 0):,}")
    print(f"  BUY_NO  (>45¢): {results.get('buy_no_count', 0):,}")
    
    print(f"\n{'─' * 70}")
    print("OVERALL")
    print(f"{'─' * 70}")
    print(f"Win rate:       {results['win_rate']:.1%}")
    print(f"Total P&L:      ${results['total_pnl']:,.2f}")
    print(f"Avg per trade:  ${results['avg_pnl_per_trade']:.4f}")
    print(f"Max drawdown:   ${results['max_drawdown']:,.2f}")
    
    if "buy_yes_ev_per_dollar" in results:
        print(f"\n{'─' * 70}")
        print("BUY_YES (price < 15¢)")
        print(f"{'─' * 70}")
        print(f"Count:       {results['buy_yes_count']:,}")
        print(f"Win rate:    {results['buy_yes_win_rate']:.1%}")
        print(f"P&L:         ${results['buy_yes_pnl']:,.2f}")
        print(f"EV/$:        ${results['buy_yes_ev_per_dollar']:.4f}")
    
    if "buy_no_ev_per_dollar" in results:
        print(f"\n{'─' * 70}")
        print("BUY_NO (price > 45¢) ← THE EDGE")
        print(f"{'─' * 70}")
        print(f"Count:       {results['buy_no_count']:,}")
        print(f"Win rate:    {results['buy_no_win_rate']:.1%}")
        print(f"P&L:         ${results['buy_no_pnl']:,.2f}")
        print(f"EV/$:        ${results['buy_no_ev_per_dollar']:.4f}")
        
        for bucket, data in results.get("buy_no_by_price", {}).items():
            flag = " ★" if data["total_pnl"] > 100 else ""
            print(f"  {bucket:10s}: {data['count']:>6,} pos  WR={data['win_rate']:.1%}  P&L=${data['total_pnl']:>10,.2f}{flag}")
    
    # Category breakdown
    by_cat = results.get("by_category", {})
    if by_cat:
        print(f"\n{'─' * 70}")
        print("BY CATEGORY (BUY_NO only)")
        print(f"{'─' * 70}")
        for cat, data in sorted(by_cat.items(), key=lambda x: x[1]["buy_no_pnl"], reverse=True):
            if data["buy_no_count"] > 0:
                flag = " ★" if data["buy_no_pnl"] > 0 else " ✗"
                print(f"  {cat:20s}: {data['buy_no_count']:>6,} pos  WR={data['buy_no_win_rate']:.1%}  P&L=${data['buy_no_pnl']:>10,.2f}{flag}")
    
    # Monthly trend
    monthly = results.get("monthly_summary", {})
    if monthly:
        print(f"\n{'─' * 70}")
        print("MONTHLY P&L TREND")
        print(f"{'─' * 70}")
        for month, data in sorted(monthly.items()):
            pnl = data['pnl']
            bar = "█" * max(1, int(pnl / 50)) if pnl > 0 else "░" * max(1, int(-pnl / 50))
            print(f"  {month}: {data['trades']:>6,} pos  WR={data['win_rate']:.1%}  P&L=${pnl:>8,.2f} {bar}")
    
    # Verdict
    print(f"\n{'=' * 70}")
    print("VERDICT")
    print(f"{'=' * 70}")
    no_pnl = results.get("buy_no_pnl", 0)
    no_wr = results.get("buy_no_win_rate", 0)
    dd = results["max_drawdown"]
    
    if no_pnl > 1000 and no_wr > 0.15:
        print(f"★ STRONG EDGE — ${no_pnl:,.2f} BUY_NO profit, {no_wr:.1%} WR")
        print(f"  Max DD ${dd:,.2f}. Survives slippage={slippage}¢, dedup={dedup_mode}.")
    elif no_pnl > 0:
        print(f"◆ MARGINAL EDGE — ${no_pnl:,.2f} BUY_NO profit, {no_wr:.1%} WR")
        print(f"  Edge thin. May not survive real execution.")
    else:
        print(f"✗ NO EDGE — ${no_pnl:,.2f} BUY_NO result")
        print(f"  Strategy fails.")


# ── Main ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Enhanced backtest with dedup, slippage, all-market expansion")
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE,
                        help="Slippage in dollars added to fill cost (e.g., 0.03 = 3¢)")
    parser.add_argument("--dedup", choices=["first", "last", "median_price", "none"], default="first",
                        help="Deduplication mode: one position per market")
    parser.add_argument("--category", default=None,
                        help="Filter to market category (weather, crypto, politics_us, sports, etc.) or 'all'")
    args = parser.parse_args()
    
    start_time = time.time()
    
    print(f"Enhanced Backtest")
    print(f"  Slippage: {args.slippage}¢")
    print(f"  Dedup: {args.dedup}")
    print(f"  Category: {args.category or 'all'}")
    
    # Step 1: Load markets
    markets = load_markets(category=args.category)
    if len(markets) == 0:
        print("No markets found. Exiting.")
        sys.exit(1)
    
    market_ids = set(markets["id"].tolist())
    
    # Step 2: Load trades with dedup
    trades = load_trades_deduped(market_ids, dedup_mode=args.dedup)
    if len(trades) == 0:
        print("No trades found. Exiting.")
        sys.exit(1)
    
    # Step 3: Apply strategy with slippage
    signals = apply_strategy(trades, markets, slippage=args.slippage)
    
    # Step 4: Analyze
    results = analyze_results(signals)
    print_results(results, args.slippage, args.dedup, args.category)
    
    # Save results
    output_path = "backtesting/enhanced_backtest_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {output_path}")
    
    elapsed = time.time() - start_time
    print(f"Backtest completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
