"""Thin SDK adapters shared by local signal routes and MCP tools.

Browse filters belong to the server. Search remains a separate, single-request
workflow with its historical client-side category refinement.
"""
from __future__ import annotations

from math import ceil
from typing import TYPE_CHECKING

from src.services.payment_budget import payment_budget
from src.shared.clients.mangrove import mangrove_ai_client
from src.shared.errors import AgentError, SdkError, ValidationError

if TYPE_CHECKING:
    from mangrove_ai import MangroveAI
    from mangrove_ai._pagination import PaginatedResponse
    from mangrove_ai.models import Signal


def _validate(limit: int, offset: int, search: str | None, regime_direction: str | None, role: str | None) -> None:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValidationError("Signal limit must be between 1 and 1000.")
    if type(offset) is not int or offset < 0:
        raise ValidationError("Signal offset must be a nonnegative integer.")
    if search and (regime_direction is not None or role is not None):
        raise ValidationError("Regime and role filters apply to browsing, not keyword search.")


def _page(client: MangroveAI, *, limit: int, offset: int, category: str | None,
          search: str | None, regime_direction: str | None, role: str | None) -> PaginatedResponse[Signal]:
    if search:
        from mangrove_ai.models import SearchSignalsRequest

        return client.signals.search(SearchSignalsRequest(query=search, limit=limit, offset=offset))
    kwargs = {key: value for key, value in (
        ("category", category), ("regime_direction", regime_direction), ("role", role),
    ) if value is not None}
    return client.signals.list(limit=limit, offset=offset, **kwargs)


def _check_page(page: PaginatedResponse[Signal], offset: int, requested: int) -> None:
    # Missing continuation metadata must not become a plausible incomplete result.
    if not isinstance(getattr(page, "has_more", None), bool):
        raise SdkError("Signal pagination requires the updated MangroveAI SDK.")
    if (page.offset != offset or len(page.items) > requested
            or (page.has_more and (not page.items or type(page.next_offset) is not int
                                  or page.next_offset <= offset))):
        raise SdkError("The upstream signal page has invalid continuation metadata.")


def _items(page: PaginatedResponse[Signal], category: str | None, search: str | None) -> list[dict]:
    items = [item.model_dump() for item in page.items]
    if search and category:
        items = [item for item in items if (item.get("category") or "").lower() == category]
    return items


def list_signals(*, limit: int = 50, offset: int = 0, category: str | None = None,
                 search: str | None = None, regime_direction: str | None = None,
                 role: str | None = None, collect: bool = False, client: MangroveAI | None = None) -> dict:
    """Return one REST page or collect at most ``limit`` records for MCP.

    Until the first response establishes the effective page size, at most one
    request per requested record is allowed. That conservative bound is tightened
    after the first page and never increased. Actual quotes are budgeted by the
    payer, not by cached discovery prices. An error aborts collection, including
    uncertain payments; no automatic fallback or partial-success response occurs.
    """
    _validate(limit, offset, search, regime_direction, role)
    category = (category.strip().lower() or None) if category else None
    try:
        try:
            from mangrove_ai.models import SignalListPage  # noqa: F401
        except ImportError:
            raise SdkError("Signal pagination requires the updated MangroveAI SDK.") from None
        client = client if client is not None else mangrove_ai_client()
        items: list[dict] = []
        max_pages = limit if collect and not search else 1
        requested = limit
        with payment_budget(max_pages) as budget:
            for index in range(max_pages):
                if index >= budget.max_payments:
                    raise SdkError("The signal response exceeded its page budget.")
                page = _page(client, limit=requested, offset=offset, category=category,
                             search=search, regime_direction=regime_direction, role=role)
                _check_page(page, offset, requested)
                items.extend(_items(page, category, search))
                if not collect:
                    result = {"items": items, "total": page.total, "limit": page.limit,
                              "offset": page.offset, "has_more": page.has_more,
                              "next_offset": page.next_offset}
                    metadata = getattr(page, "filter", None)
                    if metadata is not None:
                        result["filter"] = metadata.model_dump()
                    return result
                if search or len(items) >= limit or not page.has_more:
                    return {"items": items, "total": len(items)}
                if index == 0:
                    effective_size = min(page.limit, len(page.items))
                    if effective_size <= 0:
                        raise SdkError("The upstream signal page has an invalid size.")
                    budget.max_payments = 1 + ceil((limit - len(items)) / effective_size)
                requested = min(effective_size, limit - len(items))
                offset = page.next_offset
        raise SdkError("The signal response exceeded its page budget.")
    except AgentError:
        raise
    except Exception:
        raise SdkError("Could not list signals from the upstream service.") from None
