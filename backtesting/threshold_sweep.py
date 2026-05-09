#!/usr/bin/env python3
"""
Threshold sweep: fine-grained 1¢ bins from 0.75 to 1.00.
Shows WR, P&L per $1 position, and edge per threshold.
"""
import sys, gc
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

MARKETS_PATH = "data/markets.parquet"
QUANT_PATH = "data/quant.parquet"
FEE_RATE = 0.02

# Orderbook-calibrated slippage per 5¢ bucket
BUCKET_SLIP = {
    "75-80": -0.080, "80-85": -0.080,
    "85-90": 0.638, "90-95": 0.638,
    "95-100": -0.651,
}

def load_data():
    print("Loading markets...", flush=True)
    df = pd.read_parquet(MARKETS_PATH, columns=["id","question","outcome_prices"])
    def get_outcome(row):
        try:
            prices = row["outcome_prices"]
            if isinstance(prices, str):
                prices = [float(p) for p in prices.strip("[]").replace("'","").split(", ")]
            if isinstance(prices, list) and len(prices) >= 2:
                return "YES" if float(prices[0]) > 0.5 else "NO"
        except: return "UNKNOWN"
    df["resolution"] = df.apply(get_outcome, axis=1)
    resolved = df[df["resolution"].isin(["YES","NO"])].copy()
    resolved["id"] = resolved["id"].astype(str)

    # Category
    resolved["category"] = "other"
    cats = {
        "weather": r"temperature|°F|°C|celsius|fahrenheit|weather|rain|snow|hurricane|tornado",
        "crypto": r"bitcoin|btc|eth|ethereum|crypto|solana|sol|dogecoin|doge|token|defi|nft|blockchain",
        "sports": r"nfl|nba|mlb|nhl|soccer|football|basketball|baseball|hockey|super bowl|world cup|championship|playoff|game|win the",
        "politics_us": r"trump|biden|harris|republican|democrat|congress|senate|president|governor|election|supreme court",
        "economics": r"gdp|inflation|cpi|fed|interest rate|recession|unemployment|fomc|treasury|bond",
        "tech": r"apple|google|microsoft|amazon|tesla|meta|ai|openai|gpt|launch|ipo|stock|share price",
    }
    for cat, pat in cats.items():
        mask = resolved["question"].str.contains(pat, case=False, na=False)
        resolved.loc[mask & (resolved["category"] == "other"), "category"] = cat

    market_ids = set(resolved["id"].tolist())

    print("Loading trades...", flush=True)
    pf = pq.ParquetFile(QUANT_PATH)
    num_rg = pf.metadata.num_row_groups
    rg_chunks = []

    for i in range(num_rg):
        table = pf.read_row_group(i, columns=["market_id","price","usd_amount","timestamp"])
        d = table.to_pandas()
        d["market_id"] = d["market_id"].astype(str)
        d = d.sort_values("timestamp")
        d = d.drop_duplicates(subset="market_id", keep="first")
        if len(d) > 0:
            rg_chunks.append(d)
        if len(rg_chunks) >= 100:
            merged = pd.concat(rg_chunks, ignore_index=True).sort_values("timestamp")
            merged = merged.drop_duplicates(subset="market_id", keep="first")
            rg_chunks = [merged]
            gc.collect()
        if (i+1) % 100 == 0:
            print(f"  RG {i+1}/{num_rg}", flush=True)

    if len(rg_chunks) > 1:
        rg_chunks = [pd.concat(rg_chunks, ignore_index=True).sort_values("timestamp")
                      .drop_duplicates(subset="market_id", keep="first")]

    first_trades = rg_chunks[0]
    data = resolved.merge(first_trades, left_on="id", right_on="market_id", how="inner")
    data = data[data["price"].notna()].copy()

    data["theoretical_no_cost"] = 1.0 - data["price"]
    data["yes_bucket"] = data["price"].apply(lambda p:
        "75-80" if p < 0.80 else
        "80-85" if p < 0.85 else
        "85-90" if p < 0.90 else
        "90-95" if p < 0.95 else "95-100")
    data["slippage"] = data["yes_bucket"].map(BUCKET_SLIP)
    data["actual_no_cost"] = data["theoretical_no_cost"] * (1 + data["slippage"])
    data["actual_no_cost"] = data["actual_no_cost"].clip(lower=0.01, upper=0.99)

    return data


