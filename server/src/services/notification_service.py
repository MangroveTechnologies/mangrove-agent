"""notification_service — outbound Slack notifications for scheduler ticks.

Ported from ``live-strategies/run_cron.py``'s Slack tick formatter (the
OPEN / CLOSE / per-strategy-summary rendering plus outcome-driven icons).
live-strategies bulk-evaluates many strategies per tick and posts one
consolidated message; the agent evaluates ONE strategy per scheduler job
(per-strategy APScheduler jobs in scheduler_service), so each notification
here covers a single strategy's fired orders / closed positions — the
per-strategy analogue of that consolidated message.

Wiring: ``strategy_service.tick()`` calls :func:`notify_tick` after a
successful evaluation and :func:`notify_error` when the SDK evaluate
raises. Both are best-effort and NEVER raise back into the tick — a Slack
outage must not stop strategy evaluation or order execution. We notify
from inside ``tick()`` rather than the scheduler's ``_on_job_event``
listener because the listener only sees ``job_id`` / ``retval`` /
``exception`` (and ``tick`` deliberately swallows exceptions, so a
JOB_ERROR event never fires for an eval job), whereas the rich evaluation
result we need to render lives inside the tick.

Enablement: disabled unless ``SLACK_WEBHOOK_URL`` is configured. Quiet
ticks (no new orders, no closes, no error) post nothing unless
``SLACK_QUIET_IF_EMPTY`` is false. Both are optional config keys (declared
in ``src/config/configuration-keys.json`` under ``optional``,
secret-ref capable, no ``.env``).
"""
from __future__ import annotations

from typing import Any

import httpx

from src.config import app_config
from src.shared.logging import get_logger

_log = get_logger(__name__)

_POST_TIMEOUT_S = 10.0

# MangroveAI's OrderSide enum serializes to these string values. The agent
# stores the SDK evaluate response verbatim (Evaluation.sdk_response), so
# these arrive in the same shape live-strategies sees off /evaluate/bulk.
_ENTRY_SIDES = ("enter_long", "enter_short")
_EXIT_SIDES = ("exit_long", "exit_short")

# Icons are raw Unicode (the Slack workspaces in use render literal text
# for `:shortcode:` emoji, not graphics). Outcome-driven: OPEN is neutral
# white, CLOSE is green on a win and red on a loss; the exit_reason
# (SL / TP / TIME / SIGNAL / MANUAL) appears in the header text.
_OPEN_ICON = "⚪"          # white circle
_WIN_ICON = "\U0001f7e2"       # green circle
_LOSS_ICON = "\U0001f534"      # red circle
_DEFAULT_CLOSE_ICON = "⚫"  # black circle (outcome unknown)


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------


def _slack_webhook() -> str | None:
    """The configured Slack incoming-webhook URL, or None when disabled.

    ``getattr`` keeps this safe even for a config file that predates the
    optional keys (attribute simply resolves to None → disabled)."""
    return getattr(app_config, "SLACK_WEBHOOK_URL", None) or None


def _quiet_if_empty() -> bool:
    """Whether to suppress empty-tick heartbeats (default True).

    Tolerates both a JSON bool (from the config file) and a string form
    (env override), matching live-strategies' parsing."""
    raw = getattr(app_config, "SLACK_QUIET_IF_EMPTY", True)
    if isinstance(raw, bool):
        return raw
    return str(raw if raw is not None else "true").strip().lower() not in ("false", "0", "no", "off")


# ---------------------------------------------------------------------------
# Order / position classification (ported verbatim from run_cron.py)
# ---------------------------------------------------------------------------


def _is_entry(order: dict[str, Any]) -> bool:
    return (order.get("side") or "").lower() in _ENTRY_SIDES


def _is_exit(order: dict[str, Any]) -> bool:
    return (order.get("side") or "").lower() in _EXIT_SIDES


