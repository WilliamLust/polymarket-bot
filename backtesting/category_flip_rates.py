"""
Per-category flip rates for the 95-100¢ YES bucket (BUY_NO strategy).
Outputs category_flip_rates.json for the live trader's Kelly sizing.

Uses drop_duplicates per row group (NO isin — too slow for all-market scans).
Then filters to resolved markets after dedup.
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

MARKETS_PATH = "data/markets.parquet"
QUANT_PATH = "data/quant.parquet"
FEE_RATE = 0.02
YES_MIN = 0.95
OUTPUT_PATH = "backtesting/category_flip_rates.json"

# ── Category patterns ────────────────────────────────────────
CATEGORY_PATTERNS = {
    "weather": r"temperature|°F|°C|celsius|fahrenheit|weather|rain|snow|hurricane|tornado",
    "crypto": r"bitcoin|btc|eth|ethereum|crypto|solana|sol|dogecoin|doge|token|defi|nft|blockchain",
    "politics": r"trump|biden|harris|republican|democrat|congress|senate|president|governor|election|supreme court|putin|russia|ukraine|china|xi|europe|macron|zelensky|nato|iran|israel",
    "sports": r"nfl|nba|mlb|nhl|soccer|football|basketball|baseball|hockey|super bowl|world cup|championship|playoff|game|match|score|win the|ufc|fight|fc vs",
    "entertainment": r"oscar|grammy|emmy|box office|movie|film|album|song|billboard|spotify|netflix|disney|marvel|tv show|series finale|season|episode",
    "tech": r"apple|google|microsoft|tesla|nvidia|openai|chatgpt|ai |launch|release|ipo|spacex|starlink|github",
    "finance": r"fed |interest rate|inflation|cpi|gdp|recession|s&p |dow |nasdaq|treasury|bond|etf|stock price|share price|hit.*\$|above \$|below \$|between \$",
}

def categorize_question(question):
    q = (question or "").lower()
    for cat, pattern in CATEGORY_PATTERNS.items():
        if re.search(pattern, q):
            return cat
    return "other"

# ── Load markets ─────────────────────────────────────────────
print("Loading markets.parquet...")
markets_df = pd.read_parquet(MARKETS_PATH, columns=["id", "question", "outcome_prices"])
markets_df["id"] = markets_df["id"].astype(str)

# Parse resolution
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
resolved["category"] = resolved["question"].apply(categorize_question)
resolved_index = set(resolved["id"].values)
print(f"Resolved markets: {len(resolved):,}")

# ── Scan quant.parquet — NO isin, use drop_duplicates ────────
print("Scanning quant.parquet for 95-100¢ YES positions (dedup=first, no isin)...")
t0 = time.time()
pf = pq.ParquetFile(QUANT_PATH)
n_rg = pf.metadata.num_row_groups
print(f"Row groups: {n_rg}")

chunks = []

for rg_idx in range(n_rg):
    if rg_idx % 100 == 0:
        elapsed = time.time() - t0
        print(f"  RG {rg_idx}/{n_rg} ({elapsed:.0f}s) — {len(chunks)} chunks")
    
    table = pf.read_row_group(rg_idx, columns=["market_id", "price", "timestamp"])
    df = table.to_pandas()
    df["market_id"] = df["market_id"].astype(str)
    
    # Filter to YES >= 0.95 first (eliminates ~90% of rows cheaply)
    df = df[df["price"] >= YES_MIN].copy()
    if len(df) == 0:
        continue
    
    # Dedup per row group: keep first trade per market
    df = df.sort_values("timestamp").drop_duplicates(subset="market_id", keep="first")
    chunks.append(df[["market_id", "price"]])
    
    # Periodic concat+dedup to bound memory
    if len(chunks) >= 100:
        merged = pd.concat(chunks).sort_values("market_id")
        merged = merged.drop_duplicates(subset="market_id", keep="first")
        chunks = [merged]

# Final merge
if chunks:
    signals = pd.concat(chunks).sort_values("market_id")
    signals = signals.drop_duplicates(subset="market_id", keep="first")
else:
    signals = pd.DataFrame(columns=["market_id", "price"])

elapsed = time.time() - t0
print(f"\nTotal signals at YES>={YES_MIN} (before resolve filter): {len(signals):,} ({elapsed:.0f}s)")

# ── Filter to resolved markets only ─────────────────────────
signals = signals[signals["market_id"].isin(resolved_index)]
print(f"After resolved filter: {len(signals):,}")

# ── Join with resolution + category ─────────────────────────
markets_slim = resolved[["id", "resolution", "category"]].rename(columns={"id": "market_id"})
merged = signals.merge(markets_slim, on="market_id", how="inner")
merged["no_cost"] = 1 - merged["price"]
merged["won"] = merged["resolution"] == "NO"

print(f"Merged positions: {len(merged):,}")

# ── Compute per-category stats ──────────────────────────────
categories = {}
for cat in sorted(merged["category"].unique()):
    cat_df = merged[merged["category"] == cat]
    n = len(cat_df)
    wins = cat_df["won"].sum()
    wr = wins / n if n > 0 else 0
    avg_no_cost = cat_df["no_cost"].mean()
    
    win_payout = (1 - avg_no_cost) * (1 - FEE_RATE)
    ev = wr * win_payout - (1 - wr) * avg_no_cost
    breakeven_wr = avg_no_cost / (win_payout + avg_no_cost) if (win_payout + avg_no_cost) > 0 else 1
    
    b = (1 - avg_no_cost) / avg_no_cost if avg_no_cost > 0 else 0
    full_kelly = (b * wr - (1 - wr)) / b if b > 0 else 0
    
    categories[cat] = {
        "positions": n,
        "wins": int(wins),
        "win_rate": round(wr, 4),
        "avg_no_cost": round(avg_no_cost, 4),
        "ev_per_dollar": round(ev, 4),
        "breakeven_wr": round(breakeven_wr, 4),
        "full_kelly": round(full_kelly, 4),
        "quarter_kelly": round(full_kelly * 0.25, 4),
    }

# All combined
n_total = len(merged)
wins_total = merged["won"].sum()
wr_total = wins_total / n_total if n_total > 0 else 0
avg_no_total = merged["no_cost"].mean()
win_payout_total = (1 - avg_no_total) * (1 - FEE_RATE)
ev_total = wr_total * win_payout_total - (1 - wr_total) * avg_no_total
be_total = avg_no_total / (win_payout_total + avg_no_total) if (win_payout_total + avg_no_total) > 0 else 1
b_total = (1 - avg_no_total) / avg_no_total if avg_no_total > 0 else 0
fk_total = (b_total * wr_total - (1 - wr_total)) / b_total if b_total > 0 else 0

result = {
    "generated": datetime.utcnow().isoformat() + "Z",
    "position_size": 1,
    "min_yes": YES_MIN,
    "total_positions": n_total,
    "total_win_rate": round(wr_total, 4),
    "categories": categories,
    "all": {
        "positions": n_total,
        "wins": int(wins_total),
        "win_rate": round(wr_total, 4),
        "avg_no_cost": round(avg_no_total, 4),
        "ev_per_dollar": round(ev_total, 4),
        "breakeven_wr": round(be_total, 4),
        "full_kelly": round(fk_total, 4),
        "quarter_kelly": round(fk_total * 0.25, 4),
    },
}

with open(OUTPUT_PATH, "w") as f:
    json.dump(result, f, indent=2)
print(f"\nSaved to {OUTPUT_PATH}")

print(f"\n{'Category':<15} {'Pos':>7} {'WR':>6} {'Avg NO':>7} {'EV/$1':>7} {'BE WR':>6} {'FK':>7} {'QK':>7}")
print("─" * 70)
for cat, s in sorted(categories.items(), key=lambda x: -x[1]["positions"]):
    print(f"{cat:<15} {s['positions']:>7,} {s['win_rate']:>6.1%} {s['avg_no_cost']:>7.3f} {s['ev_per_dollar']:>+7.3f} {s['breakeven_wr']:>6.1%} {s['full_kelly']:>7.4f} {s['quarter_kelly']:>7.4f}")
print("─" * 70)
a = result["all"]
print(f"{'ALL':<15} {a['positions']:>7,} {a['win_rate']:>6.1%} {a['avg_no_cost']:>7.3f} {a['ev_per_dollar']:>+7.3f} {a['breakeven_wr']:>6.1%} {a['full_kelly']:>7.4f} {a['quarter_kelly']:>7.4f}")
