"""
Whale Watching Backtest — Do smart-money NO buyers predict market flips?

Hypothesis: Wallets with historically high WR on NO buys at YES>=95¢ 
have genuine edge. Markets where these wallets are active NO buyers
should flip (resolve NO) at a higher rate than markets without them.

Approach:
1. Scan quant.parquet for YES>=95¢ trades
2. Identify NO buyer for each trade (maker if side=BUY, taker if side=SELL)
3. Compute per-wallet WR on NO buys at 95¢+
4. Define "smart wallets" = 50+ NO-buy trades, WR significantly above baseline
5. For each market, flag whether any smart wallet bought NO
6. Compare WR: smart-wallet-present vs absent
7. Compute conditional EV and Kelly for each group

Uses row-group scanning. No isin() on full market set.
"""

import sys
import json
import time
import re
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from scipy import stats as scipy_stats

MARKETS_PATH = "data/markets.parquet"
QUANT_PATH = "data/quant.parquet"
FEE_RATE = 0.02
YES_MIN = 0.95
OUTPUT_PATH = "backtesting/whale_backtest_results.json"

# ── Load resolved markets ────────────────────────────────────
print("Loading markets.parquet...")
markets_df = pd.read_parquet(MARKETS_PATH, columns=["id", "question", "outcome_prices"])
markets_df["id"] = markets_df["id"].astype(str)

def parse_resolution(prices_str):
    try:
        p = prices_str
        if isinstance(p, str):
            p = p.strip("[]").replace("'", "").split(", ")
            p = [float(x) for x in p]
        if isinstance(p, list) and len(p) >= 2:
            return "YES" if float(p[0]) > 0.5 else "NO"
    except:
        pass
    return "UNKNOWN"

markets_df["resolution"] = markets_df["outcome_prices"].apply(parse_resolution)
resolved = markets_df[markets_df["resolution"].isin(["YES", "NO"])].copy()
resolved_map = dict(zip(resolved["id"], resolved["resolution"]))
print(f"Resolved markets: {len(resolved):,}")

# ── Pass 1: Identify NO-buyer wallets and their stats ────────
print("\n── Pass 1: Building wallet profiles ────────────────────")
t0 = time.time()
pf = pq.ParquetFile(QUANT_PATH)
n_rg = pf.metadata.num_row_groups
print(f"Row groups: {n_rg}")

# Accumulate per-wallet: {wallet: [count, wins, volume]}
wallet_stats = defaultdict(lambda: [0, 0, 0.0])

# Also collect per-market: set of NO-buyer wallets
market_buyers = defaultdict(set)

# And per-market: first (deduped) YES price for EV computation
market_entry_price = {}

total_trades_scanned = 0
total_no_buys = 0

for rg_idx in range(n_rg):
    if rg_idx % 100 == 0:
        elapsed = time.time() - t0
        print(f"  RG {rg_idx}/{n_rg} ({elapsed:.0f}s) — {total_no_buys:,} NO-buys so far")
    
    table = pf.read_row_group(rg_idx, columns=["market_id", "price", "side", "maker", "taker", "usd_amount", "timestamp"])
    df = table.to_pandas()
    df["market_id"] = df["market_id"].astype(str)
    total_trades_scanned += len(df)
    
    # Filter to YES >= 0.95 only
    df = df[df["price"] >= YES_MIN].copy()
    if len(df) == 0:
        continue
    
    # Identify NO buyer (vectorized) — skip isin on resolved_index, 
    # let resolution filter handle it (much faster than 734K-set isin)
    # side="BUY" → maker sold YES = NO buyer
    # side="SELL" → taker sold YES = NO buyer
    df["no_buyer"] = np.where(df["side"] == "BUY", df["maker"], 
                     np.where(df["side"] == "SELL", df["taker"], ""))
    df = df[df["no_buyer"] != ""].copy()
    if len(df) == 0:
        continue
    
    # Resolution via map (fast dict lookup), then filter
    df["resolution"] = df["market_id"].map(resolved_map)
    df = df[df["resolution"].isin(["YES", "NO"])].copy()
    if len(df) == 0:
        continue
    
    df["won"] = df["resolution"] == "NO"
    df["usd_amount"] = df["usd_amount"].fillna(0).astype(float)
    
    total_no_buys += len(df)
    
    # Raw numpy arrays for fast iteration (avoid pandas groupby overhead)
    buyers = df["no_buyer"].values
    won_arr = df["won"].values.astype(int)
    vol_arr = df["usd_amount"].values
    mids = df["market_id"].values
    ts_arr = df["timestamp"].values
    price_arr = df["price"].values
    
    # Wallet stats: count, wins, volume
    for i in range(len(buyers)):
        b = buyers[i]
        wallet_stats[b][0] += 1      # count
        wallet_stats[b][1] += won_arr[i]  # wins
        wallet_stats[b][2] += vol_arr[i]  # volume
    
    # Market-level: buyers set + earliest entry price
    for i in range(len(mids)):
        mid = mids[i]
        market_buyers[mid].add(buyers[i])
        ts = ts_arr[i]
        if mid not in market_entry_price or ts < market_entry_price[mid][1]:
            market_entry_price[mid] = (float(price_arr[i]), ts)

