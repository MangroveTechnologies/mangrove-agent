"""Pinned Markets ownership-v1 contract; never an arbitrary message signer."""
from __future__ import annotations

import hashlib
import json
import re
import time

from src.shared.errors import SigningError

PREFIX = "MangroveMarkets marketplace action authorization v1\n"


def canonical(value: dict) -> str:
    """Encode the ownership-v1 canonical representation."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def contract_digest(tool) -> str:
    return hashlib.sha256(canonical(tool.model_dump(mode="json", by_alias=True, exclude_none=True)).encode()).hexdigest()


def normalize(operation: str, arguments: dict, wallet: str, chain: str = "base") -> dict:
    from src.services.marketplace_catalog import get_tool_sync
    return normalize_arguments(get_tool_sync(operation), arguments, wallet, chain)


def normalize_arguments(tool, arguments: dict, wallet: str, chain: str = "base") -> dict:
    """Validate server-defined arguments without executing or resolving remote schemas."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError, ValidationError
    from referencing.exceptions import Unresolvable

    try:
        metadata = (tool.meta or {})["mangrove/marketplace"]
        if (metadata.get("version") != 1 or metadata.get("mode") != "ownership"
                or metadata.get("protocol") != "ownership-v1" or chain not in {"base", "xrpl"}):
            raise ValueError
        schema = json.loads(canonical(tool.inputSchema))
        if len(canonical(schema).encode()) > 65536:
            raise ValueError
        pending = [schema]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                if "$ref" in node and not node["$ref"].startswith("#/"):
                    raise ValueError
                if any(key in node for key in ("$dynamicRef", "$recursiveRef", "$id")):
                    raise ValueError
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
        properties = schema["properties"]
        actor = metadata["actor_field"]
        if not isinstance(actor, str) or actor not in properties or "ownership_proof" in arguments:
            raise ValueError
        supplied = json.loads(canonical(arguments))
        if actor in supplied and not same_wallet(supplied[actor], wallet, chain):
            raise ValueError
        supplied[actor] = wallet
        for field, value in metadata.get("wallet_defaults", {}).get(chain, {}).items():
            if field not in properties:
                raise ValueError
            supplied.setdefault(field, value)
        for field, value in metadata.get("prepare_constraints", {}).items():
            if field not in properties or (field in supplied and supplied[field] != value):
                raise ValueError
            supplied[field] = value
        for field, definition in properties.items():
            if field != "ownership_proof" and "default" in definition:
                supplied.setdefault(field, definition["default"])
        properties.pop("ownership_proof", None)
        schema["required"] = [field for field in schema.get("required", []) if field != "ownership_proof"]
        schema["additionalProperties"] = False
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(supplied)
        supplied = _canonical_numbers(schema, supplied, schema)
        if len(canonical(supplied).encode()) > 65536:
            raise ValueError
        return supplied
    except (KeyError, TypeError, ValueError, AttributeError, SchemaError, ValidationError, RecursionError, Unresolvable, OverflowError):
        raise SigningError("Unsupported or invalid marketplace action arguments.") from None


def _canonical_numbers(schema: dict, value, root: dict, depth: int = 0):
    if depth > 32:
        raise ValueError
    if "$ref" in schema:
        target = root
        for segment in schema["$ref"][2:].split("/"):
            target = target[segment.replace("~1", "/").replace("~0", "~")]
        return _canonical_numbers(target, value, root, depth + 1)
    if type(value) in {int, float} and (schema.get("type") == "integer" or any(
        branch.get("type") == "integer" for branch in schema.get("anyOf", [])
    )):
        converted = int(value)
        if converted != value:
            raise ValueError
        return converted
    if type(value) in {int, float} and (schema.get("type") == "number" or any(
        branch.get("type") == "number" for branch in schema.get("anyOf", [])
    )):
        converted = float(value)
        if converted != value:
            raise ValueError
        return converted
    if isinstance(value, dict):
        return {key: _canonical_numbers(schema.get("properties", {}).get(key, {}), item, root, depth + 1)
                for key, item in value.items()}
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [_canonical_numbers(schema["items"], item, root, depth + 1) for item in value]
    return value


def same_wallet(address: str, wallet: str, chain: str) -> bool:
    return address == wallet if chain == "xrpl" else address.lower() == wallet.lower()


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError


def validate_challenge(challenge: dict, *, operation: str, arguments: dict,
                       wallet: str, audience: str, identity: dict, network: str, chain: str = "base") -> str:
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
        _require(identity["audience"] == "mangrovemarkets" and identity["auth_method"] in {"api_key", "wallet"})
        if identity["auth_method"] == "wallet":
            subject = wallet if chain == "xrpl" else wallet.lower()
            _require(identity["user_id"] == f"wallet:{network}:{subject}" and identity["org_id"] is None)
            _require(identity["permissions"] == [])
        else:
            _require(all(isinstance(identity[k], str) and identity[k].strip() for k in ("user_id", "org_id")))
        _require(isinstance(identity["permissions"], list))
        _require(all(isinstance(permission, str) for permission in identity["permissions"]))
        normalized = arguments
        expected = {
            "version": 1, "audience": audience,
            "user_id": identity["user_id"], "org_id": identity["org_id"],
            "credential_type": identity["auth_method"], "operation": operation,
            "arguments_sha256": hashlib.sha256(canonical(normalized).encode()).hexdigest(),
            "chain": chain, "network": network, "address": address, **proof,
        }
        message = PREFIX + canonical(expected)
        _require(challenge["authorization"] == message)
        return message
    except (KeyError, TypeError, ValueError):
        raise SigningError("Marketplace challenge does not match the approved action or identity.") from None
