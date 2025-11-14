// Core data types for the backtest engine

use chrono::{DateTime, NaiveDate};
use std::collections::HashMap;

/// K-line bar structure
#[derive(Clone, Debug)]
pub struct Bar {
    pub datetime: DateTime<chrono::Utc>,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
}

/// Order side
#[derive(Clone, Debug, PartialEq)]
pub enum OrderSide {
    Buy,
    Sell,
}

/// Quantity type for orders
#[derive(Clone, Debug, PartialEq)]
pub enum QuantityType {
    Count,  // Number of lots (1 lot = 100 shares)
    Cash,   // Amount of cash
    Weight, // Target portfolio weight (0.0 - 1.0)
}

/// Order structure
#[derive(Clone, Debug)]
pub struct Order {
    pub symbol: String,
    pub side: OrderSide,
    pub quantity_type: QuantityType,
    pub quantity: f64,
    pub timestamp: DateTime<chrono::Utc>,
}

/// Fill (executed trade) structure
#[derive(Clone, Debug)]
pub struct Fill {
    pub symbol: String,
    pub side: OrderSide,
    pub quantity: f64,      // In lots
    pub price: f64,
    pub commission: f64,
    pub timestamp: DateTime<chrono::Utc>,
}

/// Position information
#[derive(Clone, Debug)]
pub struct Position {
    pub symbol: String,
    pub quantity: f64,        // Total position in lots
    pub avg_cost: f64,       // Average cost price
    pub market_value: f64,   // Current market value
    pub available: f64,      // Available quantity (considering T+1)
}

/// Buy record for T+1 tracking
#[derive(Clone, Debug)]
pub struct BuyRecord {
    pub date: NaiveDate,
    pub quantity: f64,       // In lots
    pub price: f64,
}

/// Portfolio state
#[derive(Clone, Debug)]
pub struct PortfolioState {
    pub cash: f64,
    pub positions: HashMap<String, Position>,
    pub buy_records: HashMap<String, Vec<BuyRecord>>, // For T+1 tracking
    pub fills: Vec<Fill>,
    pub t0_symbols: Vec<String>, // Symbols that support T+0
}

impl PortfolioState {
    pub fn new(initial_cash: f64, t0_symbols: Vec<String>) -> Self {
        Self {
            cash: initial_cash,
            positions: HashMap::new(),
            buy_records: HashMap::new(),
            fills: Vec::new(),
            t0_symbols,
        }
    }

    /// Get available quantity considering T+1 rule
    pub fn get_available(&self, symbol: &str, current_date: NaiveDate) -> f64 {
        let position = match self.positions.get(symbol) {
            Some(p) => p.quantity,
            None => return 0.0,
        };

        // T+0 symbols: available = position
        if self.t0_symbols.contains(&symbol.to_string()) {
            return position;
        }

        // T+1 symbols: subtract today's buy quantity
        let today_buys: f64 = self
            .buy_records
            .get(symbol)
            .map(|records| {
                records
                    .iter()
                    .filter(|r| r.date == current_date)
                    .map(|r| r.quantity)
                    .sum()
            })
            .unwrap_or(0.0);

        (position - today_buys).max(0.0)
    }

    /// Calculate total equity
    pub fn calculate_equity(&self, current_prices: &HashMap<String, f64>) -> f64 {
        let positions_value: f64 = self
            .positions
            .iter()
            .map(|(symbol, pos)| {
                current_prices
                    .get(symbol)
                    .map(|price| pos.quantity * price * 100.0) // Convert lots to shares
                    .unwrap_or(pos.market_value)
            })
            .sum();

        self.cash + positions_value
    }

    /// Update T+1 availability for a new date
    pub fn update_t1_availability(&mut self, current_date: NaiveDate) {
        // Keep only today's buy records (current_date)
        for records in self.buy_records.values_mut() {
            records.retain(|r| r.date == current_date);
        }
    }
}

/// Equity curve point
#[derive(Clone, Debug)]
pub struct EquityPoint {
    pub datetime: DateTime<chrono::Utc>,
    pub equity: f64,
}

/// Performance statistics
#[derive(Clone, Debug)]
pub struct PerformanceStats {
    pub total_return: f64,
    pub annualized_return: f64,
    pub max_drawdown: f64,
    pub max_drawdown_start: Option<DateTime<chrono::Utc>>,
    pub max_drawdown_end: Option<DateTime<chrono::Utc>>,
    pub sharpe_ratio: f64,
    pub win_rate: f64,
    pub profit_loss_ratio: f64,
    pub open_count: usize,   // 开仓次数（买入成交次数）
    pub close_count: usize,  // 平仓次数（卖出成交次数）
}

