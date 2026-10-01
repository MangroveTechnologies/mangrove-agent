"""Ownership signing and durable approval regressions."""
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from src.services import marketplace as svc
from src.services import wallet_manager as wm
from src.services.marketplace_authorization import PREFIX, canonical, normalize
from src.shared.errors import SigningError


def arguments():
    return dict(title='Dataset', description='Test', category='data', price_xrp=1)


def challenge(args, wallet, identity, proof=None):
    proof = proof or dict(nonce='a' * 64, issued_at=int(time.time()), expires_at=int(time.time()) + 300)
    fields = {k: proof[k] for k in ('nonce', 'issued_at', 'expires_at')}
    message = dict(version=1, audience='test-market', user_id=identity['user_id'], org_id=identity['org_id'],
                   credential_type='api_key', operation='marketplace_create_listing',
                   arguments_sha256=hashlib.sha256(canonical(args).encode()).hexdigest(),
                   chain='base', address=wallet, **fields)
    return dict(code='OWNERSHIP_REQUIRED', chain='base', address=wallet, ownership_proof=fields,
                authorization=PREFIX + canonical(message))


@pytest.fixture
def env(tmp_path, monkeypatch):
    from src.config import app_config
    from src.shared.db import sqlite as db
    monkeypatch.setattr(app_config, 'DB_PATH', str(tmp_path / 'agent.db'))
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'test-marketplace-credential')
    db.reset_connection()
    db.init_db()
    settings = dict(markets='http://localhost:8081', authority='http://localhost:5002/api/v1',
                    audience='test-market', chain_id=84532)
    monkeypatch.setattr(svc, '_settings', lambda: settings.copy())
    wallet = Account.create()
    row = dict(address=wallet.address, chain='evm', chain_id=84532, backup_confirmed_at='yes')
    monkeypatch.setattr(svc, '_get_wallet_row', lambda address: row if address == wallet.address else None)
    monkeypatch.setattr(wm, '_get_wallet_row', lambda address: row)
    monkeypatch.setattr(wm, 'require_backup_confirmed', lambda address: None)
    decrypted = []
    def load(address):
        decrypted.append(address)
        return wallet.key.hex()
    monkeypatch.setattr(wm, '_load_secret', load)
    identity = dict(version=1, audience='mangrovemarkets', auth_method='api_key', user_id='alice',
                    org_id='org', permissions=['execution:write'], cache_ttl_seconds=0)
    calls = []
    def request(method, url, body=None):
        calls.append((method, url, body))
        if url.endswith('/tools'):
            return {'tools': ['marketplace_create_listing']}
        if url.endswith('/auth/markets-context'):
            return identity.copy()
        if 'ownership_proof' not in body:
            return challenge(body, wallet.address, identity)
        proof = body['ownership_proof']
        args = {k: v for k, v in body.items() if k != 'ownership_proof'}
        original = challenge(args, wallet.address, identity, proof)
        assert Account.recover_message(encode_defunct(text=original['authorization']),
                                       signature=proof['signature']) == wallet.address
        return {'listing_id': 'saved-listing', 'status': 'active'}
    monkeypatch.setattr(svc, '_request', request)
    monkeypatch.setattr(svc, '_markets_call', lambda settings, op, body: svc._request('POST', settings['markets'] + '/api/v1/tools/' + op, body))
    yield SimpleNamespace(wallet=wallet, identity=identity, calls=calls, decrypted=decrypted,
                          settings=settings, request=request, row=row)
    db.reset_connection()


def prepare(env):
    return svc.prepare('marketplace_create_listing', arguments(), env.wallet.address)


def test_prepare_then_real_signature_and_cached_result(env):
    approval = prepare(env)
    assert env.decrypted == []
    result = svc.submit(approval['approval_id'], True)
    assert result['listing_id'] == 'saved-listing'
    assert svc.submit(approval['approval_id'], True) == result
    assert len(env.decrypted) == 1


