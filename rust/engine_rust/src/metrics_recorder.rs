// MetricsRecorder: Performance metrics recording and calculation

use crate::types::{EquityPoint, Fill, PerformanceStats};
use chrono::{DateTime, Utc};

pub struct MetricsRecorder {
    equity_curve: Vec<EquityPoint>,
    fills: Vec<Fill>,
    benchmark_curve: Vec<EquityPoint>,
}

impl MetricsRecorder {
    pub fn new() -> Self {
        Self {
            equity_curve: Vec::new(),
            fills: Vec::new(),
            benchmark_curve: Vec::new(),
        }
    }

    /// Record equity point
    pub fn record_equity(&mut self, datetime: DateTime<Utc>, equity: f64) {
        self.equity_curve.push(EquityPoint { datetime, equity });
    }

    /// Record benchmark equity point
    pub fn record_benchmark(&mut self, datetime: DateTime<Utc>, equity: f64) {
        self.benchmark_curve.push(EquityPoint { datetime, equity });
    }

    /// Record fill
    pub fn record_fill(&mut self, fill: Fill) {
        self.fills.push(fill);
    }

    /// Record multiple fills
    pub fn record_fills(&mut self, fills: Vec<Fill>) {
        self.fills.extend(fills);
    }

    /// Calculate performance statistics
    pub fn calculate_stats(&self) -> PerformanceStats {
        let strategy_stats = if self.equity_curve.is_empty() {
            PerformanceStats {
                total_return: 0.0,
                annualized_return: 0.0,
                max_drawdown: 0.0,
                max_drawdown_start: None,
                max_drawdown_end: None,
                sharpe_ratio: 0.0,
                win_rate: 0.0,
                profit_loss_ratio: 0.0,
                open_count: 0,
                close_count: 0,
                benchmark_return: None,
                benchmark_annualized_return: None,
                benchmark_max_drawdown: None,
                benchmark_max_drawdown_start: None,
                benchmark_max_drawdown_end: None,
            }
        } else {
            let initial_equity = self.equity_curve[0].equity;
            let final_equity = self.equity_curve.last().unwrap().equity;
            let total_return = (final_equity - initial_equity) / initial_equity;

            // Calculate annualized return
            let days = if self.equity_curve.len() > 1 {
                let duration = self.equity_curve.last().unwrap().datetime
                    - self.equity_curve[0].datetime;
                duration.num_days() as f64
            } else {
                1.0
            };
            let years = days / 365.25;
            let annualized_return = if years > 0.0 {
                (final_equity / initial_equity).powf(1.0 / years) - 1.0
            } else {
                total_return
            };

            // Calculate max drawdown
            let (max_drawdown, max_dd_start, max_dd_end) = self.calculate_max_drawdown_with_period();

            // Calculate Sharpe ratio
            let sharpe_ratio = self.calculate_sharpe_ratio();

            // Calculate win rate and profit/loss ratio
            let (win_rate, profit_loss_ratio) = self.calculate_trade_stats();

            // Count open and close trades
            let open_count = self.fills.iter()
                .filter(|f| matches!(f.side, crate::types::OrderSide::Buy))
                .count();
            let close_count = self.fills.iter()
                .filter(|f| matches!(f.side, crate::types::OrderSide::Sell))
                .count();

            // Calculate benchmark statistics
            let (benchmark_return, benchmark_annualized_return, benchmark_max_dd, benchmark_max_dd_start, benchmark_max_dd_end) = 
                self.calculate_benchmark_stats();

            PerformanceStats {
                total_return,
                annualized_return,
                max_drawdown,
                max_drawdown_start: max_dd_start,
                max_drawdown_end: max_dd_end,
                sharpe_ratio,
                win_rate,
                profit_loss_ratio,
                open_count,
                close_count,
                benchmark_return,
                benchmark_annualized_return,
                benchmark_max_drawdown: benchmark_max_dd,
                benchmark_max_drawdown_start: benchmark_max_dd_start,
                benchmark_max_drawdown_end: benchmark_max_dd_end,
            }
        };

        strategy_stats
    }

