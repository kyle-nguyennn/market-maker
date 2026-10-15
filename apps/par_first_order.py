"""One-shot authenticated Hyperliquid par:BTC order-and-cancel smoke test.

Dry run by default. Only --execute AND --confirm-wallet AND
--ack-key-rotated authorize one ALO order, followed immediately by cancel.

This is NOT an unattended quoter. Never log or commit the API wallet key.
Run: python -m apps.par_first_order --execute \
  --confirm-wallet 0x<the-registered-main-team-wallet> --ack-key-rotated
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from exchanges.hyperliquid.client import HyperliquidVenueClient
from exchanges.hyperliquid.config import HyperliquidVenueConfig
from exchanges.hyperliquid.types import TimeInForce, VenueOrderRequest
from market_handler.hyperliquid_adapter import HyperliquidMarketAdapter
from model.order import OrderIntent, Side


SYMBOL = "par:BTC"
MIN_ORDER_NOTIONAL = Decimal("10")
TARGET_ORDER_NOTIONAL = Decimal("15")
HARD_MAX_NOTIONAL = Decimal("25")
MAX_BOOK_AGE_MS = 5000
BID_OFFSET = Decimal("0.005")  # quote 0.5% BELOW the current best bid


class SmokeTestError(RuntimeError):
    """Fail closed when a precondition or post-trade invariant is violated."""


@dataclass(frozen=True)
class PlannedOrder:
    request: VenueOrderRequest
    notional: Decimal
    bid: Decimal
    ask: Decimal


def _assert_practice(client: HyperliquidVenueClient) -> None:
    config = client.config
    if (
        config.dex != "par"
        or config.collateral != "USDPR"
        or config.api_url.rstrip("/") != "https://api.hyperliquid-testnet.xyz"
    ):
        raise SmokeTestError("refusing to trade: only testnet par/USDPR is supported")


def _position_size(account: dict, coin: str = SYMBOL) -> Decimal:
    """Handle the SDK's clearinghouseState assetPositions object shape."""
    size = Decimal("0")
    for item in account.get("assetPositions", []):
        pos = item.get("position", item)
        if pos.get("coin") == coin:
            size += Decimal(str(pos["szi"]))
    return size


def _open_orders_for_symbol(orders: list[dict], coin: str = SYMBOL) -> list[dict]:
    return [order for order in orders if order.get("coin") == coin]


def _collateral_amount(account: dict, collateral: str = "USDPR") -> Decimal:
    for balance in account.get("balances", []):
        if balance.get("coin") == collateral:
            # The venue exposes total and hold; reject accounts without
            # adequate unheld collateral for this small smoke trade.
            return Decimal(str(balance["total"])) - Decimal(str(balance.get("hold", "0")))
    return Decimal("0")


