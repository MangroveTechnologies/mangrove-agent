"""Execution-only adapter for the official Robinhood MCP contract.

Mangrove supplies symbols, direction, quantity and exit logic. This adapter
handles broker eligibility, quote precision, order review and order state.
Only long spot crypto and whole-share equities with limit prices are supported.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from src.services import robinhood_client
from src.services.broker_store import decimal
from src.shared.errors import SdkError


class RobinhoodVenue:
    def __init__(self, client, tools):
        self.client, self.tools = client, tools

    async def call(self, name, **args):
        result = await robinhood_client.invoke(self.client, self.tools, name, args)
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
            raise SdkError(f"Invalid Robinhood {name} envelope.")
        return result["data"]

    async def pages(self, name, key, **args):
        result, cursors = [], set()
        for _ in range(100):
            data = await self.call(name, **args)
            result.extend(data.get(key) or [])
            cursor = data.get("next")
            if not cursor:
                return result
            if cursor in cursors:
                break
            cursors.add(cursor)
            args["cursor"] = cursor
        raise SdkError("Robinhood pagination incomplete; execution stopped.")

    async def account(self, expected=None):
        data = await self.call("get_accounts")
        eligible = [a for a in data.get("accounts") or [] if a and a.get("agentic_allowed") is True]
        if len(eligible) != 1:
            raise SdkError("Robinhood must expose exactly one account eligible for this agent.")
        account = eligible[0]
        if (
            (expected and account["account_number"] != expected)
            or account["state"] != "active"
            or account["deactivated"]
            or account["permanently_deactivated"]
        ):
            raise SdkError("Robinhood execution account is unavailable or changed.")
        return account

    async def balance(self, route):
        data = await self.call("get_portfolio", account_number=route["account_number"])
        if data["currency"] != "USD":
            raise SdkError("Robinhood execution requires a USD account.")
        if route["asset_class"] == "crypto":
            value = (data.get("crypto_buying_power") or {}).get("buying_power")
        else:
            value = (data.get("buying_power") or {}).get("unleveraged_buying_power")
        return decimal(value)

    async def quote(self, route, symbol):
        crypto = route["asset_class"] == "crypto"
        args = {"symbols": [symbol + "-USD" if crypto else symbol]}
        if crypto:
            args["rhs_account_number"] = route["rhs_account_number"]
        data = await self.call("get_crypto_quotes" if crypto else "get_equity_quotes", **args)
        rows = data.get("results") or []
        if len(rows) != 1:
            raise SdkError("Robinhood quote is unavailable.")
        quote = rows[0] if crypto else rows[0]["quote"]
        if quote["symbol"] not in ({symbol + "-USD", symbol + "USD"} if crypto else {symbol}):
            raise SdkError("Robinhood quote symbol mismatch.")
        for side in ("bid", "ask"):
            stamp = quote.get(f"{side}_time" if crypto else f"venue_{side}_time") or quote.get("updated_at")
            try:
                age = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                ).total_seconds()
                if not -5 <= age <= 60 or decimal(quote[f"{side}_price"]) <= 0:
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                raise SdkError("Robinhood quote is stale or unavailable.") from None
        if decimal(quote["bid_price"]) > decimal(quote["ask_price"]):
            raise SdkError("Robinhood quote is crossed; execution stopped.")
        return quote

    async def prepare(self, route, intent):
        if intent.input_token_address or intent.output_token_address or not intent.engine_position_id:
            raise SdkError("Broker execution requires a Mangrove position identity, not a wallet swap.")
        qty, reference = decimal(intent.amount), decimal(intent.ref_price)
        if qty <= 0 or reference <= 0 or (intent.action, intent.side) not in {("enter", "buy"), ("exit", "sell")}:
            raise SdkError("Invalid Mangrove long-only order intent.")
        account = await self.account(route["account_number"])
        if account["rhs_account_number"] != route["rhs_account_number"]:
            raise SdkError("Robinhood account mapping changed.")
        crypto = route["asset_class"] == "crypto"
        if crypto:
            if not account.get("rhc_account_number"):
                raise SdkError("Robinhood crypto onboarding is incomplete.")
            pairs = await self.pages("get_currency_pairs", "results")
            pair = next((p for p in pairs if p and p["symbol"] == intent.symbol + "-USD"), None)
            if (
                not pair
                or pair["tradability"] != "tradable"
                or pair["halted"]
                or pair["display_only"]
                or pair["market_orders_only"]
            ):
                raise SdkError("Robinhood crypto pair does not support this limit order.")
            step, increment = decimal(pair["min_order_quantity_increment"]), decimal(pair["min_order_price_increment"])
            if step <= 0 or increment <= 0:
                raise SdkError("Invalid Robinhood order increments.")
            qty = (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step
            if not decimal(pair["min_order_size"]) <= qty <= decimal(pair["max_order_size"]):
                raise SdkError("Mangrove quantity is outside Robinhood pair limits.")
        else:
            local = datetime.now(ZoneInfo("America/New_York"))
            if local.weekday() >= 5 or not (9 * 60 + 30 <= local.hour * 60 + local.minute < 16 * 60):
                raise SdkError("Equity execution waits for regular market hours.")
            data = await self.call(
                "get_equity_tradability", account_number=route["account_number"], symbols=[intent.symbol]
            )
            matches = [p for p in data.get("results") or [] if p["symbol"] == intent.symbol and p["tradeable"]]
            if len(matches) != 1 or matches[0].get("internal_halt_reason"):
                raise SdkError("Robinhood equity is not currently tradable.")
            if qty != qty.to_integral_value():
                raise SdkError("Robinhood equity limit orders require whole shares; Mangrove must size accordingly.")
            increment = Decimal("0.01") if reference >= 1 else Decimal("0.0001")
        quote = await self.quote(route, intent.symbol)
        bound = reference * (
            1 + decimal(route["slippage_pct"]) if intent.side == "buy" else 1 - decimal(route["slippage_pct"])
        )
        rounding = ROUND_FLOOR if intent.side == "buy" else ROUND_CEILING
        limit = (bound / increment).to_integral_value(rounding=rounding) * increment
        current = decimal(quote["ask_price" if intent.side == "buy" else "bid_price"])
        if limit <= 0 or (intent.side == "buy" and current > limit) or (intent.side == "sell" and current < limit):
            raise SdkError("Robinhood market moved beyond Mangrove's configured price tolerance.")
        if qty * limit > decimal(route["max_order_usd"]):
            raise SdkError("Mangrove order exceeds the venue's per-order allocation.")
        if crypto and pair.get("min_order_quote_amount") and qty * limit < decimal(pair["min_order_quote_amount"]):
            raise SdkError("Mangrove order is below Robinhood's minimum notional.")
        args = {
            ("rhs_account_number" if crypto else "account_number"): route[
                "rhs_account_number" if crypto else "account_number"
            ],
            "symbol": intent.symbol,
            "side": intent.side,
            "type": "limit",
            "quantity": str(qty),
            "limit_price": str(limit),
            "time_in_force": "gtc" if crypto else "gfd",
        }
        if not crypto:
            args["market_hours"] = "regular_hours"
        review = await self.call("preview_crypto_order" if crypto else "review_equity_order", **args)
        if review.get("order_checks"):
            raise SdkError("Robinhood pre-trade checks flagged the order; it was not submitted.")
        if review.get("customer_approval_required") is True:
            raise SdkError("Robinhood requires approval for this order; automatic execution is unavailable.")
        fee = decimal(review["estimated_fee"]) if crypto else Decimal(0)
        if fee < 0:
            raise SdkError("Invalid Robinhood fee estimate.")
        return args, qty * limit + fee

    async def submit(self, route, args, ref_id):
        return await self.call(
            "place_crypto_order" if route["asset_class"] == "crypto" else "place_equity_order", **args, ref_id=ref_id
        )

    async def find_order(self, route, order):
        crypto = route["asset_class"] == "crypto"
        args = {
            ("rhs_account_number" if crypto else "account_number"): route[
                "rhs_account_number" if crypto else "account_number"
            ]
        }
        if order["broker_order_id"]:
            args["order_id"] = order["broker_order_id"]
        else:
            args["created_at_gte"] = order["created_at"]
        rows = await self.pages(
            "get_crypto_orders" if crypto else "get_equity_orders", "results" if crypto else "orders", **args
        )
        matches = [
            r
            for r in rows
            if r
            and (
                (order["broker_order_id"] and r["id"] == order["broker_order_id"])
                or r.get("ref_id") in {order["id"], order["approval_id"] or order["id"]}
            )
        ]
        if len(matches) > 1:
            raise SdkError("Multiple Robinhood orders match one local intent.")
        return matches[0] if matches else None

    async def sellable(self, route, symbol):
        if route["asset_class"] == "crypto":
            rows = await self.pages("get_crypto_positions", "results", rhs_account_number=route["rhs_account_number"])
            return sum(
                (
                    decimal(p["quantity"]) - decimal(p["quantity_held_for_sell"])
                    for p in rows
                    if p and p.get("currency", {}).get("code") == symbol
                ),
                Decimal(0),
            )
        rows = await self.pages("get_equity_positions", "positions", account_number=route["account_number"])
        return sum((decimal(p["shares_available_for_sells"]) for p in rows if p and p["symbol"] == symbol), Decimal(0))

    async def approval(self, route, approval_id):
        data = await self.call(
            "get_trade_approvals", account_number=route["account_number"], approval_ids=[approval_id]
        )
        return next((a for a in data.get("approvals") or [] if a and a["approval_id"] == approval_id), None)

    async def cancel(self, route, broker_id):
        crypto = route["asset_class"] == "crypto"
        return await self.call(
            "cancel_crypto_order" if crypto else "cancel_equity_order",
            **{
                ("rhs_account_number" if crypto else "account_number"): route[
                    "rhs_account_number" if crypto else "account_number"
                ],
                "order_id": broker_id,
            },
        )
