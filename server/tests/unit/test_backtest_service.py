"""Unit tests for backtest_service — orchestration + filter/rank logic.

Integration against the dev Mangrove env lives in Task 5.2 E2E; this
module mocks the SDK so we can test the composition in isolation.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

from src.services.candidate_generator import StrategyCandidate  # noqa: E402


def _candidate(name: str = "c1") -> StrategyCandidate:
    return StrategyCandidate(
        name=name,
        asset="ETH",
        timeframe="1h",
        entry=[{"name": "macd_cross_up", "signal_type": "TRIGGER", "timeframe": "1h", "params": {}}],
        exit=[],
    )


def _fake_result(
    success: bool = True,
    irr: float = 0.5,
    win_rate: float = 0.6,
    total_trades: int = 20,
    sharpe: float = 1.2,
    max_dd: float = 0.1,
    net_pnl: float = 1500.0,
    trade_history: list | None = None,
    error: str | None = None,
) -> MagicMock:
    r = MagicMock()
    r.success = success
    r.metrics = {
        "irr_annualized": irr,
        "win_rate": win_rate,
        "total_trades": total_trades,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_dd,
        "net_pnl": net_pnl,
    }
    r.trade_count = total_trades
    r.trade_history = trade_history
    r.error = error
    return r


def _fake_status(status: str = "completed", trade_history: list | None = None,
                 error_message: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        metrics=None if status != "completed" else {
            "irr_annualized": 0.5, "win_rate": 0.6, "total_trades": 20,
            "sharpe_ratio": 1.2, "max_drawdown": 0.1, "net_pnl": 1500.0,
        },
        trade_history=trade_history,
        execution_time_seconds=1.5,
        error_message=error_message,
    )


@pytest.fixture
def mock_sdk(monkeypatch):
    sdk = MagicMock()
    monkeypatch.setattr(
        "src.services.backtest_service.mangrove_ai_client",
        lambda: sdk,
    )
    return sdk


def test_quick_backtest_returns_metrics(mock_sdk):
    from src.services.backtest_service import quick_backtest_all

    mock_sdk.backtesting.run.return_value = _fake_result()
    results = quick_backtest_all([_candidate("c1"), _candidate("c2")])
    assert len(results) == 2
    for r in results:
        assert r.success is True
        assert r.irr_annualized == 0.5
        assert r.win_rate == 0.6
        assert r.total_trades == 20
        assert r.sharpe_ratio == 1.2


def test_quick_backtest_catches_per_candidate_failures(mock_sdk):
    """One bad candidate does not abort the batch."""
    from src.services.backtest_service import quick_backtest_all

    good = _fake_result()
    mock_sdk.backtesting.run.side_effect = [good, RuntimeError("boom"), good]
    results = quick_backtest_all([_candidate("a"), _candidate("b"), _candidate("c")])
    assert len(results) == 3
    assert results[0].success is True
    assert results[1].success is False
    assert "boom" in (results[1].error or "")
    assert results[2].success is True


def test_filter_drops_low_win_rate(mock_sdk):
    from src.services.backtest_service import _summarize, filter_and_rank

    r_low = _summarize(_candidate("low"), _fake_result(win_rate=0.40))
    r_ok = _summarize(_candidate("ok"), _fake_result(win_rate=0.55))
    survivors, rejected = filter_and_rank([r_low, r_ok], min_win_rate=0.51, min_trades=10)
    assert len(survivors) == 1
    assert survivors[0].candidate.name == "ok"
    assert len(rejected) == 1
    assert "win_rate" in (rejected[0].reject_reason or "")


def test_filter_drops_low_trade_count(mock_sdk):
    from src.services.backtest_service import _summarize, filter_and_rank

    r_few = _summarize(_candidate("few"), _fake_result(total_trades=5))
    r_ok = _summarize(_candidate("ok"), _fake_result(total_trades=20))
    survivors, rejected = filter_and_rank([r_few, r_ok], min_win_rate=0.51, min_trades=10)
    assert [s.candidate.name for s in survivors] == ["ok"]
    assert "total_trades" in (rejected[0].reject_reason or "")


def test_filter_drops_failed_runs(mock_sdk):
    from src.services.backtest_service import _summarize, filter_and_rank

    r_bad = _summarize(_candidate("bad"), _fake_result(success=False, error="sdk 500"))
    r_ok = _summarize(_candidate("ok"), _fake_result())
    survivors, rejected = filter_and_rank([r_bad, r_ok])
    assert [s.candidate.name for s in survivors] == ["ok"]
    assert any("backtest failed" in (r.reject_reason or "") for r in rejected)


def test_rank_by_irr_descending(mock_sdk):
    from src.services.backtest_service import _summarize, filter_and_rank

    irr_values = [(0.2, "low"), (0.8, "high"), (0.5, "mid")]
    results = [
        _summarize(_candidate(name), _fake_result(irr=irr))
        for irr, name in irr_values
    ]
    survivors, _ = filter_and_rank(results, min_win_rate=0.0, min_trades=0)
    assert [s.candidate.name for s in survivors] == ["high", "mid", "low"]


def test_full_backtest_includes_trade_history(mock_sdk):
    from src.services.backtest_service import full_backtest

    trades = [{"entry_time": "2026-01-01", "pnl": 12.3}]
    mock_sdk.backtesting.submit_async.return_value = SimpleNamespace(backtest_id="bt-9", status="queued")
    mock_sdk.backtesting.poll_status.return_value = _fake_status(trade_history=trades)
    result = full_backtest(_candidate("winner"))
    assert result.success is True
    assert result.backtest_id == "bt-9"
    assert result.total_trades == 20
    assert "trade_history" in result.raw_metrics
    assert result.raw_metrics["trade_history"] == trades


def test_full_backtest_wraps_sdk_error(mock_sdk):
    from src.services.backtest_service import full_backtest
    from src.shared.errors import SdkError

    mock_sdk.backtesting.submit_async.side_effect = RuntimeError("upstream 503")
    with pytest.raises(SdkError):
        full_backtest(_candidate("winner"))


def test_irr_ranking_uses_config_defaults_when_thresholds_not_passed(mock_sdk, monkeypatch):
    """filter_and_rank picks up thresholds from app_config when args omitted."""
    from src.config import app_config
    from src.services.backtest_service import _summarize, filter_and_rank

    monkeypatch.setattr(app_config, "BACKTEST_MIN_WIN_RATE", 0.60)
    monkeypatch.setattr(app_config, "BACKTEST_MIN_TRADES", 5)

    r_borderline = _summarize(_candidate("borderline"), _fake_result(win_rate=0.55))
    r_ok = _summarize(_candidate("ok"), _fake_result(win_rate=0.65))
    survivors, rejected = filter_and_rank([r_borderline, r_ok])
    assert [s.candidate.name for s in survivors] == ["ok"]
    assert len(rejected) == 1


# ---------------------------------------------------------------------------
# Tracked full backtests — the server-side run id survives
# ---------------------------------------------------------------------------


def test_full_backtest_polls_until_complete(mock_sdk, monkeypatch):
    from src.services import backtest_service as bs

    monkeypatch.setattr(bs.time, "sleep", lambda *_: None)
    mock_sdk.backtesting.submit_async.return_value = SimpleNamespace(backtest_id="bt-2", status="queued")
    mock_sdk.backtesting.poll_status.side_effect = [
        _fake_status("queued"), _fake_status("running"), _fake_status("completed", trade_history=[]),
    ]
    result = bs.full_backtest(_candidate("c"))
    assert result.success is True and result.backtest_id == "bt-2"
    assert mock_sdk.backtesting.poll_status.call_count == 3
    mock_sdk.backtesting.run.assert_not_called()


def test_full_backtest_failed_run_keeps_id_and_error(mock_sdk):
    from src.services.backtest_service import full_backtest

    mock_sdk.backtesting.submit_async.return_value = SimpleNamespace(backtest_id="bt-3", status="queued")
    mock_sdk.backtesting.poll_status.return_value = _fake_status("failed", error_message="no data")
    result = full_backtest(_candidate("c"))
    assert result.success is False
    assert result.error == "no data"
    assert result.backtest_id == "bt-3"


def test_full_backtest_timeout_names_the_run(mock_sdk, monkeypatch):
    from src.services import backtest_service as bs
    from src.shared.errors import SdkError

    monkeypatch.setattr(bs.time, "sleep", lambda *_: None)
    monkeypatch.setattr(bs, "_POLL_TIMEOUT_S", -1.0)
    mock_sdk.backtesting.submit_async.return_value = SimpleNamespace(backtest_id="bt-4", status="queued")
    mock_sdk.backtesting.poll_status.return_value = _fake_status("running")
    with pytest.raises(SdkError) as exc:
        bs.full_backtest(_candidate("c"))
    assert "bt-4" in exc.value.message and "get_backtest" in exc.value.suggestion


# ---------------------------------------------------------------------------
# Stored runs
# ---------------------------------------------------------------------------


def test_list_backtests_clamps_and_maps(mock_sdk):
    from src.services.backtest_service import list_backtests

    mock_sdk.users.get_my_backtests.return_value = SimpleNamespace(total=7, items=[
        SimpleNamespace(id="b1", asset="BTC", status="completed", result="PASS", total_return=12.5),
    ])
    out = list_backtests(asset=" btc ", limit=1000, offset=-3)
    kwargs = mock_sdk.users.get_my_backtests.call_args.kwargs
    assert kwargs["asset"] == "BTC" and kwargs["limit"] == 100 and kwargs["offset"] == 0
    assert out["total"] == 7 and out["count"] == 1
    assert out["backtests"][0]["id"] == "b1"
    assert out["backtests"][0]["win_rate"] is None  # absent fields are null, never invented


def test_list_backtests_wraps_sdk_failure(mock_sdk):
    from src.services.backtest_service import list_backtests
    from src.shared.errors import SdkError

    mock_sdk.users.get_my_backtests.side_effect = RuntimeError("503")
    with pytest.raises(SdkError):
        list_backtests()


def test_get_backtest_reads_raw_record(mock_sdk):
    from src.services.backtest_service import get_backtest

    mock_sdk.backtesting._core.request.return_value.json.return_value = {
        "id": "b1", "status": "running", "asset": "ETH",
        "config": {"name": "n", "entry": [{"timeframe": "4h"}, {"timeframe": "1h"}], "exit": []},
        "metrics": None, "trade_history": None, "start_date": "2026-01-01", "end_date": "2026-02-01",
    }
    out = get_backtest("b1", include_trades=True)
    assert out["status"] == "running"
    assert out["interval"] == "1h"
    assert out["trade_count"] == 0 and out["trade_history"] == []
    assert "benchmark" not in out  # only completed runs are benchmarked


def test_get_backtest_not_found(mock_sdk):
    from mangrove_ai.exceptions import NotFoundError

    from src.services.backtest_service import get_backtest
    from src.shared.errors import BacktestNotFound

    mock_sdk.backtesting._core.request.side_effect = NotFoundError(404, "Not Found", "Backtest not found", "INVALID_REQUEST")
    with pytest.raises(BacktestNotFound):
        get_backtest("nope")
