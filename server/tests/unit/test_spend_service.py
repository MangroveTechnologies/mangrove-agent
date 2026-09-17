"""Unit tests for spend_service — the outbound x402 spend budget.

Verification case 6: a breach latches, further paid calls are refused, and a
human reset works. Everything here is offline — no facilitator, no chain, no
network. The budget is exercised directly and then through the real payer, so
both the accounting and the gate that enforces it are covered.
"""
from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

import pytest  # noqa: E402

_CENT = 10_000        # micro-USD in $0.01
_MILLI = 1_000        # micro-USD in $0.001


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_spend.db"
    from src.config import app_config
    from src.shared.db import sqlite as db_mod

    monkeypatch.setattr(app_config, "DB_PATH", str(db_file))
    # Pin the cap so a local config edit cannot silently change what these
    # tests assert. $5 is the shipped default.
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 5, raising=False)
    db_mod.reset_connection()
    from src.shared.db.sqlite import init_db

    init_db()
    yield db_file
    db_mod.reset_connection()


def _reserve(amount_micro_usd, **kw):
    from src.services import spend_service

    return spend_service.reserve(
        value=amount_micro_usd,
        wallet_address=kw.pop("wallet_address", "0x" + "11" * 20),
        **kw,
    )


# ---------------------------------------------------------------------------
# the cap itself
# ---------------------------------------------------------------------------


def test_default_cap_when_key_is_absent(temp_db, monkeypatch):
    """A config file written before this key existed must still boot and pay."""
    from src.config import app_config
    from src.services import spend_service

    monkeypatch.delattr(app_config, "X402_SPEND_CAP_USD", raising=False)
    status = spend_service.get_status()
    assert status["cap_usd"] == 25.0
    assert status["cap_source"] == "default"


def test_configured_cap_is_honoured(temp_db, monkeypatch):
    from src.config import app_config
    from src.services import spend_service

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 0.25, raising=False)
    assert spend_service.get_status()["cap_usd"] == 0.25


def test_zero_cap_means_zero_not_the_default(temp_db, monkeypatch):
    """The falsy-zero trap.

    `portfolio_risk_service._limit()` reads `config or DEFAULT`, which would
    turn a deliberate "never spend anything" into the $5 default. A cap must
    not do that, so it is read explicitly.
    """
    from src.config import app_config
    from src.services import spend_service
    from src.shared.errors import X402SpendCapExceeded

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 0, raising=False)
    assert spend_service.get_status()["cap_usd"] == 0.0
    assert spend_service.check_before_payment()["allowed"] is False
    with pytest.raises(X402SpendCapExceeded):
        _reserve(1)


def test_malformed_cap_fails_loudly(temp_db, monkeypatch):
    from src.config import app_config
    from src.services import spend_service
    from src.shared.errors import ValidationError

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", "five dollars", raising=False)
    with pytest.raises(ValidationError, match="not a number"):
        spend_service.get_status()


def test_negative_cap_refused(temp_db, monkeypatch):
    from src.config import app_config
    from src.services import spend_service
    from src.shared.errors import ValidationError

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", -1, raising=False)
    with pytest.raises(ValidationError, match="negative"):
        spend_service.get_status()


# ---------------------------------------------------------------------------
# reserve: budget is charged at authorization, not at settlement
# ---------------------------------------------------------------------------


def test_reservation_counts_immediately(temp_db):
    """An authorization that has not settled still consumes budget.

    The agent controls what it signs, not what a receiver settles, so the
    signature is the honest moment to charge.
    """
    from src.services import spend_service

    _reserve(5 * _CENT)
    status = spend_service.get_status()
    assert status["spent_usd"] == 0.05
    assert status["remaining_usd"] == 4.95
    assert status["payment_count"] == 1
    assert spend_service.list_payments()[0]["state"] == "authorized"


def test_sub_cent_amounts_do_not_drift(temp_db):
    """Micro-USD integers, not floats: 1000 x $0.001 is exactly $1."""
    from src.services import spend_service

    for _ in range(1000):
        _reserve(_MILLI)
    assert spend_service.get_status()["spent_usd"] == 1.0


