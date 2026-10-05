"""Exercise launcher boundaries and hooks without calling a model or real wallets."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    SPEC = importlib.util.spec_from_file_location('chat_launcher', ROOT / 'scripts/setup_support.py')
    chat = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(chat)
finally:
    sys.path.pop(0)


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "User B's agent with spaces"
    for name in ['scripts', '.claude/hooks']:
        shutil.copytree(ROOT / name, root / name)
    (root / 'server/src/config').mkdir(parents=True)
    (root / 'server/src/config/local-config.json').write_text(json.dumps({
        'AUTH_ENABLED': True, 'API_KEYS': 'synthetic-local-key',
        'LOCAL_AGENT_URL': 'http://127.0.0.1:9083',
    }))
    return root


def test_chat_launch_is_restricted_and_has_no_credentials_in_arguments(installation, tmp_path):
    binary = tmp_path / 'bin'
    binary.mkdir()
    capture = tmp_path / 'capture.json'
    claude = binary / 'claude'
    claude.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
args = sys.argv[1:]
profile = json.loads(pathlib.Path(args[args.index('--settings') + 1]).read_text())
pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({
    'args': args, 'cwd': os.getcwd(), 'profile': profile,
    'search': os.environ.get('ENABLE_TOOL_SEARCH'),
}))
''')
    claude.chmod(0o700)
    env = dict(os.environ, PATH=f'{binary}{os.pathsep}{os.environ["PATH"]}', CAPTURE=str(capture))
    result = subprocess.run(['bash', str(installation / 'scripts/chat.sh')], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(capture.read_text())
    args, profile = data['args'], data['profile']
    assert args[args.index('--tools') + 1] == ''
    assert args[args.index('--setting-sources') + 1] == ''
    assert args[args.index('--permission-mode') + 1] == 'default'
    assert '--strict-mcp-config' in args
    assert '--disable-slash-commands' not in args
    assert profile['permissions']['disableBypassPermissionsMode'] == 'disable'
    assert {'Bash', 'Read', 'Write', 'Edit', 'Agent', 'Task'} <= set(profile['permissions']['deny'])
    assert 'synthetic-local-key' not in capture.read_text()
    mcp = json.loads(args[args.index('--mcp-config') + 1])['mcpServers']
    assert set(mcp) == {'mangrove-agent'}
    assert mcp['mangrove-agent']['url'] == 'http://127.0.0.1:9083/mcp/'
    assert str(installation / 'scripts/setup_support.py') in shlex.split(mcp['mangrove-agent']['headersHelper'])
    assert data['cwd'] == str(installation / 'agent-data/chat')
    assert data['search'] == 'false'
    assert not Path(args[args.index('--settings') + 1]).exists()


@pytest.mark.parametrize('argument', ['--tools=Bash', '--dangerously-skip-permissions',
                                    '--settings=other.json', '--mcp-config=other.json', '--bare'])
def test_launcher_rejects_permission_overrides(installation, argument):
    result = subprocess.run(['bash', str(installation / 'scripts/chat.sh'), argument],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'does not accept' in result.stderr


@pytest.mark.parametrize('profile', ['project', 'restricted'])
@pytest.mark.parametrize('secret', [False, True])
def test_wallet_hook_works_outside_repo_with_quoted_path(installation, tmp_path, monkeypatch,
                                                       profile, secret):
    if profile == 'project':
        config = json.loads((ROOT / '.claude/settings.json').read_text())
    else:
        monkeypatch.setattr(chat, 'ROOT', installation)
        config = chat.chat_settings()
    command = config['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
    prompt = '0x' + 'a' * 64 if secret else 'Show my offers'
    result = subprocess.run(['/bin/sh', '-c', command], cwd=tmp_path,
                            env=dict(os.environ, CLAUDE_PROJECT_DIR=str(installation)),
                            input=json.dumps({'prompt': prompt}), capture_output=True, text=True)
    assert result.returncode == (2 if secret else 0), result.stderr
    assert 'No such file' not in result.stderr


def test_missing_hook_prevents_launch(installation, monkeypatch):
    (installation / '.claude/hooks/block-wallet-secrets.sh').unlink()
    monkeypatch.setattr(chat, 'ROOT', installation)
    with pytest.raises(chat.SetupError):
        chat.chat_settings()
