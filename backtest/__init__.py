"""Offline, single-asset exposure backtesting for existing alpha Strategies.

Simulated positions and fills are research objects. Nothing here authorizes
orders, accesses credentials or depends on the Alpaca / AlphArena SDKs.
"""
from .engine import (
    BacktestConfig, BacktestMetrics, BacktestResult, CausalBarBacktester,
    EquityPoint, SimulatedFill,
)

__all__ = [
    "BacktestConfig", "BacktestMetrics", "BacktestResult",
    "CausalBarBacktester", "EquityPoint", "SimulatedFill",
]
