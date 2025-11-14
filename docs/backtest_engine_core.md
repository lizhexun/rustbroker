# 回测核心架构与流程（Strategy First 设计）

本方案以策略作者的使用体验为起点，重新规划回测引擎：让“写策略”只关注行情、指标和下单，其他繁琐环节（事件调度、仓位校验、撮合、统计）全部由引擎代劳，同时保持当前 Rust 内核的性能优势。

---

## 设计原则

- **一根 K 线一个决策**：策略只需实现 `on_bar(ctx)`，其余生命周期回调保持可选。
- **API 接近交易直觉**：统一使用 `ctx.order.buy(...)`、`ctx.order.sell(...)` 或 `ctx.order.target(...)`，通过 `quantity_type` 指定按手数、金额或权重下单（将权重设为 0 即可清仓）。
- **即时反馈**：卖出、买入在同一根 bar 内执行；成交会同步反映到 `ctx`，策略侧无需手动等待事件。
- **多标的天然支持**：`ctx.symbols` 携带所有标的最新价和持仓，按字典访问即可。
- **默认安全配置**：禁止裸卖空，仓位、现金检查和成本计算自动完成；支持手续费率、最小手续费和印花税配置（A股通常最低5元佣金，0.1%印花税）；需要扩展时再显式开启。
- **A股交易规则**：自动处理最小交易单位（1手=100股）、佣金和印花税（卖出时收取），符合A股实际交易规则。
- **交易日规则可配置**：默认按照 A 股 T+1 交割，可在初始化时为特定标的声明 T+0，从而允许当日回转交易。
- **指标预计算与存储**：指标注册和计算应在策略的 `on_start` 方法中进行，这样有利于时间线对齐。指标可以使用 Rust 或 Python 计算，计算好的指标与行情数据一起存放在 Rust 引擎中。策略通过 `ctx.get_indicator_value(name, symbol, count=N)` 方便地获取指标值：`count=1` 或省略获取当前 bar 的指标值，`count=N` 获取包含当前 bar 在内的过去 N 个指标值，引擎自动确保只能访问当前及历史 bar 的数据，防止未来函数问题。

---

## 三步快速上手

1. **定义策略类**：继承 `Strategy`，实现 `on_bar(self, ctx)`。
2. **在 `on_bar` 中调用下单 Helper**：例如`ctx.order.sell(symbol="510100.SH", quantity=0.3, quantity_type="weight")`。
3. **启动回测**：传入策略与数据，其他流程（撮合、仓位记录、统计输出）由引擎自动完成。

无需手动管理事件循环、仓位状态或净值计算，写策略像写交易脚本一样简单。

---

## 双均线策略示例（完整代码）

以下是一个完整的双均线策略示例，展示如何使用预计算指标和下单 API：

**重要说明**：策略的所有计算都基于基准时间线。在本示例中，基准时间线就是 600000.SH 的行情数据本身。引擎会按照基准时间线（bars）逐根 bar 执行策略，策略在每根 bar 时获取对应的指标值和行情数据。多标的回测时，所有标的的数据都会对齐到基准时间线上。

