# First authenticated par order-and-cancel smoke test

**Scope:** ONE small post-only BTC order on Hyperliquid **testnet** practice
DEX `par`, then an immediate cancel and account reconciliation. This is
NOT a trading strategy, auto-quoter or test of the scored `pa` DEX.

The script is `apps/par_first_order.py` and is **dry-run only by default**.

## Prerequisites

1. Verify that the **previously exposed** agent-wallet private key has been
   revoked and replaced using the Hyperliquid testnet API Wallet page.
   Re-authorize the new agent wallet for the funded Auror master wallet.
2. Verify the registered team/master wallet is the one that owns the
   **1,000,000 USDPR** practice balance. The agent wallet address is NOT the
   account address for balance / order / position queries.
3. Do not run any competing market maker or other bot against `par:BTC`
   during this test. The harness refuses if BTC has a resting order or
   position at preflight.
4. Use a Codespace/local environment where the fresh API private key can be
   loaded securely; **never paste it into chat, GitHub, CI logs or screenshots**.

## Install and dry-run

From repository root:

~~~bash
git fetch origin
git switch feat/par-first-order-smoke
python -m pip install -r requirements/hyperliquid.txt

# The script uses local .env if present; explicit exported vars win.
# Set these in the Git-ignored .env or environment:
# PERPETUAL_ALPHA_TEAM_WALLET=<your funded master wallet address>
# PERPETUAL_ALPHA_API_PRIVATE_KEY=<your NEW approved agent private key>
python -m apps.par_first_order
~~~

The dry run needs only the public team wallet address. It queries
`par:BTC` metadata, the L2 book, open orders, current position and available
USDPR collateral, then prints a planned ALO quote with no signing/trading.

## Send exactly one order and cancel

**Only after** verifying the dry-run and rotating the exposed agent key:

~~~bash
python -m apps.par_first_order \
  --execute \
  --confirm-wallet 0x95D8dbDED6265D43eFA604D3d8134Db5306a0354 \
  --ack-key-rotated
~~~

There is deliberately **no** `--dex` argument: this test cannot switch to
`pa`, mainnet or a different exchange. The explicit wallet confirmation must
match `PERPETUAL_ALPHA_TEAM_WALLET`.

The harness:

- Refuses if the `par:BTC` book is missing, crossed or older than 5 seconds.
- Refuses if there are existing `par:BTC` orders or any BTC position.
- Requires at least $25 free USDPR and computes size from actual
  `szDecimals` metadata.
- Generates a BUY ALO quote **0.5% below best bid**, targeting about **$15**
  notional with a hard **$25** ceiling, and checks the ordinary $10
  minimum. The exchange's post-only protection must reject crossing.
- Generates a unique 128-bit client order ID (CLOID) per attempt.
- Submits **one** order, then attempts cancel **by CLOID** in a `finally`
  block, even when submission fails or times out.
- Verifies that there are no `par:BTC` open orders and that the account's
  `par:BTC` position is zero. Prints `PASS` only after the confirmation.
- Uses the separately authorized agent key to sign; never touches the
  master private key and never logs the agent private key.

**Important:** A post-only order can be filled after resting and before
cancel. A successful cancellation does **not** close a filled position.
If verification fails, inspect the exchange UI and handle any BTC exposure
manually before retrying. Do not repeatedly run the test against an
unreconciled account.

## Possible outcomes

| Result | Interpretation | Action |
| --- | --- | --- |
| `PASS` with resting / cancel / flat | End-to-end signed order/cancel succeeded | Record public CLOID/OID and confirm flat in UI |
| `error: ...` from venue | Order rejected; ALO can reject on crossing | Recheck book, precision and agent authorization before rerun |
| `CANCEL NOT CONFIRMED` | Cancel may have timed out; order may remain live | Open UI and check order status immediately |
| `state NOT verified` | Order may be resting, or the account might have a fill | **Stop** and reconcile positions / orders |
| No USDPR / wrong wallet | Wrong master wallet or practice funding not accessible | Fix team wallet env; never set it to the agent wallet |
| Stale book / existing BTC activity | Smoke-test preconditions not satisfied | Resolve the precondition instead of overriding safety |

## Limitations

- This test does not validate the unattended quote loop, rate limiting,
  reconnect/replay, inventory skew, kill switch or drawdown logic.
- It does not authorize scored `pa` trading. `pa` funding remains an
  independent question.
- There is no automatic flattening; doing so would place another order
  beyond this one-order smoke test.
- The smoke-test script is not a substitute for the venue's UI / ledger
  when a network response is uncertain.
