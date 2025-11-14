from __future__ import annotations
from typing import Any, Dict, List

import os
import sys

# Allow running from repo root
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "python"))

from rustbroker.api import BacktestEngine, BacktestConfig
from rustbroker.strategy import Strategy
from rustbroker.market_data import (  # type: ignore[attr-defined]
    DataRequest,
    MarketDataConfig,
    MarketDataService,
)

class MultiAssetRebalanceStrategy(Strategy):
    """
    简单的多资产等权策略：每个月首个交易日进行再平衡。
    """

    def __init__(self, symbols: List[str]) -> None:
        self.symbols = symbols
        self.target_weight = 1.0 / len(symbols) if symbols else 0.0

    def on_start(self, ctx: Any) -> None:
        print("多资产配置再平衡策略初始化完成")
        print(f"目标资产: {self.symbols}")
        print(f"等权权重: {self.target_weight:.2%}")
        ctx.state["last_rebalance"] = None

    def on_bar(self, ctx: Any) -> None:
        if not self.symbols:
            return
        current_dt = ctx.datetime
        if current_dt is None:
            return

        last = ctx.state.get("last_rebalance")
        is_rebalance = ctx.calendar.is_rebalance_day("monthly", current_dt, last)
        if not is_rebalance:
            return
        
        # 调试信息：显示再平衡判断
        if last:
            print(f"  上次再平衡: {last}, 本次日期: {current_dt}, 触发再平衡: {is_rebalance}")
        else:
            print(f"  首次再平衡: {current_dt}")

        equity = float(ctx.equity)
        if equity <= 0:
            return

        print(f"\n[{current_dt}] 执行再平衡，当前权益: {equity:,.2f}")

        # 构建目标权重：所有目标资产设置为等权权重
        target_weights: Dict[str, float] = {sym: self.target_weight for sym in self.symbols}
        
        # 归一化权重（确保总和为1）
        total = sum(target_weights.values())
        if total <= 0:
            print(f"  警告: 目标权重总和为0，跳过再平衡")
            return
        target_weights = {k: v / total for k, v in target_weights.items()}
        
        # 对于不在目标资产列表中的持仓，设置目标权重为0（触发卖出）
        for symbol in ctx.positions.keys():
            if symbol not in target_weights:
                target_weights[symbol] = 0.0
        
        print(f"  目标权重: {target_weights}")
        
        # 显示当前持仓情况
        for symbol, target in target_weights.items():
            pos = ctx.positions.get(symbol, {})
            current = float(pos.get("weight", 0.0)) if pos else 0.0
            if abs(target - current) > 1e-6:
                action = "卖出" if target < current else "买入"
                diff = abs(target - current)
                print(f"  {symbol}: 当前权重={current:.4f}, 目标权重={target:.4f}, 需要{action}权重={diff:.4f}")

        # 检查价格数据
        print(f"  价格数据检查:")
        for symbol in target_weights.keys():
            if symbol in ctx.bars:
                bar = ctx.bars[symbol]
                price = bar.get("close", 0.0) if isinstance(bar, dict) else getattr(bar, "close", 0.0)
                print(f"    {symbol}: close={price}, 在bars中")
            else:
                print(f"    {symbol}: 不在bars中")
        
        print(f"  bars中的symbols: {list(ctx.bars.keys()) if hasattr(ctx.bars, 'keys') else 'N/A'}")
        print(f"  目标symbols: {list(target_weights.keys())}")
        
        # 使用 target 方法设置目标权重：引擎会自动计算目标权重与当前权重的差值
        # 如果差值 > 0，自动买入；如果差值 < 0，自动卖出
        # 注意：这里传入的是目标权重，不是买入/卖出权重差值
        print(f"  准备调用 ctx.order.target(target_weights)")
        try:
            ctx.order.target(target_weights)
            print(f"  ctx.order.target 调用成功")
        except Exception as e:
            print(f"  ctx.order.target 调用失败: {e}")
            import traceback
            traceback.print_exc()
        

        ctx.state["last_rebalance"] = current_dt
          
        saved = ctx.state.get("last_rebalance")
   
     

    def on_trade(self, fill: Any, ctx: Any) -> None:
        """订单成交回调"""
        symbol = fill.get("symbol", "UNKNOWN")
        side = fill.get("side", "UNKNOWN")
        quantity = fill.get("filled_quantity", 0.0)
        price = fill.get("price", 0.0)
        fee = fill.get("fee", 0.0)
        trade_amount = price * quantity
        commission = fee
        cost = trade_amount + commission
        print(f"  [成交] {side} {symbol} {quantity:.2f}股 @ {price:.4f}, 手续费={fee:.2f}, 交易金额={trade_amount:.2f}, 成本={cost:.2f}")
    
    def on_stop(self, ctx: Any) -> None:
        """Invoked once after the backtest completes."""
        print(f"回测结束，净值: {ctx.equity}")
        return None 

