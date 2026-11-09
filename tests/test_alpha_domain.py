"""P1 alpha-domain contract tests: offline only; no exchange/network calls."""
from dataclasses import FrozenInstanceError
from decimal import Decimal as D

import pytest

from alpha import AlphaSignal, AlphaStrategy, FeatureSnapshot, TargetExposure


def snapshot(**changes):
    return FeatureSnapshot(**{
        "snapshot_id": "feed-AAPL-0001", "symbol": "AAPL",
        "observed_at_ms": 1_000, "available_at_ms": 1_050,
        "lookback_ms": 300_000, "source": "historical_equities_v1",
        "feature_version": "features-v1", "values": {"momentum": D("0.0123456789012345")},
        **changes,
    })


def signal(**changes):
    return AlphaSignal(**{
        "signal_id": "signal-1", "snapshot_id": "feed-AAPL-0001",
        "symbol": "AAPL", "strategy": "trend_following",
        "model_version": "v1", "generated_at_ms": 1_100,
        "feature_available_at_ms": 1_050, "horizon_ms": 1_800_000,
        "expected_return": D("0.002"), "confidence": D("0.78"),
        "regime": "TRENDING_UP", **changes,
    })


def target(**changes):
    return TargetExposure(**{
        "decision_id": "decision-1", "symbol": "AAPL",
        "as_of_ms": 1_100, "horizon_ms": 1_800_000,
        "target_weight": D("0.15"), "expected_return": D("0.002"),
        "confidence": D("0.78"), "strategy": "trend_following",
        "signal_ids": ("signal-1",), "regime": "TRENDING_UP", **changes,
    })


def test_snapshot_copies_and_freezes_features_without_decimal_rounding():
    source = {"momentum": D("0.123456789012345678901"), "vol": D("0.01")}
    item = snapshot(values=source)
    source["momentum"] = D("999")
    assert item.values["momentum"] == D("0.123456789012345678901")
    assert list(item.values) == ["momentum", "vol"]
    with pytest.raises(TypeError):
        item.values["momentum"] = D("2")
    with pytest.raises(FrozenInstanceError):
        item.symbol = "MSFT"


@pytest.mark.parametrize("changes", [
    {"observed_at_ms": True}, {"observed_at_ms": -1},
    {"available_at_ms": 999}, {"lookback_ms": 0},
    {"lookback_ms": 1.0}, {"snapshot_id": ""}, {"symbol": " AAPL "},
    {"source": ""}, {"feature_version": " "}, {"values": {}},
    {"values": {"bad": 0.1}}, {"values": {"bad": D("NaN")}},
    {"values": {1: D("1")}}, {"values": {"": D("1")}},
])
def test_snapshot_rejects_invalid_fields(changes):
    with pytest.raises(ValueError):
        snapshot(**changes)


def test_signal_preserves_signed_return_and_model_lineage():
    item = signal(expected_return=D("-0.0000000000000000000000001"),
                  regime=None)
    assert item.expected_return < 0
    assert item.expected_return == D("-0.0000000000000000000000001")
    assert item.snapshot_id == "feed-AAPL-0001"
    with pytest.raises(FrozenInstanceError):
        item.strategy = "other"


@pytest.mark.parametrize("changes", [
    {"signal_id": ""}, {"strategy": " "}, {"model_version": ""},
    {"feature_available_at_ms": 1_101},
    {"horizon_ms": 0}, {"horizon_ms": True},
    {"generated_at_ms": -1}, {"expected_return": 0.2},
    {"expected_return": D("Infinity")},
    {"confidence": D("-0.1")}, {"confidence": D("1.01")},
    {"confidence": D("NaN")}, {"regime": ""},
])
def test_signal_rejects_invalid_fields(changes):
    with pytest.raises(ValueError):
        signal(**changes)


def test_target_is_desired_exposure_not_an_order_or_venue_size():
    long = target()
    short = target(target_weight=D("-0.10"))
    flat = target(target_weight=D("0"))
    leveraged = target(target_weight=D("1.5"))
    assert [i.target_weight for i in (long, short, flat, leveraged)] == [
        D("0.15"), D("-0.10"), D("0"), D("1.5")]
    assert long.signal_ids == ("signal-1",)
    assert not hasattr(long, "price")
    assert not hasattr(long, "exchange_order_id")


def test_multi_signal_ensemble_lineage_and_immutability():
    item = target(signal_ids=("signal-1", "signal-2"),
                  strategy="static_ensemble", regime=None)
    assert item.signal_ids == ("signal-1", "signal-2")
    with pytest.raises(FrozenInstanceError):
        item.signal_ids = ()


@pytest.mark.parametrize("changes", [
    {"decision_id": ""}, {"symbol": " "}, {"as_of_ms": False},
    {"horizon_ms": 0}, {"target_weight": float("nan")},
    {"target_weight": D("NaN")}, {"expected_return": D("Infinity")},
    {"confidence": 0.9}, {"confidence": D("-0.01")},
    {"confidence": D("1.0001")}, {"strategy": ""}, {"regime": ""},
    {"signal_ids": ()}, {"signal_ids": ["signal-1"]},
    {"signal_ids": ("signal-1", "signal-1")},
    {"signal_ids": ("",)},
])
def test_target_rejects_invalid_fields(changes):
    with pytest.raises(ValueError):
        target(**changes)


def test_pure_strategy_protocol_and_optional_abstention():
    class AbstainingStrategy:
        @property
        def strategy_id(self):
            return "baseline_abstain"

        def generate_signal(self, snapshot):
            assert snapshot.symbol == "AAPL"
            return None

    strategy = AbstainingStrategy()
    assert isinstance(strategy, AlphaStrategy)
    assert strategy.generate_signal(snapshot()) is None


def test_alpha_public_interface_does_not_import_venue_or_order_types():
    # Types stand alone in offline research without an adapter or SDK.
    from alpha import __all__
    assert {"AlphaSignal", "AlphaStrategy", "FeatureSnapshot", "TargetExposure"} <= set(__all__)
    assert {"PriceBar", "FeatureConfig", "build_price_features", "read_price_bars_csv"} <= set(__all__)
    assert "exchange" not in FeatureSnapshot.__module__
    assert "order" not in TargetExposure.__module__