def _is_bracket(order: dict[str, Any]) -> bool:
    """Stop-loss / take-profit orders placed at entry time, awaiting trigger.

    Exit-side SL/TP that haven't filled yet — part of the entry bracket,
    not a close event. MangroveAI activates brackets immediately on entry,
    so they ship in ``new_orders`` at status ``pending`` (transient
    ``inactive`` accepted for safety)."""
    ot = (order.get("order_type") or "").lower()
    status = (order.get("status") or "").lower()
    return _is_exit(order) and status in ("inactive", "pending") and ot in ("stop_loss", "take_profit")


def _is_close_fill(order: dict[str, Any]) -> bool:
    """A fill that actually closes a position this tick.

    Only ``filled`` exits count as closes; pending SL/TP are brackets
    (see :func:`_is_bracket`) and pending non-bracket exits are rare
    in-flight closes we conservatively defer until they fill."""
    if not _is_exit(order):
        return False
    return (order.get("status") or "").lower() == "filled"


def _close_icon(trade: dict[str, Any] | None) -> str:
    """Pick the CLOSE icon by realized outcome; neutral black when unknown."""
    if trade is None:
        return _DEFAULT_CLOSE_ICON
    outcome = (trade.get("outcome") or "").lower()
    if outcome == "win":
        return _WIN_ICON
    if outcome == "loss":
        return _LOSS_ICON
    pl = trade.get("profit_loss")
    if isinstance(pl, (int, float)):
        return _WIN_ICON if pl > 0 else _LOSS_ICON
    return _DEFAULT_CLOSE_ICON


def _short_pid(pid: str | None) -> str:
    if not pid:
        return "?"
    return pid.split("-")[0] if "-" in pid else pid[:8]


# ---------------------------------------------------------------------------
# Formatting (ported verbatim from run_cron.py)
# ---------------------------------------------------------------------------


def _format_order_line(order: dict[str, Any]) -> str:
    side = order.get("side", "?")
    order_type = order.get("order_type", "?")
    size = order.get("position_size")
    price = order.get("price")
    parts = [f"{side}/{order_type}", f"size=`{size}`", f"price=`{price}`"]
    return "    ↳ " + "  ".join(parts)


def _format_open_block(
    entry: dict[str, Any],
    brackets: list[dict[str, Any]],
    pid: str | None,
    asset: str,
    spot: Any,
) -> list[str]:
    """A POSITION OPENED block — entry order + bracket orders as sub-bullets."""
    side = (entry.get("side") or "").replace("_", " ")
    lines = [
        f"• {_OPEN_ICON} *OPEN* — `{asset}` {side}  ·  spot=`{spot}`  ·  pos=`{_short_pid(pid)}`",
        _format_order_line(entry),
    ]
    for b in brackets:
        lines.append(_format_order_line(b))
    return lines


def _fmt_pnl(pl: float | None) -> str:
    if pl is None:
        return "--"
    # Sign before the $, not between: "-$1.64", not "$-1.64".
    sign = "+" if pl >= 0 else "-"
    return f"{sign}${abs(pl):,.2f}"


def _fmt_pnl_pct(entry: float | None, exit_: float | None, side: str | None) -> str:
    if entry in (None, 0) or exit_ is None:
        return "--"
    raw = (exit_ - entry) / entry if (side or "long") == "long" else (entry - exit_) / entry
    sign = "+" if raw >= 0 else ""
    return f"{sign}{raw * 100:.2f}%"


