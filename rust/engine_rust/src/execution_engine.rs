// ExecutionEngine: Order execution and matching

use crate::types::{Bar, BuyRecord, Fill, Order, OrderSide, PortfolioState, Position, QuantityType};
use std::collections::HashMap;

pub struct ExecutionEngine {
    orders: Vec<Order>,
    commission_rate: f64,
    min_commission: f64,
    slippage_bps: f64,
    stamp_tax_rate: f64,
}

impl ExecutionEngine {
    pub fn new(
        commission_rate: f64,
        min_commission: f64,
        slippage_bps: f64,
        stamp_tax_rate: f64,
    ) -> Self {
        Self {
            orders: Vec::new(),
            commission_rate,
            min_commission,
            slippage_bps,
            stamp_tax_rate,
        }
    }

    /// Add an order
    pub fn add_order(&mut self, order: Order) {
        self.orders.push(order);
    }

    /// Clear all orders
    pub fn clear_orders(&mut self) {
        self.orders.clear();
    }

    /// Execute all orders
    pub fn execute_all_orders(
        &mut self,
        current_bars: &HashMap<String, Bar>,
        portfolio: &mut PortfolioState,
    ) -> Vec<Fill> {
        let mut fills = Vec::new();

        // Sort orders: sell first, then buy
        self.orders.sort_by(|a, b| {
            match (&a.side, &b.side) {
                (OrderSide::Sell, OrderSide::Buy) => std::cmp::Ordering::Less,
                (OrderSide::Buy, OrderSide::Sell) => std::cmp::Ordering::Greater,
                _ => std::cmp::Ordering::Equal,
            }
        });

        // Execute sell orders first
        for order in &self.orders.clone() {
            if order.side == OrderSide::Sell {
                if let Some(fill) = self.execute_order(order, current_bars, portfolio) {
                    fills.push(fill);
                }
            }
        }

        // Execute buy orders
        for order in &self.orders.clone() {
            if order.side == OrderSide::Buy {
                if let Some(fill) = self.execute_order(order, current_bars, portfolio) {
                    fills.push(fill);
                }
            }
        }

        self.orders.clear();
        fills
    }

    /// Execute a single order
    fn execute_order(
        &self,
        order: &Order,
        current_bars: &HashMap<String, Bar>,
        portfolio: &mut PortfolioState,
    ) -> Option<Fill> {
        let bar = current_bars.get(&order.symbol)?;
        let base_price = bar.close;

        // Calculate fill price with slippage
        let fill_price = self.calculate_fill_price(&order.side, base_price);

        // Calculate quantity based on quantity_type
        let quantity_lots = match order.quantity_type {
            QuantityType::Count => order.quantity,
            QuantityType::Cash => {
                let quantity_shares = order.quantity / fill_price;
                self.round_to_lot(quantity_shares)
            }
            QuantityType::Weight => {
                let current_prices: HashMap<String, f64> = current_bars
                    .iter()
                    .map(|(s, b)| (s.clone(), b.close))
                    .collect();
                let equity = portfolio.calculate_equity(&current_prices);
                let target_value = equity * order.quantity;
                let current_position = portfolio
                    .positions
                    .get(&order.symbol)
                    .map(|p| p.market_value)
                    .unwrap_or(0.0);
                let needed_value = target_value - current_position;
                let quantity_shares = needed_value / fill_price;
                self.round_to_lot(quantity_shares)
            }
        };

        if quantity_lots <= 0.0 {
            return None;
        }

        // Validate order
        match order.side {
            OrderSide::Sell => {
                let trade_date = order.timestamp.date_naive();
                let available = portfolio.get_available(&order.symbol, trade_date);
                if quantity_lots > available {
                    return None; // Reject: insufficient position
                }
            }
            OrderSide::Buy => {
                let trade_amount = quantity_lots * fill_price * 100.0; // Convert lots to shares
                let commission = self.calculate_commission(trade_amount, &order.side);
                if trade_amount + commission > portfolio.cash {
                    return None; // Reject: insufficient cash
                }
            }
        }

        // Execute the order
        let trade_amount = quantity_lots * fill_price * 100.0; // Convert lots to shares
        let commission = self.calculate_commission(trade_amount, &order.side);

        match order.side {
            OrderSide::Buy => {
                portfolio.cash -= trade_amount + commission;
                let trade_date = order.timestamp.date_naive();
                portfolio.add_position(&order.symbol, quantity_lots, fill_price, trade_date);
            }
            OrderSide::Sell => {
                let released_cash = portfolio.reduce_position(&order.symbol, quantity_lots, fill_price);
                portfolio.cash += released_cash - commission;
            }
        }

        Some(Fill {
            symbol: order.symbol.clone(),
            side: order.side.clone(),
            quantity: quantity_lots,
            price: fill_price,
            commission,
            timestamp: order.timestamp,
        })
    }

    /// Calculate fill price with slippage
    fn calculate_fill_price(&self, side: &OrderSide, base_price: f64) -> f64 {
        match side {
            OrderSide::Buy => base_price * (1.0 + self.slippage_bps / 10000.0),
            OrderSide::Sell => base_price * (1.0 - self.slippage_bps / 10000.0),
        }
    }

    /// Calculate commission
    fn calculate_commission(&self, trade_amount: f64, side: &OrderSide) -> f64 {
        let base_commission = (trade_amount * self.commission_rate).max(self.min_commission);
        match side {
            OrderSide::Buy => base_commission,
            OrderSide::Sell => base_commission + trade_amount * self.stamp_tax_rate,
        }
    }

    /// Round quantity to lots (1 lot = 100 shares)
    fn round_to_lot(&self, quantity_shares: f64) -> f64 {
        (quantity_shares / 100.0).floor()
    }
}

// Add methods to PortfolioState
impl PortfolioState {
    /// Add position
    pub fn add_position(&mut self, symbol: &str, quantity: f64, price: f64, trade_date: chrono::NaiveDate) {
        let position = self.positions.entry(symbol.to_string()).or_insert(Position {
            symbol: symbol.to_string(),
            quantity: 0.0,
            avg_cost: 0.0,
            market_value: 0.0,
            available: 0.0,
        });

        let total_cost = position.quantity * position.avg_cost * 100.0 + quantity * price * 100.0;
        let total_quantity = position.quantity + quantity;
        position.quantity = total_quantity;
        position.avg_cost = if total_quantity > 0.0 {
            total_cost / (total_quantity * 100.0)
        } else {
            0.0
        };

        // Record buy for T+1 tracking
        if !self.t0_symbols.contains(&symbol.to_string()) {
            let records = self.buy_records.entry(symbol.to_string()).or_insert_with(Vec::new);
            records.push(BuyRecord {
                date: trade_date,
                quantity,
                price,
            });
        }
    }

    /// Reduce position
    pub fn reduce_position(&mut self, symbol: &str, quantity: f64, price: f64) -> f64 {
        let position = match self.positions.get_mut(symbol) {
            Some(p) => p,
            None => return 0.0,
        };

        if quantity > position.quantity {
            return 0.0;
        }

        position.quantity -= quantity;
        if position.quantity <= 0.0 {
            self.positions.remove(symbol);
        }

        // Return released cash
        quantity * price * 100.0
    }
}

