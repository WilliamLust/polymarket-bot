"""
Quick exploration of the quant.parquet dataset.
Reads from multiple row groups to get a representative sample.

Usage:
    source venv/bin/activate
    python backtesting/explore_data.py
"""

import pyarrow.parquet as pq
import pandas as pd
import numpy as np
from pathlib import Path

DATA_PATH = Path("data/quant.parquet")

def main():
    if not DATA_PATH.exists():
        print(f"ERROR: {DATA_PATH} not found. Download first.")
        return

    pf = pq.ParquetFile(str(DATA_PATH))
    meta = pf.metadata
    
    print(f"Dataset: {DATA_PATH}")
    print(f"Total rows: {meta.num_rows:,}")
    print(f"Row groups: {meta.num_row_groups}")
    print(f"Size on disk: {DATA_PATH.stat().st_size / 1e9:.1f} GB")
    
    # Sample strategy: read first RG, middle RG, and last RG
    rg_indices = [0, meta.num_row_groups // 4, meta.num_row_groups // 2, 
                  3 * meta.num_row_groups // 4, meta.num_row_groups - 1]
    
    print(f"\nSampling row groups: {rg_indices}")
    
    frames = []
    for i in rg_indices:
        rg = meta.row_group(i)
        print(f"  RG {i}: {rg.num_rows:,} rows ({rg.total_byte_size / 1e6:.1f} MB)")
        # Read only first 50K rows from each group to keep memory manageable
        table = pf.read_row_group(i)
        table = table.slice(0, min(50000, rg.num_rows))
        frames.append(table.to_pandas())
    
    df = pd.concat(frames, ignore_index=True)
    print(f"\nCombined sample: {len(df):,} rows")
    
    # Convert timestamp to datetime
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
    
    # Basic stats
    print(f"\n{'=' * 60}")
    print("COLUMN OVERVIEW")
    print(f"{'=' * 60}")
    for col in df.columns:
        if col == "datetime":
            continue
        nunique = df[col].nunique()
        nulls = df[col].isnull().sum()
        print(f"  {col:20s} dtype={str(df[col].dtype):10s} unique={nunique:>10,}  nulls={nulls:,}")
    
    # Date range
    print(f"\nDate range: {df['datetime'].min()} → {df['datetime'].max()}")
    
    # Market coverage
    print(f"\nUnique markets: {df['market_id'].nunique():,}")
    print(f"Unique conditions: {df['condition_id'].nunique():,}")
    print(f"Unique events: {df['event_id'].nunique():,}")
    
    # Price distribution
    print(f"\n{'=' * 60}")
    print("PRICE DISTRIBUTION")
    print(f"{'=' * 60}")
    print(df["price"].describe().to_string())
    
    # Side breakdown
    print(f"\n{'=' * 60}")
    print("SIDE BREAKDOWN")
    print(f"{'=' * 60}")
    print(df["side"].value_counts().to_string())
    
    # USD amounts
    print(f"\n{'=' * 60}")
    print("TRADE SIZE (USD)")
    print(f"{'=' * 60}")
    print(df["usd_amount"].describe().to_string())
    
    # Top markets by trade count
    print(f"\n{'=' * 60}")
    print("TOP 10 MARKETS BY TRADE COUNT")
    print(f"{'=' * 60}")
    top_markets = df.groupby("market_id").agg(
        trades=("price", "count"),
        avg_price=("price", "mean"),
        total_usd=("usd_amount", "sum"),
    ).sort_values("trades", ascending=False).head(10)
    print(top_markets.to_string())
    
    # Trading activity over time (by sample group)
    print(f"\n{'=' * 60}")
    print("TEMPORAL DISTRIBUTION")
    print(f"{'=' * 60}")
    df["date"] = df["datetime"].dt.date
    daily = df.groupby("date").size()
    for date, count in daily.items():
        print(f"  {date}: {count:>6,} trades")
    
    print(f"\n✓ Exploration complete")


if __name__ == "__main__":
    main()
