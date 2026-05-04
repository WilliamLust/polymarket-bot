"""
Strategy optimizer: test different entry thresholds with realistic fills.
Find the sweet spot where slippage doesn't kill the edge.
"""
import sys, time
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

MARKETS_PATH = "data/markets.parquet"
QUANT_PATH = "data/quant.parquet"
FEE_RATE = 0.02

# Orderbook-calibrated slippage per bucket
BUCKET_SLIP = {
    "45-55": 0.167, "55-65": 0.129, "65-75": 0.089,
    "75-85": -0.080, "85-95": 0.638, "95-100": -0.651,
}

def get_bucket(p):
    if p < 0.55: return "45-55"
    if p < 0.65: return "55-65"
    if p < 0.75: return "65-75"
    if p < 0.85: return "75-85"
    if p < 0.95: return "85-95"
    return "95-100"

def load_data():
    print("Loading markets...")
    df = pd.read_parquet(MARKETS_PATH, columns=["id","question","outcome_prices"])
    def get_outcome(row):
        try:
            prices = row["outcome_prices"]
            if isinstance(prices, str):
                prices = [float(p) for p in prices.strip("[]").replace("'", "").split(", ")]
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
    
    print(f"Loading trades (dedup=first) — skip isin, use drop_duplicates only...")
    pf = pq.ParquetFile(QUANT_PATH)
    num_rg = pf.metadata.num_row_groups
    rg_chunks = []
    trades_seen = 0
    import gc
    
    for i in range(num_rg):
        table = pf.read_row_group(i, columns=["market_id","price","usd_amount","timestamp"])
        d = table.to_pandas()
        d["market_id"] = d["market_id"].astype(str)
        trades_seen += len(d)
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
            chunk_rows = sum(len(c) for c in rg_chunks)
            print(f"  RG {i+1}/{num_rg} — {trades_seen:,} raw, {chunk_rows:,} deduped", flush=True)
    
    print(f"Merging {len(rg_chunks)} chunks...", flush=True)
    combined = pd.concat(rg_chunks, ignore_index=True).sort_values("timestamp")
    combined = combined.drop_duplicates(subset="market_id", keep="first")
    del rg_chunks
    
    # Now filter to resolved markets
    before = len(combined)
    combined = combined[combined["market_id"].isin(market_ids)]
    print(f"Filter to resolved: {before:,} -> {len(combined):,}")
    
    slim = resolved[["id","resolution","category"]].rename(columns={"id":"market_id"})
    merged = combined.merge(slim, on="market_id", how="left")
    merged = merged[merged["resolution"].isin(["YES","NO"])].copy()
    print(f"Loaded {len(merged):,} deduped positions with resolution")
    return merged