```python
from rustbroker.api import BacktestEngine, BacktestConfig
from rustbroker.strategy import Strategy
from rustbroker.data import load_csv_to_bars

# 定义Python计算指标的函数（放在类外部，便于复用和维护）
def custom_momentum(bars):
    """自定义动量指标：计算过去N根bar的收益率"""
    if len(bars) < 10:
        return None
    closes = [bar["close"] for bar in bars]
    return (closes[-1] - closes[0]) / closes[0]

def custom_rsi_numpy(bars):
    """使用numpy计算RSI指标"""
    import numpy as np
    if len(bars) < 15:
        return None
    closes = np.array([bar["close"] for bar in bars])
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-14:])
    avg_loss = np.mean(losses[-14:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# 1. 定义策略类
class DoubleMAStrategy(Strategy):
    """双均线策略：快速均线在慢均线上面时全仓买入，否则空仓"""
    
    def on_start(self, ctx):
        """策略启动时注册和计算指标（有利于时间线对齐）"""
        # 在 on_start 中注册指标，确保指标计算基于基准时间线
        # 这样在每根 bar 执行时，指标值都能正确对齐到当前时间点
        from rustbroker.indicators import Indicator
        
        # 注册指标：引擎会根据基准时间线自动计算所有 bar 的指标值
        # 指标计算基于基准时间线（benchmark_bars），确保策略在每根 bar 都能获取到对应的指标值
        
        # 方式1：使用Rust内置指标（性能最优，推荐）
        ctx.register_indicator("sma_5", Indicator.sma(period=5, field="close"))   # 5日均线
        ctx.register_indicator("sma_20", Indicator.sma(period=20, field="close")) # 20日均线
        
        # 方式2：使用Python函数计算自定义指标（灵活性高，适合复杂指标）
        ctx.register_indicator("custom_mom", custom_momentum, count=10)  # 10周期自定义动量
        
        # 方式3：使用Python库（如numpy）计算复杂指标
        ctx.register_indicator("rsi_numpy", custom_rsi_numpy, count=15)  # 使用numpy计算的RSI

    
    def on_bar(self, ctx):
        """每根 bar 执行一次"""
        for symbol in ctx.symbols:
            # 2. 获取预计算的指标值（推荐方式，性能最优）
            sma_short = ctx.get_indicator_value("sma_5", symbol)   # 5日均线（快速均线）
            sma_long = ctx.get_indicator_value("sma_20", symbol)    # 20日均线（慢速均线）
            
            # 如果指标值无效（数据不足），跳过
            if sma_short is None or sma_long is None:
                continue

            # 当前价格
            bars = ctx.get_bars(symbol, count=1)
            close = bars[0]["close"] if bars else None
            # 3. 获取当前持仓信息
            pos_info = ctx.positions.get(symbol, {})
            position = pos_info.get("position", 0.0)      # 持仓数量（单位：手）
            available = pos_info.get("available", 0.0)    # 可用数量（考虑 T+1 规则）
            has_position = position > 0                   # 是否有持仓
            
            # 4. 双均线策略逻辑：快速均线在慢均线上面全仓买入，否则空仓
            if close > sma_short > sma_long:
                # 快速均线在慢均线上面：全仓买入
                if not has_position and ctx.cash > 0:
                    # 当前没有持仓且有现金，执行买入
                    ctx.order.buy(symbol=symbol, quantity=1.0, quantity_type="weight")
            else:
                # 否则：空仓（卖出所有持仓）
                if has_position and available > 0:
                    # 当前有持仓，执行卖出
                    ctx.order.sell(symbol=symbol, quantity=available,quantity_type="count")
    
    def on_trade(self, fill, ctx):
        """订单成交回调"""
        print(f"成交: {fill['side']} {fill['symbol']} {fill['filled_quantity']:.2f}股 @ {fill['price']:.4f}")
    
    def on_stop(self, ctx):
        """回测结束回调"""
        print(f"回测结束，最终净值: {ctx.equity:.2f}")

# 7. 配置和运行回测
def main():
    # 配置回测参数
    cfg = BacktestConfig(
        start="2025-01-01",
        end="2025-12-31",
        cash=100000.0,              # 初始资金10万
        commission_rate=0.0005,     # 佣金费率 0.05%
        min_commission=5.0,         # 最小手续费5元（A股标准）
        slippage_bps=1.0,           # 滑点 1 bps
        stamp_tax_rate=0.001,       # 印花税率 0.1%（卖出时收取）
    )
    
    # 创建回测引擎
    engine = BacktestEngine(cfg)
    
    # 1. 加载行情数据（基准时间线）
    # 基准时间线：策略执行的时间基准，所有计算都基于这个时间线
    # 在本示例中，基准时间线就是 600000.SH 的行情数据本身
    symbol = "600000.SH"
    benchmark_bars = load_csv_to_bars("data/sh600000_min.csv", symbol=symbol)  # 基准时间线 = 600000.SH 的行情数据
    
    # 2. 准备行情数据字典（用于添加到引擎）
    # 行情数据通过 data 参数传入，格式为 {symbol: [bars]}
    market_data = {symbol: benchmark_bars}
    
    # 3. 准备基准数据（基准时间线）
    # 基准数据通过 benchmark 参数传入，格式为 {benchmark_name: [bars]}
    # 在本示例中，基准就是行情数据本身
    benchmark_data = {"BENCH": benchmark_bars}
    
    # 4. 创建策略实例
    strategy = DoubleMAStrategy()
    
    # 5. 运行回测
    # 引擎会：
    #   - 添加行情数据（market_data）到引擎
    #   - 调用策略的 on_start 方法，策略在 on_start 中注册和计算指标
    #   - 按照基准时间线（benchmark_data）逐根 bar 执行策略
    #   - 策略在每根 bar 时获取对应的指标值和行情数据，所有计算都对齐到基准时间线
    # 注意：指标注册和计算应该在策略的 on_start 方法中进行，这样有利于时间线对齐
    result = engine.run(
        strategy, 
        data=market_data,                    # 添加行情数据
        benchmark=benchmark_data             # 指定基准时间线
    )
    
    # 查看回测结果
    stats = result.get("stats", {})
    print(f"总收益: {stats.get('total_return', 0):.2%}")
    print(f"年化收益: {stats.get('annualized_return', 0):.2%}")
    print(f"最大回撤: {stats.get('max_drawdown', 0):.4f}")
    print(f"夏普比率: {stats.get('sharpe_ratio', 0):.4f}")
    print(f"胜率: {stats.get('win_rate', 0):.2%}")
    print(f"盈亏比: {stats.get('profit_loss_ratio', 0):.4f}")

if __name__ == "__main__":
    main()
```