def test_confirmation_required(env):
    approval = prepare(env)
    with pytest.raises(svc.MarketplaceError):
        svc.submit(approval['approval_id'])
    assert not env.decrypted


@pytest.mark.parametrize('field,value', [
    ('audience', 'evil'), ('user_id', 'bob'), ('org_id', 'other'), ('operation', 'wallet_send'),
    ('arguments_sha256', '0' * 64), ('chain', 'xrpl'), ('address', '0x' + '11' * 20),
    ('credential_type', 'jwt'), ('version', 2), ('expires_at', 9999999999),
])
def test_tampered_challenge_never_decrypts(env, field, value):
    args = normalize('marketplace_create_listing', arguments(), env.wallet.address)
    proof = challenge(args, env.wallet.address, env.identity)
    payload = json.loads(proof['authorization'].split('\n', 1)[1])
    payload[field] = value
    proof['authorization'] = PREFIX + canonical(payload)
    with pytest.raises(SigningError):
        wm.sign_marketplace_authorization(proof, operation='marketplace_create_listing', arguments=args,
                                         wallet_address=env.wallet.address, audience='test-market',
                                         identity=env.identity, chain_id=84532)
    assert not env.decrypted


def test_no_replacement_after_uncertain_submission(env, monkeypatch):
    approval = prepare(env)
    def fail(method, url, body=None):
        if body and 'ownership_proof' in body:
            raise httpx.ReadTimeout('sensitive provider detail')
        return env.request(method, url, body)
    monkeypatch.setattr(svc, '_request', fail)
    with pytest.raises(svc.MarketplaceError, match='uncertain') as error:
        svc.submit(approval['approval_id'], True)
    assert 'sensitive' not in str(error.value)
    with pytest.raises(svc.MarketplaceError, match='already submitted'):
        svc.submit(approval['approval_id'], True)
    assert len(env.decrypted) == 1


def test_concurrent_submissions_sign_once(env):
    approval = prepare(env)
    def run(_):
        try:
            return svc.submit(approval['approval_id'], True)
        except svc.MarketplaceError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert any(result and result.get('listing_id') for result in results)
    assert len(env.decrypted) == 1


@pytest.mark.parametrize('completed', [False, True])
def test_changed_key_invalidates_approval(env, monkeypatch, completed):
    approval = prepare(env)
    if completed:
        svc.submit(approval['approval_id'], True)
    env.decrypted.clear()
    env.calls.clear()
    monkeypatch.setattr(svc.app_config, 'MANGROVE_API_KEY', 'replacement-test-credential')
    with pytest.raises(svc.MarketplaceError, match='changed'):
        svc.submit(approval['approval_id'], True)
    assert not env.decrypted
    assert not env.calls


def test_approval_credentials_are_salted_and_not_stored_in_plaintext(env):
    approvals = [prepare(env), prepare(env)]
    bindings = []
    with svc.closing(svc._db()) as connection:
        for approval in approvals:
            raw = connection.execute('SELECT payload FROM marketplace_approvals WHERE id = ?',
                                     (approval['approval_id'],)).fetchone()['payload']
            assert svc.app_config.MANGROVE_API_KEY not in raw
            payload = json.loads(raw)
            assert 'credential' not in payload['settings']
            binding = payload['credential_binding']
            assert svc._credential_matches(binding)
            bindings.append(binding)
    assert bindings[0] != bindings[1]
    assert bindings[0].split('$')[1] != bindings[1].split('$')[1]


@pytest.mark.parametrize('binding', [None, {}, 'a' * 64,
    'pbkdf2-sha256-1$' + 'a' * 32 + '$' + 'b' * 64,
    'pbkdf2-sha256-600000$' + 'g' * 32 + '$' + 'b' * 64,
    'pbkdf2-sha256-600000$aa$bb',
    'pbkdf2-sha256-600000$' + 'a' * 32 + '$' + 'b' * 64])