def prepare_order(
    client: HyperliquidVenueClient,
    adapter: HyperliquidMarketAdapter,
    team_wallet: str,
    *,
    now_ms: int | None = None,
    client_order_id: str | None = None,
) -> PlannedOrder:
    """Read-only checks and a small ALO order plan; no signer is constructed."""
    _assert_practice(client)
    meta = client.query_meta()
    spec = adapter.market_spec_from_meta("BTC", meta)
    if not (0 <= spec.sz_decimals <= 8):
        raise SmokeTestError("unexpected size precision")

    book = client.query_l2_book("BTC")
    market = adapter.from_sdk_l2_snapshot(book)
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    age_ms = timestamp - market.timestamp_ms
    if not market.is_valid or market.best_bid is None or market.best_ask is None:
        raise SmokeTestError("par:BTC order book is not valid and two-sided")
    if not 0 <= age_ms <= MAX_BOOK_AGE_MS:
        raise SmokeTestError(f"stale/future order book: age_ms={age_ms}")
    if market.best_bid <= 0 or market.best_ask <= market.best_bid:
        raise SmokeTestError("invalid spread or bid price")

    # Do not interfere with any existing par:BTC activity, however small.
    if _open_orders_for_symbol(client.query_open_orders(team_wallet)):
        raise SmokeTestError("pre-existing par:BTC orders; cancel them manually first")
    position = _position_size(client.query_positions(team_wallet))
    if position:
        raise SmokeTestError(f"pre-existing par:BTC position {position}; refusing smoke test")
    collateral = _collateral_amount(client.query_spot_balance(team_wallet))
    if collateral < HARD_MAX_NOTIONAL:
        raise SmokeTestError("insufficient free USDPR collateral for capped test order")

    # Passive BUY far from the spread to minimize fill probability; ALO must
    # reject rather than take liquidity if the book moves before submission.
    price = adapter._quantize_price(
        market.best_bid * (Decimal("1") - BID_OFFSET), Side.BID,
        spec.sz_decimals,
    )
    if price <= 0 or price >= market.best_ask:
        raise SmokeTestError("calculated limit price is not passive")

    size_increment = Decimal(1).scaleb(-spec.sz_decimals)
    size = (
        (TARGET_ORDER_NOTIONAL / price / size_increment)
        .to_integral_value(rounding=ROUND_CEILING)
        * size_increment
    )
    if size <= 0:
        raise SmokeTestError("calculated order quantity is zero")
    notional = price * size
    if not MIN_ORDER_NOTIONAL <= notional <= HARD_MAX_NOTIONAL:
        raise SmokeTestError(
            f"planned notional {notional} outside [{MIN_ORDER_NOTIONAL}, {HARD_MAX_NOTIONAL}]"
        )

    # A new CLOID permits cancellation even after a network timeout in place_order.
    cloid = client_order_id or "0x" + secrets.token_hex(16)
    intent = OrderIntent(
        instrument="BTC", side=Side.BID, price=price, size=size,
        post_only=True, reduce_only=False, client_order_id=cloid,
    )
    request = adapter.to_venue_order(intent, spec)
    if request.instrument != SYMBOL or request.tif is not TimeInForce.ALO:
        raise SmokeTestError("order adapter produced a non-practice/non-ALO request")
    if Decimal(request.price) * Decimal(request.size) > HARD_MAX_NOTIONAL:
        raise SmokeTestError("venue-quantized order breaches notional ceiling")
    return PlannedOrder(request, notional, market.best_bid, market.best_ask)


def _assert_after(client: HyperliquidVenueClient, wallet: str, cloid: str) -> None:
    """Verify no BTC order is open and no position remains, even without CLOIDs."""
    orders = _open_orders_for_symbol(client.query_open_orders(wallet))
    if orders:
        raise SmokeTestError(
            f"par:BTC still has {len(orders)} open order(s), "
            f"including potentially {cloid}; inspect/cancel manually"
        )
    position = _position_size(client.query_positions(wallet))
    if position != 0:
        raise SmokeTestError(
            f"par:BTC position is {position}; order cancellation cannot close fills. "
            "Review the account and close exposure deliberately."
        )
    print("FINAL VERIFICATION: no par:BTC open orders; par:BTC position = 0.")


