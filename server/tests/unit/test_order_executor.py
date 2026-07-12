"""Unit tests for order_executor with mocked SDKs.

Live-testnet integration (Task 5.3) lives in tests/e2e. Here we verify
the paper/live branching + SDK call sequence + trade_log wiring with
mocks so we can assert exactly which calls are made in which order.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

from src.models.domain import OrderIntent  # noqa: E402


def _intent(side: str = "buy", symbol: str = "ETH", amount: float = 0.1) -> OrderIntent:
    return OrderIntent(action="enter", side=side, symbol=symbol, amount=amount, reason="unit-test")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "oe.db"
    from src.config import app_config
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    db_mod.reset_connection()
    from src.shared.db.sqlite import get_connection, init_db
    init_db()

    # Seed a strategy row; FKs from trades/evaluations/positions.
    get_connection().execute(
        """INSERT INTO strategies
           (id, mangrove_id, name, asset, timeframe, status,
            entry_json, exit_json, execution_config_json,
            generation_report_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("s1", "mg-s1", "t", "ETH", "1h", "paper",
         "[]", "[]", "{}", None,
         "2026-04-20T00:00:00+00:00", "2026-04-20T00:00:00+00:00"),
    )
    # 'user-initiated' placeholder is seeded by migration 002; no need to
    # re-insert here.
    get_connection().commit()
    yield db_file
    db_mod.reset_connection()


@pytest.fixture
def mock_mangroveai(monkeypatch):
    """Stub mangrove_ai_client (used by _fetch_mark_price in paper mode)."""
    client = MagicMock()
    market_resp = MagicMock()
    market_resp.data = {"current_price": 2500.0}
    client.crypto_assets.get_market_data.return_value = market_resp
    monkeypatch.setattr("src.services.order_executor.mangrove_ai_client", lambda: client)
    return client


@pytest.fixture
def mock_markets(monkeypatch):
    """Stub mangrove_markets_client (used in live mode).

    Patched for BOTH order_executor and dex_service: _live_swap quotes
    through dex_service (the human<->base-units boundary, #109/#122),
    which builds its own client.
    """
    client = MagicMock()

    # dex_service.resolve_decimals resolves bare symbols (the cron-intent
    # convention) via token_search exact-symbol match.
    def _token(symbol: str, decimals: int) -> MagicMock:
        t = MagicMock()
        t.symbol = symbol
        t.decimals = decimals
        return t

    _TOKENS = {"USDC": _token("USDC", 6), "ETH": _token("ETH", 18)}
    client.dex.token_search.side_effect = (
        lambda chain_id, query: [_TOKENS[query.upper()]] if query.upper() in _TOKENS else []
    )

    # The backend quotes in BASE units; dex_service converts them back.
    quote = MagicMock()
    quote.quote_id = "q-123"
    quote.model_dump.return_value = {
        "quote_id": "q-123",
        "input_amount": 100_000,                  # 0.1 USDC (6 decimals)
        "output_amount": 40_000_000_000_000_000,  # 0.04 ETH in wei
        "exchange_rate": 2500.0,
        "venue_fee": 1.0,
        "mangrove_fee": 0.5,
        "price_impact_percent": 0.05,
    }
    client.dex.get_quote.return_value = quote

    # Default: approval already in place (None returned).
    client.dex.approve_token.return_value = None

    swap_tx = MagicMock()
    swap_tx.payload = {"chainId": 84532, "to": "0x" + "a" * 40, "nonce": 0, "data": "0x"}
    client.dex.prepare_swap.return_value = swap_tx

    broadcast = MagicMock()
    broadcast.tx_hash = "0xdeadbeef"
    client.dex.broadcast.return_value = broadcast

    status = MagicMock()
    status.status = "confirmed"
    status.block_number = 12345
    status.error_message = None
    client.dex.tx_status.return_value = status

    monkeypatch.setattr("src.services.order_executor.mangrove_markets_client", lambda: client)
    monkeypatch.setattr("src.services.dex_service.mangrove_markets_client", lambda: client)
    return client


@pytest.fixture
def stub_sign(monkeypatch):
    """Stub wallet_manager.sign + backup gate so live tests don't need a
    real wallet. The backup gate (require_backup_confirmed) runs on the
    live path before signing — tests that stub signing also stub the gate."""
    monkeypatch.setattr(
        "src.services.wallet_manager.require_backup_confirmed",
        lambda address: None,
    )
    monkeypatch.setattr(
        "src.services.order_executor.wallet_sign",
        lambda payload, wallet_address, chain_id=None: "0xSIGNED",
    )


# -- Paper -----------------------------------------------------------------


