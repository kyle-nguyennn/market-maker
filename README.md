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
# Edit .env: PERPETUAL_ALPHA_DEX=par (practice) or pa (scored)
python -m apps.perpetual_alpha
python -m apps.perpetual_alpha --check
~~~

Already exported shell variables take precedence over the local .env file.
Use --dex to override only the DEX for a single run, or unset the shell
variable if you want the .env value to take effect.

You can override the environment selection for a single read-only command:

~~~bash
python -m apps.perpetual_alpha --check --dex par
python -m apps.perpetual_alpha --check --dex pa
~~~

| Setting | Practice | Scored |
| --- | --- | --- |
| DEX selector | par | pa |
| Network | Hyperliquid testnet | Hyperliquid testnet |
| REST host | api.hyperliquid-testnet.xyz | api.hyperliquid-testnet.xyz |
| DEX prefix | par: | pa: |
| Collateral | USDPR | USDP |

The shared config is loaded via HyperliquidVenueConfig.from_environment()
and passed to both HyperliquidVenueClient and HyperliquidMarketAdapter. The
client uses that host for REST, SDK-signed Exchange and SDK WebSocket connections,
and every market/account query uses the corresponding DEX. Invalid combinations
(e.g., pa with the mainnet URL, or pa with USDPR) raise ValueError early.

The diagnostics are read-only: no API private key is loaded or required.
PERPETUAL_ALPHA_PA_ALLOW_TRADING=false blocks signed pa operations even when an
agent key is available. Do **not** enable pa trading until its deployment,
endpoint, account funding, agent approval, risk limits and execution tests have
been verified. Confirm the approved agent can trade on the selected DEX; the same testnet network does not imply separate scored collateral is funded.

Network connectivity alone does not establish that a team's scored USDP
balance is funded. If the read-only pa diagnostic reports no USDP, contact
the organizers and do not attempt trading.

**Security:** Never commit .env or private keys. Revoke any API key previously
exposed in a screenshot and replace it with a fresh approved agent key.

**Live API discovery on 2026-10-15:** querying the testnet API with dex=pa returned
pa:BTC, pa:ETH and pa:HYPE. Mainnet dex=pa returned HTTP 500. The organizer's
announced ten-asset universe was not yet fully visible on pa; re-check
metadata before the scored competition begins. At the October 15 probe,
pa:BTC market metadata and contexts were accessible, but the order book had
no valid two-sided quote. That is not an execution-ready market.
