# PAS MVP-0 — One process, independent workers

**Status:** Experimental integration; run read-only first. Not cleared for unattended scored trading. PR #11 builds on the SQLite WAL execution journal (PR #10), merged order/risk foundation (PR #6), and existing Hyperliquid adapter.

## Thread model

```text
                    One OS process
  +------------------------------------------------+
  | Main thread: OMS coordinator                   |
  |   - startup and periodic REST reconciliation   |
  |   - OrderManager (single writer/owner)         |
  |   - gateway risk checks and signed actions     |
  |   - emergency cancel attempts                 |
  |                                                |
  | Quoter worker: MarketState -> desired quotes    |
  |   - capacity-one queue; replaces stale intents |
  |                                                |
  | SQLite worker: committed prepared actions/fills|
  | SDK WS callback: updates locked market cache   |
  +------------------------------------------------+
```

No Quoter/OMS IPC or separate daemon. OMS is the sole mutator of `OrderManager`. Never invoke the dispatcher thread pools concurrently with this coordinator.

## Startup and steady-state invariants

1. Market-data handler subscribes and REST-bootstraps. All quotes require a valid, fresh two-sided L2.
2. OMS reads the durable WAL journal, authoritative open orders, margin/account positions, account equity, side capacity, order statuses, and fills. All exchange queries must use the configured `pas` DEX, not the native Hyperliquid defaults. For scored unified accounts, equity is computed from the team wallet's **USDP spot total plus the unrealized P&L** in DEX-scoped perp positions; the perpetual `marginSummary.accountValue` alone is not the team's funded equity.
3. **First activation requires a new journal, zero positions and zero resting orders**; an immutable timestamp + equity anchor is committed. Existing non-flat accounts cannot silently reinitialize a journal.
4. Subsequent recovery maps every prepared/sent CLOID to an exchange OID/status and verifies all post-anchor fills. SQLite fill-signed positions must exactly match current venue positions.
5. Mismatched, stale, missing, ambiguous or foreign order/account state blocks quoting and triggers best-effort cancellation of locally known open quotes where possible. A cancel request or ACK is NOT proof of a terminal order.
6. Rest/WS events and the journal must be fully reconciled before READY. Runtime rechecks account/order/fill state every `--poll-seconds`; no subsequent action bypasses SafeExecutionGateway risk checks.
7. Hard limits are supplied explicitly for signed trading. Verified exchange account mode, verified capacity, collateral currency and reconciled positions are essential; `PERPETUAL_ALPHA_PAS_ALLOW_TRADING=false` continues to block signed operations unless deliberately changed.

## Build & read-only run

```bash
conda env create --format environment-yaml -f environment.hyperliquid.yml
conda activate market-maker
cp .env.template .env
# Set the correct public team wallet address. Do NOT commit .env.
python -m pytest -q tests/test_pas_mvp0.py
python -m apps.perpetual_alpha --check --dex pas --instrument BTC

# Runs the full state- and recovery-checking coordinator but sends NO orders:
python -m apps.pas_mvp0 --dex pas --instrument BTC \
  --quote-size 0.0002 --edge-bps 5 \
  --journal data/state/pas-btc-mvp0.sqlite
```

If the account has no scored USDP or any required unrealized-P&L field is missing/invalid, the runner correctly remains unready. **Do not delete a populated journal to bypass this barrier.**

## Before any micro-order live trial

- Confirm `pas:BTC`, funded USDP, user/agent authorization, and *actual* unified-account equity and capacity field semantics from read-only responses. Neither the public L2 check nor passing unit tests proves this.
- Supply all eight explicit risk caps (position, order notional, absolute notional, gross, net, drawdown, min order notional, price collar).
- Verify the value of `--confirmed-unified-account` against the actual account configuration, not assumption. Confirm an API-agent signer is rotated/approved. Scored trading requires changing the separate `PERPETUAL_ALPHA_PAS_ALLOW_TRADING` guard.
- Test a very small ALO order, partial fill, cancel acknowledgement, restart with resting orders, restart with inventory, REST/WS disconnect, stale data, malformed account state, and disk-full before unattended trading.
- The bot attempts to arm a 90-second venue-side dead-man cancellation, refreshing every 30 seconds. **Official PAS documentation states the venue rejects it until $1,000,000 in trading volume.** Before eligibility, the only supported initial live mode is explicit `--attended`, which accepts that particular response while requiring you to monitor open orders manually. Any other dead-man failure stops trading. The bot cannot promise unattended safety without the venue timer or another independent safeguard.
- Recheck `maxTradeSzs` semantics and **USDP spot total + DEX-scoped unrealized P&L** on this scored unified account before using live mode. The perps `marginSummary.accountValue` is not used as scored unified equity. The runner deliberately refuses missing or malformed inputs.

## Known blockers / intentionally missing

- The exchange dead-man feature is gated until $1M of account trading volume. Until eligible, there is **no independent cancellation after a host crash**. Use manual/attended microtrials only.
- No proven private WS order/fill normalization; REST polling is authoritative for now and may lag.
- No formal trade-history pagination/coverage proof if the exchange truncates account fills. Even position equality cannot prove that a same-sized round trip was not omitted.
- No robust cross-asset quoting, hedging, multi-market inventory, funding accounting or PnL reports.
- No verified end-to-end *live* scored order cycle; no private key or real order has been submitted by this PR.
- No automatic cross-session/manual reconciliation override. Unresolved state halts instead of trading under assumptions.

**Release criterion:** end-to-end signed order/cancel/fill/restart verified with tiny exposure and correct account/DEX scoping. A fully unattended release additionally requires eligible venue-side dead-man cancellation or an independent recovery agent. Do not bypass positive equity, available trading capacity or verified account-mode checks. Until then this is a candidate MVP-0 integration, not a guarantee of safety.

## Mainline compatibility audit (2026-10-21)

- PR #9 (operations telemetry, account reconciliation observer, supervisor, dead-man file CLI) and PR #10 (durable SQLite WAL journal and execution dispatcher) are already merged into `main`.
- This PR introduces the PAS one-process runtime and a small number of additive venue-client/SQLite helper methods. It must **not** replace or remove either merged subsystem; the runtime reuses the existing `SafeExecutionGateway` and SQLite journal.
- GitHub reported the PR as **mergeable** against the updated `main`; PR CI on the combined pull-request merge ref passed **204 tests** with each DEX-profile configuration (workflow 39555646333).
- The runtime intentionally does **not** wire the generic PR #9 `SafetySupervisor` and `AsyncEventJournal`; its own reconciliation gate and SQLite persistence do not supersede those shared components. That remaining integration should be handled separately before unattended deployment.
- The scored unified equity check was corrected to use USDP spot plus unrealized P&L. Regression tests now reject absent collateral, duplicated USDP entries and incomplete P&L.
- This audit establishes **source compatibility and test pass status**, not venue funding, approval to trade, or unattended safety.
