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
from tests.dex_fixtures import USDC, WALLET, WETH, approval_payload, generic_swap_payload  # noqa: E402


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
        t.address = USDC if symbol == "USDC" else WETH
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
        "input_amount": 100_000_000,              # 100 USDC (6 decimals)
        "output_amount": 40_000_000_000_000_000,  # 0.04 ETH in wei
        "exchange_rate": 2500.0,
        "venue_fee": 1.0,
        "mangrove_fee": 0.5,
        "price_impact_percent": 0.05,
    }
    def get_quote(**kwargs):
        amount = kwargs["amount"]
        output = int(amount * 10**12 / 2500) if kwargs["input_token"].lower() == USDC else int(amount * 2500 / 10**12)
        quote.model_dump.return_value.update(input_amount=amount, output_amount=output)
        quote.raw_output = output
        return quote

    client.dex.get_quote.side_effect = get_quote
    client.dex.token_info.side_effect = lambda chain_id, address: _TOKENS["USDC" if address.lower() == USDC else "ETH"]

    # Default: approval already in place (None returned).
    client.dex.approve_token.return_value = None

    swap_tx = MagicMock()
    def prepare_swap(**kwargs):
        requested = client.dex.get_quote.call_args.kwargs
        swap_tx.payload = generic_swap_payload(
            requested["input_token"], requested["output_token"], requested["amount"],
            quote.raw_output, kwargs["wallet_address"],
        )
        return swap_tx

    client.dex.prepare_swap.side_effect = prepare_swap

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
        lambda payload, wallet_address, chain_id=None, *, intent=None: "0xSIGNED",
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


def test_position_lifecycle_paper_enter_exit(temp_db, mock_mangroveai, mock_markets):
    """#151: entry fills open a LOCAL position row keyed to the engine's
    position id; exit fills close it and stamp p_and_l on the exit trade."""
    from src.models.domain import OrderIntent
    from src.services.order_executor import execute_one
    from src.services.trade_log import get_position, list_trades

    enter = OrderIntent(action="enter", side="buy", symbol="ETH", amount=0.1,
                        ref_price=2500.0, engine_position_id="engine-pos-1",
                        stop_loss=2400.0, take_profit=2700.0, reason="t")
    execute_one(enter, mode="paper", strategy_id="s1")

    pos = get_position("engine-pos-1")
    assert pos is not None and pos.status == "open"
    assert pos.asset == "ETH"
    assert pos.entry_amount == pytest.approx(0.1)
    assert pos.entry_price == pytest.approx(2500.0)
    assert pos.stop_loss == pytest.approx(2400.0)

    # Exit at a higher mark: 0.1 ETH sold at 3000 vs entered at 2500 -> +50.
    mock_mangroveai.crypto_assets.get_market_data.return_value.data = {"current_price": 3000.0}
    exit_i = OrderIntent(action="exit", side="sell", symbol="ETH", amount=0.1,
                         engine_position_id="engine-pos-1", reason="tp")
    exit_trade = execute_one(exit_i, mode="paper", strategy_id="s1")

    pos = get_position("engine-pos-1")
    assert pos.status == "closed"
    assert pos.exit_trade_id == exit_trade.id
    assert pos.exit_price == pytest.approx(3000.0)
    stored = [t for t in list_trades("s1") if t.id == exit_trade.id][0]
    assert stored.p_and_l == pytest.approx(50.0)


def test_position_exit_fifo_fallback_without_engine_id(temp_db, mock_mangroveai, mock_markets):
    """Exits with no engine_position_id close the OLDEST open (strategy, asset) position."""
    from src.models.domain import OrderIntent
    from src.services.order_executor import execute_one
    from src.services.trade_log import list_positions

    for _ in range(2):
        execute_one(OrderIntent(action="enter", side="buy", symbol="ETH",
                                amount=0.1, reason="t"), mode="paper", strategy_id="s1")
    execute_one(OrderIntent(action="exit", side="sell", symbol="ETH",
                            amount=0.1, reason="t"), mode="paper", strategy_id="s1")

    open_rows = list_positions("s1", status="open")
    closed_rows = list_positions("s1", status="closed")
    assert len(open_rows) == 1 and len(closed_rows) == 1