**策略要点说明**：

1. **指标注册**：在 `on_start` 方法中注册指标，这样有利于时间线对齐，确保指标计算基于基准时间线
2. **预计算指标**：使用 `ctx.get_indicator_value(name, symbol)` 获取预计算的指标值，性能最优
3. **简单逻辑**：快速均线在慢均线上面时全仓买入，否则空仓，逻辑清晰简单
4. **权重下单**：使用 `quantity_type="weight"` 实现全仓（weight=1.0）和空仓（weight=0.0），引擎自动计算所需手数
5. **下单 API**：使用 `ctx.order.buy()` 和 `ctx.order.sell()` 进行买卖操作，支持按权重下单
6. **自动处理**：引擎自动处理 T+1 规则、手续费、滑点等，策略只需关注交易逻辑

---

## 策略视角的工作流

1. **引擎喂给策略一个 `BarContext`**：包含本根 bar 的行情（单标或多标）、账户现金、当前持仓均价、净值等。
2. **策略在 `on_bar` 中写业务逻辑**：直接调用 Helper 下单，或返回一个简单的订单列表。
3. **引擎在同一根 bar 内完成执行**：
   - 先处理卖单释放仓位/现金。
   - 再处理买单，可按手数、金额或目标权重下单。
   - 自动计算滑点、佣金、印花税和成交价（默认使用收盘价）。
   - 所有成交数量自动向下取整到100股的整数倍（A股最小交易单位1手）。
   - 费用计算：
     - 买入：`费用 = max(交易金额 * commission_rate, min_commission)`
     - 卖出：`费用 = max(交易金额 * commission_rate, min_commission) + 交易金额 * stamp_tax_rate`
     - 确保小金额交易也能正确计算手续费（A股通常最低5元佣金）。
4. **引擎更新 `PortfolioState` 并立刻刷新 `ctx`**：让策略在下一根 bar 看到新的仓位与现金。
5. **回测结束后**：引擎生成净值曲线、成交列表、指标统计，策略作者无需自行整理。

---

## 核心组件（策略友好视角）

