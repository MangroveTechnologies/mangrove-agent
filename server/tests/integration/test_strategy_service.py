"""Integration tests for strategy_service — CRUD + lifecycle + tick.

These exercise the full stack against the real SQLite, with mocked
Mangrove + DEX SDKs. The autonomous path and tick path touch enough
collaborators (candidate_generator, backtest_service, order_executor,
allocation_service, scheduler_service) that this lives in integration
rather than unit.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "ss.db"
    from src.config import app_config
    from src.services import scheduler_service as ss
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    ss.reset_scheduler_cache()

    from src.shared.db.sqlite import get_connection, init_db
    init_db()
    ss.start()  # so register_job works

    # Seed a wallet for allocations. backup_confirmed_at is set so the
    # live-promotion gate in strategy_service.update_status passes without
    # requiring tests to go through the CLI confirm-backup flow.
    get_connection().execute(
        """INSERT INTO wallets
           (id, address, chain, network, chain_id, encrypted_secret,
            encryption_method, label, created_at, metadata_json,
            backup_confirmed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("w1", "0xabc", "evm", "testnet", 84532, b"ciphertext",
         "fernet-v1", "test", "2026-04-20T00:00:00+00:00", None,
         "2026-04-20T00:00:00+00:00"),
    )
    get_connection().commit()

    yield db_file
    ss.reset_scheduler_cache()
    db_mod.reset_connection()


@pytest.fixture
def mock_ai_sdk(monkeypatch):
    """Stub mangrove_ai_client used by strategy_service + candidate_generator + backtest_service."""
    from tests.unit.test_candidate_generator import _catalog

    client = MagicMock()
    client.signals.list_iter.side_effect = lambda **kw: iter(_catalog())

    # Backtests always succeed with the same metrics.
    bt_result = MagicMock()
    bt_result.success = True
    bt_result.metrics = {
        "irr_annualized": 0.4,
        "win_rate": 0.6,
        "total_trades": 25,
        "sharpe_ratio": 1.5,
        "max_drawdown": 0.1,
        "net_pnl": 2500.0,
    }
    bt_result.trade_count = 25
    bt_result.trade_history = []
    bt_result.error = None
    client.backtesting.run.return_value = bt_result

    # strategies.create: return a fresh mock with a unique id per call,
    # so the DB's UNIQUE(mangrove_id) constraint is respected.
    _counter = {"n": 0}

    def _fresh_create(_request):
        _counter["n"] += 1
        m = MagicMock()
        m.id = f"mg-new-{_counter['n']}"
        m.name = getattr(_request, "name", "auto")
        m.asset = getattr(_request, "asset", "ETH")
        m.status = "inactive"
        return m

    client.strategies.create.side_effect = _fresh_create

    # update_status returns SuccessResponse
    success = MagicMock()
    success.success = True
    client.strategies.update_status.return_value = success

    # execute.evaluate — empty by default
    eval_resp = MagicMock()
    eval_resp.new_orders = None
    eval_resp.order_intents = []
    eval_resp.orders = None
    eval_resp.model_dump.return_value = {"orders": []}
    client.execution.evaluate.return_value = eval_resp

    for path in (
        "src.services.candidate_generator.mangrove_ai_client",
        "src.services.backtest_service.mangrove_ai_client",
        "src.services.strategy_service.mangrove_ai_client",
    ):
        monkeypatch.setattr(path, lambda c=client: c)
    return client


def test_create_autonomous_happy_path(temp_db, mock_ai_sdk):
    from src.services.strategy_service import StrategyAutonomousRequest, create_autonomous

    detail, report = create_autonomous(StrategyAutonomousRequest(
        goal="momentum on ETH", asset="ETH", timeframe="1h",
        candidate_count=5, backtest_lookback_months=3, seed=42,
    ))
    assert detail.mangrove_id.startswith("mg-new-")
    assert detail.asset == "ETH"
    assert report["candidates_tried"] == 5
    assert report["candidates_passed_filter"] >= 1
    assert report["winner_rank"] == 1
    assert "irr_annualized" in report["full_backtest_metrics"]


