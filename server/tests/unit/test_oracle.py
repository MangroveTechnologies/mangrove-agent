"""Unit tests for oracle_service — SIEVE scoring, data query, backtest.

Mocks `mangrove_ai_client()` so we exercise the orchestration without
hitting MangroveAI's prod proxy. Integration tests live in
tests/integration/ and require a real API key + the live proxy.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

from src.services.oracle import (  # noqa: E402
    DataQueryInput,
    OracleBacktestInput,
    SieveScoreInput,
)
from src.services.oracle import (
    backtest as svc_backtest,
)
from src.services.oracle import (
    data_query as svc_data_query,
)
from src.services.oracle import (
    sieve_score as svc_sieve_score,
)
from src.services.oracle import (
    validate_experiment as svc_validate_experiment,
)
from src.shared.errors import SdkError  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _strategy() -> dict[str, Any]:
    return {
        "asset": "AVAX",
        "entry": [
            {
                "name": "bb_upper_breakout",
                "signal_type": "TRIGGER",
                "params": {"window": 95},
            }
        ],
        "exit": [
            {
                "name": "macd_bearish_cross",
                "signal_type": "TRIGGER",
                "params": {"fast": 12, "slow": 26},
            }
        ],
    }


_SIEVE_RESPONSE_MOCK = MagicMock()
_SIEVE_RESPONSE_MOCK.count = 1
_SIEVE_RESPONSE_MOCK.model_version = "mangrove-sieve:0b9a2da0d827"
_SIEVE_RESPONSE_MOCK.code_version = "oracle:v0.14.2 ai:v3.10.2 kb:1.0.5 roots:v0.3.0"
_SIEVE_RESPONSE_MOCK.model_dump.return_value = {
    "predictions": [
        {
            "binary": {"p_no_trades": 0.0, "p_trades": 0.9999},
            "four_class": {"losing": 0.24, "no_trades": 0.0, "wash": 0.06, "winning": 0.70},
        }
    ],
    "count": 1,
    "model_version": "mangrove-sieve:0b9a2da0d827",
    "code_version": "oracle:v0.14.2 ai:v3.10.2 kb:1.0.5 roots:v0.3.0",
}


_DATA_QUERY_RESPONSE_MOCK = MagicMock()
_DATA_QUERY_RESPONSE_MOCK.row_count = 1
_DATA_QUERY_RESPONSE_MOCK.code_version = "oracle:v0.14.2 ai:v3.10.2 kb:1.0.5 roots:v0.3.0"
_DATA_QUERY_RESPONSE_MOCK.model_dump.return_value = {
    "rows": [{"asset": "BTC", "irr_annualized": 52.3}],
    "row_count": 1,
    "table": "results",
    "code_version": "oracle:v0.14.2 ai:v3.10.2 kb:1.0.5 roots:v0.3.0",
}


_BACKTEST_RESPONSE_MOCK = MagicMock()
_BACKTEST_RESPONSE_MOCK.success = True
_BACKTEST_RESPONSE_MOCK.trade_count = 12
_BACKTEST_RESPONSE_MOCK.model_dump.return_value = {
    "success": True,
    "metrics": {"sharpe_ratio": 1.5, "total_return": 22.3},
    "trade_count": 12,
    "strategy_names": ["AVAX bb breakout"],
}


# ---------------------------------------------------------------------------
# sieve_score
# ---------------------------------------------------------------------------

class TestSieveScore:
    def test_returns_predictions_with_provenance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.oracle.sieve_score.return_value = _SIEVE_RESPONSE_MOCK
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)

        result = svc_sieve_score(SieveScoreInput(strategies=[_strategy()]))

        assert result["count"] == 1
        assert result["model_version"] == "mangrove-sieve:0b9a2da0d827"
        assert "oracle:v0.14.2" in result["code_version"]
        assert result["predictions"][0]["four_class"]["winning"] == pytest.approx(0.70)

    def test_empty_strategies_rejected_locally(self) -> None:
        with pytest.raises(SdkError, match="at least one strategy"):
            svc_sieve_score(SieveScoreInput(strategies=[]))

    def test_client_side_99_cap_surfaces_as_sdk_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.oracle.sieve_score.side_effect = ValueError("Max 99 items per request, got 100")
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)

        with pytest.raises(SdkError, match="validation failed"):
            svc_sieve_score(SieveScoreInput(strategies=[_strategy()] * 100))


# ---------------------------------------------------------------------------
# data_query
# ---------------------------------------------------------------------------

class TestDataQuery:
    def test_returns_rows_with_provenance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.oracle.data_query.return_value = _DATA_QUERY_RESPONSE_MOCK
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)

        result = svc_data_query(
            DataQueryInput(
                table="results",
                select=["asset", "irr_annualized"],
                filters=[{"col": "irr_annualized", "op": ">=", "value": 50}],
                limit=5,
            )
        )

        assert result["row_count"] == 1
        assert result["rows"][0]["asset"] == "BTC"
        assert "oracle:v0.14.2" in result["code_version"]


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------

class TestBacktest:
    def test_returns_metrics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.oracle.backtest.return_value = _BACKTEST_RESPONSE_MOCK
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)

        result = svc_backtest(
            OracleBacktestInput(
                asset="AVAX",
                interval="1h",
                strategy_json="{}",
                lookback_months=6,
            )
        )

        assert result["success"] is True
        assert result["metrics"]["sharpe_ratio"] == pytest.approx(1.5)
        assert result["trade_count"] == 12


# ---------------------------------------------------------------------------
# validate_experiment — must return the server's {valid, total_runs, ...}
# shape directly, NOT route through the SDK's ExperimentStatus model
# (mangroveai <= 1.5.0 mistypes this endpoint and crashes on a 200).
# ---------------------------------------------------------------------------

class TestValidateExperiment:
    def test_returns_raw_validation_result_via_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        # Real server shape (verified live against Oracle v0.15.3):
        client.oracle._core.request.return_value.json.return_value = {
            "valid": True,
            "total_runs": 12,
            "errors": [],
            "warnings": [],
        }
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)

        result = svc_validate_experiment("exp_20260606T011615775405Z")

        # The service must NOT call the mistyped SDK method...
        client.oracle.validate_experiment.assert_not_called()
        # ...and must POST straight to the validate endpoint via the transport.
        client.oracle._core.request.assert_called_once_with(
            "POST", "/oracle/experiments/exp_20260606T011615775405Z/validate"
        )
        assert result == {
            "valid": True,
            "total_runs": 12,
            "errors": [],
            "warnings": [],
        }

    def test_surfaces_invalid_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MagicMock()
        client.oracle._core.request.return_value.json.return_value = {
            "valid": False,
            "total_runs": 0,
            "errors": ["No entry filter signals selected"],
            "warnings": [],
        }
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)

        result = svc_validate_experiment("exp_x")

        assert result["valid"] is False
        assert "No entry filter signals selected" in result["errors"]
