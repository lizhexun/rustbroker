"""
Data loading utilities for backtesting
"""

import csv
from typing import List, Dict, Any, Optional


def load_csv_to_bars(csv_path: str, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Load K-line data from CSV file
    
    Args:
        csv_path: Path to CSV file
        symbol: Optional symbol code (if not provided, will try to infer from filename)
    
    Returns:
        List of bar dictionaries with keys: datetime, open, high, low, close, volume
    """
    bars = []
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            bar = {
                "datetime": row["datetime"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            bars.append(bar)
    
    return bars

