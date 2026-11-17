"""Exercise real launcher processes without a model, network, or real credentials."""
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('agent_context', ROOT / 'scripts/agent_context.py')
context = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(context)


def test_bundle_is_current_and_contains_only_reviewed_policy_sources():
    text = (ROOT / context.BUNDLE).read_text()
    assert text == context.render_context(ROOT)
    for source in context.SOURCES:
        assert f'## SOURCE: {source}\n' in text
    assert text.count('## SOURCE: ') == len(context.SOURCES)
    assert '/Users/' not in text
    assert text.isascii()
    # Budget for our complete baseline only, excluding host prompts/tool schemas.
    # Keep safety sources complete; move task references out of startup context.
    assert len(text.encode('utf-8')) <= 32 * 1024


def test_bundle_check_detects_policy_drift(tmp_path):
    for relative in context.SOURCES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, target)
    original = context.render_context(tmp_path)
    (tmp_path / 'SAGE.md').write_text((tmp_path / 'SAGE.md').read_text() + '\nNew local rule.\n')
    assert context.render_context(tmp_path) != original
    (tmp_path / '.claude/rules/wallet-presentation.md').unlink()
    with pytest.raises(FileNotFoundError):
        context.render_context(tmp_path)


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "Sage user's install"
    for relative in ('scripts', '.codex', '.grok', '.claude/hooks', 'server/src/shared'):
        shutil.copytree(ROOT / relative, root / relative,
                        ignore=shutil.ignore_patterns('config.toml', '__pycache__'))
    for relative in context.SOURCES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, target)
    config = root / 'server/src/config/local-config.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'AUTH_ENABLED': True, 'API_KEYS': 'synthetic-local-secret',
                                 'MANGROVE_API_KEY': 'synthetic-upstream-secret',
                                 'LOCAL_AGENT_URL': 'http://127.0.0.1:9087'}))
    return root


