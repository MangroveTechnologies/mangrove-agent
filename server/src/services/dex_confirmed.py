"""Durable execution evidence for externally executed live strategy orders.

Quotes authorize bounds; only finalized receipt wallet transfers change holdings.
Native fees stay in their own asset. Without an explicit valuation adapter the
financial book is incomplete, while protective exits keep the physical quantity.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

from src.models.domain import OrderIntent
from src.services import broker_store as store
from src.services import confirmed_execution as confirmed
from src.services import dex_service, receipt_settlement
from src.shared.errors import SdkError

PENDING = ("submitting", "pending", "uncertain")


def orders(strategy_id, *, pending=False):
    with store.connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM dex_orders WHERE strategy_id=?"
                + (" AND status IN ('submitting','pending','uncertain')" if pending else "")
                + " ORDER BY created_at,id",
                (strategy_id,),
            )
        ]


def reserve(strategy_id, evaluation_id, intent, template, revision):
    key = intent.engine_order_id or f"{evaluation_id}:{intent.engine_position_id}:{intent.action}"
    if intent.action == "exit":
        key += ":" + str(intent.amount)
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status,exit_only_mode,execution_revision,book_revision FROM strategies WHERE id=?", (strategy_id,)
        ).fetchone()
        if not row or not intent.engine_position_id:
            raise SdkError("Live execution requires a confirmed position identity.")
        if intent.action == "enter":
            if conn.execute("SELECT 1 FROM positions WHERE id=?", (intent.engine_position_id,)).fetchone():
                raise SdkError("Entry position identity is already recorded.")
            if row["status"] != "live" or revision is None or tuple(row)[2:] != tuple(revision):
                return None
        elif row["status"] != "live" and row["exit_only_mode"] != "live":
            return None
        pending = conn.execute(
            "SELECT intent_json FROM dex_orders WHERE strategy_id=? AND status IN ('submitting','pending','uncertain')",
            (strategy_id,),
        ).fetchall()
        # Legacy unresolved trade rows without a signed context cannot be retried.
        if conn.execute(
            "SELECT 1 FROM trades WHERE strategy_id=? AND mode='live' AND status='pending' AND id NOT IN (SELECT id FROM dex_orders) LIMIT 1",
            (strategy_id,),
        ).fetchone():
            return None
        for other in pending:
            identity = json.loads(other[0]).get("engine_position_id")
            if not identity or identity == intent.engine_position_id or intent.action == "enter":
                return None
        if intent.action == "exit":
            held = conn.execute(
                "SELECT quantity FROM dex_holdings WHERE position_id=? AND strategy_id=?",
                (intent.engine_position_id, strategy_id),
            ).fetchone()
            if not held or confirmed.decimal(intent.amount) > confirmed.decimal(held[0]):
                raise SdkError("Exit exceeds the confirmed strategy holding.")
        if conn.execute("SELECT 1 FROM dex_orders WHERE strategy_id=? AND intent_key=?", (strategy_id, key)).fetchone():
            return None
        oid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO dex_orders(id,strategy_id,evaluation_id,intent_key,intent_json,template_json,context_json,status,created_at) VALUES (?,?,?,?,?,?,?,'submitting',?)",
            (
                oid,
                strategy_id,
                evaluation_id,
                key,
                intent.model_dump_json(),
                json.dumps(template),
                json.dumps(dict(expected_revision=revision)),
                store.now(),
            ),
        )
        return oid


def update(oid, *, context=None, status=None, tx_hash=None, error=None):
    with store.connection() as conn:
        row = conn.execute("SELECT context_json FROM dex_orders WHERE id=?", (oid,)).fetchone()
        saved = json.loads(row[0])
        if context:
            saved.update(context)
        conn.execute(
            "UPDATE dex_orders SET context_json=?,status=CASE WHEN status='confirmed' THEN status ELSE COALESCE(?,status) END,tx_hash=COALESCE(?,tx_hash),error=? WHERE id=?",
            (json.dumps(saved), status, tx_hash, error, oid),
        )


def token(client, chain_id, value):
    if dex_service._is_address(value):
        return value.lower(), dex_service.resolve_decimals(client, chain_id, value)
    matches = client.dex.token_search(chain_id=chain_id, query=value)
    exact = [m for m in matches or [] if str(getattr(m, "symbol", "")).upper() == value.upper()]
    if len(exact) != 1:
        raise SdkError("Live settlement needs an unambiguous token address.")
    address = getattr(exact[0], "address", None)
    if not dex_service._is_address(address):
        raise SdkError("Token metadata did not return an ERC-20 address.")
    return address.lower(), int(exact[0].decimals)


def _authorize_entry(oid, intent):
    if intent.action != "enter":
        return
    from src.services import portfolio_risk_service

    with store.connection() as conn:
        row = conn.execute(
            "SELECT s.status,s.execution_revision,s.book_revision,o.context_json FROM strategies s JOIN dex_orders o ON o.strategy_id=s.id WHERE o.id=?",
            (oid,),
        ).fetchone()
    expected = json.loads(row["context_json"]).get("expected_revision")
    if row["status"] != "live" or expected is None or list(row)[1:3] != expected:
        raise SdkError("Entry authorization changed while preparing execution.")
    if not portfolio_risk_service.check_before_live_execution()["allowed"]:
        raise SdkError("Portfolio entry authorization is halted.")


def _signed_hash(signed):
    from eth_utils import keccak

    return "0x" + keccak(hexstr=signed).hex()


def _zero_value(payload):
    if int(str(payload.get("value") or "0"), 0) != 0:
        raise SdkError("Native-value transactions require a trace settlement adapter.")


def _broadcast(client, signed, expected_hash, chain_id):
    result = client.dex.broadcast(signed_tx=signed, chain_id=chain_id, venue_id=None)
    reported = getattr(result, "tx_hash", None)
    if reported and reported.lower() != expected_hash.lower():
        raise SdkError("Broadcast response differs from the locally signed transaction hash.")


def execute(oid, intent, wallet_address, chain_id, slippage_pct, max_input_amount):
    from src.services import order_executor as executor
    from src.services import wallet_manager
    from src.shared.clients.mangrove import mangrove_markets_client

    attempted = False

    def broadcast_signed(client, signed, tx_hash):
        nonlocal attempted
        # Lifecycle updates use the same lock: pause either precedes this final
        # check or observes an already submitted transaction with a durable hash.
        from src.services.broker_execution import _lock

        with _lock:
            _authorize_entry(oid, intent)
            attempted = True
            _broadcast(client, signed, tx_hash, chain_id)

    try:
        executor._validate_slippage(slippage_pct)
        wallet_manager.require_backup_confirmed(wallet_address)
        # Every required read capability is tested before the first signature.
        rpc = receipt_rpc(chain_id)
        try:
            if int(rpc("eth_chainId", []), 16) != chain_id:
                raise SdkError("Receipt RPC does not match the signed chain.")
            finalized = rpc("eth_getBlockByNumber", ["finalized", False])
            if not isinstance(finalized, dict) or not finalized.get("hash") or not finalized.get("number"):
                raise SdkError("Finalized receipt evidence is unavailable.")
            rpc("eth_getTransactionReceipt", ["0x" + "00" * 32])
        finally:
            rpc.close()
        client = mangrove_markets_client()
        input_token, output_token, amount = executor._resolve_tokens(intent)
        with store.connection() as conn:
            order = conn.execute("SELECT strategy_id FROM dex_orders WHERE id=?", (oid,)).fetchone()
            capital = conn.execute(
                "SELECT quote_token_address FROM execution_capital WHERE strategy_id=?", (order[0],)
            ).fetchone()
            allocation = conn.execute(
                "SELECT token_address FROM allocations WHERE strategy_id=? AND active=1", (order[0],)
            ).fetchone()
        quote_asset = capital[0] if capital else allocation[0] if allocation else None
        if not quote_asset:
            raise SdkError("A retained quote allocation is required for execution.")
        if intent.action == "enter":
            input_token = quote_asset
        else:
            output_token = quote_asset
        in_address, in_decimals = token(client, chain_id, input_token)
        out_address, out_decimals = token(client, chain_id, output_token)
        if {in_address, out_address} & dex_service._NATIVE_SENTINELS:
            raise SdkError("Native asset settlement requires a trace adapter.")
        if intent.action == "enter" and max_input_amount is not None and amount > max_input_amount:
            raise SdkError("Entry exceeds confirmed available quote cash.")
        context = dict(
            wallet_address=wallet_address,
            chain_id=chain_id,
            input_token_address=in_address,
            output_token_address=out_address,
            input_decimals=in_decimals,
            output_decimals=out_decimals,
            approval_required=False,
        )
        update(oid, context=context)
        quote = dex_service.get_quote(
            input_token=in_address, output_token=out_address, amount=amount, chain_id=chain_id
        )
        swap = client.dex.prepare_swap(
            quote_id=quote["quote_id"], wallet_address=wallet_address, slippage=slippage_pct * 100
        )
        payload = getattr(swap, "payload", {})
        _zero_value(payload)
        wallet_manager._validate_sign_target(wallet_manager._normalize_payload(payload, chain_id=chain_id))
        approval = client.dex.approve_token(token_address=in_address, chain_id=chain_id, wallet_address=wallet_address)
        if approval is not None:
            approval_payload = getattr(approval, "payload", {})
            _zero_value(approval_payload)
            wallet_manager._validate_sign_target(wallet_manager._normalize_payload(approval_payload, chain_id=chain_id))
            update(oid, context=dict(approval_required=True, approval_recipient=approval_payload.get("to")))
        update(oid, context=dict(router_address=payload.get("to"), signed_native_value="0"))
        if approval is not None:
            _authorize_entry(oid, intent)
            signed = executor.wallet_sign(approval_payload, wallet_address, chain_id=chain_id)
            approval_hash = _signed_hash(signed)
            update(oid, context=dict(approval_tx_hash=approval_hash))
            broadcast_signed(client, signed, approval_hash)
            if executor._poll_tx(approval_hash, chain_id)["status"] not in ("confirmed", "success"):
                raise SdkError("Approval outcome is unresolved; no swap submitted.")
            # Refresh the nonce after a mined approval; validate the refreshed
            # unsigned transaction before the swap signature as well.
            swap = client.dex.prepare_swap(
                quote_id=quote["quote_id"], wallet_address=wallet_address, slippage=slippage_pct * 100
            )
            payload = getattr(swap, "payload", {})
            _zero_value(payload)
            wallet_manager._validate_sign_target(wallet_manager._normalize_payload(payload, chain_id=chain_id))
            update(oid, context=dict(router_address=payload.get("to"), signed_native_value="0"))
        _authorize_entry(oid, intent)
        signed = executor.wallet_sign(payload, wallet_address, chain_id=chain_id)
        tx_hash = _signed_hash(signed)
        update(oid, status="pending", tx_hash=tx_hash)
        broadcast_signed(client, signed, tx_hash)
        reconcile_one(oid)
    except BaseException as exc:
        update(
            oid,
            status="uncertain" if attempted else "failed",
            context={"no_broadcast_confirmed": True} if not attempted else None,
            error=str(exc),
        )
        raise


def receipt_rpc(chain_id):
    from src.services.payment_reconciliation_worker import ReadOnlyRPC, configured_urls

    url = configured_urls().get(f"eip155:{chain_id}")
    if not url:
        raise SdkError("No read-only receipt RPC configured for the signed chain.")
    return ReadOnlyRPC(url)


def finalized_receipt(rpc, tx_hash):
    raw = rpc("eth_getTransactionReceipt", [tx_hash])
    if not raw:
        return None
    final = rpc("eth_getBlockByNumber", ["finalized", False])
    if int(raw["blockNumber"], 16) > int(final["number"], 16):
        return None
    block = rpc("eth_getBlockByNumber", [raw["blockNumber"], False])
    if block["hash"].lower() != raw["blockHash"].lower():
        raise SdkError("Receipt is not in the canonical finalized chain.")
    return raw


def reconcile(strategy_id):
    for order in orders(strategy_id, pending=True):
        try:
            reconcile_one(order["id"])
        except Exception as exc:
            update(order["id"], error=str(exc))


def reconcile_one(oid):
    with store.connection() as conn:
        order = dict(conn.execute("SELECT * FROM dex_orders WHERE id=?", (oid,)).fetchone())
    if order["status"] in {"confirmed", "fee_only", "failed"}:
        return
    context = json.loads(order["context_json"])
    if not order["tx_hash"] and not context.get("approval_tx_hash"):
        return
    rpc = receipt_rpc(context["chain_id"])
    try:
        chain = int(rpc("eth_chainId", []), 16)
        if not order["tx_hash"]:
            approval = receipt_settlement.verify_fee_receipt(
                finalized_receipt(rpc, context["approval_tx_hash"]),
                expected_tx_hash=context["approval_tx_hash"],
                expected_chain_id=context["chain_id"],
                reported_chain_id=chain,
                wallet_address=context["wallet_address"],
                recipient_address=context["approval_recipient"],
            )
            complete = (
                context.get("signed_native_value") == "0"
                and approval["native_fee_complete"]
                and confirmed.decimal(approval["native_fee_amount"]) == 0
            )
            _fee_only(oid, dict(approval_settlement=approval, financial_complete=complete))
            return
        raw = finalized_receipt(rpc, order["tx_hash"])
        if raw is None:
            return
        args = {
            key: context[key]
            for key in (
                "wallet_address",
                "router_address",
                "input_token_address",
                "output_token_address",
                "input_decimals",
                "output_decimals",
            )
        }
        approval = _approval_evidence(rpc, context, chain)
        try:
            verified = receipt_settlement.verify_erc20_receipt(
                raw,
                expected_tx_hash=order["tx_hash"],
                expected_chain_id=context["chain_id"],
                reported_chain_id=chain,
                **args,
            )
        except receipt_settlement.ReceiptSettlementError as exc:
            if exc.code != "transaction_reverted":
                raise
            # A finalized revert has fees but cannot produce an asset fill.
            fees = [exc.details] + ([approval] if approval else [])
            complete = context.get("signed_native_value") == "0" and all(
                r["native_fee_complete"] and confirmed.decimal(r["native_fee_amount"]) == 0 for r in fees
            )
            _fee_only(oid, dict(settlement=exc.details, approval_settlement=approval, financial_complete=complete))
            return
        block = rpc("eth_getBlockByNumber", [raw["blockNumber"], False])
        booked_at = store.now()
        from datetime import datetime, timezone

        settled_at = datetime.fromtimestamp(int(block["timestamp"], 16), timezone.utc).isoformat()
        apply_fill(order, verified, approval, settled_at, booked_at)
    finally:
        rpc.close()


def _fee_only(oid, context):
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT strategy_id,context_json,status FROM dex_orders WHERE id=?", (oid,)).fetchone()
        if row["status"] == "fee_only":
            return
        saved = json.loads(row["context_json"])
        saved.update(context)
        conn.execute(
            "UPDATE dex_orders SET status='fee_only',context_json=?,error=NULL WHERE id=?", (json.dumps(saved), oid)
        )
        conn.execute("UPDATE strategies SET book_revision=book_revision+1 WHERE id=?", (row["strategy_id"],))


def _approval_evidence(rpc, context, chain):
    if not context.get("approval_required"):
        return None
    try:
        return receipt_settlement.verify_fee_receipt(
            finalized_receipt(rpc, context["approval_tx_hash"]),
            expected_tx_hash=context["approval_tx_hash"],
            expected_chain_id=context["chain_id"],
            reported_chain_id=chain,
            wallet_address=context["wallet_address"],
            recipient_address=context["approval_recipient"],
        )
    except Exception as exc:
        # Missing fee evidence cannot erase independently proved token moves.
        return dict(
            native_fee_complete=False,
            native_fee_amount=None,
            native_fee_issues=["approval_fee_unresolved"],
            error=str(exc),
        )


def apply_fill(order, verified, approval, settled_at, booked_at):
    intent = OrderIntent.model_validate_json(order["intent_json"])
    context = json.loads(order["context_json"])
    spent, received = confirmed.decimal(verified["input_amount"]), confirmed.decimal(verified["output_amount"])
    buy = intent.action == "enter"
    qty, cash = (received, spent) if buy else (spent, received)
    # No native-to-quote converter exists. Only proven zero native fees need no
    # valuation; any nonzero/missing fee explicitly holds financial ingestion.
    receipts = [verified] + ([approval] if approval else [])
    complete = context.get("signed_native_value") == "0" and all(
        r["native_fee_complete"] and confirmed.decimal(r["native_fee_amount"]) == 0 for r in receipts
    )
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT status FROM dex_orders WHERE id=?", (order["id"],)).fetchone()
        if current[0] == "confirmed":
            return
        held = conn.execute(
            "SELECT * FROM dex_holdings WHERE position_id=? AND strategy_id=?",
            (intent.engine_position_id, order["strategy_id"]),
        ).fetchone()
        oldqty = confirmed.decimal(held["quantity"]) if held else Decimal(0)
        oldcost = confirmed.decimal(held["unit_cost"]) if held else Decimal(0)
        if (buy and held) or (not buy and (not held or qty > oldqty)):
            raise SdkError("Receipt quantity violates the confirmed position book.")
        complete = complete and (buy or bool(held["financial_complete"]))
        fees = dict(
            settlement=verified,
            approval_settlement=approval,
            financial_complete=complete,
            financial_incomplete_reason=None if complete else "native_fee_quote_valuation_unavailable",
        )
        pnl = float(cash - qty * oldcost) if not buy and complete else None
        conn.execute(
            """INSERT INTO trades(id,strategy_id,evaluation_id,order_intent_json,mode,tx_hash,input_token,input_amount,
          output_token,output_amount,fill_price,fees_json,status,executed_at,confirmed_at,p_and_l,execution_context_json)
          VALUES (?,?,?,?,'live',?,?,?,?,?,?,?,'confirmed',?,?,?,?)""",
            (
                order["id"],
                order["strategy_id"],
                order["evaluation_id"],
                order["intent_json"],
                order["tx_hash"],
                context["input_token_address"],
                float(spent),
                context["output_token_address"],
                float(received),
                float(cash / qty),
                json.dumps(fees),
                settled_at,
                settled_at,
                pnl,
                json.dumps(dict(booked_at=booked_at)),
            ),
        )
        if buy:
            conn.execute(
                """INSERT INTO positions(id,strategy_id,asset,entry_trade_id,entry_price,entry_amount,entry_time,status,stop_loss,take_profit)
             VALUES (?,?,?,?,?,?,?,'open',?,?)""",
                (
                    intent.engine_position_id,
                    order["strategy_id"],
                    intent.symbol,
                    order["id"],
                    float(cash / qty),
                    float(qty),
                    settled_at,
                    intent.stop_loss,
                    intent.take_profit,
                ),
            )
            conn.execute(
                "INSERT INTO dex_holdings VALUES (?,?,?,?,?)",
                (intent.engine_position_id, order["strategy_id"], str(qty), str(cash / qty), int(complete)),
            )
        else:
            remaining = oldqty - qty
            conn.execute(
                "UPDATE dex_holdings SET quantity=?,financial_complete=? WHERE position_id=?",
                (str(remaining), int(complete), intent.engine_position_id),
            )
            conn.execute(
                """UPDATE positions SET entry_amount=?,exit_trade_id=?,exit_amount=COALESCE(exit_amount,0)+?,exit_price=?,exit_time=?,status=? WHERE id=?""",
                (
                    float(remaining),
                    order["id"],
                    float(qty),
                    float(cash / qty),
                    settled_at,
                    "closed" if remaining == 0 else "open",
                    intent.engine_position_id,
                ),
            )
        context.update(
            settlement=verified, approval_settlement=approval, financial_complete=complete, booked_at=booked_at
        )
        conn.execute(
            "UPDATE dex_orders SET status='confirmed',context_json=?,error=NULL WHERE id=?",
            (json.dumps(context), order["id"]),
        )
        conn.execute("UPDATE strategies SET book_revision=book_revision+1 WHERE id=?", (order["strategy_id"],))
