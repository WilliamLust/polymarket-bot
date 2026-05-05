"""
Extract smart wallet list from quant.parquet for live whale monitoring.

Outputs: backtesting/smart_wallets.json — a compact JSON file with wallet addresses
that the Node.js live trader loads on startup.
"""

import sys
import json
import time
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from collections import defaultdict
from scipy import stats as scipy_stats

MARKETS_PATH = "data/markets.parquet"
QUANT_PATH = "data/quant.parquet"
OUTPUT_PATH = "backtesting/smart_wallets.json"
YES_MIN = 0.95
BASELINE_WR = 0.146

# ── Load resolved markets ────────────────────────────────────
print("Loading markets.parquet...")
markets_df = pd.read_parquet(MARKETS_PATH, columns=["id", "outcome_prices"])
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

# ── Scan quant.parquet for wallet stats ──────────────────────
print("\nScanning quant.parquet for NO-buyer wallet stats...")
t0 = time.time()
pf = pq.ParquetFile(QUANT_PATH)
n_rg = pf.metadata.num_row_groups
print(f"Row groups: {n_rg}")

wallet_stats = defaultdict(lambda: [0, 0])  # [count, wins]

total_no_buys = 0
for rg_idx in range(n_rg):
    if rg_idx % 100 == 0:
        elapsed = time.time() - t0
        print(f"  RG {rg_idx}/{n_rg} ({elapsed:.0f}s) — {total_no_buys:,} NO-buys")

    table = pf.read_row_group(rg_idx, columns=["market_id", "price", "side", "maker", "taker"])
    df = table.to_pandas()
    df["market_id"] = df["market_id"].astype(str)

    df = df[df["price"] >= YES_MIN].copy()
    if len(df) == 0:
        continue

    # Identify NO buyer
    df["no_buyer"] = np.where(df["side"] == "BUY", df["maker"],
                     np.where(df["side"] == "SELL", df["taker"], ""))
    df = df[df["no_buyer"] != ""].copy()
    if len(df) == 0:
        continue

    # Resolution
    df["resolution"] = df["market_id"].map(resolved_map)
    df = df[df["resolution"].isin(["YES", "NO"])].copy()
    if len(df) == 0:
        continue

    won_arr = (df["resolution"] == "NO").values.astype(int)
    buyers = df["no_buyer"].values
    total_no_buys += len(df)

    for i in range(len(buyers)):
        b = buyers[i]
        wallet_stats[b][0] += 1
        wallet_stats[b][1] += won_arr[i]

elapsed = time.time() - t0
print(f"\nScan complete: {total_no_buys:,} NO-buy trades in {elapsed:.0f}s")
print(f"Unique wallets: {len(wallet_stats):,}")

# ── Compute WR and select smart wallets ──────────────────────
print("\nSelecting smart wallets...")

# Binomial test for each wallet with 20+ trades
min_trades = 20
smart_wallets = {}

for wallet, (count, wins) in wallet_stats.items():
    if count < min_trades:
        continue
    wr = wins / count
    if wr <= BASELINE_WR:
        continue
    # Skip 100% WR wallets — they're arb bots trading post-resolution
    if wr >= 1.0:
        continue
    # One-tailed binomial test: P(WR > baseline)
    p_value = 1 - scipy_stats.binom.cdf(wins - 1, count, BASELINE_WR)
    if p_value < 0.01:
        smart_wallets[wallet] = {
            "trades": int(count),
            "wins": int(wins),
            "wr": round(wr, 4),
            "p_value": round(float(p_value), 6),
        }

print(f"Smart wallets (sig_001): {len(smart_wallets):,}")

# ── Save ─────────────────────────────────────────────────────
output = {
    "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "tier": "sig_001",
    "baseline_wr": BASELINE_WR,
    "min_trades": min_trades,
    "significance": 0.01,
    "wallet_count": len(smart_wallets),
    "wallets": smart_wallets,
}

with open(OUTPUT_PATH, "w") as f:
    json.dump(output, f)
print(f"Saved to {OUTPUT_PATH} ({len(json.dumps(output)) / 1e6:.1f} MB)")
