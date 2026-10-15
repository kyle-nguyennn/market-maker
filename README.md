# market-maker

Venue-independent market-making framework with separate exchange adapters.

## Install with Conda

Run these commands **from the repository root**. Choose the environment for
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
conda env create -f environment.hyperliquid.yml
conda activate market-maker
```

Installs `requirements/common.txt` and
`hyperliquid-python-sdk==0.24.0` through
`requirements/hyperliquid.txt`. No exchange SDK is installed by
the core-only environment.

To add Hyperliquid to an existing core environment without recreating it:

```bash
conda env update -n market-maker -f environment.hyperliquid.yml
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
