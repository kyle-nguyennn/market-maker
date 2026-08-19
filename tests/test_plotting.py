from utils.plotting import make_experiment_filename, plot_akuna_quote_stream


def test_make_experiment_filename_is_traceable():
    filename = make_experiment_filename("akuna_binary_option", 42, 10, "AJARAI_UP", extra="baseline")

    assert filename == "akuna_binary_option_seed42_steps10_AJARAI_UP_baseline.png"


def test_plot_akuna_quote_stream_writes_file(tmp_path):
    stream = [
        {
            "step": 0,
            "state": type("State", (), {"mid_price": 50, "best_bid": 48, "best_ask": 52})(),
            "bid": type("Bid", (), {"price": 48, "qty": 5})(),
            "ask": type("Ask", (), {"price": 52, "qty": 4})(),
        },
        {
            "step": 1,
            "state": type("State", (), {"mid_price": 51, "best_bid": 49, "best_ask": 53})(),
            "bid": type("Bid", (), {"price": 49, "qty": 6})(),
            "ask": type("Ask", (), {"price": 53, "qty": 5})(),
        },
    ]

    path = plot_akuna_quote_stream(
        stream,
        strategy="akuna_binary_option",
        seed=42,
        n_steps=2,
        instrument="AJARAI_UP",
        output_dir=str(tmp_path),
        extra="baseline",
    )

    assert path.endswith("akuna_binary_option_seed42_steps2_AJARAI_UP_baseline.png")
