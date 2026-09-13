"""Unit tests for benchmark_service — buy-and-hold from daily OHLCV (SDK mocked)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

from src.shared.errors import InsufficientData, SdkError, ValidationError  # noqa: E402

TODAY = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _daily(days: int, first: float = 100.0, last: float = 120.0) -> dict:
    """`days`+1 daily closes ending today, linear from first to last."""
    step = (last - first) / days
    return {"success": True, "symbol": "ETH", "data_points": days + 1, "data": [
        {"timestamp": (TODAY - timedelta(days=days - i)).strftime("%Y-%m-%d %H:%M:%S+00:00"),
         "close": first + i * step}
        for i in range(days + 1)
    ]}


@pytest.fixture
def sdk(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr("src.services.benchmark_service.mangrove_ai_client", lambda: client)
    return client


def test_trailing_window_return_and_coverage(sdk):
    from src.services.benchmark_service import get_benchmark

    resp = MagicMock()
    resp.model_dump.return_value = _daily(180)
    sdk.crypto_assets.get_ohlcv.return_value = resp
    out = get_benchmark("eth", lookback_days=180)

    assert out["asset"] == "ETH"
    assert out["buy_and_hold_return_pct"] == 20.0
    assert out["unit"] == "percent_0_100"
    assert out["requested_window"]["kind"] == "trailing"
    assert out["covered_window"]["bar_interval"] == "1d"
    assert out["covered_window"]["bars"] >= 180
    assert "note" not in out
    assert sdk.crypto_assets.get_ohlcv.call_args.kwargs["days"] >= 180


def test_explicit_window_slices_bars(sdk):
    from src.services.benchmark_service import get_benchmark

    sdk.crypto_assets.get_ohlcv.return_value = _daily(100, first=100.0, last=200.0)
    start = (TODAY - timedelta(days=50)).date().isoformat()
    end = (TODAY - timedelta(days=25)).date().isoformat()
    out = get_benchmark("ETH", start_date=start, end_date=end)

    # Closes on day 50 and day 75 of a 100 -> 200 line: 150 -> 175.
    assert out["first_close"] == pytest.approx(150.0)
    assert out["last_close"] == pytest.approx(175.0)
    assert out["buy_and_hold_return_pct"] == pytest.approx(16.6667, abs=1e-3)
    assert out["requested_window"]["kind"] == "explicit"


def test_short_history_is_reported_not_hidden(sdk):
    from src.services.benchmark_service import get_benchmark

    sdk.crypto_assets.get_ohlcv.return_value = _daily(30)
    out = get_benchmark("ETH", lookback_days=365)
    assert out["covered_window"]["days"] == 30
    assert "note" in out and "30" in out["note"]


def test_fewer_than_two_closes_is_insufficient(sdk):
    from src.services.benchmark_service import get_benchmark

    sdk.crypto_assets.get_ohlcv.return_value = {"success": True, "data": []}
    with pytest.raises(InsufficientData):
        get_benchmark("ETH", lookback_days=30)


@pytest.mark.parametrize("kwargs", [
    {},
    {"start_date": "2026-01-01"},
    {"start_date": "2026-02-01", "end_date": "2026-01-01"},
    {"start_date": "yesterday", "end_date": "today"},
    {"lookback_days": 0},
])
def test_bad_windows_are_validation_errors(sdk, kwargs):
    from src.services.benchmark_service import get_benchmark

    with pytest.raises(ValidationError):
        get_benchmark("ETH", **kwargs)
    sdk.crypto_assets.get_ohlcv.assert_not_called()


def test_provider_failure_is_sdk_error(sdk):
    from src.services.benchmark_service import get_benchmark

    sdk.crypto_assets.get_ohlcv.side_effect = RuntimeError("provider down")
    with pytest.raises(SdkError):
        get_benchmark("ETH", lookback_days=30)


def test_benchmark_for_window_never_raises(sdk):
    from src.services.benchmark_service import benchmark_for_window

    sdk.crypto_assets.get_ohlcv.side_effect = RuntimeError("provider down")
    out = benchmark_for_window("ETH", {"lookback_months": 3})
    assert out["available"] is False
    assert "provider down" in out["reason"]
    assert benchmark_for_window("ETH", None)["available"] is False


def test_benchmark_for_window_uses_months_when_no_dates(sdk):
    from src.services.benchmark_service import benchmark_for_window

    sdk.crypto_assets.get_ohlcv.return_value = _daily(90)
    out = benchmark_for_window("ETH", {"lookback_months": 3, "start_date": None, "end_date": None})
    assert out["available"] is True
    assert out["requested_window"]["days"] == 90.0
