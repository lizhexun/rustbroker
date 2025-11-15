// IndicatorEngine: Indicator registration and pre-computation

use crate::datafeed::DataFeed;
use std::collections::HashMap;
use ta::indicators::SimpleMovingAverage;
use ta::Next;

/// Indicator definition
#[derive(Clone, Debug)]
pub enum IndicatorDef {
    RustBuiltin {
        name: String,
        params: HashMap<String, String>,
        lookback_period: usize,
    },
    PythonFunction {
        name: String,
        lookback_period: usize,
    },
}

/// Indicator engine
pub struct IndicatorEngine {
    indicators: HashMap<String, IndicatorDef>,
    indicator_values: HashMap<(String, String), Vec<f64>>, // (indicator_name, symbol) -> values
    current_index: usize,
}

impl IndicatorEngine {
    pub fn new() -> Self {
        Self {
            indicators: HashMap::new(),
            indicator_values: HashMap::new(),
            current_index: 0,
        }
    }

    /// Register an indicator
    pub fn register_indicator(&mut self, name: String, def: IndicatorDef) {
        self.indicators.insert(name.clone(), def);
    }

    /// Check if there are any registered indicators
    pub fn has_indicators(&self) -> bool {
        !self.indicators.is_empty()
    }

    /// Compute all indicators for all bars
    pub fn compute_all_indicators(&mut self, datafeed: &DataFeed) {
        // Clear existing values
        self.indicator_values.clear();

        let symbols = datafeed.get_symbols();
        let timeline = datafeed.benchmark_timeline();
        let timeline_len = timeline.len();

        for (indicator_name, def) in &self.indicators.clone() {
            let lookback = match def {
                IndicatorDef::RustBuiltin { lookback_period, .. } => *lookback_period,
                IndicatorDef::PythonFunction { lookback_period, .. } => *lookback_period,
            };

            for symbol in &symbols {
                let key = (indicator_name.clone(), symbol.clone());
                let mut values = Vec::with_capacity(timeline_len);

                // Get all bars for this symbol
                let all_bars = datafeed.get_all_bars_for_symbol(symbol);
                if all_bars.is_empty() {
                    // No data for this symbol, fill with NaN
                    for _i in 0..timeline_len {
                        values.push(f64::NAN);
                    }
                    self.indicator_values.insert(key, values);
                    continue;
                }

                // Compute indicator for each bar in timeline
                match def {
                    IndicatorDef::RustBuiltin { name, params, .. } => {
                        if name == "sma" {
                            // Compute SMA using ta library
                            let period: usize = params
                                .get("period")
                                .and_then(|s| s.parse().ok())
                                .unwrap_or(lookback);
                            let field = params.get("field").map(|s| s.as_str()).unwrap_or("close");

                            // Extract field values once for efficiency
                            let field_values: Vec<f64> = match field {
                                "close" => all_bars.iter().map(|b| b.close).collect(),
                                "open" => all_bars.iter().map(|b| b.open).collect(),
                                "high" => all_bars.iter().map(|b| b.high).collect(),
                                "low" => all_bars.iter().map(|b| b.low).collect(),
                                "volume" => all_bars.iter().map(|b| b.volume).collect(),
                                _ => {
                                    // Invalid field, fill with NaN
                                    for _i in 0..timeline_len {
                                        values.push(f64::NAN);
                                    }
                                    self.indicator_values.insert(key, values);
                                    continue;
                                }
                            };

                            // Optimized SMA computation: initialize once and update incrementally
                            let mut sma = SimpleMovingAverage::new(period).unwrap_or_else(|_| {
                                SimpleMovingAverage::new(lookback.max(1)).unwrap()
                            });
                            
                            // Pre-compute SMA values for all bars
                            let mut sma_values: Vec<f64> = Vec::with_capacity(all_bars.len());
                            for &value in &field_values {
                                let sma_value = sma.next(value);
                                sma_values.push(sma_value);
                            }
                            
                            // For each timeline point, find the corresponding SMA value
                            let mut bar_idx = 0; // Track position in all_bars for efficiency
                            for i in 0..timeline_len {
                                let current_time = timeline[i];
                                
                                // Advance bar_idx to find the last bar <= current_time
                                while bar_idx < all_bars.len() && all_bars[bar_idx].datetime <= current_time {
                                    bar_idx += 1;
                                }
                                
                                // bar_idx now points to the first bar after current_time
                                // So the last available bar index is bar_idx - 1
                                if bar_idx == 0 || bar_idx - 1 < period - 1 {
                                    values.push(f64::NAN);
                                } else {
                                    // Use the SMA value at the last available bar
                                    let sma_value = sma_values[bar_idx - 1];
                                    values.push(sma_value);
                                }
                            }
                        } else {
                            // Unknown indicator, fill with NaN
                            for _i in 0..timeline_len {
                                values.push(f64::NAN);
                            }
                        }
                    }
                    IndicatorDef::PythonFunction { .. } => {
                        // Python functions are computed on-demand, fill with NaN for now
                        for _i in 0..timeline_len {
                            values.push(f64::NAN);
                        }
                    }
                }

                self.indicator_values.insert(key, values);
            }
        }
    }

    /// Get indicator value for current bar
    pub fn get_indicator_value(&self, name: &str, symbol: &str) -> Option<f64> {
        let key = (name.to_string(), symbol.to_string());
        let values = self.indicator_values.get(&key)?;

        if self.current_index >= values.len() {
            return None;
        }

        let val = values[self.current_index];
        // Return None if NaN, otherwise return the value
        if val.is_nan() {
            None
        } else {
            Some(val)
        }
    }

    /// Get indicator values for past N bars (including current)
    pub fn get_indicator_value_count(&self, name: &str, symbol: &str, count: usize) -> Option<Vec<f64>> {
        let key = (name.to_string(), symbol.to_string());
        let values = self.indicator_values.get(&key)?;

        if self.current_index >= values.len() {
            return None;
        }

        let start_idx = self.current_index.saturating_sub(count.saturating_sub(1));
        let end_idx = self.current_index + 1;

        if start_idx >= values.len() {
            return None;
        }

        let slice = &values[start_idx..end_idx.min(values.len())];
        
        // For single value (count=1), return the value even if NaN (caller can handle it)
        // For multiple values, filter out NaN to return only valid values
        if count == 1 {
            Some(vec![slice[0]])
        } else {
            let result: Vec<f64> = slice
                .iter()
                .filter(|v| !v.is_nan())
                .copied()
                .collect();

            if result.is_empty() {
                None
            } else {
                Some(result)
            }
        }
    }

    /// Update current bar index
    pub fn update_index(&mut self, index: usize) {
        self.current_index = index;
    }

    /// Set indicator value (for Python-computed indicators)
    pub fn set_indicator_value(&mut self, name: &str, symbol: &str, index: usize, value: f64) {
        let key = (name.to_string(), symbol.to_string());
        if let Some(values) = self.indicator_values.get_mut(&key) {
            if index < values.len() {
                values[index] = value;
            }
        }
    }
}


