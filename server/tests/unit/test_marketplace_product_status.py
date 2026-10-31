"""Product copy is visible without invalidating funded approval contracts."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from mcp.types import Tool, ToolAnnotations
from src.services.marketplace_authorization import contract_digest
from src.services.marketplace_status import MESSAGE, product_status

from tests.unit.test_marketplace_discovery import discovery as discovery

STATUS = {"version": 1, "product": "agent_to_agent_marketplace",
          "availability": "under_construction", "message": MESSAGE}
CONTRACTS = json.loads(Path(__file__).with_name("marketplace_contract_fixture.json").read_text())


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_status_copy_preserves_existing_approval_digest(name):
    old = Tool.model_validate(deepcopy(CONTRACTS[name]))
    current = old.model_copy(update={
        "description": MESSAGE + "\n\n" + (old.description or ""),
        "meta": {**deepcopy(old.meta or {}), "mangrove/product": STATUS},
    }, deep=True)
    assert contract_digest(current) == contract_digest(old)


@pytest.mark.parametrize("change", ["arguments", "ownership", "description", "annotations"])
def test_business_contract_changes_remain_digest_sensitive(change):
    old = Tool.model_validate(deepcopy(CONTRACTS["marketplace_create_listing"]))
    current = old.model_copy(update={
        "description": MESSAGE + "\n\n" + (old.description or ""),
        "meta": {**deepcopy(old.meta or {}), "mangrove/product": deepcopy(STATUS)},
    }, deep=True)
    if change == "arguments":
        current.inputSchema["properties"]["title"]["minLength"] = 10
    elif change == "ownership":
        current.meta["mangrove/marketplace"]["protocol"] = "changed"
    elif change == "description":
        current.description += " Changed transaction destination."
    else:
        current.annotations = ToolAnnotations(destructiveHint=True)
    assert contract_digest(current) != contract_digest(old)


@pytest.mark.parametrize("change", ["extra_product_field", "missing_prefix", "missing_metadata",
                                    "changed_prefix", "changed_status"])
def test_normalization_requires_the_exact_paired_presentation_change(change):
    old = Tool.model_validate(deepcopy(CONTRACTS["marketplace_create_listing"]))
    current = old.model_copy(update={
        "description": MESSAGE + "\n\n" + (old.description or ""),
        "meta": {**deepcopy(old.meta or {}), "mangrove/product": deepcopy(STATUS)},
    }, deep=True)
    if change == "extra_product_field":
        current.meta["mangrove/product"]["permission"] = "changed"
    elif change == "missing_prefix":
        current.description = old.description
    elif change == "missing_metadata":
        current.meta.pop("mangrove/product")
    elif change == "changed_prefix":
        current.description = "A different construction note.\n\n" + (old.description or "")
    else:
        current.meta["mangrove/product"]["availability"] = "ready"
    assert contract_digest(current) != contract_digest(old)


@pytest.mark.parametrize("field,value", [("version", True), ("version", 2),
    ("product", "strategy_marketplace"), ("availability", "ready"),
    ("message", "Arbitrary upstream instructions")])
def test_unknown_product_declarations_are_not_interpreted(field, value):
    assert product_status({"marketplace": {**STATUS, field: value}}) is None


def test_ready_ownership_does_not_claim_finished_product(discovery):
    from src.services import marketplace

    discovery["document"]["marketplace"] = STATUS
    result = marketplace.readiness()
    assert result["ownership_discovery"] == "ready"
    assert result["marketplace"] == STATUS
    # Product presentation is excluded from approval-bound connection settings.
    assert "marketplace" not in marketplace._settings()


def test_declared_not_ready_preserves_server_diagnostic_and_stops_before_wallet(discovery, monkeypatch):
    from src.services import marketplace

    discovery["status"] = 503
    discovery["document"] = {"error": True, "code": "OWNERSHIP_UNAVAILABLE",
                             "message": "Marketplace ownership is not configured", "marketplace": STATUS}
    result = marketplace.readiness()
    assert result["ownership_discovery"] == "unavailable"
    assert result["marketplace"] == STATUS
    assert result["diagnostic"] == {"code": "OWNERSHIP_UNAVAILABLE",
                                    "message": "Marketplace ownership is not configured", "http_status": 503}
    monkeypatch.setattr(marketplace, "_wallet", lambda *args: pytest.fail("Not-ready discovery reached wallet"))
    with pytest.raises(marketplace.MarketplaceReadinessError) as error:
        marketplace.prepare("marketplace_create_listing", {}, "fixture-wallet")
    assert error.value.to_dict()["code"] == "MARKETPLACE_UNAVAILABLE"
    assert error.value.to_dict()["diagnostic"] == result["diagnostic"]


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_auth_and_service_failures_are_not_relabelled(discovery, status):
    from src.services import marketplace

    discovery["status"] = status
    discovery["document"] = {"error": True, "code": "OWNERSHIP_UNAVAILABLE",
                             "message": "Marketplace ownership is not configured", "marketplace": STATUS}
    with pytest.raises(marketplace.MarketplaceError) as error:
        marketplace.readiness()
    assert not isinstance(error.value, marketplace.MarketplaceReadinessError)
    assert "marketplace" not in error.value.to_dict()


def test_unrelated_503_is_not_relabelled(discovery):
    from src.services import marketplace

    discovery["status"] = 503
    discovery["document"] = {"error": True, "code": "PAYMENT_STORAGE_UNAVAILABLE",
                             "message": "Payment storage unavailable", "marketplace": STATUS}
    with pytest.raises(marketplace.MarketplaceError) as error:
        marketplace.readiness()
    assert not isinstance(error.value, marketplace.MarketplaceReadinessError)
    assert "marketplace" not in error.value.to_dict()