def main():
    data = load_data()
    print(f"\nLoaded {len(data):,} positions\n")

    # Fine-grained 1¢ bins from 0.75 to 1.00
    print("=" * 95)
    print("THRESHOLD SWEEP: BUY_NO at YES >= X¢ (1¢ increments, $1/position, realistic fills)")
    print("=" * 95)
    print(f"{'YES ≥':>6} {'Count':>9} {'WR':>7} {'AvgFill':>9} {'EV/pos':>8} {'P&L $1':>10} {'Sharpe':>8} {'MaxDD$10':>10}")
    print("-" * 95)

    results = []
    for threshold_pct in range(75, 100):
        threshold = threshold_pct / 100.0
        subset = data[data["price"] >= threshold].copy()
        if len(subset) == 0:
            continue

        resolved_no = subset["resolution"] == "NO"
        wr = resolved_no.mean()
        avg_fill = subset["actual_no_cost"].mean()

        # Per $1 position
        cost = subset["actual_no_cost"] * 1  # $1 position
        win = 1 - 1 * FEE_RATE - cost
        lose = -cost
        pnl = np.where(resolved_no, win, lose)
        total_pnl = pnl.sum()
        avg_ev = pnl.mean()
        sharpe = pnl.mean() / pnl.std() if pnl.std() > 0 else 0

        # MaxDD at $10/pos
        cost10 = subset["actual_no_cost"] * 10
        win10 = 10 - 10 * FEE_RATE - cost10
        lose10 = -cost10
        pnl10 = np.where(resolved_no, win10, lose10)
        sorted10 = pd.Series(pnl10).sort_values()
        cum10 = sorted10.cumsum()
        maxdd10 = (cum10.cummax() - cum10).max()

        results.append({
            "threshold": threshold,
            "count": len(subset),
            "wr": wr,
            "avg_fill": avg_fill,
            "ev_per_pos": avg_ev,
            "pnl_1": total_pnl,
            "sharpe": sharpe,
            "maxdd_10": maxdd10,
        })

        print(f"  {threshold_pct:>4}¢ {len(subset):>9,} {wr:>6.1%} {avg_fill*100:>7.2f}c ${avg_ev:>7.4f} ${total_pnl:>9,.2f} {sharpe:>8.4f} ${maxdd10:>9,.2f}")

    # Find optimal threshold (highest Sharpe)
    best = max(results, key=lambda x: x["sharpe"])
    print(f"\n  BEST SHARPE: YES >= {best['threshold']*100:.0f}¢ — WR={best['wr']:.1%}, EV=${best['ev_per_pos']:.4f}/pos, Sharpe={best['sharpe']:.4f}")

    # Also show incremental: what does adding 85-94¢ contribute?
    print("\n" + "=" * 95)
    print("INCREMENTAL ANALYSIS: What each band adds on top of 95¢+")
    print("=" * 95)
    print(f"{'Band':>10} {'Count':>9} {'WR':>7} {'AvgFill':>9} {'EV/pos':>8} {'P&L $1':>10} {'Sharpe':>8}")
    print("-" * 75)

    bands = [
        ("95-100¢", 0.95, 1.01),
        ("90-95¢", 0.90, 0.95),
        ("85-90¢", 0.85, 0.90),
        ("80-85¢", 0.80, 0.85),
        ("75-80¢", 0.75, 0.80),
    ]

    for name, lo, hi in bands:
        subset = data[(data["price"] >= lo) & (data["price"] < hi)].copy()
        if len(subset) == 0:
            continue
        resolved_no = subset["resolution"] == "NO"
        wr = resolved_no.mean()
        avg_fill = subset["actual_no_cost"].mean()
        cost = subset["actual_no_cost"]
        win = 1 - FEE_RATE - cost
        lose = -cost
        pnl = np.where(resolved_no, win, lose)
        sharpe = pnl.mean() / pnl.std() if pnl.std() > 0 else 0
        print(f"  {name:>8} {len(subset):>9,} {wr:>6.1%} {avg_fill*100:>7.2f}c ${pnl.mean():>7.4f} ${pnl.sum():>9,.2f} {sharpe:>8.4f}")

    # Category breakdown for 85-95¢ band
    print("\n" + "=" * 95)
    print("CATEGORY BREAKDOWN: 85-95¢ YES Band (the expansion opportunity)")
    print("=" * 95)
    mid = data[(data["price"] >= 0.85) & (data["price"] < 0.95)].copy()
    print(f"  {'Category':20s} {'Count':>7} {'WR':>7} {'AvgFill':>9} {'EV/pos':>8} {'P&L $10':>10}")
    print(f"  {'-'*68}")

    for cat in sorted(mid["category"].unique()):
        sub = mid[mid["category"] == cat]
        if len(sub) < 5:
            continue
        resolved_no = sub["resolution"] == "NO"
        wr = resolved_no.mean()
        avg_fill = sub["actual_no_cost"].mean()
        cost = sub["actual_no_cost"] * 10
        win = 10 - 10 * FEE_RATE - cost
        lose = -cost
        pnl = np.where(resolved_no, win, lose).sum()
        print(f"  {cat:20s} {len(sub):>7,} {wr:>6.1%} {avg_fill*100:>7.2f}c ${pnl/len(sub)/10:>7.4f} ${pnl:>10,.2f}")

    # Compare 90-95 vs 95-100
    print("\n" + "=" * 95)
    print("HEAD-TO-HEAD: 90-95¢ vs 95-100¢ (which is the real sweet spot?)")
    print("=" * 95)
    for name, lo, hi in [("90-95¢", 0.90, 0.95), ("95-100¢", 0.95, 1.01)]:
        sub = data[(data["price"] >= lo) & (data["price"] < hi)].copy()
        resolved_no = sub["resolution"] == "NO"
        wr = resolved_no.mean()
        avg_fill = sub["actual_no_cost"].mean()
        cost = sub["actual_no_cost"] * 10
        win = 10 - 10 * FEE_RATE - cost
        lose = -cost
        pnl = np.where(resolved_no, win, lose)
        sorted_pnl = pd.Series(pnl).sort_values()
        cum = sorted_pnl.cumsum()
        maxdd = (cum.cummax() - cum).max()
        sharpe = pnl.mean() / pnl.std() if pnl.std() > 0 else 0
        print(f"\n  {name}:")
        print(f"    Positions: {len(sub):,}  WR: {wr:.1%}  AvgFill: {avg_fill*100:.2f}c")
        print(f"    P&L $10: ${pnl.sum():,.2f}  MaxDD: ${maxdd:,.2f}  Sharpe: {sharpe:.4f}")
        print(f"    EV per $1: ${np.where(resolved_no, 1-FEE_RATE-sub['actual_no_cost'], -sub['actual_no_cost']).mean():.4f}")


if __name__ == "__main__":
    main()
