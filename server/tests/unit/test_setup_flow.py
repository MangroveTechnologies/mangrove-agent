"""Setup tests use isolated checkouts/synthetic HTTP servers, never real wallets."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('setup_support', ROOT / 'scripts/setup_support.py')
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
PLUGIN_PROBE_SPEC = importlib.util.spec_from_file_location(
    'plugin_http_status', ROOT / 'scripts/plugin_http_status.py')
plugin_probe = importlib.util.module_from_spec(PLUGIN_PROBE_SPEC)
PLUGIN_PROBE_SPEC.loader.exec_module(plugin_probe)
ADDR = '0x' + '1' * 40


def arguments(**kwargs):
    return argparse.Namespace(auth=None, api_key_stdin=False, yes=True,
                              url='http://127.0.0.1:9080', markets_url=None, **kwargs)


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / 'local-config.json'
    monkeypatch.setattr(setup, 'CONFIG', path)
    # Defaults on helper function arguments are intentionally not used by configure.
    real_write = setup.atomic_config
    monkeypatch.setattr(setup, 'atomic_config', lambda cfg: real_write(cfg, path))
    return path


def test_fresh_keyless_setup_preserves_security(config):
    setup.configure(arguments())
    cfg = json.loads(config.read_text())
    assert cfg['MANGROVE_API_KEY'] == ''
    assert len(cfg['API_KEYS']) >= 32
    assert cfg['AUTH_ENABLED'] is True
    assert cfg['X402_PAYER_WALLET'] == ''
    assert cfg['X402_NETWORK'] == 'eip155:8453'
    assert cfg['X402_USDC_CONTRACT'] == '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
    assert cfg['X402_RECONCILIATION_RPC_URLS']['eip155:8453'] == 'https://mainnet.base.org'
    assert config.stat().st_mode & 0o777 == 0o600


def test_rerun_keeps_keys_wallet_cap_and_other_settings(config):
    cfg = setup.read_config(setup.EXAMPLE)
    cfg.update(MANGROVE_API_KEY='prod_existing', API_KEYS='private-local-key',
               X402_PAYER_WALLET=ADDR, X402_SPEND_CAP_USD=7, custom={'retained': True})
    config.write_text(json.dumps(cfg))
    setup.configure(arguments())
    after = json.loads(config.read_text())
    for key in ['MANGROVE_API_KEY', 'API_KEYS', 'X402_PAYER_WALLET', 'X402_SPEND_CAP_USD', 'custom']:
        assert after[key] == cfg[key]


def test_explicit_switch_preserves_key_and_selects_wallet_mode(config):
    setup.configure(arguments())
    cfg = json.loads(config.read_text())
    cfg['MANGROVE_API_KEY'] = 'prod_previous'
    config.write_text(json.dumps(cfg))
    args = arguments()
    args.auth = 'x402'
    setup.configure(args)
    after = json.loads(config.read_text())
    assert after['MANGROVE_API_KEY'] == 'prod_previous'
    assert after['MANGROVE_ACCESS_MODE'] == 'x402'
    assert setup.upstream_key(after) == ''
    assert after['API_KEYS'] == cfg['API_KEYS']


def test_api_key_input_is_data_not_python(config, monkeypatch, capsys):
    import io
    key = "prod_'\"\\literal$(not-a-command)"
    monkeypatch.setattr(sys, 'stdin', io.StringIO(key))
    args = arguments()
    args.api_key_stdin = True
    setup.configure(args)
    assert json.loads(config.read_text())['MANGROVE_API_KEY'] == key
    assert key not in capsys.readouterr().out


def test_empty_api_key_does_not_write_config(config):
    args = arguments()
    args.auth = 'api-key'
    with pytest.raises(setup.SetupError, match='needs a configured key'):
        setup.configure(args)
    assert not config.exists()


@pytest.mark.parametrize('value', [False, 'true', None])
def test_auth_disabled_or_ambiguous_is_refused(config, value):
    cfg = setup.read_config(setup.EXAMPLE)
    cfg['AUTH_ENABLED'] = value
    config.write_text(json.dumps(cfg))
    before = config.read_bytes()
    with pytest.raises(setup.SetupError, match='AUTH_ENABLED'):
        setup.configure(arguments())
    assert config.read_bytes() == before


def test_atomic_failure_preserves_original(config, monkeypatch):
    config.write_text('{"old":true}')
    monkeypatch.setattr(os, 'replace', Mock(side_effect=OSError('synthetic failure')))
    with pytest.raises(OSError):
        setup.atomic_config({'new': True})
    assert config.read_text() == '{"old":true}'
    assert not list(config.parent.glob('.setup-*'))


def test_symlink_config_refused(config, tmp_path):
    target = tmp_path / 'target'
    target.write_text('{}')
    config.symlink_to(target)
    with pytest.raises(setup.SetupError, match='symlink'):
        setup.configure(arguments())
    assert target.read_text() == '{}'


@pytest.mark.parametrize('url', ['https://example.com', 'http://example.com', 'http://127.0.0.1/path',
                                 'http://user:pass@localhost', 'http://127.0.0.1?x=1',
                                 'http://127.0.0.1:bad', 'http://127.0.0.1#fragment'])
def test_nonlocal_or_ambiguous_origins_rejected(url):
    with pytest.raises(setup.SetupError):
        setup.origin(url)


@pytest.mark.parametrize('choice,command', [('1', 'create'), ('2', 'import'), ('3', 'list'), ('4', None)])
def test_guide_only_prints_instructions(choice, command, monkeypatch, capsys):
    monkeypatch.setattr(setup, 'choose', lambda *a: choice)
    request = Mock(side_effect=AssertionError('Guide must not call wallet API'))
    monkeypatch.setattr(setup, 'request', request)
    cfg = setup.read_config(setup.EXAMPLE)
    cfg['MANGROVE_API_KEY'] = ''
    setup.guide(cfg)
    output = capsys.readouterr().out
    assert 'Payment readiness is not verified' in output
    if command == 'import':
        assert 'custody-service.py admin --config POLICY import' in output
        assert '--wallet import' not in output
    elif command:
        assert f'--wallet {command}' in output
    assert not request.called


@pytest.fixture
def interactive(monkeypatch):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)


def wallet_config():
    return {'MANGROVE_API_KEY': '', 'AUTH_ENABLED': True, 'API_KEYS': 'synthetic-local-key',
            'X402_NETWORK': 'eip155:84532', 'X402_SPEND_CAP_USD': 25,
            'LOCAL_AGENT_URL': 'http://127.0.0.1:9080'}


def test_create_uses_configured_chain_and_no_automatic_backup(interactive, monkeypatch, capsys):
    monkeypatch.setattr(setup, 'check', lambda cfg: [])
    monkeypatch.setattr('builtins.input', lambda _: 'CREATE')
    wire = Mock(return_value={'address': ADDR, 'vault_token': 'do-not-display', 'secret': 'do-not-display'})
    monkeypatch.setattr(setup, 'request', wire)
    setup.wallet_action('create', wallet_config())
    assert wire.call_count == 1
    assert wire.call_args.args[1] == '/api/v1/agent/wallet/create'
    assert wire.call_args.args[2]['chain_id'] == 84532
    assert 'do-not-display' not in capsys.readouterr().out


def test_retired_import_never_prompts_for_secret_or_sends_http(interactive, monkeypatch, capsys):
    monkeypatch.setattr(setup, 'check', lambda cfg: [])
    monkeypatch.setattr('builtins.input', lambda _: 'IMPORT')
    hidden = Mock(side_effect=AssertionError('Secret prompt forbidden'))
    wire = Mock(side_effect=AssertionError('Import HTTP forbidden'))
    before = Mock(side_effect=AssertionError('No mutation checkpoint needed'))
    monkeypatch.setattr(setup, 'hidden', hidden)
    monkeypatch.setattr(setup, 'request', wire)
    with pytest.raises(setup.SetupError, match='isolated custody administrator'):
        setup.wallet_action('import', wallet_config(), before_mutation=before)
    hidden.assert_not_called()
    wire.assert_not_called()
    before.assert_not_called()
    assert 'vault_token' not in capsys.readouterr().out


def test_select_requires_backup_before_config_mutation(interactive, monkeypatch):
    monkeypatch.setattr(setup, 'check', lambda cfg: [dict(address=ADDR, chain='evm', chain_id=84532,
                                                        network='testnet', backup_confirmed_at=None)])
    monkeypatch.setattr(setup, 'choose', lambda *a: '1')
    write = Mock()
    monkeypatch.setattr(setup, 'atomic_config', write)
    with pytest.raises(setup.SetupError, match='custody administrator must back up'):
        setup.wallet_action('select', wallet_config())
    assert not write.called


def test_select_preserves_ledger_and_saves_only_config(interactive, monkeypatch):
    monkeypatch.setattr(setup, 'check', lambda cfg: [dict(address=ADDR, chain='evm', chain_id=84532,
                                                        network='testnet', backup_confirmed_at='2026-09-18')])
    monkeypatch.setattr(setup, 'choose', lambda *a: '1')
    replies = iter(['5', 'SELECT'])
    monkeypatch.setattr('builtins.input', lambda _: next(replies))
    write = Mock()
    monkeypatch.setattr(setup, 'atomic_config', write)
    wire = Mock(return_value={'cap_usd': 25, 'cap_source': 'config', 'exhausted': False})
    monkeypatch.setattr(setup, 'request', wire)
    setup.wallet_action('select', wallet_config())
    assert write.call_args.args[0]['X402_SPEND_CAP_USD'] == 5
    assert write.call_args.args[0]['X402_PAYER_WALLET'] == ADDR
    assert wire.call_count == 1
    assert wire.call_args.args[1] == '/api/v1/agent/x402/spend'


@pytest.fixture
def wire_server():
    events = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            events.append((self.path, self.headers.get('X-API-Key')))
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/leak')
                self.end_headers()
                return
            authenticated = self.headers.get('X-API-Key') == 'synthetic-local-key'
            self.send_response(200 if authenticated else 401)
            self.end_headers()
            self.wfile.write(b'[]' if authenticated else b'{"error":"sensitive remote message"}')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, events
    server.shutdown()
    server.server_close()
    thread.join()


def test_check_proves_auth_without_paid_calls(wire_server, monkeypatch):
    server, events = wire_server
    cfg = wallet_config()
    cfg['LOCAL_AGENT_URL'] = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    setup.check(cfg)
    assert events == [('/api/v1/agent/wallet/list', 'synthetic-local-key'),
                      ('/api/v1/agent/wallet/list', None)]


def test_redirect_not_followed_or_remote_error_printed(wire_server):
    server, events = wire_server
    cfg = wallet_config()
    cfg['LOCAL_AGENT_URL'] = f'http://127.0.0.1:{server.server_port}'
    with pytest.raises(setup.SetupError, match='HTTP 302'):
        setup.request(cfg, '/redirect')
    assert len(events) == 1


def test_verify_waits_for_a_cold_catalog_without_retrying(monkeypatch):
    events = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            events.append(self.path)
            authenticated = self.headers.get('X-API-Key') == 'synthetic-local-key'
            if self.path == '/api/v1/agent/wallet/list':
                code, body = (200, []) if authenticated else (401, {})
            elif self.path == '/health':
                code, body = 200, {'scheduler_running': True}
            else:
                assert self.path == '/api/v1/agent/tools'
                # Reproduce a healthy discovery exceeding the old 10s deadline.
                time.sleep(10.2)
                code, body = 200, {'tools': [{'name': name} for name in (
                    'create_wallet', 'import_wallet', 'list_wallets', 'list_signals')]}
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = dict(wallet_config(), MANGROVE_ACCESS_MODE='x402',
               LOCAL_AGENT_URL=f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setattr(setup, 'read_config', lambda: cfg)
    monkeypatch.setattr(sys, 'argv', ['setup_support.py', 'verify'])
    try:
        assert setup.main() == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert events == ['/api/v1/agent/wallet/list', '/api/v1/agent/wallet/list',
                      '/health', '/api/v1/agent/tools']


@pytest.fixture
def checkout(tmp_path):
    """Run actual setup.sh against a fake uvicorn module, without pip/network installs."""
    repo = tmp_path / 'checkout with spaces'
    (repo / 'scripts').mkdir(parents=True)
    (repo / 'server/src/config').mkdir(parents=True)
    (repo / '.claude').mkdir()
    for name in ['setup.sh', 'setup_support.py', 'verify_quickstart.sh', 'setup-mcp.sh',
                 'agent_context.py', 'local_mcp_bridge.py', 'update.sh', 'mangrove-agent']:
        shutil.copy2(ROOT / 'scripts' / name, repo / 'scripts' / name)
    for name in ['SAGE.md', '.claude/rules/trading-bot-workflow.md',
                 '.claude/rules/wallet-presentation.md', '.claude/rules/risk-management.md']:
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    for name in ('local-example-config.json', 'mangrove-endpoints.json'):
        shutil.copy2(ROOT / 'server/src/config' / name, repo / 'server/src/config' / name)
    (repo / 'server/requirements.lock').write_text('# synthetic lockfile\n')
    venv = repo / '.venv/bin'
    venv.mkdir(parents=True)
    (venv / 'activate').write_text(f'export PATH="{venv}:$PATH"\n')
    wrapper = venv / 'python3'
    wrapper.write_text(f'#!/bin/bash\nif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit 0; fi\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    (repo / 'server/uvicorn.py').write_text('''
import json, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
cfg = json.loads(Path('server/src/config/local-config.json').read_text())
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with open('requests.jsonl', 'a') as f:
            f.write(json.dumps({'path': self.path, 'method': message['method'],
                               'tool': message.get('params', {}).get('name')})+'\\n')
        if self.headers.get('X-API-Key') != cfg['API_KEYS'].split(',')[0]:
            self.send_response(401); self.end_headers(); return
        if 'id' not in message:
            self.send_response(202); self.end_headers(); return
        if message['method'] == 'initialize':
            result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'mangrove-agent', 'version': 'synthetic'}}
        elif message['method'] == 'tools/list':
            result = {'tools': [{'name': 'status', 'inputSchema': {'type': 'object'}}]}
        else:
            result = {'content': [{'type': 'text', 'text': '{"ready":true}'}], 'isError': False}
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode())
    def do_GET(self):
        with open('requests.jsonl', 'a') as f: f.write(json.dumps({'path':self.path})+'\\n')
        valid = self.headers.get('X-API-Key') == cfg['API_KEYS'].split(',')[0]
        if self.path == '/api/v1/users/me/metrics':
            metrics_valid = self.headers.get('Authorization') == f"Bearer {cfg['MANGROVE_API_KEY']}"
            body, code = ({'success': True, 'metrics': {}}, 200) if metrics_valid else ({}, 401)
        elif self.path.endswith('/tools'):
            body = {'tools':[{'name':n} for n in ['create_wallet','import_wallet','list_wallets','list_signals']]}
            if Path('catalog.json').exists(): body = json.loads(Path('catalog.json').read_text())
            code = 200
        elif self.path == '/health': body, code = {'status': 'healthy', 'scheduler_running': True}, 200
        elif self.path.endswith('/marketplace/readiness'):
            body, code = {'ownership_discovery': 'ready'}, 200 if valid else 401
        else: body, code = [], 200 if valid else 401
        self.send_response(code); self.end_headers(); self.wfile.write(json.dumps(body).encode())
HTTPServer(('127.0.0.1', int(sys.argv[sys.argv.index('--port')+1])), Handler).serve_forever()
''')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    example_path = repo / 'server/src/config/local-example-config.json'
    example_config = json.loads(example_path.read_text())
    example_config['MANGROVEAI_BASE_URL'] = f'http://127.0.0.1:{port}/api/v1'
    example_path.write_text(json.dumps(example_config))
    # Never touch the developer's real PATH directory or shell startup files.
    env = dict(os.environ, BARE_PORT=str(port), MANGROVE_AGENT_BIN_DIR=str(tmp_path / 'bin'))
    for key in ['BASE_URL', 'BARE_HOST', 'MANGROVE_AGENT_HOME', 'PYTHONPATH', 'MANGROVE_AGENT_UPDATED']:
        env.pop(key, None)
    yield repo, env
    pid = repo / 'agent-data/bare.pid'
    if pid.exists():
        try:
            os.kill(int(pid.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            # Setup may have already stopped the mock server before teardown.
            pass


def run_setup(checkout, *args, input=None):
    repo, env = checkout
    return subprocess.run(['bash', str(repo / 'scripts/setup.sh'), *args], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=25, input=input)



def test_setup_installs_cli_link_and_never_clobbers_foreign_files(checkout):
    repo, env = checkout
    bin_dir = Path(env['MANGROVE_AGENT_BIN_DIR'])
    result = run_setup(checkout, '--yes', '--no-mcp', '--no-verify')
    assert result.returncode == 0, result.stdout + result.stderr
    link = bin_dir / 'mangrove-agent'
    assert link.is_symlink() and link.resolve() == (repo / 'scripts/mangrove-agent').resolve()
    assert 'mangrove-agent start' in result.stdout
    where = subprocess.run([str(link), 'where'], capture_output=True, text=True, check=True)
    assert Path(where.stdout.strip()) == repo.resolve()
    # A file setup did not create stays untouched on rerun.
    link.unlink()
    link.write_text('#!/bin/sh\necho mine\n')
    result = run_setup(checkout, '--yes', '--no-mcp', '--no-verify')
    assert result.returncode == 0, result.stdout + result.stderr
    assert link.read_text() == '#!/bin/sh\necho mine\n'
    assert 'left unchanged' in result.stdout

def test_agent_install_loads_sage_and_calls_status_without_claude(checkout):
    repo, env = checkout
    binary = repo / 'no-client-bin'
    binary.mkdir()
    (binary / 'python3.13').symlink_to(sys.executable)
    env = dict(env, PATH=f'{binary}:/usr/bin:/bin')
    installed = run_setup((repo, env), '--yes', '--no-mcp')
    assert installed.returncode == 0, installed.stderr + installed.stdout
    assert 'Install Claude CLI' not in installed.stdout
    assert 'scripts/local_mcp_bridge.py --call status' in installed.stdout
    python = str(repo / '.venv/bin/python3')
    policy = subprocess.run([python, str(repo / 'scripts/agent_context.py'), '--stdout'],
                            cwd=repo.parent, capture_output=True, text=True, timeout=10)
    assert policy.returncode == 0, policy.stderr
    assert policy.stdout.count('## SOURCE: ') == 4
    assert '**Name:** Unset' in policy.stdout
    outputs = policy.stdout + installed.stdout + installed.stderr
    for options in [['--list-tools'], ['--call', 'status']]:
        result = subprocess.run([python, str(repo / 'scripts/local_mcp_bridge.py'), *options],
                                cwd=repo.parent, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout)['result']
        if options[0] == '--list-tools':
            assert response['tools'][0]['name'] == 'status'
        else:
            assert json.loads(response['content'][0]['text']) == {'ready': True}
        outputs += result.stdout + result.stderr
    records = [json.loads(line) for line in (repo / 'requests.jsonl').read_text().splitlines()]
    assert [r['tool'] for r in records if r.get('method') == 'tools/call'] == ['status']
    cfg = json.loads((repo / 'server/src/config/local-config.json').read_text())
    assert cfg['MANGROVE_API_KEY'] == ''
    assert cfg['API_KEYS'] not in outputs


@pytest.mark.skipif(os.name != 'posix', reason='POSIX command-group cleanup')
def test_agent_survives_installing_command_process_group_cleanup(checkout):
    repo, env = checkout
    proc = subprocess.Popen(['bash', str(repo / 'scripts/setup.sh'), '--yes', '--no-mcp'],
                            cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    stdout, stderr = proc.communicate(timeout=25)
    assert proc.returncode == 0, stdout + stderr
    pid = int((repo / 'agent-data/bare.pid').read_text())
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        # The installing command's group may already be gone after it exits.
        pass
    assert os.getsid(pid) == pid
    cfg = json.loads((repo / 'server/src/config/local-config.json').read_text())
    assert setup.check(cfg) == []  # Still reachable from a later command.


@pytest.mark.parametrize('option', [None, '--yes'])
def test_setup_does_not_open_chat_even_when_interactive(checkout, option):
    import pty
    import select
    import time

    repo, env = checkout
    first = run_setup(checkout, '--yes', '--no-mcp', '--no-verify')
    assert first.returncode == 0, first.stderr
    original_pid = (repo / 'agent-data/bare.pid').read_text()
    shutil.copytree(ROOT / '.claude/hooks', repo / '.claude/hooks')
    binary = repo / 'fake-bin'
    binary.mkdir()
    capture = repo / 'chat-capture.json'
    claude = binary / 'claude'
    claude.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
if sys.argv[1] == 'mcp':
    sys.exit(0)
args = sys.argv[1:]
pathlib.Path(os.environ['CHAT_CAPTURE']).write_text(json.dumps(args))
''')
    claude.chmod(0o700)
    env = dict(env, PATH=f'{binary}{os.pathsep}{env["PATH"]}', CHAT_CAPTURE=str(capture))
    master, slave = pty.openpty()
    command = ['bash', str(repo / 'scripts/setup.sh'), '--auth', 'x402', '--no-verify', '--robinhood', 'skip']
    if option:
        command.append(option)
    proc = subprocess.Popen(command, cwd=repo, env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    try:
        os.write(master, b'4\n')
        deadline = time.monotonic() + 25
        while proc.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
        assert proc.wait(timeout=3) == 0, output.decode(errors='replace')
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
        os.close(master)
    assert not capture.exists()
    assert (repo / 'agent-data/bare.pid').read_text() == original_pid



def test_shell_fresh_no_key_rerun_and_mode_switch(checkout):
    first = run_setup(checkout, '--yes', '--no-mcp')
    assert first.returncode == 0, first.stdout + first.stderr
    repo, _ = checkout
    path = repo / 'server/src/config/local-config.json'
    cfg = json.loads(path.read_text())
    pid = (repo / 'agent-data/bare.pid').read_text()
    again = run_setup(checkout, '--yes', '--no-mcp')
    assert again.returncode == 0, again.stdout + again.stderr
    assert (repo / 'agent-data/bare.pid').read_text() == pid
    switched = run_setup(checkout, '--yes', '--no-mcp', '--api-key-stdin', input='prod_synthetic')
    assert switched.returncode == 0, switched.stdout + switched.stderr
    assert (repo / 'agent-data/bare.pid').read_text() != pid
    after = json.loads(path.read_text())
    assert after['API_KEYS'] == cfg['API_KEYS']
    assert after['MANGROVE_API_KEY'] == 'prod_synthetic'
    assert 'prod_synthetic' not in switched.stdout + switched.stderr
    assert 'Marketplace ownership discovery verified' in switched.stdout
    requests = [json.loads(line)['path'] for line in (repo / 'requests.jsonl').read_text().splitlines()]
    # The authenticated metrics read is separate from local API-key checks and
    # anonymous MCP catalog discovery.
    assert set(requests) == {'/api/v1/agent/wallet/list', '/api/v1/agent/tools', '/health',
                             '/api/v1/agent/marketplace/readiness',
                             '/api/v1/users/me/metrics'}


# -- upstream API-key check -------------------------------------------------

UPSTREAM_PROBE = 'https://api.mangrovedeveloper.ai/api/v1/users/me/metrics'


def _cfg(mode='api-key'):
    return {'MANGROVE_ACCESS_MODE': mode, 'MANGROVE_API_KEY': 'prod_' + 'a' * 48}


def test_check_upstream_reports_a_working_key(monkeypatch, capsys):
    class Response:
        status = 200

        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit): return b'{"success":true,"metrics":{}}'

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == UPSTREAM_PROBE
            assert request.get_header('Authorization') == 'Bearer ' + _cfg()['MANGROVE_API_KEY']
            assert request.get_header('X-api-key') is None
            assert timeout == setup.UPSTREAM_METRICS_TIMEOUT_SECONDS
            return Response()

    monkeypatch.setattr(setup.urllib.request, 'build_opener', lambda *handlers: Opener())
    assert setup.check_upstream(_cfg()) is True
    assert 'verified' in capsys.readouterr().out


def _http_error(code):
    return setup.urllib.error.HTTPError(UPSTREAM_PROBE, code, 'mock', {}, io.BytesIO(b''))


def test_check_upstream_rejects_only_an_invalid_key(monkeypatch):
    class Opener:
        def open(self, request, timeout): raise _http_error(401)
    monkeypatch.setattr(setup.urllib.request, 'build_opener', lambda *handlers: Opener())
    with pytest.raises(setup.SetupError) as exc:
        setup.check_upstream(_cfg())
    assert 'HTTP 401' in str(exc.value)
    assert 'mangrove.io/developer/api-keys' in str(exc.value)


@pytest.mark.parametrize(('code', 'result', 'message'), [
    (403, 'forbidden', 'HTTP 403'),
    (429, 'rate_limited', 'HTTP 429'),
    (503, 'unknown', 'unknown'),
])
def test_check_upstream_classifies_non_auth_failures_separately(monkeypatch, capsys, code, result, message):
    class Opener:
        def open(self, request, timeout): raise _http_error(code)
    monkeypatch.setattr(setup.urllib.request, 'build_opener', lambda *handlers: Opener())
    assert setup.check_upstream(_cfg()) is False
    output = capsys.readouterr().out
    assert message in output
    if result == 'rate_limited':
        assert 'key validity was not verified' in output
    elif result == 'forbidden':
        assert 'scope or account access is unresolved' in output


def test_check_upstream_skips_when_platform_is_unreachable_or_malformed(monkeypatch, capsys):
    class Unreachable:
        def open(self, request, timeout): raise OSError('offline')
    monkeypatch.setattr(setup.urllib.request, 'build_opener', lambda *handlers: Unreachable())
    assert setup.check_upstream(_cfg()) is False
    assert 'unknown' in capsys.readouterr().out

    class MalformedResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit): return b'[]'
    class Malformed:
        def open(self, request, timeout): return MalformedResponse()
    monkeypatch.setattr(setup.urllib.request, 'build_opener', lambda *handlers: Malformed())
    assert setup.check_upstream(_cfg()) is False
    assert 'unknown' in capsys.readouterr().out


def test_check_upstream_has_nothing_to_check_in_wallet_mode(monkeypatch):
    monkeypatch.setattr(setup.urllib.request, 'build_opener', lambda *handlers: pytest.fail('wallet mode must not probe'))
    assert setup.check_upstream(_cfg('x402')) is True


def test_upstream_key_probe_uses_configured_base_without_redirects_or_retries(monkeypatch):
    cfg = dict(_cfg(), MANGROVEAI_BASE_URL='https://api.example.test/api/v1')
    calls = []

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit): return b'{"success":true,"metrics":{}}'

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.get_header('Authorization'),
                          request.get_header('X-api-key'), timeout))
            return Response()

    monkeypatch.setattr(setup.urllib.request, 'build_opener', lambda *handlers: Opener())
    assert setup.probe_upstream_key(cfg) == 'verified'
    assert calls == [('https://api.example.test/api/v1/users/me/metrics',
                      'Bearer ' + cfg['MANGROVE_API_KEY'], None, 5)]


@pytest.mark.parametrize(('prefix', 'environment'), [
    ('dev_', None),
    ('dev_', 'dev'),
    ('dev_', 'prod'),
    ('dev_', 'local'),
    ('test_', None),
])
def test_dev_and_test_keys_never_guess_an_upstream_origin(monkeypatch, prefix, environment):
    monkeypatch.delenv('MANGROVE_BASE_URL', raising=False)
    if environment is None:
        monkeypatch.delenv('MANGROVE_ENVIRONMENT', raising=False)
    else:
        monkeypatch.setenv('MANGROVE_ENVIRONMENT', environment)
    monkeypatch.setattr(setup.urllib.request, 'build_opener',
                        lambda *handlers: pytest.fail('origin validation must happen before network access'))
    cfg = {'MANGROVE_ACCESS_MODE': 'api-key', 'MANGROVE_API_KEY': prefix + 'a' * 48}
    with pytest.raises(setup.SetupError, match='require|requires'):
        setup.probe_upstream_key(cfg)


@pytest.mark.parametrize('environment', ['prod', 'local'])
def test_dev_key_keeps_explicit_origin_when_environment_is_overridden(monkeypatch, environment):
    monkeypatch.setenv('MANGROVE_ENVIRONMENT', environment)
    monkeypatch.setenv('MANGROVE_BASE_URL', 'https://environment.example/api/v1')
    cfg = {
        'MANGROVE_ACCESS_MODE': 'api-key',
        'MANGROVE_API_KEY': 'dev_' + 'a' * 48,
        'MANGROVEAI_BASE_URL': 'https://configured.example/api/v1',
    }
    assert setup.upstream_api_base(cfg) == 'https://configured.example/api/v1'


def test_test_key_can_use_explicit_local_environment(monkeypatch):
    monkeypatch.delenv('MANGROVE_BASE_URL', raising=False)
    monkeypatch.setenv('MANGROVE_ENVIRONMENT', 'local')
    cfg = {'MANGROVE_ACCESS_MODE': 'api-key', 'MANGROVE_API_KEY': 'test_' + 'a' * 48}
    assert setup.upstream_api_base(cfg) == 'http://localhost:5001/api/v1'


def test_plugin_health_requires_current_agent_body(monkeypatch):
    class Response:
        status = 200
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit): return json.dumps(self.body).encode()

    class Opener:
        def __init__(self, body): self.body = body
        def open(self, request, timeout):
            assert timeout == 2
            return Response(self.body)

    monkeypatch.setattr(plugin_probe.urllib.request, 'build_opener',
                        lambda *handlers: Opener({'status': 'healthy', 'scheduler_running': True}))
    assert plugin_probe.healthy_agent('http://127.0.0.1:9080/health') is True
    monkeypatch.setattr(plugin_probe.urllib.request, 'build_opener',
                        lambda *handlers: Opener({'scheduler_running': True}))
    assert plugin_probe.healthy_agent('http://127.0.0.1:9080/health') is False
    monkeypatch.setattr(plugin_probe.urllib.request, 'build_opener',
                        lambda *handlers: Opener({'status': 'healthy', 'scheduler_running': False}))
    assert plugin_probe.healthy_agent('http://127.0.0.1:9080/health') is False


def test_plugin_health_uses_real_local_http_and_does_not_follow_redirects():
    events = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_GET(self):
            events.append(self.path)
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/health')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"healthy","scheduler_running":true}')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        assert plugin_probe.healthy_agent(base + '/health') is True
        assert plugin_probe.healthy_agent(base + '/redirect') is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert events == ['/health', '/redirect']


def test_plugin_health_requires_its_recorded_process_root(monkeypatch, tmp_path):
    pid_file = tmp_path / 'agent.pid'
    root_file = tmp_path / 'agent.root'
    pid_file.write_text('123')
    root_file.write_text('/plugin/cache/current')
    venv = '/plugin/data/venv'
    command = f'{venv}/bin/python -m uvicorn src.app:app --host 127.0.0.1 --port 9080 --app-dir /plugin/cache/current/server'
    monkeypatch.setattr(plugin_probe.subprocess, 'run',
                        lambda *args, **kwargs: type('Result', (), {'returncode': 0, 'stdout': command})())
    monkeypatch.setattr(plugin_probe, 'healthy_agent', lambda url: True)
    assert plugin_probe.plugin_process('123', str(root_file)) is True
    assert plugin_probe.owned_healthy_agent('http://127.0.0.1:9080/health', str(pid_file),
                                             str(root_file), '/plugin/cache/current') is True
    # A healthy Mangrove process on another port must not be claimed as this PID.
    assert plugin_probe.owned_healthy_agent('http://127.0.0.1:9081/health', str(pid_file),
                                             str(root_file), '/plugin/cache/current') is False
    # Root identity alone remains sufficient when startup intentionally decides
    # whether an old owned PID can be restarted after a port change.
    assert plugin_probe.plugin_process('123', str(root_file)) is True
    assert plugin_probe.owned_healthy_agent('http://127.0.0.1:9080/health', str(pid_file),
                                             str(root_file), '/plugin/cache/foreign') is False
    foreign_command = f'{venv}/bin/python -m http.server 9080 --directory /plugin/cache/current/server'
    monkeypatch.setattr(plugin_probe.subprocess, 'run',
                        lambda *args, **kwargs: type('Result', (), {'returncode': 0, 'stdout': foreign_command})())
    assert plugin_probe.plugin_process('123', str(root_file)) is False


def test_plugin_process_ownership_matches_an_isolated_uvicorn_process(tmp_path, monkeypatch):
    # Linux ps truncates captured command output to COLUMNS unless wide output
    # is explicit. Long plugin cache/data paths must retain the ownership args.
    monkeypatch.setenv('COLUMNS', '80')
    module_dir = tmp_path / 'modules'
    module_dir.mkdir()
    (module_dir / 'uvicorn.py').write_text('''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status":"healthy","scheduler_running":true}')
HTTPServer(('127.0.0.1', int(sys.argv[sys.argv.index('--port') + 1])), Handler).serve_forever()
''')
    root = tmp_path / 'plugin-cache' / 'current'
    (root / 'server').mkdir(parents=True)
    venv = tmp_path / 'plugin-data' / 'venv'
    (venv / 'bin').mkdir(parents=True)
    python = venv / 'bin' / 'python'
    python.symlink_to(sys.executable)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [str(python), '-m', 'uvicorn', 'src.app:app', '--app-dir', str(root / 'server'),
         '--host', '127.0.0.1', '--port', str(port)],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(module_dir)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_file = tmp_path / 'agent.pid'
    root_file = tmp_path / 'agent.root'
    pid_file.write_text(str(process.pid))
    root_file.write_text(str(root))
    url = f'http://127.0.0.1:{port}/health'
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not plugin_probe.healthy_agent(url):
            time.sleep(0.05)
        assert plugin_probe.plugin_process(str(process.pid), str(root_file)) is True
        assert plugin_probe.owned_healthy_agent(url, str(pid_file), str(root_file), str(root)) is True
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_plugin_scripts_no_longer_probe_removed_strategy_route():
    for script in ('plugin-bootstrap.sh', 'plugin-start-agent.sh'):
        content = (ROOT / 'scripts' / script).read_text()
        assert '/api/v1/agent/strategies' not in content
        assert '--owned-health' in content


@pytest.mark.parametrize('missing_local_tool', [False, True])
def test_wallet_mode_reports_unavailable_platform_but_requires_local_tools(checkout, missing_local_tool):
    repo, _ = checkout
    names = ['create_wallet', 'list_wallets']
    if not missing_local_tool:
        names.append('import_wallet')
    (repo / 'catalog.json').write_text(json.dumps({
        'tools': [{'name': name} for name in names], 'mangroveai_status': 'unavailable',
    }))
    result = run_setup(checkout, '--yes', '--no-mcp')
    assert 'MangroveAI tools are unavailable; platform tool access is NOT verified.' in result.stdout
    if missing_local_tool:
        assert result.returncode != 0
        assert 'Required onboarding tools are missing from discovery: import_wallet' in result.stdout
        assert 'Setup complete' not in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'Setup complete' in result.stdout
    requests = [json.loads(line)['path'] for line in (repo / 'requests.jsonl').read_text().splitlines()]
    assert '/api/v1/agent/signals?limit=1' not in requests


def test_shell_verification_failure_is_failure(checkout):
    repo, _ = checkout
    (repo / 'scripts/verify_quickstart.sh').write_text('#!/bin/bash\nexit 42\n')
    result = run_setup(checkout, '--yes', '--no-mcp')
    assert result.returncode != 0
    assert 'Verification failed' in result.stderr
    assert 'Setup complete' not in result.stdout


def test_shell_lock_conflict_does_not_write_config(checkout):
    repo, _ = checkout
    (repo / 'agent-data/.setup.lock').mkdir(parents=True)
    result = run_setup(checkout, '--yes', '--no-mcp')
    assert result.returncode != 0
    assert not (repo / 'server/src/config/local-config.json').exists()


@pytest.mark.parametrize('args', [('--auth', 'bad'), ('--auth', 'x402', '--api-key', 'synthetic'),
                                 ('--api-key',), ('--wallet', 'create', '--yes')])
def test_shell_invalid_options_do_not_mutate(checkout, args):
    repo, _ = checkout
    result = run_setup(checkout, *args)
    assert result.returncode != 0
    assert not (repo / 'server/src/config/local-config.json').exists()


@pytest.mark.parametrize('mode', ['api-key', 'x402'])
def test_interactive_access_choice(config, monkeypatch, mode):
    monkeypatch.setattr(setup, 'choose', lambda *a: '1' if mode == 'api-key' else '2')
    monkeypatch.setattr(setup, 'hidden', lambda _: 'prod_synthetic')
    args = arguments()
    args.yes = False
    setup.configure(args)
    cfg = json.loads(config.read_text())
    assert bool(cfg['MANGROVE_API_KEY']) == (mode == 'api-key')


def test_wallet_commands_refuse_noninteractive_input(monkeypatch):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    wire = Mock()
    monkeypatch.setattr(setup, 'request', wire)
    with pytest.raises(setup.SetupError, match='interactive terminal'):
        setup.wallet_action('create', wallet_config())
    assert not wire.called


def test_readiness_refuses_unprotected_server(monkeypatch):
    monkeypatch.setattr(setup, 'request', lambda *a, **kw: [])
    with pytest.raises(setup.SetupError, match='unauthenticated wallet access'):
        setup.check(wallet_config())


@pytest.mark.parametrize('cap', ['NaN', 'Infinity', '-1', '0', '0.0000001', '1000001'])
def test_invalid_cap_never_changes_configuration(interactive, monkeypatch, cap):
    monkeypatch.setattr(setup, 'check', lambda cfg: [dict(address=ADDR, chain='evm', chain_id=84532,
                                                        network='testnet', backup_confirmed_at='2026-09-18')])
    monkeypatch.setattr(setup, 'choose', lambda *a: '1')
    monkeypatch.setattr('builtins.input', lambda _: cap)
    monkeypatch.setattr(setup, 'request', lambda *a: {'cap_usd': 25, 'cap_source': 'config'})
    write = Mock()
    monkeypatch.setattr(setup, 'atomic_config', write)
    with pytest.raises(setup.SetupError, match='positive cap'):
        setup.wallet_action('select', wallet_config())
    assert not write.called


def test_custom_port_persisted_for_plain_rerun(checkout):
    result = run_setup(checkout, '--yes', '--no-mcp')
    assert result.returncode == 0, result.stderr
    repo, env = checkout
    original_port = env.pop('BARE_PORT')
    result = run_setup(checkout, '--yes', '--no-mcp')
    assert result.returncode == 0, result.stderr
    assert f'127.0.0.1:{original_port}' in result.stdout


def test_foreground_completes_checks_and_cleans_up_on_term(checkout):
    repo, env = checkout
    proc = subprocess.Popen(['bash', str(repo / 'scripts/setup.sh'), '--foreground', '--yes', '--no-mcp'],
                            cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # Bound the read: the test runner must never hang if startup regresses.
    timer = threading.Timer(20, proc.terminate)
    timer.start()
    output = []
    try:
        for line in proc.stdout:
            output.append(line)
            if 'Agent attached to this terminal' in line:
                proc.terminate()
                break
        proc.communicate(timeout=15)
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert 'local authentication verified' in ''.join(output)
    assert 'Setup complete' in ''.join(output)
    assert not (repo / 'agent-data/.setup.lock').exists()
    assert not (repo / 'agent-data/bare.pid').exists()


def test_help_does_not_initialize_state(checkout):
    repo, _ = checkout
    result = run_setup(checkout, '--help')
    assert result.returncode == 0
    assert not (repo / 'agent-data').exists()


def test_unknown_live_pid_is_never_killed(checkout):
    repo, _ = checkout
    (repo / 'agent-data').mkdir()
    (repo / 'agent-data/bare.pid').write_text(str(os.getpid()))
    try:
        result = run_setup(checkout, '--yes', '--no-mcp')
    finally:
        # Never let fixture cleanup target the pytest process, even on failure.
        (repo / 'agent-data/bare.pid').unlink()
    assert result.returncode != 0
    assert 'cannot be identified' in result.stderr


def test_mcp_registration_uses_local_key_and_saved_port(checkout):
    repo, _ = checkout
    cli = repo / '.venv/bin/claude'
    cli.write_text(f'#!{sys.executable}\nimport json,sys\nwith open("claude-args.jsonl","a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
    cli.chmod(0o755)
    # Preflight runs before activation, so expose this test-only CLI there too.
    checkout[1]['PATH'] = str(cli.parent) + os.pathsep + checkout[1]['PATH']
    result = run_setup(checkout, '--yes')
    assert result.returncode == 0, result.stdout + result.stderr
    cfg = json.loads((repo / 'server/src/config/local-config.json').read_text())
    calls = [json.loads(line) for line in (repo / 'claude-args.jsonl').read_text().splitlines()]
    assert cfg['API_KEYS'] not in json.dumps(calls)
    registration = json.loads(calls[-1][-1])
    assert registration['url'] == cfg['LOCAL_AGENT_URL'] + '/mcp/'
    env = dict(checkout[1], CLAUDE_CODE_MCP_SERVER_NAME='mangrove-agent',
               CLAUDE_CODE_MCP_SERVER_URL=registration['url'])
    import shlex
    helper = subprocess.run(shlex.split(registration['headersHelper']), env=env,
                            capture_output=True, text=True, check=True)
    assert json.loads(helper.stdout) == {'X-API-Key': cfg['API_KEYS']}
    # Even correct MCP metadata must not allow a credential dump to a log file.
    output_file = repo / 'headers-output.log'
    with output_file.open('w') as stream:
        denied_file = subprocess.run(shlex.split(registration['headersHelper']), env=env,
                                     stdout=stream, stderr=subprocess.PIPE, text=True)
    assert denied_file.returncode != 0
    assert output_file.read_text() == ''
    assert cfg['API_KEYS'] not in denied_file.stderr
    if os.name != 'nt':
        import pty

        master, terminal = pty.openpty()
        try:
            denied_terminal = subprocess.run(shlex.split(registration['headersHelper']), env=env,
                                             stdout=terminal, stderr=subprocess.PIPE, text=True, timeout=10)
            assert denied_terminal.returncode != 0
            assert cfg['API_KEYS'] not in denied_terminal.stderr
            os.set_blocking(master, False)
            with pytest.raises(BlockingIOError):
                os.read(master, 4096)  # No credential or other output reached the terminal.
        finally:
            os.close(terminal)
            os.close(master)
    env['CLAUDE_CODE_MCP_SERVER_URL'] = 'https://example.invalid/mcp/'
    denied = subprocess.run(shlex.split(registration['headersHelper']), env=env,
                            capture_output=True, text=True)
    assert denied.returncode != 0
    assert cfg['API_KEYS'] not in denied.stdout + denied.stderr
    assert cfg['API_KEYS'] not in result.stdout + result.stderr
    assert 'Setup complete' in result.stdout
    assert 'Run Claude from your agent folder:' in result.stdout
    launch = next(line.strip() for line in result.stdout.splitlines() if line.strip().startswith('cd '))
    assert shlex.split(launch) == ['cd', str(repo), '&&', 'claude']
    assert './scripts/chat.sh' not in result.stdout


@pytest.mark.skipif(os.name == 'nt', reason='POSIX socketpair subprocess stdout')
@pytest.mark.parametrize('metadata', ['valid', 'wrong-name', 'wrong-url', 'missing'])
def test_headers_helper_socket_stdout(tmp_path, metadata):
    """Real socket-backed stdout matches Node/Bun, unlike subprocess.PIPE."""
    repo = tmp_path / 'repo'
    (repo / 'scripts').mkdir(parents=True)
    (repo / 'server/src/config').mkdir(parents=True)
    shutil.copy(ROOT / 'scripts/setup_support.py', repo / 'scripts/setup_support.py')
    key = 'synthetic-local-key'
    (repo / 'server/src/config/local-config.json').write_text(json.dumps({
        'AUTH_ENABLED': True, 'API_KEYS': key, 'LOCAL_AGENT_URL': 'http://127.0.0.1:9082',
    }))
    env = dict(os.environ, CLAUDE_CODE_MCP_SERVER_NAME='mangrove-agent',
               CLAUDE_CODE_MCP_SERVER_URL='http://127.0.0.1:9082/mcp/')
    if metadata == 'wrong-name':
        env['CLAUDE_CODE_MCP_SERVER_NAME'] = 'another-server'
    elif metadata == 'wrong-url':
        env['CLAUDE_CODE_MCP_SERVER_URL'] = 'https://example.invalid/mcp/'
    elif metadata == 'missing':
        env.pop('CLAUDE_CODE_MCP_SERVER_NAME')
        env.pop('CLAUDE_CODE_MCP_SERVER_URL')
    reader, writer = socket.socketpair()
    with reader, writer:
        reader.settimeout(5)
        result = subprocess.run([sys.executable, str(repo / 'scripts/setup_support.py'), 'headers'],
                                env=env, stdout=writer, stderr=subprocess.PIPE, text=True, timeout=10)
        writer.shutdown(socket.SHUT_WR)
        output = bytearray()
        while chunk := reader.recv(4096):
            output.extend(chunk)
    assert key not in result.stderr
    if metadata == 'valid':
        assert result.returncode == 0, result.stderr
        assert json.loads(output) == {'X-API-Key': key}
    else:
        assert result.returncode != 0
        assert not output


def test_authorized_period_cap_cannot_be_silently_overridden(interactive, monkeypatch):
    monkeypatch.setattr(setup, 'check', lambda cfg: [dict(address=ADDR, chain='evm', chain_id=84532,
                                                        network='testnet', backup_confirmed_at='2026-09-18')])
    monkeypatch.setattr(setup, 'choose', lambda *a: '1')
    monkeypatch.setattr('builtins.input', lambda _: '5')
    wire = Mock(return_value={'cap_usd': 100, 'cap_source': 'authorized', 'exhausted': True})
    monkeypatch.setattr(setup, 'request', wire)
    write = Mock()
    monkeypatch.setattr(setup, 'atomic_config', write)
    with pytest.raises(setup.SetupError, match='authorized period cap overrides'):
        setup.wallet_action('select', wallet_config())
    assert not write.called
    assert wire.call_count == 1
    assert wire.call_args.args[1] == '/api/v1/agent/x402/spend'


@pytest.mark.parametrize('existing_key', ['', 'prod_existing'])
@pytest.mark.parametrize('selection', ['1', '2', 'default'])
def test_existing_install_always_offers_access_menu(config, monkeypatch, existing_key, selection):
    cfg = setup.read_config(setup.EXAMPLE)
    cfg.update(MANGROVE_API_KEY=existing_key, API_KEYS='private-local-key',
               X402_PAYER_WALLET=ADDR, X402_SPEND_CAP_USD=7)
    config.write_text(json.dumps(cfg))
    menu = Mock(side_effect=lambda prompt, options, default: default if selection == 'default' else selection)
    monkeypatch.setattr(setup, 'choose', menu)
    monkeypatch.setattr(setup, 'hidden', lambda _: 'prod_new')
    args = arguments()
    args.yes = False
    setup.configure(args)
    expects_key_menu = bool(existing_key) and selection in {'1', 'default'}
    assert menu.call_count == (2 if selection in {'1', 'default'} else 1)
    assert set(menu.call_args_list[0].args[1]) == {'1', '2', '3'}
    assert menu.call_args_list[0].args[2] == '1'
    if expects_key_menu:
        assert menu.call_args_list[1].args[1] == {
            '1': 'Use the existing API key', '2': 'Enter a new API key'}
        assert menu.call_args_list[1].args[2] == '1'
    after = json.loads(config.read_text())
    expected_key = (existing_key or 'prod_new') if selection in {'1', 'default'} else existing_key
    assert after['MANGROVE_API_KEY'] == expected_key
    for field in ['API_KEYS', 'X402_PAYER_WALLET', 'X402_SPEND_CAP_USD']:
        assert after[field] == cfg[field]


def test_yes_rerun_never_prompts(config, monkeypatch):
    setup.configure(arguments())
    menu = Mock(side_effect=AssertionError('Noninteractive setup must not prompt'))
    monkeypatch.setattr(setup, 'choose', menu)
    setup.configure(arguments())
    assert not menu.called


def test_crashed_config_staging_is_private_and_gitignored(tmp_path):
    target = tmp_path / 'local-config.json'
    code = '''import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import setup_support as setup
setup.os.replace = lambda *args: os._exit(91)
setup.atomic_config({'API_KEYS':'synthetic-crash-marker'}, Path(sys.argv[2]))
'''
    result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts'), str(target)])
    assert result.returncode == 91
    remnant, = tmp_path.glob('local-config.json.setup-*')
    assert remnant.stat().st_mode & 0o777 == 0o600
    assert json.loads(remnant.read_text())['API_KEYS'] == 'synthetic-crash-marker'
    for name in [remnant.name, '.setup-legacy-crash']:
        ignored = subprocess.run(['git', 'check-ignore', '--no-index', '-q',
                                  'server/src/config/' + name], cwd=ROOT)
        assert ignored.returncode == 0


def test_foreground_exit_preserves_successor_lock_and_pid(checkout):
    repo, env = checkout
    proc = subprocess.Popen(['bash', str(repo / 'scripts/setup.sh'), '--foreground', '--yes', '--no-mcp'],
                            cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    timer = threading.Timer(20, proc.terminate)
    timer.start()
    attached = False
    try:
        for line in proc.stdout:
            if 'Agent attached to this terminal' in line:
                attached = True
                lock = repo / 'agent-data/.setup.lock'
                lock.mkdir()
                (lock / 'pid').write_text('successor-owner')
                (repo / 'agent-data/bare.pid').write_text('99999999')
                proc.terminate()
                break
        proc.communicate(timeout=15)
        assert attached
        assert (lock / 'pid').read_text() == 'successor-owner'
        assert (repo / 'agent-data/bare.pid').read_text() == '99999999'
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.fixture
def backup(monkeypatch):
    monkeypatch.setitem(sys.modules, 'setup_support', setup)
    spec = importlib.util.spec_from_file_location('wallet_backup', ROOT / 'scripts/wallet_backup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('action', ['reveal', 'confirm'])
@pytest.mark.parametrize('failure', ['redirect', 'error', 'oversize'])
def test_backup_transport_never_forwards_or_prints_secrets(backup, tmp_path, monkeypatch, capsys, action, failure):
    events = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            events.append(self.path)
            if failure == 'redirect':
                self.send_response(302)
                self.send_header('Location', '/credential-leak')
            else:
                self.send_response(500 if failure == 'error' else 200)
            self.end_headers()
            try:
                self.wfile.write(b'synthetic-sensitive-body' if failure != 'oversize' else b'x' * (1024 * 1024 + 1))
            except (BrokenPipeError, ConnectionResetError):
                # The bounded-response client deliberately closes oversized replies.
                pass

        do_POST = do_GET
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = tmp_path / 'local-config.json'
    cfg.write_text(json.dumps({'API_KEYS': 'synthetic-local-key', 'AUTH_ENABLED': True}))
    monkeypatch.setenv('CONFIG_FILE', str(cfg))
    monkeypatch.setenv('LOCAL_AGENT_URL', f'http://127.0.0.1:{server.server_port}')
    # A nonfunctional ambient proxy must not affect direct loopback transport.
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    monkeypatch.setenv('no_proxy', '')
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    try:
        with pytest.raises(setup.SetupError) as caught:
            backup.run(action, ADDR, True)
        assert 'synthetic-sensitive-body' not in str(caught.value)
        assert events == []
        assert 'isolated custody administrator' in str(caught.value)
        assert 'synthetic' not in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('url', ['https://example.invalid', 'http://192.0.2.1', 'http://user:password@127.0.0.1'])
def test_retired_backup_refuses_before_destination_or_config_access(backup, tmp_path, monkeypatch, url):
    monkeypatch.setenv('CONFIG_FILE', str(tmp_path / 'nonexistent-config.json'))
    monkeypatch.setenv('LOCAL_AGENT_URL', url)
    wire = Mock(side_effect=AssertionError('No HTTP allowed'))
    monkeypatch.setattr(setup, 'request', wire)
    with pytest.raises(setup.SetupError, match='isolated custody administrator'):
        backup.run('confirm', ADDR, True)
    wire.assert_not_called()


def test_retired_backup_refuses_with_redirected_output(backup, monkeypatch):
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: False)
    wire = Mock(side_effect=AssertionError('No HTTP allowed'))
    monkeypatch.setattr(setup, 'request', wire)
    with pytest.raises(setup.SetupError, match='isolated custody administrator'):
        backup.run('reveal', ADDR, True)
    wire.assert_not_called()


def test_verifier_rejects_stopped_scheduler(checkout):
    repo, _ = checkout
    fake = repo / 'server/uvicorn.py'
    fake.write_text(fake.read_text().replace("'scheduler_running': True", "'scheduler_running': False"))
    result = run_setup(checkout, '--yes', '--no-mcp')
    assert result.returncode != 0
    assert 'Scheduler is not ready' in result.stdout + result.stderr
    assert 'Setup complete' not in result.stdout


@pytest.mark.parametrize('addr', [ADDR, '1' * 32])
@pytest.mark.parametrize('action', ['reveal', 'confirm'])
def test_retired_backup_never_exports_or_confirms_any_wallet(backup, monkeypatch, capsys, addr, action):
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    wire = Mock(return_value={'address': addr, 'secret': 'synthetic wallet backup'})
    monkeypatch.setattr(setup, 'request', wire)
    with pytest.raises(setup.SetupError, match='Ordinary API credentials cannot authorize'):
        backup.run(action, addr, True)
    wire.assert_not_called()
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('explicit_mode', [False, True])
@pytest.mark.parametrize('selection', ['', '1', '2'])
def test_interactive_existing_key_choice(config, interactive, monkeypatch, capsys,
                                        explicit_mode, selection):
    setup.configure(arguments())
    before = json.loads(config.read_text())
    before.update(MANGROVE_API_KEY='prod_old_sentinel', custom={'keep': True})
    config.write_text(json.dumps(before))
    replies = iter(([selection] if explicit_mode else ['1', selection]))
    monkeypatch.setattr('builtins.input', lambda _: next(replies))
    secret_prompt = Mock(return_value='prod_new_sentinel')
    monkeypatch.setattr(setup.getpass, 'getpass', secret_prompt)
    args = arguments()
    args.yes = False
    args.auth = 'api-key' if explicit_mode else None
    setup.configure(args)
    after = json.loads(config.read_text())
    expected = dict(before)
    expected['MANGROVE_ACCESS_MODE'] = 'api-key'
    expected['MANGROVE_API_KEY'] = 'prod_new_sentinel' if selection == '2' else 'prod_old_sentinel'
    assert after == expected
    assert secret_prompt.call_count == (1 if selection == '2' else 0)
    output = capsys.readouterr().out
    assert 'Use the existing API key' in output
    assert 'Enter a new API key' in output
    assert 'prod_old_sentinel' not in output and 'prod_new_sentinel' not in output
    assert config.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('replacement', ['', KeyboardInterrupt(), EOFError()])
def test_failed_replacement_keeps_saved_key(config, interactive, monkeypatch, replacement):
    setup.configure(arguments())
    cfg = json.loads(config.read_text())
    cfg['MANGROVE_API_KEY'] = 'prod_existing'
    config.write_text(json.dumps(cfg))
    before = config.read_bytes()
    monkeypatch.setattr('builtins.input', lambda _: '2')
    prompt = Mock(side_effect=replacement) if isinstance(replacement, BaseException) else Mock(return_value=replacement)
    monkeypatch.setattr(setup.getpass, 'getpass', prompt)
    args = arguments()
    args.yes = False
    args.auth = 'api-key'
    with pytest.raises((setup.SetupError, KeyboardInterrupt, EOFError)):
        setup.configure(args)
    assert config.read_bytes() == before


def test_stdin_replacement_bypasses_interactive_key_choice(config, monkeypatch):
    import io

    setup.configure(arguments())
    cfg = json.loads(config.read_text())
    cfg['MANGROVE_API_KEY'] = 'prod_existing'
    config.write_text(json.dumps(cfg))
    monkeypatch.setattr(sys, 'stdin', io.StringIO('prod_replacement'))
    monkeypatch.setattr(setup, 'choose', Mock(side_effect=AssertionError('Unexpected prompt')))
    monkeypatch.setattr(setup, 'hidden', Mock(side_effect=AssertionError('Unexpected secret prompt')))
    args = arguments()
    args.yes = False
    args.api_key_stdin = True
    setup.configure(args)
    assert json.loads(config.read_text())['MANGROVE_API_KEY'] == 'prod_replacement'


def test_markets_setup_check_is_read_only(monkeypatch, capsys):
    call = Mock(return_value={'ownership_discovery': 'ready'})
    monkeypatch.setattr(setup, 'request', call)
    cfg = {'MANGROVE_API_KEY': 'private-key'}
    setup.check_markets(cfg)
    call.assert_called_once_with(cfg, '/api/v1/agent/marketplace/readiness')
    assert 'verified' in capsys.readouterr().out


def test_markets_setup_check_reports_unavailable_without_blocking_other_tools(monkeypatch, capsys):
    monkeypatch.setattr(setup, 'request', Mock(side_effect=setup.SetupError('private response')))
    setup.check_markets({'MANGROVE_API_KEY': 'private-key'})
    output = capsys.readouterr().out
    assert 'unavailable' in output
    assert 'private' not in output


@pytest.mark.parametrize('ownership', ['ready', 'unavailable'])
def test_markets_setup_shows_validated_product_status(monkeypatch, capsys, ownership):
    from src.services.marketplace_status import MESSAGE

    status = {'version': 1, 'product': 'agent_to_agent_marketplace',
              'availability': 'under_construction', 'message': MESSAGE}
    monkeypatch.setattr(setup, 'request', Mock(return_value={
        'ownership_discovery': ownership, 'marketplace': status,
    }))
    setup.check_markets({})
    output = capsys.readouterr().out
    assert MESSAGE in output
    assert ('verified' if ownership == 'ready' else 'unavailable') in output


def test_markets_setup_does_not_echo_arbitrary_product_text(monkeypatch, capsys):
    monkeypatch.setattr(setup, 'request', Mock(return_value={
        'ownership_discovery': 'ready', 'marketplace': {
            'version': 1, 'product': 'agent_to_agent_marketplace',
            'availability': 'under_construction', 'message': 'private upstream text',
        },
    }))
    setup.check_markets({})
    assert 'private upstream text' not in capsys.readouterr().out


def test_keyless_setup_checks_public_ownership_discovery(monkeypatch, capsys):
    call = Mock(return_value={'ownership_discovery': 'ready'})
    monkeypatch.setattr(setup, 'request', call)
    setup.check_markets({})
    call.assert_called_once_with({}, '/api/v1/agent/marketplace/readiness')
    assert 'verified' in capsys.readouterr().out


def test_x402_setup_configures_recovery_without_changing_custom_rpc(config):
    cfg = setup.read_config(setup.EXAMPLE)
    cfg['X402_NETWORK'] = 'eip155:84532'
    cfg['X402_RECONCILIATION_RPC_URLS'] = {'eip155:8453': 'https://operator.example/rpc'}
    setup.atomic_config(cfg)
    args = arguments()
    args.auth = 'x402'
    setup.configure(args)
    saved = json.loads(config.read_text())
    assert saved['X402_RECONCILIATION_RPC_URLS'] == {
        'eip155:8453': 'https://operator.example/rpc', 'eip155:84532': 'https://sepolia.base.org'}


@pytest.mark.parametrize('value', [[], {'eip155:84532': 'http://remote.example'},
                                  {'eip155:84532': 'https://user:secret@rpc.example'}])
def test_invalid_recovery_config_does_not_replace_saved_config(config, value):
    cfg = setup.read_config(setup.EXAMPLE)
    cfg['X402_RECONCILIATION_RPC_URLS'] = value
    setup.atomic_config(cfg)
    before = config.read_bytes()
    args = arguments()
    args.auth = 'x402'
    with pytest.raises(setup.SetupError):
        setup.configure(args)
    assert config.read_bytes() == before


class _Terminal(io.StringIO):
    def __init__(self, encoding='utf-8'):
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self):
        return self._encoding

    def isatty(self):
        return True


def _banner(monkeypatch, columns, *, encoding='utf-8', no_color=True):
    # Patch the module helper, never shutil itself: pytest's reporter shares it.
    monkeypatch.setattr(setup, '_terminal_columns', lambda: columns)
    monkeypatch.setenv('TERM', 'xterm-256color')
    if no_color:
        monkeypatch.setenv('NO_COLOR', '1')
    else:
        monkeypatch.delenv('NO_COLOR', raising=False)
    stream = _Terminal(encoding)
    setup.banner(stream)
    return stream.getvalue()


def test_banner_is_silent_when_not_a_terminal():
    stream = io.StringIO()
    setup.banner(stream)
    assert stream.getvalue() == ''


def test_logo_grid_is_well_formed():
    assert len(setup._LOGO) % 2 == 0
    assert len({len(row) for row in setup._LOGO}) == 1
    assert set(''.join(setup._LOGO)) <= set(setup._LOGO_COLORS) | {' '}
    assert all(len(glyph) == 6 for glyph in setup._GLYPHS.values())


@pytest.mark.parametrize('columns,wordmark_rows', [(140, 6), (90, 12), (50, 0)])
def test_banner_fits_terminal_width(monkeypatch, columns, wordmark_rows):
    lines = _banner(monkeypatch, columns).splitlines()
    assert all(len(line) <= columns for line in lines)
    assert sum('▀' in line or '▄' in line for line in lines) > 0  # logo drawn
    assert sum('╗' in line or '╝' in line for line in lines) == wordmark_rows
    if not wordmark_rows:
        assert 'MANGROVE AGENT' in [line.strip() for line in lines]


def test_banner_colour_follows_no_color(monkeypatch):
    assert '\x1b[' not in _banner(monkeypatch, 140)
    coloured = _banner(monkeypatch, 140, no_color=False)
    assert '\x1b[38;' in coloured and coloured.count('\x1b[0m') > 0


@pytest.mark.parametrize('columns', [140, 90, 50])
def test_coloured_banner_preserves_every_row(monkeypatch, columns):
    plain = _banner(monkeypatch, columns).splitlines()
    coloured = re.sub(r'\x1b\[[0-9;]*m', '',
                      _banner(monkeypatch, columns, no_color=False)).splitlines()
    text_start = len(setup._LOGO) // 2 + 2
    assert len(coloured) == len(plain)
    assert coloured[text_start:] == plain[text_start:]


def test_banner_ascii_terminal_gets_plain_title(monkeypatch):
    output = _banner(monkeypatch, 140, encoding='ascii')
    output.encode('ascii')
    assert 'MANGROVE AGENT' in output


def test_choose_uses_numbered_menu_without_terminal_output(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: False)
    monkeypatch.setattr('builtins.input', lambda _: '2')
    assert setup.choose('Pick one', {'1': 'First', '2': 'Second'}, '1') == '2'
    assert '  2. Second' in capsys.readouterr().out


_SELECT_CHILD = '''
import importlib.util, sys, termios
spec = importlib.util.spec_from_file_location('setup_support', sys.argv[1])
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
result = setup.choose('Pick one', {'1': 'First', '2': 'Second', '3': 'Third'}, '1')
echo = bool(termios.tcgetattr(0)[3] & termios.ECHO)
print(f'RESULT={result} ECHO={echo}')
'''


def _drive_menu(keys, *, typeahead=False):
    import fcntl
    import pty
    import termios
    import time

    master, terminal = pty.openpty()

    def controlling_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    if typeahead:
        os.write(master, keys)
    proc = subprocess.Popen([sys.executable, '-c', _SELECT_CHILD, str(ROOT / 'scripts/setup_support.py')],
                            stdin=terminal, stdout=terminal, stderr=terminal, preexec_fn=controlling_terminal,
                            env=dict(os.environ, TERM='xterm-256color'))
    os.close(terminal)
    output = bytearray()
    try:
        if not typeahead:
            time.sleep(0.5)  # Let the menu enter cbreak mode before keys arrive.
            for key in keys.split(b'|'):
                os.write(master, key)
                time.sleep(0.1)
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    return output.decode(errors='replace')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX terminal menu')
@pytest.mark.parametrize('keys,expected', [
    (b'\r', '1'),                          # Enter keeps the default
    (b'\x1b[B|\r', '2'),                   # arrow down
    (b'\x1b[A|\r', '3'),                   # arrow up wraps to the last option
    (b'\x1bOB|\x1bOB|\x1b[A|\n', '2'),     # application-mode arrows
    (b'x|\x1b[1;5C|3|\r', '3'),            # unknown keys ignored, digit jumps
])
def test_arrow_menu_selects_and_restores_terminal(keys, expected):
    output = _drive_menu(keys)
    assert f'RESULT={expected} ECHO=True' in output
    assert '\x1b[?25h' in output  # cursor shown again


@pytest.mark.skipif(os.name == 'nt', reason='POSIX terminal menu')
def test_arrow_menu_keeps_numbered_typeahead():
    assert 'RESULT=2 ECHO=True' in _drive_menu(b'2\n', typeahead=True)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX terminal menu')
def test_arrow_menu_interrupt_restores_terminal():
    output = _drive_menu(b'\x03')
    assert 'RESULT=' not in output
    assert '\x1b[?25h' in output


@pytest.mark.parametrize('target', ['eip155:8453', 'eip155:84532'])
def test_select_existing_evm_wallet_on_configured_network(interactive, monkeypatch, target):
    item = dict(address=ADDR, chain='evm', chain_id=84532,
                network='testnet', backup_confirmed_at='2026-09-18')
    monkeypatch.setattr(setup, 'check', lambda cfg: [item])
    monkeypatch.setattr(setup, 'choose', lambda *args: '1')
    replies = iter(['', 'SELECT'])
    monkeypatch.setattr('builtins.input', lambda _: next(replies))
    wire = Mock(return_value={'cap_usd': 2, 'cap_source': 'authorized'})
    monkeypatch.setattr(setup, 'request', wire)
    write = Mock()
    monkeypatch.setattr(setup, 'atomic_config', write)
    cfg = wallet_config()
    cfg['X402_NETWORK'] = target
    setup.wallet_action('select', cfg)
    assert cfg['X402_NETWORK'] == target
    assert cfg['X402_PAYER_WALLET'] == ADDR
    assert cfg['X402_SPEND_CAP_USD'] == 2
    assert item['chain_id'] == 84532
    assert wire.call_count == 1
    assert wire.call_args.args[1] == '/api/v1/agent/x402/spend'
    write.assert_called_once()


@pytest.mark.parametrize('selected', ['eip155:84532', 'eip155:8453'])
def test_setup_rerun_preserves_explicit_network_and_backend(config, selected):
    cfg = setup.read_config(setup.EXAMPLE)
    cfg.update(X402_NETWORK=selected, X402_MANGROVE_BASE_URL='http://127.0.0.1:5002/api/v1',
               X402_PAYER_WALLET=ADDR, X402_SPEND_CAP_USD=3,
               X402_FACILITATOR_URL='https://operator.example/facilitator')
    setup.atomic_config(cfg)
    args = arguments()
    args.auth = 'x402'
    setup.configure(args)
    saved = json.loads(config.read_text())
    for key in ['X402_NETWORK', 'X402_MANGROVE_BASE_URL', 'X402_PAYER_WALLET',
                'X402_SPEND_CAP_USD', 'X402_FACILITATOR_URL']:
        assert saved[key] == cfg[key]
    assert selected in saved['X402_RECONCILIATION_RPC_URLS']


def test_oauth_setup_preserves_but_does_not_use_saved_key(config, monkeypatch, capsys):
    cfg = setup.read_config(setup.EXAMPLE)
    cfg.update(MANGROVE_API_KEY='prod_saved', API_KEYS='private-local-key')
    config.write_text(json.dumps(cfg))
    args = arguments()
    args.auth = 'oauth'
    setup.configure(args)
    saved = json.loads(config.read_text())
    assert saved['MANGROVE_ACCESS_MODE'] == 'oauth'
    assert saved['MANGROVE_API_KEY'] == 'prod_saved'
    assert setup.upstream_key(saved) == ''
    setup.guide(saved)
    assert 'prod_saved' not in capsys.readouterr().out


@pytest.mark.parametrize('command', ['oauth-setup', 'oauth-login'])
def test_oauth_setup_opens_browser_and_waits_for_callback(monkeypatch, capsys, command):
    cfg = {'API_KEYS': 'local-private', 'AUTH_ENABLED': True, 'MANGROVE_ACCESS_MODE': 'oauth'}
    calls = []
    def request(cfg, path, payload=None, **kwargs):
        calls.append((path, payload))
        if path.endswith('/tools'):
            return {'mangroveai_status': 'available'}
        if path.endswith('/start'):
            return {'authorization_url': 'https://api.mangrovedeveloper.ai/oauth/authorize?state=example'}
        if path.endswith('/status'):
            return {'connected': len(calls) > 1, 'pending': False, 'expires_at': 123 if len(calls) > 1 else None}
        return {}
    monkeypatch.setattr(setup, 'request', request)
    browser = Mock()
    monkeypatch.setattr(setup.webbrowser, 'open', browser)
    setup.oauth_command(cfg, command, ['signals:evaluate'])
    assert browser.call_count == 1
    assert 'signals:evaluate' in calls[1][1]['scopes']
    assert 'Connected to Mangrove' in capsys.readouterr().out
    assert calls[-1][0].endswith('/cancel')


@pytest.mark.parametrize('command,yes,opens', [('oauth-setup', False, False), ('oauth-setup', True, False), ('oauth-login', False, True)])
def test_oauth_setup_automatically_reuses_connection(monkeypatch, capsys, command, yes, opens):
    cfg = {'API_KEYS': 'local-private', 'AUTH_ENABLED': True, 'MANGROVE_ACCESS_MODE': 'oauth'}
    started = []
    def request(cfg, path, payload=None, **kwargs):
        if path.endswith('/tools'):
            return {'mangroveai_status': 'available'}
        if path.endswith('/start'):
            started.append(payload)
            return {'authorization_url': 'https://api.mangrovedeveloper.ai/oauth/authorize?state=example'}
        if path.endswith('/status'):
            return {'connected': True, 'pending': False, 'expires_at': 456 if started else 123}
        return {}
    monkeypatch.setattr(setup, 'request', request)
    prompt = Mock(side_effect=AssertionError('Saved connections must not prompt'))
    monkeypatch.setattr(setup, 'choose', prompt)
    browser = Mock()
    monkeypatch.setattr(setup.webbrowser, 'open', browser)
    setup.oauth_command(cfg, command, [], yes=yes)
    assert browser.call_count == int(opens)
    assert started == ([{}] if opens else [])
    prompt.assert_not_called()
    if not opens:
        assert 'Already signed in to Mangrove. Using your saved OAuth connection.' in capsys.readouterr().out


def test_oauth_guide_does_not_send_user_to_internal_helper(capsys):
    setup.guide({'MANGROVE_ACCESS_MODE': 'oauth'})
    output = capsys.readouterr().out
    assert 'setup_support.py' not in output
    assert 'No separate sign-in script' in output


@pytest.mark.parametrize('options,command', [
    (['--oauth', 'login'], 'oauth-login'),
    *[(['--oauth-scope', scope], 'oauth-login') for scope in [
        'strategies:write', 'strategies:archive', 'strategies:evaluate',
        'sweeps:read', 'sweeps:run', 'signals:evaluate']],
    (['--auth', 'oauth'], 'oauth-setup'),
])
def test_setup_shell_routes_oauth_through_main_script(checkout, options, command):
    repo, _ = checkout
    helper = repo / 'scripts/setup_support.py'
    source = helper.read_text()
    hook = """
