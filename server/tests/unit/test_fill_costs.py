"""Paper fills use the backtester's fee and slippage.

The comparison is the published schedule, written out below, against the
fill the paper executor books. Agreement is relative 1e-9. A single
backtest slippage draw may sit anywhere in ``Uniform(0, slippage_pct)``;
its distance from the paper fill is at most half of that rate times the
mark notional, per leg.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.models.domain import OrderIntent
from src.services import fill_costs
from src.services.fill_costs import CostParams
from src.shared.errors import SdkError

FEE = Decimal("0.0085")
SLIP = Decimal("0.0025")
PARAMS = CostParams(FEE, SLIP)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    from src.config import app_config
    from src.shared.db import sqlite as db_mod
    from src.shared.db.sqlite import get_connection, init_db

    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "costs.db"))
    db_mod.reset_connection()
    init_db()
    get_connection().execute(
        """INSERT INTO strategies
           (id, mangrove_id, name, asset, timeframe, status,
            entry_json, exit_json, execution_config_json,
            generation_report_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("s1", "mg-s1", "t", "ETH", "1h", "paper", "[]", "[]", "{}", None,
         "2026-04-20T00:00:00+00:00", "2026-04-20T00:00:00+00:00"),
    )
    get_connection().commit()
    yield
    db_mod.reset_connection()


@pytest.fixture
def mock_mangroveai(monkeypatch):
    client = MagicMock()
    market = MagicMock()
    market.data = {"current_price": 2500.0}
    client.crypto_assets.get_market_data.return_value = market
    monkeypatch.setattr("src.services.order_executor.mangrove_ai_client", lambda: client)
    return client


def _independent_price(mark: Decimal, side: str, slippage_pct: Decimal) -> Decimal:
    """Mean of Uniform(0, slippage_pct), adverse to the side."""
    half = slippage_pct / 2
    return mark * (1 + half) if side == "buy" else mark * (1 - half)


def _independent_fee(notional: Decimal, fee_pct: Decimal) -> Decimal:
    small, large = Decimal("10000"), Decimal("1000000")
    floor = min(Decimal("0.000589"), fee_pct)
    if notional <= small:
        rate = fee_pct
    elif notional >= large:
        rate = floor
    else:
        weight = (notional - small) / (large - small)
        rate = fee_pct + weight * (floor - fee_pct)
    return rate * notional


def test_fee_schedule_matches_the_published_breakpoints():
    assert fill_costs.fee_rate(Decimal("10000"), FEE) == FEE
    assert fill_costs.fee_rate(Decimal("9999"), FEE) == FEE
    assert fill_costs.fee_rate(Decimal("1000000"), FEE) == Decimal("0.000589")
    assert fill_costs.fee_rate(Decimal("5000000"), FEE) == Decimal("0.000589")
    assert fill_costs.fee_rate(Decimal("505000"), FEE) == Decimal("0.0045445")
    # The configured rate is a maximum. A smaller fee_pct does not rise with size.
    assert fill_costs.fee_rate(Decimal("1000000"), Decimal("0.0001")) == Decimal("0.0001")


def test_quote_matches_the_independent_backtest_cost():
    mark, qty = Decimal("2500"), Decimal("0.1")
    quote = fill_costs.quote_leg(mark, "buy", qty, PARAMS)
    expected_price = _independent_price(mark, "buy", SLIP)
    assert fill_costs.within_tolerance(quote.fill_price, expected_price)
    assert fill_costs.within_tolerance(quote.notional, expected_price * qty)
    sell = fill_costs.quote_leg(mark, "sell", qty, PARAMS)
    assert fill_costs.within_tolerance(sell.fill_price, _independent_price(mark, "sell", SLIP))


def test_one_backtest_draw_stays_inside_the_stated_slippage_bound():
    mark, qty = Decimal("2500"), Decimal("4")
    paper = fill_costs.quote_leg(mark, "buy", qty, PARAMS)
    bound = fill_costs.slippage_realization_bound(mark * qty, PARAMS)
    assert bound == mark * qty * SLIP / 2
    for draw in (Decimal(0), SLIP):
        backtest_notional = mark * (1 + draw) * qty
        assert abs(backtest_notional - paper.notional) <= bound


def test_profitable_round_trip_charges_the_entry_notional_fee_and_a_loss_does_not():
    entry_price = _independent_price(Decimal("100"), "buy", SLIP)
    win_exit = _independent_price(Decimal("110"), "sell", SLIP)
    loss_exit = _independent_price(Decimal("90"), "sell", SLIP)
    qty = Decimal("2")
    notional = entry_price * qty
    fee = _independent_fee(notional, FEE)
    assert fill_costs.round_trip_fee(
        entry_notional=notional, entry_price=entry_price, exit_price=win_exit, quantity=qty, fee_pct=FEE,
    ) == fee
    assert fill_costs.round_trip_fee(
        entry_notional=notional, entry_price=entry_price, exit_price=loss_exit, quantity=qty, fee_pct=FEE,
    ) == 0
    net = fill_costs.round_trip_pnl(entry_price=entry_price, exit_price=win_exit, quantity=qty, fee=fee)
    assert net == (win_exit - entry_price) * qty - fee
    assert net < (win_exit - entry_price) * qty


def test_params_come_from_backtest_defaults(monkeypatch):
    monkeypatch.setattr(
        fill_costs,
        "_fetch_trading_defaults",
        lambda: {
            "net_execution_defaults": {"taker_fee_rate": "0.008"},
            "backtest_defaults": {"fee_pct": "0.01", "slippage_pct": "0.004"},
        },
    )
    params = fill_costs.load_cost_params()
    assert params.fee_pct == Decimal("0.01")
    assert params.slippage_pct == Decimal("0.004")
    # The sizing estimate is a different number and is not the fill cost.
    assert params.fee_pct != Decimal("0.008")


