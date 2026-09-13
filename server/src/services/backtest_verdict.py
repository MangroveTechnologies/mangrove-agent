"""backtest_verdict — PASS / MARGINAL / FAIL / INSUFFICIENT_TRADES, computed in code.

Single source for the verdict the `/backtest` skill used to recompute in
prose. Pure function over a metrics dict + `data/threshold_spec.json`; no
I/O beyond the (cached) spec read, no SDK calls.

Units
-----
MangroveAI reports percent-typed metrics on a 0-100 scale (`win_rate: 25.0`
means 25%, `max_drawdown: 19.9` means a 19.9% drawdown, `irr_annualized:
-36.1` means -36.1%/yr). The spec stores those thresholds as decimals
(`min_win_rate: 0.25`) and its `metrics_mapping` says to convert "from
percentage to decimal". So `irr_annualized`, `max_drawdown`, `win_rate` are
divided by 100 before comparison; `sortino_ratio`, `sharpe_ratio`,
`calmar_ratio` are raw ratios. This mirrors MangroveAI's canonical
`domains/backtesting/thresholds.py::evaluate_thresholds`. Each check echoes
both the `raw` SDK value and the converted `actual`.

Verdict rules (checked in this order)
-------------------------------------
- INSUFFICIENT_TRADES — `total_trades` missing or `< min_trades`
  (default `BACKTEST_MIN_TRADES`, 10). Ratios over a handful of trades are
  noise, so no PASS/MARGINAL/FAIL is issued, whatever they say. Covers
  `total_trades == 0`.
- PASS     — all 6 thresholds pass.
- MARGINAL — 4 or 5 of the 6 pass: close enough to be worth iterating on
  (alternate window, walk-forward, one targeted tweak), not good enough to
  promote without a second look.
- FAIL     — 3 or fewer pass.

A metric that is missing / null / non-numeric counts as NOT passed and is
reported with `actual: null, missing: true` — never invented.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

_SPEC_PATH = Path(__file__).parent / "data" / "threshold_spec.json"

PASS = "PASS"
MARGINAL = "MARGINAL"
FAIL = "FAIL"
INSUFFICIENT_TRADES = "INSUFFICIENT_TRADES"

# Minimum number of passing thresholds (out of 6) for MARGINAL.
MARGINAL_MIN_PASSED = 4
DEFAULT_MIN_TRADES = 10

# (spec threshold key, SDK metric key, comparison, percent-scaled metric?)
_CHECKS: tuple[tuple[str, str, str, bool], ...] = (
    ("sortino_min", "sortino_ratio", ">=", False),
    ("sharpe_min", "sharpe_ratio", ">=", False),
    ("calmar_min", "calmar_ratio", ">=", False),
    ("irr_min", "irr_annualized", ">=", True),
    ("max_drawdown_max", "max_drawdown", "<=", True),
    ("min_win_rate", "win_rate", ">=", True),
)


@lru_cache(maxsize=1)
def load_spec() -> dict[str, Any]:
    """Read threshold_spec.json once per process."""
    return json.loads(_SPEC_PATH.read_text())


def load_thresholds() -> dict[str, float]:
    """The six threshold values from threshold_spec.json (decimals / ratios)."""
    return dict(load_spec()["thresholds"])


def percent_to_decimal(value: float) -> float:
    """0-100 percent scale → decimal (25.0 → 0.25)."""
    return value / 100.0


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _default_min_trades() -> int:
    try:
        from src.config import app_config
        return int(getattr(app_config, "BACKTEST_MIN_TRADES", DEFAULT_MIN_TRADES))
    except Exception:  # noqa: BLE001 — config unavailable (pure-function use) → spec default
        return DEFAULT_MIN_TRADES


def compute_verdict(
    metrics: dict[str, Any] | None,
    *,
    min_trades: int | None = None,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Grade a backtest metrics dict against threshold_spec.json.

    Returns a JSON-serializable dict:
      verdict        PASS | MARGINAL | FAIL | INSUFFICIENT_TRADES
      passed_count   thresholds passed (0-6), always computed
      total_checks   6
      total_trades   int | None
      min_trades     the trade floor applied
      failed         SDK metric names that did not pass
      checks         per-threshold {metric, threshold, comparison, required,
                     actual, raw, unit, passed, missing}
      rules          human-readable definitions of each verdict label
      spec_version   threshold_spec.json version
    """
    metrics = metrics or {}
    th = thresholds or load_thresholds()
    floor = int(min_trades if min_trades is not None else _default_min_trades())

    checks: list[dict[str, Any]] = []
    for spec_key, metric_key, comparison, percent in _CHECKS:
        raw = _as_number(metrics.get(metric_key))
        required = float(th[spec_key])
        if raw is None:
            actual = None
            passed = False
        else:
            actual = percent_to_decimal(raw) if percent else raw
            passed = actual >= required if comparison == ">=" else actual <= required
        checks.append({
            "metric": metric_key,
            "threshold": spec_key,
            "comparison": comparison,
            "required": required,
            "actual": actual,
            "raw": metrics.get(metric_key),
            "unit": "decimal (converted from 0-100 percent)" if percent else "ratio",
            "passed": passed,
            "missing": raw is None,
        })

    passed_count = sum(1 for c in checks if c["passed"])
    trades_num = _as_number(metrics.get("total_trades"))
    total_trades = int(trades_num) if trades_num is not None else None

    if total_trades is None or total_trades < floor:
        verdict = INSUFFICIENT_TRADES
    elif passed_count == len(checks):
        verdict = PASS
    elif passed_count >= MARGINAL_MIN_PASSED:
        verdict = MARGINAL
    else:
        verdict = FAIL

    return {
        "verdict": verdict,
        "passed_count": passed_count,
        "total_checks": len(checks),
        "total_trades": total_trades,
        "min_trades": floor,
        "failed": [c["metric"] for c in checks if not c["passed"]],
        "checks": checks,
        "rules": {
            INSUFFICIENT_TRADES: f"total_trades missing or < {floor}; ratios not graded",
            PASS: f"all {len(checks)} thresholds pass",
            MARGINAL: f"{MARGINAL_MIN_PASSED}-{len(checks) - 1} of {len(checks)} thresholds pass",
            FAIL: f"{MARGINAL_MIN_PASSED - 1} or fewer of {len(checks)} thresholds pass",
        },
        "spec_version": load_spec().get("version"),
    }


def passes_win_rate_floor(win_rate_pct: float, min_win_rate: float | None = None) -> bool:
    """Candidate-filter helper: does a 0-100 win rate meet the spec's decimal floor?"""
    floor = float(min_win_rate if min_win_rate is not None else load_thresholds()["min_win_rate"])
    return percent_to_decimal(win_rate_pct) >= floor
