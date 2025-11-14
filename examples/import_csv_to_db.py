"""
CSV data import tool
Import CSV format K-line data into DuckDB database
"""

from __future__ import annotations
import os
import sys
import argparse
from pathlib import Path

# Allow running from repo root
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "python"))

from rustbroker.data import load_csv_to_bars

from engine_rust import save_klines as save_klines_rust
from engine_rust import save_klines_from_csv as save_klines_from_csv_rust



def import_csv_to_db(
    csv_path: str,
    db_path: str,
    symbol: str,
    period: str = "1d",
    replace: bool = False,
    direct_csv: bool = True
) -> None:
    """
    Import CSV file to database
    
    Args:
        csv_path: CSV file path
        db_path: Database file path
        symbol: Symbol code
        period: Period ('1m', '5m', '1d') - default is '1d'
        replace: Whether to replace existing data
    """
    print(f"Importing {csv_path} to database...")
    print(f"  Symbol: {symbol}")
    print(f"  Period: {period}")
    print(f"  Database path: {db_path}")
    
    # Validate period
    valid_periods = ["1m", "5m", "1d"]
    if period not in valid_periods:
        print(f"  Error: Invalid period '{period}'. Must be one of {valid_periods}")
        return
    
    # Check if CSV file exists
    if not os.path.exists(csv_path):
        print(f"  Error: CSV file not found: {csv_path}")
        return
    
    if save_klines_from_csv_rust is None and save_klines_rust is None:
        raise ImportError(
            "Rust module 'engine_rust' not available. Please build the Rust extension before running this script."
        )

    try:
        # Use direct CSV import if available and requested (much faster - bypasses Python parsing)
        if direct_csv and save_klines_from_csv_rust is not None:
            print("  Using direct CSV import (fastest method - bypasses Python parsing)...")
            save_klines_from_csv_rust(db_path, csv_path, symbol, period, replace)
        else:
            # Load CSV data into Python (for Python-parsed import)
            print("  Reading CSV file...")
            bars = load_csv_to_bars(csv_path, symbol=symbol)
            print(f"  Read {len(bars)} K-line records")
            
            if not bars:
                print("  Warning: No data read")
                return
            
            # Display data range
            datetimes = [bar["datetime"] for bar in bars if bar.get("datetime")]
            if datetimes:
                print(f"  Date range: {min(datetimes)} to {max(datetimes)}")
            
            # Save to database
            print(f"  Saving to database ({period} table)...")
            
            if save_klines_rust is not None:
                print("  Using Python-parsed data import...")
                save_klines_rust(db_path, symbol, period, bars, replace)
            else:
                raise ImportError("No Rust import function available")
    except Exception as exc:  # pragma: no cover - CLI error reporting
        print(f"  Error while saving data via Rust module: {exc}")
        raise

    print("  Data saved via Rust extension.")
    
    print(f"  Import completed!")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import CSV format K-line data into DuckDB database")
    parser.add_argument(
        "csv_path",
        nargs="?",
        default="data/sh600000_min.csv",
        help="CSV file path (default: data/sh600000_min.csv)"
    )
    parser.add_argument("--db", default="data/backtest.db", help="Database file path (default: data/backtest.db)")
    parser.add_argument(
        "--symbol",
        default="600000.sh",
        help="Symbol code (default: 600000.sh)"
    )
    parser.add_argument("--period", default="1m", choices=["1m", "5m", "1d"], help="Period (default: 1d)")
    parser.add_argument("--replace", action="store_true", help="Replace existing data")
    parser.add_argument("--no-direct-csv", action="store_true", help="Disable direct CSV import (use Python parsing instead)")
    
    args = parser.parse_args()
    
    # Resolve relative paths
    csv_path = args.csv_path
    db_path = args.db
    
    # If relative path, relative to script directory
    if not os.path.isabs(csv_path):
        csv_path = os.path.join(os.path.dirname(__file__), csv_path)
    
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(__file__), "..", db_path)
    
    import_csv_to_db(csv_path, db_path, args.symbol, args.period, args.replace, direct_csv=not args.no_direct_csv)


if __name__ == "__main__":
    main()
