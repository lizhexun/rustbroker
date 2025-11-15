// BacktestEngine: Main backtest engine with PyO3 bindings

use crate::datafeed::DataFeed;
use crate::execution_engine::ExecutionEngine;
use crate::indicator_engine::IndicatorEngine;
use crate::metrics_recorder::MetricsRecorder;
use crate::types::{Bar, Order, OrderSide, PortfolioState, QuantityType};
use chrono::{DateTime, NaiveDateTime, TimeZone, Utc};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::wrap_pyfunction;
use std::collections::HashMap;
use std::cell::RefCell;

/// Backtest configuration
#[derive(Clone, Debug)]
pub struct BacktestConfig {
    pub start: Option<String>,
    pub end: Option<String>,
    pub cash: f64,
    pub commission_rate: f64,
    pub min_commission: f64,
    pub slippage_bps: f64,
    pub stamp_tax_rate: f64,
    pub t0_symbols: Vec<String>,
    pub period: Option<String>,
}

impl Default for BacktestConfig {
    fn default() -> Self {
        Self {
            start: None,
            end: None,
            cash: 100000.0,
            commission_rate: 0.0005,
            min_commission: 5.0,
            slippage_bps: 1.0,
            stamp_tax_rate: 0.001,
            t0_symbols: Vec::new(),
            period: None,
        }
    }
}

/// Main backtest engine
pub struct BacktestEngine {
    pub(crate) config: BacktestConfig,
    pub(crate) datafeed: DataFeed,
    pub(crate) indicator_engine: RefCell<IndicatorEngine>,
    pub(crate) execution_engine: ExecutionEngine,
    pub(crate) portfolio: PortfolioState,
    pub(crate) metrics: MetricsRecorder,
    // Cache: current prices to avoid repeated computation
    cached_current_prices: Option<(usize, HashMap<String, f64>)>,
}

impl BacktestEngine {
    pub fn new(config: BacktestConfig) -> Self {
        let execution_engine = ExecutionEngine::new(
            config.commission_rate,
            config.min_commission,
            config.slippage_bps,
            config.stamp_tax_rate,
        );

        let portfolio = PortfolioState::new(config.cash, config.t0_symbols.clone());

        Self {
            config,
            datafeed: DataFeed::new(),
            indicator_engine: RefCell::new(IndicatorEngine::new()),
            execution_engine,
            portfolio,
            metrics: MetricsRecorder::new(),
            cached_current_prices: None,
        }
    }

    /// Add market data
    pub fn add_market_data(&mut self, symbol: String, bars: Vec<Bar>) {
        self.datafeed.add_market_data(symbol, bars);
    }

    /// Set benchmark
    pub fn set_benchmark(&mut self, benchmark_bars: Vec<Bar>) {
        self.datafeed.set_benchmark(benchmark_bars);
    }

    /// Register indicator (called from Python)
    pub fn register_indicator(&self, name: String, def: crate::indicator_engine::IndicatorDef) {
        self.indicator_engine.borrow_mut().register_indicator(name, def);
    }

    /// Compute all indicators
    pub fn compute_all_indicators(&mut self) {
        use std::time::Instant;
        let start_time = Instant::now();
        self.indicator_engine.borrow_mut().compute_all_indicators(&self.datafeed);
        let elapsed = start_time.elapsed();
        println!("[性能统计] 指标计算总耗时: {:.3}秒", elapsed.as_secs_f64());
    }

    /// Reset to start of backtest (initialize indices)
    pub fn reset(&mut self) {
        // Reset datafeed to start of timeline
        self.datafeed.reset();
        // Reset indicator engine index
        self.indicator_engine.borrow_mut().update_index(0);
        // Clear price cache
        self.cached_current_prices = None;
    }


    /// Get indicator value
    pub fn get_indicator_value(&self, name: &str, symbol: &str, count: Option<usize>) -> Option<Vec<f64>> {
        let engine = self.indicator_engine.borrow();
        match count {
            Some(n) => engine.get_indicator_value_count(name, symbol, n),
            None => engine.get_indicator_value(name, symbol).map(|v| vec![v]),
        }
    }

    /// Get multiple indicator values for a symbol (batch operation)
    pub fn get_indicator_values(&self, symbol: &str, names: &[&str]) -> std::collections::HashMap<String, Option<f64>> {
        let engine = self.indicator_engine.borrow();
        engine.get_indicator_values(symbol, names)
    }

