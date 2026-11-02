"""Portfolio pauses finalize inside their own tick without reopening tick races."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.services import broker_store as store
from src.services import confirmed_execution as confirmed
from src.services import portfolio_risk_service as portfolio
from src.services import strategy_service as service
from src.shared.db import sqlite as db
from src.shared.errors import SdkError
from tests.integration.test_confirmed_execution import reserve_fill, seed_database


@pytest.fixture
def book(tmp_path, monkeypatch):
    seed_database(tmp_path / "portfolio-tick.db")
    monkeypatch.setattr(service, "scheduler_service", MagicMock())
    monkeypatch.setattr(service, "mangrove_ai_client", lambda: SimpleNamespace(strategies=SimpleNamespace(update_status=MagicMock())))
    with store.connection() as conn:
        conn.execute("UPDATE portfolio_risk SET high_water_mark=5000 WHERE id=1")
    yield
    db.reset_connection()


def test_own_tick_finalizes_halt_and_keeps_physical_protection(book):
    reserve_fill()
    revision = confirmed.snapshot_revision("s")
    with confirmed.tick_lock("s") as acquired:
        assert acquired and confirmed.owns_strategy_tick("s")
        result = portfolio.check_before_live_execution()
        assert result["allowed"] is False and result["tripped"] is True
        row = service._get_row("s")
        assert row["status"] == "inactive" and row["exit_only_mode"] == "live"
        assert service.allocation_service.get_active_allocation("s") is not None
        service.scheduler_service.register_job.assert_called_once()
        service.scheduler_service.cancel_job.assert_not_called()
        state = portfolio._get_state()
        assert portfolio.check_before_live_execution()["allowed"] is False
        assert portfolio._get_state() == state
    assert confirmed.snapshot_revision("s") == revision
    assert confirmed.dex_inputs("s", 100)[1][0]["position_size"] == 2
    assert not confirmed.owns_strategy_tick("s")


def test_own_flat_tick_finalizes_status_and_retires_allocation(book):
    with confirmed.tick_lock("s") as acquired:
        assert acquired
        assert portfolio.check_before_live_execution()["allowed"] is False
    assert service._get_row("s")["status"] == "inactive"
    assert service._get_row("s")["exit_only_mode"] is None
    assert service.allocation_service.get_active_allocation("s") is None
    assert portfolio.get_status()["tripped"] is True
    service.scheduler_service.cancel_job.assert_called_once()


def test_latched_halt_retries_status_when_other_thread_becomes_available(book):
    reserve_fill()
    entered, continue_tick = threading.Event(), threading.Event()
    errors = []

    def worker():
        try:
            with confirmed.tick_lock("s") as acquired:
                assert acquired and confirmed.owns_strategy_tick("s")
                entered.set()
                assert continue_tick.wait(5)
                assert portfolio.check_before_live_execution()["allowed"] is False
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        assert not confirmed.owns_strategy_tick("s")
        assert portfolio.check_before_live_execution()["allowed"] is False
        # The first finalization was deferred by another thread's active tick.
        assert service._get_row("s")["status"] == "live"
        assert portfolio.get_status()["tripped"] is True
        before = portfolio._get_state()
    finally:
        continue_tick.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    row = service._get_row("s")
    assert row["status"] == "inactive" and row["exit_only_mode"] == "live"
    assert portfolio._get_state() == before
    assert service.allocation_service.get_active_allocation("s") is not None


def test_status_owner_cannot_use_internal_tick_bypass_or_start_nested_tick(book, monkeypatch):
    evaluated = MagicMock()
    monkeypatch.setattr(service, "_tick_locked", evaluated)
    with confirmed.tick_lock("s", purpose="status") as acquired:
        assert acquired and not confirmed.owns_strategy_tick("s")
        with pytest.raises(SdkError, match="evaluation is in progress"):
            service.pause_for_portfolio("s")
        service.tick("s")
        evaluated.assert_not_called()
    assert service._get_row("s")["status"] == "live"
    assert not confirmed.owns_strategy_tick("s")