def main():
    data = load_data()
    
    # Apply slippage model
    data["bucket"] = data["price"].apply(get_bucket)
    data["theoretical_no_cost"] = 1 - data["price"]
    data["slippage_mult"] = data["bucket"].map(BUCKET_SLIP).fillna(0)
    data["actual_no_cost"] = (data["theoretical_no_cost"] * (1 + data["slippage_mult"])).clip(lower=0.001, upper=0.99)
    
    print("\n" + "=" * 78)
    print("STRATEGY OPTIMIZER: Entry Threshold vs Realistic P&L")
    print("=" * 78)
    
    # Test different minimum YES price thresholds
    print(f"\n{'Min YES':>8} {'Positions':>10} {'WR':>7} {'P&L $1':>10} {'P&L $10':>11} {'P&L $50':>11} {'MaxDD $10':>11}")
    print("-" * 78)
    
    for min_yes in [0.45, 0.55, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.97, 0.99]:
        subset = data[data["price"] >= min_yes].copy()
        if len(subset) == 0:
            continue
        
        resolved_no = subset["resolution"] == "NO"
        wr = resolved_no.mean()
        
        results = {}
        for pos_size in [1, 10, 50]:
            cost = subset["actual_no_cost"] * pos_size
            win = pos_size - pos_size * FEE_RATE - cost
            lose = -cost
            pnl = np.where(resolved_no, win, lose)
            total = pnl.sum()
            
            if pos_size == 10:
                sorted_pnl = pd.Series(pnl).sort_values()
                cum = sorted_pnl.cumsum()
                max_dd = (cum.cummax() - cum).max()
                results["maxdd"] = max_dd
            
            results[f"pnl_{pos_size}"] = total
        
        print(f"{min_yes:>7.2f} {len(subset):>10,} {wr:>6.1%} ${results['pnl_1']:>9,.2f} ${results['pnl_10']:>10,.2f} ${results['pnl_50']:>10,.2f} ${results['maxdd']:>10,.2f}")
    
    # Deep dive: 95-100 bucket by category
    print("\n" + "=" * 78)
    print("DEEP DIVE: 95-100¢ Bucket by Category (realistic fills)")
    print("=" * 78)
    
    fav = data[data["price"] >= 0.95].copy()
    print(f"\n  {'Category':20s} {'Count':>7} {'WR':>7} {'Avg Fill':>9} {'P&L $10':>11} {'P&L $50':>11}")
    print(f"  {'-'*68}")
    
    for cat in sorted(fav["category"].unique()):
        sub = fav[fav["category"] == cat]
        if len(sub) < 5:
            continue
        resolved_no = sub["resolution"] == "NO"
        avg_fill = sub["actual_no_cost"].mean() * 100
        for pos_size in [10, 50]:
            cost = sub["actual_no_cost"] * pos_size
            win = pos_size - pos_size * FEE_RATE - cost
            lose = -cost
            pnl = np.where(resolved_no, win, lose).sum()
            if pos_size == 10:
                pnl10 = pnl
            else:
                pnl50 = pnl
        print(f"  {cat:20s} {len(sub):>7,} {resolved_no.mean():>6.1%} {avg_fill:>7.2f}c ${pnl10:>10,.2f} ${pnl50:>10,.2f}")
    
    # 85-95 bucket analysis (high slippage problem)
    print("\n" + "=" * 78)
    print("DEEP DIVE: 85-95¢ Bucket (63.8% slippage — is it playable?)")
    print("=" * 78)
    
    mid = data[(data["price"] >= 0.85) & (data["price"] < 0.95)].copy()
    resolved_no = mid["resolution"] == "NO"
    # What WR would we need to break even at realistic fills?
    avg_fill = mid["actual_no_cost"].mean()
    avg_theo = mid["theoretical_no_cost"].mean()
    breakeven_wr = avg_fill / (1 - FEE_RATE)
    actual_wr = resolved_no.mean()
    
    print(f"  Count: {len(mid):,}")
    print(f"  Theoretical NO cost: {avg_theo*100:.2f}c")
    print(f"  Actual fill cost:    {avg_fill*100:.2f}c")
    print(f"  Breakeven WR:        {breakeven_wr:.1%}")
    print(f"  Actual WR:           {actual_wr:.1%}")
    print(f"  Verdict: {'PASS' if actual_wr > breakeven_wr else 'SKIP'} (need {breakeven_wr:.1%} WR, have {actual_wr:.1%})")
    
    # 75-85 bucket (negative slippage — free money?)
    print("\n" + "=" * 78)
    print("DEEP DIVE: 75-85¢ Bucket (negative slippage — better than theoretical)")
    print("=" * 78)
    
    low = data[(data["price"] >= 0.75) & (data["price"] < 0.85)].copy()
    resolved_no = low["resolution"] == "NO"
    avg_fill = low["actual_no_cost"].mean()
    avg_theo = low["theoretical_no_cost"].mean()
    breakeven_wr = avg_fill / (1 - FEE_RATE)
    actual_wr = resolved_no.mean()
    
    print(f"  Count: {len(low):,}")
    print(f"  Theoretical NO cost: {avg_theo*100:.2f}c")
    print(f"  Actual fill cost:    {avg_fill*100:.2f}c")
    print(f"  Breakeven WR:        {breakeven_wr:.1%}")
    print(f"  Actual WR:           {actual_wr:.1%}")
    print(f"  Verdict: {'PLAY' if actual_wr > breakeven_wr else 'SKIP'}")
    
    # FINAL RECOMMENDATION
    print("\n" + "=" * 78)
    print("FINAL STRATEGY RECOMMENDATION")
    print("=" * 78)
    
    # The optimal strategy is 95-100 only
    opt = data[data["price"] >= 0.95].copy()
    resolved_no = opt["resolution"] == "NO"
    wr = resolved_no.mean()
    
    # At different position sizes
    print(f"\n  Strategy: BUY_NO when YES >= 95¢")
    print(f"  Positions: {len(opt):,}")
    print(f"  Win rate: {wr:.1%}")
    print(f"  Avg fill: {opt['actual_no_cost'].mean()*100:.2f}c (theoretical: {opt['theoretical_no_cost'].mean()*100:.2f}c)")
    print(f"  Negative slippage advantage: {(opt['theoretical_no_cost'] - opt['actual_no_cost']).mean()*100:.2f}c per position")
    
    print(f"\n  Position size comparison:")
    for pos_size in [1, 5, 10, 25, 50, 100]:
        cost = opt["actual_no_cost"] * pos_size
        win = pos_size - pos_size * FEE_RATE - cost
        lose = -cost
        pnl = np.where(resolved_no, win, lose)
        sorted_pnl = pd.Series(pnl).sort_values()
        cum = sorted_pnl.cumsum()
        max_dd = (cum.cummax() - cum).max()
        sharpe_like = pnl.mean() / pnl.std() if pnl.std() > 0 else 0
        print(f"    \${pos_size:>3}/pos: P&L=\${pnl.sum():>10,.2f}  MaxDD=\${max_dd:>10,.2f}  Sharpe≈{sharpe_like:.3f}  avg=\${pnl.mean():.4f}")

if __name__ == "__main__":
    main()