    /// Get bars for a symbol
    pub fn get_bars(&self, symbol: &str, count: usize) -> Vec<Bar> {
        self.datafeed.get_bars(symbol, count)
    }

    /// Add order
    pub fn add_order(&mut self, order: Order) {
        self.execution_engine.add_order(order);
    }

    /// Execute all orders for current bar
    pub fn execute_orders(&mut self) -> Vec<crate::types::Fill> {
        let current_bars = self.datafeed.get_current_bars();
        let fills = self.execution_engine.execute_all_orders(&current_bars, &mut self.portfolio);
        
        // Record fills
        for fill in &fills {
            self.portfolio.fills.push(fill.clone());
        }
        self.metrics.record_fills(fills.clone());
        fills
    }

    /// Record equity
    pub fn record_equity(&mut self) {
        use std::time::Instant;
        let start_time = Instant::now();
        
        if let Some(datetime) = self.datafeed.get_current_datetime() {
            let current_index = self.datafeed.current_index();
            
            // Check cache for current prices
            let current_prices = if let Some((cached_idx, cached_prices)) = &self.cached_current_prices {
                if *cached_idx == current_index {
                    cached_prices.clone()
                } else {
                    // Cache miss: compute prices
                    let current_bars = self.datafeed.get_current_bars();
                    let prices: HashMap<String, f64> = current_bars
                        .iter()
                        .map(|(s, b)| (s.clone(), b.close))
                        .collect();
                    // Update cache
                    self.cached_current_prices = Some((current_index, prices.clone()));
                    prices
                }
            } else {
                // No cache: compute and cache
                let current_bars = self.datafeed.get_current_bars();
                let prices: HashMap<String, f64> = current_bars
                    .iter()
                    .map(|(s, b)| (s.clone(), b.close))
                    .collect();
                self.cached_current_prices = Some((current_index, prices.clone()));
                prices
            };
            
            let equity = self.portfolio.calculate_equity(&current_prices);
            self.metrics.record_equity(datetime, equity);
            
            // Record benchmark equity (based on benchmark bar close price)
            if let Some(benchmark_bar) = self.datafeed.get_current_benchmark_bar() {
                // Get initial benchmark price from first bar in timeline
                let initial_benchmark_price = if let Some(first_bar) = self.datafeed.get_initial_benchmark_bar() {
                    first_bar.close
                } else {
                    benchmark_bar.close
                };
                // Benchmark equity = initial_cash * (current_price / initial_price)
                let benchmark_equity = self.config.cash * (benchmark_bar.close / initial_benchmark_price);
                self.metrics.record_benchmark(datetime, benchmark_equity);
            }
            
            // Track equity recording time (only print periodically to avoid spam)
            let elapsed = start_time.elapsed();
            if current_index % 1000 == 0 && elapsed.as_millis() > 1 {
                println!("[性能统计] Equity记录耗时: {:.3}毫秒 (bar #{})", elapsed.as_secs_f64() * 1000.0, current_index);
            }
        }
    }

    /// Move to next bar
    pub fn next(&mut self) {
        let current_date = self.datafeed.get_current_datetime()
            .map(|dt| dt.date_naive())
            .unwrap_or_else(|| chrono::Utc::now().date_naive());
        self.portfolio.update_t1_availability(current_date);
        self.datafeed.next();
        // Invalidate price cache when moving to next bar (will be recomputed on next record_equity)
        let new_index = self.datafeed.current_index();
        if let Some((cached_idx, _)) = &self.cached_current_prices {
            if *cached_idx != new_index {
                self.cached_current_prices = None;
            }
        }
        self.indicator_engine.borrow_mut().update_index(new_index);
    }

    /// Check if has next bar
    pub fn has_next(&self) -> bool {
        self.datafeed.has_next()
    }

    /// Get performance stats
    pub fn get_stats(&self) -> crate::types::PerformanceStats {
        self.metrics.calculate_stats()
    }

    /// Get equity curve
    pub fn get_equity_curve(&self) -> Vec<(String, f64)> {
        self.metrics
            .get_equity_curve()
            .iter()
            .map(|p| (p.datetime.to_rfc3339(), p.equity))
            .collect()
    }

