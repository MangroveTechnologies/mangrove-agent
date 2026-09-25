"""Workflow bounds complement, rather than replace, the durable payer ledger."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from src.services.payment_budget import current_budget, payment_budget
from src.shared.errors import X402SpendCapExceeded


def test_scope_preserves_unit_price_and_payment_bound():
    with payment_budget(3) as budget:
        budget.consume(1000)
        budget.consume(500)
        with pytest.raises(X402SpendCapExceeded, match="price increased"):
            budget.consume(1001)
        budget.max_payments = 2
        with pytest.raises(X402SpendCapExceeded, match="payment limit"):
            budget.consume(1)
    assert current_budget.get() is None


def test_scopes_reset_on_failure_without_releasing_ledger_reservations():
    with payment_budget(3) as outer:
        try:
            with payment_budget(1) as inner:
                inner.consume(1000)
                raise RuntimeError("stop")
        except RuntimeError as exc:
            assert str(exc) == "stop"
        else:
            pytest.fail("The payment budget scope suppressed the exception.")
        assert current_budget.get() is outer
        assert outer.payments == 0
    assert current_budget.get() is None


def test_concurrent_workflows_have_independent_price_bounds():
    barrier = Barrier(2)

    def run(amount):
        with payment_budget(2) as budget:
            budget.consume(amount)
            barrier.wait(timeout=5)
            current_budget.get().consume(amount)
            return budget.payments, budget.unit_micro_usd

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [1000, 2000]))
    assert results == [(2, 1000), (2, 2000)]