def test_payment_that_does_not_fit_is_refused_without_latching(temp_db):
    """A single oversized quote must not be able to halt the agent.

    The amount comes out of a REMOTE server's 402 envelope. Latching on it
    would hand any resource server a one-request denial of service over the
    whole payment path, so an over-budget payment is refused and nothing
    else changes.
    """
    from src.services import spend_service
    from src.shared.errors import X402SpendCapExceeded

    with pytest.raises(X402SpendCapExceeded, match="past the"):
        _reserve(6_000_000)          # $6 against a $5 cap

    status = spend_service.get_status()
    assert status["exhausted"] is False
    assert status["spent_usd"] == 0.0
    assert spend_service.list_payments() == []
    # And the budget still works for something that fits.
    _reserve(5 * _CENT)
    assert spend_service.get_status()["spent_usd"] == 0.05


def test_spending_the_budget_stops_further_payments(temp_db):
    """Verification case 6: the budget runs out and payment stops."""
    from src.services import spend_service
    from src.shared.errors import X402SpendCapExceeded

    _reserve(5_000_000)              # exactly the $5 budget -- allowed, then spent
    status = spend_service.get_status()
    assert status["exhausted"] is True
    assert status["remaining_usd"] == 0.0
    # Balance language, not breach language: a spent budget is normal use.
    assert "$5.0 of the $5.0 budget used across 1 payments" == status["exhausted_reason"]

    with pytest.raises(X402SpendCapExceeded, match="budget is spent"):
        _reserve(1)
    assert spend_service.check_before_payment()["allowed"] is False


def test_raising_the_config_cap_does_not_refill_the_budget(temp_db, monkeypatch):
    """Consent is an act, not a file. Only a top-up resumes payment."""
    from src.config import app_config
    from src.services import spend_service
    from src.shared.errors import X402SpendCapExceeded

    _reserve(5_000_000)
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 50, raising=False)
    assert spend_service.get_status()["exhausted"] is True
    with pytest.raises(X402SpendCapExceeded, match="budget is spent"):
        _reserve(_CENT)


def test_ledger_records_the_wallet_but_the_budget_is_agent_wide(temp_db):
    """An explicit payer argument must not sidestep the budget.

    `resolve_payer_wallet` lets a caller name any wallet, so a budget keyed
    to the CONFIGURED wallet would be bypassed by passing a different one.
    The cap is therefore agent-wide; the wallet is audit data.
    """
    from src.services import spend_service

    _reserve(2_000_000, wallet_address="0xAlice")
    _reserve(2_000_000, wallet_address="0xBob")
    assert spend_service.get_status()["spent_usd"] == 4.0
    assert {p["wallet_address"] for p in spend_service.list_payments()} == {"0xAlice", "0xBob"}


# ---------------------------------------------------------------------------
# amount coercion -- unbudgetable means unpaid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "abc", 1.5, b"1000", {"amount": 1}, True])
def test_unreadable_amounts_are_refused(temp_db, bad):
    """`True` is in here on purpose: bool subclasses int in Python, so a
    naive isinstance check would price it at one micro-dollar."""
    from src.shared.errors import ValidationError

    with pytest.raises(ValidationError):
        _reserve(bad)


def test_negative_amount_refused(temp_db):
    from src.shared.errors import ValidationError

    with pytest.raises(ValidationError, match="negative"):
        _reserve(-1000)


def test_integer_strings_are_accepted(temp_db):
    """The SDK types `value` as an int, but the wire format is a string."""
    from src.services import spend_service

    _reserve("50000")
    assert spend_service.get_status()["spent_usd"] == 0.05


# ---------------------------------------------------------------------------
# settle / release
# ---------------------------------------------------------------------------


def test_settle_records_evidence_without_changing_the_total(temp_db):
    from src.services import spend_service

    rid = _reserve(5 * _CENT)
    spend_service.settle(rid, transaction="0x" + "de" * 32, resource="https://x.test/a?key=secret")

    assert spend_service.get_status()["spent_usd"] == 0.05
    row = spend_service.list_payments()[0]
    assert row["state"] == "settled"
    assert row["transaction"] == "0x" + "de" * 32
    # Query strings are stripped: a durable ledger is no place for a
    # credential that leaked into a URL.
    assert row["resource"] == "https://x.test/a"


