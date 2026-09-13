"""Unit tests for reference_strategies_service.

Covers:
- JSON seed loads + parses into ReferenceStrategy models
- `search` ranks by specificity (asset+timeframe+category > asset+timeframe > asset > category)
- `search` auto-detects category from goal_hint when category omitted
- `get` returns None for unknown ids
- `build_from_reference` copies signals exactly, applies timeframe override
- `build_from_reference` rejects unknown reference_id
- Timeframe canonicalization propagates (so "1HR" → "1h" in the output)
"""
import pytest

from src.services import reference_strategies_service as svc
from src.shared.errors import ValidationError


class TestLoad:
    def test_seed_loads(self):
        items = svc.list_all()
        assert len(items) >= 8, "seed should have at least 8 strategies"

    def test_all_have_required_fields(self):
        for r in svc.list_all():
            assert r.id
            assert r.asset
            assert r.timeframe
            assert r.category
            assert r.entry_signals, f"{r.id} has no entry signals"
            # Exit can be empty; execution_config can be sparse.
            assert isinstance(r.execution_config, dict)

    def test_all_timeframes_are_canonical(self):
        """Seed data should use canonical timeframes. Protects against typos
        that would cause search() to miss the entry."""
        from src.shared import timeframes
        for r in svc.list_all():
            # Should not raise
            canonical = timeframes.canonicalize_timeframe(r.timeframe)
            assert canonical == r.timeframe, f"{r.id} timeframe {r.timeframe!r} non-canonical"


class TestSearch:
    def test_asset_exact_match_wins(self):
        results = svc.search(asset="ETH", limit=10)
        assert len(results) > 0
        # Top result must be ETH-specific.
        assert results[0].asset.upper() == "ETH"

    def test_asset_plus_timeframe_outranks_asset_only(self):
        results = svc.search(asset="ETH", timeframe="1h", limit=5)
        assert len(results) > 0
        # Top result should be ETH on 1h.
        top = results[0]
        assert top.asset.upper() == "ETH"
        assert top.timeframe == "1h"

    def test_category_filter_prefers_matching_category(self):
        results = svc.search(asset="BTC", category="momentum", limit=5)
        assert len(results) > 0
        # At least the top result should be BTC momentum.
        assert results[0].asset.upper() == "BTC"
        assert results[0].category == "momentum"

    def test_goal_hint_auto_detects_category(self):
        results = svc.search(asset="ETH", goal_hint="mean reversion bounce play", limit=3)
        # Whatever's returned, the top result should be mean_reversion if we have one.
        # (seed has ref-003 and ref-009 as mean_reversion ETH)
        mean_rev_eth = [r for r in svc.list_all() if r.asset.upper() == "ETH" and r.category == "mean_reversion"]
        if mean_rev_eth:
            assert results[0].category == "mean_reversion", f"goal_hint should route to mean_reversion; got {results[0].category}"

    def test_respects_limit(self):
        results = svc.search(asset="ETH", limit=2)
        assert len(results) <= 2

    def test_fallback_when_no_match(self):
        """Even on an unknown asset, return SOMETHING rather than empty —
        gives the agent a chance to offer alternatives."""
        results = svc.search(asset="DOGE", limit=3)
        assert len(results) > 0


class TestGet:
    def test_known_id(self):
        all_items = svc.list_all()
        first_id = all_items[0].id
        fetched = svc.get(first_id)
        assert fetched is not None
        assert fetched.id == first_id

    def test_unknown_id(self):
        assert svc.get("ref-999-does-not-exist") is None