def test_user_initiated_swaps_do_not_create_positions(temp_db, mock_mangroveai, mock_markets):
    from src.services.order_executor import execute_one
    from src.services.trade_log import list_positions

    execute_one(_intent("buy"), mode="paper", strategy_id="user-initiated")
    assert list_positions("user-initiated") == []


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
        wallet_address=WALLET,
        chain_id=8453,
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


def test_live_refuses_empty_input_balance_before_quote_or_approval(
    temp_db, mock_mangroveai, mock_markets, stub_sign,
):
    from src.services.order_executor import execute_one
    from src.services.wallet_manager import canonical_usdc_addresses
    from src.shared.errors import WalletInputBalanceEmpty

    usdc = canonical_usdc_addresses()[8453]
    mock_markets.dex.balances.return_value = MagicMock(balances={usdc: "0"})
    with pytest.raises(WalletInputBalanceEmpty):
        execute_one(_intent("buy"), mode="live", strategy_id="s1",
                    wallet_address="0xabc", chain_id=8453, slippage_pct=0.002)
    assert mock_markets.dex.get_quote.call_count == 0
    assert mock_markets.dex.approve_token.call_count == 0
    assert mock_markets.dex.broadcast.call_count == 0


def test_live_full_flow_with_approval(temp_db, mock_mangroveai, mock_markets, stub_sign):
    from src.services.order_executor import execute_one

    # First call: returns an approval; both broadcasts confirm.
    approval_tx = MagicMock()
    approval_tx.payload = approval_payload()
    mock_markets.dex.approve_token.return_value = approval_tx

    trade = execute_one(
        _intent("buy"),
        mode="live",
        strategy_id="s1",
        wallet_address=WALLET,
        chain_id=8453,
        slippage_pct=0.002,
    )
    assert trade.status == "confirmed"
    # broadcast called twice (approval + swap)
    assert mock_markets.dex.broadcast.call_count == 2
    assert trade.fees["approval_tx_hash"] == "0xdeadbeef"
    assert mock_markets.dex.approve_token.call_args.kwargs["amount"] == 250_000_000
    assert mock_markets.dex.prepare_swap.call_count == 1  # nonce fetched after approval
    assert trade.fees["routing_fee_cap_bps"] == 25
    assert trade.fees["gross_quote_output_base_units"] == "100000000000000000"
    assert trade.fees["minimum_return_base_units"] == "99550500000000000"


@pytest.mark.parametrize("mutation", ["chain", "selector", "amount", "recipient", "slippage", "value"])
def test_prepared_swap_cannot_redefine_original_order(
    temp_db, mock_mangroveai, mock_markets, stub_sign, monkeypatch, mutation,
):
    from types import SimpleNamespace

    from eth_abi import encode

    from src.services import order_executor
    from src.services.transaction_intent import _SWAP_SELECTORS, _SWAP_TYPES
    from src.shared.errors import SigningError
    from tests.dex_fixtures import EXECUTOR, ROUTER

    original = mock_markets.dex.prepare_swap.side_effect

    def malicious(**kwargs):
        tx = dict(original(**kwargs).payload)
        if mutation == "chain":
            tx["chainId"] = 1
        elif mutation == "selector":
            tx["data"] = "0xdeadbeef"
        elif mutation == "value":
            tx["value"] = 1000
        else:
            recipient = "0x" + "99" * 20 if mutation == "recipient" else WALLET
            amount = 250_000_001 if mutation == "amount" else 250_000_000
            minimum = 1 if mutation == "slippage" else 10**17
            args = (EXECUTOR, (USDC, WETH, EXECUTOR, recipient, amount, minimum, 0), b"route")
            tx["data"] = "0x" + (_SWAP_SELECTORS[ROUTER] + encode(_SWAP_TYPES[ROUTER], args)).hex()
        return SimpleNamespace(payload=tx)

    mock_markets.dex.prepare_swap.side_effect = malicious
    sign = MagicMock()
    monkeypatch.setattr(order_executor, "wallet_sign", sign)
    with pytest.raises(SigningError):
        order_executor.execute_one(_intent(), mode="live", strategy_id="s1", wallet_address=WALLET,
                                   chain_id=8453, slippage_pct=.002)
    sign.assert_not_called()
    mock_markets.dex.approve_token.assert_called_once()
    mock_markets.dex.broadcast.assert_not_called()


