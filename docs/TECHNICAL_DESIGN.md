# 回测引擎技术设计文档

## 1. 设计目标

### 1.1 核心原则
- **简单性**：策略作者只需关注交易逻辑，无需管理事件循环、仓位状态、撮合细节
- **高效性**：Rust 内核处理性能关键路径，Python 层仅负责策略逻辑
- **直观性**：API 设计贴近交易直觉，降低学习成本
- **安全性**：默认禁止裸卖空，自动校验仓位和资金约束

### 1.2 性能目标
- 单线程主循环，避免锁竞争
- 批量预加载数据，保证缓存友好
- 指标预计算，回测循环中零计算开销
- Python-Rust 交互最小化，每根 bar 仅交换一次上下文

---

## 2. 整体架构

### 2.1 分层架构

```
┌─────────────────────────────────────────┐
│         Python 策略层                   │
│  - Strategy 接口                        │
│  - BarContext 上下文                    │
│  - OrderHelper 下单助手                 │
└─────────────────┬───────────────────────┘
                  │ PyO3 绑定
┌─────────────────▼───────────────────────┐
│         Rust 引擎内核                    │
│  ┌──────────────────────────────────┐  │
│  │ DataFeed: 数据管理               │  │
│  │  - 行情数据存储                  │  │
│  │  - 基准时间线管理                │  │
│  │  - 数据对齐                      │  │
│  └──────────────────────────────────┘  │
│  ┌──────────────────────────────────┐  │
│  │ IndicatorEngine: 指标引擎        │  │
│  │  - 指标注册与预计算              │  │
│  │  - 指标值存储与访问              │  │
│  └──────────────────────────────────┘  │
│  ┌──────────────────────────────────┐  │
│  │ ExecutionEngine: 撮合引擎        │  │
│  │  - 订单收集与排序                │  │
│  │  - 卖单优先执行                  │  │
│  │  - 滑点与费用计算                │  │
│  └──────────────────────────────────┘  │
│  ┌──────────────────────────────────┐  │
│  │ PortfolioState: 组合状态         │  │
│  │  - 现金管理                      │  │
│  │  - 持仓管理                      │  │
│  │  - T+1/T+0 规则                 │  │
│  └──────────────────────────────────┘  │
│  ┌──────────────────────────────────┐  │
│  │ MetricsRecorder: 统计记录器      │  │
│  │  - 净值曲线                      │  │
│  │  - 成交记录                      │  │
│  │  - 性能指标                      │  │
│  └──────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

### 2.2 数据流

```
初始化阶段:
  行情数据 → DataFeed (存储、对齐)
  指标注册 → IndicatorEngine (预计算)
  
回测循环:
  BarContext (当前状态) → Strategy.on_bar()
  OrderHelper (下单指令) → ExecutionEngine
  ExecutionEngine → PortfolioState (更新状态)
  PortfolioState → BarContext (下一根 bar 状态)
  
结束阶段:
  MetricsRecorder → 统计结果
