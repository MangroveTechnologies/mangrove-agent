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

from decimal import Decimal
from typing import Any

from src.shared.clients.mangrove import mangrove_markets_client
from src.shared.errors import SdkError, ValidationError
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
    as ``*_base_units`` for callers that need them.
    """
    client = mangrove_markets_client()

    in_decimals = resolve_decimals(client, chain_id, input_token)
    raw_amount = to_base_units(amount, in_decimals)
    if raw_amount <= 0:
        raise ValidationError(
            f"amount {amount} is too small to quote: it rounds to 0 base "
            f"units for a {in_decimals}-decimal token.",
            suggestion="Increase the amount.",
        )

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
    if raw_in is not None:
        data["input_amount_base_units"] = raw_in
        data["input_amount"] = from_base_units(raw_in, in_decimals)
    if raw_out is not None:
        data["output_amount_base_units"] = raw_out
        data["output_amount"] = from_base_units(raw_out, out_decimals)
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
