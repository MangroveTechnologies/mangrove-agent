"""Local approval and wallet coordination for remote Markets business tools."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from urllib.parse import urlsplit

import anyio
import httpx
from mcp.client.streamable_http import streamablehttp_client

from mcp import ClientSession
from src.config import app_config
from src.shared.clients.mangrove import _api_key
from src.services.marketplace_authorization import contract_digest, normalize_arguments, validate_challenge
from src.services.wallet_manager import _get_wallet_row, sign_marketplace_proof
from src.shared.errors import AgentError, SigningError, UpstreamAccessError, upstream_access_error
from src.shared.x402.mcp_diagnostics import protect_mcp_diagnostics


class MarketplaceError(AgentError):
    code = "MARKETPLACE_UNAVAILABLE"
    http_status = 400


def _url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or "?" in value or "#" in value or any(c.isspace() for c in value)
                or parsed.scheme not in {"http", "https"}
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError
        _ = parsed.port
        return value.rstrip("/")
    except (ValueError, TypeError, AttributeError):
        raise MarketplaceError("Configure a trusted HTTPS endpoint or loopback HTTP endpoint.") from None


def _settings() -> dict:
    key = _api_key(app_config)
    pinned_audience = getattr(app_config, "MARKETPLACE_OWNERSHIP_AUDIENCE", None)
    chain_id = getattr(app_config, "MARKETPLACE_CHAIN_ID", None)
    xrpl_network = getattr(app_config, "MARKETPLACE_XRPL_NETWORK", None)
    if ((chain_id is not None and (type(chain_id) is not int or chain_id not in {8453, 84532}))
            or (xrpl_network is not None and xrpl_network not in {"mainnet", "testnet", "devnet"})):
        raise MarketplaceError("Configure a supported marketplace wallet network.")
    markets = _url(app_config.MANGROVEMARKETS_BASE_URL)
    authority = _url(getattr(app_config, "MANGROVEAI_BASE_URL", None)) if key else None
    ownership = _discover_ownership(markets)
    audience = ownership["audience"]
    if pinned_audience is not None and pinned_audience != audience:
        raise MarketplaceError("Markets ownership identity does not match the configured deployment.")
    discovered_chain_id = ownership["chain_id"]
    if chain_id is not None and chain_id != discovered_chain_id:
        raise MarketplaceError("Markets EVM network does not match the configured deployment.")
    discovered_xrpl = ownership.get("xrpl_network")
    if xrpl_network is not None and xrpl_network != discovered_xrpl:
        raise MarketplaceError("Markets XRPL network does not match the configured deployment.")
    return {
        "markets": markets, "authority": authority,
        "audience": audience, "chain_id": discovered_chain_id,
        **({"xrpl_network": discovered_xrpl} if discovered_xrpl is not None else {}),
    }


def _discover_ownership(markets: str) -> dict:
    try:
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", markets + "/.well-known/mangrove-marketplace",
                               headers={"Accept-Encoding": "identity"}) as response:
                if (response.status_code != 200
                        or response.headers.get("content-encoding", "identity") != "identity"):
                    raise ValueError
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 8192:
                        raise ValueError
                document = json.loads(raw)
        if (not isinstance(document, dict) or type(document.get("version")) is not int
                or document["version"] != 1):
            raise ValueError
        ownership = document.get("ownership")
        if (not isinstance(ownership, dict) or type(ownership.get("version")) is not int
                or ownership["version"] != 1):
            raise ValueError
        audience = ownership.get("audience")
        if (not isinstance(audience, str) or not audience.strip() or len(audience) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in audience)):
            raise ValueError
        if ("xrpl_network" in ownership
                and ownership["xrpl_network"] not in {"mainnet", "testnet", "devnet"}):
            raise ValueError
        chain_id = ownership.get("chain_id")
        if (type(chain_id) is not int or chain_id not in {8453, 84532}
                or ownership.get("evm_network") != f"eip155:{chain_id}"):
            raise ValueError
        return ownership
    except (httpx.HTTPError, ValueError, TypeError):
        raise MarketplaceError("Markets ownership discovery is unavailable or incompatible.") from None


def readiness() -> dict:
    """Check ownership configuration without signing, paying or creating approvals."""
    _settings()
    return {"ownership_discovery": "ready", "wallet_check": "on_selection"}


def _identity(settings: dict, wallet: dict) -> dict:
    if _api_key(app_config):
        return _request("GET", settings["authority"] + "/auth/markets-context")
    network = (f"xrpl:{settings['xrpl_network']}" if wallet["chain"] == "xrpl"
               else f"eip155:{settings['chain_id']}")
    address = wallet["address"] if wallet["chain"] == "xrpl" else wallet["address"].lower()
    return {"version": 1, "audience": "mangrovemarkets", "auth_method": "wallet",
            "user_id": f"wallet:{network}:{address}", "org_id": None, "permissions": []}


def _credential_binding(salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", (_api_key(app_config) or "wallet-only").encode(), salt, 600_000, dklen=32,
    )
    return f"pbkdf2-sha256-600000${salt.hex()}${digest.hex()}"


def _credential_matches(binding: object) -> bool:
    if not isinstance(binding, str):
        return False
    parts = binding.split("$")
    if (len(parts) != 3 or parts[0] != "pbkdf2-sha256-600000"
            or len(parts[1]) != 32 or len(parts[2]) != 64
            or any(c not in "0123456789abcdef" for c in parts[1] + parts[2])):
        return False
    return hmac.compare_digest(binding, _credential_binding(bytes.fromhex(parts[1])))


def _request(method: str, url: str, body: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            with client.stream(method, url, json=body, headers={
                "Authorization": f"Bearer {app_config.MANGROVE_API_KEY}",
                "Accept-Encoding": "identity",
            }) as response:
                if response.status_code != 200:
                    if response.status_code in {401, 403}:
                        raise UpstreamAccessError(response.status_code)
                    raise MarketplaceError(f"Markets integration request rejected (HTTP {response.status_code}).")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise MarketplaceError("Unsupported upstream response encoding.")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 131072:
                        raise MarketplaceError("Upstream response exceeded the size limit.")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return result
    except (httpx.HTTPError, ValueError):
        raise MarketplaceError("Markets integration request could not be completed.") from None


def _wallet(address: str, settings: dict) -> dict:
    wallet = _get_wallet_row(address)
    valid = wallet and wallet["backup_confirmed_at"] and (
        (wallet["chain"] == "evm" and wallet["chain_id"] in {8453, 84532}
         and wallet["chain_id"] == settings["chain_id"])
        or (wallet["chain"] == "xrpl" and settings.get("xrpl_network") in {"mainnet", "testnet", "devnet"}
            and wallet["network"] == settings["xrpl_network"]))
    if not valid:
        raise SigningError("Select a backed-up local wallet on the configured marketplace network.")
    return wallet


def _wallet_settings(address: str, settings: dict) -> dict:
    _wallet(address, settings)
    return settings.copy()


def _markets_call(settings: dict, operation: str, arguments: dict) -> dict:
    """Discover ownership support and invoke only a pinned tool over native MCP."""
    async def invoke() -> dict:
        protect_mcp_diagnostics()
        def factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(headers=headers, timeout=timeout or 15, auth=auth,
                                     follow_redirects=False, trust_env=False)

        with anyio.fail_after(20):
            async with streamablehttp_client(
                settings["markets"] + "/mcp/",
                headers=({"Authorization": f"Bearer {_api_key(app_config)}"} if _api_key(app_config) else {}),
                timeout=15, sse_read_timeout=15, httpx_client_factory=factory,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    cursor = None
                    found = None
                    for _ in range(10):
                        page = await session.list_tools(cursor=cursor)
                        found = next((tool for tool in page.tools if tool.name == operation), None)
                        cursor = page.nextCursor
                        if found or not cursor:
                            break
                    metadata = (found.meta or {}).get("mangrove/marketplace", {}) if found else {}
                    if (found is None or metadata.get("version") != 1
                            or metadata.get("mode") != "ownership"
                            or metadata.get("protocol") != "ownership-v1"
                            or settings.get("contract_digest") != contract_digest(found)):
                        raise MarketplaceError("Markets does not advertise the supported ownership tool contract.")
                    result = await session.call_tool(operation, arguments=arguments)
                    if len(result.content) != 1 or result.content[0].type != "text":
                        raise MarketplaceError("Unsupported Markets response format.")
                    text = result.content[0].text
                    if len(text.encode()) > 131072:
                        raise MarketplaceError("Markets response exceeded the size limit.")
                    payload = json.loads(text)
                    if not isinstance(payload, dict):
                        raise MarketplaceError("Unsupported Markets response format.")
                    if result.isError:
                        payload = {**payload, "error": True}
                    return payload
    try:
        return anyio.run(invoke)
    except Exception as error:
        access = upstream_access_error(error)
        if access is None and isinstance(error, BaseExceptionGroup):
            pending = list(error.exceptions)
            while pending and access is None:
                child = pending.pop()
                if isinstance(child, BaseExceptionGroup):
                    pending.extend(child.exceptions)
                else:
                    access = upstream_access_error(child)
        if access is not None:
            raise access from None
        raise MarketplaceError("Markets MCP request failed or its ownership contract is incompatible.") from None


def _db() -> sqlite3.Connection:
    if str(app_config.DB_PATH) == ":memory:":
        raise MarketplaceError("Marketplace approvals require a persistent local database.")
    connection = sqlite3.connect(str(app_config.DB_PATH), timeout=3, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def prepare(operation: str, arguments: dict, wallet_address: str) -> dict:
    """Fetch and validate a challenge without signing or executing a mutation."""
    settings = _settings()
    settings = _wallet_settings(wallet_address, settings)
    wallet = _wallet(wallet_address, settings)
    from src.services.marketplace_catalog import get_tool_sync
    tool = get_tool_sync(operation)
    arguments = normalize_arguments(tool, arguments, wallet["address"], "xrpl" if wallet["chain"] == "xrpl" else "base")
    identity = _identity(settings, wallet)
    challenge = _markets_call({**settings, "contract_digest": contract_digest(tool)}, operation, arguments)
    if challenge.get("code") != "OWNERSHIP_REQUIRED":
        if challenge.get("error") is True:
            return challenge
        raise MarketplaceError("Markets did not return an ownership challenge; no signing was attempted.")
    validate_challenge(challenge, operation=operation, arguments=arguments, wallet=wallet["address"],
                       audience=settings["audience"], identity=identity,
                       chain="xrpl" if wallet["chain"] == "xrpl" else "base",
                       network=(f"xrpl:{settings['xrpl_network']}" if wallet["chain"] == "xrpl"
                                else f"eip155:{settings['chain_id']}"))
    payment_intent = None
    if challenge.get("payment_intent") is not None:
        from src.services.marketplace_payments import validate_intent
        payment_intent = validate_intent(challenge["payment_intent"], settings)
    approval_id = str(uuid.uuid4())
    expires_at = challenge["ownership_proof"]["expires_at"]
    payload = {"settings": settings, "operation": operation, "arguments": arguments,
               "wallet": wallet["address"], "identity": identity, "challenge": challenge,
               "credential_binding": _credential_binding(secrets.token_bytes(16)),
               "contract_digest": contract_digest(tool), "payment_intent": payment_intent}
    with closing(_db()) as connection:
        connection.execute("DELETE FROM marketplace_approvals WHERE id IN "
                           "(SELECT id FROM marketplace_approvals WHERE state = 'prepared' AND expires_at <= ? LIMIT 1000)",
                           (time.time(),))
        connection.execute("INSERT INTO marketplace_approvals VALUES (?, ?, ?, 'prepared', NULL)",
                           (approval_id, json.dumps(payload), expires_at))
    return {**({"payment": payment_intent} if payment_intent else {}), "approval_id": approval_id, "operation": operation, "arguments": arguments,
            "wallet_address": wallet["address"], "chain_id": settings["chain_id"],
            "chain": "xrpl" if wallet["chain"] == "xrpl" else "base",
            "network": (settings.get("xrpl_network") if wallet["chain"] == "xrpl"
                        else "mainnet" if settings["chain_id"] == 8453 else "testnet"),
            "settlement_network": (f"xrpl:{settings['xrpl_network']}" if wallet["chain"] == "xrpl"
                                   else f"eip155:{settings['chain_id']}"),
            "markets_url": settings["markets"], "expires_at": expires_at,
            "status": "approval_required",
            "message": "Review this exact action with the user before calling marketplace_submit with confirm=true."}


def submit(approval_id: str, confirm: bool = False) -> dict:
    """Claim once before signing; never retry or reauthorize an uncertain write."""
    if confirm is not True:
        raise MarketplaceError("Explicit confirmation of the prepared action is required.")
    settings = _settings()
    with closing(_db()) as connection:
        row = connection.execute("SELECT * FROM marketplace_approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise MarketplaceError("Marketplace approval was not found.")
        payload = json.loads(row["payload"])
        settings = _wallet_settings(payload["wallet"], settings)
        if payload["settings"] != settings or not _credential_matches(payload.get("credential_binding")):
            raise MarketplaceError("Marketplace configuration or API key changed; approval is invalid.")
        if row["state"] == "completed":
            from src.services.marketplace_payments import continue_payment
            return continue_payment(payload, json.loads(row["result"]), approval_id)
        if row["state"] != "prepared":
            raise MarketplaceError("This action was already submitted; inspect its outcome before creating another approval.")
        from src.services.marketplace_catalog import get_tool_sync
        current_tool = get_tool_sync(payload["operation"])
        if payload.get("contract_digest") != contract_digest(current_tool):
            raise MarketplaceError("Markets tool contract changed; prepare the action again before signing.")
        wallet = _wallet(payload["wallet"], settings)
        identity = _identity(settings, wallet)
        if identity != payload["identity"]:
            raise MarketplaceError("Account authorization changed; approval is invalid.")
        wallet = _wallet(payload["wallet"], settings)
        validate_challenge(payload["challenge"], operation=payload["operation"], arguments=payload["arguments"],
                           wallet=payload["wallet"], audience=settings["audience"], identity=identity,
                           chain="xrpl" if wallet["chain"] == "xrpl" else "base",
                           network=(f"xrpl:{settings['xrpl_network']}" if wallet["chain"] == "xrpl"
                                    else f"eip155:{settings['chain_id']}"))
        if payload.get("payment_intent"):
            from src.services.purchase_balance import require_purchase_balance
            require_purchase_balance(payload["wallet"], payload["payment_intent"]["requirements"])
        claimed = connection.execute(
            "UPDATE marketplace_approvals SET state = 'submitted' WHERE id = ? AND state = 'prepared' AND expires_at > ?",
            (approval_id, time.time()),
        ).rowcount
        if claimed != 1:
            raise MarketplaceError("Approval expired or was already submitted.")
        try:
            proof = sign_marketplace_proof(
                payload["challenge"], operation=payload["operation"], arguments=payload["arguments"],
                wallet_address=payload["wallet"], audience=settings["audience"], identity=identity,
                chain_id=settings["chain_id"], xrpl_network=settings.get("xrpl_network"),
            )
            arguments = {**payload["arguments"], "ownership_proof": {
                **payload["challenge"]["ownership_proof"], **proof,
            }}
            result = _markets_call({**settings, "contract_digest": payload["contract_digest"]}, payload["operation"], arguments)
            connection.execute("UPDATE marketplace_approvals SET state = 'completed', result = ? WHERE id = ?",
                               (json.dumps(result), approval_id))
        except Exception:
            raise MarketplaceError(
                "Action outcome is uncertain. Do not submit a replacement; inspect Markets records first.",
                suggestion=f"Retain approval ID {approval_id} for reconciliation.",
            ) from None
        from src.services.marketplace_payments import continue_payment
        return continue_payment(payload, result, approval_id)
