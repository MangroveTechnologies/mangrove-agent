"""Pinned Markets ownership-v1 contract; never an arbitrary message signer."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.shared.errors import SigningError

PREFIX = "MangroveMarkets marketplace action authorization v1\n"


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Listing(Arguments):
    seller_address: str
    title: str
    description: str
    category: str
    price_xrp: float
    listing_type: str = "static"
    currency: Literal["USDC", "XRP", "RLUSD"] = "USDC"
    chain: Literal["base", "xrpl"] = "base"
    fulfillment_type: str = "digital"
    delivery_evidence_schema: dict | None = None
    storage_uri: str | None = None
    content_hash: str | None = None
    subcategory: str | None = None
    tags: list[str] | None = None


class Offer(Arguments):
    listing_id: str
    buyer_address: str
    chain: Literal["base", "xrpl"] = "base"
    currency: Literal["USDC", "XRP", "RLUSD"] = "USDC"
    payment: Literal[""] = ""
    offer_id: Literal[""] = ""


class Accept(Arguments):
    offer_id: str
    seller_address: str
    escrow_sequence: int | None = None


class Delivery(Arguments):
    offer_id: str
    buyer_address: str


class Rating(Arguments):
    offer_id: str
    rater_address: str
    score: int
    comment: str | None = None


MODELS = {
    "marketplace_create_listing": (Listing, "seller_address"),
    "marketplace_make_offer": (Offer, "buyer_address"),
    "marketplace_accept_offer": (Accept, "seller_address"),
    "marketplace_confirm_delivery": (Delivery, "buyer_address"),
    "marketplace_rate": (Rating, "rater_address"),
}


def canonical(value: dict) -> str:
    """Encode the ownership-v1 canonical representation."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalize(operation: str, arguments: dict, wallet: str, chain: str = "base") -> dict:
    """Expand reviewed defaults and bind the actor to the selected local wallet."""
    try:
        model, actor = MODELS[operation]
        supplied = dict(arguments)
        if chain not in {"base", "xrpl"}:
            raise ValueError
        if actor in supplied and not same_wallet(supplied[actor], wallet, chain):
            raise ValueError
        supplied[actor] = wallet
        if model in (Listing, Offer):
            supplied.setdefault("chain", chain)
            supplied.setdefault("currency", "XRP" if chain == "xrpl" else "USDC")
        result = model.model_validate(supplied).model_dump()
        if model in (Listing, Offer) and (result["chain"] != chain or
                (chain, result["currency"]) not in {("base", "USDC"), ("xrpl", "XRP"), ("xrpl", "RLUSD")}):
            raise ValueError
        if model is Accept and result["escrow_sequence"] is not None:
            if chain != "xrpl" or not 0 < result["escrow_sequence"] < 2**32:
                raise ValueError
        if len(canonical(result).encode()) > 65536:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SigningError("Unsupported or invalid marketplace action arguments.") from None


def same_wallet(address: str, wallet: str, chain: str) -> bool:
    return address == wallet if chain == "xrpl" else address.lower() == wallet.lower()


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError


def validate_challenge(challenge: dict, *, operation: str, arguments: dict,
                       wallet: str, audience: str, identity: dict, chain: str = "base") -> str:
    """Reconstruct the entire expected message before allowing key access."""
    try:
        proof = challenge["ownership_proof"]
        _require(set(proof) == {"nonce", "issued_at", "expires_at"})
        _require(re.fullmatch(r"[0-9a-f]{64}", proof["nonce"]))
        _require(type(proof["issued_at"]) is int and type(proof["expires_at"]) is int)
        _require(proof["issued_at"] <= time.time() < proof["expires_at"])
        _require(0 < proof["expires_at"] - proof["issued_at"] <= 300)
        _require(challenge["code"] == "OWNERSHIP_REQUIRED")
        address = challenge["address"]
        _require(chain in {"base", "xrpl"} and challenge["chain"] == chain and isinstance(address, str))
        if chain == "xrpl":
            from xrpl.core.addresscodec import is_valid_classic_address
            _require(is_valid_classic_address(address) and address == wallet)
        else:
            _require(re.fullmatch(r"0x[0-9a-fA-F]{40}", address) is not None and address.lower() == wallet.lower())
        _require(identity["version"] == 1 and type(identity["version"]) is int)
        _require(identity["audience"] == "mangrovemarkets" and identity["auth_method"] == "api_key")
        _require(all(isinstance(identity[k], str) and identity[k].strip() for k in ("user_id", "org_id")))
        _require(isinstance(identity["permissions"], list))
        _require(all(isinstance(permission, str) for permission in identity["permissions"]))
        _require("execution:write" in identity["permissions"])
        normalized = normalize(operation, arguments, wallet, chain)
        expected = {
            "version": 1, "audience": audience,
            "user_id": identity["user_id"], "org_id": identity["org_id"],
            "credential_type": "api_key", "operation": operation,
            "arguments_sha256": hashlib.sha256(canonical(normalized).encode()).hexdigest(),
            "chain": chain, "address": address, **proof,
        }
        message = PREFIX + canonical(expected)
        _require(challenge["authorization"] == message)
        return message
    except (KeyError, TypeError, ValueError):
        raise SigningError("Marketplace challenge does not match the approved action or identity.") from None
