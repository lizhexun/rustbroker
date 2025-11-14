"""
Main API for backtest engine
"""

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
        
        # Call on_start
        ctx = self._create_context()
        strategy.on_start(ctx)
        
        # Compute indicators (if any were registered)
        # This must be called after on_start and before the main loop
        if self._indicator_registry:
            self._rust_engine.compute_all_indicators()
        
        # Reset to start of backtest (ensure indices are initialized)
        self._rust_engine.reset()
        
        # Main backtest loop
        while self._rust_engine.has_next():
            ctx = self._create_context()
            
            # Call strategy
            strategy.on_bar(ctx)
            
            # Execute orders
            fills = self._rust_engine.execute_orders()
            
            # Call on_trade for each fill
            for fill in fills:
                fill_dict = {
                    'side': fill.side,
                    'symbol': fill.symbol,
                    'filled_quantity': fill.quantity,
                    'price': fill.price,
                    'commission': fill.commission,
                    'timestamp': fill.timestamp,
                }
                strategy.on_trade(fill_dict, ctx)
            
            # Record equity
            self._rust_engine.record_equity()
            
            # Move to next bar
            self._rust_engine.next()
        
        # Call on_stop
        ctx = self._create_context()
        strategy.on_stop(ctx)
        
        # Return results
        stats_result = self._rust_engine.get_stats()
        equity_curve = self._rust_engine.get_equity_curve()
        
        # Convert stats PyObject to dict (it should already be a dict from Rust)
        stats_dict = {}
        try:
            # Try to convert PyObject to dict
            if hasattr(stats_result, 'get'):
                stats_dict = dict(stats_result)
            else:
                # Fallback: access as attributes
                stats_dict = {
                    'total_return': getattr(stats_result, 'total_return', 0.0),
                    'annualized_return': getattr(stats_result, 'annualized_return', 0.0),
                    'max_drawdown': getattr(stats_result, 'max_drawdown', 0.0),
                    'sharpe_ratio': getattr(stats_result, 'sharpe_ratio', 0.0),
                    'win_rate': getattr(stats_result, 'win_rate', 0.0),
                    'profit_loss_ratio': getattr(stats_result, 'profit_loss_ratio', 0.0),
                }
        except Exception:
            # If conversion fails, use empty dict
            stats_dict = {}
        
        return {
            "stats": stats_dict,
            "equity_curve": equity_curve,
        }
    
    def _create_context(self):
        """Create BarContext for strategy"""
        return BarContext(self, self._indicator_registry)
    
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
    """Context object passed to strategy"""
    
    def __init__(self, engine: BacktestEngine, indicator_registry: Dict):
        self._backtest_engine = engine
        self._indicator_registry = indicator_registry
        self._current_bars = None
        self._positions = None
        self._cash = None
        self._equity = None
    
    @property
    def datetime(self) -> str:
        """Current bar datetime"""
        return self._backtest_engine._rust_engine.get_current_datetime() or ""
    
    @property
    def symbols(self) -> List[str]:
        """List of all symbols"""
        return self._backtest_engine._rust_engine.get_symbols()
    
    @property
    def cash(self) -> float:
        """Available cash"""
        if self._cash is None:
            self._cash = self._backtest_engine._rust_engine.get_cash()
        return self._cash
    
    @property
    def equity(self) -> float:
        """Total equity"""
        if self._equity is None:
            self._equity = self._backtest_engine._rust_engine.get_equity()
        return self._equity
    
    @property
    def positions(self) -> Dict[str, Dict[str, float]]:
        """Current positions"""
        if self._positions is None:
            self._positions = self._backtest_engine._rust_engine.get_positions()
        return self._positions
    
    def get_bars(self, symbol: str, count: int = 1) -> List[Dict[str, Any]]:
        """Get historical bars for a symbol"""
        py_bars = self._backtest_engine._rust_engine.get_bars(symbol, count)
        return [self._bar_to_dict(bar) for bar in py_bars]
    
    def get_indicator_value(self, name: str, symbol: str, count: Optional[int] = None) -> Optional[Any]:
        """Get indicator value"""
        values = self._backtest_engine._rust_engine.get_indicator_value(name, symbol, count)
        if values is None:
            return None
        if count is None or count == 1:
            return round(values[0], 4) if values else None
        return [round(v, 4) for v in values]
    
    def register_indicator(self, name: str, indicator_def, count: Optional[int] = None):
        """Register an indicator (called in on_start)"""
        # Store in Python registry for reference
        self._indicator_registry[name] = {
            "def": indicator_def,
            "lookback": count or 1,
        }
        
        # Also register in Rust engine
        if isinstance(indicator_def, dict):
            indicator_type = indicator_def.get("type", "rust_builtin")
            params = indicator_def.get("params", {})
            lookback_period = count or indicator_def.get("lookback_period", 1)
            
            # Convert params to string dict for Rust
            params_str = {k: str(v) for k, v in params.items()}
            # Add indicator name to params if present
            if "name" in indicator_def:
                params_str["name"] = indicator_def["name"]
            
            self._backtest_engine._rust_engine.register_indicator(
                name,
                indicator_type,
                params_str,
                lookback_period
            )
    
    def is_tradable(self, symbol: str) -> bool:
        """Check if symbol is tradable at current time"""
        return symbol in self.symbols
    
    @property
    def order(self):
        """Order helper for placing orders"""
        return OrderHelper(self._backtest_engine._rust_engine)
    
    def _bar_to_dict(self, bar: PyBar) -> Dict[str, Any]:
        """Convert PyBar to dict"""
        return {
            "datetime": bar.datetime,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }


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

