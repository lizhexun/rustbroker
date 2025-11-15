// DataFeed: Market data management and benchmark timeline

use crate::types::Bar;
use chrono::{DateTime, Utc};
use std::collections::HashMap;

// Make benchmark_timeline accessible
impl DataFeed {
    pub fn benchmark_timeline(&self) -> &[DateTime<Utc>] {
        &self.benchmark_timeline
    }
}

pub struct DataFeed {
    benchmark_timeline: Vec<DateTime<Utc>>,
    benchmark_bars: Vec<Bar>,  // Store benchmark bars for equity calculation
    market_data: HashMap<String, Vec<Bar>>,
    current_index: usize,
    // Cache: symbol -> current bar index in that symbol's data
    symbol_indices: HashMap<String, usize>,
    // Cache: current bars to avoid repeated computation
    cached_current_bars: Option<(usize, HashMap<String, Bar>)>,
}

impl DataFeed {
    pub fn new() -> Self {
        Self {
            benchmark_timeline: Vec::new(),
            benchmark_bars: Vec::new(),
            market_data: HashMap::new(),
            current_index: 0,
            symbol_indices: HashMap::new(),
            cached_current_bars: None,
        }
    }

    /// Add market data for a symbol
    pub fn add_market_data(&mut self, symbol: String, bars: Vec<Bar>) {
        // Sort bars by datetime
        let mut sorted_bars = bars;
        sorted_bars.sort_by_key(|b| b.datetime);
        self.market_data.insert(symbol.clone(), sorted_bars);
        // Initialize index for this symbol if benchmark is already set
        if !self.benchmark_timeline.is_empty() {
            self._update_symbol_index(&symbol);
        }
    }

    /// Set benchmark timeline (from benchmark symbol's bars)
    pub fn set_benchmark(&mut self, benchmark_bars: Vec<Bar>) {
        let mut sorted_bars = benchmark_bars;
        sorted_bars.sort_by_key(|b| b.datetime);
        self.benchmark_timeline = sorted_bars.iter().map(|b| b.datetime).collect();
        self.benchmark_bars = sorted_bars;
        // Initialize symbol indices using binary search
        self._update_all_symbol_indices();
    }
    
    /// Find the index of the last bar <= target_time using binary search
    fn _find_bar_index(&self, bars: &[Bar], target_time: DateTime<Utc>) -> Option<usize> {
        if bars.is_empty() {
            return None;
        }
        
        // Binary search for the last bar <= target_time
        let mut left = 0;
        let mut right = bars.len();
        
        while left < right {
            let mid = (left + right) / 2;
            if bars[mid].datetime <= target_time {
                left = mid + 1;
            } else {
                right = mid;
            }
        }
        
        if left > 0 {
            Some(left - 1)
        } else {
            None
        }
    }
    
    /// Update index for a single symbol
    fn _update_symbol_index(&mut self, symbol: &str) {
        if self.current_index >= self.benchmark_timeline.len() {
            return;
        }
        
        let current_time = self.benchmark_timeline[self.current_index];
        if let Some(bars) = self.market_data.get(symbol) {
            if let Some(idx) = self._find_bar_index(bars, current_time) {
                self.symbol_indices.insert(symbol.to_string(), idx);
            }
        }
    }
    
    /// Update indices for all symbols
    fn _update_all_symbol_indices(&mut self) {
        let symbols: Vec<String> = self.market_data.keys().cloned().collect();
        for symbol in symbols {
            self._update_symbol_index(&symbol);
        }
    }

    /// Get current bars for all symbols (with caching)
    pub fn get_current_bars(&self) -> HashMap<String, Bar> {
        if self.current_index >= self.benchmark_timeline.len() {
            return HashMap::new();
        }

        // Check cache first
        if let Some((cached_idx, cached_bars)) = &self.cached_current_bars {
            if *cached_idx == self.current_index {
                return cached_bars.clone();
            }
        }

        // Cache miss: compute current bars (should rarely happen if next() is called properly)
        self._compute_current_bars()
    }
    
    /// Internal method to compute current bars (used for caching)
    fn _compute_current_bars(&self) -> HashMap<String, Bar> {
        if self.current_index >= self.benchmark_timeline.len() {
            return HashMap::new();
        }

        let mut result = HashMap::new();
        for (symbol, bars) in &self.market_data {
            if let Some(&idx) = self.symbol_indices.get(symbol) {
                if idx < bars.len() {
                    result.insert(symbol.clone(), bars[idx].clone());
                }
            }
        }
        result
    }

