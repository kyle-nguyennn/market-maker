"""Vendored-for-now, venue-independent alpha research and strategy package.

This public package depends only on Python's standard library; market-maker's
existing quoter/order bridge lives separately under quoter/.
"""
from .calculators import FeatureCalculator, PriceFeatureCalculator
from .domain import AlphaSignal, FeatureSnapshot, TargetExposure
from .feature_engine import FeatureEngine, replay_bars
from .features import FeatureConfig, build_price_features
from .observations import BarProvider, PriceBar
from .pipeline import AlphaPipeline, PipelineResult, replay_pipeline
from .signal_engine import SignalEngine
from .signal_models import MomentumSignalModel, MeanReversionSignalModel
from .strategy_engine import StrategyBatch, StrategyEngine
from .strategies import (
    DirectionalSignalStrategy, MeanReversionStrategy, MomentumStrategy,
)
from .strategy import (
    AlphaStrategy, MarketView, PortfolioSnapshot, QuoteProposal, SignalModel,
    Strategy, StrategyContext, StrategyDecision,
)

__all__ = [
    "AlphaPipeline", "AlphaSignal", "AlphaStrategy",
    "BarProvider", "DirectionalSignalStrategy", "FeatureCalculator",
    "FeatureConfig", "FeatureEngine", "FeatureSnapshot", "MarketView",
    "MeanReversionSignalModel", "MeanReversionStrategy", "MomentumSignalModel",
    "MomentumStrategy", "PipelineResult",
    "PortfolioSnapshot", "PriceBar", "PriceFeatureCalculator",
    "QuoteProposal", "SignalEngine", "SignalModel", "Strategy",
    "StrategyBatch", "StrategyEngine",
    "StrategyContext", "StrategyDecision", "TargetExposure",
    "build_price_features", "replay_bars",
    "replay_pipeline",
]
