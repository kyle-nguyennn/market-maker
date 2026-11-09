"""No-lookahead and exact-Decimal tests for P2-A offline bar features."""
from dataclasses import FrozenInstanceError
from decimal import Decimal as D, localcontext

import pytest

from alpha.features import FeatureConfig, build_price_features
from alpha.market_data import PriceBar, read_price_bars_csv


def bar(index, price="100", **changes):
    price = D(price)
    fields = {
        "symbol": "AAPL", "source": "adjusted-bars-v1",
        "end_ms": index * 1000,
        "available_at_ms": index * 1000 + 20,
        "interval_ms": 1000,
        "open": price, "high": price, "low": price, "close": price,
        "volume": D("100.000000000000000001"),
    }
    fields.update(changes)
    return PriceBar(**fields)


def config(**changes):
    return FeatureConfig(**{
        "interval_ms": 1000, "lookback_bars": 3,
        "max_staleness_ms": 1500, "feature_version": "simple-price-v1",
        **changes,
    })


def make_bars():
    return [bar(1, "100"), bar(2, "101"), bar(3, "102"), bar(4, "103")]


def build(bars=None, **changes):
    return build_price_features(
        make_bars() if bars is None else bars,
        symbol=changes.pop("symbol", "AAPL"),
        decision_at_ms=changes.pop("decision_at_ms", 4100),
        config=changes.pop("config", config()),
        **changes,
    )


def test_exact_features_and_timestamp_provenance():
    snap = build()
    assert snap.symbol == "AAPL"
    assert snap.source == "adjusted-bars-v1"
    assert snap.feature_version == "simple-price-v1"
    assert snap.observed_at_ms == 4000
    assert snap.available_at_ms == 4020
    assert snap.lookback_ms == 3000
    assert snap.values["return_1"] == D(103) / D(102) - 1
    assert snap.values["momentum"] == D("0.03")
    assert snap.values["realized_vol"] > D("0")
    assert snap.values["price_zscore"] > D("0")
    assert set(snap.values) == {
        "return_1", "momentum", "realized_vol", "price_zscore",
    }


def test_input_order_and_future_rows_cannot_change_asof_snapshot():
    reference = build()
    shuffled = list(reversed(make_bars()))
    assert build(shuffled) == reference
    future = [bar(5, "9000"), bar(6, "1", source="future-vendor")]
    assert build(shuffled + future) == reference
    assert build(make_bars() + [bar(4, "999", available_at_ms=8000)]) == reference


def test_bar_available_after_decision_is_invisible_even_when_already_closed():
    bars = [bar(1), bar(2), bar(3), bar(4, available_at_ms=9000)]
    with pytest.raises(ValueError, match="insufficient eligible history"):
        build(bars)
    # An earlier late-published observation cannot be bypassed by filling
    # the hole from a later ready bar: refuse a discontinuous window.
    bars = [bar(1), bar(2, available_at_ms=9000), bar(3), bar(4), bar(5)]
    with pytest.raises(ValueError, match="non-contiguous"):
        build(bars, decision_at_ms=5100)


def test_after_late_bar_publishes_it_can_participate_in_replay():
    bars = [bar(1), bar(2), bar(3), bar(4),
            bar(5, available_at_ms=6500), bar(6, available_at_ms=6520)]
    pre = build(bars, decision_at_ms=6100, config=config(max_staleness_ms=3000))
    assert pre.observed_at_ms == 4000
    post = build(bars, decision_at_ms=6600)
    assert post.observed_at_ms == 6000
    assert post.available_at_ms == 6520
    assert pre.snapshot_id != post.snapshot_id


def test_duplicate_revision_rejected_once_both_versions_are_visible():
    bars = make_bars()
    earlier = build(bars + [bar(2, "123", available_at_ms=7000)])
    assert earlier == build()
    with pytest.raises(ValueError, match="duplicate available bars"):
        build(bars + [bar(2, "123", available_at_ms=7000)],
              decision_at_ms=7100, config=config(max_staleness_ms=4000))


@pytest.mark.parametrize("changes,match", [
    ({"source": "other-source"}, "mixed sources"),
    ({"interval_ms": 500}, "mixed sources or bar intervals"),
])
def test_rejects_mixed_source_or_interval_in_active_window(changes, match):
    bars = make_bars()
    # For 500 ms, a 4000 end is still valid; only the interval differs.
    bars[-1] = bar(4, **changes)
    with pytest.raises(ValueError, match=match):
        build(bars)


