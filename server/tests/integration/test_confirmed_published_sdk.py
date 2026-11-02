"""The published SDK carries the confirmed consumer contract without live I/O."""

import json
from importlib.metadata import version
from unittest.mock import MagicMock

import httpx
import pytest
from mangrove_ai import MangroveAI
from src.config import app_config
from src.services import confirmed_execution as confirmed
from src.services import dex_confirmed as dex
from src.services import strategy_service
from src.shared.db import sqlite as db

from tests.integration.test_confirmed_execution import reserve_fill, seed_database


@pytest.mark.parametrize("native_fee,complete", [("0", True), ("0.00001", False)])
@pytest.mark.parametrize("action", ["enter", "exit"])
def test_published_sdk_preserves_book_ack_and_entry_only_hold(tmp_path, monkeypatch, native_fee, complete, action):
    """Known holdings still exit when missing fee valuation blocks new entries."""
    assert tuple(map(int, version("mangroveai").split(".")[:2])) >= (2, 1)
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "published-sdk.db"))
    seed_database(tmp_path / "published-sdk.db")
    execution_config = {
        "initial_balance": 1000, "position_size_calc": "v2", "cooldown_model": "trades",
        "max_drawdown_limit": 0.13, "max_drawdown_halt_limit": 0.17,
        "circuit_breaker": {"consecutive_loss_limit": 2, "loss_amount_pct": 0.04,
                            "pause_bars": 48, "drawdown_pause_bars": 96, "max_consecutive_pauses": 2,
                            "resume_size_fraction": 0.25, "resume_trades": 2,
                            "pause_min_hours": 48, "pause_max_days": 7},
    }
    with db.get_connection() as conn:
        conn.execute("UPDATE strategies SET execution_config_json=? WHERE id='s'", (json.dumps(execution_config),))
    calls = []
    dispatched = MagicMock()
    try:
        reserve_fill(fee=native_fee)

        def serve(request):
            assert request.method == "POST" and request.url.path == "/api/v1/execution/evaluate"
            payload = json.loads(request.content)
            calls.append(payload)
            strategy = payload["strategy"]
            state = strategy["execution_state"]
            assert strategy["execution_config"]["circuit_breaker"] == execution_config["circuit_breaker"]
            for key in ("cooldown_model", "max_drawdown_limit", "max_drawdown_halt_limit"):
                assert strategy["execution_config"][key] == execution_config[key]
            assert payload["persist"] is False
            assert strategy["closed_positions"] == []
            assert strategy["closed_positions_bootstrap_complete"] is complete
            assert state["allow_entries"] is complete
            assert state["cash_balance"] == 800
            assert payload["open_positions"][0]["id"] == "p"
            assert payload["open_positions"][0]["position_size"] == 2
            position_id = "p" if action == "exit" else "next"
            return httpx.Response(200, json={
                "success": True,
                "execution_state": {"risk_state": {"close_history_bootstrapped": True}},
                "open_positions": payload["open_positions"],
                "new_orders": [{"order_id": "proposal", "asset": "BTC-USD", "side": "exit_long" if action == "exit" else "enter_long",
                                "status": "filled", "position_size": 2, "price": 100, "position_id": position_id}],
                "proposed_positions": [{"id": "next", "asset": "BTC-USD", "execution_confirmed": False}],
            })

        with httpx.Client(transport=httpx.MockTransport(serve)) as transport:
            client = MangroveAI(api_key="sk_contract_fixture", base_url="http://127.0.0.1:1/api/v1",
                                load_dotenv=False, httpx_client=transport)
            monkeypatch.setattr(strategy_service, "mangrove_ai_client", lambda: client)
            monkeypatch.setattr(strategy_service.order_executor, "_fetch_mark_price", lambda _: 100)
            monkeypatch.setattr(dex, "reconcile", lambda _: None)
            monkeypatch.setattr(dex, "execute", dispatched)
            strategy_service.tick("s")
        assert len(calls) == 1
        assert dispatched.call_count == int(complete or action == "exit")
        if dispatched.called:
            assert dispatched.call_args.args[1].action == action
        stored = json.loads(strategy_service._get_row("s")["execution_state_json"] or "{}")
        assert bool(stored.get("risk_state", {}).get("close_history_bootstrapped")) is complete
        # An engine proposal never changes the physical journal.
        assert confirmed.dex_inputs("s", 100)[1][0]["id"] == "p"
        with db.get_connection() as conn:
            assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
    finally:
        db.reset_connection()