| 组件 | 简化职责 |
| --- | --- |
| `SimpleDataFeed` | 预载基准 K 线，按时间逐根返回行情快照。 |
| `BarContext` | 每根 bar 的状态快照，包含 `bar`, `position`, `cash`, `equity`, `symbols`, `indicators`。 |
| `OrderHelper` | 提供 `buy` 与 `sell` 两个 API，结合 `quantity_type ∈ {"count","cash","weight"}` 实现按手数/金额/目标权重下单。 |
| `ExecutionEngine` | 同一根 bar 内顺序撮合：卖单 → 买单；转换金额/权重指令，计算滑点与佣金。 |
| `PortfolioState` | 管理现金、持仓、均价、浮亏、成交记录；拒绝超仓位卖出。 |
| `MetricsRecorder` | 累积净值、回撤、成交列表，回测结束后输出结果。 |

Rust 侧仍负责性能关键路径（数据预提取、状态更新、指标计算），但这些细节不再暴露给策略端。

---

## 下单能力

### OrderHelper（买/卖/目标权重模式）

| 方法 | 参数 | 描述 | 示例 |
| --- | --- | --- | --- |
| `ctx.order.buy(symbol=None, quantity=1.0, quantity_type="count")` | `quantity_type` 可取：`count`（手数，默认）、`cash`（投入金额）、`weight`（目标组合权重，0~1，自动换算缺口手数） | 按指定模式买入或增持某标的 | `ctx.order.buy("513500.SH", quantity=50)`；`ctx.order.buy("513500.SH", quantity=20000, quantity_type="cash")`；`ctx.order.buy("513500.SH", quantity=0.3, quantity_type="weight")` |
| `ctx.order.sell(symbol=None, quantity=1.0, quantity_type="count")` | `quantity_type` 语义同上；`quantity` 表示要减仓的手数/金额，或目标权重（0~1） | 按指定模式卖出或减仓 | `ctx.order.sell("513500.SH", quantity=10)`；`ctx.order.sell("513500.SH", quantity=10000, quantity_type="cash")`；`ctx.order.sell("513500.SH", quantity=0.0, quantity_type="weight")` |
| `ctx.order.target(weights, symbol=None)` | `weights` 可以是字典（多标的）或单个浮点数（单标的）；`symbol` 仅在 `weights` 为单个浮点数时使用 | 设置目标权重，引擎自动计算差值并生成买入/卖出订单 | `ctx.order.target({"513500.SH": 0.3, "159941.SZ": 0.2})`；`ctx.order.target(0.5, symbol="513500.SH")` |

- 卖单先执行，买单后执行，确保现金与仓位约束。
- `target` 方法专门用于按目标权重调整组合，相比使用 `buy/sell` 配合 `quantity_type="weight"` 更加清晰直观。
- `quantity_type="weight"` 支持一次传入字典批量调仓：`ctx.order.buy(symbol="513500.SH", quantity={"513500.SH": 0.3, "MSFT": 0.2}, quantity_type="weight")`（需要明确指定 symbol 参数，通常使用字典中的第一个 symbol）。
- 所有模式自动应用滑点、佣金和印花税并进行风险校验。
- **A股交易规则**：
  - 最小交易单位为1手（100股），所有成交数量自动向下取整到100股的整数倍。
  - 买入时：只收取佣金，`费用 = max(交易金额 * commission_rate, min_commission)`
  - 卖出时：收取佣金和印花税，`费用 = max(交易金额 * commission_rate, min_commission) + 交易金额 * stamp_tax_rate`
  - 手续费计算：`commission = max(trade_amount * commission_rate, min_commission)`，确保小金额交易也能正确计算手续费（A股通常最低5元）。
  - 印花税率：默认 0.1%（0.001），仅卖出时收取，可在 `BacktestConfig` 中配置 `stamp_tax_rate`。

---

## 多标的支持

- `BarContext` 中的 `ctx.bars` 是 `dict[symbol] -> BarSnapshot`。
- `ctx.positions` 返回每个标的的 `position`, `avg_cost`, `market_value`。
- `ctx.order` Helper 支持传入 `symbol` 参数，默认使用主标的。
- 支持在 `on_bar` 中一次性做多资产再平衡，不需要手动同步时间线。

---

## 指标计算与使用

### 指标计算方式

指标计算支持两种时机：