def run_once(
    client: HyperliquidVenueClient,
    adapter: HyperliquidMarketAdapter,
    wallet: str,
    api_private_key: str,
    *,
    wait_seconds: float = 0.4,
) -> None:
    """Attempt ONE order, cancellation, and verification even after SDK timeouts."""
    plan = prepare_order(client, adapter, wallet)
    print(
        f"PLAN: {plan.request.instrument} BUY ALO sz={plan.request.size} "
        f"px={plan.request.price} notional={plan.notional:.2f} USDPR; "
        f"bid={plan.bid} ask={plan.ask} cloid={plan.request.client_order_id}"
    )
    client.connect_exchange(team_wallet=wallet, api_private_key=api_private_key)
    submission_error: Exception | None = None
    cancel_error: Exception | None = None
    try:
        result = client.place_order(plan.request)
        print(f"ORDER ACK: status={result.status} oid={result.order_id}")
        if result.status == "error":
            raise SmokeTestError(f"exchange rejected ALO order: {result.error}")
        if result.status != "resting":
            raise SmokeTestError(
                "expected post-only order to rest; possible fill: "
                f"status={result.status}, filled_size={result.filled_size}"
            )
        if wait_seconds:
            time.sleep(wait_seconds)
    except Exception as exc:
        # A thrown timeout is ambiguous: the venue may have accepted the order.
        submission_error = exc
    finally:
        print(f"CANCEL ATTEMPT (by CLOID): {plan.request.client_order_id}")
        try:
            if not client.cancel_order(
                "BTC", client_order_id=plan.request.client_order_id
            ):
                raise SmokeTestError("cancel response was not successful")
            print("CANCEL ACK: success")
        except Exception as exc:
            cancel_error = exc
            print(
                f"CANCEL NOT CONFIRMED ({type(exc).__name__}); "
                "the order may still be live; check Hyperliquid's UI.",
                file=sys.stderr,
            )

    # Even after cancellation errors, try to establish whether any exposure
    # exists. Only a confirmed empty open-order book AND flat position pass.
    verification_error: Exception | None = None
    for attempt in range(3):
        try:
            _assert_after(client, wallet, plan.request.client_order_id)
            verification_error = None
            break
        except Exception as exc:
            verification_error = exc
            if attempt < 2:
                time.sleep(0.5)

    if verification_error is not None:
        raise SmokeTestError(
            f"post-trade state NOT verified: {verification_error}. "
            "Inspect open orders and positions manually."
        ) from verification_error
    if cancel_error is not None:
        raise SmokeTestError(
            "order may be flat, but cancellation was not acknowledged; "
            "check the venue's order status before another attempt"
        ) from cancel_error
    if submission_error is not None:
        raise SmokeTestError(
            f"submission did not produce a confirmed resting order: "
            f"{type(submission_error).__name__}: {submission_error}"
        ) from submission_error


def main() -> int:
    parser = argparse.ArgumentParser(description="ONE par:BTC ALO order + cancel")
    parser.add_argument(
        "--execute", action="store_true", help="actually submit one order; default dry-run"
    )
    parser.add_argument(
        "--confirm-wallet", default="", metavar="ADDRESS",
        help="must equal the registered team wallet to enable order submission",
    )
    parser.add_argument(
        "--ack-key-rotated", action="store_true",
        help="confirm previously exposed agent key was revoked and replaced",
    )
    args = parser.parse_args()

    # Explicitly avoid accepting a --dex option: this is par ONLY.
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
        config = HyperliquidVenueConfig()
        client = HyperliquidVenueClient(config)
        adapter = HyperliquidMarketAdapter(config)
        wallet = os.environ.get("PERPETUAL_ALPHA_TEAM_WALLET", "")
        if not wallet:
            raise SmokeTestError("PERPETUAL_ALPHA_TEAM_WALLET is not configured")

        _assert_practice(client)
        plan = prepare_order(client, adapter, wallet)
        print(
            f"DRY RUN PLAN: {plan.request.instrument} BUY post-only "
            f"price={plan.request.price} size={plan.request.size} "
            f"notional={plan.notional:.2f} USDPR "
            f"cloid={plan.request.client_order_id}"
        )
        if not args.execute:
            print(
                "No order submitted. To authorize one order and cancel: "
                "pass --execute, --confirm-wallet <TEAM_ADDRESS>, --ack-key-rotated."
            )
            return 0

        if args.confirm_wallet.lower() != wallet.lower():
            raise SmokeTestError("--confirm-wallet must exactly match the main team wallet")
        if not args.ack_key_rotated:
            raise SmokeTestError("revoke exposed agent key, then pass --ack-key-rotated")
        if not os.environ.get("PERPETUAL_ALPHA_API_PRIVATE_KEY"):
            raise SmokeTestError("PERPETUAL_ALPHA_API_PRIVATE_KEY is unset")
        # The preflight is repeated immediately before creating the Exchange.
        run_once(client, adapter, wallet, os.environ["PERPETUAL_ALPHA_API_PRIVATE_KEY"])
        print("PASS: authenticated order, cancellation and flat-position verification")
        return 0
    except Exception as exc:
        print(
            f"STOP / REVIEW: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