def test_invalid_or_legacy_credential_binding_fails_before_signing(env, binding):
    approval = prepare(env)
    with svc.closing(svc._db()) as connection:
        row = connection.execute('SELECT payload FROM marketplace_approvals WHERE id = ?',
                                 (approval['approval_id'],)).fetchone()
        payload = json.loads(row['payload'])
        if binding is None:
            payload.pop('credential_binding')
            payload['settings']['credential'] = 'legacy-fingerprint'
        else:
            payload['credential_binding'] = binding
        connection.execute('UPDATE marketplace_approvals SET payload = ? WHERE id = ?',
                           (json.dumps(payload), approval['approval_id']))
    env.calls.clear()
    with pytest.raises(svc.MarketplaceError, match='changed'):
        svc.submit(approval['approval_id'], True)
    assert not env.decrypted
    assert not env.calls


def test_revoked_permission_invalidates_approval(env):
    approval = prepare(env)
    env.identity['permissions'] = []
    with pytest.raises(svc.MarketplaceError, match='authorization changed'):
        svc.submit(approval['approval_id'], True)
    assert not env.decrypted


@pytest.mark.parametrize('args', [{'payment': 'signed-money'}, {'ownership_proof': {}},
                                  {'seller_address': 'other'}, {'price_xrp': float('nan')}])
def test_unsupported_input_fails_before_callout(env, args):
    with pytest.raises(SigningError):
        svc.prepare('marketplace_create_listing', {**arguments(), **args}, env.wallet.address)
    assert not env.calls


def test_wrong_network_wallet_rejected(env):
    env.row['chain_id'] = 8453
    with pytest.raises(SigningError):
        prepare(env)
    assert not env.calls


def test_generic_message_signing_remains_disabled(env):
    with pytest.raises(SigningError):
        wm.sign_message('anything', env.wallet.address)
    assert not env.decrypted


@pytest.mark.parametrize('url', ['http://evil.example', 'https://user:pass@example.com',
                                'https://example.com?redirect=x', 'file:///tmp/a'])
def test_untrusted_endpoint_rejected(url):
    with pytest.raises(svc.MarketplaceError):
        svc._url(url)


@pytest.mark.parametrize('change', ['expired', 'future', 'bool', 'nonce', 'extra'])
def test_bad_proof_fields_never_decrypt(env, change):
    args = normalize('marketplace_create_listing', arguments(), env.wallet.address)
    data = challenge(args, env.wallet.address, env.identity)
    updates = {
        'expired': {'expires_at': int(time.time()) - 1},
        'future': {'issued_at': int(time.time()) + 100},
        'bool': {'issued_at': True}, 'nonce': {'nonce': 'bad'}, 'extra': {'signature': 'bad'},
    }
    data['ownership_proof'].update(updates[change])
    with pytest.raises(SigningError):
        wm.sign_marketplace_authorization(data, operation='marketplace_create_listing', arguments=args,
                                         wallet_address=env.wallet.address, audience='test-market',
                                         identity=env.identity, chain_id=84532)
    assert not env.decrypted


