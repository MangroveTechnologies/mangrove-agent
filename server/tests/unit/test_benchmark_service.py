"""Benchmark adapters preserve server results without recomputing returns."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.services.benchmark_service import benchmark_for_window, get_benchmark
from src.shared.errors import SdkError, ValidationError


@pytest.fixture
def sdk(monkeypatch):
    client = MagicMock()
    now = datetime.now(timezone.utc)
    client.backtesting.get_benchmark.return_value = {
        "asset": "ETH", "start": (now - timedelta(days=30)).isoformat(),
        "end": now.isoformat(), "bars": 31, "first_close": 100., "last_close": 110.,
        "buy_and_hold_return_raw": 10., "buy_and_hold_return": "10.0%",
        "unit": "percent_0_100", "interval": "1d", "partial": False,
        "base_token": "ETH", "quote_token": "USD", "market_data_venue": "KRAKEN",
    }
    monkeypatch.setattr("src.services.benchmark_service.mangrove_ai_client", lambda: client)
    return client


def test_return_is_server_authoritative_and_keeps_provenance(sdk):
    sdk.backtesting.get_benchmark.return_value["buy_and_hold_return_raw"] = 9.875
    result = get_benchmark("eth", lookback_days=30)
    assert result["buy_and_hold_return_pct"] == 9.875
    assert result["market_data_venue"] == "KRAKEN"
    assert result["covered_window"]["bars"] == 31
    assert result["requested_window"]["kind"] == "trailing"
    sdk.backtesting.get_benchmark.assert_called_once()
    sdk.crypto_assets.get_ohlcv.assert_not_called()


def test_explicit_window_is_forwarded_in_utc(sdk):
    get_benchmark("ETH", "2026-01-01T01:00:00+01:00", "2026-02-01")
    sdk.backtesting.get_benchmark.assert_called_once_with(
        "ETH", "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00")


def test_partial_coverage_is_preserved(sdk):
    sdk.backtesting.get_benchmark.return_value["partial"] = True
    result = get_benchmark("ETH", lookback_days=365)
    assert result["partial"] is True
    assert "30.0" in result["note"]


@pytest.mark.parametrize("kwargs", [{}, {"start_date": "2026-01-01"},
    {"start_date": "2026-02-01", "end_date": "2026-01-01"},
    {"lookback_days": 0}, {"lookback_days": True}, {"lookback_days": 2.5},
    {"start_date": "2026-01-01", "end_date": "2026-02-01", "lookback_days": 10}])
def test_invalid_window_does_not_call_server(sdk, kwargs):
    with pytest.raises(ValidationError):
        get_benchmark("ETH", **kwargs)
    sdk.backtesting.get_benchmark.assert_not_called()


def test_provider_failure_is_sanitized(sdk):
    sdk.backtesting.get_benchmark.side_effect = RuntimeError("secret-token")
    with pytest.raises(SdkError) as error:
        get_benchmark("ETH", lookback_days=30)
    assert "secret-token" not in str(error.value)


def test_invalid_units_are_rejected(sdk):
    sdk.backtesting.get_benchmark.return_value["unit"] = "fraction"
    with pytest.raises(SdkError):
        get_benchmark("ETH", lookback_days=30)


def test_optional_benchmark_failure_keeps_backtest_available(sdk):
    sdk.backtesting.get_benchmark.side_effect = RuntimeError("private")
    result = benchmark_for_window("ETH", {"lookback_months": 3})
    assert result["available"] is False
    assert "private" not in str(result)


def test_optional_benchmark_without_window_does_not_fetch(sdk):
    assert benchmark_for_window("ETH", None)["available"] is False
    sdk.backtesting.get_benchmark.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_access_failure_preserves_status_without_fallback(sdk, status):
    from mangrove_ai.exceptions import APIError

    from src.shared.errors import UpstreamAccessError

    sdk.backtesting.get_benchmark.side_effect = APIError(status_code=status, error="denied", code="DENIED", message="private")
    with pytest.raises(UpstreamAccessError) as error:
        get_benchmark("ETH", lookback_days=30)
    assert error.value.http_status == status
    assert error.value.to_dict()["retry_payment"] is False
    sdk.backtesting.get_benchmark.assert_called_once()
    sdk.crypto_assets.get_ohlcv.assert_not_called()


def test_recorded_market_is_forwarded(sdk):
    benchmark_for_window("ETH", {
        "start_date": "2026-01-01", "end_date": "2026-02-01",
        "base_token": "ETH", "quote_token": "USD", "market_data_venue": "KRAKEN",
    })
    assert sdk.backtesting.get_benchmark.call_args.kwargs == {
        "base_token": "ETH", "quote_token": "USD", "market_data_venue": "KRAKEN",
    }


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
def test_invalid_numeric_return_is_rejected(sdk, value):
    sdk.backtesting.get_benchmark.return_value["buy_and_hold_return_raw"] = value
    with pytest.raises(SdkError):
        get_benchmark("ETH", lookback_days=30)