def test_create_autonomous_no_viable_candidates(temp_db, mock_ai_sdk):
    """If every backtest fails the filter, raise StrategyNoViableCandidates."""
    from src.services.strategy_service import StrategyAutonomousRequest, create_autonomous
    from src.shared.errors import StrategyNoViableCandidates

    # Drop win_rate under the threshold.
    mock_ai_sdk.backtesting.run.return_value.metrics["win_rate"] = 0.3

    with pytest.raises(StrategyNoViableCandidates):
        create_autonomous(StrategyAutonomousRequest(
            goal="momentum on ETH", asset="ETH", timeframe="1h",
            candidate_count=5, seed=1,
        ))


def test_create_manual_happy_path(temp_db, mock_ai_sdk):
    from src.services.strategy_service import StrategyManualRequest, create_manual

    detail = create_manual(StrategyManualRequest(
        name="my strategy",
        asset="ETH",
        timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h", "params": {}}],
        exit=[],
    ))
    assert detail.mangrove_id.startswith("mg-new-")


def test_create_manual_invalid_composition_raises(temp_db, mock_ai_sdk):
    from src.services.strategy_service import StrategyManualRequest, create_manual
    from src.shared.errors import StrategyInvalidComposition

    with pytest.raises(StrategyInvalidComposition):
        create_manual(StrategyManualRequest(
            name="bad", asset="ETH", timeframe="1h",
            entry=[  # two triggers — not allowed
                {"name": "rsi_oversold", "signal_type": "TRIGGER"},
                {"name": "macd_cross_up", "signal_type": "TRIGGER"},
            ],
        ))


def test_list_and_get_strategy(temp_db, mock_ai_sdk):
    from src.services.strategy_service import StrategyManualRequest, create_manual, get_strategy, list_strategies

    a = create_manual(StrategyManualRequest(
        name="a", asset="ETH", timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h"}],
    ))
    b = create_manual(StrategyManualRequest(
        name="b", asset="BTC", timeframe="4h",
        entry=[{"name": "macd_cross_up", "signal_type": "TRIGGER", "timeframe": "4h"}],
    ))
    items = list_strategies()
    ids = {s.id for s in items}
    assert a.id in ids
    assert b.id in ids

    fetched = get_strategy(a.id)
    assert fetched.asset == "ETH"


def test_status_paper_to_live_requires_confirm(temp_db, mock_ai_sdk):
    from src.services.strategy_service import (
        StrategyAllocationInput,
        StrategyManualRequest,
        StrategyStatusUpdate,
        create_manual,
        update_status,
    )
    from src.shared.errors import ConfirmationRequired

    s = create_manual(StrategyManualRequest(
        name="s", asset="ETH", timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h"}],
    ))
    # draft -> inactive -> paper first
    update_status(s.id, StrategyStatusUpdate(status="inactive"))
    update_status(s.id, StrategyStatusUpdate(status="paper"))
    # paper -> live requires confirm
    with pytest.raises(ConfirmationRequired):
        update_status(
            s.id,
            StrategyStatusUpdate(
                status="live",
                allocation=StrategyAllocationInput(
                    wallet_address="0xabc", token="USDC",
                    token_address="0xusdc", amount=100,
                    slippage_pct=0.002,
                ),
            ),
        )


def test_status_to_live_registers_cron_and_allocation(temp_db, mock_ai_sdk):
    from src.services.allocation_service import get_active_allocation
    from src.services.scheduler_service import active_job_count
    from src.services.strategy_service import (
        StrategyAllocationInput,
        StrategyManualRequest,
        StrategyStatusUpdate,
        create_manual,
        update_status,
    )

    s = create_manual(StrategyManualRequest(
        name="s", asset="ETH", timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h"}],
    ))
    update_status(s.id, StrategyStatusUpdate(status="inactive"))
    result = update_status(
        s.id,
        StrategyStatusUpdate(
            status="live",
            confirm=True,
            allocation=StrategyAllocationInput(
                wallet_address="0xabc", token="USDC",
                token_address="0xusdc", amount=100,
                slippage_pct=0.002,
            ),
        ),
    )
    assert result.status == "live"
    alloc = get_active_allocation(s.id)
    assert alloc is not None
    assert alloc.amount == 100
    assert active_job_count() == 1