def test_approval_survives_connection_restart(env):
    from src.shared.db.sqlite import reset_connection
    approval = prepare(env)
    reset_connection()
    assert svc.submit(approval['approval_id'], True)['listing_id'] == 'saved-listing'
    reset_connection()
    assert svc.submit(approval['approval_id'], True)['listing_id'] == 'saved-listing'
    assert len(env.decrypted) == 1


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('upstream_error', [False, True])
def test_native_mcp_discovery_before_call(monkeypatch, legacy, upstream_error):
    from mcp.client.streamable_http import streamablehttp_client as real_transport

    from src.config import app_config
    from src.services.marketplace_authorization import Listing
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'test_key')
    methods = []
    def handle(request):
        assert request.url.host == 'localhost'
        assert request.headers['authorization'] == 'Bearer test_key'
        if request.method in {'GET', 'DELETE'}:
            return httpx.Response(405)
        body = json.loads(request.content)
        methods.append(body['method'])
        if 'id' not in body:
            return httpx.Response(202)
        if body['method'] == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'test-markets', 'version': '1'}}
        elif body['method'] == 'tools/list':
            schema = Listing.model_json_schema()
            if not legacy:
                schema['properties']['ownership_proof'] = {'type': 'object'}
            result = {'tools': [{'name': 'marketplace_create_listing', 'inputSchema': schema}]}
        else:
            result = {'isError': upstream_error, 'content': [{'type': 'text', 'text': json.dumps({'code': 'OWNERSHIP_REQUIRED'})}]}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})
    def transport(url, **kwargs):
        def factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(transport=httpx.MockTransport(handle), headers=headers,
                                     timeout=timeout, auth=auth, trust_env=False, follow_redirects=False)
        kwargs['httpx_client_factory'] = factory
        return real_transport(url, **kwargs)
    monkeypatch.setattr(svc, 'streamablehttp_client', transport)
    if legacy:
        with pytest.raises(svc.MarketplaceError):
            svc._markets_call({'markets': 'http://localhost:8081'}, 'marketplace_create_listing', {})
        assert 'tools/call' not in methods
    else:
        result = svc._markets_call({'markets': 'http://localhost:8081'}, 'marketplace_create_listing', {})
        assert result['code'] == 'OWNERSHIP_REQUIRED'
        assert bool(result.get('error')) is upstream_error
        assert methods.index('tools/list') < methods.index('tools/call')


@pytest.mark.asyncio
async def test_local_mcp_auth_and_service_wiring(monkeypatch):
    from mcp.server.fastmcp import FastMCP

    import src.mcp.tools as tools
    from src.mcp.marketplace_tools import register_marketplace
    server = FastMCP('test')
    monkeypatch.setattr(tools, '_require', lambda key: key == 'local-key')
    register_marketplace(server)
    calls = []
    monkeypatch.setattr(svc, 'prepare', lambda *args: calls.append(args) or {'approval_id': 'a'})
    tool = server._tool_manager._tools['marketplace_prepare']
    args = {'operation': 'marketplace_create_listing', 'arguments': {}, 'wallet_address': 'wallet'}
    assert json.loads(await tool.run(args))['code'] == 'AUTH_INVALID_API_KEY'
    assert not calls
    assert json.loads(await tool.run({**args, 'api_key': 'local-key'}))['approval_id'] == 'a'
    assert len(calls) == 1


def test_local_rest_uses_same_service(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes.marketplace import router
    from src.shared.auth.dependency import require_api_key
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_api_key] = lambda: 'local-key'
    monkeypatch.setattr(svc, 'prepare', lambda *args: {'approval_id': 'a'})
    with TestClient(app) as client:
        result = client.post('/marketplace/prepare', json={
            'operation': 'marketplace_create_listing', 'arguments': {}, 'wallet_address': 'wallet',
        })
        assert result.status_code == 200
        assert result.json() == {'approval_id': 'a'}
        result = client.post('/marketplace/submit', json={'approval_id': 'a', 'confirm': 'true'})
        assert result.status_code == 422


@pytest.mark.parametrize('operation,args', [
    ('marketplace_make_offer', {'listing_id': 'listing'}),
    ('marketplace_accept_offer', {'offer_id': 'offer'}),
    ('marketplace_confirm_delivery', {'offer_id': 'offer'}),
    ('marketplace_rate', {'offer_id': 'offer', 'score': 5}),
])
def test_other_actions_have_guarded_signatures(env, operation, args):
    normalized = normalize(operation, args, env.wallet.address)
    data = challenge(normalized, env.wallet.address, env.identity)
    message = json.loads(data['authorization'].split('\n', 1)[1])
    message['operation'] = operation
    message['address'] = env.wallet.address.lower()
    data['address'] = env.wallet.address.lower()
    data['authorization'] = PREFIX + canonical(message)
    signature = wm.sign_marketplace_authorization(
        data, operation=operation, arguments=normalized, wallet_address=env.wallet.address,
        audience='test-market', identity=env.identity, chain_id=84532,
    )
    assert Account.recover_message(encode_defunct(text=data['authorization']), signature=signature) == env.wallet.address


