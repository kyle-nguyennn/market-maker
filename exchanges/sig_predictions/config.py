from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class SigPredictionsVenueConfig:
    """Configuration copied from the official Super Market OpenAPI document."""

    site_url: str = "https://sig.thesuper.market"
    guide_url: str = "https://sig.thesuper.market/docs"
    api_reference_url: str = "https://sig.thesuper.market/api/v1/docs#description/introduction"
    production_api_base_url: str = "https://www.thesuper.market/api/v1"
    markets_guide_url: str = "https://sig.thesuper.market/docs/markets-and-trading"
    changelog_url: str = "https://sig.thesuper.market/docs/changelog"
    rules_url: str = "https://predictionscup.com/rules/"
    api_version: str = "v1"

    api_key_env: str = "SIG_PREDICTIONS_API_KEY"
    api_base_url_env: str = "SIG_PREDICTIONS_API_BASE_URL"
    tournament_id_env: str = "SIG_PREDICTIONS_TOURNAMENT_ID"

    standard_reads_per_minute: int = 100
    standard_writes_per_minute: int = 30
    realtime_token_lifetime_hours: int = 3
    max_realtime_channels_per_connection: int = 100

    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env)

    def api_base_url(self) -> str:
        return os.getenv(self.api_base_url_env, self.production_api_base_url).rstrip("/")

    def tournament_id(self) -> str | None:
        return os.getenv(self.tournament_id_env)

    @staticmethod
    def realtime_market_channel(tournament_id: str, market_id: str) -> str:
        return f"tournament:{tournament_id}:market:{market_id}"

    @staticmethod
    def realtime_tournament_markets_channel(tournament_id: str) -> str:
        return f"tournament:{tournament_id}:markets"
