from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class SigPredictionsVenueConfig:
    """Configuration for the official SIG Predictions Cup v1 API."""

    site_url: str = "https://sig.thesuper.market"
    guide_url: str = "https://sig.thesuper.market/docs"
    markets_guide_url: str = "https://sig.thesuper.market/docs/markets-and-trading"
    changelog_url: str = "https://sig.thesuper.market/docs/changelog"
    rules_url: str = "https://predictionscup.com/rules/"
    api_version: str = "v1"

    api_key_env: str = "SIG_PREDICTIONS_API_KEY"
    api_base_url_env: str = "SIG_PREDICTIONS_API_BASE_URL"
    realtime_url_env: str = "SIG_PREDICTIONS_REALTIME_URL"
    tournament_id_env: str = "SIG_PREDICTIONS_TOURNAMENT_ID"

    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env)

    def api_base_url(self) -> str | None:
        return os.getenv(self.api_base_url_env)

    def realtime_url(self) -> str | None:
        return os.getenv(self.realtime_url_env)

    def tournament_id(self) -> str | None:
        return os.getenv(self.tournament_id_env)

    @staticmethod
    def realtime_market_channel(tournament_id: str, market_id: str) -> str:
        return f"tournament:{tournament_id}:market:{market_id}"