def test_unsupported_swap_after_finite_approval_reports_and_journals_partial_outcome(
    temp_db, mock_mangroveai, mock_markets, stub_sign, monkeypatch,
):
    from types import SimpleNamespace

    from src.services import order_executor
    from src.services.trade_log import list_trades
    from src.shared.errors import SigningError

    original = mock_markets.dex.prepare_swap.side_effect

    def refresh(**kwargs):
        tx = dict(original(**kwargs).payload)
        # Match the real provider: preparation is reached only after the
        # approval is confirmed and its nonce is consumed on chain.
        assert mock_markets.dex.broadcast.call_count == 1
        assert mock_markets.dex.tx_status.call_count == 1
        tx["data"] = "0xdeadbeef"
        return SimpleNamespace(payload=tx)

    mock_markets.dex.prepare_swap.side_effect = refresh
    mock_markets.dex.approve_token.return_value = SimpleNamespace(payload=approval_payload())
    sign = MagicMock(return_value="0xSIGNED")
    monkeypatch.setattr(order_executor, "wallet_sign", sign)
    with pytest.raises(SigningError, match="Approval 0xdeadbeef was confirmed.*UNSUPPORTED_SWAP_SELECTOR"):
        order_executor.execute_one(_intent(), mode="live", strategy_id="s1", wallet_address=WALLET,
                                   chain_id=8453, slippage_pct=.002)
    assert sign.call_count == 1
    assert sign.call_args.kwargs["intent"].kind == "approval"
    assert mock_markets.dex.broadcast.call_count == 1
    recorded = list_trades("s1")
    assert len(recorded) == 1
    assert recorded[0].status == "failed"
    assert recorded[0].tx_hash is None and recorded[0].output_amount == 0
    assert recorded[0].fees["approval_tx_hash"] == "0xdeadbeef"
    assert recorded[0].fees["swap_broadcast"] is False


def test_approval_cannot_escalate_allowance_to_unlimited(
    temp_db, mock_mangroveai, mock_markets, stub_sign, monkeypatch,
):
    from types import SimpleNamespace

    from src.services import order_executor
    from src.shared.errors import SigningError

    mock_markets.dex.approve_token.return_value = SimpleNamespace(payload=approval_payload(amount=2**256 - 1))
    sign = MagicMock()
    monkeypatch.setattr(order_executor, "wallet_sign", sign)
    with pytest.raises(SigningError, match="Approval spender or amount"):
        order_executor.execute_one(_intent(), mode="live", strategy_id="s1", wallet_address=WALLET,
                                   chain_id=8453, slippage_pct=.002)
    sign.assert_not_called()
    mock_markets.dex.broadcast.assert_not_called()


def test_live_requotes_after_approval_and_binds_minimum_to_that_quote(
    temp_db, mock_mangroveai, mock_markets, stub_sign, monkeypatch,
):
    """The swap floor comes from the quote the provider prepares after approval."""
    from types import SimpleNamespace

    from src.services import order_executor
    from src.services.transaction_intent import ONEINCH_ROUTING_FEE_BPS, minimum_return

    calls, signed = [], []
    original_quote = mock_markets.dex.get_quote.side_effect
    original_prepare = mock_markets.dex.prepare_swap.side_effect

    def get_quote(**kwargs):
        calls.append("quote")
        quote = original_quote(**kwargs)
        if calls.count("quote") > 1:  # the price moved while the approval confirmed
            quote.raw_output = quote.raw_output * 99 // 100
            quote.model_dump.return_value["output_amount"] = quote.raw_output
        return quote

    mock_markets.dex.get_quote.side_effect = get_quote
    mock_markets.dex.approve_token.side_effect = (
        lambda **kwargs: calls.append("approve") or SimpleNamespace(payload=approval_payload())
    )
    mock_markets.dex.prepare_swap.side_effect = lambda **kwargs: calls.append("prepare") or original_prepare(**kwargs)
    mock_markets.dex.broadcast.side_effect = (
        lambda **kwargs: calls.append("broadcast") or SimpleNamespace(tx_hash="0xdeadbeef")
    )
    monkeypatch.setattr(
        order_executor, "wallet_sign",
        lambda payload, wallet_address, chain_id=None, *, intent=None: signed.append(intent) or "0xSIGNED",
    )

    trade = order_executor.execute_one(_intent(), mode="live", strategy_id="s1", wallet_address=WALLET,
                                       chain_id=8453, slippage_pct=.002)

    assert calls == ["quote", "approve", "broadcast", "quote", "prepare", "broadcast"]
    refreshed = 99 * 10**17 // 100
    approval, swap = signed
    assert approval.amount == swap.amount == 250_000_000
    assert swap.min_return_amount == minimum_return(refreshed, .002, routing_fee_bps=ONEINCH_ROUTING_FEE_BPS)
    assert trade.fees["gross_quote_output_base_units"] == str(refreshed)
    assert trade.status == "confirmed"