def test_deactivating_live_releases_allocation_and_cancels_cron(temp_db, mock_ai_sdk):
    from src.services.allocation_service import get_active_allocation
    from src.services.scheduler_service import active_job_count
    from src.services.strategy_service import (
        StrategyAllocationInput,
        StrategyManualRequest,
        StrategyStatusUpdate,
        create_manual,
        update_status,
    )

    s = create_manual(StrategyManualRequest(
        name="s", asset="ETH", timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h"}],
    ))
    update_status(s.id, StrategyStatusUpdate(status="inactive"))
    update_status(
        s.id,
        StrategyStatusUpdate(
            status="live",
            confirm=True,
            allocation=StrategyAllocationInput(
                wallet_address="0xabc", token="USDC",
                token_address="0xusdc", amount=100,
                slippage_pct=0.002,
            ),
        ),
    )
    update_status(s.id, StrategyStatusUpdate(status="inactive", confirm=True))

    assert get_active_allocation(s.id) is None
    assert active_job_count() == 0


def test_tick_paper_mode_logs_simulated_trade(temp_db, mock_ai_sdk, monkeypatch):
    """tick fires evaluate, dispatches returned orders, logs everything."""
    from src.services.strategy_service import (
        StrategyManualRequest,
        StrategyStatusUpdate,
        create_manual,
        tick,
        update_status,
    )
    from src.services.trade_log import list_evaluations, list_trades

    # SDK returns one order intent.
    eval_resp = MagicMock()
    eval_resp.new_orders = None
    eval_resp.order_intents = [
        {"action": "enter", "side": "buy", "symbol": "ETH",
         "amount": 0.1, "reason": "rsi_oversold fired"},
    ]
    eval_resp.orders = None
    eval_resp.model_dump.return_value = {"orders": ["something"]}
    mock_ai_sdk.execution.evaluate.return_value = eval_resp

    # Stub crypto_assets.get_market_data for paper-mode mark price.
    md = MagicMock()
    md.data = {"current_price": 2500.0}
    mock_ai_sdk.crypto_assets.get_market_data.return_value = md
    monkeypatch.setattr("src.services.order_executor.mangrove_ai_client",
                        lambda: mock_ai_sdk)

    s = create_manual(StrategyManualRequest(
        name="tickee", asset="ETH", timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h"}],
    ))
    update_status(s.id, StrategyStatusUpdate(status="inactive"))
    update_status(s.id, StrategyStatusUpdate(status="paper"))

    tick(s.id)

    evals = list_evaluations(s.id)
    assert len(evals) == 1
    assert evals[0].status == "ok"
    trades = list_trades(s.id)
    assert len(trades) == 1
    assert trades[0].mode == "paper"
    assert trades[0].status == "simulated"


def test_tick_catches_sdk_errors(temp_db, mock_ai_sdk):
    """SDK failure during tick logs evaluation with status=error, does NOT raise."""
    from src.services.strategy_service import (
        StrategyManualRequest,
        StrategyStatusUpdate,
        create_manual,
        tick,
        update_status,
    )
    from src.services.trade_log import list_evaluations

    mock_ai_sdk.execution.evaluate.side_effect = RuntimeError("upstream 500")

    s = create_manual(StrategyManualRequest(
        name="err", asset="ETH", timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h"}],
    ))
    update_status(s.id, StrategyStatusUpdate(status="inactive"))
    update_status(s.id, StrategyStatusUpdate(status="paper"))

    tick(s.id)  # must not raise

    evals = list_evaluations(s.id)
    assert len(evals) == 1
    assert evals[0].status == "error"
    assert "upstream 500" in (evals[0].error_msg or "")


def test_invalid_transition_raises(temp_db, mock_ai_sdk):
    from src.services.strategy_service import (
        StrategyManualRequest,
        StrategyStatusUpdate,
        create_manual,
        update_status,
    )
    from src.shared.errors import StrategyInvalidStatusTransition

    s = create_manual(StrategyManualRequest(
        name="s", asset="ETH", timeframe="1h",
        entry=[{"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h"}],
    ))
    # draft -> live is illegal (must go through inactive then paper)
    with pytest.raises(StrategyInvalidStatusTransition):
        update_status(s.id, StrategyStatusUpdate(status="live", confirm=True))


def test_get_missing_strategy_raises(temp_db, mock_ai_sdk):
    from src.services.strategy_service import get_strategy
    from src.shared.errors import StrategyNotFound

    with pytest.raises(StrategyNotFound):
        get_strategy("does-not-exist")