    /// Get fills
    pub fn get_fills(&self) -> &[crate::types::Fill] {
        &self.portfolio.fills
    }
}

// PyO3 bindings
#[pyclass]
pub struct PyBacktestConfig {
    #[pyo3(get, set)]
    pub start: Option<String>,
    #[pyo3(get, set)]
    pub end: Option<String>,
    #[pyo3(get, set)]
    pub cash: f64,
    #[pyo3(get, set)]
    pub commission_rate: f64,
    #[pyo3(get, set)]
    pub min_commission: f64,
    #[pyo3(get, set)]
    pub slippage_bps: f64,
    #[pyo3(get, set)]
    pub stamp_tax_rate: f64,
    #[pyo3(get, set)]
    pub t0_symbols: Vec<String>,
    #[pyo3(get, set)]
    pub period: Option<String>,
}

#[pymethods]
impl PyBacktestConfig {
    #[new]
    #[pyo3(signature = (start=None, end=None, cash=None, commission_rate=None, min_commission=None, slippage_bps=None, stamp_tax_rate=None, t0_symbols=None, period=None))]
    fn new(
        start: Option<String>,
        end: Option<String>,
        cash: Option<f64>,
        commission_rate: Option<f64>,
        min_commission: Option<f64>,
        slippage_bps: Option<f64>,
        stamp_tax_rate: Option<f64>,
        t0_symbols: Option<Vec<String>>,
        period: Option<String>,
    ) -> Self {
        Self {
            start,
            end,
            cash: cash.unwrap_or(100000.0),
            commission_rate: commission_rate.unwrap_or(0.0005),
            min_commission: min_commission.unwrap_or(5.0),
            slippage_bps: slippage_bps.unwrap_or(1.0),
            stamp_tax_rate: stamp_tax_rate.unwrap_or(0.001),
            t0_symbols: t0_symbols.unwrap_or_default(),
            period,
        }
    }
}

#[pyclass]
pub struct PyBacktestEngine {
    engine: BacktestEngine,
}

#[pymethods]
impl PyBacktestEngine {
    #[new]
    fn new(config: &PyBacktestConfig) -> Self {
        let rust_config = BacktestConfig {
            start: config.start.clone(),
            end: config.end.clone(),
            cash: config.cash,
            commission_rate: config.commission_rate,
            min_commission: config.min_commission,
            slippage_bps: config.slippage_bps,
            stamp_tax_rate: config.stamp_tax_rate,
            t0_symbols: config.t0_symbols.clone(),
            period: config.period.clone(),
        };
        Self {
            engine: BacktestEngine::new(rust_config),
        }
    }

    fn add_market_data(&mut self, symbol: String, bars: Vec<PyBar>) -> PyResult<()> {
        let rust_bars: Vec<Bar> = bars.into_iter().map(|b| b.into()).collect();
        self.engine.add_market_data(symbol, rust_bars);
        Ok(())
    }

    fn set_benchmark(&mut self, bars: Vec<PyBar>) -> PyResult<()> {
        let rust_bars: Vec<Bar> = bars.into_iter().map(|b| b.into()).collect();
        self.engine.set_benchmark(rust_bars);
        Ok(())
    }

    fn get_current_bars(&self) -> PyResult<HashMap<String, PyBar>> {
        let bars = self.engine.datafeed.get_current_bars();
        Ok(bars.into_iter().map(|(k, v)| (k, PyBar::from(v))).collect())
    }

    fn get_symbols(&self) -> Vec<String> {
        self.engine.datafeed.get_symbols()
    }

    fn get_current_datetime(&self) -> Option<String> {
        self.engine.datafeed.get_current_datetime().map(|dt| dt.to_rfc3339())
    }

    fn get_indicator_value(&self, name: String, symbol: String, count: Option<usize>) -> PyResult<Option<Vec<f64>>> {
        Ok(self.engine.get_indicator_value(&name, &symbol, count))
    }

    fn get_indicator_values(&self, symbol: String, names: Vec<String>) -> PyResult<HashMap<String, Option<f64>>> {
        let name_refs: Vec<&str> = names.iter().map(|s| s.as_str()).collect();
        let result = self.engine.get_indicator_values(&symbol, &name_refs);
        Ok(result)
    }

