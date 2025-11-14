"""
双均线策略示例
快速均线在慢均线上面时全仓买入，否则空仓
"""

import os
import sys
from pathlib import Path
from datetime import datetime
# 添加项目路径到 sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "python"))

from rustbroker.api import BacktestEngine, BacktestConfig
from rustbroker.strategy import Strategy
from rustbroker.indicators import Indicator
from rustbroker.data import load_csv_to_bars


class DoubleMAStrategy(Strategy):
    """双均线策略：快速均线在慢均线上面时全仓买入，否则空仓"""
    
    def on_start(self, ctx):
        """策略启动时注册和计算指标（有利于时间线对齐）"""
        # 在 on_start 中注册指标，确保指标计算基于基准时间线
        # 这样在每根 bar 执行时，指标值都能正确对齐到当前时间点
        
        # 尝试使用预计算指标（如果 Rust 引擎支持）
        
        ctx.register_indicator("sma_5", Indicator.sma(period=50, field="close"))   # 5日均线
        ctx.register_indicator("sma_20", Indicator.sma(period=200, field="close")) # 20日均线
        print("register sma_5 and sma_20")

    
    def _calculate_sma(self, bars, period):
        """计算简单移动平均"""
        if len(bars) < period:
            return None
        closes = [bar["close"] for bar in bars[-period:]]
        return sum(closes) / len(closes)
    
    def on_bar(self, ctx):
        """每根 bar 执行一次"""
        for symbol in ctx.symbols:
            # 尝试获取预计算的指标值
            sma_short = ctx.get_indicator_value("sma_5", symbol)
            sma_long = ctx.get_indicator_value("sma_20", symbol)
            # 如果预计算指标不可用，使用实时计算
            if sma_short is None or sma_long is None:
                # 获取历史 bar 数据用于计算指标
                bars_long = ctx.get_bars(symbol, count=20)
                
                if len(bars_long) < 20:
                    continue
                
                # 从20根bar中计算5日均线和20日均线
                # sma_short = self._calculate_sma(bars_long, 5)   # 从20根bar中取最后5根
                # sma_long = self._calculate_sma(bars_long, 20)  # 使用全部20根bar
                
                if sma_short is None or sma_long is None:
                    continue

            # 获取当前 bar 数据
            bars = ctx.get_bars(symbol, count=1)
            if not bars:
                continue
            
            current_bar = bars[0]
            close = current_bar["close"]
            
            # 获取当前持仓信息
            pos_info = ctx.positions.get(symbol, {})
            position = pos_info.get("position", 0.0)      # 持仓数量（单位：手）
            available = pos_info.get("available", 0.0)    # 可用数量（考虑 T+1 规则）
            has_position = position > 0                   # 是否有持仓
            # 双均线策略逻辑：快速均线在慢均线上面全仓买入，否则空仓
            if close > sma_short > sma_long:
                # 快速均线在慢均线上面：全仓买入
                if not has_position and ctx.cash > 0:
                    # 当前没有持仓且有现金，执行买入
                    ctx.order.buy(symbol=symbol, quantity=1.0, quantity_type="weight")
                    
            else:
                # 否则：空仓（卖出所有持仓）
                if has_position and available > 0:
                    # 当前有持仓，执行卖出
                    ctx.order.sell(symbol=symbol, quantity=available, quantity_type="count")
                
    
    def on_trade(self, fill, ctx):
        """订单成交回调"""
        # side_str = "买入" if fill['side'] == 'buy' else "卖出"
        # quantity_shares = fill.get('filled_quantity', 0) * 100  # 转换为股数
        
        # # 格式化时间
        # timestamp_str = fill.get('timestamp', '')
     
        # # 解析 RFC3339 格式的时间字符串
        # dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        # # 转换为本地时间格式（去掉时区信息）
        # time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
       
        # print(f"{time_str} 成交: {side_str} {fill['symbol']} {quantity_shares:.0f}股 @ {fill.get('price', 0):.4f}元 (手续费: {fill.get('commission', 0):.2f}元)")
    
    def on_stop(self, ctx):
        """回测结束回调"""
        print(f"回测结束，最终净值: {ctx.equity:.2f}")


