"""Request-local payment bounds layered over the durable spend ledger.

The first authorization fixes the maximum unit price for this workflow. Later
pages cannot silently reprice it. Each actual payment is still reserved by the
existing payer before signing; this scope never releases durable reservations.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from src.shared.errors import X402SpendCapExceeded


@dataclass
class PaymentBudget:
    max_payments: int
    payments: int = 0
    unit_micro_usd: int | None = None

    def consume(self, amount: int) -> None:
        if self.payments >= self.max_payments:
            raise X402SpendCapExceeded("The signal request reached its payment limit.")
        if self.unit_micro_usd is not None and amount > self.unit_micro_usd:
            raise X402SpendCapExceeded("The signal page price increased during this request.")
        if self.unit_micro_usd is None:
            self.unit_micro_usd = amount
        self.payments += 1


current_budget: ContextVar[PaymentBudget | None] = ContextVar("payment_budget", default=None)


@contextmanager
def payment_budget(max_payments: int):
    budget = PaymentBudget(max_payments)
    token = current_budget.set(budget)
    try:
        yield budget
    finally:
        current_budget.reset(token)