    fn get_bars(&self, symbol: String, count: usize) -> PyResult<Vec<PyBar>> {
        let bars = self.engine.get_bars(&symbol, count);
        Ok(bars.into_iter().map(PyBar::from).collect())
    }

    fn has_next(&self) -> bool {
        self.engine.has_next()
    }

    fn next(&mut self) {
        self.engine.next();
    }

    fn get_cash(&self) -> f64 {
        self.engine.portfolio.cash
    }

    fn get_equity(&self) -> PyResult<f64> {
        let current_bars = self.engine.datafeed.get_current_bars();
        let current_prices: HashMap<String, f64> = current_bars
            .iter()
            .map(|(s, b)| (s.clone(), b.close))
            .collect();
        Ok(self.engine.portfolio.calculate_equity(&current_prices))
    }

    fn get_positions(&self) -> PyResult<HashMap<String, HashMap<String, f64>>> {
        let mut result = HashMap::new();
        let current_date = self
            .engine
            .datafeed
            .get_current_datetime()
            .map(|dt| dt.date_naive())
            .unwrap_or_else(|| chrono::Utc::now().date_naive());
        for (symbol, pos) in &self.engine.portfolio.positions {
            let mut pos_dict = HashMap::new();
            pos_dict.insert("position".to_string(), pos.quantity);
            pos_dict.insert("available".to_string(), self.engine.portfolio.get_available(symbol, current_date));
            pos_dict.insert("avg_cost".to_string(), pos.avg_cost);
            pos_dict.insert("market_value".to_string(), pos.market_value);
            result.insert(symbol.clone(), pos_dict);
        }
        Ok(result)
    }

    fn add_order(&mut self, symbol: String, side: String, quantity: f64, quantity_type: String) -> PyResult<()> {
        let order_side = match side.as_str() {
            "buy" => OrderSide::Buy,
            "sell" => OrderSide::Sell,
            _ => return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>("Invalid side")),
        };