def test_paper_simulates_at_mark_price(temp_db, mock_mangroveai, mock_markets):
    from src.services.order_executor import execute_one
    from src.services.trade_log import list_trades

    trade = execute_one(_intent("buy"), mode="paper", strategy_id="s1")
    assert trade.mode == "paper"
    assert trade.status == "simulated"
    assert trade.tx_hash is None
    assert trade.fill_price == 2500.0
    # No DEX calls whatsoever.
    assert not mock_markets.dex.get_quote.called
    assert not mock_markets.dex.prepare_swap.called
    assert not mock_markets.dex.broadcast.called

    fetched = list_trades("s1")
    assert len(fetched) == 1
    assert fetched[0].mode == "paper"


def test_paper_sell_swaps_tokens(temp_db, mock_mangroveai, mock_markets):
    from src.services.order_executor import execute_one

    trade = execute_one(_intent("sell"), mode="paper", strategy_id="s1")
    # Sell: input is the asset, output is USDC
    assert trade.input_token == "ETH"
    assert trade.output_token == "USDC"


# -- Live ------------------------------------------------------------------


def test_live_skips_approval_when_none(temp_db, mock_mangroveai, mock_markets, stub_sign):
    from src.services.order_executor import execute_one

    # approve_token returns None → no approval sign/broadcast
    mock_markets.dex.approve_token.return_value = None

    trade = execute_one(
        _intent("buy"),
        mode="live",
        strategy_id="s1",
        wallet_address="0xabc",
        chain_id=84532,
        slippage_pct=0.002,
    )
    assert trade.mode == "live"
    assert trade.status == "confirmed"
    assert trade.tx_hash == "0xdeadbeef"
    # get_quote called once; prepare_swap called once; broadcast called ONCE (only swap, not approval)
    assert mock_markets.dex.get_quote.call_count == 1
    assert mock_markets.dex.prepare_swap.call_count == 1
    assert mock_markets.dex.broadcast.call_count == 1
    # slippage_pct must be converted to percentage (x100) at the prepare_swap boundary.
    _, kwargs = mock_markets.dex.prepare_swap.call_args
    assert kwargs["slippage"] == pytest.approx(0.2)


def test_live_full_flow_with_approval(temp_db, mock_mangroveai, mock_markets, stub_sign):
    from src.services.order_executor import execute_one

    # First call: returns an approval; both broadcasts confirm.
    approval_tx = MagicMock()
    approval_tx.payload = {"chainId": 84532, "to": "0x" + "b" * 40, "data": "0x"}
    mock_markets.dex.approve_token.return_value = approval_tx

    trade = execute_one(
        _intent("buy"),
        mode="live",
        strategy_id="s1",
        wallet_address="0xabc",
        chain_id=84532,
        slippage_pct=0.002,
    )
    assert trade.status == "confirmed"
    # broadcast called twice (approval + swap)
    assert mock_markets.dex.broadcast.call_count == 2
    assert trade.fees["approval_tx_hash"] == "0xdeadbeef"


def test_live_quotes_in_base_units(temp_db, mock_mangroveai, mock_markets, stub_sign):
    """#122 regression: the live path must convert the HUMAN intent amount
    to base units before dex.get_quote (0.1 USDC @ 6 decimals -> 100_000),
    and record the quote's output_amount converted back to human units
    (4e16 wei -> 0.04 ETH) — not the raw base-units value."""
    from src.services.order_executor import execute_one

    trade = execute_one(
        _intent("buy", "ETH", 0.1),
        mode="live",
        strategy_id="s1",
        wallet_address="0xabc",
        chain_id=8453,
        slippage_pct=0.002,
    )
    _, kwargs = mock_markets.dex.get_quote.call_args
    assert kwargs["amount"] == 100_000
    assert trade.input_amount == pytest.approx(0.1)
    assert trade.output_amount == pytest.approx(0.04)
    # fill_price is derived from the HUMAN amounts (0.1 USDC / 0.04 ETH),
    # NOT the quote's exchange_rate (a base-units ratio, 2500.0 here).
    assert trade.fill_price == pytest.approx(2.5)
    # prepare_swap must still be driven by the returned quote_id.
    _, prep_kwargs = mock_markets.dex.prepare_swap.call_args
    assert prep_kwargs["quote_id"] == "q-123"


def test_live_sell_quotes_asset_side_in_base_units(temp_db, mock_mangroveai, mock_markets, stub_sign):
    """Sell: input is the asset (ETH, 18 decimals) — 0.1 ETH -> 1e17 wei."""
    from src.services.order_executor import execute_one

    execute_one(
        _intent("sell", "ETH", 0.1),
        mode="live",
        strategy_id="s1",
        wallet_address="0xabc",
        chain_id=8453,
        slippage_pct=0.002,
    )
    _, kwargs = mock_markets.dex.get_quote.call_args
    assert kwargs["amount"] == 100_000_000_000_000_000


