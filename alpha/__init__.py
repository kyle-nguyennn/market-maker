"""Venue-neutral alpha predictions and desired portfolio exposures.

This package does not execute trades or implement research policies.
"""
from .domain import AlphaSignal, FeatureSnapshot, TargetExposure
from .strategy import AlphaStrategy

__all__ = ["AlphaSignal", "AlphaStrategy", "FeatureSnapshot", "TargetExposure"]