```

---

## 3. 核心组件设计

### 3.1 DataFeed（数据管理器）

#### 职责
- 存储所有标的的行情数据（按时间排序）
- 管理基准时间线（benchmark timeline）
- 提供当前 bar 的行情快照
- 确保多标的数据对齐到基准时间线

#### 数据结构
```
DataFeed {
    benchmark_timeline: Vec<DateTime>,      // 基准时间线
    market_data: HashMap<Symbol, Vec<Bar>>, // 标的 -> 行情序列
    current_index: usize,                    // 当前 bar 索引
}
```

#### 关键方法
- `add_market_data(symbol, bars)`: 添加标的行情数据
- `set_benchmark(benchmark_name, bars)`: 设置基准时间线
- `get_current_bars() -> HashMap<Symbol, Bar>`: 获取当前时间点所有标的的 bar
- `get_bars(symbol, count) -> Vec<Bar>`: 获取指定标的的历史 bar（防止未来函数）
- `next()`: 推进到下一根 bar
- `has_next() -> bool`: 是否还有下一根 bar

#### 设计要点
- 所有标的数据对齐到基准时间线，缺失数据标记为不可交易
- 使用 `Vec` 存储，保证顺序访问的缓存友好性
- 时间戳作为主键，支持快速查找

---

### 3.2 IndicatorEngine（指标引擎）

#### 职责
- 接收指标注册请求（在 `on_start` 中）
- 预计算所有 bar 的指标值
- 存储指标值（按时间序列组织）
- 提供指标值访问接口（防止未来函数）

#### 数据结构
```
IndicatorEngine {
    indicators: HashMap<IndicatorName, IndicatorDef>,  // 指标定义
    indicator_values: HashMap<(IndicatorName, Symbol), Vec<Value>>, // 指标值存储
    current_index: usize,                               // 当前 bar 索引
}
```

#### 指标定义类型
```
IndicatorDef {
    indicator_type: RustBuiltin | PythonFunction,
    params: HashMap<String, Value>,
    lookback_period: usize,  // 回看周期
}
```

#### 关键方法
- `register_indicator(name, def)`: 注册指标（在 on_start 中调用）
- `compute_all_indicators()`: 一次性计算所有指标的所有 bar 值
- `get_indicator_value(name, symbol, count) -> Value | Vec<Value>`: 获取指标值
  - `count=1` 或省略：返回当前 bar 的指标值
  - `count=N`：返回包含当前 bar 在内的过去 N 个指标值
- `update_index(index)`: 更新当前 bar 索引（由主循环调用）

#### 设计要点
- **预计算策略**：在回测开始前一次性计算所有指标值，避免在回测循环中重复计算
- **时间对齐**：指标计算基于基准时间线，确保与行情数据对齐
- **防止未来函数**：`get_indicator_value` 只能访问当前及历史 bar 的指标值
- **存储优化**：使用 `Vec` 按时间顺序存储，O(1) 访问当前值，O(N) 访问历史值

#### 指标计算流程
1. **注册阶段**（on_start）：策略注册指标定义
2. **计算阶段**（回测开始前）：
   - 遍历基准时间线的所有 bar
   - 对每个 bar，计算所有已注册指标的值
   - 存储到 `indicator_values` 中
3. **访问阶段**（on_bar）：根据当前 bar 索引，从存储中读取指标值

---

### 3.3 ExecutionEngine（撮合引擎）

#### 职责
- 收集策略在 `on_bar` 中产生的订单
- 按规则排序订单（卖单优先）
- 执行撮合：计算成交价、滑点、费用
- 更新 PortfolioState

#### 订单类型
```
Order {
    symbol: Symbol,
    side: Buy | Sell,
    quantity_type: Count | Cash | Weight,
    quantity: f64,
    timestamp: DateTime,
}
```

#### 关键方法
- `add_order(order)`: 添加订单（由 OrderHelper 调用）
- `execute_all_orders() -> Vec<Fill>`: 执行所有订单
  - 先执行所有卖单（释放现金和仓位）
  - 再执行所有买单（消耗现金，增加仓位）
- `calculate_fill_price(order, bar) -> f64`: 计算成交价（考虑滑点）
- `calculate_commission(trade_amount, side) -> f64`: 计算手续费
- `round_to_lot(quantity) -> f64`: 向下取整到 1 手（100股）

#### 撮合规则
1. **执行顺序**：卖单 → 买单（确保有足够现金买入）
2. **成交价计算**：
   - 默认使用收盘价
   - 应用滑点：`fill_price = base_price * (1 ± slippage_bps / 10000)`
3. **数量处理**：
   - 按手数下单：直接使用
   - 按金额下单：`quantity = cash / price`，向下取整到 1 手
   - 按权重下单：`quantity = (target_weight * equity - current_market_value) / price`，向下取整到 1 手
4. **费用计算**：
   - 买入：`commission = max(trade_amount * commission_rate, min_commission)`
   - 卖出：`commission = max(trade_amount * commission_rate, min_commission) + trade_amount * stamp_tax_rate`
5. **A股规则**：
   - 最小交易单位：1 手（100股），所有数量向下取整
   - 禁止裸卖空：卖出数量不能超过可用持仓

---

### 3.4 PortfolioState（组合状态）

#### 职责
- 管理现金余额
- 管理持仓（数量、成本价、市值）
- 管理 T+1/T+0 规则（可用数量）
- 记录成交历史

#### 数据结构
```
PortfolioState {
    cash: f64,                                    // 可用现金
    positions: HashMap<Symbol, Position>,         // 持仓信息
    buy_records: HashMap<Symbol, Vec<BuyRecord>>, // 买入记录（用于 T+1）
    fills: Vec<Fill>,                            // 成交记录
}

Position {
    symbol: Symbol,
    quantity: f64,        // 持仓数量（单位：手）
    avg_cost: f64,       // 平均成本价
    market_value: f64,   // 市值
    available: f64,      // 可用数量（考虑 T+1）
}

BuyRecord {
    date: Date,
    quantity: f64,
    price: f64,
}