1. **预计算（推荐）**：在回测开始前一次性计算所有 bar 的指标值，存储在 Rust 引擎中，**推荐使用**，性能最优。
   - **Rust 计算**：性能最优，适合常用技术指标（如 MA、RSI、MACD 等），由引擎内置或通过 Rust 扩展实现。
   - **Python 计算**：灵活性高，适合自定义复杂指标或需要调用 Python 生态库的场景。
   - **优势**：一次性计算，`on_bar` 中只需读取，零计算开销，性能最优
2. **实时计算**：在 `on_bar` 中实时计算指标值，适合需要动态调整参数或基于当前 bar 和历史行情计算的场景。
   - 通过 `ctx.get_bars(symbol, count=N)` 统一获取 bar 数据：`count=1` 获取当前 bar，`count=N` 获取包含当前 bar 在内的过去 N 根 bar（例如 `ctx.get_bars(symbol, count=20)` 获取当前 bar 和过去19根bar，共20根）
   - 引擎自动确保只能访问当前及历史 bar 的数据，防止未来函数问题
   - **性能说明**：实时计算会在每根 bar 时执行，相比预计算会有性能开销。仅在需要动态调整参数或复杂计算时使用。

### 指标存储与访问

- **预计算指标（推荐）**：计算好的指标与行情数据一起存放在 Rust 引擎中，按时间序列组织，便于高效访问。策略通过 `ctx.get_indicator_value(name, symbol, count=N)` 方法获取指标值：`count=1` 或省略获取当前 bar 的指标值，`count=N` 获取包含当前 bar 在内的过去 N 个指标值。**性能最优，推荐使用**，适合固定参数、高频访问的场景。
- **实时计算指标**：在 `on_bar` 中直接计算，通过 `ctx.get_bars(symbol, count=N)` 统一获取当前 bar 和历史 bar 数据进行计算。**灵活性高**，但每根 bar 都会执行计算，性能低于预计算。仅在需要动态调整参数或复杂计算时使用。
- **防止未来函数**：无论预计算还是实时计算，引擎都自动确保策略只能访问当前及历史 bar 的数据，无法访问未来数据，从根本上防止未来函数问题。

### 使用示例

#### 预计算指标访问

```python
# 在策略中使用预计算的指标
class MyStrategy(Strategy):
    def on_start(self, ctx):
        """在 on_start 中注册指标（推荐方式，有利于时间线对齐）"""
        from rustbroker.indicators import Indicator
        
        # 注册指标：引擎会根据基准时间线自动计算所有 bar 的指标值
        ctx.register_indicator("ma_20", Indicator.sma(period=20, field="close"))
        ctx.register_indicator("rsi_14", Indicator.rsi(period=14, field="close"))
    
    def on_bar(self, ctx):
        for symbol in ctx.symbols:
            # 方式1：获取当前 bar 的指标值
            ma20 = ctx.get_indicator_value("ma_20", symbol)  # 或 ctx.get_indicator_value("ma_20", symbol, count=1)
            rsi = ctx.get_indicator_value("rsi_14", symbol)
            
            # 方式2：获取过去指定数量的指标值（例如过去20个指标值）
            ma20_history = ctx.get_indicator_value("ma_20", symbol, count=20)  # 返回包含当前 bar 在内的过去20个指标值的列表
            
            # 引擎自动确保只能访问当前及历史 bar 的数据，防止未来函数
            # 获取当前 bar：可以使用 ctx.get_bars(symbol, count=1)
            current_bar = ctx.get_bars(symbol, count=1)[0]
            if ma20 > current_bar["close"]:
                # 做多逻辑
                pass
            
            # 使用历史指标值进行分析（例如计算指标的变化趋势）
            if len(ma20_history) >= 20:
                # 当前指标值是列表的最后一个元素
                current_ma20 = ma20_history[-1]
                # 可以分析指标的变化趋势
                ma20_trend = ma20_history[-1] - ma20_history[0]
```

#### 实时计算指标

