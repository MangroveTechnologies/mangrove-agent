"""Contract tests for the Trade model — venue-agnostic shape.

Trade represents a DEX swap (input/output_token), a CEX fill
(base/quote/side/qty, e.g. Kraken), or a paper sim. The venue + identity
fields are optional so one record covers all three. These tests pin that
contract; DB persistence of the CEX fields is a separate migration step.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from src.models.domain import OrderIntent, Trade  # noqa: E402


def _now() -> datetime:
    return datetime(2026, 6, 27, tzinfo=timezone.utc)


def test_legacy_dex_trade_still_valid():
    """The pre-existing DEX strategy-trade shape must keep validating."""
    t = Trade(
        id="t1",
        strategy_id="s1",
        order_intent=OrderIntent(action="enter", side="buy", symbol="ETH", amount=100.0),
        mode="paper",
        input_token="USDC",
        input_amount=100.0,
        output_token="ETH",
        output_amount=0.04,
        fill_price=2500.0,
        status="simulated",
        executed_at=_now(),
    )
    assert t.venue is None and t.user_id is None
    assert t.base is None and t.qty is None


def test_cex_kraken_fill_shape():
    """A strategy-less CEX fill: base/quote/side/qty + venue refs, no tx_hash."""
    t = Trade(
        id="t2",
        mode="live",
        venue="kraken",
        user_id="u_123",            # server-stamped at ingestion
        venue_order_ref="OABC12-...",   # Kraken ordertxid
        venue_trade_ref="TQLM2-...",    # Kraken trade_id
        base="BTC",
        quote="USD",
        side="buy",
        qty=0.01,
        fill_price=64000.0,
        status="confirmed",
        executed_at=_now(),
    )
    assert t.strategy_id is None and t.order_intent is None
    assert t.tx_hash is None          # CEX spot has no chain hash
    assert t.venue_trade_ref == "TQLM2-..."
    assert t.input_token is None      # DEX aliases unused for CEX


def test_validate_mode_accepted():
    t = Trade(id="t3", mode="validate", venue="kraken", base="BTC", quote="USD",
              side="sell", qty=0.5, fill_price=0.0, status="pending", executed_at=_now())
    assert t.mode == "validate"


def test_bad_side_rejected():
    with pytest.raises(ValidationError):
        Trade(id="t4", mode="live", side="long", fill_price=1.0,  # type: ignore[arg-type]
              status="pending", executed_at=_now())