def test_missing_or_negative_rates_are_refused():
    with pytest.raises(SdkError, match="backtest_defaults"):
        fill_costs.params_from_payload({"risk_management": {}})
    with pytest.raises(SdkError, match="fee_pct"):
        fill_costs.params_from_payload({"backtest_defaults": {"fee_pct": "-0.01", "slippage_pct": "0.0025"}})


def test_paper_fill_matches_the_backtest_cost_for_the_same_order(temp_db, mock_mangroveai):
    from src.services.order_executor import _paper_fill

    intent = OrderIntent(action="enter", side="buy", symbol="ETH", amount=0.1, reason="cost")
    trade = _paper_fill(intent, "s1", None, record=False)
    quote = fill_costs.quote_leg(Decimal("2500"), "buy", Decimal("0.1"), PARAMS)
    assert fill_costs.within_tolerance(Decimal(str(trade.fill_price)), quote.fill_price)
    assert fill_costs.within_tolerance(Decimal(str(trade.input_amount)), quote.notional)
    assert trade.output_amount == pytest.approx(0.1)
    assert trade.fees["source"] == "trading_defaults.backtest_defaults"
    assert Decimal(trade.fees["slippage_applied"]) == SLIP / 2
    assert trade.fees["fee"] == "0"
    assert fill_costs.journal_complete(trade.fees)

    exit_intent = OrderIntent(
        action="exit", side="sell", symbol="ETH", amount=0.1,
        engine_position_id="pos-cost", reason="cost",
    )
    # Book the entry the way the executor does, then close it above the mark.
    from src.services.order_executor import execute_one
    from src.services.trade_log import get_position, list_trades

    opened = execute_one(
        OrderIntent(action="enter", side="buy", symbol="ETH", amount=0.1,
                    engine_position_id="pos-cost", reason="cost"),
        mode="paper", strategy_id="s1",
    )
    mock_mangroveai.crypto_assets.get_market_data.return_value.data = {"current_price": 2600.0}
    closed = execute_one(exit_intent, mode="paper", strategy_id="s1")
    entry_px = Decimal(str(opened.fill_price))
    exit_px = Decimal(str(closed.fill_price))
    expected_exit = _independent_price(Decimal("2600"), "sell", SLIP)
    assert fill_costs.within_tolerance(exit_px, expected_exit)
    notional = Decimal(opened.fees["notional"])
    fee = _independent_fee(notional, FEE)
    expected_pnl = (expected_exit - entry_px) * Decimal("0.1") - fee
    stored = [row for row in list_trades("s1") if row.id == closed.id][0]
    assert fill_costs.within_tolerance(Decimal(str(stored.p_and_l)), expected_pnl)
    assert fill_costs.within_tolerance(Decimal(stored.fees["fee"]), fee)
    assert stored.output_amount == pytest.approx(float(expected_exit * Decimal("0.1") - fee), rel=1e-9)
    assert get_position("pos-cost").status == "closed"
    # Same orders through the pure backtest cost and the paper book.
    assert fill_costs.within_tolerance(
        Decimal(str(stored.p_and_l)),
        fill_costs.round_trip_pnl(entry_price=entry_px, exit_price=exit_px, quantity=Decimal("0.1"), fee=fee),
    )


def test_a_losing_paper_close_keeps_slippage_and_drops_the_fee(temp_db, mock_mangroveai):
    from src.services.order_executor import execute_one
    from src.services.trade_log import list_trades

    execute_one(
        OrderIntent(action="enter", side="buy", symbol="ETH", amount=1,
                    engine_position_id="pos-loss", reason="cost"),
        mode="paper", strategy_id="s1",
    )
    mock_mangroveai.crypto_assets.get_market_data.return_value.data = {"current_price": 2400.0}
    closed = execute_one(
        OrderIntent(action="exit", side="sell", symbol="ETH", amount=1,
                    engine_position_id="pos-loss", reason="cost"),
        mode="paper", strategy_id="s1",
    )
    stored = [row for row in list_trades("s1") if row.id == closed.id][0]
    assert stored.fees["fee"] == "0"
    entry = [row for row in list_trades("s1") if row.order_intent.action == "enter"][0]
    expected = (Decimal(str(stored.fill_price)) - Decimal(str(entry.fill_price))) * 1
    assert stored.p_and_l == pytest.approx(float(expected))
    assert stored.p_and_l < 0


def test_paper_fill_uses_the_configured_rates_not_a_hardcoded_schedule(temp_db, mock_mangroveai, monkeypatch):
    from src.services.order_executor import _paper_fill

    monkeypatch.setattr(
        fill_costs,
        "_fetch_trading_defaults",
        lambda: {"backtest_defaults": {"fee_pct": "0.02", "slippage_pct": "0.01"}},
    )
    trade = _paper_fill(
        OrderIntent(action="enter", side="buy", symbol="ETH", amount=1, reason="cost"),
        "s1", None, record=False,
    )
    # Half of 1% on a 2500 mark, not the canon 0.125%.
    assert trade.fill_price == pytest.approx(2512.5)
    assert Decimal(trade.fees["fee_pct"]) == Decimal("0.02")
    assert Decimal(trade.fees["slippage_pct"]) == Decimal("0.01")


def test_legacy_empty_fees_stay_complete_and_other_payloads_do_not():
    assert fill_costs.journal_complete({})
    assert fill_costs.journal_complete(fill_costs.fee_record(PARAMS, Decimal("10"), Decimal("0")))
    assert not fill_costs.journal_complete({"gas_usd": 1})
    assert not fill_costs.journal_complete({"model": "backtest", "source": "trading_defaults.backtest_defaults"})