        let qty_type = match quantity_type.as_str() {
            "count" => QuantityType::Count,
            "cash" => QuantityType::Cash,
            "weight" => QuantityType::Weight,
            _ => return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>("Invalid quantity_type")),
        };

        let datetime = self.engine.datafeed.get_current_datetime()
            .unwrap_or_else(|| Utc::now());

        let order = Order {
            symbol,
            side: order_side,
            quantity_type: qty_type,
            quantity,
            timestamp: datetime,
        };

        self.engine.add_order(order);
        Ok(())
    }

    fn execute_orders(&mut self) -> PyResult<Vec<PyFill>> {
        let fills = self.engine.execute_orders();
        Ok(fills.iter().map(|f| PyFill::from(f.clone())).collect())
    }

    fn record_equity(&mut self) {
        self.engine.record_equity();
    }

    fn get_stats(&self) -> PyResult<PyObject> {
        let stats = self.engine.get_stats();
        Python::with_gil(|py| {
            let dict = PyDict::new_bound(py);
            dict.set_item("total_return", stats.total_return)?;
            dict.set_item("annualized_return", stats.annualized_return)?;
            dict.set_item("max_drawdown", stats.max_drawdown)?;
            if let Some(start) = stats.max_drawdown_start {
                dict.set_item("max_drawdown_start", start.to_rfc3339())?;
            }
            if let Some(end) = stats.max_drawdown_end {
                dict.set_item("max_drawdown_end", end.to_rfc3339())?;
            }
            dict.set_item("sharpe_ratio", stats.sharpe_ratio)?;
            dict.set_item("win_rate", stats.win_rate)?;
            dict.set_item("profit_loss_ratio", stats.profit_loss_ratio)?;
            dict.set_item("open_count", stats.open_count)?;
            dict.set_item("close_count", stats.close_count)?;
            
            // Benchmark statistics
            if let Some(benchmark_return) = stats.benchmark_return {
                dict.set_item("benchmark_return", benchmark_return)?;
            }
            if let Some(benchmark_annualized_return) = stats.benchmark_annualized_return {
                dict.set_item("benchmark_annualized_return", benchmark_annualized_return)?;
            }
            if let Some(benchmark_max_drawdown) = stats.benchmark_max_drawdown {
                dict.set_item("benchmark_max_drawdown", benchmark_max_drawdown)?;
            }
            if let Some(start) = stats.benchmark_max_drawdown_start {
                dict.set_item("benchmark_max_drawdown_start", start.to_rfc3339())?;
            }
            if let Some(end) = stats.benchmark_max_drawdown_end {
                dict.set_item("benchmark_max_drawdown_end", end.to_rfc3339())?;
            }
            
            Ok(dict.into())
        })
    }

    fn get_equity_curve(&self) -> Vec<(String, f64)> {
        self.engine.get_equity_curve()
    }

    fn register_indicator(&self, name: String, indicator_type: String, params: HashMap<String, String>, lookback_period: usize) -> PyResult<()> {
        use crate::indicator_engine::IndicatorDef;
        let def = match indicator_type.as_str() {
            "rust_builtin" => IndicatorDef::RustBuiltin {
                name: params.get("name").cloned().unwrap_or_else(|| "unknown".to_string()),
                params,
                lookback_period,
            },
            "python_function" => IndicatorDef::PythonFunction {
                name: params.get("name").cloned().unwrap_or_else(|| "unknown".to_string()),
                lookback_period,
            },
            _ => return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Unknown indicator type: {}", indicator_type))),
        };
        self.engine.register_indicator(name, def);
        Ok(())
    }

    fn compute_all_indicators(&mut self) -> PyResult<()> {
        self.engine.compute_all_indicators();
        Ok(())
    }

    fn reset(&mut self) {
        self.engine.reset();
    }

    fn get_fills(&self) -> PyResult<Vec<PyFill>> {
        let fills = self.engine.get_fills();
        Ok(fills.iter().map(|f| PyFill::from(f.clone())).collect())
    }

    /// Run backtest with strategy callbacks
    /// Main loop is executed in Rust for better performance
    fn run_backtest(
        this: &Bound<'_, Self>,
        py: Python,
        strategy: &Bound<'_, PyAny>,
        create_context: &Bound<'_, PyAny>,
        compute_indicators: bool,
    ) -> PyResult<PyObject> {
        use std::time::Instant;
        let total_start_time = Instant::now();
        
        // Call on_start - allow Python callbacks to access engine
        {
            let ctx = create_context.call0()?;
            // Call strategy on_start - this may register indicators
            strategy.call_method1("on_start", (ctx,))?;
        }

        // Now get mutable reference to mutate engine
        let mut self_ref = this.borrow_mut();
        
        // Compute indicators if needed (after on_start to allow registration)
        // Check if there are any registered indicators in the engine
        let has_indicators = self_ref.engine.indicator_engine.borrow().has_indicators();
        if has_indicators {
            self_ref.engine.compute_all_indicators();
        }

        // Reset to start of backtest
        self_ref.engine.reset();

        // Main backtest loop
        let loop_start_time = Instant::now();
        let mut bar_count = 0;
        let mut python_callback_time = std::time::Duration::ZERO;
        let mut order_execution_time = std::time::Duration::ZERO;
        let mut equity_recording_time = std::time::Duration::ZERO;
        
        while self_ref.engine.has_next() {
            bar_count += 1;
            
            // Execute orders first (needs mutable access)
            let order_start = Instant::now();
            let fills = self_ref.engine.execute_orders();
            order_execution_time += order_start.elapsed();
            
            // Then create context and call Python callbacks (releases mutable borrow)
            {
                // Drop mutable borrow before calling Python
                drop(self_ref);
                
                let python_start = Instant::now();
                let ctx = create_context.call0()?;
                
                // Invalidate cache before calling on_bar (ensures fresh data)
                // Note: This is a no-op if cache invalidation is not implemented
                // The cache will be populated on first property access
                
                // Call strategy.on_bar
                strategy.call_method1("on_bar", (ctx.clone(),))?;
                
                // Call on_trade for each fill
                for fill in &fills {
                    let fill_dict: PyObject = {
                        let dict = PyDict::new_bound(py);
                        dict.set_item("symbol", &fill.symbol)?;
                        dict.set_item("side", match fill.side {
                            crate::types::OrderSide::Buy => "buy",
                            crate::types::OrderSide::Sell => "sell",
                        })?;
                        dict.set_item("filled_quantity", fill.quantity)?;
                        dict.set_item("price", fill.price)?;
                        dict.set_item("commission", fill.commission)?;
                        dict.set_item("timestamp", fill.timestamp.to_rfc3339())?;
                        dict.into()
                    };
                    
                    // Call strategy.on_trade
                    strategy.call_method1("on_trade", (fill_dict, ctx.clone()))?;
                }
                python_callback_time += python_start.elapsed();
                
                // Re-acquire mutable borrow after Python callbacks
                self_ref = this.borrow_mut();
            }
            
            // Record equity (needs mutable access)
            let equity_start = Instant::now();
            self_ref.engine.record_equity();
            equity_recording_time += equity_start.elapsed();
            
            // Move to next bar (needs mutable access)
            self_ref.engine.next();
        }
        
        // Print loop statistics
        let loop_elapsed = loop_start_time.elapsed();
        println!("\n[性能统计] ========== 回测主循环统计 ==========");
        println!("[性能统计] 总bar数: {}", bar_count);
        println!("[性能统计] 主循环总耗时: {:.3}秒", loop_elapsed.as_secs_f64());
        println!("[性能统计] 平均每根bar耗时: {:.3}毫秒", loop_elapsed.as_secs_f64() * 1000.0 / bar_count.max(1) as f64);
        println!("[性能统计] Python回调总耗时: {:.3}秒 ({:.1}%)", 
                 python_callback_time.as_secs_f64(),
                 if loop_elapsed.as_secs_f64() > 0.0 { python_callback_time.as_secs_f64() / loop_elapsed.as_secs_f64() * 100.0 } else { 0.0 });
        println!("[性能统计] 订单执行总耗时: {:.3}秒 ({:.1}%)", 
                 order_execution_time.as_secs_f64(),
                 if loop_elapsed.as_secs_f64() > 0.0 { order_execution_time.as_secs_f64() / loop_elapsed.as_secs_f64() * 100.0 } else { 0.0 });
        println!("[性能统计] Equity记录总耗时: {:.3}秒 ({:.1}%)", 
                 equity_recording_time.as_secs_f64(),
                 if loop_elapsed.as_secs_f64() > 0.0 { equity_recording_time.as_secs_f64() / loop_elapsed.as_secs_f64() * 100.0 } else { 0.0 });
        println!("[性能统计] ======================================\n");

        // Call on_stop - release mutable borrow temporarily
        drop(self_ref);
        {
            let ctx = create_context.call0()?;
            strategy.call_method1("on_stop", (ctx,))?;
        }

        // Print total backtest time
        let total_elapsed = total_start_time.elapsed();
        println!("[性能统计] ========== 回测总耗时 ==========");
        println!("[性能统计] 回测总耗时: {:.3}秒", total_elapsed.as_secs_f64());
        println!("[性能统计] ===============================\n");
        
        // Get and return results (use immutable borrow)
        let self_immut = this.borrow();
        let stats_result = self_immut.engine.get_stats();
        let equity_curve = self_immut.engine.get_equity_curve();

        // Build result dictionary
        let result_dict = PyDict::new_bound(py);
        let stats_dict = PyDict::new_bound(py);
        
        stats_dict.set_item("total_return", stats_result.total_return)?;
        stats_dict.set_item("annualized_return", stats_result.annualized_return)?;
        stats_dict.set_item("max_drawdown", stats_result.max_drawdown)?;
        if let Some(start) = stats_result.max_drawdown_start {
            stats_dict.set_item("max_drawdown_start", start.to_rfc3339())?;
        }
        if let Some(end) = stats_result.max_drawdown_end {
            stats_dict.set_item("max_drawdown_end", end.to_rfc3339())?;
        }
        stats_dict.set_item("sharpe_ratio", stats_result.sharpe_ratio)?;
        stats_dict.set_item("win_rate", stats_result.win_rate)?;
        stats_dict.set_item("profit_loss_ratio", stats_result.profit_loss_ratio)?;
        stats_dict.set_item("open_count", stats_result.open_count)?;
        stats_dict.set_item("close_count", stats_result.close_count)?;
        
        // Benchmark statistics
        if let Some(benchmark_return) = stats_result.benchmark_return {
            stats_dict.set_item("benchmark_return", benchmark_return)?;
        }
        if let Some(benchmark_annualized_return) = stats_result.benchmark_annualized_return {
            stats_dict.set_item("benchmark_annualized_return", benchmark_annualized_return)?;
        }
        if let Some(benchmark_max_drawdown) = stats_result.benchmark_max_drawdown {
            stats_dict.set_item("benchmark_max_drawdown", benchmark_max_drawdown)?;
        }
        if let Some(start) = stats_result.benchmark_max_drawdown_start {
            stats_dict.set_item("benchmark_max_drawdown_start", start.to_rfc3339())?;
        }
        if let Some(end) = stats_result.benchmark_max_drawdown_end {
            stats_dict.set_item("benchmark_max_drawdown_end", end.to_rfc3339())?;
        }
        
        result_dict.set_item("stats", stats_dict)?;
        result_dict.set_item("equity_curve", equity_curve)?;
        
        Ok(result_dict.into())
    }
}

