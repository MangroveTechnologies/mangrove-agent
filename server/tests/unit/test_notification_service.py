"""Unit tests for notification_service.

Covers the Slack tick formatter ported from live-strategies (OPEN / CLOSE /
summary blocks, outcome icons, quiet-tick suppression) and the defensive
notify_* transport (disabled when no webhook, never raises on POST failure).

Order/trade payloads use the real MangroveAI evaluate shape (side=enter_long,
order_type, status, position_id, exit_reason, closed_trades) captured from
live-strategies/logs/events.jsonl.
"""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

from src.config import app_config  # noqa: E402
from src.services import notification_service as ns  # noqa: E402

# --- Real-shape fixtures ----------------------------------------------------

PID_OPEN = "911e768e-0c97-4607-8438-30c1d5ce33d5"
PID_CLOSE = "5694e089-ab30-4d9b-a5c2-78217738d96e"


def _entry_order(pid: str = PID_OPEN) -> dict:
    return {
        "order_id": "653af51f-ecdb-4eb5-aea4-27c7392965a5",
        "asset": "AVAX", "side": "enter_long", "order_type": "limit",
        "status": "filled", "price": 9.32, "position_size": 1003.39443428,
        "position_id": pid, "exit_reason": None,
    }


def _bracket_order(pid: str = PID_OPEN) -> dict:
    return {
        "order_id": "9c8c6793-39b2-4e69-b71f-0e0226c0c083",
        "asset": "AVAX", "side": "exit_long", "order_type": "stop_loss",
        "status": "pending", "price": 9.10, "position_size": 1003.39,
        "position_id": pid, "exit_reason": None,
    }


def _close_fill(pid: str = PID_CLOSE) -> dict:
    return {
        "order_id": "c1a2b3", "asset": "AVAX", "side": "exit_long",
        "order_type": "take_profit", "status": "filled", "price": 10.50,
        "position_size": 1003.39, "position_id": pid, "exit_reason": "take_profit",
    }


def _closed_trade(pid: str = PID_CLOSE) -> dict:
    return {
        "position_id": pid, "trade_id": "t-deadbeef", "profit_loss": 42.50,
        "entry_price": 9.32, "exit_price": 10.50, "side": "long",
        "outcome": "win", "held_seconds": 7200,
    }


def _text(payload: dict) -> str:
    return payload["blocks"][0]["text"]["text"]


# --- Quiet-tick suppression -------------------------------------------------


def test_quiet_tick_returns_none_by_default(monkeypatch):
    monkeypatch.setattr(app_config, "SLACK_QUIET_IF_EMPTY", True, raising=False)
    payload = ns.build_tick_payload(
        strategy_name="s", asset="ETH", timeframe="1h", mode="paper",
        sdk_dump={"new_orders": [], "closed_trades": []},
    )
    assert payload is None


def test_quiet_disabled_posts_heartbeat(monkeypatch):
    monkeypatch.setattr(app_config, "SLACK_QUIET_IF_EMPTY", False, raising=False)
    payload = ns.build_tick_payload(
        strategy_name="s", asset="ETH", timeframe="1h", mode="paper",
        sdk_dump={"new_orders": [], "closed_trades": [], "current_price": 2500},
    )
    assert payload is not None
    assert "\U0001f493" in _text(payload)  # heartbeat icon


def test_quiet_if_empty_accepts_string_form(monkeypatch):
    # env-style override: the string "false" must disable quiet.
    monkeypatch.setattr(app_config, "SLACK_QUIET_IF_EMPTY", "false", raising=False)
    assert ns._quiet_if_empty() is False
    monkeypatch.setattr(app_config, "SLACK_QUIET_IF_EMPTY", "true", raising=False)
    assert ns._quiet_if_empty() is True


# --- OPEN / CLOSE rendering -------------------------------------------------


def test_open_block_renders_entry_and_bracket(monkeypatch):
    monkeypatch.setattr(app_config, "SLACK_QUIET_IF_EMPTY", True, raising=False)
    payload = ns.build_tick_payload(
        strategy_name="oracle_AVAX_1h", asset="AVAX", timeframe="1h", mode="paper",
        sdk_dump={"new_orders": [_entry_order(), _bracket_order()],
                  "closed_trades": [], "current_price": 9.32},
    )
    text = _text(payload)
    assert "*OPEN*" in text
    assert ns._OPEN_ICON in text
    assert "enter long" in text          # side underscored -> spaced
    assert "stop_loss" in text           # bracket order line
    assert "1003.39443428" in str(text)  # real position size


def test_close_block_includes_pnl_and_win_icon(monkeypatch):
    monkeypatch.setattr(app_config, "SLACK_QUIET_IF_EMPTY", True, raising=False)
    payload = ns.build_tick_payload(
        strategy_name="oracle_AVAX_1h", asset="AVAX", timeframe="1h", mode="live",
        sdk_dump={"new_orders": [_close_fill()], "closed_trades": [_closed_trade()],
                  "current_price": 10.50,
                  "strategy_summary": {"window_days": 90, "total_trades": 12,
                                       "wins": 8, "losses": 4, "win_rate_pct": 66.7,
                                       "total_pnl": 318.40, "total_return_pct": 3.18,
                                       "num_open_positions": 1, "open_positions_value": 1050.0,
                                       "nav": 10318.40}},
    )
    text = _text(payload)
    assert "*CLOSE* (TAKE_PROFIT)" in text
    assert ns._WIN_ICON in text          # positive PnL -> green
    assert "+$42.50" in text             # realized PnL formatting
    assert "+12.66%" in text             # (10.50-9.32)/9.32
    assert "held=`2h0m`" in text         # 7200s
    assert "\U0001f4ca 90d:" in text     # summary attached on close


