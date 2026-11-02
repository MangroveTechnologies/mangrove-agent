"""Interactive authorization cancellation, with no live broker access."""

import asyncio
import importlib.util
import io
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('robinhood_connect', ROOT / 'scripts/robinhood_connect.py')
connector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(connector)


@pytest.mark.asyncio
async def test_success_automatically_finishes_and_cancels_terminal_reader(monkeypatch):
    monkeypatch.setattr(connector.sys.stdin, 'isatty', lambda: True)
    stopped = asyncio.Event()

    async def reader():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(connector, 'connect', AsyncMock())
    monkeypatch.setattr(connector, 'wait_for_skip', reader)
    assert await connector.run_connection() == 0
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_skip_cancels_oauth_and_releases_callback_port(monkeypatch, tmp_path):
    monkeypatch.setattr(connector.sys.stdin, 'isatty', lambda: True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(connector, 'CALLBACK', f'http://127.0.0.1:{port}/callback')
    monkeypatch.setattr(connector.app_config, 'DB_PATH', str(tmp_path / 'agent.db'))
    waiting, closed = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def session(**kwargs):
        try:
            waiting.set()
            await kwargs['callback_handler']()
            yield None
        finally:
            closed.set()

    async def skip():
        await waiting.wait()

    monkeypatch.setattr(connector, 'session', session)
    monkeypatch.setattr(connector, 'wait_for_skip', skip)
    catalog = AsyncMock()
    monkeypatch.setattr(connector, 'catalog', catalog)
    assert await connector.run_connection() == 2
    assert closed.is_set()
    catalog.assert_not_called()
    assert not (tmp_path / 'robinhood-tools.json').exists()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))  # No orphan callback listener.


@pytest.mark.asyncio
async def test_failure_cleans_up_reader_and_is_not_reported_as_skip(monkeypatch):
    monkeypatch.setattr(connector.sys.stdin, 'isatty', lambda: True)
    stopped = asyncio.Event()

    async def reader():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(connector, 'connect', AsyncMock(side_effect=RuntimeError('synthetic')))
    monkeypatch.setattr(connector, 'wait_for_skip', reader)
    with pytest.raises(RuntimeError, match='synthetic'):
        await connector.run_connection()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_noninteractive_connector_does_not_read_stdin(monkeypatch):
    monkeypatch.setattr(connector.sys.stdin, 'isatty', lambda: False)
    connect, skip = AsyncMock(), AsyncMock()
    monkeypatch.setattr(connector, 'connect', connect)
    monkeypatch.setattr(connector, 'wait_for_skip', skip)
    assert await connector.run_connection() == 0
    connect.assert_awaited_once()
    skip.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["", "1", " SKIP "])
async def test_terminal_accepts_skip_and_ignores_other_input(monkeypatch, choice):
    monkeypatch.setattr(connector.os, 'name', 'posix')
    monkeypatch.setattr(connector.sys, 'stdin', io.StringIO('other\n' + choice + '\n'))
    monkeypatch.setattr(connector.select, 'select', lambda *a: ([connector.sys.stdin], [], []))
    await asyncio.wait_for(connector.wait_for_skip(), timeout=1)


def test_pending_menu_is_one_selected_action(capsys):
    connector.show_pending_menu()
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 2
    assert 'Skip for now' in output
    assert 'Enter: skip' not in output
    assert 'Login completes automatically' not in output