def _fmt_held(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h{int((seconds % 3600) / 60)}m"
    return f"{int(seconds / 86400)}d"


def _format_close_block(
    exit_order: dict[str, Any],
    pid: str | None,
    asset: str,
    spot: Any,
    trade: dict[str, Any] | None = None,
) -> list[str]:
    """A POSITION CLOSED block, linking back to the parent position.

    When a matching ``closed_trades`` entry is attached, the block includes
    realized PnL, percent return, and held duration. Falls back to
    order-only rendering when no trade record is available."""
    reason = (exit_order.get("exit_reason") or "?").upper()
    icon = _close_icon(trade)
    side = (exit_order.get("side") or "").replace("_", " ")

    header = (
        f"• {icon} *CLOSE* ({reason}) — `{asset}` {side}  ·  "
        f"spot=`{spot}`  ·  parent=`{_short_pid(pid)}`"
    )
    lines = [header]

    if trade is not None:
        pl = trade.get("profit_loss")
        pct = _fmt_pnl_pct(trade.get("entry_price"), trade.get("exit_price"), trade.get("side"))
        held = _fmt_held(trade.get("held_seconds"))
        lines.append(
            f"    entry=`{trade.get('entry_price')}` -> exit=`{trade.get('exit_price')}`  "
            f"PnL=`{_fmt_pnl(pl)}` ({pct})  held=`{held}`  "
            f"outcome=`{trade.get('outcome', '?')}`"
        )
        tid = trade.get("trade_id")
        if tid:
            lines.append(f"    trade_id=`{_short_pid(tid)}`")

    lines.append(_format_order_line(exit_order))
    return lines


def _format_summary_line(summary: dict[str, Any]) -> str:
    """The per-strategy rolling-window summary (attached on CLOSE only).

    Source: ``strategy_summary`` returned by MangroveAI evaluate."""
    days = summary.get("window_days", 90)
    n = summary.get("total_trades", 0)
    w = summary.get("wins", 0)
    losses = summary.get("losses", 0)
    wr = summary.get("win_rate_pct")
    wr_str = "--" if wr is None else f"{wr:.1f}%"
    pnl = summary.get("total_pnl", 0.0)
    ret = summary.get("total_return_pct")
    ret_str = "--" if ret is None else f"{ret:+.2f}%"
    open_n = summary.get("num_open_positions", 0)
    open_v = summary.get("open_positions_value", 0.0)
    nav = summary.get("nav")
    nav_str = "--" if nav is None else f"${nav:,.2f}"
    return (
        f"    \U0001f4ca {days}d:  trades=`{n}`  W/L=`{w}/{losses}` ({wr_str})  "
        f"PnL=`{_fmt_pnl(pnl)}` ({ret_str})  "
        f"open=`{open_n}` (${open_v:,.2f})  NAV=`{nav_str}`"
    )


def _format_orphan_block(
    orders: list[dict[str, Any]],
    pid: str | None,
    asset: str,
    spot: Any,
) -> list[str]:
    """Fallback for orders that don't classify as open/close — surfaced
    rather than silently dropped."""
    lines = [
        f"• ❓ *ORDERS* — `{asset}`  ·  spot=`{spot}`  ·  pos=`{_short_pid(pid)}`",
    ]
    for o in orders:
        lines.append(_format_order_line(o))
    return lines


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def build_tick_payload(
    *,
    strategy_name: str,
    asset: str,
    timeframe: str,
    mode: str,
    sdk_dump: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the Slack payload for one strategy's tick.

    Returns None on a quiet tick (no new orders, no closes) when
    ``SLACK_QUIET_IF_EMPTY`` is true, so the caller skips the POST.

    ``sdk_dump`` is the verbatim MangroveAI evaluate response
    (``Evaluation.sdk_response``); ``strategy_name`` / ``asset`` /
    ``timeframe`` / ``mode`` come from the agent's local strategy row."""
    new_orders = sdk_dump.get("new_orders") or []
    closed_trades = sdk_dump.get("closed_trades") or []

    if not new_orders and not closed_trades and _quiet_if_empty():
        return None

    spot = sdk_dump.get("current_price")
    asset = asset or sdk_dump.get("asset") or "?"
    icon = "\U0001f4c8" if (new_orders or closed_trades) else "\U0001f493"  # 📈 / 💓
    lines = [
        f"{icon} *{strategy_name}* — `{asset}` {timeframe}  ·  {mode}",
        f"orders: `{len(new_orders)}`  closes: `{len(closed_trades)}`  spot=`{spot}`",
    ]

    # Group orders by position_id so entry + brackets (+ a same-tick close)
    # stay visually tied together.
    by_pid: dict[str, list[dict[str, Any]]] = {}
    for o in new_orders:
        by_pid.setdefault(o.get("position_id") or "?", []).append(o)

    # Index closed_trades by position_id so a CLOSE block can attach the
    # realized-PnL record without a second lookup.
    trade_by_pid: dict[str, dict[str, Any]] = {
        t.get("position_id"): t for t in closed_trades if t.get("position_id")
    }

    any_close = False
    for pid, pos_orders in by_pid.items():
        entries = [o for o in pos_orders if _is_entry(o)]
        closes = [o for o in pos_orders if _is_close_fill(o)]
        brackets = [o for o in pos_orders if _is_bracket(o)]

        if entries:
            lines.extend(_format_open_block(entries[0], brackets, pid, asset, spot))
        if closes:
            any_close = True
            trade = trade_by_pid.get(pid)
            for c in closes:
                lines.extend(_format_close_block(c, pid, asset, spot, trade=trade))
        if not entries and not closes:
            lines.extend(_format_orphan_block(pos_orders, pid, asset, spot))

    summary = sdk_dump.get("strategy_summary")
    if any_close and isinstance(summary, dict):
        lines.append(_format_summary_line(summary))

    return {
        "text": f"{strategy_name} tick [{timeframe}] orders={len(new_orders)} closes={len(closed_trades)}",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}],
    }


