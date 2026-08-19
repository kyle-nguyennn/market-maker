from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def make_experiment_filename(strategy: str, seed: int, n_steps: int, instrument: str, extra: str | None = None) -> str:
    label = f"{strategy}_seed{seed}_steps{n_steps}_{instrument}"
    if extra:
        label = f"{label}_{extra}"
    return f"{label}.png"


def plot_akuna_quote_stream(
    stream: list[dict[str, Any]],
    *,
    strategy: str,
    seed: int,
    n_steps: int,
    instrument: str,
    output_dir: str = "img",
    extra: str | None = None,
) -> str:
    """Plot quote mid/bid/ask over time along with the synthetic underlying movement.

    The resulting filename is designed to be traceable back to the config that
    produced the experiment.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    steps = [row["step"] for row in stream]
    mids = [row["state"].mid_price for row in stream]
    bids = [row["bid"].price for row in stream]
    asks = [row["ask"].price for row in stream]

    underlying_series = []
    for row in stream:
        state = row["state"]
        underlying_series.append({
            "fed": state.best_bid,
            "ajarai": state.best_ask,
            "theori": state.best_bid + state.best_ask,
        })

    fig, axes = plt.subplots(2, 1, figsize=(10, 8), sharex=True)

    axes[0].plot(steps, mids, label="mid", color="tab:blue", linewidth=2)
    axes[0].plot(steps, bids, label="bid", linestyle="--", color="tab:green")
    axes[0].plot(steps, asks, label="ask", linestyle="--", color="tab:red")
    axes[0].set_title(f"{strategy} quotes for {instrument}")
    axes[0].set_ylabel("Price")
    axes[0].legend()
    axes[0].grid(alpha=0.25)

    fed = [row["state"].best_bid for row in stream]
    ajarai = [row["state"].best_ask for row in stream]
    theori = [row["state"].best_bid + row["state"].best_ask for row in stream]
    axes[1].plot(steps, fed, label="fed_funds_rate", color="tab:purple")
    axes[1].plot(steps, ajarai, label="ajarai_valuation", color="tab:orange")
    axes[1].plot(steps, theori, label="theori_valuation", color="tab:brown")
    axes[1].set_title("Synthetic underlying movement")
    axes[1].set_xlabel("Step")
    axes[1].set_ylabel("Level")
    axes[1].legend()
    axes[1].grid(alpha=0.25)

    fig.tight_layout()

    filename = make_experiment_filename(strategy, seed, n_steps, instrument, extra=extra)
    filepath = output_path / filename
    fig.savefig(filepath, dpi=200)
    plt.close(fig)
    return str(filepath)
