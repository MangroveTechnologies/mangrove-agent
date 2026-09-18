"""Setup tests use isolated checkouts/synthetic HTTP servers, never real wallets."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('setup_support', ROOT / 'scripts/setup_support.py')
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
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
    assert cfg['X402_NETWORK'] == 'eip155:84532'
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


def test_explicit_switch_clears_only_upstream_key(config):
    setup.configure(arguments())
    cfg = json.loads(config.read_text())
    cfg['MANGROVE_API_KEY'] = 'prod_previous'
    config.write_text(json.dumps(cfg))
    args = arguments()
    args.auth = 'x402'
    setup.configure(args)
    after = json.loads(config.read_text())
    assert after['MANGROVE_API_KEY'] == ''
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
    if command:
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


def test_import_uses_hidden_entry_and_in_memory_vault_handoff(interactive, monkeypatch, capsys):
    monkeypatch.setattr(setup, 'check', lambda cfg: [])
    replies = iter(['IMPORT', 'BACKED UP'])
    monkeypatch.setattr('builtins.input', lambda _: next(replies))
    monkeypatch.setattr(setup, 'hidden', lambda _: 'synthetic-secret')
    token = 'synthetic_vault_token_12345'
    wire = Mock(side_effect=[{'vault_token': token}, {'address': ADDR}])
    monkeypatch.setattr(setup, 'request', wire)
    setup.wallet_action('import', wallet_config())
    assert wire.call_count == 2
    assert wire.call_args_list[0].args[1].endswith('/stash-secret')
    assert wire.call_args_list[1].args[2]['vault_token'] == token
    assert token not in capsys.readouterr().out


def test_select_requires_backup_before_config_mutation(interactive, monkeypatch):
    monkeypatch.setattr(setup, 'check', lambda cfg: [dict(address=ADDR, chain='evm', chain_id=84532,
                                                        network='testnet', backup_confirmed_at=None)])
    monkeypatch.setattr(setup, 'choose', lambda *a: '1')
    write = Mock()
    monkeypatch.setattr(setup, 'atomic_config', write)
    with pytest.raises(setup.SetupError, match='Back up'):
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


@pytest.fixture
def checkout(tmp_path):
    """Run actual setup.sh against a fake uvicorn module, without pip/network installs."""
    repo = tmp_path / 'checkout with spaces'
    (repo / 'scripts').mkdir(parents=True)
    (repo / 'server/src/config').mkdir(parents=True)
    (repo / '.claude').mkdir()
    for name in ['setup.sh', 'setup_support.py', 'verify_quickstart.sh', 'setup-mcp.sh']:
        shutil.copy2(ROOT / 'scripts' / name, repo / 'scripts' / name)
    shutil.copy2(ROOT / 'server/src/config/local-example-config.json', repo / 'server/src/config/local-example-config.json')
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
    def do_GET(self):
        with open('requests.jsonl', 'a') as f: f.write(json.dumps({'path':self.path})+'\\n')
        valid = self.headers.get('X-API-Key') == cfg['API_KEYS'].split(',')[0]
        if self.path.endswith('/tools'):
            body = {'tools':[{'name':n} for n in ['create_wallet','import_wallet','list_wallets','list_signals']]}
            code = 200
        elif self.path == '/health': body, code = {'scheduler_running': True}, 200
        else: body, code = [], 200 if valid else 401
        self.send_response(code); self.end_headers(); self.wfile.write(json.dumps(body).encode())
HTTPServer(('127.0.0.1', int(sys.argv[sys.argv.index('--port')+1])), Handler).serve_forever()
''')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, BARE_PORT=str(port))
    for key in ['BASE_URL', 'BARE_HOST', 'MANGROVE_AGENT_HOME', 'PYTHONPATH']:
        env.pop(key, None)
    yield repo, env
    pid = repo / 'agent-data/bare.pid'
    if pid.exists():
        try:
            os.kill(int(pid.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass


def run_setup(checkout, *args, input=None):
    repo, env = checkout
    return subprocess.run(['bash', str(repo / 'scripts/setup.sh'), *args], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=25, input=input)


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
    requests = [json.loads(line)['path'] for line in (repo / 'requests.jsonl').read_text().splitlines()]
    assert set(requests) == {'/api/v1/agent/wallet/list', '/api/v1/agent/tools', '/health'}


def test_shell_verification_failure_is_failure(checkout):
    repo, _ = checkout
    (repo / 'scripts/verify_quickstart.sh').write_text('#!/bin/bash\nexit 42\n')
    result = run_setup(checkout, '--yes', '--no-mcp')
    assert result.returncode != 0
    assert 'Verification failed' in result.stderr
    assert 'Done.' not in result.stdout


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
    assert 'Local agent authenticated' in ''.join(output)
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
    env['CLAUDE_CODE_MCP_SERVER_URL'] = 'https://example.invalid/mcp/'
    denied = subprocess.run(shlex.split(registration['headersHelper']), env=env,
                            capture_output=True, text=True)
    assert denied.returncode != 0
    assert cfg['API_KEYS'] not in denied.stdout + denied.stderr
    assert cfg['API_KEYS'] not in result.stdout + result.stderr


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
    menu.assert_called_once()
    assert set(menu.call_args.args[1]) == {'1', '2'}
    assert menu.call_args.args[2] == ('1' if existing_key else '2')
    after = json.loads(config.read_text())
    expected_key = existing_key if selection == 'default' else (existing_key or 'prod_new') if selection == '1' else ''
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
        assert len(events) == 1
        assert '/credential-leak' not in events
        assert 'synthetic' not in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('url', ['https://example.invalid', 'http://192.0.2.1', 'http://user:password@127.0.0.1'])
def test_backup_rejects_nonlocal_targets_before_request(backup, tmp_path, monkeypatch, url):
    cfg = tmp_path / 'local-config.json'
    cfg.write_text(json.dumps({'API_KEYS': 'synthetic-local-key', 'AUTH_ENABLED': True}))
    monkeypatch.setenv('CONFIG_FILE', str(cfg))
    monkeypatch.setenv('LOCAL_AGENT_URL', url)
    wire = Mock()
    monkeypatch.setattr(backup, 'request', wire)
    with pytest.raises(setup.SetupError, match='loopback'):
        backup.run('confirm', ADDR, True)
    assert not wire.called


def test_backup_reveal_refuses_redirected_output(backup, tmp_path, monkeypatch):
    cfg = tmp_path / 'local-config.json'
    cfg.write_text(json.dumps({'API_KEYS': 'synthetic-local-key', 'AUTH_ENABLED': True}))
    monkeypatch.setenv('CONFIG_FILE', str(cfg))
    monkeypatch.setenv('LOCAL_AGENT_URL', 'http://127.0.0.1:9080')
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: False)
    wire = Mock()
    monkeypatch.setattr(backup, 'request', wire)
    with pytest.raises(setup.SetupError, match='private terminal'):
        backup.run('reveal', ADDR, True)
    assert not wire.called


def test_verifier_rejects_stopped_scheduler(checkout):
    repo, _ = checkout
    fake = repo / 'server/uvicorn.py'
    fake.write_text(fake.read_text().replace("'scheduler_running': True", "'scheduler_running': False"))
    result = run_setup(checkout, '--yes', '--no-mcp')
    assert result.returncode != 0
    assert 'Scheduler is not ready' in result.stdout + result.stderr
    assert 'Done.' not in result.stdout


@pytest.mark.parametrize('addr', [ADDR, '1' * 32])
@pytest.mark.parametrize('action', ['reveal', 'confirm'])
def test_backup_success_preserves_both_wallet_families(backup, tmp_path, monkeypatch, capsys, addr, action):
    cfg = tmp_path / 'local-config.json'
    cfg.write_text(json.dumps({'API_KEYS': 'synthetic-local-key', 'AUTH_ENABLED': True}))
    monkeypatch.setenv('CONFIG_FILE', str(cfg))
    monkeypatch.setenv('LOCAL_AGENT_URL', 'http://localhost:9080')
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    wire = Mock(return_value={'address': addr, 'secret': 'synthetic wallet backup',
                              'backup_confirmed_at': '2026-09-18T00:00:00Z', 'message': 'untrusted message'})
    monkeypatch.setattr(backup, 'request', wire)
    backup.run(action, addr, True)
    output = capsys.readouterr().out
    assert addr in output
    assert 'untrusted message' not in output
    assert ('synthetic wallet backup' in output) is (action == 'reveal')
    assert wire.call_args.args[0]['LOCAL_AGENT_URL'] == 'http://127.0.0.1:9080'
