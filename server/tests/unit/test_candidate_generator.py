"""Unit tests for candidate_generator — deterministic goal→candidate mapping."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402


@dataclass
class _FakeSignalMetadata:
    params: dict[str, Any] | None = None


@dataclass
class _FakeSignal:
    name: str
    category: str
    signal_type: str
    metadata: _FakeSignalMetadata | None = None


def _catalog() -> list[_FakeSignal]:
    """A small catalog covering the goal keywords the generator supports."""
    return [
        _FakeSignal("rsi_oversold", "overbought_oversold", "TRIGGER",
                    _FakeSignalMetadata({"window": {"default": 14}, "threshold": {"default": 30}})),
        _FakeSignal("rsi_overbought", "overbought_oversold", "TRIGGER",
                    _FakeSignalMetadata({"window": {"default": 14}, "threshold": {"default": 70}})),
        _FakeSignal("macd_cross_up", "momentum", "TRIGGER", _FakeSignalMetadata({})),
        _FakeSignal("price_breakout", "breakout", "TRIGGER", _FakeSignalMetadata({})),
        _FakeSignal("ema_trend", "trend", "TRIGGER", _FakeSignalMetadata({})),
        _FakeSignal("volume_spike", "volume", "FILTER", _FakeSignalMetadata({})),
        _FakeSignal("atr_volatility", "volatility", "FILTER", _FakeSignalMetadata({})),
        _FakeSignal("ema_above_ma", "trend", "FILTER", _FakeSignalMetadata({})),
        _FakeSignal("rsi_momentum", "momentum", "FILTER", _FakeSignalMetadata({})),
    ]


@pytest.fixture
def mock_sdk_catalog(monkeypatch):
    """Stub mangrove_ai_client().signals.list_iter() with our fake catalog."""
    client = MagicMock()
    client.signals.list_iter.return_value = iter(_catalog())

    # list_iter is consumed by list(...), so it must be re-creatable per call.
    def _fresh_iter(**_kwargs):
        return iter(_catalog())
    client.signals.list_iter.side_effect = _fresh_iter

    monkeypatch.setattr(
        "src.services.candidate_generator.mangrove_ai_client",
        lambda: client,
    )
    return client


# -- parse_goal --------------------------------------------------------------


def test_parse_goal_momentum():
    from src.services.candidate_generator import parse_goal

    parsed = parse_goal("Trade ETH on momentum breakouts with tight stops")
    assert "momentum" in parsed["keywords_matched"]
    assert "breakout" in parsed["keywords_matched"]
    # trigger categories union: momentum+trend (from momentum) + breakout (from breakout)
    assert "momentum" in parsed["trigger_categories"]
    assert "breakout" in parsed["trigger_categories"]


def test_parse_goal_mean_reversion_alias():
    from src.services.candidate_generator import parse_goal

    parsed = parse_goal("classic mean-reversion setup on BTC")
    assert "mean_reversion" in parsed["keywords_matched"]
    assert "overbought_oversold" in parsed["trigger_categories"]


def test_parse_goal_defaults_to_momentum():
    from src.services.candidate_generator import parse_goal

    parsed = parse_goal("something totally unrecognized")
    assert parsed["keywords_matched"] == ["momentum"]


def test_parse_goal_case_insensitive():
    from src.services.candidate_generator import parse_goal

    parsed = parse_goal("BREAKOUT on the daily")
    assert "breakout" in parsed["keywords_matched"]


# -- generate ----------------------------------------------------------------


def test_generate_returns_requested_count(mock_sdk_catalog):
    from src.services.candidate_generator import generate

    candidates = generate("momentum on ETH", "ETH", "1h", n=7, seed=42)
    assert len(candidates) == 7


def test_generate_clamps_n_to_5_10(mock_sdk_catalog):
    from src.services.candidate_generator import generate

    assert len(generate("momentum", "ETH", "1h", n=1, seed=1)) == 5
    assert len(generate("momentum", "ETH", "1h", n=50, seed=1)) == 10


def test_generate_is_deterministic_with_seed(mock_sdk_catalog):
    from src.services.candidate_generator import generate

    a = generate("momentum on ETH", "ETH", "1h", n=5, seed=42)
    b = generate("momentum on ETH", "ETH", "1h", n=5, seed=42)
    assert [c.model_dump() for c in a] == [c.model_dump() for c in b]


def test_generate_respects_composition_rules(mock_sdk_catalog):
    from src.services.candidate_generator import generate

    candidates = generate("momentum on ETH", "ETH", "1h", n=10, seed=7)
    for c in candidates:
        # entry: exactly 1 TRIGGER
        triggers = [r for r in c.entry if r["signal_type"] == "TRIGGER"]
        filters = [r for r in c.entry if r["signal_type"] == "FILTER"]
        assert len(triggers) == 1, f"entry must have exactly 1 TRIGGER, got {len(triggers)}"
        assert 0 <= len(filters) <= 2

        # exit: 0 or 1 TRIGGER, 0+ FILTERs
        exit_triggers = [r for r in c.exit if r["signal_type"] == "TRIGGER"]
        assert 0 <= len(exit_triggers) <= 1


def test_generate_uses_signal_defaults(mock_sdk_catalog):
    """Picked signals carry their default params pulled from metadata."""
    from src.services.candidate_generator import generate

    candidates = generate("oversold on BTC", "BTC", "1h", n=5, seed=0)
    # rsi_oversold is one of the TRIGGER signals in our fake catalog.
    has_rsi_with_defaults = False
    for c in candidates:
        for rule in c.entry:
            if rule["name"] == "rsi_oversold":
                assert rule["params"].get("window") == 14
                assert rule["params"].get("threshold") == 30
                has_rsi_with_defaults = True
    assert has_rsi_with_defaults, "expected at least one rsi_oversold entry with defaults"


def test_generate_raises_when_no_trigger_signals(monkeypatch):
    """Empty trigger pool → StrategyNoViableCandidates."""
    from src.services.candidate_generator import generate
    from src.shared.errors import StrategyNoViableCandidates

    empty_client = MagicMock()
    empty_client.signals.list_iter.side_effect = lambda **kw: iter([])
    monkeypatch.setattr(
        "src.services.candidate_generator.mangrove_ai_client",
        lambda: empty_client,
    )

    with pytest.raises(StrategyNoViableCandidates):
        generate("momentum", "ETH", "1h")


def test_generate_carries_asset_and_timeframe(mock_sdk_catalog):
    from src.services.candidate_generator import generate

    candidates = generate("momentum", "SOL", "4h", n=5, seed=3)
    for c in candidates:
        assert c.asset == "SOL"
        assert c.timeframe == "4h"
        for rule in c.entry + c.exit:
            assert rule["timeframe"] == "4h"