// PyBar for Python interface
#[pyclass]
#[derive(Clone)]
pub struct PyBar {
    #[pyo3(get, set)]
    pub datetime: String,
    #[pyo3(get, set)]
    pub open: f64,
    #[pyo3(get, set)]
    pub high: f64,
    #[pyo3(get, set)]
    pub low: f64,
    #[pyo3(get, set)]
    pub close: f64,
    #[pyo3(get, set)]
    pub volume: f64,
}

#[pymethods]
impl PyBar {
    #[new]
    fn new(datetime: String, open: f64, high: f64, low: f64, close: f64, volume: f64) -> Self {
        Self {
            datetime,
            open,
            high,
            low,
            close,
            volume,
        }
    }
}

impl From<Bar> for PyBar {
    fn from(bar: Bar) -> Self {
        Self {
            datetime: bar.datetime.to_rfc3339(),
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
            volume: bar.volume,
        }
    }
}

impl From<PyBar> for Bar {
    fn from(bar: PyBar) -> Self {
        let datetime = DateTime::parse_from_rfc3339(&bar.datetime)
            .map(|dt| dt.with_timezone(&Utc))
            .or_else(|_| {
                NaiveDateTime::parse_from_str(&bar.datetime, "%Y-%m-%d %H:%M:%S")
                    .map(|naive| Utc.from_utc_datetime(&naive))
            })
            .unwrap_or_else(|_| Utc::now());
        Self {
            datetime,
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
            volume: bar.volume,
        }
    }
}