    /// Get historical bars for a symbol (preventing look-ahead bias)
    pub fn get_bars(&self, symbol: &str, count: usize) -> Vec<Bar> {
        if self.current_index >= self.benchmark_timeline.len() {
            return Vec::new();
        }

        let bars = match self.market_data.get(symbol) {
            Some(b) => b,
            None => return Vec::new(),
        };

        // Use cached index
        let current_bar_idx = match self.symbol_indices.get(symbol) {
            Some(&idx) => idx,
            None => return Vec::new(),
        };

        let start_idx = current_bar_idx.saturating_sub(count.saturating_sub(1));
        let end_idx = current_bar_idx + 1;
        
        if end_idx > bars.len() {
            return Vec::new();
        }

        bars[start_idx..end_idx].to_vec()
    }

    /// Get current bar for a symbol
    pub fn get_current_bar(&self, symbol: &str) -> Option<Bar> {
        if self.current_index >= self.benchmark_timeline.len() {
            return None;
        }

        let bars = self.market_data.get(symbol)?;
        let idx = self.symbol_indices.get(symbol)?;
        
        if *idx < bars.len() {
            Some(bars[*idx].clone())
        } else {
            None
        }
    }

    /// Get current datetime
    pub fn get_current_datetime(&self) -> Option<DateTime<Utc>> {
        self.benchmark_timeline.get(self.current_index).copied()
    }

    /// Get current benchmark bar
    pub fn get_current_benchmark_bar(&self) -> Option<Bar> {
        if self.current_index < self.benchmark_bars.len() {
            Some(self.benchmark_bars[self.current_index].clone())
        } else {
            None
        }
    }

    /// Get initial benchmark bar (first bar)
    pub fn get_initial_benchmark_bar(&self) -> Option<Bar> {
        self.benchmark_bars.first().cloned()
    }

    /// Move to next bar
    pub fn next(&mut self) {
        if self.current_index < self.benchmark_timeline.len() {
            self.current_index += 1;
            
            // Update symbol indices for the new time point
            // Since time only moves forward, we can optimize by only advancing indices
            let current_time = if self.current_index < self.benchmark_timeline.len() {
                self.benchmark_timeline[self.current_index]
            } else {
                // At end, clear cache
                self.cached_current_bars = None;
                return;
            };
            
            // Collect symbols first to avoid borrowing issues
            let symbols: Vec<String> = self.market_data.keys().cloned().collect();
            
            // Update indices for all symbols (advance if needed)
            for symbol in symbols {
                if let Some(bars) = self.market_data.get(&symbol) {
                    if let Some(current_idx) = self.symbol_indices.get_mut(&symbol) {
                        // Advance index if current bar's time is before current_time
                        while *current_idx + 1 < bars.len() && bars[*current_idx + 1].datetime <= current_time {
                            *current_idx += 1;
                        }
                    } else {
                        // Initialize if not found
                        if let Some(idx) = self._find_bar_index(bars, current_time) {
                            self.symbol_indices.insert(symbol, idx);
                        }
                    }
                }
            }
            
            // Update cache for new index
            let current_bars = self._compute_current_bars();
            self.cached_current_bars = Some((self.current_index, current_bars));
        }
    }

    /// Check if there are more bars
    pub fn has_next(&self) -> bool {
        self.current_index < self.benchmark_timeline.len()
    }

    /// Get current index
    pub fn current_index(&self) -> usize {
        self.current_index
    }

    /// Get all symbols
    pub fn get_symbols(&self) -> Vec<String> {
        self.market_data.keys().cloned().collect()
    }

    /// Check if symbol is tradable at current time
    pub fn is_tradable(&self, symbol: &str) -> bool {
        self.get_current_bar(symbol).is_some()
    }

    /// Get all bars for a symbol (for indicator computation)
    pub fn get_all_bars_for_symbol(&self, symbol: &str) -> Vec<Bar> {
        self.market_data.get(symbol).cloned().unwrap_or_default()
    }

    /// Reset to start of timeline
    pub fn reset(&mut self) {
        self.current_index = 0;
        // Re-initialize symbol indices
        self._update_all_symbol_indices();
        // Initialize cache for index 0
        let current_bars = self._compute_current_bars();
        self.cached_current_bars = Some((0, current_bars));
    }
}

