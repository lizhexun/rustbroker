"""
Strategy base class for backtesting
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any


class Strategy(ABC):
    """Base strategy class for backtesting"""
    
    def on_start(self, ctx):
        """
        Called once at the start of backtest.
        Use this to register indicators.
        
        Args:
            ctx: BarContext object
        """
        pass
    
    @abstractmethod
    def on_bar(self, ctx):
        """
        Called for each bar during backtest.
        Implement your trading logic here.
        
        Args:
            ctx: BarContext object
        """
        pass
    
    def on_trade(self, fill: Dict[str, Any], ctx):
        """
        Called when an order is filled.
        
        Args:
            fill: Fill dictionary with symbol, side, quantity, price, commission, timestamp
            ctx: BarContext object
        """
        pass
    
    def on_stop(self, ctx):
        """
        Called once at the end of backtest.
        
        Args:
            ctx: BarContext object
        """
        pass