def test_release_gives_budget_back(temp_db):
    from src.services import spend_service

    rid = _reserve(5 * _CENT)
    spend_service.release(rid, reason="rejected_by_receiver")

    status = spend_service.get_status()
    assert status["spent_usd"] == 0.0
    assert status["payment_count"] == 0
    row = spend_service.list_payments()[0]
    assert row["state"] == "released" and row["release_reason"] == "rejected_by_receiver"


def test_release_cannot_resurrect_budget_from_a_settled_payment(temp_db):
    """Money that moved stays spent, however many times release is called."""
    from src.services import spend_service

    rid = _reserve(5 * _CENT)
    spend_service.settle(rid, transaction="0x" + "ab" * 32)
    spend_service.release(rid, reason="oops")

    assert spend_service.get_status()["spent_usd"] == 0.05
    assert spend_service.list_payments()[0]["state"] == "settled"


def test_settling_a_retry_retains_the_earlier_authorization(temp_db):
    """A later settlement does not cancel an earlier signature."""
    from src.services import spend_service

    first = _reserve(5 * _CENT)
    second = _reserve(5 * _CENT)
    assert spend_service.get_status()["spent_usd"] == 0.10

    spend_service.settle([first, second], transaction="0x" + "fe" * 32)

    assert spend_service.get_status()["spent_usd"] == 0.10
    by_id = {p["id"]: p for p in spend_service.list_payments()}
    assert by_id[first]["state"] == "authorized"
    assert by_id[second]["state"] == "settled"


def test_settle_and_release_tolerate_no_reservations(temp_db):
    """A free resource never signs, so there is nothing to reconcile."""
    from src.services import spend_service

    spend_service.settle([], transaction="0x1")
    spend_service.release(None, reason="nothing")
    assert spend_service.list_payments() == []


# ---------------------------------------------------------------------------
# human reset
# ---------------------------------------------------------------------------


def test_reset_clears_the_latch_keeps_history_and_resumes(temp_db):
    from src.services import spend_service

    rid = _reserve(5_000_000)
    spend_service.settle(rid, transaction="0x1")
    assert spend_service.get_status()["exhausted"] is True

    out = spend_service.reset()
    assert out["exhausted"] is False
    assert out["spent_usd"] == 0.0
    assert out["remaining_usd"] == 5.0
    assert out["period_id"] == 2

    # History survives: the old payment is still on the ledger, stamped
    # with the period it belonged to.
    ledger = spend_service.list_payments()
    assert len(ledger) == 1 and ledger[0]["period_id"] == 1
    assert spend_service.list_payments(period_id=2) == []

    # And payments work again.
    _reserve(5 * _CENT)
    assert spend_service.get_status()["spent_usd"] == 0.05


def test_check_before_payment_reports_the_budget(temp_db):
    from src.services import spend_service

    ok = spend_service.check_before_payment()
    assert ok == {"allowed": True, "exhausted": False, "reason": None,
                  "spent_usd": 0.0, "cap_usd": 5.0, "remaining_usd": 5.0}

    _reserve(5_000_000)
    blocked = spend_service.check_before_payment()
    assert blocked["allowed"] is False and blocked["exhausted"] is True
    assert "needs authorizing again" in blocked["reason"]


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def test_concurrent_reservations_never_exceed_the_cap(temp_db, monkeypatch):
    """The HTTP server and the APScheduler tick run on different threads
    against one shared SQLite connection. Read-total-then-insert is not
    atomic on its own, so two payments could each see a total that excludes
    the other and both be allowed through."""
    import threading

    from src.config import app_config
    from src.services import spend_service
    from src.shared.errors import X402SpendCapExceeded

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 1, raising=False)

    start = threading.Barrier(8)
    accepted: list[str] = []
    lock = threading.Lock()

    def attempt():
        start.wait()
        try:
            rid = _reserve(250_000)          # $0.25 -- exactly 4 fit in $1
        except X402SpendCapExceeded:
            return
        with lock:
            accepted.append(rid)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(accepted) == 4
    assert spend_service.get_status()["spent_usd"] == 1.0
    assert spend_service.get_status()["exhausted"] is True