def main() -> None:
    # Prepare config with commission and slippage
    cfg = BacktestConfig(
        start="2022-01-01",
        end="2025-12-31",
        cash=100000.0,
        commission_rate=0.0005,  # 5 bps commission (0.05%)
        min_commission=5.0,      # 最小手续费 5元 (A股通常最低5元)
        slippage_bps=1.0,        # 1 bps slippage
        stamp_tax_rate=0.001,    # 印花税率 0.1% (卖出时收取，A股标准)
    )
    engine = BacktestEngine(cfg)

    symbol_list = ["513500.SH", "159941.SZ", "518880.SH", "511090.SH", "512890.SH"]
    period = "1d"
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "backtest.db")
    xtdata_dir = os.environ.get("XTDATA_DIR", r"D:\国金证券QMT交易端\userdata_mini")

    config = MarketDataConfig(
        db_path=db_path,
        xtdata_enabled=True,
        xtdata_data_dir=xtdata_dir,
    )

    service = MarketDataService(config)

    feeds: Dict[str, List[Dict[str, Any]]] = {}

    for symbol in symbol_list:
        request = DataRequest(
            symbols=[symbol],
            period=period,
            start_time=cfg.start,
            end_time=cfg.end,
            count=-1,
        )
        bars = service.fetch_bars(request, symbol=symbol)
   
        if not bars:
            print(f"[{symbol}] No data returned from xtdata/DB.")
            continue

        feeds[symbol] = bars

    if not feeds:
        print("未能获取任何标的的数据，退出。")
        return

    # 加载沪深300作为基准
    benchmark_symbol = "000300.SH"  # 沪深300指数
    print(f"\n正在加载基准数据: {benchmark_symbol}")
    benchmark_request = DataRequest(
        symbols=[benchmark_symbol],
        period=period,
        start_time=cfg.start,
        end_time=cfg.end,
        count=-1,
    )
    benchmark_bars = service.fetch_bars(benchmark_request, symbol=benchmark_symbol)
    
    benchmark_data: Dict[str, List[Dict[str, Any]]] = {}
    if benchmark_bars:
        benchmark_data[benchmark_symbol] = benchmark_bars
        print(f"基准数据加载成功，共 {len(benchmark_bars)} 条记录")
    else:
        print(f"警告: 未能加载基准数据 {benchmark_symbol}，将使用策略资产等权组合作为基准")

    strategy = MultiAssetRebalanceStrategy(symbols=list(feeds.keys()))
    result = engine.run(strategy, feeds, benchmark=benchmark_data if benchmark_data else None)

    print("\n" + "=" * 60)
    print("投资组合回测结果")
    print("=" * 60)

    stats = result.get("stats", {})
    if stats:
        print("\n[收益指标]")
        print(f"  起始净值:          {stats.get('start_equity', 0):>15,.2f}")
        print(f"  结束净值:          {stats.get('end_equity', 0):>15,.2f}")
        print(f"  总收益:            {stats.get('total_return', 0):>15,.2%}")
        print(f"  年化收益:          {stats.get('annualized_return', 0):>15,.2%}")

        print("\n[风险指标]")
        print(f"  波动率:            {stats.get('volatility', 0):>15,.4f}")
        print(f"  夏普比:            {stats.get('sharpe', 0):>15,.4f}")
        print(f"  卡玛比:            {stats.get('calmar', 0):>15,.4f}")
        print(f"  最大回撤:          {stats.get('max_drawdown', 0):>15,.4f}")
        print(f"  最大回撤时长:      {stats.get('max_dd_duration', 0):>15,} bars")
        max_dd_start = stats.get('max_dd_start')
        max_dd_end = stats.get('max_dd_end')
        if max_dd_start and max_dd_end:
            print(f"  最大回撤区间:      {max_dd_start} ~ {max_dd_end}")

        print("\n[交易统计]")
        print(f"  总交易数:          {stats.get('total_trades', 0):>15,}")
        print(f"  盈利笔数:          {stats.get('winning_trades', 0):>15,}")
        print(f"  亏损笔数:          {stats.get('losing_trades', 0):>15,}")
        print(f"  胜率:              {stats.get('win_rate', 0):>15,.2%}")
        realized_pnl = result.get("realized_pnl", stats.get("total_pnl", 0))
        print(f"  总盈亏:            {realized_pnl:>15,.2f}")

        print("\n[基准对比]")
        benchmark_return = stats.get('benchmark_return', 0)
        benchmark_max_dd = stats.get('benchmark_max_drawdown', 0)
        benchmark_max_dd_start = stats.get('benchmark_max_dd_start')
        benchmark_max_dd_end = stats.get('benchmark_max_dd_end')
        print(f"  基准收益:          {benchmark_return:>15,.2%}")
        print(f"  基准最大回撤:      {benchmark_max_dd:>15,.4f}")
        if benchmark_max_dd_start and benchmark_max_dd_end:
            print(f"  基准最大回撤区间:  {benchmark_max_dd_start} ~ {benchmark_max_dd_end}")
        elif benchmark_max_dd_start:
            print(f"  基准最大回撤区间:  {benchmark_max_dd_start} ~ (进行中)")

    print("\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    main()


