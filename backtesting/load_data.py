"""
Load and explore the Polymarket quant.parquet dataset.

Usage:
    source venv/bin/activate
    python backtesting/load_data.py [--path data/quant.parquet] [--sample 100000]
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import numpy as np


def load_data(path: str, sample: int | None = None) -> pd.DataFrame:
    """Load quant.parquet with optional sampling for quick exploration."""
    p = Path(path)
    if not p.exists():
        print(f"ERROR: {path} not found. Download with:")
        print(f"  huggingface-cli download SII-WANGZJ/Polymarket_data quant.parquet --repo-type dataset --local-dir data/")
        sys.exit(1)

    print(f"Loading {path}...")
    if sample:
        # Read only first N rows for quick exploration
        df = pd.read_parquet(path).head(sample)
    else:
        df = pd.read_parquet(path)
    
    return df


def compute_stats(df: pd.DataFrame) -> dict:
    """Compute basic dataset statistics."""
    stats = {}
    stats["total_records"] = len(df)
    stats["columns"] = list(df.columns)
    stats["dtypes"] = {col: str(dt) for col, dt in df.dtypes.items()}
    stats["date_range"] = {}
    
    # Try to find date columns
    for col in df.columns:
        if "date" in col.lower() or "time" in col.lower() or "timestamp" in col.lower():
            try:
                dates = pd.to_datetime(df[col])
                stats["date_range"][col] = {
                    "min": str(dates.min()),
                    "max": str(dates.max()),
                }
            except Exception:
                pass

    # Numeric column stats
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if numeric_cols:
        desc = df[numeric_cols].describe().to_dict()
        stats["numeric_summary"] = desc

    # Unique markets if column exists
    for col in ["market", "market_id", "condition_id", "question", "title"]:
        if col in df.columns:
            stats[f"unique_{col}"] = df[col].nunique()

    return stats


def print_summary(df: pd.DataFrame, stats: dict) -> None:
    """Print a readable summary of the dataset."""
    print("\n" + "=" * 70)
    print("POLYMARKET DATASET SUMMARY")
    print("=" * 70)
    print(f"\nTotal records: {stats['total_records']:,}")
    print(f"Columns ({len(stats['columns'])}): {', '.join(stats['columns'])}")
    
    if stats["date_range"]:
        print("\nDate ranges:")
        for col, rng in stats["date_range"].items():
            print(f"  {col}: {rng['min']} → {rng['max']}")
    
    # Unique markets
    for key, val in stats.items():
        if key.startswith("unique_"):
            print(f"\nUnique {key.replace('unique_', '')}: {val:,}")

    # Numeric summaries
    if "numeric_summary" in stats:
        print("\nNumeric column summaries:")
        for col, vals in stats["numeric_summary"].items():
            print(f"\n  {col}:")
            for stat_name, val in vals.items():
                if isinstance(val, float):
                    print(f"    {stat_name}: {val:,.4f}")
                else:
                    print(f"    {stat_name}: {val:,}")

    # Sample rows
    print("\n" + "-" * 70)
    print("FIRST 5 ROWS:")
    print("-" * 70)
    print(df.head().to_string())
    
    print("\n" + "-" * 70)
    print("NULL COUNTS:")
    print("-" * 70)
    nulls = df.isnull().sum()
    if nulls.any():
        print(nulls[nulls > 0].to_string())
    else:
        print("No nulls found")


def main():
    parser = argparse.ArgumentParser(description="Load and explore Polymarket data")
    parser.add_argument("--path", default="data/quant.parquet", help="Path to parquet file")
    parser.add_argument("--sample", type=int, default=None, help="Load only first N rows")
    parser.add_argument("--save-stats", default=None, help="Save stats JSON to this path")
    args = parser.parse_args()

    df = load_data(args.path, args.sample)
    stats = compute_stats(df)
    print_summary(df, stats)

    if args.save_stats:
        import json
        with open(args.save_stats, "w") as f:
            json.dump(stats, f, indent=2, default=str)
        print(f"\nStats saved to {args.save_stats}")


if __name__ == "__main__":
    main()
