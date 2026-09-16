"""spend_service — the agent-side cap on OUTBOUND x402 spending.

Why this lives here, and only here
----------------------------------
Nothing else can see the total. The wallet signing guard
(`wallet_manager.sign_x402_authorization`) validates one payload's shape and
says so in its own docstring: "a signing guard validates one payload's shape
and cannot see an aggregate." The payer (`x402_payer`) sees one request. The
receiving server sees one payment, from one caller, once. Only this process
holds every outbound payment it has ever made, so the aggregate control has
to be here — the identical argument that puts the portfolio kill switch in
`portfolio_risk_service` rather than in the MangroveAI engine.

The risk it exists to bound is not one absurd charge; the SDK's per-payment
ceiling (`x402_payer._MAX_AMOUNT_PER_PAYMENT`, $1) already covers that. It is
volume. An autonomous sweep is 99 backtests at $0.02 — $2 for one experiment.
Worse, `oracle_backtest_async` + `oracle_backtest_poll` form a poll loop
against a priced meter: a 5-second poll on a two-minute backtest is 24 paid
calls for a single result. Each one is individually reasonable and passes
every per-payment check there is.

The model: one budget, many wallets
-----------------------------------
The cap is AGENT-WIDE, not per wallet. `x402_payer.resolve_payer_wallet`
lets an explicit argument override `X402_PAYER_WALLET`, so a budget keyed to
the configured wallet would be sidestepped by any caller that names a
different address. The ledger still records which wallet paid — that is audit
data, not the budget key.

Authorizations, not settlements
-------------------------------
Spending is counted from the moment a payment is AUTHORIZED, because that is
the last moment the agent is in control. After a signature exists, whether
money moves is the receiver's decision, reported through an optional header
that servers disagree about the name of. A budget that only counted confirmed
settlements would let the counterparty choose how much of the budget it had
used. So:

- `reserve()` writes an `authorized` row and it counts immediately.
- `settle()` attaches the settlement transaction — evidence, not accounting.
- `release()` removes a row from the count, and ONLY on positive evidence the
  authorization is dead: the guard refused to sign it, or the receiver
  rejected it (which burns the nonce, so it can never be settled later).

Anything ambiguous stays counted.

Why expired authorizations are NOT reclaimed automatically
----------------------------------------------------------
An authorization dies at `validBefore` (+300s): past that instant USDC will
reject it, so nobody can ever settle it. That makes an `authorized` row older
than its own expiry provably dead money, and handing its budget back looks
like an obvious win. `valid_before` is recorded on every row precisely so it
can be — but nothing does it yet, deliberately.

The reason is that "still `authorized` after expiry" does not yet mean "never
settled". `reconcile()` has to be CALLED — it needs the response, which the
signer never sees — so a payment driver that skips it leaves every row
`authorized` whatever the outcome. Under that regime an expiry sweep would
release successful payments too, and the running total would count nothing at
all. A budget that silently stops counting is far worse than one that counts
slightly high.

So the order matters: prove reconciliation is actually happening, then turn on
the sweep. `unreconciled_count()` is the proof — when it stays at zero, every
`authorized` row really is in flight and expiry really does mean unsettled.
Reclaiming before then would convert a conservative over-count into an
unbounded under-count, the one direction a spend control must never fail.

A budget, not a circuit breaker
------------------------------
The two look alike and are not. A breaker fires on an ANOMALY, latches, and
demands review — a 30% book drawdown means something went wrong, which is
why `portfolio_risk_service` is shaped the way it is. A budget runs out
through entirely NORMAL use. Nothing went wrong; the remedy is a top-up, not
an investigation.

So spending here is `exhausted`, never `tripped`, and it does not clear
itself — but authorizing more is one sentence through the `x402_spend_reset`
MCP tool, not a trip to a terminal. That matters because the risk this
bounds is unattended spend: a cron tick at 3am that exhausts the budget
finds no human to ask and stays stopped, which is correct, while a user
who is right there answers "yes, make it $50" and carries on. The strictness
scales with the absence of a human, and the code can actually see that
difference — a tool call has one, a scheduler tick does not.

One thing that is NOT a top-up prompt: a single payment larger than the
remaining budget. That is refused outright and the budget is left alone,
because the amount comes out of a remote server's 402 envelope — treating it
as exhaustion would hand any resource server a one-request denial of service
over the agent's whole payment path.
"""
from __future__ import annotations

