"""
Category Flip Rate Analysis

For each category, calculate flip rate statistics:
1. Positions at YES >= 0.95 (dedup=first)
2. Win rate (NO wins = YES price resolved to 0)
3. Average NO fill cost (1 - yes_price)
4. Expected value per $1 position
5. Break-even win rate
6. Kelly fraction

Uses row-group scanning with periodic concat+dedup to avoid memory issues.
"""

import sys
import json
import time
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

# Configuration
MARKETS_PATH = Path("~/polymarket-bot/data/markets.parquet").expanduser()
QUANT_PATH = Path("~/polymarket-bot/data/quant.parquet").expanduser()
OUTPUT_PATH = Path("~/polymarket-bot/backtesting/category_flip_rates.json").expanduser()
MIN_YES_PRICE = 0.95
POSITION_SIZE = 1.0
FEE_RATE = 0.02

# Category patterns
CATEGORY_PATTERNS = {
    "weather": r"temperature|°F|°C|celsius|fahrenheit|weather|rain|snow|hurricane|tornado|wind chill|heat index",
    "crypto": r"bitcoin|btc|eth|ethereum|crypto|solana|sol|dogecoin|doge|token|defi|nft|blockchain|xrp|ripple",
    "sports": r"nfl|nba|mlb|nhl|soccer|football|basketball|baseball|hockey|super bowl|world cup|championship|playoff|game|match|score|win the|world series|stanley cup|fa cup|premier league|la liga",
    "politics": r"trump|biden|harris|republican|democrat|congress|senate|president|governor|election|primary|supreme court|politics|putin|russia|ukraine|china|xi|europe|eu|macron|zelensky|nato|iran|israel|hamas|uk election|parliament",
    "entertainment": r"oscar|emmy|grammy|box office|movie|film|album|song|award|celebrity|show|series|tv|television|netflix|hbo|disney|pop culture|music|artist|actor",
    "tech": r"apple|google|microsoft|amazon|tesla|meta|ai|openai|gpt|launch|ipo|stock|share price|market cap|science|spacex|nasa|moon|mars|rocket|fusion|particle|discovery|research|study|climate|technology",
    "finance": r"gdp|inflation|cpi|fed|interest rate|recession|unemployment|jobs report|fomc|treasury|bond|finance|economics|markets|s&p|dow jones|nasdaq|futures|trading",
}


def classify_market(question, event_slug=None):
    text = str(question).lower()
    if event_slug:
        text += " " + str(event_slug).lower()
    for category, pattern in CATEGORY_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            return category
    return "other"


def get_resolution(outcome_prices):
    try:
        prices = outcome_prices
        if isinstance(prices, str):
            prices = prices.strip("[]").replace("'", "").split(", ")
            prices = [float(p) for p in prices]
        if isinstance(prices, list) and len(prices) >= 2:
            yes_price = float(prices[0])
            return "YES" if yes_price > 0.5 else "NO"
    except:
        pass
    return "UNKNOWN"


def load_markets_with_categories():
    print("Loading markets.parquet...", flush=True)
    df = pd.read_parquet(MARKETS_PATH, columns=[
        "id", "question", "slug", "event_slug", "outcome_prices", "closed"
    ])
    print(f"Total markets: {len(df):,}", flush=True)
    df["resolution"] = df["outcome_prices"].apply(get_resolution)
    resolved = df[df["resolution"].isin(["YES", "NO"])].copy()
    print(f"Resolved markets: {len(resolved):,}", flush=True)
    resolved["category"] = resolved.apply(
        lambda row: classify_market(row["question"], row.get("event_slug")), axis=1
    )
    cat_counts = resolved["category"].value_counts()
    print("\nCategory distribution:", flush=True)
    for cat, count in cat_counts.items():
        print(f"  {cat:15s}: {count:>6,}", flush=True)
    resolved["id"] = resolved["id"].astype(str)
    return resolved[["id", "resolution", "category"]]


