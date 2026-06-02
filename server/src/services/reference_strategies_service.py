"""reference_strategies_service — curated seed library of known-good strategies.

Mechanism 2 from the /create-strategy skill: instead of library-default
parameter guessing, the agent pulls from a curated set of strategies
that are known to backtest reasonably on the (asset, timeframe) combo.

Today this is backed by a hand-curated JSON file. When MangroveOracle
issue #156 + the MangroveAI reference-strategies endpoint land, this
service swaps its backend to `mangroveai.reference_strategies.search()`
with no change to callers. The interface here mirrors what that
endpoint will return.

See docs in `data/reference_strategies.json`.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from src.shared import timeframes
from src.shared.logging import get_logger

_log = get_logger(__name__)

_DATA_PATH = Path(__file__).parent / "data" / "reference_strategies.json"


class ReferenceSignal(BaseModel):
    name: str
    signal_type: str  # TRIGGER | FILTER
    params: dict[str, Any]


class ReferenceStrategy(BaseModel):
    id: str
    label: str
    asset: str
    timeframe: str
    category: str  # momentum | mean_reversion | trend_following | breakout | volatility
    description: str
    entry_signals: list[ReferenceSignal]
    exit_signals: list[ReferenceSignal]
    execution_config: dict[str, Any]
    source: str
    notes: str = ""


def _load_execution_defaults() -> dict[str, Any]:
    """Flat execution_config defaults, merged from canon trading_defaults.

    Reference strategies in the seed JSON store only per-strategy *overrides*
    (e.g. max_risk_per_trade=0.008). The upstream SDK's strategies.create
    endpoint requires a full flat execution_config including fields like
    initial_balance, reward_factor, atr_period, etc. This loader produces
    that flat dict.

    Single-source via backtest_service.flattened_defaults() — fetched from
    the public canon endpoint with offline-startup fallback. See
    backtest_service for the lazy-cache + fallback semantics.

    Mechanism note: before this existed, build_from_reference returned only
    the reference's override dict, and downstream create_strategy_manual
    hit a 500 on the missing initial_balance key. The reference data is
    intentionally sparse — the merge responsibility lives here.
    """
    # Local import avoids circular: backtest_service imports nothing from
    # reference_strategies_service; this is the only direction.
    from src.services.backtest_service import flattened_defaults
    return flattened_defaults()


@lru_cache(maxsize=1)
def _load_all() -> list[ReferenceStrategy]:
    """Read + validate the JSON seed file. Cached for process lifetime."""
    if not _DATA_PATH.is_file():
        _log.warning("reference_strategies.seed_missing", path=str(_DATA_PATH))
        return []
    raw = json.loads(_DATA_PATH.read_text())
    items = raw.get("strategies", [])
    parsed: list[ReferenceStrategy] = []
    for i, item in enumerate(items):
        try:
            parsed.append(ReferenceStrategy.model_validate(item))
        except Exception as e:  # noqa: BLE001 — validation errors are loud enough in logs
            _log.warning("reference_strategies.invalid_entry", index=i, error=str(e), id=item.get("id"))
    _log.info("reference_strategies.loaded", count=len(parsed), path=str(_DATA_PATH))
    return parsed


_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("mean_reversion", ("mean revers", "bollinger", "oversold", "bounce", "buy the dip")),
    ("breakout",       ("breakout", "donchian", "channel", "range expansion", "ichimoku")),
    ("momentum",       ("momentum", "macd", "roc", "stochastic", "rsi cross")),
    ("volatility",     ("volatility", "atr", "squeeze", "high vol", "vol expansion")),
    ("trend_following", ("trend", "ema cross", "sma cross", "golden cross", "adx", "supertrend")),
]


def _detect_category(text: str) -> str | None:
    """Auto-detect intended strategy category from a user-supplied goal/style string.

    Mirrors ai_copilot's `_detect_strategy_type` in shape.
    """
    t = (text or "").lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(k in t for k in keywords):
            return category
    return None


def _score_reference(
    r: ReferenceStrategy,
    asset_u: str,
    tf: str | None,
    cat: str | None,
) -> int:
    """Score a reference strategy by how specifically it matches the filters.

    Higher = better match. Used for ranking in search().
    """
    s = 0
    if asset_u and r.asset.upper() == asset_u:
        s += 8
    if tf and timeframes.canonicalize_timeframe(r.timeframe) == tf:
        s += 4
    if cat and r.category.lower() == cat:
        s += 2
    return s


def _pad_results(
    ranked: list[ReferenceStrategy],
    top: list[ReferenceStrategy],
    limit: int,
) -> list[ReferenceStrategy]:
    """Fill `top` with lower-scored entries from `ranked` until `limit` is reached."""
    seen = {r.id for r in top}
    for r in ranked:
        if r.id not in seen:
            top.append(r)
            if len(top) >= limit:
                break
    return top[:limit]


def search(
    asset: str,
    timeframe: str | None = None,
    category: str | None = None,
    goal_hint: str | None = None,
    limit: int = 5,
) -> list[ReferenceStrategy]:
    """Return up to `limit` reference strategies matching the filter.

    Ranking (most → least specific):
      1. exact asset + exact timeframe + exact category match
      2. exact asset + exact timeframe (any category)
      3. exact asset (any timeframe or category)
      4. exact category match only (for cross-asset learnings)
      5. everything else, capped at `limit`

    If `category` is None and `goal_hint` is set, a category is auto-
    detected from the hint via `_detect_category`.
    """
    asset_u = (asset or "").upper().strip()
    tf = timeframes.canonicalize_timeframe(timeframe) if timeframe else None
    cat = (category or _detect_category(goal_hint or "") or None)
    if cat:
        cat = cat.lower()

    all_refs = _load_all()

    ranked = sorted(all_refs, key=lambda r: (-_score_reference(r, asset_u, tf, cat), r.id))
    # Drop any with score 0 ONLY if we have better matches; otherwise fall
    # through to show something rather than nothing.
    top = [r for r in ranked if _score_reference(r, asset_u, tf, cat) > 0]
    if len(top) >= limit:
        return top[:limit]
    return _pad_results(ranked, top, limit)


def get(reference_id: str) -> ReferenceStrategy | None:
    """Look up a single reference by id."""
    for r in _load_all():
        if r.id == reference_id:
            return r
    return None


def list_all() -> list[ReferenceStrategy]:
    """Return the whole seed set. Mainly for diagnostics / docs."""
    return list(_load_all())


def build_from_reference(
    reference_id: str,
    timeframe_override: str | None = None,
    asset_override: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Produce a create_strategy_manual-compatible payload from a reference.

    Copies the reference's signals EXACTLY; the caller can retarget the
    payload onto a different asset (`asset_override`) or timeframe
    (`timeframe_override`) — signal names and params do not change, only the
    strategy's `asset` field and each signal's `timeframe` field. Reference
    strategies are portable: a combo that worked on BTC 1h is a candidate
    to bulk-backtest on any other asset or timeframe.

    Raises ValueError if reference_id is unknown.
    """
    ref = get(reference_id)
    if ref is None:
        raise ValueError(f"reference_id {reference_id!r} not found")

    tf = timeframes.canonicalize_timeframe(timeframe_override or ref.timeframe)
    asset = (asset_override or ref.asset).upper().strip()

    def _to_rule(sig: ReferenceSignal) -> dict[str, Any]:
        return {
            "name": sig.name,
            "signal_type": sig.signal_type,
            "timeframe": tf,
            "params": dict(sig.params),
        }

    entry = [_to_rule(s) for s in ref.entry_signals]
    exit_rules = [_to_rule(s) for s in ref.exit_signals]

    # Flatten trading_defaults.json, then let the reference's overrides win.
    # Keeps the reference data sparse (overrides only) while producing a
    # payload the SDK will accept without extra patching by the caller.
    exec_cfg = dict(_load_execution_defaults())
    exec_cfg.update(dict(ref.execution_config))

    return {
        "name": name or f"{ref.label} [from {ref.id}]",
        "asset": asset,
        "timeframe": tf,
        "entry": entry,
        "exit": exit_rules,
        "execution_config": exec_cfg,
        "source_reference_id": ref.id,
    }