def test_live_amount_is_float_exact_for_quote_approval_and_intent(
    temp_db, mock_mangroveai, mock_markets, stub_sign, monkeypatch,
):
    """Markets reads amounts as float and builds calldata from int(float(amount))."""
    from types import SimpleNamespace

    from src.services import order_executor

    original_quote = mock_markets.dex.get_quote.side_effect

    def float_quote(**kwargs):
        quote = original_quote(**kwargs)
        quote.model_dump.return_value["input_amount"] = float(kwargs["amount"])
        return quote

    def float_approval(**kwargs):
        return SimpleNamespace(payload=approval_payload(WETH, int(float(kwargs["amount"]))))

    def float_prepare(**kwargs):
        requested = mock_markets.dex.get_quote.call_args.kwargs
        payload = generic_swap_payload(WETH, USDC, int(float(requested["amount"])), 10**30, WALLET)
        return SimpleNamespace(payload=payload)

    signed = []
    mock_markets.dex.get_quote.side_effect = float_quote
    mock_markets.dex.approve_token.side_effect = float_approval
    mock_markets.dex.prepare_swap.side_effect = float_prepare
    monkeypatch.setattr(
        order_executor, "wallet_sign",
        lambda payload, wallet_address, chain_id=None, *, intent=None: signed.append(intent) or "0xSIGNED",
    )

    # 123.456789012345678901 ETH reaches the boundary as 123456789012345680000 wei,
    # which float transport would turn into 123456789012345683968 wei.
    order_executor.execute_one(_intent("sell", "ETH", 123.456789012345678901), mode="live", strategy_id="s1",
                               wallet_address=WALLET, chain_id=8453, slippage_pct=.002)

    quoted = mock_markets.dex.get_quote.call_args.kwargs["amount"]
    approval, swap = signed
    assert int(float(quoted)) == quoted <= 123_456_789_012_345_680_000
    assert mock_markets.dex.approve_token.call_args.kwargs["amount"] == quoted
    assert approval.amount == swap.amount == quoted


def test_live_quotes_in_base_units(temp_db, mock_mangroveai, mock_markets, stub_sign):
    """#122 + #139 regression: a cron buy intent's amount is ASSET units
    (engine position_size). Buying 0.04 ETH at the 2500 mark price spends
    100 USDC, which must reach dex.get_quote in BASE units (100_000_000
    @ 6 decimals), and the trade must record human amounts + a real
    fill price — not raw base-units values."""
    from src.services.order_executor import execute_one

    trade = execute_one(
        _intent("buy", "ETH", 0.04),
        mode="live",
        strategy_id="s1",
        wallet_address=WALLET,
        chain_id=8453,
        slippage_pct=0.002,
    )
    _, kwargs = mock_markets.dex.get_quote.call_args
    assert kwargs["amount"] == 100_000_000
    assert trade.input_amount == pytest.approx(100.0)
    assert trade.output_amount == pytest.approx(0.04)
    # fill_price derived from HUMAN amounts (100 USDC / 0.04 ETH = 2500),
    # NOT the quote's exchange_rate (a base-units ratio).
    assert trade.fill_price == pytest.approx(2500.0)
    # prepare_swap must still be driven by the returned quote_id.
    _, prep_kwargs = mock_markets.dex.prepare_swap.call_args
    assert prep_kwargs["quote_id"] == "q-123"


def test_live_buy_uses_ref_price_and_allocation_cap(temp_db, mock_mangroveai, mock_markets, stub_sign):
    """#139: the engine's evaluation-time ref_price converts asset units to
    USDC spend without a mark-price fetch, and the allocation amount caps
    the spend (the engine sizes off its own simulated account, not the
    user's allocation)."""
    from src.models.domain import OrderIntent
    from src.services.order_executor import execute_one

    intent = OrderIntent(action="enter", side="buy", symbol="ETH",
                         amount=0.1, ref_price=2000.0, reason="engine order")
    execute_one(
        intent, mode="live", strategy_id="s1", wallet_address=WALLET,
        chain_id=8453, slippage_pct=0.002,
        max_input_amount=50.0,  # 0.1 ETH * 2000 = 200 USDC, capped to 50
    )
    _, kwargs = mock_markets.dex.get_quote.call_args
    assert kwargs["amount"] == 50_000_000  # 50 USDC in base units
    # ref_price supplied -> no mark-price lookup on the live path
    assert not mock_mangroveai.crypto_assets.get_market_data.called


