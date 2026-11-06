"""Real Linux process/UID/HTTP/MCP custody acceptance in a disposable container.

Run only inside the dedicated security-test image, as its test administrator.
The harness creates unfunded wallets. It never broadcasts or uses host secrets.
Only case names and public outcomes are written to stdout.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import pty
import re
import select
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path('/opt/mangrove')
POLICY = Path('/etc/mangrove-custody/policy.json')
SOCKET = '/run/mangrove-custody/agent.sock'
CUSTODY, AGENT, OUTSIDER = 22001, 22002, 22003
PYTHON = sys.executable
LAUNCHER = str(ROOT / 'scripts/custody-service.py')
RESULTS: list[dict] = []
PROCESSES: list[subprocess.Popen] = []
DAEMON = None
TOKEN = 'synthetic-local-security-test-credential'
WALLET = ''


def check(name, condition):
    RESULTS.append({'case': name, 'passed': bool(condition)})
    if not condition:
        raise AssertionError(name)


def directory(path, uid=0, mode=0o755):
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    p.chmod(mode)
    os.chown(p, uid, uid)
    return p


def write_json(path, value, uid=0, mode=0o644):
    p = Path(path)
    p.write_text(json.dumps(value))
    p.chmod(mode)
    os.chown(p, uid, uid)


def run_as(uid, args, **kwargs):
    return subprocess.run(args, user=uid, group=uid, extra_groups=[],
                          capture_output=True, timeout=30, **kwargs)


def rpc(operation, arguments, uid=AGENT, socket_path=SOCKET):
    # The actual socket request comes from a separate process with this OS UID.
    program = (
        'import socket,json,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(15); '
        's.connect(sys.argv[1]); s.sendall(sys.stdin.buffer.read()+b"\\n"); '
        'f=s.makefile("rb"); sys.stdout.buffer.write(f.readline(262145))'
    )
    result = run_as(uid, [PYTHON, '-I', '-c', program, socket_path],
                    input=json.dumps({'operation': operation, 'arguments': arguments}).encode())
    if result.returncode:
        return {'ok': False, 'error': 'transport refused'}
    return json.loads(result.stdout)


def admin(action, answer, uid=CUSTODY):
    """Simulate the independent test administrator's terminal, without printing it."""
    child, fd = pty.fork()
    if child == 0:
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
        os.execv(PYTHON, [PYTHON, '-I', LAUNCHER, 'admin', '--config', str(POLICY), *action])
    output = bytearray()
    deadline = time.monotonic() + 25
    sent = False
    status = None
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], .1)[0]:
                try:
                    block = os.read(fd, 65536)
                except OSError:
                    break
                if not block:
                    break
                output.extend(block)
                if not sent and (b': ' in output or b'(hidden' in output):
                    os.write(fd, answer.encode() + b'\n')
                    sent = True
            finished, status = os.waitpid(child, os.WNOHANG)
            if finished:
                break
        else:
            os.kill(child, signal.SIGKILL)
        try:
            _, status = os.waitpid(child, 0)
        except ChildProcessError:
            pass
        return os.waitstatus_to_exitcode(status or 0), bytes(output)
    finally:
        os.close(fd)


def start(uid, command, env, log):
    stream = open(log, 'ab')
    process = subprocess.Popen(command, user=uid, group=uid, extra_groups=[],
                               cwd=ROOT / 'server', env=env, stdout=stream, stderr=stream)
    PROCESSES.append(process)
    return process


def restart(settings):
    global DAEMON
    write_json(POLICY, settings)
    DAEMON.terminate()
    DAEMON.wait(timeout=15)
    check('graceful daemon stop removes socket', not Path(SOCKET).exists())
    DAEMON = start(CUSTODY, [PYTHON, '-I', LAUNCHER, 'daemon', '--config', str(POLICY)],
                   dict(os.environ), '/var/log/mangrove-security-test/custody.log')
    for _ in range(200):
        if Path(SOCKET).exists():
            break
        if DAEMON.poll() is not None:
            raise AssertionError('custody restart')
        time.sleep(.05)
    check('protected policy survives real daemon restart', rpc('list_wallets', {}).get('ok') is True)