def test_live_rejects_dust_amount(temp_db, mock_mangroveai, mock_markets, stub_sign):
    """#122: an amount that rounds to 0 base units must be refused with a
    clear error BEFORE any quote/approval/signing happens — not sent
    upstream to come back as a misleading INSUFFICIENT_LIQUIDITY."""
    from src.services.order_executor import execute_one
    from src.shared.errors import ValidationError

    with pytest.raises(ValidationError, match="too small to quote"):
        execute_one(
            _intent("buy", "ETH", 0.0000001),  # < 1e-6 USDC -> 0 base units
            mode="live",
            strategy_id="s1",
            wallet_address="0xabc",
            chain_id=8453,
            slippage_pct=0.002,
        )
    assert not mock_markets.dex.get_quote.called
    assert not mock_markets.dex.approve_token.called
    assert not mock_markets.dex.broadcast.called


def test_live_requires_slippage_pct(temp_db, mock_mangroveai, mock_markets, stub_sign):
    """slippage_pct has no default in live mode — direct callers must pass
    it (SwapRequest enforces), cron callers pull it from the active
    allocation (migration 004)."""
    from src.services.order_executor import execute_one
    from src.shared.errors import SigningError

    with pytest.raises(SigningError, match="slippage_pct is required"):
        execute_one(_intent("buy"), mode="live", strategy_id="s1",
                    wallet_address="0xabc", chain_id=84532)


def test_live_rejects_slippage_above_cap(temp_db, mock_mangroveai, mock_markets, stub_sign):
    """Defense-in-depth: even if somehow a slippage above 0.25% reaches
    _live_swap (shouldn't, Pydantic rejects first), refuse to execute."""
    from src.services.order_executor import execute_one
    from src.shared.errors import SigningError

    with pytest.raises(SigningError, match="outside allowed range"):
        execute_one(_intent("buy"), mode="live", strategy_id="s1",
                    wallet_address="0xabc", chain_id=84532,
                    slippage_pct=0.01)  # 1%, over the 0.25% cap


def test_live_requires_wallet_address(temp_db, mock_mangroveai, mock_markets, stub_sign):
    from src.services.order_executor import execute_one
    from src.shared.errors import SigningError

    with pytest.raises(SigningError):
        execute_one(_intent("buy"), mode="live", strategy_id="s1",
                    chain_id=84532, slippage_pct=0.002)


def test_live_requires_chain_id(temp_db, mock_mangroveai, mock_markets, stub_sign):
    from src.services.order_executor import execute_one
    from src.shared.errors import SigningError

    with pytest.raises(SigningError):
        execute_one(_intent("buy"), mode="live", strategy_id="s1",
                    wallet_address="0xabc", slippage_pct=0.002)


def test_live_wraps_sdk_failures(temp_db, mock_mangroveai, mock_markets, stub_sign):
    from src.services.order_executor import execute_one
    from src.shared.errors import SdkError

    mock_markets.dex.get_quote.side_effect = RuntimeError("upstream 503")
    with pytest.raises(SdkError):
        execute_one(_intent("buy"), mode="live", strategy_id="s1",
                    wallet_address="0xabc", chain_id=84532,
                    slippage_pct=0.002)


# -- Batching --------------------------------------------------------------


def test_execute_many_failure_does_not_block_others(temp_db, mock_mangroveai, mock_markets):
    from src.services.order_executor import execute_many

    # Second call to get_market_data raises; first + third succeed.
    bad = MagicMock()
    bad.data = {}  # triggers SdkError in _fetch_mark_price
    good = MagicMock()
    good.data = {"current_price": 3000.0}
    mock_mangroveai.crypto_assets.get_market_data.side_effect = [good, bad, good]

    intents = [_intent("buy", "ETH", 0.1), _intent("buy", "BTC", 0.01), _intent("sell", "ETH", 0.05)]
    trades = execute_many(intents, mode="paper", strategy_id="s1")
    # Two succeeded (first + third); middle failed.
    assert len(trades) == 2
    assert trades[0].order_intent.symbol == "ETH"
    assert trades[1].order_intent.symbol == "ETH"


def test_execute_many_empty_list(temp_db, mock_mangroveai, mock_markets):
    from src.services.order_executor import execute_many

    assert execute_many([], mode="paper", strategy_id="s1") == []


# -- Unknown mode ----------------------------------------------------------


def test_unknown_mode_raises(temp_db, mock_mangroveai, mock_markets):
    from src.services.order_executor import execute_one
    from src.shared.errors import SigningError

    with pytest.raises(SigningError):
        execute_one(_intent(), mode="backtest", strategy_id="s1")  # type: ignore[arg-type]