def scan_trades_for_flip_positions(resolved_market_ids):
    print(f"\nScanning quant.parquet for YES >= {MIN_YES_PRICE} positions...", flush=True)
    print(f"Target: {len(resolved_market_ids):,} resolved markets", flush=True)
    
    pf = pq.ParquetFile(QUANT_PATH)
    num_rg = pf.metadata.num_row_groups
    
    rg_chunks = []
    trades_seen = 0
    t0 = time.time()
    last_print = 0
    resolved_set = resolved_market_ids
    
    for i in range(num_rg):
        table = pf.read_row_group(i, columns=["market_id", "price", "timestamp"])
        df = table.to_pandas()
        df["market_id"] = df["market_id"].astype(str)
        trades_seen += len(df)
        
        # Filter to YES >= 0.95 and resolved markets
        mask = df["price"] >= MIN_YES_PRICE
        df = df[mask]
        df = df[df["market_id"].isin(resolved_set)]
        
        if len(df) > 0:
            df = df.sort_values("timestamp")
            df = df.drop_duplicates(subset="market_id", keep="first")
            rg_chunks.append(df)
        
        if len(rg_chunks) >= 100:
            merged = pd.concat(rg_chunks, ignore_index=True)
            merged = merged.sort_values("timestamp")
            merged = merged.drop_duplicates(subset="market_id", keep="first")
            rg_chunks = [merged]
            import gc
            gc.collect()
        
        now = time.time()
        if (i + 1) % 50 == 0 or (now - last_print > 30):
            elapsed = now - t0
            pct = (i + 1) / num_rg * 100
            chunk_rows = sum(len(c) for c in rg_chunks)
            eta = elapsed / (i + 1) * (num_rg - i - 1)
            print(f"  RG {i+1}/{num_rg} ({pct:.0f}%) - {trades_seen:,} raw, {chunk_rows:,} candidates - {elapsed:.0f}s, ETA {eta:.0f}s", flush=True)
            last_print = now
        
        del df, table
    
    if not rg_chunks:
        print("No positions found!", flush=True)
        return pd.DataFrame()
    
    print(f"Final merge of {len(rg_chunks)} chunks...", flush=True)
    combined = pd.concat(rg_chunks, ignore_index=True)
    combined = combined.sort_values("timestamp")
    combined = combined.drop_duplicates(subset="market_id", keep="first")
    print(f"Scan complete: {trades_seen:,} raw rows -> {len(combined):,} flip positions", flush=True)
    return combined.reset_index(drop=True)


def compute_category_stats(positions, markets):
    print("\nComputing statistics...", flush=True)
    merged = positions.merge(markets, left_on="market_id", right_on="id", how="left")
    merged = merged.dropna(subset=["resolution", "category"])
    print(f"Positions with category/resolution: {len(merged):,}", flush=True)
    
    results = {}
    categories = sorted(merged["category"].unique())
    
    for cat in categories:
        cat_df = merged[merged["category"] == cat]
        n_positions = len(cat_df)
        if n_positions == 0:
            continue
        
        wins = (cat_df["resolution"] == "NO").sum()
        win_rate = wins / n_positions
        no_costs = 1.0 - cat_df["price"]
        avg_no_cost = no_costs.mean()
        
        # Vectorized EV calculation
        payouts = np.where(
            cat_df["resolution"] == "NO",
            1 - FEE_RATE - (1 - cat_df["price"]),
            -(1 - cat_df["price"])
        )
        ev_per_dollar = payouts.mean()
        breakeven_wr = avg_no_cost / (1 - FEE_RATE)
        
        if avg_no_cost > 0 and avg_no_cost < 1:
            b = (1 - avg_no_cost) / avg_no_cost
            p = win_rate
            q = 1 - p
            kelly = max(0, (b * p - q) / b if b > 0 else 0)
        else:
            kelly = 0
        
        results[cat] = {
            "positions": int(n_positions),
            "win_rate": round(win_rate, 4),
            "avg_no_cost": round(avg_no_cost, 4),
            "ev_per_dollar": round(ev_per_dollar, 4),
            "breakeven_wr": round(breakeven_wr, 4),
            "kelly_fraction": round(kelly, 4),
        }
        print(f"  {cat:15s}: {n_positions:>6,} pos, WR={win_rate:.1%}, EV=${ev_per_dollar:.4f}, Kelly={kelly:.2%}", flush=True)
    
    # All categories combined
    n_all = len(merged)
    wins_all = (merged["resolution"] == "NO").sum()
    wr_all = wins_all / n_all
    no_costs_all = 1.0 - merged["price"]
    avg_cost_all = no_costs_all.mean()
    payouts_all = np.where(
        merged["resolution"] == "NO",
        1 - FEE_RATE - (1 - merged["price"]),
        -(1 - merged["price"])
    )
    ev_all = payouts_all.mean()
    be_all = avg_cost_all / (1 - FEE_RATE)
    
    if avg_cost_all > 0 and avg_cost_all < 1:
        b_all = (1 - avg_cost_all) / avg_cost_all
        kelly_all = max(0, (b_all * wr_all - (1 - wr_all)) / b_all)
    else:
        kelly_all = 0
    
    results["all"] = {
        "positions": int(n_all),
        "win_rate": round(wr_all, 4),
        "avg_no_cost": round(avg_cost_all, 4),
        "ev_per_dollar": round(ev_all, 4),
        "breakeven_wr": round(be_all, 4),
        "kelly_fraction": round(kelly_all, 4),
    }
    print(f"  all             : {n_all:>6,} pos, WR={wr_all:.1%}, EV=${ev_all:.4f}, Kelly={kelly_all:.2%}", flush=True)
    
    return results