import math
import threading
import uuid
from datetime import datetime, timedelta, timezone

from src.config import app_config
from src.shared.db.sqlite import get_connection
from src.shared.errors import ValidationError, X402SpendCapExceeded
from src.shared.logging import get_logger
from src.shared.urls import strip_query

_log = get_logger(__name__)

# Fallback when X402_SPEND_CAP_USD is absent from a config file written
# before this key existed.
#
# $25 is sized against what the product actually does, not against what a
# mistake should cost. A sweep is 99 backtests -- about $2 -- so $5 was
# two and a half sweeps, roughly one afternoon of the
# headline workflow, and a budget a normal user exhausts on day one teaches
# them to raise it without reading it. $25 is ~10 sweeps or ~25,000 $0.001
# signal reads: comfortably more than a session, still cheap enough that
# discovering the limit the hard way costs a coffee.
_DEFAULT_CAP_USD = 25.0

# USDC is 6-decimal on every chain the signing guard allows, so an EIP-3009
# `value` is already an integer count of micro-dollars. Money is only ever
# added, compared and stored in these units; floats appear at the API
# boundary for display and nowhere else.
_MICRO_USD_PER_USD = 1_000_000

# The largest amount a SQLite INTEGER column can hold. An EIP-3009 `value` is
# a uint256, so it can carry numbers far past this; they are refused rather
# than allowed to reach the insert.
_MAX_STORABLE_MICRO_USD = 2**63 - 1

# How long after an authorization is written it can still plausibly be
# in flight. Past this, `authorized` no longer means "we are waiting" -- an
# EIP-3009 authorization is dead at `validBefore` (+300s by convention), so
# nothing can still be settling. Used only to spot reservations nobody ever
# closed out; it never releases anything on its own.
_AUTHORIZATION_GRACE_S = 300

# States that consume budget. `released` is the only one that does not.
_COUNTING_STATES = ("authorized", "settled")

# Reserving is read-then-write: the running total is summed, a decision is
# made, and a row is inserted. The agent is a single process, but the HTTP
# server and the APScheduler tick run on different threads against one shared
# SQLite connection, so two concurrent payments could each read a total that
# does not include the other. This lock makes the sequence atomic. It is NOT
# a substitute for a database constraint across processes — the agent is
# single-process by construction (one SQLite file, one scheduler).
_reserve_lock = threading.Lock()

