from __future__ import annotations

from exchanges.sig_predictions.client import SigPredictionsVenueClient
from exchanges.sig_predictions.config import SigPredictionsVenueConfig
from market_handler.sig_predictions_adapter import SigPredictionsMarketAdapter


def build_stack() -> tuple[SigPredictionsVenueClient, SigPredictionsMarketAdapter]:
    config = SigPredictionsVenueConfig()
    return SigPredictionsVenueClient(config), SigPredictionsMarketAdapter()


def main() -> None:
    client, _ = build_stack()
    print(
        "SIG Predictions scaffold ready:",
        f"api={client.config.api_version}",
        f"base={client.config.api_base_url()}",
        "rest=ready",
        "realtime_socket=pending",
    )


if __name__ == "__main__":
    main()
