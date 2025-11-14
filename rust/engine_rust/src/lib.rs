use pyo3::prelude::*;

mod database;
mod datafeed;
mod engine;
mod execution_engine;
mod indicator_engine;
mod metrics_recorder;
mod types;

pub use database::{get_market_data, resample_klines, save_klines, save_klines_from_csv};
pub use engine::{PyBacktestConfig, PyBacktestEngine, PyBar};

// Placeholder for indicators module - can be added later
pub mod indicators {
    pub fn vectorized_sma(_data: &[f64], _period: usize) -> Vec<f64> {
        vec![]
    }
    
    pub fn vectorized_rsi(_data: &[f64], _period: usize) -> Vec<f64> {
        vec![]
    }
}

#[pymodule]
fn engine_rust(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    engine::register_module(py, m)
} 