```python
# 在策略中实时计算指标（仅在需要动态调整时使用）
def on_bar(self, ctx):
    for symbol in ctx.symbols:
        # 使用 ctx.get_bars(symbol, count=N) 统一获取 bar 数据
        # count=1 获取当前 bar，count=20 获取包含当前 bar 在内的过去20根bar
        bars = ctx.get_bars(symbol, count=20)  # 返回包含当前 bar 在内的过去20根bar的列表
        
        if len(bars) < 20:
            continue  # 数据不足，跳过
        
        # 当前 bar 是列表的最后一个元素（最新的）
        current_bar = bars[-1]
        current_close = current_bar["close"]
        
        # 基于历史数据实时计算指标
        # 例如：计算简单移动平均（MA20）
        closes = [bar["close"] for bar in bars]
        ma20 = sum(closes) / len(closes)
        
        # 或者使用预计算的指标
        # ma20 = ctx.get_indicator_value("ma_20", symbol)
        
        # 使用计算好的指标值进行交易决策
        if current_close > ma20:
            ctx.order.buy(symbol=symbol, quantity=100, quantity_type="count")
```

**实时计算 vs 预计算的对比**：

| 特性 | 预计算 | 实时计算 |
|------|--------|----------|
| **性能** | ⭐⭐⭐⭐⭐ 最优，一次性计算，访问时零开销 | ⭐⭐⭐ 每根 bar 执行计算，有一定开销 |
| **灵活性** | ⭐⭐ 固定参数，难以动态调整 | ⭐⭐⭐⭐⭐ 可动态调整参数，适应市场变化 |
| **适用场景** | 固定参数的常用指标（MA、RSI、MACD等） | 需要动态调整参数或复杂计算的指标 |
| **代码维护** | ⭐⭐⭐ 指标定义与策略分离 | ⭐⭐⭐⭐⭐ 指标计算与策略逻辑在一起，便于理解 |

**选择建议（推荐预计算）**：

- **优先推荐**：使用预计算（例如 MA20、RSI14、MACD 等标准指标），性能最优
- **特殊场景**：仅在需要动态调整参数或复杂计算时使用实时计算（例如自适应均线、动态阈值等）

### 指标计算流程

#### 预计算流程

1. **指标注册阶段（在 on_start 中）**：策略在 `on_start` 方法中注册需要计算的指标。**推荐在 `on_start` 中注册指标，这样有利于时间线对齐**，确保指标计算基于基准时间线，在每根 bar 执行时指标值都能正确对齐到当前时间点。
2. **指标计算阶段**：引擎根据在 `on_start` 中注册的指标列表，使用 Rust 或 Python 一次性计算所有 bar 的指标值。指标计算基于基准时间线，确保所有标的的数据都对齐到基准时间线上。
3. **存储阶段**：计算好的指标按 `指标名 -> 标的 -> 时间序列` 的结构存储在 Rust 引擎中。
4. **访问阶段**：在 `on_bar` 中，引擎根据当前 bar 的时间戳，自动从存储中提取对应的指标值提供给策略。

#### 实时计算流程

1. **数据访问**：
   - 统一使用 `ctx.get_bars(symbol, count=N)` 获取 bar 数据：
     - `count=1` 或 `ctx.get_bars(symbol)` 获取当前 bar（包含 open、high、low、close、volume 等字段）
     - `count=N` 获取包含当前 bar 在内的过去 N 根 bar（例如 `ctx.get_bars(symbol, count=20)` 返回当前 bar 和过去19根bar，共20根bar的列表）
   - 引擎自动确保只能访问当前及历史 bar 的数据，防止未来函数问题

2. **指标计算**：基于当前 bar 和历史 bar 数据实时计算指标值。
   - 例如：使用过去20根bar的收盘价计算简单移动平均（MA20）
   - 例如：使用过去14根bar计算相对强弱指标（RSI14）

3. **使用指标**：计算好的指标值直接用于交易决策。
   - 例如：`if current_close > ma20: ctx.order.buy(...)`

**性能对比**：

- **预计算**：指标在初始化时一次性计算完成，`on_bar` 中只需读取，性能最优（O(1) 访问）
- **实时计算**：每根 bar 都需要执行计算，性能取决于计算复杂度。对于简单指标（如 MA），性能影响通常可接受（<1ms/bar）；对于复杂指标或大量标的，建议优先考虑预计算

