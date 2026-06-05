"""candidate_generator — goal → 5-10 strategy candidates.

Deterministic heuristic: parse keywords from the user's goal to pick
signal categories, then randomly sample within those buckets. The result
is a list of StrategyCandidate dicts that are ready to pass to
mangroveai.backtesting.run() and eventually mangroveai.strategies.create().

Not an LLM; not Oracle; not anything clever. Just a mapping table plus
random sampling with a user-supplied seed. The "intelligence" lives in
(a) the mapping and (b) the user's choice of goal language.

Composition constraints (enforced):
- entry = exactly 1 TRIGGER + 0–2 FILTERs
- exit  = 0–1 TRIGGER + 0–2 FILTERs (can be empty — strategy may exit only via SL/TP)
"""
from __future__ import annotations

import random
from typing import Any

from pydantic import BaseModel

from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import StrategyNoViableCandidates
from src.shared.logging import get_logger

_log = get_logger(__name__)


# Keyword -> {"trigger": [categories], "filter": [categories]}
# Lowercase keys; multiple keywords can match, we union them.
GOAL_TO_CATEGORIES: dict[str, dict[str, list[str]]] = {
    "momentum": {
        "trigger": ["momentum", "trend"],
        "filter": ["volume", "trend"],
    },
    "mean_reversion": {
        "trigger": ["overbought_oversold", "oscillator"],
        "filter": ["volatility", "trend"],
    },
    "breakout": {
        "trigger": ["breakout"],
        "filter": ["volume", "volatility"],
    },
    "trend": {
        "trigger": ["trend"],
        "filter": ["momentum", "volume"],
    },
    "oversold": {
        "trigger": ["overbought_oversold"],
        "filter": ["volatility"],
    },
    "overbought": {
        "trigger": ["overbought_oversold"],
        "filter": ["volatility"],
    },
    "volume": {
        "trigger": ["volume"],
        "filter": ["momentum"],
    },
}

# Aliases that collapse to a canonical keyword.
_KEYWORD_ALIASES: dict[str, str] = {
    "mean-reversion": "mean_reversion",
    "meanreversion": "mean_reversion",
    "revert": "mean_reversion",
    "reversion": "mean_reversion",
    "break out": "breakout",
    "break-out": "breakout",
    "trending": "trend",
    "trends": "trend",
    "momentum-based": "momentum",
}


class StrategyCandidate(BaseModel):
    """A proposed strategy — ready to backtest or submit to mangroveai."""

    name: str
    asset: str
    timeframe: str
    entry: list[dict[str, Any]]
    exit: list[dict[str, Any]]


def parse_goal(goal: str) -> dict[str, list[str]]:
    """Detect goal keywords and return the unioned category buckets.

    Defaults to "momentum" when nothing matches so we always produce
    candidates; the user can try different phrasing if they don't like
    what comes back.
    """
    text = goal.lower()
    for alias, canonical in _KEYWORD_ALIASES.items():
        text = text.replace(alias, canonical)

    matched = [k for k in GOAL_TO_CATEGORIES if k in text]
    if not matched:
        matched = ["momentum"]

    triggers: set[str] = set()
    filters: set[str] = set()
    for kw in matched:
        triggers.update(GOAL_TO_CATEGORIES[kw]["trigger"])
        filters.update(GOAL_TO_CATEGORIES[kw]["filter"])

    return {
        "keywords_matched": matched,
        "trigger_categories": sorted(triggers),
        "filter_categories": sorted(filters),
    }


def _default_params(signal: Any) -> dict[str, Any]:
    """Extract sensible default parameter values from a signal's metadata.

    mangroveai's Signal.metadata.params is a dict describing each param
    (type, default, min, max). We want {param_name: default_value}.
    If a default isn't present we skip the param and let the SDK apply
    its own defaults.
    """
    meta = getattr(signal, "metadata", None)
    params_spec = getattr(meta, "params", None) if meta else None
    if not params_spec:
        return {}
    out: dict[str, Any] = {}
    for pname, pspec in params_spec.items():
        if isinstance(pspec, dict) and "default" in pspec:
            out[pname] = pspec["default"]
        elif not isinstance(pspec, dict):
            # Some specs might just be the default value itself.
            out[pname] = pspec
    return out


def _signal_rule(signal: Any, timeframe: str) -> dict[str, Any]:
    return {
        "name": signal.name,
        "signal_type": signal.signal_type or "TRIGGER",
        "timeframe": timeframe,
        "params": _default_params(signal),
    }