@pytest.mark.parametrize('client', ['codex', 'grok'])
@pytest.mark.parametrize('plugin_home', [False, True])
def test_native_client_receives_key_in_environment_only(installation, tmp_path, client, plugin_home):
    env = dict(os.environ)
    env.pop('MANGROVE_AGENT_HOME', None)
    if plugin_home:
        home = tmp_path / 'plugin state'
        (home / 'config').mkdir(parents=True)
        (installation / 'server/src/config/local-config.json').rename(home / 'config/local-config.json')
        env['MANGROVE_AGENT_HOME'] = str(home)
    binary = tmp_path / 'bin'
    binary.mkdir()
    capture = tmp_path / 'capture.json'
    cli = binary / client
    cli.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({
    'args': sys.argv[1:], 'cwd': os.getcwd(),
    'correct_key': os.environ.get('MANGROVE_LOCAL_API_KEY') == 'synthetic-local-secret',
    'url': os.environ.get('MANGROVE_AGENT_MCP_URL'),
}))
''')
    cli.chmod(0o700)
    result = subprocess.run([sys.executable, str(installation / 'scripts/client_session.py'), client],
                            cwd=tmp_path, capture_output=True, text=True,
                            env=dict(env, PATH=f'{binary}{os.pathsep}{os.environ["PATH"]}',
                                     CAPTURE=str(capture)))
    assert result.returncode == 0, result.stderr
    data = json.loads(capture.read_text())
    if client == 'codex':
        assert data['args'][::2] == ['-c', '-c', '-c']
        invocation = tomllib.loads('\n'.join(data['args'][1::2]))['hooks']
        assert set(invocation) == {'UserPromptSubmit', 'PreToolUse', 'PostToolUse'}
        assert all('--client codex' in invocation[event][0]['hooks'][0]['command'] for event in invocation)
        assert all(shlex.split(invocation[event][0]['hooks'][0]['command'])[1] ==
                   str((installation / 'scripts/wallet_secret_guard.py').resolve()) for event in invocation)
        assert 'bypass-hook-trust' not in ' '.join(data['args'])
    else:
        assert data['args'] == []
    assert data['cwd'] == str(installation)
    assert data['correct_key']
    assert data['url'] == 'http://127.0.0.1:9087/mcp/'
    settings_text = (installation / f'.{client}/config.toml').read_text()
    assert 'synthetic-' not in settings_text + result.stdout + result.stderr + capture.read_text()
    settings = tomllib.loads(settings_text)['mcp_servers']['mangrove-agent']
    if client == 'codex':
        assert settings['url'] == data['url']
        assert settings['env_http_headers']['X-API-Key'] == 'MANGROVE_LOCAL_API_KEY'
        assert 'hooks' not in tomllib.loads(settings_text)
    else:
        assert settings['headers']['X-API-Key'] == '${MANGROVE_LOCAL_API_KEY}'


@pytest.mark.parametrize('client', ['codex', 'grok'])
def test_unrelated_existing_client_settings_are_preserved(installation, tmp_path, client):
    settings = installation / f'.{client}/config.toml'
    original = '[mcp_servers.other]\ncommand = "preserve-me"\n'
    settings.write_text(original)
    binary = tmp_path / 'bin'
    binary.mkdir()
    cli = binary / client
    cli.write_text('#!' + sys.executable + '\nraise RuntimeError("must not launch")\n')
    cli.chmod(0o700)
    result = subprocess.run([sys.executable, str(installation / 'scripts/client_session.py'), client],
                            capture_output=True, text=True,
                            env=dict(os.environ, PATH=f'{binary}{os.pathsep}{os.environ["PATH"]}'))
    assert result.returncode != 0
    assert 'Existing settings were preserved' in result.stderr
    assert settings.read_text() == original


@pytest.mark.parametrize('client', ['codex', 'grok'])
def test_missing_policy_prevents_native_client_launch(installation, tmp_path, client):
    (installation / 'SAGE.md').unlink()
    binary = tmp_path / 'bin'
    binary.mkdir()
    sentinel = tmp_path / 'client-started'
    cli = binary / client
    cli.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n'
                   f'Path({str(sentinel)!r}).touch()\n')
    cli.chmod(0o700)
    result = subprocess.run([sys.executable, str(installation / 'scripts/client_session.py'), client],
                            capture_output=True, text=True,
                            env=dict(os.environ, PATH=f'{binary}{os.pathsep}{os.environ["PATH"]}'))
    assert result.returncode != 0
    assert 'Required agent instructions are missing' in result.stderr
    assert not sentinel.exists()
    assert not (installation / f'.{client}/config.toml').exists()


def test_plugin_injects_shared_policy_before_unconfigured_status(tmp_path):
    result = subprocess.run(['bash', str(ROOT / 'scripts/plugin-bootstrap.sh')],
                            capture_output=True, text=True,
                            env=dict(os.environ, CLAUDE_PLUGIN_ROOT=str(ROOT),
                                     MANGROVE_AGENT_HOME=str(tmp_path / 'empty-agent-home'),
                                     CLAUDE_PLUGIN_DATA=str(tmp_path / 'empty-plugin-data'),
                                     CLAUDE_PLUGIN_OPTION_MANGROVE_API_KEY=''))
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(context.render_context(ROOT))
    assert '**Name:** Unset' in result.stdout
    assert 'Client-only skills:' in result.stdout
    assert 'NOT CONFIGURED:' in result.stdout


@pytest.mark.parametrize('relative', ['scripts/wallet_secret_guard.py', 'server/src/shared/secret_guard.py',
                                    '.claude/hooks/bip39-english.txt', '.codex/session-hooks.toml'])
def test_missing_codex_hook_asset_prevents_launch(installation, tmp_path, relative):
    (installation / relative).unlink()
    binary = tmp_path / 'bin'
    binary.mkdir()
    capture = tmp_path / 'started'
    cli = binary / 'codex'
    cli.write_text('#!' + sys.executable + '\nfrom pathlib import Path\n'
                   f'Path({str(capture)!r}).touch()\n')
    cli.chmod(0o700)
    result = subprocess.run([sys.executable, str(installation / 'scripts/client_session.py'), 'codex'],
                            capture_output=True, text=True,
                            env=dict(os.environ, PATH=f'{binary}{os.pathsep}{os.environ["PATH"]}'))
    assert result.returncode != 0
    assert 'Required Codex wallet-secret hook is missing' in result.stderr
    assert not capture.exists()


@pytest.mark.parametrize('duplicate', [False, True, 'matched'])
def test_existing_codex_config_is_preserved_and_duplicate_hooks_are_refused(installation, duplicate):
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        spec = importlib.util.spec_from_file_location('client_session_hook_check', ROOT / 'scripts/client_session.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
    finally:
        sys.path.pop(0)
    path = installation / '.codex/config.toml'
    original = ('[mcp_servers.mangrove-agent]\nurl = "http://127.0.0.1:9087/mcp/"\n'
                'env_http_headers = { "X-API-Key" = "MANGROVE_LOCAL_API_KEY" }\n')
    if duplicate == 'matched':
        handler = tomllib.loads((installation / '.codex/session-hooks.toml').read_text())['hooks']['PreToolUse'][0]['hooks'][0]
        original += ('[[hooks.PreToolUse]]\nmatcher = "Bash"\n[[hooks.PreToolUse.hooks]]\n' +
                     '\n'.join(key + '=' + launcher.toml_literal(value) for key, value in handler.items()) + '\n')
    elif duplicate:
        original += (installation / '.codex/session-hooks.toml').read_text()
    path.write_text(original)
    if duplicate:
        with pytest.raises(launcher.SetupError, match='Remove duplicate wallet-secret hook tables'):
            launcher.prepare_config('codex', installation, 'http://127.0.0.1:9087/mcp/')
    else:
        assert launcher.prepare_config('codex', installation, 'http://127.0.0.1:9087/mcp/') == path
    assert path.read_text() == original


def test_native_codex_bound_guard_runs_from_another_cwd_with_spaces_and_quotes(installation, tmp_path):
    # The fixture installation contains both spaces and an apostrophe. A second
    # cwd must neither reselect the script nor break the shell's path quoting.
    code = ('import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
            'from client_session import codex_hook_arguments; '
            'print(json.dumps(codex_hook_arguments(Path(sys.argv[2]))))')
    result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts'), str(installation)],
                            cwd=tmp_path, capture_output=True, text=True, check=True)
    arguments = json.loads(result.stdout)
    hooks = tomllib.loads('\n'.join(arguments[1::2]))['hooks']
    for event, groups in hooks.items():
        command = groups[0]['hooks'][0]['command']
        assert 'git rev-parse' not in command and '$(' not in command
        assert shlex.split(command) == ['python3', str((installation / 'scripts/wallet_secret_guard.py').resolve()),
                                        '--client', 'codex']
        checked = subprocess.run(['/bin/sh', '-c', command], cwd=tmp_path,
                                 input=json.dumps({'hook_event_name': event, 'prompt': 'Show status',
                                                   'tool_input': {}, 'tool_response': {'ok': True}}),
                                 capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr


GROK_BOT_DOC = ROOT / 'docs/clients/grok-bot.md'


def _documented_flags(doc: str) -> dict[str, set[str]]:
    """Each script the doc's command blocks run, with the flags passed to it."""
    used: dict[str, set[str]] = {}
    for block in re.findall(r'```bash\n(.*?)```', doc, re.S):
        for command in re.split(r'\||&&|\n', block):
            match = re.search(r'scripts/([\w.-]+\.(?:py|sh))', command)
            if match:
                used.setdefault(match.group(1), set()).update(
                    re.findall(r'(?<![\w-])--[a-z][a-z0-9-]*', command[match.end():]))
    return used


def test_grok_bot_doc_is_linked_and_its_links_resolve():
    doc = GROK_BOT_DOC.read_text()
    assert doc.isascii()
    assert 'docs/clients/grok-bot.md' in (ROOT / 'AGENTS.md').read_text()
    assert '(clients/grok-bot.md)' in (ROOT / 'docs/clients.md').read_text()
    for target in re.findall(r'\]\((\.\./[^)#]+)', doc):
        assert (GROK_BOT_DOC.parent / target).resolve().is_file(), target
    # Policy stays in the shared sources; the host doc links to each of them.
    for source in context.SOURCES:
        assert source.rsplit('/', 1)[-1] in doc


def test_grok_bot_doc_uses_only_flags_its_scripts_accept():
    used = _documented_flags(GROK_BOT_DOC.read_text())
    assert used['setup.sh'] >= {'--auth', '--api-key-stdin', '--yes', '--no-mcp'}
    assert used['agent_context.py'] == {'--stdout'}
    assert used['local_mcp_bridge.py'] >= {'--call', '--arguments-file'}
    setup = (ROOT / 'scripts/setup.sh').read_text()
    for flag in used.pop('setup.sh'):
        assert re.search(rf'^\s+{re.escape(flag)}\)', setup, re.M), flag
    for script, flags in used.items():
        result = subprocess.run([sys.executable, str(ROOT / 'scripts' / script), '--help'],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        for flag in flags:
            assert re.search(rf'(?<![\w-]){re.escape(flag)}(?![\w-])', result.stdout), (script, flag)
