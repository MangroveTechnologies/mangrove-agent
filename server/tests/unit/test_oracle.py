"""Unit tests for oracle_service — SIEVE scoring, data query, backtest.

Mocks `mangrove_ai_client()` so we exercise the orchestration without
hitting MangroveAI's prod proxy. Integration tests live in
tests/integration/ and require a real API key + the live proxy.
"""
from __future__ import annotations

import json
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
    pause_experiment as svc_pause_experiment,
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


# ---------------------------------------------------------------------------
# pause_experiment — same SDK contract bug as validate: the server returns
# {"status": "paused"} with NO experiment_id, but the SDK types it as
# ExperimentStatus (experiment_id required) and crashes. The service must
# read the body via the transport directly.
# ---------------------------------------------------------------------------

class TestPauseExperiment:
    def test_returns_raw_status_via_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = MagicMock()
        client.oracle._core.request.return_value.json.return_value = {"status": "paused"}
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)

        result = svc_pause_experiment("exp_20260606T011615775405Z")

        # Must NOT route through the mistyped SDK method...
        client.oracle.pause_experiment.assert_not_called()
        # ...and must POST straight to the pause endpoint via the transport.
        client.oracle._core.request.assert_called_once_with(
            "POST", "/oracle/experiments/exp_20260606T011615775405Z/pause"
        )
        assert result == {"status": "paused"}


class TestLaunchExperimentPollOn504:
    """WS4 (MangroveOracle#296): launch is non-idempotent; a gateway 504 may have
    succeeded server-side, so the service confirms via polling instead of failing
    or re-launching."""

    def test_happy_path_returns_status_without_polling(self, monkeypatch):
        from src.services.oracle import launch_experiment

        client = MagicMock()
        result = MagicMock()
        result.model_dump.return_value = {"status": "preparing", "experiment_id": "e1", "total_runs": 72}
        client.oracle.launch_experiment.return_value = result
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)

        body = launch_experiment("e1")
        assert body["status"] == "preparing"
        client.oracle.get_experiment.assert_not_called()  # no poll on success

    def test_gateway_504_confirms_via_poll(self, monkeypatch):
        from mangrove_ai.exceptions import APIError

        from src.services.oracle import launch_experiment

        client = MagicMock()
        client.oracle.launch_experiment.side_effect = APIError(504, "timeout", "gw", "GATEWAY_TIMEOUT")
        # first poll still validated, then advanced to preparing
        client.oracle.get_experiment.side_effect = [
            {"status": "validated"},
            {"status": "preparing", "total_runs": 72},
        ]
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)
        monkeypatch.setattr("src.services.oracle.time.sleep", lambda *_: None)

        body = launch_experiment("e1")
        assert body["status"] == "preparing"
        assert body["confirmed_via"] == "poll"
        assert client.oracle.launch_experiment.call_count == 1  # never re-launched

    def test_non_gateway_error_reraises(self, monkeypatch):
        from mangrove_ai.exceptions import APIError

        from src.services.oracle import launch_experiment

        client = MagicMock()
        client.oracle.launch_experiment.side_effect = APIError(400, "bad", "nope", "BAD")
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)

        with pytest.raises(APIError):
            launch_experiment("e1")
        client.oracle.get_experiment.assert_not_called()


# ---------------------------------------------------------------------------
# oracle_backtest execution_config canon merge
# Regression: a minimal/empty execution_config previously 500-ed with
# "position_size_calc is required in execution_config" (MangroveAI v3.8.0).
# The raw oracle backtest path must merge canonical defaults like the
# registered-strategy path does.
# ---------------------------------------------------------------------------

class TestBacktestExecutionConfigMerge:
    def _force_fallback_canon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Avoid a live trading-defaults fetch; use the hardcoded fallback,
        # which now carries position_size_calc.
        from src.services import backtest_service as bs
        monkeypatch.setattr(bs, "_get_trading_defaults", lambda: bs._FALLBACK_TRADING_DEFAULTS)

    def _mock_client(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        client = MagicMock()
        client.oracle.backtest.return_value = _BACKTEST_RESPONSE_MOCK
        monkeypatch.setattr("src.services.oracle.mangrove_ai_client", lambda: client)
        return client

    def test_empty_config_gets_canon_position_size_calc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._force_fallback_canon(monkeypatch)
        client = self._mock_client(monkeypatch)

        svc_backtest(OracleBacktestInput(
            asset="XRP", interval="1h",
            strategy_json=json.dumps(_strategy()),
            execution_config={},
        ))

        sent = client.oracle.backtest.call_args.args[0]
        assert sent.execution_config["position_size_calc"] == "v2"
        assert "max_risk_per_trade" in sent.execution_config  # full canon merged

    def test_none_config_merges_canon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._force_fallback_canon(monkeypatch)
        client = self._mock_client(monkeypatch)

        svc_backtest(OracleBacktestInput(
            asset="XRP", interval="1h",
            strategy_json=json.dumps(_strategy()),
        ))

        sent = client.oracle.backtest.call_args.args[0]
        assert sent.execution_config["position_size_calc"] == "v2"

    def test_caller_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._force_fallback_canon(monkeypatch)
        client = self._mock_client(monkeypatch)

        svc_backtest(OracleBacktestInput(
            asset="XRP", interval="1h",
            strategy_json=json.dumps(_strategy()),
            execution_config={"position_size_calc": "v9", "reward_factor": 5},
        ))

        sent = client.oracle.backtest.call_args.args[0]
        assert sent.execution_config["position_size_calc"] == "v9"
        assert sent.execution_config["reward_factor"] == 5

    def test_fallback_canon_includes_position_size_calc(self) -> None:
        from src.services.backtest_service import _FALLBACK_TRADING_DEFAULTS
        assert _FALLBACK_TRADING_DEFAULTS["risk_management"]["position_size_calc"] == "v2"
