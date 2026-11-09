"""CLI end-to-end tests using local CSV only, no SDK keys or market requests."""
import json
from decimal import Decimal as D

import pytest

from apps.backtest_alpha import _as_ms, main


def csv_fixture(tmp_path):
    path = tmp_path / "eight-bars.csv"
    lines = ["symbol,end_ms,available_at_ms,open,high,low,close,volume"]
    for i, price in enumerate(range(101, 109), start=1):
        ms = i * 60_000
        lines.append(f"AAPL,{ms},{ms + 1000},{price},{price},{price},{price},1000")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def args(csv_path):
    return [
        "--csv", str(csv_path), "--source", "fixture-v1", "--symbol", "AAPL",
        "--lookback-bars", "3", "--strategy", "momentum",
        "--min-edge", "0", "--target-weight", "0.5",
        "--forecast-horizon-ms", "600000",
    ]


def test_cli_generates_reproducible_report_and_csv_ledgers(tmp_path, capsys):
    path = csv_fixture(tmp_path)
    export = tmp_path / "reports"
    argv = args(path) + ["--out-dir", str(export)]
    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["bars"] == 8
    assert first["decisions"] == 5
    assert first["fills"] >= 1
    assert first["point_in_time_certified"] is False
    assert first["fill_assumption"] == "first_future_bar_open_after_signal_and_latency"
    assert D(first["initial_equity"]) == D("100000")
    assert D(first["total_fees"]) > 0
    assert (export / "equity.csv").exists()
    assert (export / "fills.csv").exists()
    assert len((export / "equity.csv").read_text().splitlines()) == 9
    assert len((export / "fills.csv").read_text().splitlines()) >= 2
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out) == first


def test_cli_cash_control_is_flat(tmp_path, capsys):
    path = csv_fixture(tmp_path)
    argv = args(path)
    i = argv.index("momentum")
    argv[i] = "cash"
    assert main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["strategy"] == "cash"
    assert result["fills"] == 0
    assert D(result["net_pnl"]) == D("0")
    assert D(result["final_equity"]) == D("100000")


def test_cli_mean_reversion_emits_short_targets(tmp_path, capsys):
    path = csv_fixture(tmp_path)
    argv = args(path)
    argv[argv.index("momentum")] = "mean_reversion"
    assert main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["strategy"] == "mean_reversion"
    assert result["fills"] >= 1
    assert D(result["net_pnl"]) < 0


def test_cli_csv_symbol_mismatch_fails_closed(tmp_path):
    path = csv_fixture(tmp_path)
    argv = args(path)
    argv[argv.index("AAPL")] = "MSFT"
    with pytest.raises(ValueError, match="different symbol"):
        main(argv)


def test_alpaca_requires_real_minute_interval_before_reading_keys():
    with pytest.raises(ValueError, match="only minute bars"):
        main([
            "--alpaca", "--symbol", "AAPL", "--source", "dataset-v1",
            "--start-utc", "2026-10-01T00:00:00+00:00",
            "--end-utc", "2026-10-10T00:00:00+00:00",
            "--interval-ms", "120000",
        ])


def test_cli_rejects_timezone_naive_dates():
    with pytest.raises(ValueError, match="explicit UTC offsets"):
        _as_ms("2026-10-01T14:30:00")
    assert _as_ms("1970-01-01T08:00:00+08:00") == 0
