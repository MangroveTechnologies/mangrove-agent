"""dex_service — shared DEX quote logic for routes + MCP tools.

Why this module exists
----------------------
The MangroveMarkets backend (and the 1inch aggregator it proxies) expect a
swap ``amount`` in the input token's **smallest units** — base units / wei —
and return ``input_amount`` / ``output_amount`` the same way. The
``mangrovemarkets`` SDK documents this in every example::

    amount=1_000_000   # == 1 USDC (6 decimals)
    amount=100_000     # == 0.1 USDC
    # quote.output_amount is wei

The agent, by contrast, works in **human-readable** token quantities
everywhere else: ``get_balances`` converts to human units, ``_paper_fill``
multiplies ``intent.amount`` by a mark price, and ``wallet-presentation.md``
states the convention outright ("Convert raw amounts to human-readable
units"). The LLM driving the tools — and the tester who filed this bug —
naturally pass ``0.001`` meaning *0.001 ETH*.

Passing that human float straight through to ``dex.get_quote`` made the
backend read ``0.001`` as 0.001 **base units** — sub-wei dust for an
18-decimal token — so 1inch found no route and returned
``INSUFFICIENT_LIQUIDITY`` for **every** pair/chain/amount (0.0001 .. 1.0 all
collapse to dust in base units). That is the reported blocker.

This module is the single boundary that converts human <-> base units, so
the agent keeps its human-amount convention end-to-end while the backend
always receives the base units it expects.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from src.shared.clients.mangrove import mangrove_markets_client
from src.shared.errors import SdkError, ValidationError, WalletInputBalanceEmpty
from src.shared.logging import get_logger

_log = get_logger(__name__)

# 1inch's native-asset sentinels (ETH on mainnet/Base, etc.) — never an
# ERC-20 contract, always 18 decimals, so resolve offline.
_NATIVE_SENTINELS = {
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "0x0000000000000000000000000000000000000000",
}
_NATIVE_DECIMALS = 18


def _is_address(token: str) -> bool:
    return isinstance(token, str) and token.startswith("0x") and len(token) == 42


def resolve_token_address(client: Any, chain_id: int, token: str) -> str:
    """Resolve the original requested asset, refusing ambiguous symbol matches."""
    from src.services.transaction_intent import NATIVE_TOKEN, ZERO_ADDRESS, address

    if _is_address(token):
        result = address(token)
        return NATIVE_TOKEN if result == ZERO_ADDRESS else result
    try:
        matches = client.dex.token_search(chain_id=chain_id, query=token)
    except Exception:
        raise SdkError("Could not resolve the requested token address.") from None
    exact = [m for m in matches or [] if str(getattr(m, "symbol", "")).upper() == token.upper()]
    if len(exact) != 1:
        raise SdkError("Live signing requires one exact token-symbol match; use the contract address.")
    return address(getattr(exact[0], "address", None))


def resolve_decimals(client: Any, chain_id: int, token: str) -> int:
    """Return the ERC-20 decimals for ``token`` on ``chain_id``.

    Resolution order:
    1. Native-asset sentinel -> 18 (offline, no network call).
    2. 0x address -> ``dex.token_info(...).decimals``.
    3. Symbol -> first exact ``dex.token_search(...)`` match's decimals.

    Raises ``SdkError`` if decimals cannot be determined. We refuse to
    guess: a wrong decimals silently mis-scales the amount (e.g. treating
    6-decimal USDC as 18 would request 10^12x too much), which is far worse
    than a clear error.
    """
    t = (token or "").strip()
    if not t:
        raise ValidationError("token is required to resolve decimals.")
    if t.lower() in _NATIVE_SENTINELS:
        return _NATIVE_DECIMALS

    if _is_address(t):
        # We need ONLY decimals here. The SDK's strongly-typed TokenInfo can
        # reject an otherwise-fine response over an unrelated field — e.g. the
        # live markets server returns `tags` as {provider, value} objects while
        # the SDK models `tags: list[str]` (root cause fixed upstream in the
        # MangroveMarkets SDK). A swap must not fail because a metadata field's
        # schema drifted, so if the typed lookup fails we fall back to the raw
        # tool payload for the decimals field only.
        dec: Any = None
        try:
            info = client.dex.token_info(chain_id=chain_id, address=t)
            dec = getattr(info, "decimals", None)
        except Exception as e:  # noqa: BLE001
            try:
                raw = client.dex._call_tool(
                    "oneinch_token_info", {"chain_id": chain_id, "address": t}
                )
                tok = raw.get("token", raw) if isinstance(raw, dict) else {}
                dec = tok.get("decimals")
            except Exception:  # noqa: BLE001
                dec = None
            if dec is None:
                raise SdkError(
                    f"Could not look up token decimals for {t} on chain {chain_id}: {e}",
                    suggestion=(
                        "The amount must be converted to the token's smallest "
                        "units, which needs its decimals. Verify the token "
                        "address + chain_id are correct."
                    ),
                ) from e
            _log.warning(
                "token_info model rejected the response for %s on chain %s "
                "(%s); fell back to raw decimals=%s",
                t, chain_id, e, dec,
            )
        if dec is None:
            raise SdkError(f"token_info for {t} returned no decimals.")
        return int(dec)

    # Symbol path (e.g. "USDC"). Best-effort via token_search.
    try:
        matches = client.dex.token_search(chain_id=chain_id, query=t)
    except Exception as e:  # noqa: BLE001
        raise SdkError(
            f"Could not resolve token symbol '{t}' on chain {chain_id}: {e}",
            suggestion="Pass the token's contract address instead of its symbol.",
        ) from e
    for m in matches or []:
        if str(getattr(m, "symbol", "")).upper() == t.upper():
            return int(m.decimals)
    if matches:
        return int(matches[0].decimals)
    raise SdkError(
        f"Unknown token '{t}' on chain {chain_id}; cannot determine decimals.",
        suggestion="Pass the token's contract address instead of its symbol.",
    )


def to_base_units(amount: float, decimals: int) -> int:
    """Human token amount -> integer base units. Uses Decimal for exactness."""
    if amount is None:
        raise ValidationError("amount is required.")
    if amount < 0:
        raise ValidationError(f"amount must be non-negative, got {amount}.")
    return int(Decimal(str(amount)) * (Decimal(10) ** decimals))


def float_exact_base_units(raw: int) -> int:
    """Return the largest integer <= ``raw`` that a float represents exactly.

    MangroveMarkets types swap and approval amounts as float and builds the
    1inch request from ``int(float(amount))``. Integers above 2**53 lose
    low-order bits on that path, and float rounding can move them up. The
    returned value is unchanged by that conversion, so the quote, the approval
    and the signed intent all use one amount that is never above the request.
    """
    if type(raw) is not int or raw < 0:
        raise ValidationError("Base-unit amount must be a non-negative integer.")
    try:
        nearest = float(raw)
    except OverflowError:
        raise ValidationError("Base-unit amount exceeds the transport range.") from None
    if int(nearest) > raw:
        nearest = math.nextafter(nearest, 0.0)
    return int(nearest)


def swap_base_units(amount: float | Decimal, decimals: int) -> int:
    """Human input amount -> the exact base units quoted, approved and signed.

    Raises ``ValidationError`` when the amount rounds to 0 base units.
    """
    raw = float_exact_base_units(to_base_units(amount, decimals))
    if raw <= 0:
        raise ValidationError(
            f"amount {amount} is too small to quote: it rounds to 0 base "
            f"units for a {decimals}-decimal token.",
            suggestion="Increase the amount.",
        )
    return raw


def _exact_number(value: Any) -> Decimal:
    """Exact Decimal value of a JSON number; a float keeps all of its bits."""
    if isinstance(value, bool):
        raise ValueError("boolean is not an amount")
    return Decimal(value) if isinstance(value, float) else Decimal(str(value))


def from_base_units(raw: float | int | str, decimals: int) -> float:
    """Integer base units -> human token amount."""
    return float(Decimal(str(raw)) / (Decimal(10) ** decimals))


def get_quote(
    input_token: str,
    output_token: str,
    amount: float,
    chain_id: int,
    venue_id: str | None = None,
    mode: str | None = None,
) -> dict:
    """Get a DEX swap quote, in human token units in *and* out.

    ``amount`` is a human-readable quantity of ``input_token`` (e.g. 0.001
    ETH, 25 USDC). It is converted to the token's base units before the
    backend call; the returned ``input_amount`` / ``output_amount`` are
    converted back to human units. The raw base-unit values are preserved
    as exact integers in ``*_base_units`` for callers that need them.

    The requested base units are ``swap_base_units(amount, decimals)``, the
    largest float-exact value not above the request, and the quote must echo
    exactly that value. Callers approve and sign ``input_amount_base_units``.
    """
    client = mangrove_markets_client()

    in_decimals = resolve_decimals(client, chain_id, input_token)
    raw_amount = swap_base_units(amount, in_decimals)

    kwargs: dict[str, Any] = {
        "input_token": input_token,
        "output_token": output_token,
        "amount": raw_amount,
        "chain_id": chain_id,
        "venue_id": venue_id,
    }
    if mode is not None:
        kwargs["mode"] = mode

    try:
        quote = client.dex.get_quote(**kwargs)
    except Exception as e:  # noqa: BLE001
        raise SdkError(f"dex.get_quote failed: {e}") from e

    out_decimals = resolve_decimals(client, chain_id, output_token)

    data = quote.model_dump() if hasattr(quote, "model_dump") else dict(quote)
    raw_in = data.get("input_amount")
    raw_out = data.get("output_amount")
    # A quote may supply a price; it cannot replace the requested operation.
    if raw_in is None:
        raise SdkError("DEX quote input amount differs from the requested amount.")
    try:
        exact_in = _exact_number(raw_in)
        exact_out = None if raw_out is None else _exact_number(raw_out)
    except (ValueError, ArithmeticError, TypeError):
        raise SdkError("DEX quote amounts are not numbers.") from None
    if exact_in != raw_amount:
        raise SdkError("DEX quote input amount differs from the requested amount.")
    if exact_out is not None and exact_out.is_finite() and exact_out == exact_out.to_integral_value():
        raw_out = int(exact_out)
    if data.get("chain_id") is not None and data["chain_id"] != chain_id:
        raise SdkError("DEX quote chain differs from the requested chain.")
    for field, requested in (("src_token_address", input_token), ("dst_token_address", output_token)):
        if data.get(field) and _is_address(requested) and str(data[field]).lower() != requested.lower():
            raise SdkError("DEX quote asset differs from the requested token.")
    data["input_amount_base_units"] = raw_amount
    data["input_amount"] = from_base_units(raw_amount, in_decimals)
    if raw_out is not None:
        data["output_amount_base_units"] = raw_out
        data["output_amount"] = from_base_units(raw_out, out_decimals)
        from src.services.transaction_intent import ONEINCH_ROUTING_FEE_BPS, quote_after_max_routing_fee

        # Current Markets quotes are gross of the fixed partner fee added by
        # prepare_swap. Expose that separate allowance without relabeling it
        # as price slippage or reporting it as a measured execution fee.
        data["routing_fee_cap_bps"] = ONEINCH_ROUTING_FEE_BPS
        net_floor = quote_after_max_routing_fee(
            raw_out, ONEINCH_ROUTING_FEE_BPS,
        )
        data["output_amount_after_max_routing_fee_base_units"] = net_floor
        data["output_amount_after_max_routing_fee"] = from_base_units(net_floor, out_decimals)
    data["input_token_decimals"] = in_decimals
    data["output_token_decimals"] = out_decimals

    _log.info(
        "dex.quote",
        input_token=input_token,
        output_token=output_token,
        amount=amount,
        amount_base_units=raw_amount,
        in_decimals=in_decimals,
        out_decimals=out_decimals,
        quote_id=data.get("quote_id"),
    )
    return data


def require_input_balance(client: Any, input_token: str, chain_id: int, wallet_address: str) -> None:
    """Refuse a live swap whose wallet holds zero of the token it would spend.

    Server-side replacement for the zero-balance check that used to live only
    in the Claude Code hook ``.claude/hooks/preflight-swap.sh``, so every MCP
    and REST client gets it. Runs before the quote and before any approval is
    signed, so an unfundable swap never spends gas on an approval.

    Fails open, like the hook: an unreadable balance, a token missing from the
    tracked set, or a symbol other than USDC that cannot be mapped to an
    address offline lets the swap continue to its own SDK errors. Only an
    explicit zero blocks.
    """
    from src.services.wallet_manager import canonical_usdc_addresses

    token = input_token.strip()
    if token.upper() == "USDC":
        token = canonical_usdc_addresses().get(chain_id, "")
    if not _is_address(token):
        return
    try:
        balances = getattr(client.dex.balances(chain_id=chain_id, wallet=wallet_address), "balances", None)
    except Exception as exc:  # noqa: BLE001 - fail open; the swap surfaces its own error
        _log.warning("dex.preflight.balance_unavailable", chain_id=chain_id, error_type=type(exc).__name__)
        return
    if not isinstance(balances, dict):
        return
    target = token.lower()
    raw = next((value for key, value in balances.items() if str(key).lower() == target), None)
    try:
        empty = raw is not None and Decimal(str(raw)) == 0
    except ArithmeticError:
        return
    if empty:
        raise WalletInputBalanceEmpty(
            f"Wallet {wallet_address} holds 0 of input token {input_token} on chain_id {chain_id}. "
            "No quote was requested and nothing was signed.",
            suggestion=(
                "Confirm the deposit landed on this chain (the same address exists on every EVM "
                "chain), then retry. On Base Sepolia (84532), fund the wallet from a faucet first."
            ),
        )
