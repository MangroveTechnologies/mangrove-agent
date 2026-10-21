"""Exercise real launcher processes without a model, network, or real credentials."""
import importlib.util
import json
import os
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
    for relative in ('scripts', '.codex', '.grok'):
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
def test_native_client_receives_key_in_environment_only(installation, tmp_path, client):
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
                            env=dict(os.environ, PATH=f'{binary}{os.pathsep}{os.environ["PATH"]}',
                                     CAPTURE=str(capture)))
    assert result.returncode == 0, result.stderr
    data = json.loads(capture.read_text())
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
    assert 'Required Sage instructions are missing' in result.stderr
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
    assert '**Name:** Sage' in result.stdout
    assert 'Client-only skills:' in result.stdout
    assert 'NOT CONFIGURED:' in result.stdout
