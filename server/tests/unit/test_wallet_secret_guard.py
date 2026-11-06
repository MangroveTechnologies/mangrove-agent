"""The Claude hook adapter applies the shared secret_guard detector to hook events.

All values are synthetic shapes; none controls an account.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
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


def invoke(event, *, python=sys.executable, **kwargs):
    return subprocess.run([python, str(SCRIPT)],
                          input=json.dumps({'hook_event_name': event, **kwargs}),
                          capture_output=True, text=True)


def test_adapter_uses_the_shared_detector_module():
    detector = guard.load_detector()
    assert Path(detector.__file__).resolve() == (ROOT / 'server/src/shared/secret_guard.py').resolve()
    assert not hasattr(guard, 'Detector')


@pytest.mark.parametrize('secret', [ZERO, HEX, PHRASE, XRPL])
def test_prompt_detection_blocks_without_echo(secret):
    result = invoke('UserPromptSubmit', prompt=secret)
    assert result.returncode == 2
    assert json.loads(result.stdout)['decision'] == 'block'
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize('secret', [ZERO, HEX, PHRASE, XRPL])
@pytest.mark.parametrize('envelope', [
    lambda s: {'private_key': s},
    lambda s: {'content': [{'type': 'text', 'text': json.dumps({'private_key': s})}]},
    lambda s: {'structuredContent': {'payload': {'seed_phrase': s}}},
    lambda s: {'stdout': json.dumps({'privateKey': s}), 'stderr': '', 'interrupted': False},
])
def test_secret_in_key_named_result_field_is_replaced_with_same_shape(secret, envelope):
    original = envelope(secret)
    result = invoke('PostToolUse', tool_name='Bash', tool_response=original)
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output['decision'] == 'block'
    assert secret not in result.stdout + result.stderr
    clean = output['hookSpecificOutput']['updatedToolOutput']
    assert isinstance(clean, type(original)) and set(clean) == set(original)
    if 'interrupted' in original:
        assert clean['interrupted'] is False


@pytest.mark.parametrize('value', [
    {'tx_hash': HEX}, {'transactionHash': HEX}, {'sha256': ZERO},
    {'content': [{'type': 'text', 'text': json.dumps({'tx_hash': HEX})}]},
    {'stdout': 'Transaction hash: ' + HEX},
    {'stdout': 'r:       ' + HEX + '\ns:       ' + HEX},
    {'address': '0x' + '0' * 40}, {'message': 'Show my wallet balance and trading status.'},
])
def test_public_hashes_documents_and_prose_pass_post_tool(value):
    result = invoke('PostToolUse', tool_response=value)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ''


def test_reading_the_incident_report_passes():
    text = (ROOT / 'docs/incidents/2026-04-24-eip7702-drain.md').read_text()
    result = invoke('PostToolUse', tool_name='Read', tool_response={'type': 'text', 'file': {'content': text}})
    assert result.stdout == result.stderr == ''


@pytest.mark.parametrize('tool_input', [
    {'command': 'cast receipt ' + HEX},
    {'command': 'cast tx ' + HEX},
    {'tx_hash': HEX},
    {'query': 'What happened in transaction ' + HEX + '?'},
])
def test_hash_labelled_tool_input_passes(tool_input):
    result = invoke('PreToolUse', tool_name='Bash', tool_input=tool_input)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ''


@pytest.mark.parametrize('tool_input', [
    {'command': 'cast send --private-key ' + HEX},
    {'command': 'echo ' + HEX},
    {'query': 'import my wallet ' + ZERO},
    {'privateKey': HEX},
    {'description': ' '.join(['abandon'] * 11 + ['about'])},  # valid BIP-39 checksum
])
def test_secret_in_tool_input_is_denied(tool_input):
    result = invoke('PreToolUse', tool_name='Bash', tool_input=tool_input)
    assert result.returncode == 0
    decision = json.loads(result.stdout)['hookSpecificOutput']
    assert decision['permissionDecision'] == 'deny'
    assert HEX not in result.stdout + result.stderr


@pytest.mark.parametrize('command', [
    './scripts/reveal-secret.sh --address synthetic',
    'curl http://127.0.0.1:9080/api/v1/agent/wallet/reveal-secret/synthetic',
    'curl http://127.0.0.1:9080/api/v1/agent/wallet/synthetic/reveal',
    'cat agent-data/master.key', 'sqlite3 agent-data/agent.db "select 1"',
    'cat agent-data/agent.db-wal', 'python scripts/custody-admin.py export',
    'security find-generic-password -s mangrove -w',
])
def test_known_export_and_file_access_is_denied_before_execution(command):
    result = invoke('PreToolUse', tool_name='Bash', tool_input={'command': command})
    decision = json.loads(result.stdout)['hookSpecificOutput']
    assert decision['permissionDecision'] == 'deny'
    assert command not in result.stdout + result.stderr


def test_failure_with_key_field_stops_without_inventing_error_replacement():
    result = invoke('PostToolUseFailure', error='Rejected: {"private_key": "' + HEX + '"}')
    output = json.loads(result.stdout)
    assert output['continue'] is False
    assert output['hookSpecificOutput']['hookEventName'] == 'PostToolUseFailure'
    assert 'updatedToolOutput' not in result.stdout
    assert HEX not in result.stdout + result.stderr


def test_failure_quoting_a_transaction_hash_passes():
    result = invoke('PostToolUseFailure', error='Exit code 1\ntransaction ' + HEX + ' reverted')
    assert result.stdout == result.stderr == ''


@pytest.mark.parametrize('raw', [b'{"prompt":', b'[]', b'\xff', b'x' * (guard.MAX_BYTES + 1)],
                         ids=['truncated-json', 'wrong-shape', 'invalid-utf8', 'oversize'])
def test_malformed_and_oversize_input_stop_without_printing_payload(raw):
    result = subprocess.run([sys.executable, str(SCRIPT)], input=raw, capture_output=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)['continue'] is False
    assert b'Traceback' not in result.stderr
    assert b'could not complete' in result.stdout


@pytest.mark.parametrize('mode', ['user', 'pre', 'tool', 'failure', None])
@pytest.mark.parametrize('failure', ['malformed', 'detector', 'depth'])
def test_inspection_failure_stops_continuation(mode, failure, monkeypatch, capsys, tmp_path):
    raw = json.dumps({'hook_event_name': 'PostToolUse', 'tool_response': {'private_key': HEX}}).encode()
    if failure == 'malformed':
        raw = b'{"sensitive":'
    if failure == 'depth':
        nested = '{"nested":' * 200_000 + json.dumps(HEX) + '}' * 200_000
        raw = ('{"hook_event_name": "PostToolUse", "prompt": ' + nested + ', "tool_input": ' + nested +
               ', "tool_response": ' + nested + ', "error": ' + nested + '}').encode()
    if failure == 'detector':
        monkeypatch.setattr(guard, 'DETECTOR', tmp_path / 'missing.py')
    argv = ['guard'] + (['--mode', mode] if mode else [])
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw)))
    assert guard.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['continue'] is False
    assert 'could not complete' in captured.out
    assert HEX not in captured.out + captured.err


@pytest.mark.parametrize('relative', ['.claude/settings.json', '.claude-plugin/hooks.json'])
def test_claude_configs_register_every_event_and_work_outside_repo(relative, tmp_path):
    config = json.loads((ROOT / relative).read_text())['hooks']
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(ROOT), CLAUDE_PLUGIN_ROOT=str(ROOT))
    for event in ('UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure'):
        entry = next(e for e in config[event] if 'block-wallet-secrets.sh' in e['hooks'][0]['command'])
        assert not entry.get('matcher')
        result = subprocess.run(['/bin/sh', '-c', entry['hooks'][0]['command']], cwd=tmp_path, env=env,
                                input=json.dumps({'hook_event_name': event, 'prompt': 'Show status',
                                                  'tool_input': {}, 'tool_response': {'ok': True},
                                                  'error': 'failed'}),
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ''


def test_no_codex_hook_package_is_shipped():
    assert not (ROOT / '.codex-plugin').exists()
    assert not (ROOT / '.codex/session-hooks.toml').exists()
    assert not (ROOT / '.codex/plugin-hooks.json').exists()
