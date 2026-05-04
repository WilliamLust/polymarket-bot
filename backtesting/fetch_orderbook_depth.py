"""
Fetch orderbook depth snapshots from live Polymarket CLOB API.
Calibrates slippage model by sampling NO-token asks across YES price buckets.

Output: orderbook_depth_calibration.json with per-market depth metrics.
"""

import sys
import json
import time
import pyarrow.parquet as pq
import pandas as pd
from py_clob_client.client import ClobClient

MARKETS_PATH = "data/markets.parquet"
OUTPUT_PATH = "backtesting/orderbook_depth_calibration.json"

def main():
    client = ClobClient("https://clob.polymarket.com")
    
    # Load markets
    print("Loading markets.parquet...")
    df = pq.ParquetFile(MARKETS_PATH).read(
        columns=["id", "token1", "token2", "closed", "outcome_prices", "question", "volume"]
    ).to_pandas()
    
    # Filter to active markets with volume
    active = df[(df["closed"] == False) & (df["volume"] > 1000)].copy()
    print(f"Active markets with volume > $1K: {len(active)}")
    
    # Parse YES price
    def parse_yes(prices):
        try:
            if isinstance(prices, str):
                p = prices.strip("[]").replace("'", "").split(", ")
                return float(p[0])
            elif isinstance(prices, list):
                return float(prices[0])
        except:
            return None
    
    active["yes_price"] = active["outcome_prices"].apply(parse_yes)
    active = active.dropna(subset=["yes_price"])
    
    # Sample markets across price buckets
    buckets = [
        (0.45, 0.55, 10),
        (0.55, 0.65, 10),
        (0.65, 0.75, 10),
        (0.75, 0.85, 10),
        (0.85, 0.95, 15),
        (0.95, 1.01, 20),  # Our key bucket — sample more
    ]
    
    results = []
    errors = 0
    
    for low, high, n_samples in buckets:
        bucket = active[(active["yes_price"] >= low) & (active["yes_price"] < high)]
        if len(bucket) == 0:
            print(f"  Bucket {low:.2f}-{high:.2f}: no markets")
            continue
        
        # Take top N by volume
        top = bucket.nlargest(n_samples, "volume")
        bucket_name = f"{low:.2f}-{high:.2f}"
        
        for _, m in top.iterrows():
            token2 = str(m["token2"])  # NO token (what we buy)
            try:
                book = client.get_order_book(token2)
                asks = book.asks
                if not asks:
                    continue
                
                # Sort asks by price ascending
                ask_list = sorted([(float(a.price), float(a.size)) for a in asks], key=lambda x: x[0])
                
                # Compute cumulative depth at each ask level
                total_size = 0.0
                total_cost = 0.0
                levels = []
                for price, size in ask_list:
                    total_size += size
                    total_cost += price * size
                    levels.append({
                        "price": price,
                        "size": size,
                        "cum_size": total_size,
                        "avg_price": total_cost / total_size if total_size > 0 else 0,
                    })
                
                # Extract key depth metrics
                depth_at = {}
                for threshold in [10, 50, 100, 250, 500, 1000]:
                    depth_at[threshold] = None
                    for l in levels:
                        if l["cum_size"] >= threshold:
                            depth_at[threshold] = round(l["avg_price"], 4)
                            break
                
                results.append({
                    "market_id": str(m["id"]),
                    "question": str(m["question"])[:60],
                    "yes_price": round(float(m["yes_price"]), 4),
                    "no_best_ask": round(ask_list[0][0], 4) if ask_list else None,
                    "no_best_ask_size": round(ask_list[0][1], 2) if ask_list else None,
                    "no_spread_bps": round((ask_list[0][0] - (1 - float(m["yes_price"]))) * 10000, 1) if ask_list else None,
                    "num_ask_levels": len(ask_list),
                    "total_ask_depth": round(total_size, 2),
                    "depth_at": depth_at,
                    "bucket": bucket_name,
                })
                
                spread = ask_list[0][0] - (1 - float(m["yes_price"]))
                print(
                    f"  {bucket_name} | YES={float(m['yes_price']):.3f} | "
                    f"NO best={ask_list[0][0]:.3f} spread={spread*100:.1f}c | "
                    f"size@best={ask_list[0][1]:.0f} | "
                    f"d@100={depth_at.get(100)} d@500={depth_at.get(500)}",
                    flush=True,
                )
                
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"  Error for market {m['id']}: {e}")
            
            time.sleep(0.25)  # rate limit
    
    # Save results
    with open(OUTPUT_PATH, "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\nSaved {len(results)} orderbook snapshots to {OUTPUT_PATH}")
    print(f"Errors: {errors}")
    
    # Print summary by bucket
    print("\n=== DEPTH SUMMARY BY BUCKET ===")
    for low, high, _ in buckets:
        bucket_name = f"{low:.2f}-{high:.2f}"
        bucket_results = [r for r in results if r["bucket"] == bucket_name]
        if not bucket_results:
            continue
        
        avg_spread = sum(r["no_spread_bps"] for r in bucket_results if r["no_spread_bps"] is not None) / max(1, len([r for r in bucket_results if r["no_spread_bps"] is not None]))
        avg_best_size = sum(r["no_best_ask_size"] for r in bucket_results if r["no_best_ask_size"]) / max(1, len([r for r in bucket_results if r["no_best_ask_size"]]))
        avg_depth_100 = [r["depth_at"].get("100") for r in bucket_results if r["depth_at"].get("100") is not None]
        avg_depth_500 = [r["depth_at"].get("500") for r in bucket_results if r["depth_at"].get("500") is not None]
        
        print(f"  {bucket_name}: {len(bucket_results)} markets, avg spread={avg_spread:.1f}bps, "
              f"avg best size=${avg_best_size:.0f}, "
              f"d@100={sum(avg_depth_100)/len(avg_depth_100):.4f if avg_depth_100 else 'N/A'}, "
              f"d@500={sum(avg_depth_500)/len(avg_depth_500):.4f if avg_depth_500 else 'N/A'}")


if __name__ == "__main__":
    main()