def test_loss_close_uses_red_icon(monkeypatch):
    monkeypatch.setattr(app_config, "SLACK_QUIET_IF_EMPTY", True, raising=False)
    trade = _closed_trade()
    trade.update(profit_loss=-15.0, exit_price=9.0, outcome="loss")
    payload = ns.build_tick_payload(
        strategy_name="s", asset="AVAX", timeframe="1h", mode="paper",
        sdk_dump={"new_orders": [_close_fill()], "closed_trades": [trade]},
    )
    text = _text(payload)
    assert ns._LOSS_ICON in text
    assert "-$15.00" in text


def test_error_payload_shape():
    payload = ns.build_error_payload(
        strategy_name="s", asset="ETH", timeframe="1h", mode="paper",
        error="SDK evaluate failed: boom",
    )
    text = _text(payload)
    assert "\U0001f6a8" in text
    assert "evaluate failed" in text
    assert "boom" in text


# --- Formatting helpers -----------------------------------------------------


def test_pnl_and_pct_formatting():
    assert ns._fmt_pnl(42.5) == "+$42.50"
    assert ns._fmt_pnl(-1.64) == "-$1.64"
    assert ns._fmt_pnl(None) == "--"
    assert ns._fmt_pnl_pct(100, 110, "long") == "+10.00%"
    assert ns._fmt_pnl_pct(100, 90, "short") == "+10.00%"   # short inverts
    assert ns._fmt_pnl_pct(None, 110, "long") == "--"
    assert ns._fmt_held(45) == "45s"
    assert ns._fmt_held(7200) == "2h0m"


def test_bracket_vs_close_classification():
    assert ns._is_bracket(_bracket_order()) is True
    assert ns._is_close_fill(_bracket_order()) is False
    assert ns._is_close_fill(_close_fill()) is True
    assert ns._is_entry(_entry_order()) is True


# --- Defensive transport ----------------------------------------------------


def test_notify_disabled_when_no_webhook(monkeypatch):
    monkeypatch.setattr(app_config, "SLACK_WEBHOOK_URL", "", raising=False)
    posted = []
    monkeypatch.setattr(ns, "_post", lambda w, p: posted.append(p))
    sent = ns.notify_tick(strategy_name="s", asset="ETH", timeframe="1h",
                          mode="paper", sdk_dump={"new_orders": [_entry_order()]})
    assert sent is False
    assert posted == []


def test_notify_posts_when_webhook_set(monkeypatch):
    monkeypatch.setattr(app_config, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/x", raising=False)
    monkeypatch.setattr(app_config, "SLACK_QUIET_IF_EMPTY", True, raising=False)
    captured = {}
    monkeypatch.setattr(ns, "_post", lambda w, p: captured.update(url=w, payload=p))
    sent = ns.notify_tick(strategy_name="s", asset="AVAX", timeframe="1h",
                          mode="paper", sdk_dump={"new_orders": [_entry_order()],
                                                  "current_price": 9.32})
    assert sent is True
    assert captured["url"] == "https://hooks.slack.test/x"
    assert "*OPEN*" in _text(captured["payload"])


def test_notify_quiet_tick_does_not_post(monkeypatch):
    monkeypatch.setattr(app_config, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/x", raising=False)
    monkeypatch.setattr(app_config, "SLACK_QUIET_IF_EMPTY", True, raising=False)
    posted = []
    monkeypatch.setattr(ns, "_post", lambda w, p: posted.append(p))
    sent = ns.notify_tick(strategy_name="s", asset="ETH", timeframe="1h",
                          mode="paper", sdk_dump={"new_orders": [], "closed_trades": []})
    assert sent is False
    assert posted == []


def test_notify_swallows_post_failure(monkeypatch):
    monkeypatch.setattr(app_config, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/x", raising=False)

    def _boom(webhook, payload):
        raise RuntimeError("slack 500")

    monkeypatch.setattr(ns, "_post", _boom)
    # Must NOT raise — a Slack outage cannot break a scheduler tick.
    sent = ns.notify_tick(strategy_name="s", asset="ETH", timeframe="1h",
                          mode="paper", sdk_dump={"new_orders": [_entry_order()],
                                                  "current_price": 9.32})
    assert sent is False


def test_notify_error_swallows_failure(monkeypatch):
    monkeypatch.setattr(app_config, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/x", raising=False)
    monkeypatch.setattr(ns, "_post", lambda w, p: (_ for _ in ()).throw(RuntimeError("down")))
    assert ns.notify_error(strategy_name="s", asset="ETH", timeframe="1h",
                           mode="paper", error="boom") is False


# --- Optional-config wiring -------------------------------------------------


def test_optional_keys_default_to_none_under_test_env():
    # test-config.json declares neither SLACK key; the optional-key loader
    # must still define the attributes (as None), so callers never hit
    # AttributeError and Slack stays disabled by default.
    assert app_config.SLACK_WEBHOOK_URL is None
    assert app_config.SLACK_QUIET_IF_EMPTY is None
    # ...and a None SLACK_QUIET_IF_EMPTY resolves to "quiet" (the safe default).
    assert ns._quiet_if_empty() is True