// PyFill for Python interface
#[pyclass]
#[derive(Clone)]
pub struct PyFill {
    #[pyo3(get, set)]
    pub symbol: String,
    #[pyo3(get, set)]
    pub side: String,
    #[pyo3(get, set)]
    pub quantity: f64,
    #[pyo3(get, set)]
    pub price: f64,
    #[pyo3(get, set)]
    pub commission: f64,
    #[pyo3(get, set)]
    pub timestamp: String,
}

#[pymethods]
impl PyFill {
    #[new]
    fn new(symbol: String, side: String, quantity: f64, price: f64, commission: f64, timestamp: String) -> Self {
        Self {
            symbol,
            side,
            quantity,
            price,
            commission,
            timestamp,
        }
    }
}

impl From<crate::types::Fill> for PyFill {
    fn from(fill: crate::types::Fill) -> Self {
        Self {
            symbol: fill.symbol,
            side: match fill.side {
                crate::types::OrderSide::Buy => "buy".to_string(),
                crate::types::OrderSide::Sell => "sell".to_string(),
            },
            quantity: fill.quantity,
            price: fill.price,
            commission: fill.commission,
            timestamp: fill.timestamp.to_rfc3339(),
        }
    }
}

// OrderHelper for Python - will be created in Python layer

pub fn register_module(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyBacktestConfig>()?;
    m.add_class::<PyBacktestEngine>()?;
    m.add_class::<PyBar>()?;
    m.add_class::<PyFill>()?;
    
    // Register database functions
    m.add_function(wrap_pyfunction!(crate::database::get_market_data, m)?)?;
    m.add_function(wrap_pyfunction!(crate::database::save_klines, m)?)?;
    m.add_function(wrap_pyfunction!(crate::database::save_klines_from_csv, m)?)?;
    m.add_function(wrap_pyfunction!(crate::database::resample_klines, m)?)?;
    m.add_function(wrap_pyfunction!(crate::database::load_and_synthesize_klines, m)?)?;
    
    Ok(())
}