    /// Calculate benchmark statistics
    fn calculate_benchmark_stats(&self) -> (Option<f64>, Option<f64>, Option<f64>, Option<DateTime<Utc>>, Option<DateTime<Utc>>) {
        if self.benchmark_curve.is_empty() {
            return (None, None, None, None, None);
        }

        let initial_equity = self.benchmark_curve[0].equity;
        let final_equity = self.benchmark_curve.last().unwrap().equity;
        let total_return = (final_equity - initial_equity) / initial_equity;

        // Calculate annualized return
        let days = if self.benchmark_curve.len() > 1 {
            let duration = self.benchmark_curve.last().unwrap().datetime
                - self.benchmark_curve[0].datetime;
            duration.num_days() as f64
        } else {
            1.0
        };
        let years = days / 365.25;
        let annualized_return = if years > 0.0 {
            (final_equity / initial_equity).powf(1.0 / years) - 1.0
        } else {
            total_return
        };

        // Calculate max drawdown
        let (max_drawdown, max_dd_start, max_dd_end) = self.calculate_benchmark_max_drawdown_with_period();

        (Some(total_return), Some(annualized_return), Some(max_drawdown), max_dd_start, max_dd_end)
    }

    /// Calculate benchmark maximum drawdown with period information
    fn calculate_benchmark_max_drawdown_with_period(&self) -> (f64, Option<DateTime<Utc>>, Option<DateTime<Utc>>) {
        if self.benchmark_curve.is_empty() {
            return (0.0, None, None);
        }

        let mut max_equity = self.benchmark_curve[0].equity;
        let mut max_equity_time = self.benchmark_curve[0].datetime;
        let mut max_dd = 0.0;
        let mut max_dd_start: Option<DateTime<Utc>> = None;
        let mut max_dd_end: Option<DateTime<Utc>> = None;
        let mut current_dd_start: Option<DateTime<Utc>> = None;

        for point in &self.benchmark_curve {
            if point.equity > max_equity {
                // New peak reached, reset drawdown tracking
                max_equity = point.equity;
                max_equity_time = point.datetime;
                current_dd_start = None;
            } else {
                // In drawdown
                if current_dd_start.is_none() {
                    // Start of a new drawdown period
                    current_dd_start = Some(max_equity_time);
                }
                
                let drawdown = (max_equity - point.equity) / max_equity;
                if drawdown > max_dd {
                    max_dd = drawdown;
                    max_dd_start = current_dd_start;
                    max_dd_end = Some(point.datetime);
                }
            }
        }

        (max_dd, max_dd_start, max_dd_end)
    }

    /// Calculate maximum drawdown
    fn calculate_max_drawdown(&self) -> f64 {
        if self.equity_curve.is_empty() {
            return 0.0;
        }

        let mut max_equity = self.equity_curve[0].equity;
        let mut max_dd = 0.0;

        for point in &self.equity_curve {
            if point.equity > max_equity {
                max_equity = point.equity;
            }
            let drawdown = (max_equity - point.equity) / max_equity;
            if drawdown > max_dd {
                max_dd = drawdown;
            }
        }

        max_dd
    }

    /// Calculate maximum drawdown with period information
    fn calculate_max_drawdown_with_period(&self) -> (f64, Option<DateTime<Utc>>, Option<DateTime<Utc>>) {
        if self.equity_curve.is_empty() {
            return (0.0, None, None);
        }

        let mut max_equity = self.equity_curve[0].equity;
        let mut max_equity_time = self.equity_curve[0].datetime;
        let mut max_dd = 0.0;
        let mut max_dd_start: Option<DateTime<Utc>> = None;
        let mut max_dd_end: Option<DateTime<Utc>> = None;
        let mut current_dd_start: Option<DateTime<Utc>> = None;

        for point in &self.equity_curve {
            if point.equity > max_equity {
                // New peak reached, reset drawdown tracking
                max_equity = point.equity;
                max_equity_time = point.datetime;
                current_dd_start = None;
            } else {
                // In drawdown
                if current_dd_start.is_none() {
                    // Start of a new drawdown period
                    current_dd_start = Some(max_equity_time);
                }
                
                let drawdown = (max_equity - point.equity) / max_equity;
                if drawdown > max_dd {
                    max_dd = drawdown;
                    max_dd_start = current_dd_start;
                    max_dd_end = Some(point.datetime);
                }
            }
        }

        (max_dd, max_dd_start, max_dd_end)
    }