def main():
    print("=" * 60, flush=True)
    print("CATEGORY FLIP RATE ANALYSIS", flush=True)
    print("=" * 60, flush=True)
    print(f"Min YES price: {MIN_YES_PRICE}", flush=True)
    print(f"Position size: ${POSITION_SIZE}", flush=True)
    print(f"Fee rate: {FEE_RATE * 100:.0f}%", flush=True)
    
    start_time = time.time()
    
    markets = load_markets_with_categories()
    resolved_ids = set(markets["id"].tolist())
    
    positions = scan_trades_for_flip_positions(resolved_ids)
    
    if len(positions) == 0:
        print("No positions found. Exiting.", flush=True)
        sys.exit(1)
    
    category_stats = compute_category_stats(positions, markets)
    
    output = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "position_size": POSITION_SIZE,
        "min_yes": MIN_YES_PRICE,
        "categories": category_stats,
    }
    
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\nResults saved to {OUTPUT_PATH}", flush=True)
    
    elapsed = time.time() - start_time
    print(f"\n" + "=" * 60, flush=True)
    print("SUMMARY", flush=True)
    print("=" * 60, flush=True)
    
    all_stats = category_stats.get("all", {})
    print(f"Total positions: {all_stats.get('positions', 0):,}", flush=True)
    print(f"Overall win rate: {all_stats.get('win_rate', 0):.1%}", flush=True)
    print(f"Average NO cost: ${all_stats.get('avg_no_cost', 0):.4f}", flush=True)
    print(f"EV per $1: ${all_stats.get('ev_per_dollar', 0):.4f}", flush=True)
    print(f"Break-even WR: {all_stats.get('breakeven_wr', 0):.1%}", flush=True)
    print(f"Kelly fraction: {all_stats.get('kelly_fraction', 0):.2%}", flush=True)
    
    sorted_cats = sorted(
        [(k, v) for k, v in category_stats.items() if k != "all" and v["positions"] >= 10],
        key=lambda x: x[1]["ev_per_dollar"],
        reverse=True
    )
    
    if sorted_cats:
        print(f"\nTop categories by EV (min 10 positions):", flush=True)
        for cat, stats in sorted_cats[:5]:
            print(f"  {cat:15s}: EV=${stats['ev_per_dollar']:.4f}, WR={stats['win_rate']:.1%}, Kelly={stats['kelly_fraction']:.2%}", flush=True)
    
    print(f"\nAnalysis completed in {elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