class TestBuildFromReference:
    def test_copies_signals_exactly(self):
        # Pick any reference and build without overrides.
        ref = svc.list_all()[0]
        payload = svc.build_from_reference(reference_id=ref.id)
        # Signals must match exactly except `timeframe` is now the canonical on each rule.
        assert len(payload["entry"]) == len(ref.entry_signals)
        for got, expected in zip(payload["entry"], ref.entry_signals):
            assert got["name"] == expected.name
            assert got["signal_type"] == expected.signal_type
            assert got["params"] == expected.params, f"{ref.id}: params MUST be copied verbatim"

    def test_timeframe_override_applies_to_all_rules(self):
        ref = svc.list_all()[0]  # has timeframe "1h"
        payload = svc.build_from_reference(
            reference_id=ref.id, timeframe_override="4h",
        )
        assert payload["timeframe"] == "4h"
        for rule in payload["entry"] + payload["exit"]:
            assert rule["timeframe"] == "4h"

    def test_timeframe_canonicalizes(self):
        ref = svc.list_all()[0]
        payload = svc.build_from_reference(reference_id=ref.id, timeframe_override="4HR")
        assert payload["timeframe"] == "4h"
        for rule in payload["entry"]:
            assert rule["timeframe"] == "4h"

    def test_asset_override_retargets_strategy(self):
        """Reference strategies are portable combos — asset_override must
        replace the strategy's asset field while leaving signal names and
        params untouched. The retarget flow (BTC combo → user's AVAX goal)
        depends on this."""
        ref = svc.list_all()[0]
        payload = svc.build_from_reference(reference_id=ref.id, asset_override="avax")
        assert payload["asset"] == "AVAX", "asset_override must be normalized to upper"
        # Signals must still be byte-identical to the reference.
        for got, expected in zip(payload["entry"], ref.entry_signals):
            assert got["name"] == expected.name
            assert got["params"] == expected.params

    def test_asset_override_absent_preserves_reference_asset(self):
        ref = svc.list_all()[0]
        payload = svc.build_from_reference(reference_id=ref.id)
        assert payload["asset"] == ref.asset.upper()

    def test_unsupported_timeframe_rejected(self):
        ref = svc.list_all()[0]
        with pytest.raises(ValidationError):
            svc.build_from_reference(reference_id=ref.id, timeframe_override="1m")

    def test_unknown_id_raises(self):
        with pytest.raises(ValueError):
            svc.build_from_reference(reference_id="ref-missing")

    def test_payload_shape_matches_create_strategy_manual(self):
        """The returned payload should be directly POST-able to /strategies/manual."""
        ref = svc.list_all()[0]
        payload = svc.build_from_reference(reference_id=ref.id)
        # Required fields on StrategyManualRequest:
        for key in ("name", "asset", "timeframe", "entry"):
            assert key in payload, f"missing required field {key}"
        assert isinstance(payload["entry"], list)
        assert isinstance(payload["exit"], list)

    def test_execution_config_includes_initial_balance(self):
        """Regression: build_from_reference must produce a complete execution_config.

        The reference JSON only stores per-strategy *overrides* (e.g.
        max_risk_per_trade=0.008). The SDK's strategies.create endpoint
        requires a full flat execution_config including initial_balance;
        previously build_from_reference returned only the overrides and the
        downstream create_strategy_manual call 500'd on the missing key.
        """
        ref = svc.list_all()[0]
        payload = svc.build_from_reference(reference_id=ref.id)
        exec_cfg = payload["execution_config"]
        assert "initial_balance" in exec_cfg, (
            "execution_config must carry initial_balance from trading_defaults.json"
        )
        assert exec_cfg["initial_balance"] > 0

    def test_execution_config_merges_trading_defaults_under_reference_overrides(self):
        """Reference overrides must win over trading_defaults.json; the rest fills from defaults."""
        # ref-009 in the seed has max_risk_per_trade=0.008 (an override below the 0.01 default).
        # After the merge, that override must be preserved.
        ref = svc.get("ref-009")
        if ref is None:
            pytest.skip("ref-009 not present in seed")
        payload = svc.build_from_reference(reference_id=ref.id)
        exec_cfg = payload["execution_config"]
        # Reference override wins:
        assert exec_cfg["max_risk_per_trade"] == 0.008
        # Default fills in missing fields:
        assert exec_cfg.get("reward_factor") is not None
        assert exec_cfg.get("atr_period") is not None

    def test_no_reference_builds_superseded_stop_keys(self):
        """Regression: MangroveAI's strategy write path refuses the superseded
        stop keys (400 "execution_config names superseded stop parameter(s)").
        Every Oracle-sweep reference carried them, so all 120 failed
        create_strategy_manual. No built payload may carry one."""
        for ref in svc.list_all():
            exec_cfg = svc.build_from_reference(reference_id=ref.id)["execution_config"]
            leaked = svc.SUPERSEDED_STOP_KEYS & set(exec_cfg)
            assert not leaked, f"{ref.id} builds superseded stop keys {sorted(leaked)}"

    def test_seed_data_has_no_superseded_stop_keys(self):
        """The seed itself stays clean, so search results don't advertise
        parameters the platform no longer accepts."""
        for ref in svc.list_all():
            leaked = svc.SUPERSEDED_STOP_KEYS & set(ref.execution_config)
            assert not leaked, f"{ref.id} seed carries superseded stop keys {sorted(leaked)}"


class TestCategoryDetection:
    @pytest.mark.parametrize(
        "hint,expected",
        [
            ("mean reversion bounce", "mean_reversion"),
            ("buy the dip on oversold RSI", "mean_reversion"),
            ("momentum swing trade", "momentum"),
            ("MACD crossover", "momentum"),
            ("breakout above the range", "breakout"),
            ("Ichimoku breakout", "breakout"),
            ("trend following setup", "trend_following"),
            ("golden cross SMA", "trend_following"),
            ("high volatility expansion", "volatility"),
            ("ATR squeeze", "volatility"),
            ("random goal text", None),
            ("", None),
        ],
    )
    def test_detect_category(self, hint, expected):
        assert svc._detect_category(hint) == expected
