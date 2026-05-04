"""
Load and explore the Polymarket quant.parquet dataset.

Uses PyArrow row-group reading to handle the 36GB file without OOM.
This machine has ~32GB RAM — can't load the full file at once.

Usage:
    source venv/bin/activate
    python backtesting/load_data.py [--rows 100000] [--stats] [--schema]
"""

import argparse
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pandas as pd
import numpy as np


def get_schema(path: str) -> None:
    """Print schema without loading data."""
    pf = pq.ParquetFile(path)
    print("\n" + "=" * 70)
    print("SCHEMA")
    print("=" * 70)
    for i, field in enumerate(pf.schema_arrow):
        print(f"  [{i:2d}] {field.name:30s} {field.type}")
    
    meta = pf.metadata
    print(f"\nRow groups: {meta.num_row_groups}")
    print(f"Total rows: {meta.num_rows:,}")
    print(f"Total columns: {meta.num_columns}")
    
    # Show per-row-group sizes
    print(f"\nRow group details (first 10):")
    for i in range(min(10, meta.num_row_groups)):
        rg = meta.row_group(i)
        print(f"  RG {i}: {rg.num_rows:,} rows, {rg.total_byte_size / 1e6:.1f} MB")


def load_sample(path: str, rows: int = 100000) -> pd.DataFrame:
    """
    Load a sample from the parquet file using row-group reading.
    Reads only the first row group(s) needed to get `rows` records.
    """
    pf = pq.ParquetFile(path)
    
    # Read only first row group — each group has millions of rows
    first_rg = pf.metadata.row_group(0)
    rg_rows = first_rg.num_rows
    
    print(f"File: {path}")
    print(f"Total rows: {pf.metadata.num_rows:,}")
    print(f"Row groups: {pf.metadata.num_row_groups}")
    print(f"First row group: {rg_rows:,} rows")
    print(f"Loading first {rows:,} rows...")
    
    if rows >= rg_rows:
        # Read entire first row group
        table = pf.read_row_group(0)
    else:
        # Read first row group, then trim
        table = pf.read_row_group(0)
        table = table.slice(0, rows)
    
    df = table.to_pandas()
    print(f"Loaded: {len(df):,} rows, {len(df.columns)} columns")
    return df


def compute_stats(df: pd.DataFrame) -> dict:
    """Compute basic dataset statistics."""
    stats = {}
    stats["total_records_loaded"] = len(df)
    stats["columns"] = list(df.columns)
    stats["dtypes"] = {col: str(dt) for col, dt in df.dtypes.items()}
    
    # Date ranges
    stats["date_range"] = {}
    for col in df.columns:
        if any(kw in col.lower() for kw in ["date", "time", "timestamp"]):
            try:
                dates = pd.to_datetime(df[col])
                stats["date_range"][col] = {
                    "min": str(dates.min()),
                    "max": str(dates.max()),
                }
            except Exception:
                pass

    # Unique counts
    for col in ["market", "market_id", "condition_id", "question", "title", "slug"]:
        if col in df.columns:
            stats[f"unique_{col}"] = int(df[col].nunique())

    # Numeric summaries
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if numeric_cols:
        desc = df[numeric_cols].describe().to_dict()
        stats["numeric_summary"] = desc

    return stats


def print_summary(df: pd.DataFrame, stats: dict) -> None:
    """Print a readable summary."""
    print("\n" + "=" * 70)
    print("POLYMARKET DATASET SUMMARY")
    print("=" * 70)
    print(f"\nRecords loaded: {stats['total_records_loaded']:,}")
    print(f"Columns ({len(stats['columns'])}): {', '.join(stats['columns'][:20])}")
    if len(stats['columns']) > 20:
        print(f"  ... and {len(stats['columns']) - 20} more")
    
    if stats["date_range"]:
        print("\nDate ranges:")
        for col, rng in stats["date_range"].items():
            print(f"  {col}: {rng['min']} → {rng['max']}")
    
    for key, val in stats.items():
        if key.startswith("unique_"):
            print(f"\nUnique {key.replace('unique_', '')}: {val:,}")

    if "numeric_summary" in stats:
        print("\nNumeric summaries:")
        for col, vals in list(stats["numeric_summary"].items())[:10]:
            print(f"\n  {col}:")
            for stat_name, val in vals.items():
                if isinstance(val, float):
                    print(f"    {stat_name}: {val:,.4f}")
                else:
                    print(f"    {stat_name}: {val:,}")

    print("\n" + "-" * 70)
    print("FIRST 3 ROWS:")
    print("-" * 70)
    # Print only first few columns to fit terminal
    sample_cols = stats['columns'][:8]
    print(df[sample_cols].head(3).to_string())
    
    print("\n" + "-" * 70)
    print("NULL COUNTS (non-zero):")
    print("-" * 70)
    nulls = df.isnull().sum()
    nulls = nulls[nulls > 0]
    if len(nulls) > 0:
        print(nulls.to_string())
    else:
        print("No nulls in this sample")


def main():
    parser = argparse.ArgumentParser(description="Load and explore Polymarket data")
    parser.add_argument("--path", default="data/quant.parquet")
    parser.add_argument("--rows", type=int, default=100000, help="Number of rows to sample")
    parser.add_argument("--schema", action="store_true", help="Print schema only (no data load)")
    parser.add_argument("--stats", action="store_true", help="Compute and print full stats")
    parser.add_argument("--save-stats", default=None, help="Save stats JSON to path")
    args = parser.parse_args()

    p = Path(args.path)
    if not p.exists():
        print(f"ERROR: {args.path} not found.")
        print(f"Download: hf download SII-WANGZJ/Polymarket_data quant.parquet --repo-type dataset --local-dir data/")
        sys.exit(1)

    if args.schema:
        get_schema(args.path)
        return

    df = load_sample(args.path, args.rows)
    
    if args.stats or args.save_stats:
        stats = compute_stats(df)
        print_summary(df, stats)
        
        if args.save_stats:
            import json
            with open(args.save_stats, "w") as f:
                json.dump(stats, f, indent=2, default=str)
            print(f"\nStats saved to {args.save_stats}")
    else:
        # Quick overview
        print(f"\nShape: {df.shape}")
        print(f"\nColumns: {list(df.columns)}")
        print(f"\nDtypes:\n{df.dtypes.to_string()}")
        print(f"\nFirst 3 rows:")
        print(df.head(3).to_string())


if __name__ == "__main__":
    main()
