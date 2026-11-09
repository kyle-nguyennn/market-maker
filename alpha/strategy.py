"""Pure venue-neutral strategy contract; no HTTP, order or SDK dependencies."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .domain import AlphaSignal, FeatureSnapshot


@runtime_checkable
class AlphaStrategy(Protocol):
    """A deterministic strategy under fixed features/config/model state.

    A strategy may abstain with None. It must not submit venue requests,
    look up future data, size positions, or modify the supplied snapshot.
    Callers validate that returned signal symbol/snapshot_id match the
    supplied input and that the forecast horizon is compatible with the
    evaluation environment. No strategy is implemented in P1.
    """

    @property
    def strategy_id(self) -> str:
        ...

    def generate_signal(self, snapshot: FeatureSnapshot) -> AlphaSignal | None:
        ...