    /// Calculate Sharpe ratio
    fn calculate_sharpe_ratio(&self) -> f64 {
        if self.equity_curve.len() < 2 {
            return 0.0;
        }

        // Calculate daily returns
        let mut returns = Vec::new();
        for i in 1..self.equity_curve.len() {
            let prev_equity = self.equity_curve[i - 1].equity;
            let curr_equity = self.equity_curve[i].equity;
            if prev_equity > 0.0 {
                returns.push((curr_equity - prev_equity) / prev_equity);
            }
        }

        if returns.is_empty() {
            return 0.0;
        }

        let mean_return = returns.iter().sum::<f64>() / returns.len() as f64;
        let variance = returns
            .iter()
            .map(|r| (r - mean_return).powi(2))
            .sum::<f64>()
            / returns.len() as f64;
        let std_dev = variance.sqrt();

        if std_dev == 0.0 {
            return 0.0;
        }

        // Annualized Sharpe (assuming 252 trading days)
        (mean_return / std_dev) * (252.0_f64).sqrt()
    }

    /// Calculate trade statistics
    fn calculate_trade_stats(&self) -> (f64, f64) {
        // Group fills by round trips (simplified)
        let mut profits = Vec::new();
        let mut losses = Vec::new();

        // Simple approach: track buy/sell pairs
        let mut positions: std::collections::HashMap<String, Vec<(f64, f64)>> =
            std::collections::HashMap::new();

        for fill in &self.fills {
            let entry = positions.entry(fill.symbol.clone()).or_insert_with(Vec::new);

            match fill.side {
                crate::types::OrderSide::Buy => {
                    entry.push((fill.quantity, fill.price));
                }
                crate::types::OrderSide::Sell => {
                    let mut remaining = fill.quantity;
                    let mut total_cost = 0.0;

                    while remaining > 0.0 && !entry.is_empty() {
                        let (qty, price) = entry[0];
                        let used = remaining.min(qty);
                        total_cost += used * price * 100.0;
                        remaining -= used;

                        if used >= qty {
                            entry.remove(0);
                        } else {
                            entry[0] = (qty - used, price);
                        }
                    }

                    if total_cost > 0.0 {
                        let revenue = fill.quantity * fill.price * 100.0;
                        let pnl = revenue - total_cost;
                        if pnl > 0.0 {
                            profits.push(pnl);
                        } else {
                            losses.push(pnl.abs());
                        }
                    }
                }
            }
        }

        let total_trades = profits.len() + losses.len();
        let win_rate = if total_trades > 0 {
            profits.len() as f64 / total_trades as f64
        } else {
            0.0
        };

        let avg_profit = if !profits.is_empty() {
            profits.iter().sum::<f64>() / profits.len() as f64
        } else {
            0.0
        };

        let avg_loss = if !losses.is_empty() {
            losses.iter().sum::<f64>() / losses.len() as f64
        } else {
            0.0
        };

        let profit_loss_ratio = if avg_loss > 0.0 {
            avg_profit / avg_loss
        } else if avg_profit > 0.0 {
            f64::INFINITY
        } else {
            0.0
        };

        (win_rate, profit_loss_ratio)
    }

    /// Get equity curve
    pub fn get_equity_curve(&self) -> &[EquityPoint] {
        &self.equity_curve
    }

    /// Get fills
    pub fn get_fills(&self) -> &[Fill] {
        &self.fills
    }

    /// Get benchmark curve
    pub fn get_benchmark_curve(&self) -> &[EquityPoint] {
        &self.benchmark_curve
    }
}