def test_payment_continuation_is_not_an_ownership_only_action(env):
    with pytest.raises(SigningError):
        normalize('marketplace_make_offer', {'listing_id': 'l', 'offer_id': 'o', 'payment': 'funds'}, env.wallet.address)
    assert not env.decrypted


def test_tampered_persisted_preview_never_signs(env):
    approval = prepare(env)
    with svc._db() as connection:
        row = connection.execute('SELECT payload FROM marketplace_approvals WHERE id=?',
                                 (approval['approval_id'],)).fetchone()
        payload = json.loads(row['payload'])
        payload['arguments']['price_xrp'] = 100
        connection.execute('UPDATE marketplace_approvals SET payload=? WHERE id=?',
                           (json.dumps(payload), approval['approval_id']))
    with pytest.raises(SigningError):
        svc.submit(approval['approval_id'], True)
    assert not env.decrypted


@pytest.mark.parametrize('status', [401, 403])
def test_authority_denial_is_sanitized(monkeypatch, status):
    from src.config import app_config
    from src.shared.errors import UpstreamAccessError
    monkeypatch.setattr(app_config, 'MANGROVE_API_KEY', 'local_test_key')
    original = httpx.Client
    def client(**kwargs):
        return original(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text='sensitive upstream text')), **kwargs)
    monkeypatch.setattr(svc.httpx, 'Client', client)
    with pytest.raises(UpstreamAccessError) as error:
        svc._request('GET', 'http://localhost/auth/markets-context')
    assert error.value.http_status == status
    assert 'sensitive' not in str(error.value)


def test_general_signing_remains_disabled_after_marketplace_sign(env):
    approval = prepare(env)
    svc.submit(approval['approval_id'], True)
    with pytest.raises(SigningError):
        wm.sign_message('arbitrary', env.wallet.address)
    assert len(env.decrypted) == 1


@pytest.mark.parametrize('algorithm', ['ed25519', 'secp256k1'])
@pytest.mark.parametrize('operation, action_args', [
    ('marketplace_create_listing', arguments()),
    ('marketplace_make_offer', {'listing_id': 'listing'}),
    ('marketplace_accept_offer', {'offer_id': 'offer', 'escrow_sequence': 7}),
    ('marketplace_confirm_delivery', {'offer_id': 'offer'}),
    ('marketplace_rate', {'offer_id': 'offer', 'score': 5}),
])
def test_xrpl_prepare_sign_submit_and_replay(env, monkeypatch, algorithm, operation, action_args):
    from xrpl.constants import CryptoAlgorithm
    from xrpl.core import keypairs
    from xrpl.wallet import Wallet
    wallet = Wallet.create(algorithm=CryptoAlgorithm(algorithm))
    env.settings['xrpl_network'] = 'testnet'
    env.row.update(address=wallet.classic_address, chain='xrpl', chain_id=None, network='testnet')
    monkeypatch.setattr(svc, '_get_wallet_row', lambda address: env.row)
    monkeypatch.setattr(wm, '_load_secret', lambda address: env.decrypted.append(address) or wallet.seed)
    calls = []
    def invoke(settings, operation, args):
        calls.append(args)
        data = challenge({k: v for k, v in args.items() if k != 'ownership_proof'}, wallet.classic_address, env.identity,
                         args.get('ownership_proof'))
        message = json.loads(data['authorization'].split('\n', 1)[1])
        message['chain'] = data['chain'] = 'xrpl'
        message['operation'] = operation
        data['authorization'] = PREFIX + canonical(message)
        if 'ownership_proof' not in args:
            return data
        proof = args['ownership_proof']
        assert proof['public_key'] == wallet.public_key
        assert keypairs.is_valid_message(data['authorization'].encode(), bytes.fromhex(proof['signature']), wallet.public_key)
        return {'listing_id': 'xrpl-listing'}
    monkeypatch.setattr(svc, '_markets_call', invoke)
    approval = svc.prepare(operation, action_args, wallet.classic_address)
    assert not env.decrypted
    if operation in {'marketplace_create_listing', 'marketplace_make_offer'}:
        assert approval['arguments']['chain'] == 'xrpl'
        assert approval['arguments']['currency'] == 'XRP'
    assert approval['network'] == 'testnet'
    assert svc.submit(approval['approval_id'], True) == {'listing_id': 'xrpl-listing'}
    assert svc.submit(approval['approval_id'], True) == {'listing_id': 'xrpl-listing'}
    assert len(calls) == 2 and len(env.decrypted) == 1


