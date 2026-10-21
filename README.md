# market-maker

Venue-independent market-making framework with separate exchange adapters.

## Install with Conda

Run these commands **from the repository root**. The explicit `--format` for
exchange-specific filenames makes Conda recognize the YAML spec on recent versions.
Choose the environment for
your intended exchange; both variants use the same `market-maker` environment
name, so create **one** of them in a fresh environment.

### Core only (no exchange SDK)

```bash
conda env create -f environment.yml
conda activate market-maker
```

Installs the shared dependencies in `requirements/common.txt`.

### Hyperliquid / Perpetual Alpha (core + Hyperliquid SDK)

```bash
conda env create --format environment-yaml -f environment.hyperliquid.yml
conda activate market-maker
```

Installs `requirements/common.txt` and
`hyperliquid-python-sdk==0.24.0` through
`requirements/hyperliquid.txt`. No exchange SDK is installed by
the core-only environment.

To add Hyperliquid to an existing core environment without recreating it:

```bash
conda env update --format environment-yaml -n market-maker -f environment.hyperliquid.yml
conda activate market-maker
```

### Sanity checks

```bash
python -m pytest -q
python -m apps.perpetual_alpha
python -m pip check
```

The Perpetual Alpha app currently runs in **read-only scaffold mode**;
a successful initialization does not mean that trading is enabled.

### Adding another exchange

Put its SDK requirements in `requirements/<exchange>.txt`, starting with
`-r common.txt`. Add an `environment.<exchange>.yml` using the same Conda
`python` / `pip` configuration and pointing the `pip:` section at that
exchange requirements file. Keep exchange SDKs out of `common.txt`.

For Perpetual Alpha architecture and prerequisites, see
[docs/PERPETUAL_ALPHA.md](docs/PERPETUAL_ALPHA.md).

## Perpetual Alpha: choose practice or scored connectivity

The Hyperliquid-specific environment includes python-dotenv. Copy the template
to a Git-ignored local file, then select a DEX:

~~~bash
cp .env.template .env
# Edit .env: PERPETUAL_ALPHA_DEX=par (practice) or pas (scored)
python -m apps.perpetual_alpha
python -m apps.perpetual_alpha --check
~~~

Already exported shell variables take precedence over the local .env file.
Use --dex to override only the DEX for a single run, or unset the shell
variable if you want the .env value to take effect.

You can override the environment selection for a single read-only command:

~~~bash
python -m apps.perpetual_alpha --check --dex par
python -m apps.perpetual_alpha --check --dex pas
~~~

| Setting | Practice | Scored |
| --- | --- | --- |
| DEX selector | par | pas |
| Network | Hyperliquid testnet | Hyperliquid testnet |
| REST host | api.hyperliquid-testnet.xyz | api.hyperliquid-testnet.xyz |
| DEX prefix | par: | pas: |
| Collateral | USDPR | USDP |

The shared config is loaded via HyperliquidVenueConfig.from_environment()
and passed to both HyperliquidVenueClient and HyperliquidMarketAdapter. The
client uses that host for REST, SDK-signed Exchange and SDK WebSocket connections,
and every market/account query uses the corresponding DEX. Invalid combinations
(e.g., pas with the mainnet URL, or pas with USDPR) raise ValueError early.

The diagnostics are read-only: no API private key is loaded or required.
PERPETUAL_ALPHA_PAS_ALLOW_TRADING=false blocks signed pas operations even when an
agent key is available. Do **not** enable pas trading until its deployment,
endpoint, account funding, agent approval, risk limits and execution tests have
been verified. Confirm the approved agent can trade on the selected DEX; the same testnet network does not imply separate scored collateral is funded.

Network connectivity alone does not establish that a team's scored USDP
balance is funded. If the read-only pas diagnostic reports no USDP, contact
the organizers and do not attempt trading.

**Security:** Never commit .env or private keys. Revoke any API key previously
exposed in a screenshot and replace it with a fresh approved agent key.

**Correction (2026-10-20):** The prior 2026-10-15 testnet probes selected
`pa`, which is **not** the scored DEX identified by the participant guide.
Those results (three assets and no two-sided BTC book) say nothing about
`pas` readiness. The official guide specifies scored DEX `pas` (index 271),
`USDP` collateral and unified margin mode. Verify live `pas` metadata, valid
books, team balance and approved agent separately before any scored trading:

```bash
python -m apps.perpetual_alpha --check --dex pas --instrument BTC
```

## SIG Predictions Cup — read-only first

The SIG v1 API wrapper and YES-normalized adapter are ported to mainline.
Use a participant API key from the SIG account profile. Do not commit keys.

```bash
export SIG_PREDICTIONS_API_KEY='<your participant API key>'
python -m apps.sig_predictions
python -m apps.sig_predictions --check
# Optional: use a real exchange ID, not the market container ID:
python -m apps.sig_predictions --check --exchange-id 36
```

Optionally export SIG_PREDICTIONS_TOURNAMENT_ID to scope market/book queries
or pass --tournament-slug for tournament-specific portfolio reads.

**Read-only by default.** The CLI never sends orders. API client signed
order/cancel methods require explicit allow_trading=True, but this flag
does not replace risk approval. Cash/outcome risk, order persistence,
authoritative reconciliation, rate limits and realtime recovery remain
essential before unattended trading.

Internal instrument: SIG_EXCHANGE:<exchange_id>. Internal BID -> BUY YES
at p; ASK -> BUY NO at (1-p). SIG does **not** support post-only orders.
Set post_only=False explicitly only after accounting for crossing risk.

See [SIG integration](docs/SIG_PREDICTIONS.md).

### SIG single-order smoke (attended only)

After the read-only `--check` succeeds, use the safe GET-only preflight:

```bash
export SIG_PREDICTIONS_TOURNAMENT_ID='<actual tournament UUID>'
python -m apps.sig_order_smoke --exchange-id <real exchange ID>
```

The explicitly authorized live mode submits **BUY YES, 1 share @ 0.005 SUSQies**
and tries to cancel only that order. SIG has no post-only guarantee, so a fill
is still possible. The command refuses stale/invalid books or other open orders,
persists an idempotency key before POST, and stops on uncertain order state.

See [SIG order/cancel smoke runbook](docs/SIG_ORDER_SMOKE.md) for live flags,
recovery, and the remaining requirements before unattended trading.

### SIG-only environment (published Scalar SDK)

The published [Super Market Python SDK](https://pypi.org/project/super-market/)
is version-pinned in `requirements/sig_predictions.txt` as `super-market==0.1.0`.
The Python import is `super_market`. Neither core nor Hyperliquid installs it.

```bash
conda env create --format environment-yaml -f environment.sig_predictions.yml
conda activate market-maker
python -m pip check
# Existing proven standard-library HTTP check:
python -m apps.sig_predictions --check
# SDK-backed GET-only parity check (does not send or cancel orders):
python -m apps.sig_predictions --check --sdk
# Optional actual tournament and exchange (replace values from SIG):
python -m apps.sig_predictions --check --sdk --tournament-slug midterm-elections --exchange-id 36
```

For an existing core environment, use
`python -m pip install -r requirements/sig_predictions.txt`.
The SDK-backed adapter is initially **GET-only**. The attended signed
`apps.sig_order_smoke` still uses the existing guarded HTTP client, as the
Scalar SDK's write/error/partial-fill semantics require explicit parity
tests before swapping live transports. The generated SDK automatically
retries by default; the SDK-backed client forces `max_retries=0`.