async def exercise_mcp():
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(headers={'X-API-Key': TOKEN}, follow_redirects=True) as http:
        async with streamable_http_client('http://127.0.0.1:19080/mcp/', http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.call_tool('list_wallets', {})
                text = listed.model_dump_json()
                check('real MCP wallet metadata contains created address', WALLET.lower() in text.lower())
                check('real MCP metadata omits secret material', 'encrypted_secret' not in text and 'private_key' not in text)
                imported = await session.call_tool('import_wallet', {'vault_token': 'synthetic-invalid-token'})
                text = imported.model_dump_json().lower()
                check('real MCP cannot import an agent-supplied secret token', 'custody' in text and ('error' in text or 'retired' in text))


def provision():
    global DAEMON
    from src.services.transaction_intent import ROUTER_V6

    directory('/etc/mangrove-custody/config')
    directory('/var/lib/mangrove-custody', CUSTODY, 0o700)
    directory('/run/mangrove-custody', CUSTODY)
    directory('/var/lib/mangrove-agent-test', AGENT, 0o700)
    directory('/etc/mangrove-agent-test/config')
    directory('/var/log/mangrove-security-test')
    installed_socket = '/run/mangrove-installed/agent.sock'
    installed = start(CUSTODY, ['/opt/mangrove-installed/.venv/bin/python', '-I',
                      '/opt/mangrove-installed/scripts/custody-service.py', 'daemon',
                      '--config', '/etc/mangrove-installed/policy.json'], dict(os.environ),
                      '/var/log/mangrove-security-test/installed.log')
    for _ in range(400):
        if Path(installed_socket).exists():
            break
        if installed.poll() is not None:
            raise AssertionError('administrator-installed runtime startup')
        time.sleep(.05)
    check('real installer produces runnable protected environment',
          rpc('list_wallets', {}, socket_path=installed_socket).get('ok') is True)
    installed.terminate()
    installed.wait(timeout=15)
    check('installed service shuts down cleanly', not Path(installed_socket).exists())
    example = json.loads((ROOT / 'server/src/config/local-example-config.json').read_text())
    config = dict(example, AUTH_ENABLED=True, API_KEYS=TOKEN,
                  MANGROVE_API_KEY='synthetic-upstream-key-not-used', MANGROVE_ACCESS_MODE='api-key',
                  DB_PATH='/var/lib/mangrove-custody/wallets.db',
                  MASTER_KEY_PATH='/var/lib/mangrove-custody/master.key',
                  CUSTODY_SOCKET=None, CUSTODY_UID=None)
    config.update(X402_NETWORK='eip155:84532', X402_MANGROVE_ENVIRONMENT='local',
                  X402_MANGROVE_BASE_URL='https://custody-test.invalid')
    write_json(POLICY.parent / 'config/custody-config.json', config)
    rule = {
        'chain_id': 8453, 'input_token': '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913',
        'output_token': '0x4200000000000000000000000000000000000006',
        'router': ROUTER_V6, 'allowed_executors': ['0x' + '12' * 20],
        'max_amount': 1_000_000, 'lifetime_amount': 2_000_000,
        'min_rate_numerator': 1, 'min_rate_denominator': 100_000,
        'max_gas': 300_000, 'max_fee_per_gas': 2_000_000_000,
        'max_total_fee_wei': 600_000_000_000_000, 'max_native_value': 0,
        'lifetime_total_fee_wei': 1_000_000_000_000_000,
        'max_routing_fee_bps': 25,
        'wallet_address': '0x' + '00' * 20,
    }
    settings = {
        'custody_uid': CUSTODY, 'agent_uid': AGENT,
        'state_dir': '/var/lib/mangrove-custody', 'socket_path': SOCKET,
        'policy': {'swap_rules': [rule], 'x402': {
            'chain_id': 84532, 'payees': ['0x' + '34' * 20],
            'max_payment_micro_usd': 100_000, 'lifetime_micro_usd': 200_000,
            'max_validity_seconds': 300,
        }, 'siwx_origins': [], 'identity_chains': []},
    }
    write_json(POLICY, settings)
    config.update(DB_PATH='/var/lib/mangrove-agent-test/agent.db',
                  MASTER_KEY_PATH='/var/lib/mangrove-agent-test/master.key',
                  CUSTODY_SOCKET=SOCKET, CUSTODY_UID=CUSTODY,
                  LOCAL_AGENT_URL='http://127.0.0.1:19080')
    write_json('/etc/mangrove-agent-test/config/security-test-config.json', config)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    daemon = start(CUSTODY, [PYTHON, '-I', LAUNCHER, 'daemon', '--config', str(POLICY)],
                   env, '/var/log/mangrove-security-test/custody.log')
    DAEMON = daemon
    for _ in range(200):
        if Path(SOCKET).exists():
            break
        if daemon.poll() is not None:
            raise AssertionError('custody daemon startup')
        time.sleep(.05)
    check('real dedicated-UID custody daemon starts', Path(SOCKET).exists())
    env.update(ENVIRONMENT='security-test', MANGROVE_AGENT_HOME='/etc/mangrove-agent-test')
    agent = start(AGENT, [PYTHON, '-m', 'uvicorn', 'src.app:app', '--host', '127.0.0.1',
                         '--port', '19080'], env, '/var/log/mangrove-security-test/agent.log')
    import httpx
    for _ in range(400):
        try:
            if httpx.get('http://127.0.0.1:19080/health', timeout=.3).status_code == 200:
                return settings
        except httpx.HTTPError:
            pass
        if agent.poll() is not None:
            break
        time.sleep(.05)
    raise AssertionError('real HTTP agent startup')


def exercise(settings):
    global WALLET
    import httpx
    from eth_abi import encode
    from eth_account import Account
    from eth_utils import keccak
    from src.services.transaction_intent import ROUTER_V6

    with httpx.Client(base_url='http://127.0.0.1:19080', headers={'X-API-Key': TOKEN}, timeout=20) as http:
        created = http.post('/api/v1/agent/wallet/create', json={'chain': 'evm', 'network': 'mainnet', 'chain_id': 8453})
        check('HTTP creates an unfunded custody wallet', created.status_code == 201)
        item = created.json()
        WALLET = item['address']
        check('creation returns neither secret nor redeemable token',
              not any(item.get(k) for k in ('secret', 'private_key', 'seed_phrase', 'vault_token')))
        for suffix in (WALLET + '/reveal', 'reveal-secret/synthetic-token'):
            result = http.get('/api/v1/agent/wallet/' + suffix)
            check('local API cannot export ' + suffix.split('/')[0], result.status_code in (403, 404, 410))
        check('managed-service credential cannot authenticate local wallet access',
              http.get('/api/v1/agent/wallet/list', headers={'X-API-Key': 'synthetic-upstream-key-not-used'}).status_code in (401, 403))
        check('local API cannot confirm backup', http.post('/api/v1/agent/wallet/' + WALLET + '/confirm-backup').status_code in (403, 404, 410))
        check('local API cannot stash plaintext', http.post('/api/v1/agent/wallet/stash-secret', json={'secret': 'invalid-test-input'}).status_code in (403, 404, 410))

    # This is the separate test administrator, not an agent RPC. Authorize this
    # exact unfunded wallet in the protected policy and restart the real daemon.
    settings['policy']['swap_rules'][0]['wallet_address'] = WALLET
    restart(settings)
    asyncio.run(exercise_mcp())

    for op in ('reveal_wallet_secret', 'reveal', 'import_wallet', 'confirm_backup', '__import__'):
        check('custody RPC refuses ' + op, rpc(op, {'address': WALLET}).get('ok') is False)
    check('different OS UID cannot use custody RPC', rpc('list_wallets', {}, OUTSIDER).get('ok') is False)
    for path in ('/var/lib/mangrove-custody/master.key', '/var/lib/mangrove-custody/wallets.db', str(POLICY)):
        access = 'open(__import__("sys").argv[1],"rb").read()' if path != str(POLICY) else 'open(__import__("sys").argv[1],"w")'
        result = run_as(AGENT, [PYTHON, '-I', '-c', access, path])
        check('agent UID denied custody file access: ' + Path(path).name, result.returncode != 0)
    code, _ = admin(['confirm-backup', WALLET], 'BACKED UP', uid=AGENT)
    check('captured terminal does not give agent admin authority', code != 0)

    token = settings['policy']['swap_rules'][0]['input_token']
    approval = {
        'chainId': 8453, 'nonce': 0, 'gas': 100_000, 'type': 2,
        'maxFeePerGas': 1_000_000_000, 'maxPriorityFeePerGas': 1,
        'to': token, 'value': 0,
        'data': '0x095ea7b3' + encode(['address', 'uint256'], [ROUTER_V6, 1_000_000]).hex(),
    }
    intent = {'kind': 'approval', 'chain_id': 8453, 'wallet_address': WALLET,
              'token_address': token, 'spender': ROUTER_V6, 'amount': 1_000_000}
    arguments = {'unsigned_tx': approval, 'intent': intent, 'wallet_address': WALLET, 'chain_id': 8453}
    check('unbacked wallet cannot sign', rpc('sign', arguments).get('ok') is False)
    code, backup_output = admin(['backup', WALLET], 'DISPLAY SECRET')
    private_values = re.findall(rb'(?m)^0x[0-9a-fA-F]{64}\r?$', backup_output)
    check('independent administrator can back up the correct synthetic wallet',
          code == 0 and len(private_values) == 1 and Account.from_key(private_values[0].strip().decode()).address.lower() == WALLET.lower())
    private_value = private_values[0].strip()
    for log in Path('/var/log/mangrove-security-test').glob('*.log'):
        check('wallet secret absent from service log: ' + log.name, private_value not in log.read_bytes())
    del private_values, private_value, backup_output
    code, _ = admin(['confirm-backup', WALLET], 'BACKED UP')
    check('independent custody administrator confirms backup', code == 0)
    result = rpc('sign', arguments)
    check('same nonce works after deterministic backup refusal', result.get('ok') is True)
    check('real approval signature recovers custody wallet', Account.recover_transaction(result['result']).lower() == WALLET.lower())
    check('exact signing retry returns same signature', rpc('sign', arguments).get('result') == result['result'])
    changed = copy.deepcopy(arguments)
    changed['unsigned_tx']['maxPriorityFeePerGas'] = 2
    check('changed transaction cannot reuse reserved nonce', rpc('sign', changed).get('ok') is False)
    for label, updates in (
        ('7702 type', {'type': 4}), ('authorization list', {'authorizationList': []}),
        ('wrong chain', {'chainId': 1}), ('native value on approval', {'value': 1}),
        ('wrong approval target', {'to': '0x' + '56' * 20}),
        ('truncated approval', {'data': approval['data'][:-64]}),
        ('excessive fees', {'maxFeePerGas': 10**15}),
        ('unrecognized transaction field', {'untrusted': True}),
    ):
        bad = copy.deepcopy(arguments)
        bad['unsigned_tx'].update(updates, nonce=1)
        check('rejects ' + label, rpc('sign', bad).get('ok') is False)

    rule = settings['policy']['swap_rules'][0]
    description = (token, rule['output_token'], rule['allowed_executors'][0], WALLET, 1_000_000, 100, 0)
    signature = 'swap(address,(address,address,address,address,uint256,uint256,uint256),bytes)'
    data = keccak(text=signature)[:4] + encode(
        ['address', '(address,address,address,address,uint256,uint256,uint256)', 'bytes'],
        [rule['allowed_executors'][0], description, b''])
    swap = dict(approval, nonce=1, to=ROUTER_V6, data='0x' + data.hex())
    swap_intent = {'kind': 'swap', 'chain_id': 8453, 'wallet_address': WALLET,
                   'router_address': ROUTER_V6, 'input_token_address': token,
                   'output_token_address': rule['output_token'], 'amount': 1_000_000,
                   'min_return_amount': 100}
    request = {'unsigned_tx': swap, 'intent': swap_intent, 'wallet_address': WALLET, 'chain_id': 8453}
    result = rpc('sign', request)
    check('real generic swap signature recovers custody wallet', result.get('ok') is True and Account.recover_transaction(result['result']).lower() == WALLET.lower())
    bad = copy.deepcopy(request)
    bad['unsigned_tx'].update(nonce=2, data='0xdeadbeef')
    check('allowlisted router cannot sign arbitrary calldata', rpc('sign', bad).get('ok') is False)
    second = copy.deepcopy(request)
    second['unsigned_tx']['nonce'] = 2
    check('second swap fits independent lifetime cap', rpc('sign', second).get('ok') is True)
    third = copy.deepcopy(request)
    third['unsigned_tx']['nonce'] = 3
    check('third swap exceeds independent lifetime cap', rpc('sign', third).get('ok') is False)
    restart(settings)
    check('restart preserves spent budget', rpc('sign', third).get('ok') is False)
    check('restart preserves exact signature retry', rpc('sign', request).get('result') == result['result'])
    settings['policy']['swap_rules'][0]['max_amount'] = 1
    restart(settings)
    check('policy tightening prevents cached signature release', rpc('sign', request).get('ok') is False)
    exercise_x402(settings)
    exercise_frames()
    exercise_migration()
    exercise_auth_disabled()


def exercise_x402(settings):
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    wallet = rpc('create_wallet', {'chain': 'evm', 'network': 'testnet', 'chain_id': 84532})['result']['address']
    settings['policy']['x402']['wallet_address'] = wallet
    settings['policy'].update(siwx_origins=['https://custody-test.invalid'],
                              identity_chains=['eip155:84532'], identity_wallets=[wallet])
    restart(settings)
    code, _ = admin(['confirm-backup', wallet], 'BACKED UP')
    check('test administrator authorizes synthetic payment wallet', code == 0)
    fields = [('from', 'address'), ('to', 'address'), ('value', 'uint256'),
              ('validAfter', 'uint256'), ('validBefore', 'uint256'), ('nonce', 'bytes32')]
    args = {'wallet_address': wallet, 'domain': {'name': 'USDC', 'version': '2', 'chainId': 84532,
            'verifyingContract': '0x036CbD53842c5426634e7929541eC2318f3dCF7e'},
            'types': {'TransferWithAuthorization': [{'name': name, 'type': kind} for name, kind in fields]},
            'primary_type': 'TransferWithAuthorization',
            'message': {'from': wallet, 'to': '0x' + '34' * 20, 'value': 100_000,
                        'validAfter': 0, 'validBefore': int(time.time()) + 250, 'nonce': '0x' + '01' * 32}}
    for label, field, value in [('unknown payee', 'to', '0x' + '56' * 20),
                               ('excess amount', 'value', 100_001),
                               ('expired payment', 'validBefore', int(time.time()) - 1),
                               ('future payment', 'validAfter', int(time.time()) + 100),
                               ('excess validity', 'validBefore', int(time.time()) + 1000)]:
        bad = copy.deepcopy(args)
        bad['message'][field] = value
        check('payment rejects ' + label, rpc('sign_x402_authorization', bad).get('ok') is False)
    signed = rpc('sign_x402_authorization', args)
    signable = encode_typed_data(domain_data=args['domain'], message_types=args['types'], message_data=args['message'])
    check('real x402 signature recovers exact payer', signed.get('ok') is True and
          Account.recover_message(signable, signature=signed['result']).lower() == wallet.lower())
    check('x402 retry returns same signature', rpc('sign_x402_authorization', args).get('result') == signed['result'])
    bad = copy.deepcopy(args)
    bad['message']['value'] = 1
    check('x402 nonce cannot sign changed terms', rpc('sign_x402_authorization', bad).get('ok') is False)
    args['message']['nonce'] = '0x' + '02' * 32
    check('second payment fills independent budget', rpc('sign_x402_authorization', args).get('ok') is True)
    args['message']['nonce'] = '0x' + '03' * 32
    args['message']['value'] = 1
    restart(settings)
    check('payment budget survives process restart', rpc('sign_x402_authorization', args).get('ok') is False)
    now = datetime.now(timezone.utc)
    identity = {'wallet_address': wallet, 'domain': 'custody-test.invalid',
                'uri': 'https://custody-test.invalid/login', 'chain': 'eip155:84532', 'nonce': 'testnonce12345',
                'issued_at': now.isoformat(), 'expiration_time': (now + timedelta(seconds=5)).isoformat()}
    proof = rpc('sign_siwx_message', identity)
    check('independently authorized short identity proof signs', proof.get('ok') is True)
    from eth_account.messages import encode_defunct
    from src.shared.x402.siwx import STATEMENT, VERSION, format_eip4361
    message = format_eip4361(domain=identity['domain'], address=wallet, uri=identity['uri'], version=VERSION,
                             chain_id=84532, nonce=identity['nonce'], issued_at=identity['issued_at'],
                             statement=STATEMENT, expiration_time=identity['expiration_time'])
    check('real identity signature recovers custody wallet',
          Account.recover_message(encode_defunct(text=message), signature=proof['result']).lower() == wallet.lower())
    for field, value in [('nonce', 'testnonce12345\n'), ('issued_at', now.isoformat().replace('T', '\n')),
                         ('uri', 'https://attacker.invalid/login'), ('domain', 'attacker.invalid')]:
        bad = dict(identity, **{field: value})
        check('identity refuses malformed or substituted ' + field, rpc('sign_siwx_message', bad).get('ok') is False)
    time.sleep(max(0, (now + timedelta(seconds=5) - datetime.now(timezone.utc)).total_seconds()) + .1)
    check('expired identity proof cannot be released from cache', rpc('sign_siwx_message', identity).get('ok') is False)


def exercise_frames():
    program = ('import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(15); '
               's.connect(sys.argv[1]); s.sendall(sys.stdin.buffer.read()); '
               'sys.stdout.buffer.write(s.recv(262144))')
    for name, frame in [
        ('duplicate JSON keys', b'{"operation":"list_wallets","operation":"reveal","arguments":{}}\n'),
        ('oversized frame', b' ' * 262144 + b'\n'),
        ('deep JSON', b'[' * 1100 + b']' * 1100 + b'\n'),
        ('malformed JSON', b'{bad}\n'),
        ('multiple frames', b'{}\n{}\n'),
    ]:
        outcome = run_as(AGENT, [PYTHON, '-I', '-c', program, SOCKET], input=frame)
        check('socket refuses ' + name, b'"ok":true' not in outcome.stdout)
        check('daemon remains usable after ' + name, rpc('list_wallets', {}).get('ok') is True)


def exercise_migration():
    from cryptography.fernet import Fernet
    from eth_account import Account

    folder = directory('/var/lib/mangrove-legacy-test', CUSTODY, 0o700)
    database, keyfile = folder / 'legacy.db', folder / 'legacy.key'
    secret = Account.create()
    address = secret.address
    key = Fernet.generate_key()
    ciphertext = Fernet(key).encrypt(('0x' + secret.key.hex()).encode())
    keyfile.write_bytes(key)
    conn = sqlite3.connect(database)
    for migration in sorted((ROOT / 'server/src/shared/db/migrations').glob('*.sql')):
        conn.executescript(migration.read_text())
    conn.execute('INSERT INTO wallets(id,address,chain,network,chain_id,encrypted_secret,encryption_method,created_at,backup_confirmed_at) VALUES(?,?,?,?,?,?,?,?,?)',
                 ('synthetic-legacy-wallet', address, 'evm', 'testnet', 84532, ciphertext,
                  'fernet-v1', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'))
    conn.commit()
    conn.close()
    for path in (database, keyfile):
        os.chown(path, CUSTODY, CUSTODY)
        path.chmod(0o600)
    action = ['migrate', '--legacy-db', str(database), '--legacy-master-key', str(keyfile)]
    code, _ = admin(action, 'MIGRATE STOPPED SNAPSHOT')
    check('human migration preserves a real synthetic legacy wallet', code == 0)
    code, _ = admin(action, 'MIGRATE STOPPED SNAPSHOT')
    check('migration safely resumes an already copied wallet', code == 0)
    code, output = admin(['backup', address], 'DISPLAY SECRET')
    values = re.findall(rb'(?m)^0x[0-9a-fA-F]{64}\r?$', output)
    check('migrated secret recovers original public identity', code == 0 and len(values) == 1
          and Account.from_key(values[0].strip().decode()).address == address)
    del secret, key, output, values
    receipt = Path('/var/lib/mangrove-custody/migration-receipt.json')
    command = [PYTHON, '-I', str(ROOT / 'scripts/custody-retire-legacy.py'),
               '--legacy-db', str(database), '--receipt', str(receipt), '--custody-uid', str(CUSTODY)]
    check('agent UID cannot retire legacy ciphertext', run_as(AGENT, command).returncode != 0)
    changed = sqlite3.connect(database)
    changed.execute("UPDATE wallets SET encrypted_secret=?", (b'tampered-ciphertext',))
    changed.commit()
    changed.close()
    check('retirement refuses a changed source snapshot', run_as(0, command).returncode != 0)
    changed = sqlite3.connect(database)
    changed.execute('UPDATE wallets SET encrypted_secret=?', (ciphertext,))
    changed.commit()
    changed.close()
    check('root retirement accepts verified protected snapshot', run_as(0, command).returncode == 0)
    conn = sqlite3.connect(database)
    row = conn.execute('SELECT address,encrypted_secret,encryption_method FROM wallets').fetchone()
    conn.close()
    check('retired database retains address without wallet ciphertext', row == (address, b'', 'external-custody'))
    check('retired active DB bytes omit original ciphertext', ciphertext not in database.read_bytes())
    check('shared legacy master remains for unrelated encrypted records', keyfile.exists())


def exercise_auth_disabled():
    import httpx

    directory('/etc/mangrove-disabled-test/config')
    directory('/var/lib/mangrove-disabled-test', AGENT, 0o700)
    config = json.loads(Path('/etc/mangrove-agent-test/config/security-test-config.json').read_text())
    config.update(AUTH_ENABLED=False, DB_PATH='/var/lib/mangrove-disabled-test/agent.db',
                  MASTER_KEY_PATH='/var/lib/mangrove-disabled-test/master.key')
    write_json('/etc/mangrove-disabled-test/config/security-test-config.json', config)
    env = dict(os.environ, ENVIRONMENT='security-test', MANGROVE_AGENT_HOME='/etc/mangrove-disabled-test')
    process = start(AGENT, [PYTHON, '-m', 'uvicorn', 'src.app:app', '--host', '127.0.0.1', '--port', '19081'],
                    env, '/var/log/mangrove-security-test/auth-disabled.log')
    for _ in range(400):
        try:
            if httpx.get('http://127.0.0.1:19081/health', timeout=.3).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if process.poll() is not None:
            raise AssertionError('auth-disabled test service startup')
        time.sleep(.05)
    with httpx.Client(base_url='http://127.0.0.1:19081', timeout=10) as http:
        check('disabled auth cannot export wallet', http.get('/api/v1/agent/wallet/' + WALLET + '/reveal').status_code in (401, 403, 410))
        check('disabled auth cannot create wallet', http.post('/api/v1/agent/wallet/create', json={'chain': 'evm', 'network': 'testnet', 'chain_id': 84532}).status_code in (401, 403))


def main():
    if sys.platform != 'linux' or os.geteuid() != 0 or os.environ.get('MANGROVE_SECURITY_E2E') != '1':
        raise SystemExit('Run only as the administrator of the disposable custody security-test container.')
    sys.path.insert(0, str(ROOT / 'server'))
    success = False
    try:
        settings = provision()
        exercise(settings)
        success = True
    except Exception as error:
        # Do not serialize exception messages/locals: administrator terminal output
        # and generated credentials intentionally stay inside this test process.
        RESULTS.append({'case': 'harness completed', 'passed': False, 'error_type': type(error).__name__})
    finally:
        for process in reversed(PROCESSES):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        print(json.dumps({'success': success, 'cases': RESULTS}, indent=2))
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