def test_xrpl_wallet_network_mismatch_fails_before_remote_call(env, monkeypatch):
    env.settings['xrpl_network'] = 'testnet'
    env.row.update(chain='xrpl', network='mainnet')
    with pytest.raises(SigningError):
        prepare(env)
    assert not env.calls and not env.decrypted


@pytest.mark.parametrize('args', [{'chain': 'base'}, {'currency': 'USDC'}, {'seller_address': 'rOTHER'}])
def test_xrpl_wrong_chain_or_actor_rejected(args):
    from xrpl.wallet import Wallet
    wallet = Wallet.create()
    with pytest.raises(SigningError):
        normalize('marketplace_create_listing', {**arguments(), **args}, wallet.classic_address, 'xrpl')


def test_xrpl_escrow_sequence_and_base_guard():
    from xrpl.wallet import Wallet
    address = Wallet.create().classic_address
    args = {'offer_id': 'offer', 'escrow_sequence': 123}
    assert normalize('marketplace_accept_offer', args, address, 'xrpl')['escrow_sequence'] == 123
    for bad in (True, 0, -1, 2**32):
        with pytest.raises(SigningError):
            normalize('marketplace_accept_offer', {**args, 'escrow_sequence': bad}, address, 'xrpl')
    with pytest.raises(SigningError):
        normalize('marketplace_accept_offer', args, Account.create().address)


@pytest.mark.parametrize('field', ['chain', 'address', 'authorization'])
def test_xrpl_tampering_never_decrypts(env, monkeypatch, field):
    from xrpl.wallet import Wallet
    wallet = Wallet.create()
    env.row.update(chain='xrpl', address=wallet.classic_address, network='testnet')
    args = normalize('marketplace_create_listing', arguments(), wallet.classic_address, 'xrpl')
    data = challenge(args, wallet.classic_address, env.identity)
    payload = json.loads(data['authorization'].split('\n', 1)[1])
    payload['chain'] = data['chain'] = 'xrpl'
    data['authorization'] = PREFIX + canonical(payload)
    data[field] = {'chain': 'base', 'address': wallet.classic_address.lower(), 'authorization': 'other'}[field]
    with pytest.raises(SigningError):
        wm.sign_marketplace_proof(data, operation='marketplace_create_listing', arguments=args,
                                 wallet_address=wallet.classic_address, audience='test-market',
                                 identity=env.identity, chain_id=None, xrpl_network='testnet')
    assert not env.decrypted


@pytest.mark.parametrize('mode', ['user', 'tool'])
@pytest.mark.parametrize('algorithm', ['ed25519', 'secp256k1'])
def test_hook_blocks_xrpl_seeds(mode, algorithm):
    import subprocess
    from pathlib import Path
    from xrpl.constants import CryptoAlgorithm
    from xrpl.wallet import Wallet
    seed = Wallet.create(algorithm=CryptoAlgorithm(algorithm)).seed
    hook = Path(__file__).resolve().parents[3] / '.claude/hooks/block-wallet-secrets.sh'
    result = subprocess.run(['bash', str(hook), '--mode', mode], input=json.dumps({'seed': seed}), text=True, capture_output=True)
    assert result.returncode == 2
    assert seed not in result.stdout + result.stderr
