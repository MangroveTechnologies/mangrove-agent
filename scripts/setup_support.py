"""Internal helpers for setup.sh. No application imports or implicit wallet actions."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import warnings
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'server/src/config/local-config.json'
EXAMPLE = ROOT / 'server/src/config/local-example-config.json'
PUBLISHED = {'dev-key-1', 'GENERATED_BY_SETUP'}
PLACEHOLDER = 'REPLACE_WITH_YOUR_DEV_OR_PROD_KEY'
NETWORKS = {'eip155:84532': (84532, 'testnet', 'Base Sepolia (test USDC)'),
            'eip155:8453': (8453, 'mainnet', 'Base mainnet (real USDC)')}


class SetupError(Exception):
    pass


def read_config(path=CONFIG):
    if path.is_symlink():
        raise SetupError('Config must be a regular file, not a symlink.')
    try:
        cfg = json.loads(path.read_text())
        if not isinstance(cfg, dict):
            raise ValueError
        return cfg
    except (OSError, ValueError):
        raise SetupError('Cannot read configuration JSON; repair it before running setup.') from None


def atomic_config(cfg, path=CONFIG):
    if path.is_symlink():
        raise SetupError('Refusing to replace a symlink config.')
    fd, name = tempfile.mkstemp(prefix='local-config.json.setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.chmod(name, 0o600)
            json.dump(cfg, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def upstream_key(cfg):
    value = cfg.get('MANGROVE_API_KEY')
    if value is None:
        return ''
    if not isinstance(value, str):
        raise SetupError('MANGROVE_API_KEY must be a string or null.')
    value = value.strip()
    if not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SetupError('MANGROVE_API_KEY contains invalid characters.')
    return '' if value.lower() in {'', 'none', 'null'} or value == PLACEHOLDER else value


def local_key(cfg):
    value = cfg.get('API_KEYS')
    if not isinstance(value, str):
        raise SetupError('API_KEYS must be a comma-separated string.')
    keys = [k.strip() for k in value.split(',') if k.strip()]
    if not keys or any(k in PUBLISHED for k in keys):
        raise SetupError('Local credentials are missing or published defaults; run setup first.')
    if any(not k.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in k) for k in keys):
        raise SetupError('Local credentials contain invalid characters.')
    return keys[0]


def origin(value):
    try:
        u = urllib.parse.urlsplit(value)
        host = '127.0.0.1' if u.hostname == 'localhost' else u.hostname
        if (u.scheme != 'http' or not ipaddress.ip_address(host).is_loopback
                or u.username is not None or u.password is not None
                or u.query or u.fragment or u.path not in {'', '/'}):
            raise ValueError
        port = u.port or 80
        authority = f'[{host}]' if ':' in host else host
        return f'http://{authority}:{port}'
    except (ValueError, TypeError):
        raise SetupError('Agent URL must be an HTTP loopback origin with no path or credentials.') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(cfg, path, payload=None, *, authenticated=True):
    url = origin(cfg.get('LOCAL_AGENT_URL', '')) + path
    headers = {'Content-Type': 'application/json'}
    if authenticated:
        headers['X-API-Key'] = local_key(cfg)
    req = urllib.request.Request(url, headers=headers,
                                 data=None if payload is None else json.dumps(payload).encode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    # Do not retry mutations: a lost response is not evidence of a failed operation.
    try:
        with opener.open(req, timeout=10) as response:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise SetupError('Agent response exceeded the setup limit.')
            return json.loads(data)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise SetupError(f'Agent returned HTTP {code}; check local authentication and server logs.') from None
    except (OSError, ValueError):
        raise SetupError('No valid response from the local agent. For wallet operations, list wallets before retrying.') from None


def require_auth(cfg):
    if cfg.get('AUTH_ENABLED') is not True:
        raise SetupError('Setup requires AUTH_ENABLED=true to protect local wallet operations.')
    local_key(cfg)


def check(cfg):
    require_auth(cfg)
    wallets = request(cfg, '/api/v1/agent/wallet/list')
    if not isinstance(wallets, list):
        raise SetupError('Agent returned an invalid wallet list.')
    try:
        request(cfg, '/api/v1/agent/wallet/list', authenticated=False)
    except SetupError as exc:
        if not str(exc).startswith(('Agent returned HTTP 401;', 'Agent returned HTTP 403;')):
            raise
    else:
        raise SetupError('Agent accepted unauthenticated wallet access; refusing to report readiness.')
    return wallets


def hidden(prompt):
    if not sys.stdin.isatty():
        raise SetupError('Secret entry requires an interactive terminal. No secret was read.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt).strip()


def choose(prompt, options, default):
    if not sys.stdin.isatty():
        raise SetupError('Use --yes and explicit options when running without an interactive terminal.')
    print(prompt)
    for key, label in options.items():
        print(f'  {key}. {label}')
    while True:
        value = input(f'Choice [{default}]: ').strip() or default
        if value in options:
            return value
        print('Choose one of the listed options.')


def configure(args):
    exists = CONFIG.exists()
    cfg = read_config(CONFIG if exists else EXAMPLE)
    old = dict(cfg)
    key = upstream_key(cfg)
    mode = args.auth
    if args.api_key_stdin:
        supplied = sys.stdin.read().strip()
        if not supplied or upstream_key({'MANGROVE_API_KEY': supplied}) != supplied:
            raise SetupError('Provide a nonempty API key, not a placeholder.')
        key, mode = supplied, 'api-key'
    # Interactive runs always offer both modes; Enter preserves the current mode.
    if mode is None:
        if not args.yes:
            mode = {'1': 'api-key', '2': 'x402'}[choose(
                'How would you like to access MangroveAI?',
                {'1': 'Use an API key', '2': 'Pay with a wallet (x402; no signup)'},
                '1' if key else '2')]
        else:
            mode = 'api-key' if key else 'x402'
    if mode == 'api-key' and not key:
        if args.yes:
            raise SetupError('API-key mode needs a configured key; run interactively with --auth api-key.')
        key = hidden('MangroveAI API key (hidden): ')
        if not key or not upstream_key({'MANGROVE_API_KEY': key}):
            raise SetupError('A real API key is required for API-key mode.')
    cfg['MANGROVE_API_KEY'] = key if mode == 'api-key' else ''
    raw = cfg.get('API_KEYS', '')
    if raw is None:
        raw = ''
    if not isinstance(raw, str):
        raise SetupError('API_KEYS must be a comma-separated string.')
    kept = [k.strip() for k in raw.split(',') if k.strip() and k.strip() not in PUBLISHED]
    cfg['API_KEYS'] = ','.join(kept) if kept else secrets.token_urlsafe(32)
    require_auth(cfg)
    cfg['LOCAL_AGENT_URL'] = origin(args.url)
    if args.markets_url:
        parsed = urllib.parse.urlsplit(args.markets_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise SetupError('Markets URL must be HTTP(S) without credentials.')
        cfg['MANGROVEMARKETS_BASE_URL'] = args.markets_url
    elif cfg.get('MANGROVEMARKETS_BASE_URL') == 'http://localhost:9081':
        cfg['MANGROVEMARKETS_BASE_URL'] = 'https://mangrovemarkets-pcqgpciucq-uc.a.run.app'
    if not exists or cfg != old:
        atomic_config(cfg)
    else:
        CONFIG.chmod(0o600)
    print(f'Access mode: {mode}. Local authentication remains enabled.')
    if mode == 'api-key':
        print('Upstream API-key validity/quota have not been checked. Failed keys never trigger wallet payments.')
    else:
        print('Setup will not create/import a wallet, confirm a backup, or make a payment.')


def network(cfg):
    try:
        return NETWORKS[cfg['X402_NETWORK']]
    except (KeyError, TypeError):
        raise SetupError('Choose a supported X402_NETWORK in configuration before wallet onboarding.') from None


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', value) or int(value[2:], 16) == 0:
        raise SetupError('Agent returned an invalid wallet address.')
    return value


def guide(cfg, *, yes=False, docker=False):
    if upstream_key(cfg):
        print('Open Claude in this directory. MangroveAI calls use your API key.')
        return
    chain, _, label = network(cfg)
    suffix = ' --docker' if docker else ''
    cmd = './scripts/setup.sh'
    selected = '4' if yes else choose('Payment wallet instructions (nothing is executed):', {
        '1': 'Create a new wallet', '2': 'Import an existing wallet',
        '3': 'Use a wallet already saved in this agent', '4': 'Finish wallet setup later'}, '4')
    print(f'Configured payment network: {label} (chain {chain}).')
    print('Payment readiness is not verified. No funding or paid request was checked.')
    if selected == '4':
        print('Step 1. Resume later - rerun setup when you are ready for wallet instructions.')
        print(f'  {cmd}{suffix}')
    else:
        steps = []
        if selected == '1':
            steps.append(('Create your wallet - creates an encrypted local wallet and prints its public address.',
                          f'{cmd} --wallet create'))
        elif selected == '2':
            steps.append(('Import your wallet - securely saves your existing wallet in this agent.',
                          f'{cmd} --wallet import'))
        else:
            steps.append(('List saved wallets - shows addresses on this network and their backup status.',
                          f'{cmd} --wallet list'))
        if selected != '2':
            steps.extend([
                ('Save a backup - reveals the secret in your terminal so you can save it securely; skip if already backed up.',
                 './scripts/reveal-secret.sh --address WALLET_ADDRESS'),
                ('Confirm your backup - records that you have saved the secret outside this agent; skip if already confirmed.',
                 './scripts/confirm-backup.sh WALLET_ADDRESS'),
            ])
        steps.extend([
            ('Select the payer - choose the saved wallet and review its spending limit.', f'{cmd} --wallet select'),
            ('Apply the settings - restarts the agent if needed and verifies local access.', f'{cmd} --yes{suffix}'),
            (f'Fund the selected address - send USDC on {label} only after checking the service accepts this network.', None),
            ('Open Claude - start with your wallet list and spending status before requesting a paid tool.', 'claude'),
        ])
        print('\nRun each command yourself in this checkout, in order:')
        for number, (description, command) in enumerate(steps, 1):
            print(f'\nStep {number}. {description}')
            if command:
                print(f'  {command}')
            if selected == '2' and number == 1:
                print('  Type IMPORT, then BACKED UP if you have saved the secret safely.')
                print('  Enter your private key or recovery phrase only at the hidden terminal prompt.')
                print('  No address entry is needed: the agent derives and displays it. Check it matches your wallet.')
        if selected != '2':
            print('\nReplace WALLET_ADDRESS with the public address from the wallet command.')
    print('\nThese setup commands do not transfer funds or make a paid test call.')
    print('Base Sepolia uses test USDC; it cannot pay for a Base mainnet service.')
    print('Once configured, paid tool calls can spend automatically within the cap.')
    print('Paper trades are simulated, but their upstream data/backtests may still cost money.')
    print('See docs/setup-x402.md for the complete steps and recovery instructions.')


def wallet_action(action, cfg):
    if not sys.stdin.isatty():
        raise SetupError('Run wallet commands yourself in an interactive terminal.')
    if upstream_key(cfg):
        raise SetupError('These payment-wallet commands require x402 mode; run setup --auth x402 first.')
    wallets = check(cfg)
    chain, net, label = network(cfg)
    print(f'Payment network: {label} (chain {chain}).')
    if action in {'list', 'select'}:
        eligible = []
        for item in wallets:
            addr = address(item.get('address'))
            if item.get('chain') == 'evm' and item.get('chain_id') == chain and item.get('network') == net:
                eligible.append(item)
                print(f'{len(eligible)}. {addr} | backup {"confirmed" if item.get("backup_confirmed_at") else "needed"}')
        if action == 'list':
            if not eligible:
                print('No saved wallet matches this payment network.')
            return
        if not eligible:
            raise SetupError('No matching wallet. Create/import one using the printed instructions first.')
        choice = choose('Select a payment wallet:', {str(i): address(w['address']) for i, w in enumerate(eligible, 1)}, '1')
        item = eligible[int(choice) - 1]
        addr = address(item['address'])
        if not item.get('backup_confirmed_at'):
            raise SetupError('Back up this wallet and run confirm-backup.sh with its address before selecting it.')
        budget = request(cfg, '/api/v1/agent/x402/spend')
        try:
            active_cap = Decimal(str(budget['cap_usd']))
            source = budget['cap_source']
            if not active_cap.is_finite() or active_cap < 0 or source not in {'authorized', 'config', 'default'}:
                raise ValueError
        except (KeyError, InvalidOperation, ValueError, TypeError):
            raise SetupError('Cannot verify the active spending cap; no configuration was changed.') from None
        print(f'Current effective cap: {active_cap} USD ({source}).')
        if budget.get('exhausted'):
            print('The budget is exhausted. Setup will not reset or unlock it.')
        raw = input(f'Spending cap in USD [{active_cap}]: ').strip()
        try:
            cap = Decimal(raw or str(active_cap))
            if not cap.is_finite() or cap <= 0 or cap > Decimal('1000000') or cap != cap.quantize(Decimal('.000001')):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise SetupError('Use a positive cap up to 1000000 USD with at most six decimal places.') from None
        if source == 'authorized' and cap != active_cap:
            raise SetupError('An authorized period cap overrides config. Use the explicit budget-management workflow '
                             'to change it, or rerun selection keeping the displayed active cap. Nothing was saved.')
        print(f'Payer: {addr}\nNetwork: {label}\nConfigured cap: {cap} USD. Existing spend and latches are preserved.')
        print('A previously authorized period cap takes precedence over this config value.\n'
              'Check x402_spend_status in Claude before paid use; setup never resets the budget.')
        if input('Type SELECT to save this payment configuration: ').strip() != 'SELECT':
            raise SetupError('Selection cancelled; configuration unchanged.')
        cfg['X402_PAYER_WALLET'] = addr
        cfg['X402_SPEND_CAP_USD'] = float(cap)
        atomic_config(cfg)
        print('Saved. Run ./scripts/setup.sh --yes (add --docker for Docker) to apply it.')
        print(f'Funding address: {addr}\nAsset/network: USDC on {label}. Funding and receiver compatibility are not verified.')
        return
    print('This imports or creates a wallet only when you confirm below. No payment is made.')
    print('Step 1. Confirm the wallet operation - type the word shown to continue, or Ctrl+C to cancel.')
    if input(f'Type {action.upper()} to {action} a payment wallet: ').strip() != action.upper():
        raise SetupError('Cancelled; no wallet request was sent.')
    payload = {'chain': 'evm', 'network': net, 'chain_id': chain, 'label': 'x402 payments'}
    if action == 'import':
        print('Step 2. Confirm your backup - continue only if the secret is safely saved outside this agent.')
        if input('Type BACKED UP to confirm you have saved its secret outside the agent: ').strip() != 'BACKED UP':
            raise SetupError('Import cancelled; backup was not confirmed.')
        print('Step 3. Enter the wallet secret - paste it at the hidden prompt below, in this terminal only.')
        print('You do not enter an address; the agent derives it from the secret and displays it after import.')
        secret = hidden('Private key or recovery phrase (hidden; never paste into chat): ')
        if not secret:
            raise SetupError('Empty secret; nothing was sent.')
        try:
            result = request(cfg, '/api/v1/agent/wallet/stash-secret', {'secret': secret})
        finally:
            del secret
        token = result.get('vault_token')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', token):
            raise SetupError('Invalid vault handoff; nothing was imported.')
        payload['vault_token'] = token
    result = request(cfg, '/api/v1/agent/wallet/' + action, payload)
    addr = address(result.get('address'))
    # Never print arbitrary server responses, vault tokens or reveal commands.
    print(f'Wallet {action} completed. Public address: {addr}')
    if action == 'import':
        print('Step 4. Check the address above - it should match the wallet you intended to import.')
    if action == 'create':
        print('Save your backup yourself in a private terminal:')
        print(f'  ./scripts/reveal-secret.sh --address {addr}')
        print('Only after saving it:')
        print(f'  ./scripts/confirm-backup.sh {addr}')
    print('Next: select this wallet as payer and review its spending limit:')
    print('  ./scripts/setup.sh --wallet select')
    print('No payment was made.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['configure', 'check', 'verify', 'guide', 'wallet', 'fingerprint', 'headers', 'register'])
    parser.add_argument('--auth', choices=['api-key', 'x402'])
    parser.add_argument('--api-key-stdin', action='store_true')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--docker', action='store_true')
    parser.add_argument('--url', default='http://127.0.0.1:9080')
    parser.add_argument('--markets-url')
    parser.add_argument('--action', choices=['create', 'import', 'list', 'select'])
    args = parser.parse_args()
    try:
        if args.command == 'configure':
            configure(args)
        elif args.command == 'fingerprint':
            digest = hashlib.sha256()
            for path in [CONFIG, ROOT / 'server/requirements.lock', *sorted((ROOT / 'server/src').rglob('*.py'))]:
                digest.update(str(path.relative_to(ROOT)).encode())
                digest.update(path.read_bytes())
            print(digest.hexdigest())
        else:
            cfg = read_config()
            if args.command == 'headers':
                require_auth(cfg)
                expected = origin(cfg.get('LOCAL_AGENT_URL', '')) + '/mcp/'
                if (not stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode)
                        or os.environ.get('CLAUDE_CODE_MCP_SERVER_NAME') != 'mangrove-agent'
                        or os.environ.get('CLAUDE_CODE_MCP_SERVER_URL') != expected):
                    raise SetupError('Headers require a pipe to the configured Claude MCP connection.')
                # Intentional credential protocol output to Claude's stdout pipe,
                # not a log. The guard above rejects terminals and regular files;
                # the configured server name/URL must also match. Do not move this
                # payload into diagnostics or remove it: Claude needs the raw header.
                print(json.dumps({'X-API-Key': local_key(cfg)}))
            elif args.command == 'register':
                check(cfg)
                registration = {'type': 'http', 'url': origin(cfg['LOCAL_AGENT_URL']) + '/mcp/',
                                'headersHelper': shlex.join([sys.executable, str(Path(__file__).resolve()), 'headers'])}
                subprocess.run(['claude', 'mcp', 'remove', 'mangrove-agent', '-s', 'local'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                result = subprocess.run(['claude', 'mcp', 'add-json', '--scope', 'local', 'mangrove-agent',
                                         json.dumps(registration)], capture_output=True, check=False)
                if result.returncode:
                    raise SetupError('Claude MCP registration failed; update Claude Code and retry setup.')
                print('Registration saved. Restart Claude Code here and approve the header helper if prompted.')
            elif args.command in {'check', 'verify'}:
                check(cfg)
                if args.command == 'verify':
                    if request(cfg, '/health', authenticated=False).get('scheduler_running') is not True:
                        raise SetupError('Scheduler is not ready; inspect agent startup logs.')
                    catalog = request(cfg, '/api/v1/agent/tools')
                    names = {item['name'] for item in catalog['tools']}
                    if not {'create_wallet', 'import_wallet', 'list_wallets', 'list_signals'} <= names:
                        raise SetupError('Required onboarding tools are missing from discovery.')
                print('Local wallet access authenticated; unauthenticated access refused. No payment made.')
            elif args.command == 'guide':
                guide(cfg, yes=args.yes, docker=args.docker)
            elif args.command == 'wallet':
                if not args.action:
                    raise SetupError('Choose an explicit wallet action.')
                wallet_action(args.action, cfg)
        return 0
    except SetupError as exc:
        print(f'Setup: {exc}', file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print('Setup cancelled. Rerun to resume; list wallets before retrying a wallet mutation.', file=sys.stderr)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, getpass.GetPassWarning):
        print('Setup could not complete. Check config/permissions and local agent availability; no automatic retry.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
