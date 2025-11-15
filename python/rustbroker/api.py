"""
Main API for backtest engine
"""

import math
from typing import Dict, List, Optional, Any
from datetime import datetime
from engine_rust import PyBacktestConfig, PyBacktestEngine, PyBar

class BacktestConfig:
    """Backtest configuration"""
    
    def __init__(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        cash: float = 100000.0,
        commission_rate: float = 0.0005,
        min_commission: float = 5.0,
        slippage_bps: float = 1.0,
        stamp_tax_rate: float = 0.001,
        t0_symbols: Optional[List[str]] = None,
        period: Optional[str] = None,
    ):
        self.start = start
        self.end = end
        self.cash = cash
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.slippage_bps = slippage_bps
        self.stamp_tax_rate = stamp_tax_rate
        self.t0_symbols = t0_symbols or []
        self.period = period


class BacktestEngine:
    """Main backtest engine"""
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        self._rust_config = PyBacktestConfig(
            start=config.start,
            end=config.end,
            cash=config.cash,
            commission_rate=config.commission_rate,
            min_commission=config.min_commission,
            slippage_bps=config.slippage_bps,
            stamp_tax_rate=config.stamp_tax_rate,
            t0_symbols=config.t0_symbols,
            period=config.period,
        )
        self._rust_engine = PyBacktestEngine(self._rust_config)
        self._strategy = None
        self._indicator_registry = {}
        self._benchmark_name = None
    
    def add_market_data(self, symbol: str, bars: List[Dict[str, Any]]):
        """Add market data for a symbol"""
        filtered_bars = self._filter_bars_by_date(bars)
        py_bars = [self._dict_to_bar(bar) for bar in filtered_bars]
        self._rust_engine.add_market_data(symbol, py_bars)
    
    def set_benchmark(self, benchmark_name: str, bars: List[Dict[str, Any]]):
        """
        Set benchmark timeline
        
        Args:
            benchmark_name: Name of the benchmark (for identification)
            bars: List of bar dictionaries
        """
        filtered_bars = self._filter_bars_by_date(bars)
        py_bars = [self._dict_to_bar(bar) for bar in filtered_bars]
        self._rust_engine.set_benchmark(py_bars)
        # Note: benchmark_name is stored for reference but not used in Rust layer
        self._benchmark_name = benchmark_name
    
    def run(self, strategy, data: Dict[str, List[Dict[str, Any]]], benchmark: Dict[str, List[Dict[str, Any]]]):
        """
        Run backtest
        
        Args:
            strategy: Strategy instance
            data: Market data dict {symbol: [bars]}
            benchmark: Benchmark data dict {name: [bars]}
        """
        self._strategy = strategy
        
        # Add market data
        for symbol, bars in data.items():
            self.add_market_data(symbol, bars)
        
        # Set benchmark (use first benchmark)
        if benchmark:
            benchmark_name = list(benchmark.keys())[0]
            self.set_benchmark(benchmark_name, benchmark[benchmark_name])
        
        # Create reusable context object (optimization: reuse instead of creating new each time)
        _reusable_context = BarContext(self._rust_engine, self._indicator_registry)
        
        # Create context factory function that returns the reusable context
        # and invalidates cache to ensure fresh data for each bar
        def create_context():
            _reusable_context._invalidate_cache()  # Invalidate cache for new bar
            return _reusable_context
        
        # Run backtest in Rust (main loop is executed in Rust for better performance)
        result = self._rust_engine.run_backtest(
            strategy,
            create_context,
            bool(self._indicator_registry)
        )
        
        # Convert result dict to Python dict
        # result is already a dict from Rust, but convert to ensure compatibility
        if isinstance(result, dict):
            return result
        return dict(result)
    
    def _create_context(self):
        """Create BarContext for strategy"""
        return BarContext(self._rust_engine, self._indicator_registry)
    
    def _dict_to_bar(self, bar_dict: Dict[str, Any]) -> PyBar:
        """Convert dict to PyBar"""
        return PyBar(
            datetime=bar_dict["datetime"],
            open=bar_dict["open"],
            high=bar_dict["high"],
            low=bar_dict["low"],
            close=bar_dict["close"],
            volume=bar_dict["volume"],
        )

    def _parse_datetime(self, dt_str: str) -> datetime:
        """Parse datetime string supporting multiple formats"""
        if dt_str.endswith("Z"):
            try:
                return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(dt_str)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(dt_str, fmt)
                except ValueError:
                    continue
        # Fallback: treat as string without conversion
        return datetime.min

    def _filter_bars_by_date(self, bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter bars by config start/end if provided"""
        start_dt = self._parse_datetime(self.config.start) if self.config.start else None
        end_dt = self._parse_datetime(self.config.end) if self.config.end else None

        if start_dt is None and end_dt is None:
            return bars

        filtered = []
        for bar in bars:
            dt = self._parse_datetime(bar["datetime"])
            if start_dt and dt < start_dt:
                continue
            if end_dt and dt > end_dt:
                continue
            filtered.append(bar)
        return filtered


class BarContext:
    """Context object passed to strategy - simplified, logic in Rust
    
    Performance optimization: Cache frequently accessed properties to reduce
    Python-Rust boundary calls. Cache is invalidated when moving to next bar.
    """
    
    def __init__(self, rust_engine, indicator_registry: Dict):
        self._rust_engine = rust_engine
        self._indicator_registry = indicator_registry
        # Cache for frequently accessed properties
        self._cached_cash = None
        self._cached_equity = None
        self._cached_positions = None
        self._cached_datetime = None
        self._cached_symbols = None
        self._cache_valid = False
    
    def _ensure_cache(self):
        """Ensure cache is up to date - called before accessing cached properties"""
        if not self._cache_valid:
            # Batch fetch all properties in one go (reduces Python-Rust calls)
            self._cached_cash = self._rust_engine.get_cash()
            self._cached_equity = self._rust_engine.get_equity()
            self._cached_positions = self._rust_engine.get_positions()
            self._cached_datetime = self._rust_engine.get_current_datetime() or ""
            self._cached_symbols = self._rust_engine.get_symbols()
            self._cache_valid = True
    
    def _invalidate_cache(self):
        """Invalidate cache - called when moving to next bar"""
        self._cache_valid = False
    
    @property
    def datetime(self) -> str:
        self._ensure_cache()
        return self._cached_datetime
    
    @property
    def symbols(self) -> List[str]:
        self._ensure_cache()
        return self._cached_symbols
    
    @property
    def cash(self) -> float:
        self._ensure_cache()
        return self._cached_cash
    
    @property
    def equity(self) -> float:
        self._ensure_cache()
        return self._cached_equity
    
    @property
    def positions(self) -> Dict[str, Dict[str, float]]:
        self._ensure_cache()
        return self._cached_positions
    
    def get_bars(self, symbol: str, count: int = 1) -> List[Dict[str, Any]]:
        """Get historical bars for a symbol"""
        py_bars = self._rust_engine.get_bars(symbol, count)
        return [{
            "datetime": bar.datetime,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        } for bar in py_bars]
    
    def get_indicator_value(self, name: str, symbol: str, count: Optional[int] = None) -> Optional[Any]:
        """Get indicator value"""
        values = self._rust_engine.get_indicator_value(name, symbol, count)
        if values is None:
            return None
        if count is None or count == 1:
            if not values:
                return None
            val = values[0]
            # Return None if value is NaN, otherwise round to 4 decimal places
            if math.isnan(val):
                return None
            return round(val, 4)
        # For multiple values, filter out NaN and round
        result = [round(v, 4) for v in values if not math.isnan(v)]
        return result if result else None
    
    def get_indicator_values(self, symbol: str, names: List[str]) -> Dict[str, Optional[float]]:
        """Get multiple indicator values in a single call (performance optimization)
        
        Args:
            symbol: Symbol to get indicators for
            names: List of indicator names
            
        Returns:
            Dictionary mapping indicator names to their values (or None if not available)
        """
        result = self._rust_engine.get_indicator_values(symbol, names)
        # Process and round values
        processed = {}
        for name, val_opt in result.items():
            if val_opt is None:
                processed[name] = None
            else:
                val = val_opt
                if math.isnan(val):
                    processed[name] = None
                else:
                    processed[name] = round(val, 4)
        return processed
    
    def get_symbol_data(self, symbol: str, indicators: Optional[List[str]] = None, bars: int = 0) -> Dict[str, Any]:
        """Get all data for a symbol in a single call (performance optimization)
        
        Args:
            symbol: Symbol to get data for
            indicators: List of indicator names to get (optional)
            bars: Number of bars to get (0 = don't get bars)
            
        Returns:
            Dictionary containing:
            - indicators: Dict of indicator values
            - bars: List of bar dictionaries (if bars > 0)
            - position: Position info dict (if symbol has position)
        """
        result = {}
        
        # Batch get indicators
        if indicators:
            result["indicators"] = self.get_indicator_values(symbol, indicators)
        
        # Get bars if requested
        if bars > 0:
            result["bars"] = self.get_bars(symbol, bars)
        
        # Get position info (from cached positions)
        self._ensure_cache()
        if symbol in self._cached_positions:
            result["position"] = self._cached_positions[symbol]
        else:
            result["position"] = {}
        
        return result
    
    def register_indicator(self, name: str, indicator_def, count: Optional[int] = None):
        """Register an indicator (called in on_start)"""
        self._indicator_registry[name] = {"def": indicator_def, "lookback": count or 1}
        
        if isinstance(indicator_def, dict):
            indicator_type = indicator_def.get("type", "rust_builtin")
            params = indicator_def.get("params", {})
            lookback_period = count or indicator_def.get("lookback_period", 1)
            params_str = {k: str(v) for k, v in params.items()}
            if "name" in indicator_def:
                params_str["name"] = indicator_def["name"]
            # Debug: uncomment to see indicator registration
            # print(f"Registering indicator: {name}, type: {indicator_type}, params: {params_str}, lookback: {lookback_period}")
            self._rust_engine.register_indicator(name, indicator_type, params_str, lookback_period)
    
    def is_tradable(self, symbol: str) -> bool:
        return symbol in self.symbols
    
    @property
    def order(self):
        return OrderHelper(self._rust_engine)


class OrderHelper:
    """Helper for placing orders"""
    
    def __init__(self, engine: PyBacktestEngine):
        self._engine = engine
    
    def buy(self, symbol: str, quantity: float = 1.0, quantity_type: str = "count"):
        """Place buy order"""
        self._engine.add_order(symbol, "buy", quantity, quantity_type)
    
    def sell(self, symbol: str, quantity: float = 1.0, quantity_type: str = "count"):
        """Place sell order"""
        self._engine.add_order(symbol, "sell", quantity, quantity_type)
    
    def target(self, weights, symbol: Optional[str] = None):
        """
        Set target weights for portfolio
        
        Args:
            weights: Dict of {symbol: weight} or single weight value
            symbol: Symbol (if weights is a single value)
        """
        if isinstance(weights, dict):
            for sym, weight in weights.items():
                self._engine.add_order(sym, "buy", weight, "weight")
        else:
            if symbol is None:
                raise ValueError("symbol must be provided when weights is a single value")
            self._engine.add_order(symbol, "buy", weights, "weight")