# Said the same way everywhere a payment is refused for want of budget.
# Names the MCP tools rather than the REST routes on purpose: the whole
# point is that authorizing more should not require leaving the
# conversation for a terminal.
TOP_UP_SUGGESTION = (
    "Show the user what the money went on (`x402_spend_status` lists the "
    "ledger), then ask whether to authorize more. If they agree, "
    "`x402_spend_reset` with confirm=true and the cap_usd they chose. Never "
    "top up a budget on the user's behalf — their consent IS the control."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cap_micro_usd(state: dict | None = None) -> int:
    """The budget for the current period, in micro-USD.

    Precedence: what a human authorized for this period, then config, then
    the default. The period value wins because it was set by an explicit
    act at a known moment — better provenance for "how much did the user
    agree to spend" than a file that could have been edited at any time.
    Config remains the starting budget for a fresh install.
    """
    state = state if state is not None else _get_state()
    authorized = state.get("period_cap_micro_usd")
    if authorized is not None:
        return int(authorized)
    return _configured_cap_micro_usd()


def _config_cap_is_set() -> bool:
    """Whether X402_SPEND_CAP_USD actually supplies the budget.

    Present-but-blank counts as unset, exactly as `_configured_cap_micro_usd`
    treats it. The two must agree: reporting `cap_source: "config"` next to a
    number that came from the default would misattribute where the budget was
    decided, which is the one thing that field exists to answer.
    """
    configured = getattr(app_config, "X402_SPEND_CAP_USD", None)
    return configured is not None and str(configured).strip() != ""


def _configured_cap_micro_usd() -> int:
    """X402_SPEND_CAP_USD in micro-USD.

    Deliberately NOT written as `config_value or _DEFAULT`, the idiom used by
    `portfolio_risk_service._limit()`: a configured cap of `0` is falsy, and
    that idiom would silently replace "never spend anything" with the
    default. Zero is a legitimate, meaningful budget and must survive.
    """
    if not _config_cap_is_set():
        return _to_micro_usd(_DEFAULT_CAP_USD, source="X402_SPEND_CAP_USD")
    return _to_micro_usd(
        getattr(app_config, "X402_SPEND_CAP_USD", None), source="X402_SPEND_CAP_USD"
    )


def _to_micro_usd(cap_usd: object, *, source: str) -> int:
    """Validate a dollar budget from any origin and convert it exactly.

    Shared by the config path and the human top-up so the two cannot drift
    apart. They did once: the top-up rejected infinity and NaN while the
    config path let both through to `round()`, which raised OverflowError /
    ValueError instead of a refusal. JSON admits `Infinity` and `NaN` as
    literals, so a config file could reach it.
    """
    try:
        value = float(cap_usd)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ValidationError(
            f"{source} is {cap_usd!r}, which is not a number.",
            suggestion=f"Set {source} to a dollar amount, e.g. {_DEFAULT_CAP_USD:g}. The agent will not pay for anything until the budget is readable.",
        ) from e
    # NaN and infinity both survive float() and neither can bound anything.
    # `isfinite` says that in one predicate; a range check cannot, because
    # NaN compares False against every bound you could write.
    if not math.isfinite(value):
        raise ValidationError(
            f"{source} is {value}, which is not a usable budget.",
            suggestion=f"Set {source} to a finite dollar amount, e.g. {_DEFAULT_CAP_USD:g}.",
        )
    if value < 0:
        raise ValidationError(
            f"{source} is {value}, which is negative.",
            suggestion="Use 0 to block all outbound payments, or a positive dollar amount to allow them up to that total.",
        )
    micro = round(value * _MICRO_USD_PER_USD)
    if micro > _MAX_STORABLE_MICRO_USD:
        raise ValidationError(
            f"{source} is {value}, which is larger than any budget this agent can track.",
            suggestion="Pick a budget you would actually be willing to lose to a single runaway loop.",
        )
    return micro


def _to_usd(micro_usd: int) -> float:
    """Micro-USD to dollars, for display only. Never fed back into a total."""
    return round(micro_usd / _MICRO_USD_PER_USD, 6)


def _get_state() -> dict:
    row = get_connection().execute(
        "SELECT period_id, period_started_at, period_cap_micro_usd, exhausted, "
        "exhausted_at, exhausted_reason, updated_at FROM x402_spend_state WHERE id = 1"
    ).fetchone()
    # Defensive: the migration seeds row 1, but never assume.
    if row is None:
        return {"period_id": 1, "period_started_at": None, "period_cap_micro_usd": None,
                "exhausted": False, "exhausted_at": None, "exhausted_reason": None,
                "updated_at": None}
    return {
        "period_id": int(row["period_id"]),
        "period_started_at": row["period_started_at"],
        "period_cap_micro_usd": row["period_cap_micro_usd"],
        "exhausted": bool(row["exhausted"]),
        "exhausted_at": row["exhausted_at"],
        "exhausted_reason": row["exhausted_reason"],
        "updated_at": row["updated_at"],
    }


def _update_state(**fields) -> None:
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    conn.execute(f"UPDATE x402_spend_state SET {cols} WHERE id = 1", tuple(fields.values()))
    conn.commit()


def _spent_micro_usd(period_id: int) -> int:
    """Committed spend for a period: every row that is not released."""
    placeholders = ", ".join("?" for _ in _COUNTING_STATES)
    row = get_connection().execute(
        f"SELECT COALESCE(SUM(amount_micro_usd), 0) AS v FROM x402_payments "
        f"WHERE period_id = ? AND state IN ({placeholders})",
        (period_id, *_COUNTING_STATES),
    ).fetchone()
    return int(row["v"])


def _coerce_unix_seconds(value: object) -> int | None:
    """Read an EIP-3009 `validBefore` as a unix second, or None.

    Unlike the amount, an unreadable value here is NOT fatal: this field is
    audit metadata, and refusing a payment because its expiry timestamp was
    oddly typed would trade a real capability for a cosmetic one. The guard
    validates it properly a moment later either way.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if 0 < seconds <= _MAX_STORABLE_MICRO_USD else None


def _coerce_micro_usd(value: object) -> int:
    """Read an EIP-3009 `value` as an integer count of micro-USD.

    Refuses anything it cannot read exactly. A payment whose amount is
    unreadable cannot be budgeted, and "unbudgetable" must mean "unpaid" —
    the alternative is a charge that never appears in the running total.

    `bool` is excluded explicitly because it is an `int` subclass in Python,
    so `True` would otherwise be accepted as one micro-dollar.
    """
    if isinstance(value, bool):
        raise ValidationError(
            "Refused to budget an x402 payment: the authorization `value` is a boolean.",
            suggestion="This is a malformed payment envelope, not a price. Treat it as a tampered 402 response.",
        )
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        amount = int(value.strip())
    else:
        raise ValidationError(
            f"Refused to budget an x402 payment: the authorization `value` is {value!r}, "
            "which is not an integer amount of USDC base units.",
            suggestion="The agent budgets every payment before signing it, so an amount it cannot read is an amount it will not pay. Check the 402 envelope from the resource server.",
        )
    if amount < 0:
        raise ValidationError(
            f"Refused to budget an x402 payment: the authorization `value` is {amount}, "
            "which is negative.",
            suggestion="A payment amount cannot be negative. Treat this as a tampered 402 response.",
        )
    if amount > _MAX_STORABLE_MICRO_USD:
        # uint256 admits numbers SQLite's 64-bit INTEGER cannot hold. With a
        # sane cap such a value is refused on budget grounds anyway, but that
        # depends on the configured cap being sane -- and a money control
        # should not be one config typo away from an OverflowError instead of
        # a refusal.
        raise ValidationError(
            f"Refused to budget an x402 payment: the authorization `value` is {amount}, "
            "which is larger than any real USDC amount.",
            suggestion="USDC's entire supply is a rounding error next to this number. Treat it as a tampered 402 response.",
        )
    return amount


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_status() -> dict:
    """Current budget state, for /status, the REST route, and the MCP tool."""
    state = _get_state()
    cap = _cap_micro_usd(state)
    spent = _spent_micro_usd(state["period_id"])
    return {
        "exhausted": state["exhausted"],
        "exhausted_at": state["exhausted_at"],
        "exhausted_reason": state["exhausted_reason"],
        "cap_usd": _to_usd(cap),
        # Where the budget came from, so the agent can say "the $25 default"
        # rather than implying someone chose it.
        "cap_source": "authorized" if state["period_cap_micro_usd"] is not None else (
            "config" if _config_cap_is_set() else "default"
        ),
        "spent_usd": _to_usd(spent),
        # Clamped at zero: a caller reads this to answer "can I afford the
        # next call", and a negative remainder is not a more useful no.
        "remaining_usd": _to_usd(max(cap - spent, 0)),
        "payment_count": _payment_count(state["period_id"]),
        # Should be zero. Non-zero means some payment driver is not calling
        # reconcile(), so those rows will never record whether they settled.
        "unreconciled_count": unreconciled_count(state["period_id"]),
        "period_id": state["period_id"],
        "period_started_at": state["period_started_at"],
    }


def unreconciled_count(period_id: int | None = None) -> int:
    """Authorizations that should have been closed out by now and were not.

    The number that makes a forgotten `reconcile()` visible. A row is counted
    once its authorization can no longer be settled by anybody — past
    `valid_before`, or, for a row written without one, `_AUTHORIZATION_GRACE_S`
    after it was created. At that point "still `authorized`" cannot mean "in
    flight"; it means nobody ever recorded what happened.

    **It should be zero.** A non-zero value does not mean money was lost — it
    means the ledger is no longer an audit trail, because those rows will
    never carry a settlement transaction or a terminal state. That is the
    failure mode this counter exists to stop being silent, since skipping
    reconciliation raises nothing and breaks no payment.
    """
    state_period = period_id if period_id is not None else _get_state()["period_id"]
    now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(seconds=_AUTHORIZATION_GRACE_S)).isoformat()
    row = get_connection().execute(
        """SELECT COUNT(*) AS c FROM x402_payments
            WHERE period_id = ? AND state = 'authorized'
              AND ((valid_before IS NOT NULL AND valid_before < ?)
                OR (valid_before IS NULL AND created_at < ?))""",
        (state_period, int(now.timestamp()), cutoff_iso),
    ).fetchone()
    return int(row["c"])


def _payment_count(period_id: int) -> int:
    placeholders = ", ".join("?" for _ in _COUNTING_STATES)
    row = get_connection().execute(
        f"SELECT COUNT(*) AS c FROM x402_payments "
        f"WHERE period_id = ? AND state IN ({placeholders})",
        (period_id, *_COUNTING_STATES),
    ).fetchone()
    return int(row["c"])


def list_payments(*, limit: int = 50, period_id: int | None = None) -> list[dict]:
    """The payment ledger, newest first. The audit trail behind the total."""
    limit = max(1, min(int(limit), 500))
    sql = (
        "SELECT id, period_id, state, amount_micro_usd, wallet_address, payee, "
        "network, resource, transaction_hash, valid_before, release_reason, "
        "created_at, updated_at "
        "FROM x402_payments"
    )
    params: tuple = ()
    if period_id is not None:
        sql += " WHERE period_id = ?"
        params = (int(period_id),)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    rows = get_connection().execute(sql, (*params, limit)).fetchall()
    return [
        {
            "id": r["id"],
            "period_id": r["period_id"],
            "state": r["state"],
            "amount_usd": _to_usd(int(r["amount_micro_usd"])),
            "wallet_address": r["wallet_address"],
            "payee": r["payee"],
            "network": r["network"],
            "resource": r["resource"],
            "transaction": r["transaction_hash"],
            "valid_before": r["valid_before"],
            "release_reason": r["release_reason"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def check_before_payment() -> dict:
    """Cheap pre-flight, before a request is even sent.

    The binding check is in `reserve()`, which is the one that knows the
    amount. This exists so an exhausted budget costs a dictionary lookup
    instead of a network round trip to a server that is about to quote a
    price the agent cannot pay.
    """
    state = _get_state()
    cap = _cap_micro_usd(state)
    spent = _spent_micro_usd(state["period_id"])
    remaining = cap - spent
    if state["exhausted"] or remaining <= 0:
        return {"allowed": False, "exhausted": True,
                "reason": _spent_reason(state, spent=spent, cap=cap),
                "spent_usd": _to_usd(spent), "cap_usd": _to_usd(cap), "remaining_usd": 0.0}
    return {"allowed": True, "exhausted": False, "reason": None,
            "spent_usd": _to_usd(spent), "cap_usd": _to_usd(cap),
            "remaining_usd": _to_usd(remaining)}


def _spent_reason(state: dict, *, spent: int, cap: int) -> str:
    """Explain why payment is stopped, without arguing against itself.

    The naive version recomputed "$X of $Y used" at read time. That reads
    correctly right up until a payment is released after the budget ran
    out: the total drops back below the limit while the stop stays in
    force, and the message becomes "the budget is spent — $0.95 of $1.00
    used", which invites the reader to conclude the accounting is broken.

    So the recorded reason -- the numbers as they stood when the budget
    actually ran out -- is preferred, and the live figures are only used
    when there is no recorded reason to quote.
    """
    if state["exhausted"] and state["exhausted_reason"]:
        return (f"the x402 budget was spent and needs authorizing again "
                f"({state['exhausted_reason']})")
    return (f"the x402 budget is spent — ${_to_usd(spent)} of "
            f"${_to_usd(cap)} used")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def reserve(
    *,
    value: object,
    wallet_address: str,
    payee: str | None = None,
    network: str | None = None,
    resource: str | None = None,
    valid_before: object = None,
) -> str:
    """Claim budget for one payment. Raises rather than overspending.

    Called immediately before a signature is produced, with the `value` from
    the authorization about to be signed — so what is budgeted is exactly
    what gets signed, not an estimate quoted earlier in the exchange.

    Returns the reservation id. The caller must later `settle()` or
    `release()` it; anything left `authorized` keeps consuming budget, which
    is the intended behaviour for an outcome nobody can confirm.

    Raises `X402SpendCapExceeded` when the latch is set or the payment does
    not fit. Raises `ValidationError` when the amount is unreadable.
    """
    amount = _coerce_micro_usd(value)
    resource = strip_query(resource)
    valid_before = _coerce_unix_seconds(valid_before)

    with _reserve_lock:
        state = _get_state()
        cap = _cap_micro_usd(state)
        spent = _spent_micro_usd(state["period_id"])

        if state["exhausted"]:
            _log.warning("x402.spend.refused", reason="budget_spent", amount_usd=_to_usd(amount),
                         spent_usd=_to_usd(spent), cap_usd=_to_usd(cap))
            raise X402SpendCapExceeded(
                f"The x402 budget is spent: {state['exhausted_reason']}. "
                "Nothing further will be paid for until someone authorizes more.",
                suggestion=TOP_UP_SUGGESTION,
            )

        if spent + amount > cap:
            # Refused, NOT latched. The amount came from a remote server's
            # envelope; latching on it would let any resource server halt
            # the agent's payments with one oversized quote.
            _log.warning("x402.spend.refused", reason="would_exceed_budget",
                         amount_usd=_to_usd(amount), spent_usd=_to_usd(spent),
                         cap_usd=_to_usd(cap), payee=payee, resource=resource)
            raise X402SpendCapExceeded(
                f"This payment of ${_to_usd(amount)} would take x402 spending to "
                f"${_to_usd(spent + amount)}, past the ${_to_usd(cap)} budget "
                f"(${_to_usd(max(cap - spent, 0))} left). Nothing was signed.",
                suggestion="A single payment this size usually means the resource is priced differently than expected — read the ledger (`x402_spend_status`) before authorizing more. If the price is right and the budget is simply too small, `x402_spend_reset` with a higher cap_usd.",
            )

        reservation_id = str(uuid.uuid4())
        now = _now()
        conn = get_connection()
        conn.execute(
            """INSERT INTO x402_payments (id, period_id, state, amount_micro_usd,
                 wallet_address, payee, network, resource, transaction_hash,
                 valid_before, release_reason, created_at, updated_at)
               VALUES (?, ?, 'authorized', ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)""",
            (reservation_id, state["period_id"], amount, wallet_address, payee,
             network, resource, valid_before, now, now),
        )
        conn.commit()

        spent_after = spent + amount
        count = _payment_count(state["period_id"])
        _log.info("x402.spend.reserved", reservation_id=reservation_id,
                  amount_usd=_to_usd(amount), spent_usd=_to_usd(spent_after),
                  cap_usd=_to_usd(cap), wallet_address=wallet_address,
                  payee=payee, resource=resource)

        # The budget is now spent -- this payment fit exactly, or filled the
        # last of it. Marked here rather than on the next attempt so it is
        # recorded against the payment that consumed it.
        #
        # `spent_after > 0` keeps a budget of $0 from being marked spent by a
        # zero-value authorization: "never pay for anything" is already
        # enforced by the refusal above, and there is nothing to top up.
        if spent_after >= cap and spent_after > 0:
            _mark_exhausted(
                f"${_to_usd(spent_after)} of the ${_to_usd(cap)} budget used "
                f"across {count} payments",
                spent=spent_after, cap=cap,
            )

    return reservation_id


def _mark_exhausted(reason: str, *, spent: int, cap: int) -> None:
    """Record that the budget is spent. Never clears itself.

    WARNING, not ERROR: a spent budget is the expected end of normal use,
    not a fault. Logging it at error level would put a routine top-up
    prompt in the same bucket as the portfolio kill switch.
    """
    _update_state(exhausted=1, exhausted_at=_now(), exhausted_reason=reason)
    _log.warning("x402.spend.exhausted", reason=reason,
                 spent_usd=_to_usd(spent), cap_usd=_to_usd(cap))


def reconcile(
    reservation_ids: list[str] | tuple[str, ...] | str | None,
    *,
    status_code: int,
    settlement: dict | None = None,
    resource: str | None = None,
) -> None:
    """Close out an exchange's reservations. **Every payment driver must call this.**

    `reserve()` is unavoidable — it runs inside the signer, so no transport
    can pay without being charged. This is the other half, and it is NOT
    structural, because closing a reservation needs the OUTCOME and the
    signer never sees a response. It therefore has to be called explicitly
    by whatever drove the request: `x402_payer.pay` today, and any transport
    that injects payment into an SDK client tomorrow.

    Skipping it does not raise and does not break a payment. What it does is
    quieter and worse: every row stays `authorized` forever, so the ledger
    records only that the agent *tried* to pay. No settlement transaction is
    written, success and failure become indistinguishable on disk, and the
    audit trail this table exists to provide is empty while still looking
    like a ledger. `get_status()["unreconciled_count"]` is how that shows up
    if it ever happens — it should be zero.

    The outcome-to-state mapping lives here rather than in the caller so
    every driver agrees on what a given response means:

    - **A settlement receipt** — settled, transaction recorded. The total
      does not change; it was charged when the authorization was signed.
    - **402** — the receiver rejected the signature and burned the nonce, so
      the authorization can never be presented again. Released.
    - **>= 400** — on REST the receiver skips settlement for an errored
      resource, so nothing was charged. Released.
    - **Anything else** — a success with no readable receipt. Ambiguous, and
      deliberately left counted: releasing on a missing optional header
      would let the counterparty decide how much budget it had used.
    """
    ids = _as_id_list(reservation_ids)
    if not ids:
        return

    transaction = settlement.get("transaction") if isinstance(settlement, dict) else None
    if settlement is not None:
        settle(ids, transaction=transaction if isinstance(transaction, str) else None,
               resource=resource)
    elif status_code == 402:
        release(ids, reason="rejected_by_receiver")
    elif status_code >= 400:
        release(ids, reason="resource_error_not_settled")
    else:
        _log.warning(
            "x402.spend.settlement_unconfirmed",
            reservation_ids=list(ids),
            status_code=status_code,
            resource=strip_query(resource),
        )


def settle(reservation_ids: list[str] | tuple[str, ...] | str | None, *,
           transaction: str | None = None, resource: str | None = None) -> None:
    """Attach settlement evidence to a reservation. Does not change the total.

    Only the LAST reservation can have settled: the receiver burns a nonce
    before verifying it, so every earlier attempt in the same exchange is
    dead by the time a later one succeeds. Those earlier rows are released
    here rather than left counting.
    """
    ids = _as_id_list(reservation_ids)
    if not ids:
        return
    settled_id, superseded = ids[-1], ids[:-1]
    if superseded:
        release(superseded, reason="superseded_by_later_attempt")

    now = _now()
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE x402_payments SET state = 'settled', transaction_hash = ?, "
        "resource = COALESCE(?, resource), updated_at = ? "
        "WHERE id = ? AND state = 'authorized'",
        (transaction, strip_query(resource), now, settled_id),
    )
    conn.commit()
    if cursor.rowcount:
        _log.info("x402.spend.settled", reservation_id=settled_id, transaction=transaction)
    else:
        # Already settled or already released. Worth a line rather than a
        # silent no-op: on a ledger, "nothing changed" is itself the finding.
        _log.warning("x402.spend.settle_noop", reservation_id=settled_id,
                     transaction=transaction)


def release(reservation_ids: list[str] | tuple[str, ...] | str | None, *, reason: str) -> None:
    """Give budget back, for authorizations that provably cannot be settled.

    Call this ONLY with positive evidence the authorization is dead — the
    guard refused to sign, or the receiver rejected the payment and burned
    the nonce. An unknown outcome is not evidence: leaving a row counted
    costs part of a budget, releasing one that later settles costs money.

    Releases only `authorized` rows, so re-running it cannot resurrect
    budget from a payment already known to have settled.
    """
    ids = _as_id_list(reservation_ids)
    if not ids:
        return
    now = _now()
    conn = get_connection()
    placeholders = ", ".join("?" for _ in ids)
    cursor = conn.execute(
        f"UPDATE x402_payments SET state = 'released', release_reason = ?, updated_at = ? "
        f"WHERE id IN ({placeholders}) AND state = 'authorized'",
        (reason, now, *ids),
    )
    conn.commit()
    if cursor.rowcount:
        _log.info("x402.spend.released", reservation_ids=list(ids),
                  released=cursor.rowcount, reason=reason)


def _as_id_list(value: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [v for v in value if v]


# ---------------------------------------------------------------------------
# Human re-activation
# ---------------------------------------------------------------------------


def reset(cap_usd: float | None = None) -> dict:
    """Top up: start a fresh budget period, optionally with a new size.

    This is the human-consent step, and it is the ONLY thing that resumes
    payment. `cap_usd` records what they actually agreed to for this period
    and overrides config from here on; omit it to keep the current budget
    size and simply start again.

    The running total restarts at zero rather than merely unblocking —
    otherwise the very next payment would exhaust the budget again against a
    total already at the limit.

    History is kept. The period id advances and past rows keep their old
    one, so `list_payments()` still shows every payment the agent has ever
    authorized while the total starts fresh.
    """
    state = _get_state()
    previous_spent = _spent_micro_usd(state["period_id"])
    new_period = state["period_id"] + 1
    fields: dict = {
        "period_id": new_period,
        "period_started_at": _now(),
        "exhausted": 0,
        "exhausted_at": None,
        "exhausted_reason": None,
    }
    if cap_usd is not None:
        fields["period_cap_micro_usd"] = _authorized_cap_micro_usd(cap_usd)
    _update_state(**fields)

    _log.warning("x402.spend.topped_up", previous_period=state["period_id"],
                 previous_spent_usd=_to_usd(previous_spent), new_period=new_period,
                 authorized_cap_usd=cap_usd)
    return get_status()


def _authorized_cap_micro_usd(cap_usd: float) -> int:
    """Validate a human-supplied budget. Refuses rather than coercing.

    A typo in the one number that bounds outbound spending should stop the
    top-up, not round itself into something plausible. Same validator as the
    config path, so the two cannot disagree about what a budget may be.
    """
    return _to_micro_usd(cap_usd, source="cap_usd")