**选择原则（推荐预计算）**：**优先使用预计算**以获得最佳性能。固定参数的常用指标（如 MA、RSI、MACD 等）应使用预计算；仅在需要动态调整参数或复杂计算的特殊场景下使用实时计算。

---

## 性能保持简单同时高效

- 行情批量预提取+顺序遍历，保证缓存友好。
- Rust 内核维护单线程主循环，避免锁；统计分析可在回测结束后用 Rayon 并行。
- Python 与 Rust 仅在每根 bar 交换一次 `BarContext`；上下文对象复用以减少分配。
- 数量换算（金额/权重）在 Rust 内部完成，避免策略端重复计算。
- 指标支持预计算和实时计算两种方式。预计算：策略在 `on_start` 中注册指标后，引擎统一计算（支持 Rust 或 Python 计算），计算好的指标与行情数据一起存放在 Rust 引擎中，回测循环中通过 `ctx.get_indicator_value()` 直接读取。**推荐在 `on_start` 中注册指标，这样有利于时间线对齐**。实时计算：在 `on_bar` 中基于当前 bar 和历史行情数据实时计算，更灵活且便于调试。引擎自动确保只能访问当前及历史 bar 的数据，防止未来函数问题。

---

## 扩展能力（可选）

- **高级撮合**：可在配置中切换 `execution_mode = "close" | "open" | "vwap"`，默认仍是收盘价。
- **风险规则**：默认启用“仓位必须≥卖出量”；可选启用“最大仓位/资金占比”等。
- **交割周期配置**：通过引擎初始化参数 `t0_symbols={"510300","159915"}` 等指定 T+0 ETF，其他标的仍按默认 T+1；撮合逻辑会基于此决定当日是否允许卖出。
- **自定义指标注入**：策略应在 `on_start` 方法中注册指标，这样有利于时间线对齐。指标可以使用 Rust 或 Python 计算。计算好的指标与行情数据一起存放在 Rust 引擎中，策略通过 `ctx.get_indicator_value(name, symbol, count=N)` 方便地获取指标值：`count=1` 或省略获取当前 bar 的指标值，`count=N` 获取包含当前 bar 在内的过去 N 个指标值，引擎自动防止未来函数问题。
- **事件钩子**：提供 `on_start`、`on_bar`、`on_trade`、`on_stop` 等回调，策略可按需实现。

---

## 策略接口概览

```python
class Strategy:
    def on_start(self, ctx): ...
    def on_bar(self, ctx): ...
    def on_trade(self, fill, ctx): ...
    def on_stop(self, ctx): ...
```

`ctx` 结构：

- `ctx.datetime`（当前 bar 的时间戳）
- `ctx.get_bars(symbol, count=N)`（统一获取 bar 数据的方法：`count=1` 获取当前 bar，`count=N` 获取包含当前 bar 在内的过去 N 根 bar）
- `ctx.period`（当前数据频率，例 "1d" / "1h"）
- `ctx.symbols`（股票池，所有标的代码列表）
- `ctx.cash`（可用现金）
- `ctx.equity`（总资产 = 现金 + 股票市值）
- `ctx.position` / `ctx.positions[symbol]`，包含以下字段：
  - `position`（持仓数量，单位：手）
  - `available`（可用数量，T+1 规则下当日买入不可卖出，T+0 标的等于持仓数量）
  - `avg_cost`（平均成本价）
  - `market_value`（市值 = 持仓数量 × 当前价格）
  - `weight`（权重 = 市值 / 总资产）
- `ctx.benchmark`（当前基准净值、收益等）
- `ctx.state`（策略持久化字典，生命周期内共享）
- `ctx.calendar`（交易日工具，支持 `is_rebalance_day` 等）
- `ctx.order`（下单 Helper）
- `ctx.get_indicator_value(name, symbol, count=N)`（获取预计算的指标值：`count=1` 或省略获取当前 bar 的指标值，`count=N` 获取包含当前 bar 在内的过去 N 个指标值，引擎自动防止未来函数问题）
- `ctx.factors`（若初始化传入因子数据）
- `ctx.is_tradable(symbol)`（辅助过滤停牌/不可交易标的）
