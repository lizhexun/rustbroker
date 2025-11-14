"""
Market data service package combining DuckDB storage with QMT xtdata fallback.
"""

from .service import (
    DataRequest,
    MarketDataService,
    MissingRange,
    MarketDataConfig,
)

__all__ = [
    "DataRequest",
    "MarketDataService",
    "MissingRange",
    "MarketDataConfig",
]


