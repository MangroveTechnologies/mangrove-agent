"""Offline invalid/shape-only fixtures exercise native hook wire contracts."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'scripts/wallet_secret_guard.py'
SPEC = importlib.util.spec_from_file_location('wallet_secret_guard', SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
# Zero is outside the EVM private key range. These fixtures control no account.
ZERO = '0' * 64
HEX = '0x' + ZERO
PHRASE = ' '.join(['abandon'] * 12)  # Wordlist-shaped; deliberately invalid checksum.
XRPL = 'sEd' + '1' * 28  # Shape-only; deliberately invalid Base58Check.


def invoke(client, event, **kwargs):
    return subprocess.run([sys.executable, str(SCRIPT), '--client', client],
                          input=json.dumps({'hook_event_name': event, **kwargs}),
                          capture_output=True, text=True)


@pytest.mark.parametrize('secret', [ZERO, HEX, PHRASE, XRPL])
@pytest.mark.parametrize('envelope', [
    lambda s: {'private_key': s},
    lambda s: {'stdout': s, 'stderr': '', 'interrupted': False, 'isImage': False},
    lambda s: {'stdout': '', 'stderr': 'failure: ' + s, 'exit_code': 1},
    lambda s: {'content': [{'type': 'text', 'text': json.dumps({'private_key': s})}]},
    lambda s: {'content': [{'type': 'text', 'text': json.dumps(json.dumps({'secret': s}))}]},
    lambda s: {'structuredContent': {'payload': {'seed_phrase': s}}},
    lambda s: {'isError': True, 'content': [{'type': 'text', 'text': 'Rejected: ' + s}]},
    lambda s: {'secret': {'tx_hash': s}},
])
@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_nested_shell_mcp_and_error_results_are_blocked_without_echo(client, secret, envelope):
    original = envelope(secret)
    result = invoke(client, 'PostToolUse', tool_name='Bash', tool_response=original)
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output['decision'] == 'block'
    assert secret not in result.stdout + result.stderr
    if client == 'claude':
        clean = output['hookSpecificOutput']['updatedToolOutput']
        assert isinstance(clean, type(original))
        assert set(clean) == set(original)
        if 'interrupted' in original:
            assert clean['interrupted'] is False
    else:
        assert 'updatedMCPToolOutput' not in result.stdout
        assert 'suppressOutput' not in result.stdout
        assert 'updatedToolOutput' not in result.stdout


@pytest.mark.parametrize('value', [
    {'tx_hash': HEX}, {'transactionHash': HEX}, {'block_hash': HEX}, {'sha256': ZERO},
    {'content': [{'type': 'text', 'text': json.dumps({'tx_hash': HEX})}]},
    {'stdout': 'Transaction hash: ' + HEX},
    {'address': '0x' + '0' * 40}, {'message': 'Show my wallet balance and trading status.'},
])
def test_public_hashes_addresses_and_prose_pass(value):
    for client in ('claude', 'codex'):
        result = invoke(client, 'PostToolUse', tool_response=value)
        assert result.returncode == 0
        assert result.stdout == result.stderr == ''


def test_public_hash_does_not_hide_separate_secret():
    value = {'tx_hash': HEX, 'private_key': ZERO}
    clean, hit = guard.Detector().scan(value)
    assert hit
    assert clean == {'tx_hash': HEX, 'private_key': guard.REDACTED}


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('arguments', [
    {'url': 'https://basescan.org/tx/' + HEX},
    {'open': [{'ref_id': 'https://sepolia.basescan.org/tx/' + HEX}]},
    {'url': 'https://etherscan.io/tx/' + HEX},
    {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_getTransactionReceipt', 'params': [HEX]},
    {'jsonrpc': '2.0', 'method': 'eth_getTransactionByHash', 'params': [HEX]},
    {'body': json.dumps({'jsonrpc': '2.0', 'id': 1,
                         'method': 'eth_getTransactionReceipt', 'params': [HEX]})},
])
def test_structured_public_transaction_references_pass_pretool(client, arguments):
    result = invoke(client, 'PreToolUse', tool_input=arguments)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ''


@pytest.mark.parametrize('arguments', [
    {'url': 'https://basescan.org.attacker.invalid/tx/' + HEX},
    {'url': 'https://attacker.invalid/tx/' + HEX},
    {'url': 'https://user@basescan.org/tx/' + HEX},
    {'url': 'https://basescan.org:8443/tx/' + HEX},
    {'url': 'http://basescan.org/tx/' + HEX},
    {'url': 'https://basescan.org/tx/' + HEX + '?secret=' + HEX},
    {'url': 'https://basescan.org/tx/' + HEX + '#' + HEX},
    {'url': 'https://basescan.org/address/' + HEX},
    {'method': 'eth_getTransactionReceipt', 'params': [HEX]},
    {'jsonrpc': '2.0', 'method': 'eth_sendRawTransaction', 'params': [HEX]},
    {'jsonrpc': '2.0', 'method': 'eth_getTransactionReceipt', 'params': ['latest', HEX]},
    {'jsonrpc': '2.0', 'method': 'eth_getTransactionReceipt', 'params': [HEX, HEX]},
    {'jsonrpc': '2.0', 'method': 'eth_getTransactionReceipt', 'params': [HEX], 'private_key': HEX},
    {'method': 'eth_getTransactionReceipt', 'private_key': HEX},
    {'private_key': {'url': 'https://basescan.org/tx/' + HEX}},
    {'secret': {'jsonrpc': '2.0', 'method': 'eth_getTransactionReceipt', 'params': [HEX]}},
])
def test_unknown_or_secret_context_does_not_inherit_public_reference_exemptions(arguments):
    result = invoke('codex', 'PreToolUse', tool_input=arguments)
    assert result.returncode == 0
    assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert HEX not in result.stdout + result.stderr


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('command', [
    './scripts/reveal-secret.sh --address synthetic',
    'curl http://127.0.0.1:9080/api/v1/agent/wallet/reveal-secret/synthetic',
    'curl http://127.0.0.1:9080/api/v1/agent/wallet/synthetic/reveal',
    'cat agent-data/master.key', 'sqlite3 agent-data/agent.db "select 1"',
    'cat agent-data/agent.db-wal', 'python scripts/custody-admin.py export',
    'security find-generic-password -s mangrove -w',
    'echo ' + HEX,
])
def test_known_export_file_and_secret_inputs_are_denied_before_execution(client, command):
    result = invoke(client, 'PreToolUse', tool_name='Bash', tool_input={'command': command})
    assert result.returncode == 0
    decision = json.loads(result.stdout)['hookSpecificOutput']
    assert decision['permissionDecision'] == 'deny'
    assert decision['hookEventName'] == 'PreToolUse'
    assert command not in result.stdout + result.stderr


@pytest.mark.parametrize('tool_input', [
    {'file_path': '/repo/agent-data/master.key'},
    {'path': r'C:\agent\agent-data\agent.db'},
    {'url': 'http://127.0.0.1:9080/api/v1/agent/wallet/synthetic/reveal'},
])
def test_native_file_and_http_tools_are_denied(tool_input):
    result = invoke('codex', 'PreToolUse', tool_name='mcp__fs__read', tool_input=tool_input)
    assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'


@pytest.mark.parametrize('secret', [ZERO, HEX, PHRASE, XRPL])
@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_prompt_detection_returns_native_block_without_secret(client, secret):
    result = invoke(client, 'UserPromptSubmit', prompt=secret)
    assert result.returncode == 2
    assert secret not in result.stdout + result.stderr


def test_claude_failure_stops_without_inventing_error_replacement():
    result = invoke('claude', 'PostToolUseFailure', error='Exit code 1\n' + HEX)
    output = json.loads(result.stdout)
    assert output['continue'] is False
    assert output['hookSpecificOutput']['hookEventName'] == 'PostToolUseFailure'
    assert 'updatedToolOutput' not in result.stdout
    assert HEX not in result.stdout + result.stderr


@pytest.mark.parametrize('raw', [b'{"prompt":', b'[]', b'\xff', b'x' * (guard.MAX_BYTES + 1)],
                         ids=['truncated-json', 'wrong-shape', 'invalid-utf8', 'oversize'])
def test_malformed_and_oversize_input_fail_without_printing_payload(raw):
    result = subprocess.run([sys.executable, str(SCRIPT), '--client', 'codex'],
                            input=raw, capture_output=True)
    assert result.returncode == 2
    assert not result.stdout
    assert b'Traceback' not in result.stderr
    assert b'could not complete' in result.stderr


def test_traversal_limit_is_safe():
    output = HEX
    for _ in range(guard.MAX_DEPTH + 2):
        output = {'nested': output}
    result = invoke('codex', 'PostToolUse', tool_response=output)
    assert result.returncode == 2
    assert HEX not in result.stdout + result.stderr


@pytest.mark.parametrize('mode', ['user', 'pre', 'tool', 'failure', None])
@pytest.mark.parametrize('failure', ['malformed', 'wordlist', 'depth'])
def test_claude_inspection_failure_stops_continuation(mode, failure, monkeypatch, capsys, tmp_path):
    payload = {'hook_event_name': 'PostToolUse', 'tool_response': HEX}
    if failure == 'depth':
        for _ in range(guard.MAX_DEPTH + 2):
            payload = {'nested': payload}
        payload = {'hook_event_name': 'PostToolUse', 'tool_response': payload,
                   'prompt': payload, 'tool_input': payload, 'error': payload}
    raw = b'{"sensitive":' if failure == 'malformed' else json.dumps(payload).encode()
    if failure == 'wordlist':
        monkeypatch.setattr(guard, 'ROOT', tmp_path)
    argv = ['guard', '--client', 'claude'] + (['--mode', mode] if mode else [])
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw)))
    assert guard.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['continue'] is False
    assert 'could not complete' in captured.out
    assert HEX not in captured.out + captured.err


@pytest.mark.parametrize('relative', ['.claude/settings.json', '.claude-plugin/hooks.json',
                                    '.codex/session-hooks.toml', '.codex/plugin-hooks.json'])
def test_native_configs_register_all_tools_and_adapter_assets_work_outside_repo(relative, tmp_path):
    text = (ROOT / relative).read_text()
    config = (tomllib.loads(text) if relative.endswith('.toml') else json.loads(text))['hooks']
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(ROOT), CLAUDE_PLUGIN_ROOT=str(ROOT), PLUGIN_ROOT=str(ROOT))
    for event in ('UserPromptSubmit', 'PreToolUse', 'PostToolUse'):
        assert any(not entry.get('matcher') for entry in config[event])
        command = config[event][0]['hooks'][0]['command']
        if relative == '.codex/session-hooks.toml':
            # Native project hooks resolve from the active git root. A subdir
            # exercises the actual shipped command without relying on cwd=root.
            cwd = ROOT / 'server'
        else:
            cwd = tmp_path
        result = subprocess.run(['/bin/sh', '-c', command], cwd=cwd, env=env,
                                input=json.dumps({'hook_event_name': event, 'prompt': 'Show status',
                                                  'tool_input': {}, 'tool_response': {'ok': True}}),
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    if relative.startswith('.claude'):
        assert 'PostToolUseFailure' in config


def test_codex_plugin_uses_native_adapter_instead_of_claude_replacement_contract():
    manifest = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())
    original = json.loads((ROOT / '.claude-plugin/plugin.json').read_text())
    assert manifest['name'] == original['name']
    assert 'userConfig' not in manifest and 'mcpServers' not in manifest
    assert 'user_config' not in json.dumps(manifest)
    assert 'client_session.py' in manifest['description']
    assert manifest['author']['name']
    assert set(manifest['interface']) >= {'displayName', 'shortDescription', 'longDescription',
                                          'developerName', 'category', 'capabilities'}
    assert len(manifest['interface']['shortDescription']) <= 30
    assert all((ROOT / path).is_dir() for path in manifest['skills'])
    hooks = json.loads((ROOT / manifest['hooks']).read_text())['hooks']
    command = hooks['PostToolUse'][0]['hooks'][0]['command']
    assert '--client codex' in command
    assert 'PLUGIN_ROOT' in command
    startup = hooks['SessionStart'][0]['hooks'][0]['command']
    assert 'agent_context.py' in startup and '--stdout' in startup
    assert 'plugin-bootstrap.sh' not in startup
    assert not (ROOT / '.codex/hooks.json').exists()
    assert 'hooks' not in tomllib.loads((ROOT / '.codex/config.toml.example').read_text())
