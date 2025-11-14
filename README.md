# rustbroker

基于 Rust 核心引擎的高性能 Python 回测框架

## 特�?

- 🚀 **高性能**：Rust 核心引擎处理性能关键路径，Python 层仅负责策略逻辑
- 📊 **易用�?*：简洁直观的 API 设计，策略作者只需关注交易逻辑
- 🔧 **功能丰富**：支持多资产回测、指标计算、订单管理、风险控�?
- 🎯 **A股优�?*：内�?T+1/T+0 规则、手续费计算、印花税�?A 股特�?

## 安装

### 前置要求

- **Rust**: 1.70+ ([安装 Rust](https://www.rust-lang.org/tools/install))
- **Python**: 3.8+
- **maturin**: 用于构建 Python 扩展

### 安装步骤

1. **安装 Rust**（如果尚未安装）�?

   ```bash
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
   ```

2. **安装 maturin**�?

   ```bash
   pip install maturin
  

   ```

3. **构建 Rust 扩展**�?

   ```bash
   cd rust/engine_rust
   maturin develop
   # 或者使�?release 模式（更慢但更快）：
   maturin develop --release

   python -m maturin develop --release
   
   ```

4. **安装 Python 依赖**（如果需要）�?

   ```bash
   pip install pandas numpy  # 用于数据处理
   ```

## 快速开�?

### 1. 创建策略

创建一个简单的双均线策略：

```python
import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "python"))

from rustbroker.api import BacktestEngine, BacktestConfig
from rustbroker.strategy import Strategy
from rustbroker.indicators import Indicator

class DoubleMAStrategy(Strategy):
    """双均线策略示�?""
    
    def on_start(self, ctx):
        """初始化策�?""
        ctx.state["last_signal"] = {}
    
    def on_bar(self, ctx):
        """每个bar的处理逻辑"""
        for symbol in ctx.symbols:
            # 获取指标�?
            sma_short = ctx.get_indicator_value("sma_5_close", symbol)
            sma_long = ctx.get_indicator_value("sma_20_close", symbol)
            
            if sma_short is None or sma_long is None:
                continue
            
            # 获取持仓信息
            pos_info = ctx.positions.get(symbol, {})
            position = pos_info.get("position", 0.0)
            available = pos_info.get("available", 0.0)
            
            # 双均线策略：金叉买入，死叉卖�?
            if sma_short > sma_long and position == 0:
                # 金叉：买�?
                ctx.order.buy(symbol=symbol, quantity=1.0, quantity_type="count")
            elif sma_short < sma_long and available > 0:
                # 死叉：卖�?
                ctx.order.sell(symbol=symbol, quantity=available, quantity_type="count")
    
    def on_stop(self, ctx):
        """回测结束"""
        print(f"回测结束，最终净�? {ctx.equity:.2f}")
```

### 2. 准备数据

准备 CSV 格式的行情数据（`data/sh600000_min.csv`）：

```csv
datetime,open,high,low,close,volume
2025-01-01 09:30:00,10.0,10.5,9.8,10.2,1000000
2025-01-01 09:31:00,10.2,10.8,10.0,10.5,1200000
...
```

### 3. 配置并运行回�?

```python
def main():
    # 配置回测参数
    cfg = BacktestConfig(
        start="2025-01-01",
        end="2025-12-31",
        cash=100000.0,              # 初始资金
        commission_rate=0.0005,     # 佣金费率 0.05%
        min_commission=5.0,         # 最小手续费
        slippage_bps=1.0,           # 滑点 1 bps
        stamp_tax_rate=0.001,       # 印花税率 0.1%
    )
    
    # 创建回测引擎
    engine = BacktestEngine(cfg)
    
    # 加载行情数据（需要根据实际的数据加载方式调整�?
    # 这里假设�?load_csv_to_bars 函数来加载CSV数据
    from rustbroker.data import load_csv_to_bars  # 如果存在
    
    symbol = "600000.SH"
    data_path = "data/sh600000_min.csv"
    bars = load_csv_to_bars(data_path, symbol=symbol)
    
    # 或者手动准备数�?
    # bars = [
    #     {
    #         "datetime": "2025-01-01 09:30:00",
    #         "open": 10.0,
    #         "high": 10.5,
    #         "low": 9.8,
    #         "close": 10.2,
    #         "volume": 1000000
    #     },
    #     # ... 更多数据
    # ]
    
    # 准备数据字典
    market_data = {symbol: [dict(bar) for bar in bars]}
    benchmark_data = {"BENCH": [dict(bar) for bar in bars]}
    
    # 创建策略
    strategy = DoubleMAStrategy()
    
    # 运行回测
    # 注意：根据实际API，可能需要先注册指标
    result = engine.run(strategy, market_data, benchmark=benchmark_data)
    
    # 查看结果
    stats = result.get("stats", {})
    print(f"总收�? {stats.get('total_return', 0):.2%}")
    print(f"年化收益: {stats.get('annualized_return', 0):.2%}")
    print(f"最大回�? {stats.get('max_drawdown', 0):.4f}")
    print(f"夏普比率: {stats.get('sharpe', 0):.4f}")

if __name__ == "__main__":
    main()
```

### 4. 运行示例

项目提供了多个示例，可以直接运行�?

```bash
# 双均线策略示�?
python examples/double_sma_strategy.py

# 投资组合回测示例
python examples/run_portfolio_backtest.py

# 多资产回测示�?
python examples/run_multi_assets.py
```

## 核心概念

### BacktestEngine

回测引擎是核心组件，负责�?

- 管理市场数据
- 执行策略逻辑
- 处理订单撮合
- 计算性能指标

### Strategy

策略接口，需要实现：

- `on_start(ctx)`: 策略初始化，注册指标
- `on_bar(ctx)`: 每个bar的处理逻辑
- `on_trade(fill, ctx)`: 订单成交回调（可选）
- `on_stop(ctx)`: 回测结束回调（可选）

### BarContext

上下文对象，提供�?

- `ctx.datetime`: 当前bar的时�?
- `ctx.cash`: 可用现金
- `ctx.equity`: 总资�?
- `ctx.positions`: 持仓信息
- `ctx.order`: 下单接口
- `ctx.get_indicator_value(name, symbol)`: 获取指标�?
- `ctx.get_bars(symbol, count)`: 获取历史bars

### OrderHelper

下单助手，提供：

- `ctx.order.buy(symbol, quantity, quantity_type)`: 买入
- `ctx.order.sell(symbol, quantity, quantity_type)`: 卖出
- `ctx.order.target(weights)`: 设置目标权重

## 更多示例

查看 `examples/` 目录了解更多示例�?

- `double_sma_strategy.py`: 双均线策�?
- `run_portfolio_backtest.py`: 投资组合回测
- `run_multi_assets.py`: 多资产回�?
- `run_grid_search.py`: 参数网格搜索

## 文档

- [技术设计文档](docs/TECHNICAL_DESIGN.md)
- [回测引擎核心文档](docs/backtest_engine_core.md)

## 许可�?

MIT License
