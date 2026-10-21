"""SIG Predictions diagnostics — explicitly read-only.

From the repository root:

  SIG_PREDICTIONS_API_KEY=<key> python -m apps.sig_predictions --check
  python -m apps.sig_predictions --check --exchange-id 36

The CLI never constructs a trading-enabled client or sends an order.
"""
from __future__ import annotations

import argparse
import re

from exchanges.sig_predictions.client import SigPredictionsVenueClient
from exchanges.sig_predictions.config import SigPredictionsVenueConfig
from market_handler.sig_predictions_adapter import SigPredictionsMarketAdapter


def build_stack() -> tuple[SigPredictionsVenueClient, SigPredictionsMarketAdapter]:
    return SigPredictionsVenueClient(allow_trading=False), SigPredictionsMarketAdapter()


def check_account(
    client: SigPredictionsVenueClient,
    *,
    tournament_slug: str | None = None,
    exchange_id: str | None = None,
) -> None:
    """Validate authenticated read-access without printing private account data."""
    if not client.config.api_key():
        raise ValueError(f"missing {client.config.api_key_env}")
    if tournament_slug and not re.fullmatch(r"[a-zA-Z0-9-]+", tournament_slug):
        raise ValueError("invalid tournament slug")
    if exchange_id and (not exchange_id.isdigit() or int(exchange_id) <= 0):
        raise ValueError("exchange_id must be a positive numeric ID")

    client.get_account()
    print("Account: authenticated read succeeded")
    if tournament_slug:
        payload = client.get_tournament(tournament_slug)
        record = payload.get("tournament", payload) if isinstance(payload, dict) else None
        tournament_id = (
            record.get("id", record.get("tournamentId"))
            if isinstance(record, dict) else None
        )
        if isinstance(tournament_id, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", tournament_id):
            print(f"Tournament resolved: {tournament_slug} UUID={tournament_id}")
            print("Use this UUID in SIG_PREDICTIONS_TOURNAMENT_ID for the smoke.")
        else:
            print(f"Tournament resolved: {tournament_slug} (UUID absent in known response fields)")
    client.get_positions(tournament_slug=tournament_slug)
    print("Portfolio positions: read succeeded")
    markets = client.list_markets(
        limit=5, tournament_id=client.config.tournament_id(),
    )
    if isinstance(markets, dict) and isinstance(markets.get("markets"), list):
        print(f"Markets: {len(markets['markets'])} returned (limit=5)")
    else:
        print("Markets: read succeeded")
    if exchange_id:
        book = client.get_order_book(
            exchange_id,
            tournament_id=client.config.tournament_id(),
        )
        adapter = SigPredictionsMarketAdapter()
        state = adapter.snapshot(book)
        print(
            f"Book SIG_EXCHANGE:{exchange_id}: "
            f"bid={state.best_bid}, ask={state.best_ask}, "
            f"valid={state.is_valid}, version={book.as_of}"
        )
    print("Read-only SIG checks complete. No orders were placed or cancelled.")


def main() -> None:
    parser = argparse.ArgumentParser(description="SIG Predictions read-only API diagnostics")
    parser.add_argument("--check", action="store_true", help="run authenticated reads")
    parser.add_argument("--sdk", action="store_true", help="deprecated compatibility alias; SDK is always used")
    parser.add_argument("--exchange-id", default=None, help="optional numeric exchange ID")
    parser.add_argument("--tournament-slug", default=None, help="optional tournament slug")
    args = parser.parse_args()

    client, _ = build_stack()
    try:
        if not args.check:
            print(
                "SIG Predictions adapter available:",
                f"api={client.config.api_version}",
                f"base={client.config.api_base_url()}",
                "trading=disabled",
                "transport=scalar-sdk",
                "realtime_socket=pending",
            )
            return
        check_account(client, tournament_slug=args.tournament_slug, exchange_id=args.exchange_id)
    finally:
        client.close()


if __name__ == "__main__":
    main()