def test_zero_cap_does_not_latch_on_a_zero_value_authorization(temp_db, monkeypatch):
    """A latch means "a human should look at this". Nothing was spent."""
    from src.config import app_config
    from src.services import spend_service

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 0, raising=False)
    _reserve(0)
    assert spend_service.get_status()["exhausted"] is False


def test_release_does_not_clear_a_latch(temp_db):
    """Reaching the cap is worth a human's attention even if the last
    authorization turned out to be dead -- everything before it was real."""
    from src.services import spend_service

    rid = _reserve(5_000_000)
    assert spend_service.get_status()["exhausted"] is True

    spend_service.release(rid, reason="rejected_by_receiver")
    status = spend_service.get_status()
    assert status["spent_usd"] == 0.0
    assert status["exhausted"] is True          # only reset() clears it


# ---------------------------------------------------------------------------
# the top-up: a human authorizing a budget
# ---------------------------------------------------------------------------


def test_top_up_records_the_budget_the_human_chose(temp_db):
    """Config is the default for a fresh install; this is what someone agreed
    to, recorded at the moment they agreed to it."""
    from src.services import spend_service

    _reserve(5_000_000)                       # spends the $5 config budget
    assert spend_service.get_status()["exhausted"] is True

    after = spend_service.reset(cap_usd=50)
    assert after["cap_usd"] == 50.0
    assert after["cap_source"] == "authorized"
    assert after["exhausted"] is False
    assert after["remaining_usd"] == 50.0

    _reserve(30_000_000)                      # $30 -- would never have fit in $5
    assert spend_service.get_status()["spent_usd"] == 30.0


def test_authorized_budget_outranks_config(temp_db, monkeypatch):
    """Once a human has set a budget, editing the config file does not
    silently move it -- in either direction."""
    from src.config import app_config
    from src.services import spend_service

    spend_service.reset(cap_usd=2)
    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 999, raising=False)
    assert spend_service.get_status()["cap_usd"] == 2.0


def test_top_up_without_an_amount_keeps_the_current_budget(temp_db):
    from src.services import spend_service

    _reserve(5_000_000)
    after = spend_service.reset()
    assert after["cap_usd"] == 5.0
    assert after["cap_source"] == "config"


def test_a_human_can_authorize_zero(temp_db):
    """"Stop paying for things" has to be expressible."""
    from src.services import spend_service
    from src.shared.errors import X402SpendCapExceeded

    spend_service.reset(cap_usd=0)
    assert spend_service.get_status()["cap_usd"] == 0.0
    with pytest.raises(X402SpendCapExceeded):
        _reserve(1)


# `None` is absent from this list on purpose: it is the documented way to say
# "keep the current budget", covered above.
@pytest.mark.parametrize("bad", ["fifty", -1, float("inf"), float("nan")])
def test_a_mistyped_budget_is_refused_not_rounded(temp_db, bad):
    """A typo in the one number bounding outbound spend must stop the top-up."""
    from src.services import spend_service
    from src.shared.errors import ValidationError

    with pytest.raises(ValidationError):
        spend_service.reset(cap_usd=bad)


# ---------------------------------------------------------------------------
# audit regressions -- defects found reviewing the finished branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_config_and_top_up_reject_the_same_values(temp_db, monkeypatch, bad):
    """Two validators for one concept WILL drift, and these did.

    The top-up path refused infinity and NaN; the config path let both reach
    round(), which raises OverflowError / ValueError instead of a refusal.
    JSON admits `Infinity` and `NaN` as literals, so a config file could get
    there. Both paths now share one validator.
    """
    from src.config import app_config
    from src.services import spend_service
    from src.shared.errors import ValidationError

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", bad, raising=False)
    with pytest.raises(ValidationError):
        spend_service.get_status()

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 5, raising=False)
    with pytest.raises(ValidationError):
        spend_service.reset(cap_usd=bad)


def test_cap_source_does_not_credit_a_blank_config_key(temp_db, monkeypatch):
    """`cap_source` answers "who decided this number". A blank config key
    falls through to the default, so claiming "config" would misattribute it."""
    from src.config import app_config
    from src.services import spend_service

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", "", raising=False)
    status = spend_service.get_status()
    assert status["cap_usd"] == 25.0
    assert status["cap_source"] == "default"