def oauth_command(cfg, command, scopes, *, yes=False):
    assert cfg['MANGROVE_ACCESS_MODE'] == 'oauth'
    (ROOT / 'oauth-call.json').write_text(json.dumps({'command': command, 'scopes': scopes, 'yes': yes}))
"""
    helper.write_text(source.replace("if __name__ == '__main__':", hook + "\nif __name__ == '__main__':"))
    result = run_setup(checkout, '--yes', '--no-mcp', '--no-verify', *options)
    assert result.returncode == 0, result.stderr + result.stdout
    call = json.loads((repo / 'oauth-call.json').read_text())
    assert call['command'] == command
    assert call['scopes'] == ([options[1]] if '--oauth-scope' in options else [])
    assert call['yes'] == (command == 'oauth-setup')
    assert 'setup_support.py' not in result.stdout


@pytest.mark.parametrize('options', [
    ['--auth', 'api-key', '--oauth', 'login'],
    ['--oauth', 'status', '--oauth-scope', 'signals:evaluate'],
    ['--oauth-scope', 'invalid'],
    ['--wallet', 'create', '--oauth', 'login'],
])
def test_setup_rejects_conflicting_oauth_options_before_mutation(checkout, options):
    repo, _ = checkout
    result = run_setup(checkout, *options)
    assert result.returncode != 0
    assert not (repo / 'agent-data').exists()
    assert not (repo / 'server/src/config/local-config.json').exists()


def test_oauth_disconnect_surfaces_issuer_cleanup_instructions(monkeypatch, capsys):
    monkeypatch.setattr(setup, 'require_auth', lambda cfg: None)
    message = ('Configure MANGROVE_OAUTH_ISSUER to https://issuer.test, restart the agent, '
               'and run ./scripts/setup.sh --oauth disconnect.')
    monkeypatch.setattr(setup, 'request', lambda *a, **k: {
        'disconnected': True, 'revoked': False, 'message': message})
    setup.oauth_command({}, 'oauth-disconnect', [])
    output = capsys.readouterr().out
    assert message in output
    assert 'Remote grant revoked.' not in output


def test_oauth_reconnect_reports_required_local_cleanup(monkeypatch, capsys):
    cfg = {'API_KEYS': 'synthetic-local', 'AUTH_ENABLED': True, 'MANGROVE_ACCESS_MODE': 'oauth'}
    states = iter([{'connected': False}, {'connected': True, 'expires_at': 123,
                                        'revocation_pending': True}])
    def request(cfg, path, payload=None, **kwargs):
        if path.endswith('/status'):
            return next(states)
        if path.endswith('/tools'):
            return {'mangroveai_status': 'available'}
        if path.endswith('/start'):
            return {'authorization_url': 'https://api.mangrovedeveloper.ai/oauth/authorize?state=synthetic'}
        return {}
    monkeypatch.setattr(setup, 'request', request)
    monkeypatch.setattr(setup.webbrowser, 'open', lambda _: True)
    setup.oauth_command(cfg, 'oauth-login', [])
    output = capsys.readouterr().out
    assert './scripts/setup.sh --oauth disconnect' in output
    assert 'Portal revocation alone does not clear the local cleanup receipt.' in output
    assert 'sign in again after cleanup' in output

# Guided onboarding: isolated state and synthetic wallets only.
@pytest.fixture
def wizard(tmp_path, monkeypatch, interactive):
    checkpoint = tmp_path / 'wallet-setup.json'
    monkeypatch.setattr(setup, 'onboarding_path', lambda: checkpoint)
    cfg = dict(wallet_config(), X402_NETWORK='eip155:8453', MANGROVE_ACCESS_MODE='x402')
    wallets = []
    monkeypatch.setattr(setup, 'check', lambda cfg: list(wallets))
    return cfg, wallets, checkpoint


def test_wizard_creates_once_then_selects_same_wallet(wizard, monkeypatch, capsys):
    action = 'create'
    cfg, wallets, checkpoint = wizard
    choices = iter(['1' if action == 'create' else '2', '1'])
    monkeypatch.setattr(setup, 'choose', lambda *a: next(choices))
    responses = iter([action.upper()] + (['BACKED UP'] if action == 'import' else []) + ['', 'SELECT'])
    monkeypatch.setattr('builtins.input', lambda _: next(responses))
    monkeypatch.setattr(setup, 'hidden', lambda _: 'synthetic-secret')
    real_write = setup.atomic_config
    writes = []
    monkeypatch.setattr(setup, 'atomic_config', lambda value, **kw: real_write(value, **kw) if kw else writes.append(dict(value)))
    def wire(config, path, payload=None):
        if path.endswith('/stash-secret'):
            return {'vault_token': 'synthetic_token_1234567890'}
        if path.endswith('/' + action):
            assert json.loads(checkpoint.read_text())['stage'] == 'pending'
            assert payload['chain_id'] == 8453 and payload['network'] == 'mainnet'
            wallets.append(dict(address=ADDR, chain='evm', backup_confirmed_at='confirmed'))
            return {'address': ADDR, 'vault_token': 'must-not-print'}
        assert path.endswith('/x402/spend')
        return {'cap_usd': 25, 'cap_source': 'config', 'exhausted': False}
    request = Mock(side_effect=wire)
    monkeypatch.setattr(setup, 'request', request)
    setup.onboard(cfg)
    assert sum(c.args[1].endswith('/' + action) for c in request.call_args_list) == 1
    assert writes[0]['X402_PAYER_WALLET'] == ADDR
    assert json.loads(checkpoint.read_text())['stage'] == 'selected'
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert 'synthetic-secret' not in output and 'must-not-print' not in output
    assert 'testnet' not in output.lower()
    assert '--wallet select' not in output and 'Run ./scripts/setup.sh --yes' not in output
    assert 'synthetic_token' not in checkpoint.read_text()


def test_wizard_import_redirects_to_human_admin_without_secret_or_checkpoint(wizard, monkeypatch, capsys):
    cfg, wallets, checkpoint = wizard
    choices = iter(['2', '4'])  # Import instruction, then finish wallet setup later.
    monkeypatch.setattr(setup, 'choose', lambda *a: next(choices))
    monkeypatch.setattr('builtins.input', lambda _: 'IMPORT')
    hidden = Mock(side_effect=AssertionError('Secret input forbidden'))
    request = Mock(side_effect=AssertionError('No wallet HTTP mutation permitted'))
    monkeypatch.setattr(setup, 'hidden', hidden)
    monkeypatch.setattr(setup, 'request', request)
    setup.onboard(cfg)
    hidden.assert_not_called()
    request.assert_not_called()
    assert wallets == [] and not checkpoint.exists()
    output = capsys.readouterr().out
    assert 'isolated custody administrator' in output
    assert 'No wallet mutation was sent' in output
    assert 'Private key or recovery phrase' not in output


def test_backup_enter_does_not_confirm_and_admin_commands_pin_wallet(wizard, monkeypatch, capsys):
    cfg, wallets, _ = wizard
    wallets.append(dict(address=ADDR, chain='evm', backup_confirmed_at=None))
    monkeypatch.setattr(setup, 'ROOT', Path('/untrusted/checkout with spaces'))
    turns = []
    def answer(prompt):
        turns.append(prompt)
        if len(turns) == 2:
            wallets[0]['backup_confirmed_at'] = 'confirmed-in-private-terminal'
        return ''
    monkeypatch.setattr('builtins.input', answer)
    monkeypatch.setattr(setup, 'request', Mock(side_effect=AssertionError('No mutation allowed')))
    assert setup.wait_for_backup(cfg, ADDR)
    assert len(turns) == 2
    output = capsys.readouterr().out
    assert 'custody-service.py admin --config POLICY backup ' + ADDR in output
    assert 'custody-service.py admin --config POLICY confirm-backup ' + ADDR in output
    assert output.count(' admin --config POLICY backup ') == 1
    assert '/untrusted/checkout' not in output
    assert './scripts/reveal-secret.sh' not in output
    assert './scripts/confirm-backup.sh' not in output
    assert 'Backup not confirmed yet' in output
    assert 'SETUP_PYTHON=' not in output
    assert 'WALLET_ADDRESS' not in output


def test_backup_defer_and_resume_never_recreates(wizard, monkeypatch):
    cfg, wallets, checkpoint = wizard
    wallets.append(dict(address=ADDR, chain='evm', backup_confirmed_at=None))
    setup.save_onboarding(dict(stage='wallet', action='create', network=cfg['X402_NETWORK'], address=ADDR))
    choices = iter(['1', '2', '1'])  # resume, defer backup, resume next run
    monkeypatch.setattr(setup, 'choose', lambda *a: next(choices))
    action = Mock(side_effect=AssertionError('Must not repeat mutation'))
    monkeypatch.setattr(setup, 'wallet_action', action)
    setup.onboard(cfg)
    assert checkpoint.exists() and not action.called
    wallets[0]['backup_confirmed_at'] = 'confirmed'
    monkeypatch.setattr(setup, 'wallet_action', Mock())
    setup.onboard(cfg)
    setup.wallet_action.assert_called_once_with('select', cfg, guided=True, selected_address=ADDR)
    assert json.loads(checkpoint.read_text())['stage'] == 'selected'


def test_uncertain_create_recovers_without_repeating(wizard, monkeypatch):
    cfg, wallets, checkpoint = wizard
    choices = iter(['1', '1', '1'])  # create, recover, choose known public address
    monkeypatch.setattr(setup, 'choose', lambda *a: next(choices))
    monkeypatch.setattr('builtins.input', lambda _: 'CREATE')
    def action(name, config, **kwargs):
        if name == 'create':
            kwargs['before_mutation']()
            wallets.append(dict(address=ADDR, chain='evm', backup_confirmed_at='confirmed'))
            raise setup.SetupError('Response lost')
        assert name == 'select' and kwargs['selected_address'] == ADDR
    operation = Mock(side_effect=action)
    monkeypatch.setattr(setup, 'wallet_action', operation)
    setup.onboard(cfg)
    assert [c.args[0] for c in operation.call_args_list] == ['create', 'select']
    assert json.loads(checkpoint.read_text())['address'] == ADDR


def test_pending_operation_without_wallet_can_defer(wizard, monkeypatch):
    cfg, _, checkpoint = wizard
    setup.save_onboarding(dict(stage='pending', action='import', network=cfg['X402_NETWORK']))
    choices = iter(['1', '2'])
    monkeypatch.setattr(setup, 'choose', lambda *a: next(choices))
    mutation = Mock(side_effect=AssertionError('No repeated import'))
    monkeypatch.setattr(setup, 'wallet_action', mutation)
    setup.onboard(cfg)
    assert json.loads(checkpoint.read_text())['stage'] == 'pending'
    assert not mutation.called


@pytest.mark.parametrize('mode,yes', [('x402', True), ('api-key', False), ('oauth', False)])
def test_nonwallet_or_automated_setup_never_mutates(wizard, monkeypatch, mode, yes):
    cfg, _, _ = wizard
    cfg.update(MANGROVE_ACCESS_MODE=mode, MANGROVE_API_KEY='prod_existing' if mode == 'api-key' else '')
    monkeypatch.setattr(setup, 'choose', Mock(side_effect=AssertionError('No prompts')))
    monkeypatch.setattr(setup, 'request', Mock(side_effect=AssertionError('No requests')))
    setup.onboard(cfg, yes=yes)


def test_existing_valid_payer_does_not_prompt_for_budget(wizard, monkeypatch):
    cfg, wallets, checkpoint = wizard
    cfg['X402_PAYER_WALLET'] = ADDR
    wallets.append(dict(address=ADDR, chain='evm', backup_confirmed_at='confirmed'))
    choices = iter(['3', '1'])
    monkeypatch.setattr(setup, 'choose', lambda *a: next(choices))
    monkeypatch.setattr(setup, 'wallet_action', Mock(side_effect=AssertionError('No selection mutation')))
    setup.onboard(cfg)
    assert not checkpoint.exists()


def test_import_address_rejection_keeps_wallet_but_not_payer(wizard, monkeypatch):
    cfg, wallets, checkpoint = wizard
    wallets.append(dict(address=ADDR, chain='evm', backup_confirmed_at='confirmed'))
    setup.save_onboarding(dict(stage='wallet', action='import', network=cfg['X402_NETWORK'], address=ADDR))
    choices = iter(['1', '2', '4'])  # resume, reject address, finish later
    monkeypatch.setattr(setup, 'choose', lambda *a: next(choices))
    monkeypatch.setattr(setup, 'wallet_action', Mock(side_effect=AssertionError('Do not select')))
    setup.onboard(cfg)
    assert wallets and not checkpoint.exists() and not cfg.get('X402_PAYER_WALLET')


def test_invalid_cap_can_retry_without_recreating(wizard, monkeypatch):
    cfg, wallets, checkpoint = wizard
    wallets.append(dict(address=ADDR, chain='evm', backup_confirmed_at='confirmed'))
    setup.save_onboarding(dict(stage='wallet', action='create', network=cfg['X402_NETWORK'], address=ADDR))
    choices = Mock(return_value='1')
    monkeypatch.setattr(setup, 'choose', choices)
    operation = Mock(side_effect=[setup.SetupError('Invalid cap'), None])
    monkeypatch.setattr(setup, 'wallet_action', operation)
    setup.onboard(cfg)
    assert [c.args[0] for c in operation.call_args_list] == ['select', 'select']
    assert json.loads(checkpoint.read_text())['stage'] == 'selected'


def test_selected_checkpoint_resumes_application_without_reapproval(wizard, monkeypatch):
    cfg, wallets, checkpoint = wizard
    cfg['X402_PAYER_WALLET'] = ADDR
    wallets.append(dict(address=ADDR, chain='evm', backup_confirmed_at='confirmed'))
    setup.save_onboarding(dict(stage='selected', action='create', network=cfg['X402_NETWORK'], address=ADDR))
    monkeypatch.setattr(setup, 'wallet_action', Mock(side_effect=AssertionError('Already selected')))
    setup.onboard(cfg)
    assert checkpoint.exists()
    setup.finish_onboarding(cfg)
    assert not checkpoint.exists()


@pytest.mark.parametrize('selection,expected', [('1', 'eip155:8453'), ('2', 'eip155:84532')])
def test_existing_network_switch_is_explicit(config, monkeypatch, selection, expected):
    cfg = setup.read_config(setup.EXAMPLE)
    cfg.update(MANGROVE_ACCESS_MODE='x402', X402_NETWORK='eip155:84532',
               X402_USDC_CONTRACT='original-test-contract')
    config.write_text(json.dumps(cfg))
    args = arguments()
    args.auth, args.yes = 'x402', False
    monkeypatch.setattr(setup, 'choose', lambda *a: selection)
    setup.configure(args)
    after = json.loads(config.read_text())
    assert after['X402_NETWORK'] == expected
    assert after['X402_USDC_CONTRACT'] == ('original-test-contract' if selection == '2' else
                                           '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX interactive terminal')
@pytest.mark.parametrize('action,foreground', [('create', False), ('create', True)])
@pytest.mark.parametrize('terminal_type', ['dumb', 'xterm-256color'])
def test_guided_shell_one_session_applies_wallet_and_restarts(checkout, action, foreground, terminal_type):
    """Actual shell + PTY + synthetic HTTP wallet service. No real keys or payments."""
    import fcntl
    import pty
    import select
    import termios
    repo, env = checkout
    server = repo / 'server/uvicorn.py'
    source = server.read_text()
    source = source.replace('class Handler', '''with open('starts.jsonl', 'a') as f: f.write(json.dumps(cfg)+'\\n')
class Handler''')
    source = source.replace("        message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))", '''        message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.startswith('/api/v1/agent/wallet/'):
            with open('mutations.jsonl', 'a') as f: f.write(json.dumps({'path':self.path})+'\\n')
            if self.path.endswith('/stash-secret'):
                body = {'vault_token': 'synthetic_token_1234567890'}
            else:
                body = {'address': '0x' + '1'*40}
                Path('wallets.json').write_text(json.dumps([dict(body, chain='evm', backup_confirmed_at='confirmed' if self.path.endswith('/import') else None)]))
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(body).encode()); return''')
    source = source.replace("        else: body, code = [], 200 if valid else 401", '''        elif self.path.endswith('/wallet/list'):
            body = json.loads(Path('wallets.json').read_text()) if Path('wallets.json').exists() else []
            code = 200 if valid else 401
        elif self.path.endswith('/brokers/robinhood/status'):
            body, code = {'connected': Path('robinhood-connected').exists()}, 200
        elif self.path.endswith('/x402/spend'):
            body, code = {'cap_usd': 25, 'cap_source': 'config', 'exhausted': False}, 200
        else: body, code = [], 200 if valid else 401''')
    server.write_text(source)
    (repo / 'scripts/robinhood_connect.py').write_text(
        "from pathlib import Path\nPath('robinhood-connected').touch()\n")
    env = dict(env, TERM=terminal_type)
    master, terminal = pty.openpty()
    def control_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    args = ['bash', str(repo / 'scripts/setup.sh'), '--auth', 'x402', '--no-mcp', '--no-verify']
    if foreground:
        args.append('--foreground')
    proc = subprocess.Popen(args, cwd=repo, env=env, stdin=terminal, stdout=terminal, stderr=terminal,
                            preexec_fn=control_terminal)
    output = b''
    def expect(text, timeout=25):
        nonlocal output
        deadline = time.monotonic() + timeout
        while text.encode() not in output:
            if time.monotonic() > deadline:
                pytest.fail('Missing prompt: ' + text + '\\n' + output.decode(errors='replace'))
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b''
                if not chunk:
                    pytest.fail('Unexpected exit: ' + output.decode(errors='replace'))
                output += chunk
    def send(text):
        os.write(master, (text + '\n').encode())
    try:
        expect('Set up your payment wallet:')
        send('1' if action == 'create' else '2')
        expect('Type ' + action.upper())
        send(action.upper())
        if action == 'import':
            expect('Type BACKED UP')
            send('BACKED UP')
            expect('Private key or recovery phrase (hidden; never paste into chat):')
            send('synthetic-secret')
            expect('Does this address match')
            send('1')
        else:
            expect('Ready to continue?')
            # This emulates the independently confirmed backup state, without
            # revealing any secret in the test harness.
            wallet_file = repo / 'wallets.json'
            wallets = json.loads(wallet_file.read_text())
            wallets[0]['backup_confirmed_at'] = 'confirmed-in-private-terminal'
            wallet_file.write_text(json.dumps(wallets))
            send('')
        expect('Spending cap in USD [25]')
        send('')
        expect('Type SELECT')
        send('SELECT')
        expect('Wallet configuration complete.')
        expect('Connect Robinhood?')
        send('1' if action == 'import' else '2')
        expect('Robinhood: connected and verified.' if action == 'import' else 'Robinhood: skipped;')
        expect('Setup complete')
        if foreground:
            expect('Agent attached to this terminal')
            proc.terminate()
        # Keep draining the PTY after the completion heading: the launch
        # instructions follow it, and terminal output can block process exit.
        deadline = time.monotonic() + 15
        while proc.poll() is None and time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
        proc.wait(timeout=5)
        assert proc.returncode == (143 if foreground else 0), output.decode(errors='replace')
        starts = [json.loads(line) for line in (repo / 'starts.jsonl').read_text().splitlines()]
        assert len(starts) == 2 and starts[-1]['X402_PAYER_WALLET'] == ADDR
        assert starts[-1]['X402_NETWORK'] == 'eip155:8453'
        mutations = (repo / 'mutations.jsonl').read_text()
        assert mutations.count('/' + action + '"') == 1
        assert '/confirm-backup' not in mutations
        assert not (repo / 'agent-data/wallet-setup.json').exists()
        assert not (repo / 'agent-data/.setup.lock').exists()
        assert (repo / 'robinhood-connected').exists() == (action == 'import')
        assert b'synthetic-secret' not in output
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        os.close(terminal)
        os.close(master)


def test_empty_saved_wallet_selection_returns_to_menu(wizard, monkeypatch, capsys):
    cfg, _, checkpoint = wizard
    choices = iter(['3', '4'])
    monkeypatch.setattr(setup, 'choose', lambda *a: next(choices))
    setup.onboard(cfg)
    assert not checkpoint.exists()
    assert 'No saved EVM wallet' in capsys.readouterr().out


def test_checkpoint_symlink_is_rejected(wizard, tmp_path):
    cfg, _, checkpoint = wizard
    target = tmp_path / 'other.json'
    target.write_text('{}')
    checkpoint.symlink_to(target)
    with pytest.raises(setup.SetupError, match='symlink'):
        setup.onboard(cfg)
    assert target.read_text() == '{}'


def test_finish_does_not_discard_unfinished_or_other_mode_progress(wizard):
    cfg, _, checkpoint = wizard
    state = dict(stage='wallet', action='create', network=cfg['X402_NETWORK'], address=ADDR)
    setup.save_onboarding(state)
    setup.finish_onboarding(cfg)
    assert checkpoint.exists()
    state['stage'] = 'selected'
    setup.save_onboarding(state)
    cfg.update(X402_PAYER_WALLET=ADDR, MANGROVE_ACCESS_MODE='oauth')
    setup.finish_onboarding(cfg)
    assert checkpoint.exists()


def test_docker_wizard_recreates_after_selection_without_recursive_setup(checkout):
    """Shell orchestration only: a fake Docker executable never touches a daemon."""
    repo, env = checkout
    env.pop('BARE_PORT')
    docker = repo / '.venv/bin/docker'
    docker.write_text(f'#!{sys.executable}\nimport json,sys\nwith open("docker-calls.jsonl","a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
    docker.chmod(0o755)
    env['PATH'] = str(docker.parent) + os.pathsep + env['PATH']
    helper = repo / 'scripts/setup_support.py'
    hook = '''
def check(cfg):
    return []
def check_markets(cfg):
    pass
def onboard(cfg, *, yes=False):
    assert (ROOT / 'agent-data/.setup.lock/pid').exists()
    cfg['X402_PAYER_WALLET'] = '0x' + '1'*40
    atomic_config(cfg)
    save_onboarding(dict(stage='selected', action='create', address=cfg['X402_PAYER_WALLET'], network=cfg['X402_NETWORK']))
'''
    helper.write_text(helper.read_text().replace("if __name__ == '__main__':", hook + "\nif __name__ == '__main__':"))
    result = run_setup(checkout, '--docker', '--yes', '--no-mcp', '--no-verify')
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in (repo / 'docker-calls.jsonl').read_text().splitlines()]
    assert calls == [['info'], ['compose', 'up', '-d', '--build', '--force-recreate'],
                     ['compose', 'up', '-d', '--force-recreate']]
    assert 'Wallet configuration complete' in result.stdout
    assert not (repo / 'agent-data/wallet-setup.json').exists()
    assert not (repo / 'agent-data/.setup.lock').exists()


@pytest.mark.parametrize('action', ['reveal', 'confirm'])
def test_retired_setup_backup_ignores_other_agent_environment(backup, monkeypatch, capsys, action):
    monkeypatch.setenv('CONFIG_FILE', '/other-agent/config.json')
    monkeypatch.setenv('LOCAL_AGENT_URL', 'https://untrusted.invalid')
    wire = Mock(side_effect=AssertionError('No HTTP allowed'))
    prompt = Mock(side_effect=AssertionError('No confirmation allowed'))
    monkeypatch.setattr(setup, 'request', wire)
    monkeypatch.setattr('builtins.input', prompt)
    with pytest.raises(setup.SetupError, match='isolated custody administrator'):
        backup.run(action, None, setup=True)
    wire.assert_not_called()
    prompt.assert_not_called()
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('stage,network', [('pending', 'eip155:8453'), ('wallet', 'eip155:84532')])
def test_retired_setup_backup_does_not_read_old_checkpoint(backup, monkeypatch, stage, network):
    reader = Mock(return_value={'stage': stage, 'network': network, 'address': ADDR})
    monkeypatch.setattr(setup, 'read_config', reader)
    with pytest.raises(setup.SetupError, match='isolated custody administrator'):
        backup.run('confirm', None, setup=True)
    reader.assert_not_called()


def test_retired_setup_backup_cannot_be_reenabled_by_acknowledgment(backup, monkeypatch):
    prompt = Mock(return_value='BACKED UP')
    wire = Mock()
    monkeypatch.setattr('builtins.input', prompt)
    monkeypatch.setattr(setup, 'request', wire)
    with pytest.raises(setup.SetupError, match='isolated custody administrator'):
        backup.run('confirm', None, setup=True)
    wire.assert_not_called()
    prompt.assert_not_called()


def test_wizard_step_plain_text_wraps_without_escape_codes(monkeypatch, capsys):
    monkeypatch.setattr(setup, '_terminal_columns', lambda: 40)
    monkeypatch.setenv('NO_COLOR', '1')
    setup.flow_step(2, 'Save your backup', 'Keep this window open. In a second private terminal:')
    output = capsys.readouterr().out
    assert '\x1b' not in output
    assert 'WALLET SETUP  2 / 4' in output
    assert max(map(len, output.splitlines())) <= 40
    assert '\n\n' in output


@pytest.mark.parametrize('backed_up', [True, False])
def test_saved_wallet_menu_can_switch_away_from_current(wizard, monkeypatch, backed_up):
    cfg, wallets, checkpoint = wizard
    other = '0x' + '2' * 40
    cfg['X402_PAYER_WALLET'] = ADDR
    wallets.extend([
        dict(address=ADDR, chain='evm', backup_confirmed_at='confirmed'),
        dict(address=other, chain='evm', backup_confirmed_at='confirmed' if backed_up else None),
    ])
    menus = []
    answers = iter(['3', '2'])
    def choose(prompt, options, default):
        menus.append((prompt, options, default))
        return next(answers)
    monkeypatch.setattr(setup, 'choose', choose)
    backup_check = Mock(return_value=backed_up)
    monkeypatch.setattr(setup, 'wait_for_backup', backup_check)
    select_wallet = Mock()
    monkeypatch.setattr(setup, 'wallet_action', select_wallet)
    setup.onboard(cfg)
    options = menus[1][1]
    assert 'Current' in options['1'] and 'Backed up' in options['1']
    assert ADDR in options['1'] and other in options['2']
    assert ('Backed up' if backed_up else 'Backup needed') in options['2']
    backup_check.assert_called_once_with(cfg, other)
    if backed_up:
        select_wallet.assert_called_once_with('select', cfg, guided=True, selected_address=other)
    else:
        select_wallet.assert_not_called()
    assert json.loads(checkpoint.read_text())['address'] == other


def test_saved_wallet_menu_defaults_to_current_and_can_defer(monkeypatch):
    other = '0x' + '2' * 40
    wallets = [dict(address=ADDR, backup_confirmed_at=None),
               dict(address=other, backup_confirmed_at='confirmed')]
    menu = Mock(return_value='l')
    monkeypatch.setattr(setup, 'choose', menu)
    assert setup.choose_saved_wallet(wallets, payer=other.upper()) is None
    assert menu.call_args.args[2] == '2'
    assert 'Current' in menu.call_args.args[1]['2']


@pytest.mark.parametrize('has_payer', [False, True])
def test_wallet_action_menu_always_highlights_first_option(wizard, monkeypatch, has_payer):
    cfg, wallets, _ = wizard
    if has_payer:
        cfg['X402_PAYER_WALLET'] = ADDR
        wallets.append(dict(address=ADDR, chain='evm', backup_confirmed_at='confirmed'))
    menu = Mock(return_value='4')
    monkeypatch.setattr(setup, 'choose', menu)
    setup.onboard(cfg)
    menu.assert_called_once()
    assert menu.call_args.args[2] == '1'
    assert menu.call_args.args[1]['1'] == 'Create a new wallet'



def test_unfinished_create_can_switch_to_import_without_deleting_wallet(wizard, monkeypatch):
    cfg, wallets, checkpoint = wizard
    other = '0x' + '2' * 40
    cfg['X402_PAYER_WALLET'] = ADDR
    wallets.append(dict(address=ADDR, chain='evm', backup_confirmed_at=None))
    setup.save_onboarding(dict(stage='wallet', action='create', network=cfg['X402_NETWORK'], address=ADDR))
    choices = iter(['2', '2', '1'])  # change setup option, import, verify imported address
    monkeypatch.setattr(setup, 'choose', lambda *a: next(choices))
    monkeypatch.setattr(setup, 'wait_for_backup', lambda *a: False)
    def operation(action, config, **kwargs):
        assert action == 'import'
        kwargs['before_mutation']()
        wallets.append(dict(address=other, chain='evm', backup_confirmed_at=None))
        return other
    action = Mock(side_effect=operation)
    monkeypatch.setattr(setup, 'wallet_action', action)
    setup.onboard(cfg)
    action.assert_called_once()
    assert [w['address'] for w in wallets] == [ADDR, other]
    assert cfg['X402_PAYER_WALLET'] == ADDR  # switching menus doesn't authorize a payer change
    saved = json.loads(checkpoint.read_text())
    assert saved['action'] == 'import' and saved['address'] == other


def test_unfinished_wallet_finish_later_preserves_checkpoint(wizard, monkeypatch):
    cfg, _, checkpoint = wizard
    setup.save_onboarding(dict(stage='wallet', action='create', network=cfg['X402_NETWORK'], address=ADDR))
    before = checkpoint.read_bytes()
    monkeypatch.setattr(setup, 'choose', lambda *a: '3')
    mutation = Mock(side_effect=AssertionError('No wallet mutation'))
    monkeypatch.setattr(setup, 'wallet_action', mutation)
    setup.onboard(cfg)
    assert checkpoint.read_bytes() == before
    mutation.assert_not_called()


def test_pending_mutation_does_not_offer_discard(wizard, monkeypatch):
    cfg, _, checkpoint = wizard
    setup.save_onboarding(dict(stage='pending', action='create', network=cfg['X402_NETWORK']))
    menu = Mock(return_value='2')
    monkeypatch.setattr(setup, 'choose', menu)
    setup.onboard(cfg)
    assert menu.call_args.args[0] == 'Recover the existing operation:'
    assert 'Choose a different setup option' not in menu.call_args.args[1].values()
    assert json.loads(checkpoint.read_text())['stage'] == 'pending'


@pytest.mark.parametrize('value', ['', None, 'null', 'REPLACE_WITH_YOUR_DEV_OR_PROD_KEY'])
def test_missing_api_key_explains_before_secret_prompt(config, monkeypatch, value):
    cfg = setup.read_config(setup.EXAMPLE)
    cfg.update(MANGROVE_API_KEY=value, MANGROVE_ACCESS_MODE='x402')
    config.write_text(json.dumps(cfg))
    menu = Mock(return_value='1')
    monkeypatch.setattr(setup, 'choose', menu)
    secret = Mock(return_value='prod_synthetic')
    monkeypatch.setattr(setup, 'hidden', secret)
    args = arguments()
    args.auth, args.yes = 'api-key', False
    setup.configure(args)
    menu.assert_called_once_with('No MangroveAI API key is saved in this checkout.', {
        '1': 'Enter a new API key', '2': 'Choose another access method'}, '1')
    secret.assert_called_once()
    assert json.loads(config.read_text())['MANGROVE_API_KEY'] == 'prod_synthetic'


def test_missing_api_key_can_return_to_x402_without_secret_entry(config, monkeypatch):
    setup.configure(arguments())
    before = json.loads(config.read_text())
    choices = iter(['1', '2', '2'])  # API key, go back, x402
    monkeypatch.setattr(setup, 'choose', lambda *a: next(choices))
    secret = Mock(side_effect=AssertionError('No key should be requested'))
    monkeypatch.setattr(setup, 'hidden', secret)
    args = arguments()
    args.yes = False
    setup.configure(args)
    assert json.loads(config.read_text()) == before
    secret.assert_not_called()


@pytest.mark.parametrize('options', [{'yes': True}, {'action': 'skip'}, {}])
def test_robinhood_skip_never_contacts_broker_or_opens_browser(monkeypatch, capsys, options):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: False)
    request, run = Mock(), Mock()
    monkeypatch.setattr(setup, 'request', request)
    monkeypatch.setattr(setup.subprocess, 'run', run)
    setup.robinhood_setup({}, **options)
    request.assert_not_called()
    run.assert_not_called()
    assert '--robinhood connect' in capsys.readouterr().out


def test_robinhood_interactive_connect_is_default_and_skip_is_available(monkeypatch):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: True)
    choose, request = Mock(return_value='2'), Mock()
    monkeypatch.setattr(setup, 'choose', choose)
    monkeypatch.setattr(setup, 'request', request)
    setup.robinhood_setup({})
    assert choose.call_args.args[2] == '1'
    request.assert_not_called()


def test_robinhood_reuses_verified_connection_without_launching_connector(monkeypatch, capsys):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: True)
    request, run = Mock(return_value={'connected': True}), Mock()
    monkeypatch.setattr(setup, 'request', request)
    monkeypatch.setattr(setup.subprocess, 'run', run)
    setup.robinhood_setup({}, action='connect')
    request.assert_called_once_with({}, '/api/v1/agent/brokers/robinhood/status', timeout=60)
    run.assert_not_called()
    assert 'connected and verified' in capsys.readouterr().out


def test_robinhood_connect_uses_existing_connector_and_verifies_result(monkeypatch, capsys):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: True)
    request = Mock(side_effect=[setup.SetupError('private broker error'), {'connected': True}])
    run = Mock(return_value=argparse.Namespace(returncode=0))
    monkeypatch.setattr(setup, 'request', request)
    monkeypatch.setattr(setup.subprocess, 'run', run)
    cfg = {'MANGROVE_ACCESS_MODE': 'oauth'}
    setup.robinhood_setup(cfg, action='connect')
    assert run.call_args.args[0] == [str(ROOT / '.venv/bin/python3'), str(ROOT / 'scripts/robinhood_connect.py')]
    assert run.call_args.kwargs['env']['ENVIRONMENT'] == 'local'
    assert run.call_args.kwargs['cwd'] == ROOT
    assert cfg == {'MANGROVE_ACCESS_MODE': 'oauth'}
    output = capsys.readouterr().out
    assert 'connected and verified' in output
    assert 'requires Mangrove API-key or x402' in output
    assert 'private broker error' not in output


@pytest.mark.parametrize('outcome', [argparse.Namespace(returncode=1),
                                   subprocess.TimeoutExpired('connector', 360), KeyboardInterrupt()])
def test_robinhood_failure_or_cancel_can_skip_without_false_success(monkeypatch, capsys, outcome):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(setup, 'request', Mock(side_effect=setup.SetupError('private error')))
    run = Mock(side_effect=outcome) if isinstance(outcome, BaseException) else Mock(return_value=outcome)
    monkeypatch.setattr(setup.subprocess, 'run', run)
    monkeypatch.setattr(setup, 'choose', lambda *a: '2')
    setup.robinhood_setup({}, action='connect')
    output = capsys.readouterr().out
    assert 'not verified; any saved grant is retained' in output
    assert 'connected and verified' not in output
    assert 'private error' not in output


def test_robinhood_retry_rechecks_saved_connection(monkeypatch):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: True)
    request = Mock(side_effect=[setup.SetupError('unavailable'), {'connected': True}])
    monkeypatch.setattr(setup, 'request', request)
    run = Mock(return_value=argparse.Namespace(returncode=1))
    monkeypatch.setattr(setup.subprocess, 'run', run)
    monkeypatch.setattr(setup, 'choose', lambda *a: '1')
    setup.robinhood_setup({}, action='connect')
    assert request.call_count == 2
    assert run.call_count == 1


def test_robinhood_docker_does_not_launch_host_connector(monkeypatch, capsys):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: True)
    run = Mock()
    monkeypatch.setattr(setup.subprocess, 'run', run)
    setup.robinhood_setup({}, docker=True)
    assert 'Docker browser login needs container callback routing' in ' '.join(capsys.readouterr().out.split())
    with pytest.raises(setup.SetupError, match='bare-metal'):
        setup.robinhood_setup({}, docker=True, action='connect')
    run.assert_not_called()


@pytest.mark.parametrize('options', [ ['--robinhood', 'wrong'], ['--robinhood'],
    ['--robinhood', 'connect', '--yes'], ['--robinhood', 'connect', '--docker'],
    ['--robinhood', 'skip', '--wallet', 'list'], ['--robinhood', 'skip', '--oauth', 'status']])
def test_robinhood_invalid_options_fail_before_config_changes(checkout, options):
    repo, _ = checkout
    result = run_setup(checkout, *options)
    assert result.returncode != 0
    assert not (repo / 'server/src/config/local-config.json').exists()
    assert not (repo / 'agent-data').exists()


def test_robinhood_shell_passes_skip_option_to_setup_helper(checkout):
    result = run_setup(checkout, '--yes', '--no-mcp', '--no-verify', '--robinhood', 'skip')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Robinhood: skipped; existing connection unchanged.' in result.stdout
    repo, _ = checkout
    assert '/brokers/' not in (repo / 'requests.jsonl').read_text()


def test_robinhood_mid_login_skip_continues_without_retry_prompt(monkeypatch, capsys):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: True)
    request = Mock(side_effect=setup.SetupError('not connected'))
    monkeypatch.setattr(setup, 'request', request)
    monkeypatch.setattr(setup.subprocess, 'run', Mock(return_value=argparse.Namespace(returncode=2)))
    choose = Mock()
    monkeypatch.setattr(setup, 'choose', choose)
    setup.robinhood_setup({}, action='connect')
    choose.assert_not_called()
    assert request.call_count == 1
    output = capsys.readouterr().out
    assert 'Robinhood: skipped;' in output
    assert 'connected and verified' not in output

@pytest.mark.parametrize('connected', [True, False])
def test_oauth_setup_rejects_unavailable_catalog(monkeypatch, capsys, connected):
    cfg = {'API_KEYS': 'local-private', 'AUTH_ENABLED': True, 'MANGROVE_ACCESS_MODE': 'oauth'}
    started = []
    def request(cfg, path, payload=None, **kwargs):
        if path.endswith('/status'):
            return {'connected': connected or bool(started), 'pending': False,
                    'expires_at': 456 if started else 123}
        if path.endswith('/start'):
            started.append(True)
            return {'authorization_url': 'https://api.mangrovedeveloper.ai/oauth/authorize?state=example'}
        if path.endswith('/tools'):
            return {'mangroveai_status': 'unavailable', 'tools': [{'name': 'status'}]}
        return {}
    monkeypatch.setattr(setup, 'request', request)
    monkeypatch.setattr(setup.webbrowser, 'open', Mock())
    with pytest.raises(setup.SetupError, match='verification failed.*Setup is incomplete'):
        setup.oauth_command(cfg, 'oauth-setup', [])
    output = capsys.readouterr().out
    assert 'Using your saved OAuth connection' not in output
    assert 'Connected to Mangrove.' not in output


def test_final_verification_cannot_skip_failed_oauth_catalog(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['setup_support.py', 'verify'])
    monkeypatch.setattr(setup, 'read_config', lambda: {'MANGROVE_ACCESS_MODE': 'oauth'})
    monkeypatch.setattr(setup, 'check', lambda cfg: None)
    monkeypatch.setattr(setup, 'check_upstream', lambda cfg: True)
    def request(cfg, path, **kwargs):
        if path == '/health':
            return {'scheduler_running': True}
        return {'mangroveai_status': 'unavailable', 'tools': [
            {'name': name} for name in ['create_wallet', 'import_wallet', 'list_wallets']]}
    monkeypatch.setattr(setup, 'request', request)
    assert setup.main() == 1
    assert 'setup is incomplete' in capsys.readouterr().err


def test_oauth_status_shows_cleanup_for_still_connected_grant(monkeypatch, capsys):
    cfg = {'API_KEYS': 'synthetic-local', 'AUTH_ENABLED': True, 'MANGROVE_ACCESS_MODE': 'oauth'}
    monkeypatch.setattr(setup, 'request', lambda *a, **k: {
        'connected': True, 'revocation_pending': True,
        'action': 'Run ./scripts/setup.sh --oauth disconnect to finish cleanup.'})
    setup.oauth_command(cfg, 'oauth-status', [])
    output = capsys.readouterr().out
    assert 'Connected.' in output
    assert '--oauth disconnect' in output