Fill {
    symbol: Symbol,
    side: Buy | Sell,
    quantity: f64,
    price: f64,
    commission: f64,
    timestamp: DateTime,
}
```

#### 关键方法
- `update_cash(amount)`: 更新现金（买入减少，卖出增加）
- `add_position(symbol, quantity, price)`: 增加持仓
- `reduce_position(symbol, quantity, price) -> f64`: 减少持仓，返回释放的现金
- `get_available(symbol) -> f64`: 获取可用数量（考虑 T+1）
- `update_t1_availability(current_date)`: 更新 T+1 可用数量
- `calculate_equity(current_prices) -> f64`: 计算总资产（现金 + 持仓市值）
- `add_fill(fill)`: 记录成交

#### T+1/T+0 规则
- **T+1 标的**（默认）：
  - 当日买入的股票，当日不可卖出
  - `available = position - 当日买入数量`
- **T+0 标的**（可配置）：
  - 当日买入的股票，当日可以卖出
  - `available = position`
- **实现方式**：
  - 维护 `buy_records`，记录每日买入数量
  - 每日更新时，将 T+1 标的的 `available` 减去当日买入数量
  - T+0 标的的 `available` 始终等于 `position`

---

### 3.5 MetricsRecorder（统计记录器）

#### 职责
- 记录每根 bar 的净值
- 记录所有成交记录
- 计算性能指标（回撤、夏普比率等）

#### 数据结构
```
MetricsRecorder {
    equity_curve: Vec<(DateTime, f64)>,  // 净值曲线
    fills: Vec<Fill>,                    // 成交记录
    benchmark_curve: Vec<(DateTime, f64)>, // 基准净值曲线
}
```

#### 关键方法
- `record_equity(datetime, equity)`: 记录净值
- `record_fill(fill)`: 记录成交
- `calculate_stats() -> Stats`: 计算统计指标
  - 总收益、年化收益
  - 最大回撤
  - 最大回撤时间段
  - 夏普比率
  - 胜率、盈亏比等

---

### 3.6 BarContext（上下文对象）

#### 职责
- 封装当前 bar 的状态信息
- 提供策略访问接口
- 提供下单接口（OrderHelper）

#### 数据结构
```
BarContext {
    // 时间信息
    datetime: DateTime,
    period: String,  // "1d", "1h" 等
    
    // 行情数据访问
    symbols: Vec<Symbol>,
    get_bars: fn(symbol, count) -> Vec<Bar>,
    
    // 账户信息
    cash: f64,
    equity: f64,
    positions: HashMap<Symbol, PositionInfo>,
    
    // 指标访问
    get_indicator_value: fn(name, symbol, count) -> Value | Vec<Value>,
    
    // 下单接口
    order: OrderHelper,
    
    // 其他
    benchmark: BenchmarkInfo,
    state: HashMap<String, Value>,  // 策略持久化状态
    calendar: Calendar,
    is_tradable: fn(symbol) -> bool,
}
```

#### 设计要点
- **对象复用**：在整个回测过程中复用同一个 `BarContext` 对象，仅更新其内容，减少内存分配
- **延迟计算**：`equity`、`positions` 等字段在访问时计算，避免不必要的计算
- **只读访问**：策略只能通过 `order` 接口下单，不能直接修改 `cash`、`positions` 等状态

---

### 3.7 OrderHelper（下单助手）

#### 职责
- 提供直观的下单 API
- 收集订单，传递给 ExecutionEngine

#### 关键方法
- `buy(symbol, quantity, quantity_type)`: 买入
- `sell(symbol, quantity, quantity_type)`: 卖出
- `target(weights, symbol)`: 设置目标权重

#### 设计要点
- **参数转换**：将 `quantity_type` 为 `cash` 或 `weight` 的订单转换为手数，由 ExecutionEngine 处理
- **订单收集**：在 `on_bar` 中收集所有订单，在 bar 结束时统一执行

---

## 4. 执行流程设计

### 4.1 初始化流程

```
1. 创建 BacktestEngine，传入 BacktestConfig
2. 添加行情数据：engine.add_market_data(data)
3. 设置基准时间线：engine.set_benchmark(benchmark)
4. 创建策略实例：strategy = MyStrategy()
5. 调用 strategy.on_start(ctx)：
   - 策略注册指标：ctx.register_indicator(...)
   - 引擎记录指标定义
6. 引擎预计算所有指标：
   - IndicatorEngine.compute_all_indicators()
   - 遍历基准时间线的所有 bar
   - 对每个 bar，计算所有已注册指标的值
   - 存储到 indicator_values
