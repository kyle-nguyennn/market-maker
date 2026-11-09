"""Host market-data readers normalize observations; alpha never owns I/O."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from alpha import FeatureConfig, FeatureEngine, PriceBar, replay_bars
from alpha.observations import BarProvider
from market_handler.historical_bar_reader import read_price_bars_csv


def test_csv_host_ingestion_and_live_publication_share_feature_api(tmp_path):
    path = tmp_path / "bars.csv"
    path.write_text(
        "symbol,end_ms,available_at_ms,open,high,low,close,volume\n"
        "AAPL,1000,1020,100,100,100,100,1.0000000000000000001\n"
        "AAPL,2000,2020,101,101,101,101,2\n"
        "AAPL,3000,3020,102,102,102,102,3\n"
        "AAPL,4000,4020,103,103,103,103,4\n",
        encoding="utf-8",
    )
    historical = read_price_bars_csv(
        path, interval_ms=1000, source="example-vendor-adjusted-v1",
    )
    assert all(isinstance(item, PriceBar) for item in historical)
    assert historical[0].volume == Decimal("1.0000000000000000001")
    cfg = FeatureConfig(
        interval_ms=1000, lookback_bars=3, max_staleness_ms=1000,
        feature_version="price-v1",
    )
    replay_engine = FeatureEngine(symbol="AAPL", config=cfg)
    live_engine = FeatureEngine(symbol="AAPL", config=cfg)
    replay_result = replay_bars(historical, replay_engine)
    live_result = tuple(
        snap for bar in historical
        if (snap := live_engine.on_bar(
            bar, at_ms=bar.available_at_ms
        )) is not None
    )
    assert live_result == replay_result
    assert replay_result[0].values["momentum"] == Decimal("0.03")


def test_alpha_exports_only_observation_port_not_host_reader():
    import alpha
    import alpha.observations
    assert alpha.PriceBar is alpha.observations.PriceBar
    assert not hasattr(alpha, "read_price_bars_csv")
    assert not hasattr(alpha.observations, "read_price_bars_csv")
    for source_path in Path(alpha.__file__).parent.glob("*.py"):
        src = source_path.read_text(encoding="utf-8")
        assert "from market_handler" not in src
        assert "import market_handler" not in src
        assert "from exchanges" not in src
    assert BarProvider.__module__ == "alpha.observations"


def test_csv_validation_stays_in_host_layer(tmp_path):
    path = tmp_path / "invalid.csv"
    path.write_text("symbol,end_ms,available_at_ms,open,high,low,close,volume\n"
                    "AAPL,1000,1020,100,100,100,100,1\n",
                    encoding="utf-8")
    for interval, source in [(True, "vendor-v1"), (1000, " ")]:
        try:
            read_price_bars_csv(path, interval_ms=interval, source=source)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid host reader config unexpectedly accepted")
