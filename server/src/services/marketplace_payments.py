"""Execute a server-declared payment continuation within a saved user approval."""
from __future__ import annotations

import json
import re
import time

import anyio
import httpx
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from mcp import ClientSession
from src.config import app_config
from src.services import marketplace, marketplace_catalog, payment_operations, x402_payer
from src.services.marketplace_authorization import contract_digest, normalize_arguments, validate_challenge
from src.services.wallet_manager import sign_marketplace_proof
from src.shared.clients.mangrove import _api_key
from src.shared.errors import AgentError, X402PaymentUncertain
from src.shared.x402.mcp_diagnostics import protect_mcp_diagnostics


def _require(condition: object) -> None:
    if not condition:
        raise ValueError


def validate_intent(intent: dict, settings: dict) -> dict:
    try:
        terms = intent['requirements']
        _require(type(intent['version']) is int and intent['version'] == 1 and intent['protocol'] == 'x402-mcp')
        _require(re.fullmatch(r'marketplace_[a-z0-9_]{1,100}', intent['tool']))
        _require(terms['scheme'] == 'exact' and terms['network'] == f"eip155:{settings['chain_id']}")
        _require(re.fullmatch(r'[1-9][0-9]{0,77}', terms['amount']))
        _require(all(re.fullmatch(r'0x[0-9a-fA-F]{40}', terms[k]) and int(terms[k][2:], 16)
                   for k in ('asset', 'payTo')))
        bindings = intent['result_bindings']
        _require(isinstance(bindings, dict) and 0 < len(bindings) <= 10)
        _require(all(isinstance(k, str) and isinstance(v, str) for k, v in bindings.items()))
        tool = marketplace_catalog.get_tool_sync(intent['tool'])
        _require(marketplace_catalog.contract(tool)['mode'] == 'ownership')
        return {**intent, 'contract_digest': contract_digest(tool)}
    except (KeyError, TypeError, ValueError):
        raise marketplace.MarketplaceError('Markets returned an incompatible purchase preview.') from None


def _body(result: CallToolResult) -> dict:
    if len(result.content) == 1 and result.content[0].type == 'text':
        text = result.content[0].text
        if len(text.encode()) <= 131072:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise marketplace.MarketplaceError('Invalid Markets payment response.')


async def _pay(payload: dict, result: dict, approval_id: str) -> x402_payer.PaymentResult:
    intent, settings = payload['payment_intent'], payload['settings']
    wallet = marketplace._wallet(payload['wallet'], settings)
    tool = await marketplace_catalog.get_tool(intent['tool'])
    if contract_digest(tool) != intent['contract_digest']:
        raise marketplace.MarketplaceError('Markets payment contract changed; review the existing offer before continuing.')
    arguments = normalize_arguments(tool, {k: result[v] for k, v in intent['result_bindings'].items()},
                                    payload['wallet'], 'base')
    identity = await anyio.to_thread.run_sync(marketplace._identity, settings, wallet)
    if identity != payload['identity']:
        raise marketplace.MarketplaceError('Account identity changed; payment approval is invalid.')
    key = _api_key(app_config)
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=False, trust_env=False) as http:
        async with streamable_http_client(settings['markets'] + '/mcp/', http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                class ApprovedSession:
                    async def initialize(self) -> None:
                        await session.initialize()

                    async def call_tool(self, *, name: str, arguments: dict, meta: dict | None = None) -> CallToolResult:
                        if name != intent['tool']:
                            raise marketplace.MarketplaceError('Payment continuation changed.')
                        challenge = _body(await session.call_tool(name, arguments=arguments))
                        if challenge.get('code') != 'OWNERSHIP_REQUIRED':
                            raise marketplace.MarketplaceError('Markets did not authorize this payment continuation.')
                        validate_challenge(challenge, operation=name, arguments=arguments,
                                           wallet=payload['wallet'], audience=settings['audience'], identity=identity,
                                           chain='base', network=intent['requirements']['network'])
                        proof = await anyio.to_thread.run_sync(lambda: sign_marketplace_proof(
                            challenge, operation=name, arguments=arguments, wallet_address=payload['wallet'],
                            audience=settings['audience'], identity=identity, chain_id=settings['chain_id']))
                        return await session.call_tool(name, arguments={**arguments, 'ownership_proof': {
                            **challenge['ownership_proof'], **proof}}, meta=meta)

                return await x402_payer.pay_mcp(ApprovedSession(), name=intent['tool'], arguments=arguments,
                    resource=settings['markets'] + '/mcp/', wallet_address=payload['wallet'],
                    operation_id=approval_id, expected_payment=intent['requirements'])


def continue_payment(payload: dict, result: dict, approval_id: str) -> dict:
    intent = payload.get('payment_intent')
    if not intent:
        return result
    if result.get('x402Version') == 2 and payload['operation'] == intent['tool']:
        result = {**result, **{source: payload['arguments'][target]
                              for target, source in intent['result_bindings'].items()}}
    elif result.get('code') != 'PAYMENT_REQUIRED':
        return result
    if time.time() >= payload['challenge']['ownership_proof']['expires_at'] and not payment_operations.reservation_ids(approval_id):
        raise marketplace.MarketplaceError('Purchase approval expired before payment. Review the existing offer again.')
    protect_mcp_diagnostics()
    async def run() -> x402_payer.PaymentResult:
        with x402_payer._private_mcp_diagnostics(), anyio.fail_after(45):
            return await _pay(payload, result, approval_id)
    try:
        outcome = anyio.run(run)
    except Exception as error:
        pending = [error]
        while pending:
            child = pending.pop()
            if isinstance(child, AgentError):
                raise child from None
            if isinstance(child, BaseExceptionGroup):
                pending.extend(child.exceptions)
        try:
            reservations = payment_operations.reservation_ids(approval_id)
        except Exception:
            raise X402PaymentUncertain(operation_id=approval_id) from None
        if reservations:
            raise X402PaymentUncertain(operation_id=approval_id, reservation_ids=reservations) from None
        raise marketplace.MarketplaceError('The approved purchase could not be completed. Check its offer status.') from None
    return _body(CallToolResult.model_validate(outcome.mcp_result))
