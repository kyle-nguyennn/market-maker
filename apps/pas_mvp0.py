"""Run MVP-0 in one Python process. Dry-run is the default.

Example (read-only, no signer):
  python -m apps.pas_mvp0 --dex pas --instrument BTC --quote-size 0.0002

Explicitly authorized trading ALSO requires --live, a matching wallet confirmation,
verified account mode, complete risk caps, a separately approved API agent key,
and PERPETUAL_ALPHA_PAS_ALLOW_TRADING=true. Never use your master wallet key.
"""
from __future__ import annotations

import argparse
import logging
import os
from decimal import Decimal

from execution.order_manager import OrderManager
from execution.pas_mvp0_runtime import MVP0PasBot, PassiveQuoter
from execution.safe_gateway import SafeExecutionGateway
from execution.sqlite_journal import SQLiteWALJournal
from exchanges.hyperliquid.client import HyperliquidVenueClient
from exchanges.hyperliquid.config import HyperliquidVenueConfig
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter
from market_handler.hyperliquid_market_data_handler import HyperliquidMarketDataHandler
from risk.risk_manager import RiskLimits, RiskManager


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PAS MVP-0: one-process conservative maker")
    parser.add_argument("--dex", choices=("par", "pas"), default="pas")
    parser.add_argument("--instrument", default="BTC")
    parser.add_argument("--quote-size", type=Decimal, required=True)
    parser.add_argument("--edge-bps", type=Decimal, default=Decimal("5"))
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--quote-seconds", type=float, default=1.0)
    parser.add_argument("--journal", default=None)
    parser.add_argument("--live", action="store_true", help="explicitly request authenticated orders")
    parser.add_argument("--confirm-wallet", default=None,
                        help="must exactly equal team wallet for live trading")
    parser.add_argument("--confirmed-unified-account", action="store_true",
                        help="operator has independently verified the PAS account mode")
    parser.add_argument("--max-position", type=Decimal)
    parser.add_argument("--max-order-notional", type=Decimal)
    parser.add_argument("--max-abs-notional", type=Decimal)
    parser.add_argument("--max-gross-notional", type=Decimal)
    parser.add_argument("--max-net-notional", type=Decimal)
    parser.add_argument("--max-drawdown", type=Decimal)
    parser.add_argument("--min-order-notional", type=Decimal)
    parser.add_argument("--max-quote-deviation-bps", type=Decimal)
    parser.add_argument("--max-book-age-ms", type=int, default=2000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = HyperliquidVenueConfig.from_environment(dex=args.dex)
    team_wallet = os.environ.get("PERPETUAL_ALPHA_TEAM_WALLET", "")
    if not team_wallet:
        raise SystemExit("PERPETUAL_ALPHA_TEAM_WALLET is required in .env")
    if args.live:
        if args.dex != "pas":
            raise SystemExit("MVP-0 live mode only supports explicitly scored pas")
        if args.confirm_wallet is None or args.confirm_wallet.lower() != team_wallet.lower():
            raise SystemExit("--confirm-wallet must match PERPETUAL_ALPHA_TEAM_WALLET")
        if not args.confirmed_unified_account:
            raise SystemExit("verify account mode before --confirmed-unified-account")
        if not config.allow_live_trading:
            raise SystemExit("PERPETUAL_ALPHA_PAS_ALLOW_TRADING must be explicitly enabled")
        if not os.getenv("PERPETUAL_ALPHA_API_PRIVATE_KEY"):
            raise SystemExit("approved PERPETUAL_ALPHA_API_PRIVATE_KEY is required")
    required_caps = (
        "max_position", "max_order_notional", "max_abs_notional",
        "max_gross_notional", "max_net_notional", "max_drawdown",
        "min_order_notional", "max_quote_deviation_bps",
    )
    if args.live and any(getattr(args, name) is None for name in required_caps):
        raise SystemExit("live trading requires every explicit --max/--min risk limit")

    limits = RiskLimits(
        max_abs_position=args.max_position,
        max_order_notional=args.max_order_notional,
        max_abs_notional=args.max_abs_notional,
        max_drawdown=args.max_drawdown,
        max_market_data_age_ms=args.max_book_age_ms,
        min_order_notional=args.min_order_notional,
        max_gross_notional=args.max_gross_notional,
        max_net_notional=args.max_net_notional,
        max_quote_deviation_bps=args.max_quote_deviation_bps,
    )
    client = HyperliquidVenueClient(config)
    adapter = HyperliquidMarketAdapter(config)
    market = HyperliquidMarketDataHandler(
        client, args.instrument, adapter=adapter, max_age_ms=args.max_book_age_ms,
    )
    spec = adapter.market_spec_from_meta(args.instrument, client.query_meta())
    risk = RiskManager(limits)
    manager = OrderManager(dex=args.dex, account_id=team_wallet)
    gateway = SafeExecutionGateway(
        manager=manager, risk=risk, adapter=adapter, client=client,
        market_spec=spec, live_enabled=args.live, operator_authorized=args.live,
    )
    journal_path = args.journal or f"data/state/{args.dex}-{args.instrument.lower()}-mvp0.sqlite"
    if args.live:
        client.connect_exchange(
            team_wallet=team_wallet, api_private_key=os.environ["PERPETUAL_ALPHA_API_PRIVATE_KEY"]
        )
    with SQLiteWALJournal(journal_path, dex=args.dex, account_id=team_wallet) as journal:
        recovery = journal.load_recovery().result(timeout=10)
        if recovery.kill_switch:
            raise SystemExit(f"persisted kill switch; operator intervention required: {recovery.kill_reason}")
        app = MVP0PasBot(
            client=client, adapter=adapter, market=market, manager=manager,
            gateway=gateway, risk=risk, journal=journal,
            team_wallet=team_wallet, instrument=args.instrument,
            quoter=PassiveQuoter(size=args.quote_size, edge_bps=args.edge_bps),
            live=args.live, verified_unified=args.confirmed_unified_account,
            poll_s=args.poll_seconds, quote_s=args.quote_seconds,
        )
        app.run()


if __name__ == "__main__":
    main()
