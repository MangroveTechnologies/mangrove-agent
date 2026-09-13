"""Unit tests for backtest_verdict — server-side PASS/MARGINAL/FAIL/INSUFFICIENT_TRADES.

Metrics use MangroveAI's real scale: percent-typed fields are 0-100
(`win_rate: 25.0` = 25%), ratios are raw.
"""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import json  # noqa: E402

import pytest  # noqa: E402

from src.services import backtest_verdict as bv  # noqa: E402


def _metrics(**overrides):
    """A metrics dict that passes all 6 thresholds by default."""
    m = {
        "total_trades": 40,
        "sortino_ratio": 2.0,
        "sharpe_ratio": 1.5,
        "calmar_ratio": 1.4,
        "irr_annualized": 30.0,   # 30%  → 0.30 >= 0.15
        "max_drawdown": 12.0,     # 12%  → 0.12 <= 0.70
        "win_rate": 45.0,         # 45%  → 0.45 >= 0.25
    }
    m.update(overrides)
    return m


def _check(result, metric):
    return next(c for c in result["checks"] if c["metric"] == metric)


def test_thresholds_come_from_spec_file():
    spec = json.loads(bv._SPEC_PATH.read_text())["thresholds"]
    assert bv.load_thresholds() == spec


def test_all_six_pass_is_pass():
    r = bv.compute_verdict(_metrics(), min_trades=10)
    assert r["verdict"] == "PASS"
    assert r["passed_count"] == 6
    assert r["total_checks"] == 6
    assert r["failed"] == []


@pytest.mark.parametrize("n_failing,expected", [(1, "MARGINAL"), (2, "MARGINAL"), (3, "FAIL"), (6, "FAIL")])
def test_marginal_is_four_or_five_of_six(n_failing, expected):
    failing = [
        ("sortino_ratio", 0.1), ("sharpe_ratio", 0.1), ("calmar_ratio", 0.1),
        ("irr_annualized", 1.0), ("max_drawdown", 90.0), ("win_rate", 10.0),
    ][:n_failing]
    r = bv.compute_verdict(_metrics(**dict(failing)), min_trades=10)
    assert r["verdict"] == expected
    assert r["passed_count"] == 6 - n_failing


def test_percent_metrics_convert_from_0_100_scale():
    r = bv.compute_verdict(_metrics(win_rate=25.0, max_drawdown=19.875, irr_annualized=-36.06), min_trades=10)
    wr = _check(r, "win_rate")
    assert wr["raw"] == 25.0
    assert wr["actual"] == pytest.approx(0.25)
    assert wr["required"] == 0.25
    assert wr["passed"] is True  # >= is inclusive
    dd = _check(r, "max_drawdown")
    assert dd["actual"] == pytest.approx(0.19875)
    assert dd["comparison"] == "<="
    assert dd["passed"] is True
    irr = _check(r, "irr_annualized")
    assert irr["actual"] == pytest.approx(-0.3606)
    assert irr["passed"] is False


def test_ratios_are_not_converted():
    r = bv.compute_verdict(_metrics(sharpe_ratio=1.19), min_trades=10)
    sh = _check(r, "sharpe_ratio")
    assert sh["actual"] == 1.19
    assert sh["unit"] == "ratio"
    assert sh["passed"] is False


def test_decimal_win_rate_is_not_mistaken_for_a_pass():
    """A 0.6 win_rate on the SDK's 0-100 scale is 0.6% — must fail."""
    r = bv.compute_verdict(_metrics(win_rate=0.6), min_trades=10)
    assert _check(r, "win_rate")["passed"] is False


def test_zero_trades_is_insufficient_even_if_ratios_pass():
    r = bv.compute_verdict(_metrics(total_trades=0), min_trades=10)
    assert r["verdict"] == "INSUFFICIENT_TRADES"
    assert r["passed_count"] == 6  # still reported, just not graded


def test_below_min_trades_is_insufficient():
    assert bv.compute_verdict(_metrics(total_trades=9), min_trades=10)["verdict"] == "INSUFFICIENT_TRADES"
    assert bv.compute_verdict(_metrics(total_trades=10), min_trades=10)["verdict"] == "PASS"


def test_missing_total_trades_is_insufficient():
    m = _metrics()
    del m["total_trades"]
    assert bv.compute_verdict(m, min_trades=10)["verdict"] == "INSUFFICIENT_TRADES"


def test_min_trades_defaults_to_config(monkeypatch):
    from src.config import app_config
    monkeypatch.setattr(app_config, "BACKTEST_MIN_TRADES", 50)
    r = bv.compute_verdict(_metrics(total_trades=40))
    assert r["min_trades"] == 50
    assert r["verdict"] == "INSUFFICIENT_TRADES"


def test_missing_metric_fails_and_is_flagged_not_invented():
    m = _metrics()
    del m["sortino_ratio"]
    m["calmar_ratio"] = None
    r = bv.compute_verdict(m, min_trades=10)
    so = _check(r, "sortino_ratio")
    assert so["missing"] is True and so["actual"] is None and so["passed"] is False
    assert _check(r, "calmar_ratio")["missing"] is True
    assert r["verdict"] == "MARGINAL"
    assert set(r["failed"]) == {"sortino_ratio", "calmar_ratio"}


def test_empty_metrics():
    r = bv.compute_verdict({}, min_trades=10)
    assert r["verdict"] == "INSUFFICIENT_TRADES"
    assert r["passed_count"] == 0


def test_result_is_json_serializable():
    json.dumps(bv.compute_verdict(_metrics(), min_trades=10))


def test_win_rate_floor_helper_uses_spec_and_percent_scale():
    assert bv.passes_win_rate_floor(25.0) is True
    assert bv.passes_win_rate_floor(24.9) is False
    assert bv.passes_win_rate_floor(55.0, min_win_rate=0.51) is True