elapsed = time.time() - t0
print(f"\nPass 1 complete: {total_no_buys:,} NO-buy trades in {elapsed:.0f}s")
print(f"Unique NO-buyer wallets: {len(wallet_stats):,}")
print(f"Markets with NO buyers: {len(market_buyers):,}")

# ── Analyze wallet distribution ──────────────────────────────
print("\n── Wallet distribution ─────────────────────────────────")

# Compute WR for wallets with sufficient trades
baseline_wr = 0.146  # From category flip rates (overall WR at YES>=95¢)
min_trades = 20  # Minimum trades to consider a wallet

wallet_list = []
for wallet, s in wallet_stats.items():
    count, wins, volume = s[0], s[1], s[2]
    if count >= min_trades:
        wr = wins / count
        wallet_list.append({
            "wallet": wallet,
            "no_buy_count": count,
            "wins": wins,
            "win_rate": wr,
            "volume": volume,
        })

wallet_df = pd.DataFrame(wallet_list)
print(f"Wallets with {min_trades}+ NO-buy trades: {len(wallet_df):,}")

if len(wallet_df) == 0:
    print("Not enough wallet data. Try lowering min_trades.")
    sys.exit(1)

# Statistical significance: binomial test against baseline
def binomial_pvalue(wins, n, p_null=baseline_wr):
    """P-value that WR > baseline (one-tailed)"""
    if n == 0:
        return 1.0
    return 1 - scipy_stats.binom.cdf(wins - 1, n, p_null)

wallet_df["p_value"] = wallet_df.apply(
    lambda r: binomial_pvalue(r["wins"], r["no_buy_count"]), axis=1
)
wallet_df["significant_05"] = wallet_df["p_value"] < 0.05
wallet_df["significant_01"] = wallet_df["p_value"] < 0.01

print(f"\nWin rate distribution (wallets with {min_trades}+ trades):")
print(f"  Mean WR: {wallet_df['win_rate'].mean():.1%}")
print(f"  Median WR: {wallet_df['win_rate'].median():.1%}")
print(f"  Top decile WR: {wallet_df['win_rate'].quantile(0.9):.1%}")
print(f"  Significantly above baseline (p<0.05): {wallet_df['significant_05'].sum()}")
print(f"  Significantly above baseline (p<0.01): {wallet_df['significant_01'].sum()}")