def test_missing_bar_or_stale_data_does_not_generate_synthetic_features():
    with pytest.raises(ValueError, match="non-contiguous"):
        build([bar(1), bar(2), bar(4), bar(5)], decision_at_ms=5100)
    with pytest.raises(ValueError, match="stale"):
        build(decision_at_ms=8000)


def test_flat_prices_have_zero_variance_and_zero_zscore():
    snap = build([bar(i) for i in range(1, 5)])
    assert snap.values == {
        "momentum": D("0"), "price_zscore": D("0"),
        "realized_vol": D("0"), "return_1": D("0"),
    }


def test_fixed_decimal_precision_even_if_caller_changes_global_context():
    bars = [bar(i, "1.000000000000000000001") for i in range(1, 5)]
    bars[-1] = bar(4, "1.000000000000000000002")
    first = build(bars)
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.rounding = "ROUND_DOWN"
        second = build(bars)
    assert second.snapshot_id == first.snapshot_id
    assert second.values == first.values
    assert first.values["return_1"] > 0


def test_asof_and_config_version_change_snapshot_identity():
    first = build()
    assert build(decision_at_ms=4200).snapshot_id != first.snapshot_id
    assert build(config=config(feature_version="simple-price-v2")).snapshot_id != first.snapshot_id
    revised = make_bars()
    revised[0] = bar(1, "100", volume=D("101"))
    assert build(revised).snapshot_id != first.snapshot_id


@pytest.mark.parametrize("kwargs", [
    {"interval_ms": 0}, {"interval_ms": True}, {"lookback_bars": 1},
    {"lookback_bars": False}, {"lookback_bars": 3.5},
    {"max_staleness_ms": -1}, {"max_staleness_ms": True},
    {"feature_version": " "},
])
def test_bad_config_rejected(kwargs):
    with pytest.raises(ValueError):
        config(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"end_ms": -1}, {"end_ms": True}, {"available_at_ms": 999},
    {"interval_ms": 0}, {"symbol": ""},
    {"source": " "}, {"close": D("0")}, {"volume": D("-1")},
    {"open": 100.0}, {"high": D("NaN")},
    {"low": D("101")}, {"high": D("99")},
])
def test_bad_bars_rejected(kwargs):
    with pytest.raises(ValueError):
        bar(1, **kwargs)


def test_immutable_bars_and_deterministic_csv_ingestion(tmp_path):
    path = tmp_path / "bars.csv"
    path.write_text(
        "symbol,end_ms,available_at_ms,open,high,low,close,volume\n"
        "AAPL,1000,1020,100,100,100,100,1.00000000000000000001\n"
        "AAPL,2000,2020,101,101,101,101,1\n"
        "AAPL,3000,3020,102,102,102,102,1\n"
        "AAPL,4000,4020,103,103,103,103,1\n",
        encoding="utf-8",
    )
    loaded = read_price_bars_csv(
        path, interval_ms=1000, source="vendor-dataset-revision-20261109"
    )
    assert type(loaded) is tuple and len(loaded) == 4
    assert loaded[0].volume == D("1.00000000000000000001")
    with pytest.raises(FrozenInstanceError):
        loaded[0].close = D("1")
    snap = build(loaded)
    assert snap.values["momentum"] == D("0.03")
    assert snap.source == "vendor-dataset-revision-20261109"


@pytest.mark.parametrize("contents", [
    "symbol,end_ms,open,high,low,close,volume\n",  # no availability
    "symbol,end_ms,available_at_ms,open,high,low,close,volume\n"
    "AAPL,1000,999,100,100,100,100,1\n",  # publish before bar end
    "symbol,end_ms,available_at_ms,open,high,low,close,volume\n"
    "AAPL,1000,1020,100,100,100,,1\n",  # blank price
    "symbol,end_ms,available_at_ms,open,high,low,close,volume\n"
    "AAPL,1000,1020,100,100,100,NaN,1\n",  # nonfinite
])
def test_malformed_csv_fails_closed(tmp_path, contents):
    path = tmp_path / "bad.csv"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        read_price_bars_csv(path, interval_ms=1000, source="vendor-v1")


def test_build_does_not_import_any_venue_or_send_orders():
    from alpha import features, market_data
    assert "alpharena" not in features.__dict__
    assert not hasattr(market_data, "place_order")
