"""allocation_service — per-strategy fund accounting.

Live strategies require an explicit allocation: a commitment of (token, amount)
from a specific wallet. Paper strategies do not.

- record_allocation: called when a strategy transitions to `live` (via
  PATCH /strategies/{id}/status with an `allocation` block). Validates the
  wallet exists; inserts an active allocation row.
- release_allocation: called when a live strategy is deactivated or
  archived. Marks the strategy's active allocation as inactive and stamps
  released_at.
- get_active_allocation: lookup helper, returns Allocation or None.

Invariant: at most one active allocation per strategy_id. We don't enforce
this at the DB level (would need a partial unique index), but the release
logic and the strategy-service transitions make it true in practice.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from src.models.domain import Allocation
from src.services.wallet_manager import wallet_exists
from src.shared.db.sqlite import get_connection
from src.shared.errors import AllocationInsufficient, WalletNotFound
from src.shared.logging import get_logger

_log = get_logger(__name__)


def _row_to_allocation(r) -> Allocation:
    return Allocation(
        id=r["id"],
        strategy_id=r["strategy_id"],
        wallet_address=r["wallet_address"],
        token_address=r["token_address"],
        token_symbol=r["token_symbol"],
        amount=r["amount"],
        active=bool(r["active"]),
        created_at=datetime.fromisoformat(r["created_at"]),
        released_at=datetime.fromisoformat(r["released_at"]) if r["released_at"] else None,
        # slippage_pct column added in migration 004; older rows may return None.
        slippage_pct=(r["slippage_pct"] if "slippage_pct" in r.keys() else None),
    )


def record_allocation(
    strategy_id: str,
    wallet_address: str,
    token_address: str,
    token_symbol: str,
    amount: float,
    slippage_pct: float | None = None,
) -> Allocation:
    """Record a new active allocation for a strategy.

    Validates the wallet exists and amount > 0. Does NOT check on-chain
    balance — that's the user's responsibility when they fund the wallet.

    `slippage_pct` is DECIMAL (0.005 = 0.5%). Callers via
    StrategyAllocationInput enforce the 0.0025 cap + required-ness at the
    Pydantic layer; we accept None here only so pre-migration test fixtures
    and legacy call sites don't break compilation. The tick / cron path
    refuses to execute_many with a None slippage_pct.
    """
    if not Decimal(str(amount)).is_finite() or amount <= 0:
        raise AllocationInsufficient(
            f"Allocation amount must be > 0; got {amount}.",
            suggestion="Pass a positive amount in the allocation block of PATCH /strategies/{id}/status.",
        )
    if not wallet_exists(wallet_address):
        raise WalletNotFound(
            f"Wallet {wallet_address} not in local store.",
            suggestion="Use POST /wallet/create first, or pass an address that matches GET /wallet/list.",
        )

    existing = get_active_allocation(strategy_id)
    if existing:
        if (
            existing.wallet_address.lower() == wallet_address.lower()
            and existing.token_address.lower() == token_address.lower()
            and existing.amount == amount
            and existing.slippage_pct == slippage_pct
        ):
            return existing
        raise AllocationInsufficient("An active execution allocation cannot be replaced while retained.")
    alloc_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)
    conn = get_connection()
    conn.execute(
        """INSERT INTO allocations
           (id, strategy_id, wallet_address, token_address, token_symbol,
            amount, active, created_at, released_at, slippage_pct)
           VALUES (?,?,?,?,?,?,1,?,NULL,?)""",
        (
            alloc_id,
            strategy_id,
            wallet_address,
            token_address,
            token_symbol,
            amount,
            created_at.isoformat(),
            slippage_pct,
        ),
    )
    # A new explicit allocation records the opening quote budget. Historical
    # capital cannot be reconstructed from an already-running legacy book.
    history = conn.execute(
        "SELECT 1 FROM trades WHERE strategy_id=? AND mode='live' LIMIT 1", (strategy_id,)
    ).fetchone()
    history = history or conn.execute("SELECT 1 FROM dex_orders WHERE strategy_id=? LIMIT 1", (strategy_id,)).fetchone()
    history = (
        history or conn.execute("SELECT 1 FROM broker_orders WHERE strategy_id=? LIMIT 1", (strategy_id,)).fetchone()
    )
    baseline = conn.execute("SELECT * FROM execution_capital WHERE strategy_id=?", (strategy_id,)).fetchone()
    if baseline:
        if (
            Decimal(baseline["amount"]) != Decimal(str(amount))
            or baseline["quote_token_address"].lower() != token_address.lower()
        ):
            conn.rollback()
            raise AllocationInsufficient(
                "Execution capital is immutable once recorded; use a new strategy for a different budget."
            )
    elif not history:
        conn.execute(
            "INSERT INTO execution_capital VALUES (?,?,?,?,?,?)",
            (strategy_id, alloc_id, token_address.lower(), token_symbol, str(amount), created_at.isoformat()),
        )
        conn.execute(
            "UPDATE strategies SET execution_state_json=NULL,open_positions_json='[]',execution_revision=execution_revision+1 WHERE id=?",
            (strategy_id,),
        )
    conn.commit()

    _log.info(
        "allocation.recorded",
        allocation_id=alloc_id,
        strategy_id=strategy_id,
        wallet_address=wallet_address,
        token_symbol=token_symbol,
        amount=amount,
        slippage_pct=slippage_pct,
    )

    return Allocation(
        id=alloc_id,
        strategy_id=strategy_id,
        wallet_address=wallet_address,
        token_address=token_address,
        token_symbol=token_symbol,
        amount=amount,
        active=True,
        created_at=created_at,
        released_at=None,
        slippage_pct=slippage_pct,
    )


def release_allocation(strategy_id: str) -> Allocation | None:
    """Mark the strategy's active allocation as inactive.

    Returns the released allocation (with released_at populated), or None if
    no active allocation existed. Safe to call repeatedly.
    """
    conn = get_connection()
    row = conn.execute(
        """SELECT * FROM allocations
           WHERE strategy_id = ? AND active = 1
           ORDER BY created_at DESC LIMIT 1""",
        (strategy_id,),
    ).fetchone()
    if not row:
        return None

    released_at = datetime.now(timezone.utc)
    conn.execute(
        """UPDATE allocations
           SET active = 0, released_at = ?
           WHERE id = ?""",
        (released_at.isoformat(), row["id"]),
    )
    conn.commit()

    _log.info(
        "allocation.released",
        allocation_id=row["id"],
        strategy_id=strategy_id,
    )

    alloc = _row_to_allocation(row)
    return alloc.model_copy(update={"active": False, "released_at": released_at})


def get_active_allocation(strategy_id: str) -> Allocation | None:
    """Return the strategy's currently-active allocation, or None."""
    row = (
        get_connection()
        .execute(
            """SELECT * FROM allocations
           WHERE strategy_id = ? AND active = 1
           ORDER BY created_at DESC LIMIT 1""",
            (strategy_id,),
        )
        .fetchone()
    )
    return _row_to_allocation(row) if row else None
