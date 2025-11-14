from .api import BacktestEngine, BacktestConfig, BarContext, OrderHelper
from .strategy import Strategy
from .indicators import Indicator, IndicatorRegistry

# Import vectorized functions from Rust if available
try:
    from engine_rust import vectorized_sma, vectorized_rsi
except ImportError:
    vectorized_sma = None
    vectorized_rsi = None

__all__ = [
    "BacktestEngine",
    "BacktestConfig",
    "BarContext",
    "OrderHelper",
    "Strategy",
    "Indicator",
    "IndicatorRegistry",
    "vectorized_sma",
    "vectorized_rsi",
] 