def test_query_strings_never_reach_the_ledger_or_an_error_message(temp_db, monkeypatch):
    """A token in a URL must not be persisted OR echoed into the transcript.

    Stripping it from the ledger while an error message carries it intact
    would make the stripping decorative -- error text travels further than
    the database does.
    """
    from src.config import app_config
    from src.services import spend_service, x402_payer
    from src.shared.errors import X402SpendCapExceeded

    leaky = "https://api.test/v1/signals?api_key=SUPERSECRET&x=1"

    spend_service.reserve(value=10, wallet_address="0x" + "11" * 20, resource=leaky)
    assert spend_service.list_payments()[0]["resource"] == "https://api.test/v1/signals"

    monkeypatch.setattr(app_config, "X402_SPEND_CAP_USD", 0, raising=False)
    with pytest.raises(X402SpendCapExceeded) as excinfo:
        x402_payer.check_payment_budget(leaky)
    assert "SUPERSECRET" not in excinfo.value.message
    assert "https://api.test/v1/signals" in excinfo.value.message


def test_the_stop_message_does_not_argue_with_itself(temp_db):
    """After a release, the recorded reason is quoted -- not a recomputed one.

    Recomputing "$X of $Y used" at read time reads correctly until a payment
    is released AFTER the budget ran out: the total falls back below the
    limit while the stop stays in force, and the message becomes "the budget
    is spent -- $0.95 of $1.00 used", which invites the reader to conclude
    the accounting is broken.
    """
    from src.services import spend_service

    rid = _reserve(5 * _CENT)
    _reserve(4_950_000)                       # fills the $5 budget
    assert spend_service.get_status()["exhausted"] is True

    spend_service.release(rid, reason="rejected_by_receiver")

    status = spend_service.get_status()
    assert status["exhausted"] is True
    assert status["spent_usd"] == 4.95        # below the $5 limit again
    reason = spend_service.check_before_payment()["reason"]
    assert "needs authorizing again" in reason
    # The live total must NOT be presented as the reason payment is stopped.
    assert "$4.95 of $5.0 used" not in reason


def test_the_authorization_expiry_is_recorded(temp_db):
    """`valid_before` is the only sound basis for ever reclaiming a dead
    authorization's budget, and it cannot be backfilled -- adding the column
    later means migrating live ledgers."""
    from src.services import spend_service

    _reserve(_CENT, valid_before=1_800_000_000)
    assert spend_service.list_payments()[0]["valid_before"] == 1_800_000_000


@pytest.mark.parametrize("junk", [None, "soon", -1, True, 1.5, {"t": 1}])
def test_an_unreadable_expiry_does_not_block_the_payment(temp_db, junk):
    """Audit metadata must never be able to veto a payment the budget allows.
    Refusing here would trade a real capability for a cosmetic one."""
    from src.services import spend_service

    _reserve(_CENT, valid_before=junk)
    row = spend_service.list_payments()[0]
    assert row["valid_before"] is None
    assert row["state"] == "authorized"
    assert spend_service.get_status()["spent_usd"] == 0.01


def test_expired_authorizations_are_not_reclaimed_yet(temp_db):
    """Deliberate, and the ordering matters.

    Reconciliation is currently only done by `x402_payer.pay`; a caller that
    drives the signer directly leaves every row `authorized` whatever the
    outcome. An expiry sweep under that regime would release SUCCESSFUL
    payments too and the total would count nothing at all. Over-counting is
    survivable; a budget that silently stops counting is not.
    """
    from src.services import spend_service

    _reserve(_CENT, valid_before=1)           # expired in 1970
    assert spend_service.get_status()["spent_usd"] == 0.01
    assert spend_service.list_payments()[0]["state"] == "authorized"


# ---------------------------------------------------------------------------
# reconcile(): the one entry point every payment driver must call
# ---------------------------------------------------------------------------


def _reconcile(rid, **kw):
    from src.services import spend_service

    kw.setdefault("status_code", 200)
    return spend_service.reconcile(rid, **kw)


