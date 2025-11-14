"""
Indicator definitions and registry
"""

from typing import Dict, Optional, Callable, Any


class Indicator:
    """Indicator factory class"""
    
    @staticmethod
    def sma(period: int, field: str = "close") -> Dict[str, Any]:
        """
        Simple Moving Average indicator
        
        Args:
            period: Period for moving average
            field: Field to use (default: "close")
        
        Returns:
            Indicator definition dict
        """
        return {
            "type": "rust_builtin",
            "name": "sma",
            "params": {"period": period, "field": field},
            "lookback_period": period,
        }
    
    @staticmethod
    def rsi(period: int, field: str = "close") -> Dict[str, Any]:
        """
        Relative Strength Index indicator
        
        Args:
            period: Period for RSI
            field: Field to use (default: "close")
        
        Returns:
            Indicator definition dict
        """
        return {
            "type": "rust_builtin",
            "name": "rsi",
            "params": {"period": period, "field": field},
            "lookback_period": period + 1,
        }
    
    @staticmethod
    def python_function(func: Callable, count: int) -> Dict[str, Any]:
        """
        Python function indicator
        
        Args:
            func: Python function that takes bars and returns indicator value
            count: Lookback period
        
        Returns:
            Indicator definition dict
        """
        return {
            "type": "python_function",
            "func": func,
            "lookback_period": count,
        }


class IndicatorRegistry:
    """Registry for managing indicators"""
    
    def __init__(self):
        self._indicators: Dict[str, Dict[str, Any]] = {}
    
    def register(self, name: str, indicator_def: Dict[str, Any]):
        """Register an indicator"""
        self._indicators[name] = indicator_def
    
    def get(self, name: str) -> Optional[Dict[str, Any]]:
        """Get indicator definition"""
        return self._indicators.get(name)