def _bucket_signals(
    signals: list[Any],
    categories: list[str],
    signal_type: str,
) -> list[Any]:
    """Return signals whose category is in the bucket AND type matches."""
    cat_set = {c.lower() for c in categories}
    return [
        s for s in signals
        if (s.category or "").lower() in cat_set
        and (s.signal_type or "").upper() == signal_type.upper()
    ]


def _fetch_catalog() -> list[Any]:
    """Pull the full signal catalog via the SDK (paginated)."""
    client = mangrove_ai_client()
    return list(client.signals.list_iter(limit_per_page=100))


def _build_entry(
    rng: random.Random,
    trigger_pool: list[Any],
    filter_pool: list[Any],
    timeframe: str,
) -> tuple[list[dict[str, Any]], Any]:
    """Pick one trigger + 0-2 filters for the entry side. Returns (rules, trigger)."""
    entry_trigger = rng.choice(trigger_pool)
    n_filters = rng.randint(0, min(2, len(filter_pool)))
    filter_picks = rng.sample(filter_pool, n_filters) if n_filters else []
    rules = [_signal_rule(entry_trigger, timeframe)]
    rules += [_signal_rule(s, timeframe) for s in filter_picks]
    return rules, entry_trigger


def _build_exit(
    rng: random.Random,
    trigger_pool: list[Any],
    filter_pool: list[Any],
    timeframe: str,
    entry_trigger: Any,
) -> list[dict[str, Any]]:
    """Pick 0-1 trigger + 0-2 filters for the exit side, avoiding entry trigger duplication."""
    exit_rules: list[dict[str, Any]] = []
    if rng.random() < 0.5 and trigger_pool:
        exit_trigger = rng.choice(trigger_pool)
        # Avoid picking the exact same signal we used for entry.
        if exit_trigger.name == entry_trigger.name and len(trigger_pool) > 1:
            alt_triggers = [s for s in trigger_pool if s.name != entry_trigger.name]
            exit_trigger = rng.choice(alt_triggers)
        exit_rules.append(_signal_rule(exit_trigger, timeframe))
    n_filters = rng.randint(0, min(2, len(filter_pool)))
    exit_rules += [_signal_rule(s, timeframe) for s in rng.sample(filter_pool, n_filters)] if n_filters else []
    return exit_rules


def generate(
    goal: str,
    asset: str,
    timeframe: str,
    n: int = 7,
    seed: int | None = None,
) -> list[StrategyCandidate]:
    """Produce `n` candidate strategies matching the goal.

    Args:
        goal: Natural-language goal (e.g. "momentum on ETH").
        asset: Asset symbol (e.g. "ETH").
        timeframe: Bar timeframe (e.g. "1h").
        n: Number of candidates (clamped to [5, 10]).
        seed: Optional deterministic seed for reproducibility.

    Raises StrategyNoViableCandidates if the SDK returns no signals in
    the matched categories.
    """
    n = max(5, min(10, n))
    rng = random.Random(seed)

    parsed = parse_goal(goal)
    catalog = _fetch_catalog()

    trigger_pool = _bucket_signals(catalog, parsed["trigger_categories"], "TRIGGER")
    filter_pool = _bucket_signals(catalog, parsed["filter_categories"], "FILTER")

    if not trigger_pool:
        raise StrategyNoViableCandidates(
            f"No TRIGGER signals available in categories {parsed['trigger_categories']} for goal '{goal}'.",
            suggestion="Try a different goal keyword (momentum, mean_reversion, breakout, trend) or broaden the timeframe.",
        )

    candidates: list[StrategyCandidate] = []
    for i in range(n):
        entry, entry_trigger = _build_entry(rng, trigger_pool, filter_pool, timeframe)
        exit_rules = _build_exit(rng, trigger_pool, filter_pool, timeframe, entry_trigger)

        candidates.append(StrategyCandidate(
            name=f"auto-{goal[:20].replace(' ', '-')}-{i+1}",
            asset=asset,
            timeframe=timeframe,
            entry=entry,
            exit=exit_rules,
        ))

    _log.info(
        "candidate_generator.generated",
        goal=goal,
        asset=asset,
        timeframe=timeframe,
        keywords_matched=parsed["keywords_matched"],
        trigger_pool_size=len(trigger_pool),
        filter_pool_size=len(filter_pool),
        n_candidates=len(candidates),
    )
    return candidates
