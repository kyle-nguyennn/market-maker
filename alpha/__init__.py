"""Venue-independent alpha predictions, features, and portfolio targets.

Importing this package never reaches an exchange, network or trade executor.
"""
from .domain import AlphaSignal, FeatureSnapshot, TargetExposure
from .features import FeatureConfig, build_price_features
from .market_data import BarProvider, PriceBar, read_price_bars_csv
from .strategy import AlphaStrategy

__all__ = [
    "AlphaSignal", "AlphaStrategy", "BarProvider",
    "FeatureConfig", "FeatureSnapshot", "PriceBar", "TargetExposure",
    "build_price_features", "read_price_bars_csv",
]