def test_reconcile_settles_when_a_receipt_came_back(temp_db):
    from src.services import spend_service

    rid = _reserve(5 * _CENT)
    _reconcile(rid, settlement={"success": True, "transaction": "0x" + "ab" * 32, "network": "eip155:84532", "payer": "0x" + "11" * 20},
               resource="https://api.test/v1/x?token=LEAK")

    row = spend_service.list_payments()[0]
    assert row["state"] == "settled"
    assert row["transaction"] == "0x" + "ab" * 32
    assert row["resource"] == "https://api.test/v1/x"      # query still stripped
    assert spend_service.get_status()["spent_usd"] == 0.05  # settling is not a charge


def test_reconcile_retains_a_rejected_payment(temp_db):
    from src.services import spend_service

    rid = _reserve(5 * _CENT)
    _reconcile(rid, status_code=402)

    assert spend_service.get_status()["spent_usd"] == 0.05
    assert spend_service.list_payments()[0]["state"] == "authorized"


def test_reconcile_retains_when_the_resource_errored(temp_db):
    from src.services import spend_service

    rid = _reserve(5 * _CENT)
    _reconcile(rid, status_code=500)

    assert spend_service.get_status()["spent_usd"] == 0.05
    assert spend_service.list_payments()[0]["state"] == "authorized"


def test_reconcile_keeps_an_unconfirmed_success_counted(temp_db):
    """Releasing on a missing optional header would let the counterparty
    decide how much of the budget it had used."""
    from src.services import spend_service

    rid = _reserve(5 * _CENT)
    _reconcile(rid, status_code=200, settlement=None)

    assert spend_service.get_status()["spent_usd"] == 0.05
    assert spend_service.list_payments()[0]["state"] == "authorized"


def test_reconcile_survives_a_receipt_with_no_transaction_field(temp_db):
    """Missing settlement evidence remains uncertain and counted."""
    from src.services import spend_service

    rid = _reserve(5 * _CENT)
    _reconcile(rid, settlement={"network": "eip155:84532"})

    row = spend_service.list_payments()[0]
    assert row["state"] == "authorized" and row["transaction"] is None


# ---------------------------------------------------------------------------
# unreconciled_count(): makes a forgotten reconcile() visible
# ---------------------------------------------------------------------------


def test_a_reconciled_ledger_reports_nothing_unreconciled(temp_db):
    from src.services import spend_service

    rid = _reserve(_CENT, valid_before=1)          # long expired
    _reconcile(rid, settlement={"success": True, "transaction": "0x" + "ab" * 32, "network": "eip155:84532", "payer": "0x" + "11" * 20})
    assert spend_service.get_status()["unreconciled_count"] == 0


def test_a_driver_that_skips_reconcile_shows_up_in_the_count(temp_db):
    """The forcing function.

    Skipping reconcile() raises nothing and breaks no payment -- the ledger
    just silently stops recording whether anything settled. This is the
    number that makes that visible, and it is why the payer's own path
    asserts it stays at zero.
    """
    from src.services import spend_service

    _reserve(_CENT, valid_before=1)                # expired, never closed out
    assert spend_service.get_status()["unreconciled_count"] == 1


def test_a_payment_still_in_flight_is_not_counted_as_unreconciled(temp_db):
    """An authorization signed seconds ago is legitimately open. Counting it
    would make the signal cry wolf on every healthy call."""
    import time

    from src.services import spend_service

    _reserve(_CENT, valid_before=int(time.time()) + 300)
    assert spend_service.get_status()["unreconciled_count"] == 0


def test_rows_without_an_expiry_fall_back_to_a_grace_window(temp_db):
    """Older rows carry no `valid_before`. They still have to be detectable,
    or the counter is blind to exactly the history most likely to be stale."""
    from src.services import spend_service
    from src.shared.db.sqlite import get_connection

    rid = _reserve(_CENT)                          # no valid_before
    assert spend_service.get_status()["unreconciled_count"] == 0
    get_connection().execute(
        "UPDATE x402_payments SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (rid,),
    )
    get_connection().commit()
    assert spend_service.get_status()["unreconciled_count"] == 1