def test_live_sell_quotes_asset_side_in_base_units(temp_db, mock_mangroveai, mock_markets, stub_sign):
    """Sell: input is the asset (ETH, 18 decimals) — 0.1 ETH -> 1e17 wei."""
    from src.services.order_executor import execute_one

    execute_one(
        _intent("sell", "ETH", 0.1),
        mode="live",
        strategy_id="s1",
        wallet_address=WALLET,
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
            # sell of 1e-19 ETH -> less than 1 wei -> 0 base units
            _intent("sell", "ETH", 1e-19),
            mode="live",
            strategy_id="s1",
            wallet_address=WALLET,
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
                    wallet_address=WALLET, chain_id=8453)


def test_live_rejects_slippage_above_cap(temp_db, mock_mangroveai, mock_markets, stub_sign):
    """Defense-in-depth: even if somehow a slippage above 0.25% reaches
    _live_swap (shouldn't, Pydantic rejects first), refuse to execute."""
    from src.services.order_executor import execute_one
    from src.shared.errors import SigningError

    with pytest.raises(SigningError, match="outside allowed range"):
        execute_one(_intent("buy"), mode="live", strategy_id="s1",
                    wallet_address=WALLET, chain_id=8453,
                    slippage_pct=0.01)  # 1%, over the 0.25% cap


def test_live_requires_wallet_address(temp_db, mock_mangroveai, mock_markets, stub_sign):
    from src.services.order_executor import execute_one
    from src.shared.errors import SigningError

    with pytest.raises(SigningError):
        execute_one(_intent("buy"), mode="live", strategy_id="s1",
                    chain_id=8453, slippage_pct=0.002)


def test_live_requires_chain_id(temp_db, mock_mangroveai, mock_markets, stub_sign):
    from src.services.order_executor import execute_one
    from src.shared.errors import SigningError

    with pytest.raises(SigningError):
        execute_one(_intent("buy"), mode="live", strategy_id="s1",
                    wallet_address=WALLET, slippage_pct=0.002)


def test_live_wraps_sdk_failures(temp_db, mock_mangroveai, mock_markets, stub_sign):
    from src.services.order_executor import execute_one
    from src.shared.errors import SdkError

    mock_markets.dex.get_quote.side_effect = RuntimeError("upstream 503")
    with pytest.raises(SdkError):
        execute_one(_intent("buy"), mode="live", strategy_id="s1",
                    wallet_address=WALLET, chain_id=8453,
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


@pytest.mark.parametrize("failure", [RuntimeError("quote service unavailable"), None])
def test_failure_after_a_confirmed_approval_is_reported_and_recorded(
    temp_db, mock_mangroveai, mock_markets, stub_sign, monkeypatch, failure,
):
    """A re-quote or intent failure after the approval confirms names the approval and records a failed trade."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.services import order_executor
    from src.services.trade_log import list_trades
    from src.shared.errors import SdkError

    original_quote = mock_markets.dex.get_quote.side_effect
    calls = []

    def get_quote(**kwargs):
        calls.append(1)
        quote = original_quote(**kwargs)
        if len(calls) > 1:
            if failure is not None:
                raise failure
            quote.model_dump.return_value["input_amount"] = quote.model_dump.return_value["input_amount"] + 1
        return quote

    mock_markets.dex.get_quote.side_effect = get_quote
    mock_markets.dex.approve_token.return_value = SimpleNamespace(payload=approval_payload())
    monkeypatch.setattr(order_executor, "wallet_sign", MagicMock(return_value="0xSIGNED"))
    with pytest.raises(SdkError) as raised:
        order_executor.execute_one(_intent(), mode="live", strategy_id="s1", wallet_address=WALLET,
                                   chain_id=8453, slippage_pct=.002)
    assert "was confirmed" in raised.value.message
    assert mock_markets.dex.prepare_swap.call_count == 0
    (trade,) = list_trades("s1")
    assert trade.status == "failed" and trade.fees["swap_broadcast"] is False
