"""Reference search/build response semantics (dogfood findings 1, 2, 4).

- search must never imply a filter it did not apply: every result says how
  it matched, and strict=true really filters.
- curated (ref-*) labels/descriptions must describe the actual rules.
- build must say it did not persist, without breaking POST-ability.
"""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import re  # noqa: E402

import pytest  # noqa: E402

from src.services import reference_strategies_service as svc  # noqa: E402


class TestSearchMatchAnnotation:
    def test_every_result_is_annotated(self):
        resp = svc.search_response(asset="ETH", timeframe="1h", limit=10)
        assert resp["count"] == len(resp["strategies"]) == 10
        for s in resp["strategies"]:
            assert s["match"] in {"exact", "partial", "none"}
            assert isinstance(s["matched_on"], list) and isinstance(s["unmatched"], list)

    def test_non_matching_timeframe_is_never_exact(self):
        resp = svc.search_response(asset="ETH", timeframe="1h", limit=10)
        for s in resp["strategies"]:
            if s["timeframe"] != "1h":
                assert s["match"] != "exact", s["id"]
                assert "timeframe" in s["unmatched"], s["id"]
            if s["asset"] == "ETH" and s["timeframe"] == "1h":
                assert s["match"] == "exact"

    def test_exact_matches_rank_first_and_are_counted(self):
        resp = svc.search_response(asset="ETH", timeframe="1h", limit=10)
        levels = [s["match"] for s in resp["strategies"]]
        n_exact = levels.count("exact")
        assert resp["exact_match_count"] == n_exact >= 1
        assert levels[:n_exact] == ["exact"] * n_exact
        assert "ranked, not filtered" in resp["filter_semantics"]

    def test_strict_returns_only_exact_matches(self):
        resp = svc.search_response(asset="ETH", timeframe="1h", limit=10, strict=True)
        assert resp["strict"] is True
        assert resp["count"] == resp["exact_match_count"] >= 1
        assert all(s["asset"] == "ETH" and s["timeframe"] == "1h" for s in resp["strategies"])

    def test_strict_can_be_empty(self):
        assert svc.search(asset="NOPE", timeframe="1h", strict=True) == []

    def test_strict_with_category(self):
        items = svc.search(asset="BTC", timeframe="4h", category="breakout", strict=True, limit=50)
        assert items
        assert all((r.asset, r.timeframe, r.category) == ("BTC", "4h", "breakout") for r in items)

    def test_default_search_unchanged_for_existing_callers(self):
        """Non-strict search still pads to `limit` (portability)."""
        assert len(svc.search(asset="ETH", timeframe="1h", limit=10)) == 10

    def test_echo_is_canonical_and_category_source_reported(self):
        resp = svc.search_response(asset="eth", timeframe="1HR", goal_hint="momentum play")
        assert resp["asset"] == "ETH"
        assert resp["timeframe"] == "1h"
        assert resp["category"] == "momentum"
        assert resp["category_source"] == "goal_hint"
        explicit = svc.search_response(asset="ETH", category="breakout")
        assert explicit["category_source"] == "explicit"


# Indicator families → how they're named in human text.
_FAMILIES = {
    "macd": "MACD", "sma": "SMA", "ema": "EMA", "rsi": "RSI", "stoch": "Stoch",
    "adx": "ADX", "roc": "ROC|Rate of Change", "pvo": "PVO", "ichimoku": "Ichimoku", "atr": "ATR",
}


def _family(signal_name: str) -> str:
    if signal_name == "is_above_sma":
        return "sma"
    return signal_name.split("_")[0]


@pytest.mark.parametrize("ref", [r for r in svc.list_all() if r.id.startswith("ref-")], ids=lambda r: r.id)
def test_curated_label_and_description_name_every_rule_family(ref):
    """Each indicator used in entry/exit must be named in the label or description,
    and the description must not name an indicator the rules don't use."""
    text = f"{ref.label} {ref.description}"
    used = {_family(s.name) for s in ref.entry_signals + ref.exit_signals}
    for fam in used:
        assert re.search(_FAMILIES[fam], text), f"{ref.id}: rules use {fam} but text never names it"
    for fam, pattern in _FAMILIES.items():
        if fam not in used and re.search(pattern, ref.description):
            # A contrast mention to another reference (e.g. "than plain RSI ... (ref-003)") is fine.
            assert re.search(r"ref-\d{3}", ref.description), f"{ref.id}: description names {fam}, rules don't use it"


def test_ref_009_describes_its_rsi_rules():
    ref = svc.get("ref-009")
    assert [s.name for s in ref.entry_signals] == ["rsi_cross_up", "stoch_oversold"]
    assert [s.name for s in ref.exit_signals] == ["rsi_cross_down"]
    assert "RSI" in ref.label and "RSI" in ref.description
    assert "80" not in ref.description  # the old, wrong stoch-overbought exit


class TestBuildNotPersistedHint:
    def test_build_says_not_persisted(self):
        payload = svc.build_from_reference("ref-001", asset_override="ETH")
        assert payload["persisted"] is False
        assert payload["next_step"]["rest"] == "POST /api/v1/agent/strategies/manual"
        assert payload["next_step"]["mcp_tool"] == "create_strategy_manual"

    def test_build_payload_validates_as_manual_request(self):
        """The hints are extra keys; StrategyManualRequest must still accept the dict as-is."""
        from src.services.strategy_service import StrategyManualRequest

        payload = svc.build_from_reference("ref-004", timeframe_override="1h", asset_override="ETH")
        req = StrategyManualRequest.model_validate(payload)
        assert req.asset == "ETH" and req.timeframe == "1h"
        assert not hasattr(req, "persisted")