# ── Define "smart wallets" ───────────────────────────────────
# Multiple tiers to test
tiers = {
    "top_1pct_wr": wallet_df.nlargest(max(1, len(wallet_df) // 100), "win_rate"),
    "top_5pct_wr": wallet_df.nlargest(max(1, len(wallet_df) // 20), "win_rate"),
    "top_10pct_wr": wallet_df.nlargest(max(1, len(wallet_df) // 10), "win_rate"),
    "sig_005": wallet_df[wallet_df["significant_05"] & (wallet_df["win_rate"] > baseline_wr)],
    "sig_001": wallet_df[wallet_df["significant_01"] & (wallet_df["win_rate"] > baseline_wr)],
    "high_volume_50": wallet_df[wallet_df["no_buy_count"] >= 50].nlargest(50, "win_rate"),
}

print(f"\n── Smart wallet tiers ──────────────────────────────────")
for name, tier_df in tiers.items():
    if len(tier_df) == 0:
        print(f"  {name}: 0 wallets (empty tier)")
        continue
    print(f"  {name}: {len(tier_df)} wallets, avg WR={tier_df['win_rate'].mean():.1%}, "
          f"avg trades={tier_df['no_buy_count'].mean():.0f}")

# ── Pass 2: Compute conditional WR per tier ──────────────────
print(f"\n── Pass 2: Conditional WR analysis ─────────────────────")

results = {}

for tier_name, tier_df in tiers.items():
    if len(tier_df) == 0:
        continue
    
    smart_wallets = set(tier_df["wallet"].values)
    
    # For each market: was a smart wallet present as NO buyer?
    smart_markets = set()
    all_95_markets = set(market_entry_price.keys())
    
    for mid, buyers in market_buyers.items():
        if buyers & smart_wallets:  # Intersection
            smart_markets.add(mid)
    
    non_smart_markets = all_95_markets - smart_markets
    
    # Compute WR for each group
    def compute_group_stats(market_ids, label):
        if not market_ids:
            return None
        wins = 0
        total = 0
        no_costs = []
        for mid in market_ids:
            resolution = resolved_map.get(mid)
            if resolution not in ("YES", "NO"):
                continue
            total += 1
            if resolution == "NO":
                wins += 1
            if mid in market_entry_price:
                no_costs.append(1 - market_entry_price[mid][0])
        
        if total == 0:
            return None
        
        wr = wins / total
        avg_no_cost = np.mean(no_costs) if no_costs else 0.03
        win_payout = (1 - avg_no_cost) * (1 - FEE_RATE)
        ev = wr * win_payout - (1 - wr) * avg_no_cost
        
        # Kelly
        b = (1 - avg_no_cost) / avg_no_cost if avg_no_cost > 0 else 0
        full_kelly = (b * wr - (1 - wr)) / b if b > 0 else 0
        
        return {
            "label": label,
            "positions": total,
            "wins": wins,
            "win_rate": round(wr, 4),
            "avg_no_cost": round(avg_no_cost, 4),
            "ev_per_dollar": round(ev, 4),
            "full_kelly": round(full_kelly, 4),
            "quarter_kelly": round(full_kelly * 0.25, 4),
        }
    
    smart_stats = compute_group_stats(smart_markets, f"{tier_name}_present")
    non_smart_stats = compute_group_stats(non_smart_markets, f"{tier_name}_absent")
    all_stats = compute_group_stats(all_95_markets, f"{tier_name}_all")
    
    if smart_stats and non_smart_stats:
        wr_delta = smart_stats["win_rate"] - non_smart_stats["win_rate"]
        
        # Chi-squared test for WR difference
        contingency = np.array([
            [smart_stats["wins"], smart_stats["positions"] - smart_stats["wins"]],
            [non_smart_stats["wins"], non_smart_stats["positions"] - non_smart_stats["wins"]],
        ])
        chi2, p_val, _, _ = scipy_stats.chi2_contingency(contingency, correction=False)
        
        print(f"\n  {tier_name}:")
        print(f"    Smart present: {smart_stats['positions']:,} pos, WR={smart_stats['win_rate']:.1%}, EV=${smart_stats['ev_per_dollar']:+.3f}")
        print(f"    Smart absent:  {non_smart_stats['positions']:,} pos, WR={non_smart_stats['win_rate']:.1%}, EV=${non_smart_stats['ev_per_dollar']:+.3f}")
        print(f"    WR delta: {wr_delta:+.1%} (chi2={chi2:.1f}, p={p_val:.4f})")
        print(f"    Kelly: present={smart_stats['quarter_kelly']:.4f} vs absent={non_smart_stats['quarter_kelly']:.4f}")
        
        results[tier_name] = {
            "smart_wallets_count": len(smart_wallets),
            "smart_present": smart_stats,
            "smart_absent": non_smart_stats,
            "all": all_stats,
            "wr_delta": round(wr_delta, 4),
            "chi2": round(chi2, 2),
            "p_value": round(p_val, 4),
            "significant": bool(p_val < 0.05),
        }

# ── Top smart wallets detail ─────────────────────────────────
print(f"\n── Top 20 smart wallets (by WR, 50+ trades) ──────────")
top_wallets = wallet_df[wallet_df["no_buy_count"] >= 50].nlargest(20, "win_rate")
for _, w in top_wallets.iterrows():
    print(f"  {w['wallet'][:10]}... {w['no_buy_count']:>4} trades  WR={w['win_rate']:.1%}  vol=${w['volume']:,.0f}  p={w['p_value']:.4f}")

# ── Save results ─────────────────────────────────────────────
output = {
    "generated": datetime.utcnow().isoformat() + "Z",
    "hypothesis": "Markets with smart-money NO buyers flip at higher rate than markets without",
    "baseline_wr": baseline_wr,
    "min_wallet_trades": min_trades,
    "total_no_buy_trades": total_no_buys,
    "unique_no_buyer_wallets": len(wallet_stats),
    "wallets_with_min_trades": len(wallet_df),
    "tier_results": results,
}

with open(OUTPUT_PATH, "w") as f:
    json.dump(output, f, indent=2)
print(f"\nSaved to {OUTPUT_PATH}")

# ── Final verdict ────────────────────────────────────────────
print(f"\n{'='*60}")
print("VERDICT")
print(f"{'='*60}")
best_tier = None
best_delta = 0
for tier_name, r in results.items():
    if r["wr_delta"] > best_delta and r["significant"]:
        best_delta = r["wr_delta"]
        best_tier = tier_name

if best_tier:
    r = results[best_tier]
    print(f"★ WHALE SIGNAL IS REAL (best tier: {best_tier})")
    print(f"  WR delta: {r['wr_delta']:+.1%} (p={r['p_value']:.4f})")
    print(f"  Smart present WR: {r['smart_present']['win_rate']:.1%} vs absent: {r['smart_absent']['win_rate']:.1%}")
    print(f"  EV: ${r['smart_present']['ev_per_dollar']:+.3f} vs ${r['smart_absent']['ev_per_dollar']:+.3f}")
    print(f"  Recommend: build live whale monitoring pipeline")
else:
    # Check if any tier has positive (even if not significant) delta
    any_positive = any(r["wr_delta"] > 0 for r in results.values())
    if any_positive:
        print("◇ WHALE SIGNAL IS WEAK — positive direction but not statistically significant")
        for tier_name, r in results.items():
            if r["wr_delta"] > 0:
                print(f"  {tier_name}: +{r['wr_delta']:.1%} delta, p={r['p_value']:.4f}")
        print("  Recommend: shelve whale watching, build lightweight entry filters instead")
    else:
        print("✗ NO WHALE SIGNAL — smart wallets don't predict flips better than random")
        print("  Recommend: shelve whale watching, focus on entry signal features")