def main():
    """配置和运行回测"""
    # 配置回测参数
    cfg = BacktestConfig(
        start="2016-01-01",
        end="2025-12-31",
        cash=100000.0,              # 初始资金10万
        commission_rate=0.0005,     # 佣金费率 0.05%
        min_commission=0.0,         # 最小手续费5元（A股标准）
        slippage_bps=1.0,           # 滑点 1 bps
        stamp_tax_rate=0.001,       # 印花税率 0.1%（卖出时收取）
    )
    
    # 创建回测引擎
    engine = BacktestEngine(cfg)
    
    # 1. 加载行情数据（基准时间线）
    # 基准时间线：策略执行的时间基准，所有计算都基于这个时间线
    data_path = os.path.join(os.path.dirname(__file__), "data", "sh600000_min.csv")
    symbol = "600000.SH"
    benchmark_bars = load_csv_to_bars(data_path, symbol=symbol)  # 基准时间线 = 600000.SH 的行情数据
    
    if not benchmark_bars:
        print(f"错误：无法加载数据文件 {data_path}")
        return
    
    print(f"加载了 {len(benchmark_bars)} 根 K 线数据")
    print(f"数据范围: {benchmark_bars[0]['datetime']} 到 {benchmark_bars[-1]['datetime']}")
    
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
    print("\n开始回测...")
    result = engine.run(
        strategy, 
        data=market_data,                    # 添加行情数据
        benchmark=benchmark_data             # 指定基准时间线
    )
    
    # 查看回测结果
    stats = result.get("stats", {})
    print("\n=== 回测结果 ===")
    print(f"总收益: {stats.get('total_return', 0):.2%}")
    print(f"年化收益: {stats.get('annualized_return', 0):.2%}")
    print(f"最大回撤: {stats.get('max_drawdown', 0):.4f}")
    
    # 打印最大回撤时间段
    max_dd_start = stats.get('max_drawdown_start')
    max_dd_end = stats.get('max_drawdown_end')
    if max_dd_start and max_dd_end:
        try:
            start_dt = datetime.fromisoformat(max_dd_start.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(max_dd_end.replace('Z', '+00:00'))
            print(f"最大回撤时间段: {start_dt.strftime('%Y-%m-%d %H:%M:%S')} 至 {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except:
            print(f"最大回撤时间段: {max_dd_start} 至 {max_dd_end}")

    print(f"夏普比率: {stats.get('sharpe_ratio', 0):.4f}")
    print(f"胜率: {stats.get('win_rate', 0):.2%}")
    print(f"盈亏比: {stats.get('profit_loss_ratio', 0):.4f}")
    print(f"开仓次数: {stats.get('open_count', 0)}")
    print(f"平仓次数: {stats.get('close_count', 0)}")
    
    # 打印基准信息（从引擎返回的统计中获取）
    print("\n=== 基准信息 ===")
    benchmark_return = stats.get('benchmark_return')
    benchmark_annualized_return = stats.get('benchmark_annualized_return')
    benchmark_max_dd = stats.get('benchmark_max_drawdown')
    benchmark_max_dd_start = stats.get('benchmark_max_drawdown_start')
    benchmark_max_dd_end = stats.get('benchmark_max_drawdown_end')
    
    if benchmark_return is not None:
        print(f"基准总收益: {benchmark_return:.2%}")
    if benchmark_annualized_return is not None:
        print(f"基准年化收益: {benchmark_annualized_return:.2%}")
    if benchmark_max_dd is not None:
        print(f"基准最大回撤: {benchmark_max_dd:.4f}")
    if benchmark_max_dd_start and benchmark_max_dd_end:
        try:
            start_dt = datetime.fromisoformat(benchmark_max_dd_start.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(benchmark_max_dd_end.replace('Z', '+00:00'))
            print(f"基准最大回撤时间段: {start_dt.strftime('%Y-%m-%d %H:%M:%S')} 至 {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except:
            print(f"基准最大回撤时间段: {benchmark_max_dd_start} 至 {benchmark_max_dd_end}")
    
    # 如果基准信息存在，显示对比
    if benchmark_return is not None:
        print("\n=== 策略 vs 基准 ===")
        strategy_return = stats.get('total_return', 0)
        excess_return = strategy_return - benchmark_return
        print(f"超额收益: {excess_return:.2%}")
        if benchmark_return != 0:
            print(f"策略/基准收益比: {strategy_return / benchmark_return:.4f}")


if __name__ == "__main__":
    main()

