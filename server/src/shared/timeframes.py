"""Canonical timeframe whitelist + recommended-lookback mapping.

Mirrors MangroveAI's `SUPPORTED_TIMEFRAMES` (utils/time_utils.py) and the
docstring contract in `ai_copilot/agentic/prompt_builder.py` that
declares per-timeframe lookback defaults. This is the single source of
truth on the mangrove-agent side so strategy creation, backtesting, and
tool descriptions stay consistent.

Upstream reference (MangroveAI):
  utils/time_utils.py
      SUPPORTED_TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h", "1D")
  ai_copilot/agentic/prompt_builder.py
      "5m/15m/30m/1h: 3 months
       4h: 6 months
       1d: 12 months"

NOTE: 1m is NOT supported. The upstream API dataclass docstring and the
backtesting route model mention "1m" in their examples (historical noise),
but the server's `_TIMEFRAME_TO_MINUTES` map does not contain "1m" —
`normalize_timeframe("1m")` silently falls back to "1h". We reject 1m
up front with a clear error so the agent doesn't create a strategy it
can't run.
"""
from __future__ import annotations

from src.shared.errors import ValidationError

# Canonical form. "1d" and "1D" both normalize to "1d" on our side for
# consistency; the server tolerates both via `normalize_timeframe`.
SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "30m", "1h", "4h", "1d")

# Aliases the user or agent might pass — we accept any of these and
# canonicalize before validation. Keep conservative: anything outside
# this set is an error, not a silent normalization.
_ALIASES: dict[str, str] = {
    "5m": "5m", "5min": "5m", "5MIN": "5m",
    "15m": "15m", "15min": "15m", "15MIN": "15m",
    "30m": "30m", "30min": "30m", "30MIN": "30m",
    "1h": "1h", "1hr": "1h", "1HR": "1h",
    "4h": "4h", "4hr": "4h", "4HR": "4h",
    "1d": "1d", "1D": "1d", "1day": "1d", "1DAY": "1d",
}

# Recommended default lookback per timeframe, matching upstream's
# documented defaults. Only applied when the caller does NOT provide
# explicit lookback_months / lookback_days / lookback_hours / start_date /
# end_date. Caller overrides always win.
_RECOMMENDED_LOOKBACK_MONTHS: dict[str, int] = {
    "5m": 3,
    "15m": 3,
    "30m": 3,
    "1h": 3,
    "4h": 6,
    "1d": 12,
}


def canonicalize_timeframe(tf: str | None) -> str:
    """Normalize a timeframe string to canonical form.

    Raises ValidationError if the input is missing or not in SUPPORTED_TIMEFRAMES
    (after alias resolution).
    """
    if not tf:
        raise ValidationError(
            "timeframe is required",
            suggestion=f"Supported: {', '.join(SUPPORTED_TIMEFRAMES)}",
        )
    key = str(tf).strip()
    canonical = _ALIASES.get(key) or _ALIASES.get(key.lower()) or _ALIASES.get(key.upper())
    if canonical is None or canonical not in SUPPORTED_TIMEFRAMES:
        raise ValidationError(
            f"timeframe '{tf}' is not supported",
            suggestion=(
                f"Supported timeframes: {', '.join(SUPPORTED_TIMEFRAMES)}. "
                "Sub-5-minute resolutions (e.g. 1m) are not available on the "
                "MangroveAI data source; the server would silently fall back "
                "to 1h, producing misleading backtest results."
            ),
        )
    return canonical


def recommended_lookback_months(tf: str) -> int:
    """Return the recommended lookback window (in months) for a given timeframe.

    Matches upstream's `ai_copilot/agentic/prompt_builder.py` declared
    defaults: shorter timeframes need shorter history; 1d needs a full year
    to produce statistically meaningful backtests.

    Raises ValidationError via canonicalize_timeframe if tf is unsupported.
    """
    canonical = canonicalize_timeframe(tf)
    return _RECOMMENDED_LOOKBACK_MONTHS[canonical]