7. 初始化 PortfolioState：设置初始现金
8. 初始化 MetricsRecorder
```

### 4.2 回测主循环

```
for each bar in benchmark_timeline:
    // 1. 准备 BarContext
    current_bars = DataFeed.get_current_bars()
    positions = PortfolioState.get_all_positions()
    cash = PortfolioState.get_cash()
    equity = PortfolioState.calculate_equity(current_prices)
    
    ctx = BarContext {
        datetime: bar.datetime,
        symbols: current_bars.keys(),
        cash: cash,
        equity: equity,
        positions: positions,
        ...
    }
    
    // 2. 更新指标引擎的当前索引
    IndicatorEngine.update_index(current_index)
    
    // 3. 调用策略
    strategy.on_bar(ctx)
    // 策略通过 ctx.order.buy/sell() 添加订单到 ExecutionEngine
    
    // 4. 执行订单
    fills = ExecutionEngine.execute_all_orders()
    // 执行顺序：卖单 → 买单
    // 更新 PortfolioState
    
    // 5. 记录统计
    MetricsRecorder.record_equity(ctx.datetime, ctx.equity)
    MetricsRecorder.record_fills(fills)
    
    // 6. 触发成交回调
    for fill in fills:
        strategy.on_trade(fill, ctx)
    
    // 7. 推进到下一根 bar
    DataFeed.next()
    PortfolioState.update_t1_availability(current_date)
    
    current_index += 1
```

### 4.3 结束流程

```
1. 调用 strategy.on_stop(ctx)
2. 计算最终统计指标：MetricsRecorder.calculate_stats()
3. 返回回测结果：
   {
       "stats": {...},
       "equity_curve": [...],
       "fills": [...],
       ...
   }
```

---

## 5. 性能优化设计

### 5.1 数据访问优化

- **批量预加载**：所有行情数据在初始化时一次性加载到内存
- **顺序访问**：使用 `Vec` 存储，保证顺序遍历的缓存友好性
- **数据对齐**：多标的数据对齐到基准时间线，避免运行时查找

### 5.2 指标计算优化

- **预计算策略**：所有指标在回测开始前一次性计算完成
- **存储优化**：指标值按时间序列存储，O(1) 访问当前值
- **Rust 优先**：常用指标（MA、RSI 等）使用 Rust 实现，性能最优

### 5.3 Python-Rust 交互优化

- **最小化交互**：每根 bar 仅交换一次 `BarContext` 对象
- **对象复用**：`BarContext` 对象在整个回测过程中复用，仅更新内容
- **批量传递**：订单批量收集，一次性传递给 Rust 执行

### 5.4 内存管理

- **预分配**：关键数据结构（净值曲线、成交记录）预分配容量
- **避免拷贝**：使用引用传递，避免不必要的数据拷贝
- **及时释放**：不再使用的数据及时释放内存

---

## 6. 扩展设计

### 6.1 高级撮合模式

- **执行模式**：`execution_mode = "close" | "open" | "vwap"`
  - `close`：使用收盘价（默认）
  - `open`：使用开盘价
  - `vwap`：使用成交量加权平均价

### 6.2 风险规则

- **默认规则**：
  - 禁止裸卖空：卖出数量不能超过可用持仓
  - 资金检查：买入金额不能超过可用现金
- **可选规则**：
  - 最大仓位限制：单个标的的最大持仓比例
  - 最大资金占比：单个标的的最大资金占比

### 6.3 交易日规则

- **T+1/T+0 配置**：通过 `t0_symbols` 参数指定 T+0 标的
- **交易日历**：支持交易日历，过滤非交易日

### 6.4 自定义指标

- **Rust 指标**：通过 Rust 扩展实现，性能最优
- **Python 指标**：通过 Python 函数实现，灵活性高
- **指标链**：支持指标之间的依赖关系

---

## 7. 错误处理

### 7.1 数据错误

- **缺失数据**：标记为不可交易，跳过该 bar
- **数据异常**：记录警告，使用前值或跳过

### 7.2 订单错误

- **资金不足**：拒绝订单，记录警告
- **仓位不足**：拒绝订单，记录警告
- **无效参数**：拒绝订单，返回错误信息

### 7.3 指标错误

- **数据不足**：返回 `None`，策略自行处理
- **计算错误**：记录错误，返回 `None`

---

## 8. 总结

### 8.1 设计亮点

1. **简单性**：策略作者只需实现 `on_bar`，其他由引擎处理
2. **高效性**：Rust 内核 + 预计算指标，性能最优
3. **直观性**：API 设计贴近交易直觉，降低学习成本
4. **安全性**：默认禁止裸卖空，自动校验约束

### 8.2 关键设计决策

1. **预计算指标**：牺牲少量灵活性，换取最佳性能
2. **卖单优先执行**：确保有足够现金买入
3. **对象复用**：减少内存分配，提升性能
4. **基准时间线对齐**：简化多标的数据管理

### 8.3 实现优先级

1. **Phase 1**：核心功能
   - DataFeed、PortfolioState、ExecutionEngine
   - 基本的 buy/sell API
   - T+1 规则
   
2. **Phase 2**：指标系统
   - IndicatorEngine
   - 预计算指标
   - 常用指标（MA、RSI 等）
   
3. **Phase 3**：高级功能
   - 权重下单（target API）
   - 高级撮合模式
   - 风险规则扩展