def build_error_payload(
    *,
    strategy_name: str,
    asset: str,
    timeframe: str,
    mode: str,
    error: str,
) -> dict[str, Any]:
    """Build the Slack alert payload for a failed evaluation tick."""
    return {
        "text": f"\U0001f6a8 {strategy_name} evaluate failed [{timeframe}]",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": (
            f"\U0001f6a8 *evaluate failed* — `{strategy_name}` `{asset or '?'}` {timeframe}  ·  {mode}\n"
            f"error: `{str(error)[:400]}`"
        )}}],
    }


# ---------------------------------------------------------------------------
# Transport + public API
# ---------------------------------------------------------------------------


def _post(webhook: str, payload: dict[str, Any]) -> None:
    r = httpx.post(webhook, json=payload, timeout=_POST_TIMEOUT_S)
    r.raise_for_status()


def notify_tick(
    *,
    strategy_name: str,
    asset: str,
    timeframe: str,
    mode: str,
    sdk_dump: dict[str, Any] | None,
) -> bool:
    """Best-effort Slack notification for a completed tick.

    Returns True if a message was posted, False otherwise (disabled, quiet
    tick, or a swallowed failure). NEVER raises — the scheduler tick must
    not break because Slack is down or misconfigured."""
    webhook = _slack_webhook()
    if not webhook:
        return False
    try:
        payload = build_tick_payload(
            strategy_name=strategy_name,
            asset=asset,
            timeframe=timeframe,
            mode=mode,
            sdk_dump=sdk_dump or {},
        )
    except Exception as e:  # noqa: BLE001 — never let formatting break a tick
        _log.warning("notification.build_failed", strategy=strategy_name, kind="tick", error=str(e))
        return False
    if payload is None:
        return False
    try:
        _post(webhook, payload)
    except Exception as e:  # noqa: BLE001
        _log.warning("notification.post_failed", strategy=strategy_name, kind="tick", error=str(e))
        return False
    _log.info("notification.sent", strategy=strategy_name, timeframe=timeframe, kind="tick")
    return True


def notify_error(
    *,
    strategy_name: str,
    asset: str,
    timeframe: str,
    mode: str,
    error: str,
) -> bool:
    """Best-effort Slack alert for a failed evaluation. Never raises."""
    webhook = _slack_webhook()
    if not webhook:
        return False
    try:
        payload = build_error_payload(
            strategy_name=strategy_name,
            asset=asset,
            timeframe=timeframe,
            mode=mode,
            error=error,
        )
        _post(webhook, payload)
    except Exception as e:  # noqa: BLE001
        _log.warning("notification.post_failed", strategy=strategy_name, kind="error", error=str(e))
        return False
    _log.info("notification.sent", strategy=strategy_name, timeframe=timeframe, kind="error")
    return True
