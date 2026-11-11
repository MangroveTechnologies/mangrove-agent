"""Offline invalid/shape-only fixtures exercise the native hook adapters over secret_guard."""
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
detector = guard.load_detector()
# Zero is outside the EVM private key range. These fixtures control no account.
ZERO = '0' * 64
HEX = '0x' + ZERO
# Distinct, unordered wordlist words with an invalid checksum, built from wordlist positions so
# this file holds no phrase text.
PHRASE = ' '.join(sorted(detector.bip39_words())[i] for i in (2047,2,1407,680,1875,1111,213,1993,1230,1124,359,553))
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
    {'address': '0x' + '0' * 40}, {'message': 'Show my wallet balance and trading status.'},
])
def test_public_hash_fields_pass_structured_mcp_results(value):
    for client in ('claude', 'codex'):
        result = invoke(client, 'PostToolUse', tool_name='mcp__mangrove-agent__get_order', tool_response=value)
        assert result.returncode == 0
        assert result.stdout == result.stderr == ''


@pytest.mark.parametrize('value', [
    {'stdout': 'Transaction hash: ' + HEX, 'stderr': ''},
    {'stdout': 'done\n  tx_hash:\t' + HEX + '\r\nnext line'},
    {'stdout': 'Show my wallet balance and trading status.'},
])
def test_labelled_hash_lines_and_prose_pass_shell_output(value):
    result = invoke('claude', 'PostToolUse', tool_name='Bash', tool_response=value)
    assert result.stdout == result.stderr == ''


@pytest.mark.parametrize('value', [
    {'tx_hash': HEX},
    {'stdout': json.dumps({'tx_hash': HEX})},
    {'sha256': ZERO, 'stdout': ''},
])
def test_public_hash_field_names_do_not_exempt_shell_output(value):
    result = invoke('claude', 'PostToolUse', tool_name='Bash', tool_response=value)
    assert json.loads(result.stdout)['decision'] == 'block'
    assert HEX not in result.stdout + result.stderr


@pytest.mark.parametrize('text', [
    'tx_hash:\n' + HEX,
    'tx_hash:\n\n' + HEX,
    'digest =\n   ' + ZERO,
])
def test_hash_line_exemption_does_not_span_lines(text):
    result = invoke('claude', 'PostToolUse', tool_name='Bash', tool_response={'stdout': text})
    assert json.loads(result.stdout)['decision'] == 'block'


def test_public_hash_does_not_hide_separate_secret():
    value = {'tx_hash': HEX, 'private_key': ZERO}
    clean, hit = detector.scan_hook_content(value, output=True, structured=True)
    assert hit
    assert clean == {'tx_hash': HEX, 'private_key': detector.HOOK_REDACTED}


@pytest.mark.parametrize('text', [
    '{"tx_hash": "' + HEX + '", "tx_hash": "0x01"}',
    '{"a": {"tx_hash": "' + HEX + '", "tx_hash": "none"}}',
])
@pytest.mark.parametrize('structured', [True, False])
def test_duplicate_json_keys_cannot_hide_a_value(text, structured):
    _, hit = detector.scan_hook_content({'text': text}, output=True, structured=structured)
    assert hit


def test_json_text_is_also_scanned_raw_with_only_exempted_values_removed(monkeypatch):
    text = json.dumps({'tx_hash': HEX, 'note': 'key ' + ZERO.replace('0', '1')})
    # Simulate a parser that drops a member: the raw scan must still find it.
    monkeypatch.setattr(detector, '_decode_json', lambda raw: {'tx_hash': HEX} if raw == text else None)
    _, hit = detector.scan_hook_content({'text': text}, output=True, structured=True)
    assert hit
    monkeypatch.undo()
    # The exempted value itself is removed before the raw scan.
    _, hit = detector.scan_hook_content({'text': json.dumps({'tx_hash': HEX})}, output=True, structured=True)
    assert not hit


@pytest.mark.parametrize('number', [int('1' * 64), int('9' * 64)])
def test_numeric_leaves_are_scanned(number):
    _, hit = detector.scan_hook_content({'value': number}, output=True, structured=True)
    assert hit
    _, hit = detector.scan_hook_content({'value': 1_000_000, 'ok': True, 'none': None}, output=False)
    assert not hit


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('arguments', [
    {'url': 'https://basescan.org/tx/' + HEX},
    {'open': [{'ref_id': 'https://sepolia.basescan.org/tx/' + HEX}]},
    {'url': 'https://etherscan.io/tx/' + HEX},
    {'url': 'https://mainnet.base.org', 'json': {'jsonrpc': '2.0', 'id': 1,
                                                'method': 'eth_getTransactionReceipt', 'params': [HEX]}},
    {'endpoint': 'https://sepolia.base.org/', 'json': {'jsonrpc': '2.0', 'method': 'eth_getTransactionByHash',
                                                       'params': [HEX]}},
    {'url': 'https://mainnet.base.org', 'body': json.dumps({'jsonrpc': '2.0', 'id': 1,
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


def test_traversal_limit_on_output_falls_back_to_the_linear_scan():
    output = HEX
    for _ in range(detector.HOOK_MAX_DEPTH + 2):
        output = {'nested': output}
    result = invoke('codex', 'PostToolUse', tool_response=output)
    assert result.returncode == 0
    assert json.loads(result.stdout)['decision'] == 'block'
    assert HEX not in result.stdout + result.stderr


def _run_main(monkeypatch, capsys, client, mode, raw):
    argv = ['guard', '--client', client] + (['--mode', mode] if mode else [])
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw)))
    status = guard.main()
    captured = capsys.readouterr()
    assert HEX not in captured.out + captured.err
    assert 'payload text' not in captured.out + captured.err
    assert 'Traceback' not in captured.err
    return status, (json.loads(captured.out) if captured.out else None), captured.err


def _failing_payload(failure):
    payload = {'hook_event_name': 'PostToolUse', 'tool_response': {'stdout': HEX, 'interrupted': False}}
    if failure == 'depth':
        nested = HEX
        for _ in range(detector.HOOK_MAX_DEPTH + 2):
            nested = {'nested': nested}
        payload.update(prompt=nested, tool_input=nested)
    return b'{"sensitive":' if failure == 'malformed' else json.dumps(payload).encode()


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('failure', ['malformed', 'detector', 'depth', 'crash'])
def test_failed_pretool_check_denies_the_tool_call(client, failure, monkeypatch, capsys, tmp_path):
    if failure == 'detector':
        monkeypatch.setattr(guard, 'DETECTOR', tmp_path / 'missing.py')
    if failure == 'crash':
        monkeypatch.setattr(guard, 'response', lambda *a, **k: (_ for _ in ()).throw(KeyError('payload text')))
    status, output, _ = _run_main(monkeypatch, capsys, client, 'pre', _failing_payload(failure))
    assert status == 0
    decision = output['hookSpecificOutput']
    assert decision['hookEventName'] == 'PreToolUse'
    assert decision['permissionDecision'] == 'deny'
    assert 'could not complete' in decision['permissionDecisionReason']


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('failure', ['malformed', 'detector', 'depth', 'crash'])
def test_failed_prompt_check_blocks_the_prompt(client, failure, monkeypatch, capsys, tmp_path):
    if failure == 'detector':
        monkeypatch.setattr(guard, 'DETECTOR', tmp_path / 'missing.py')
    if failure == 'crash':
        monkeypatch.setattr(guard, 'response', lambda *a, **k: (_ for _ in ()).throw(KeyError('payload text')))
    status, output, err = _run_main(monkeypatch, capsys, client, 'user', _failing_payload(failure))
    assert status == 2
    assert output['decision'] == 'block'
    assert 'could not complete' in err


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('mode', ['tool', None])
@pytest.mark.parametrize('failure', ['malformed', 'detector', 'crash'])
def test_failed_output_check_withholds_without_stopping_the_session(client, mode, failure, monkeypatch,
                                                                    capsys, tmp_path):
    if failure == 'detector':
        monkeypatch.setattr(guard, 'DETECTOR', tmp_path / 'missing.py')
    if failure == 'crash':
        monkeypatch.setattr(guard, 'response', lambda *a, **k: (_ for _ in ()).throw(KeyError('payload text')))
    raw = _failing_payload(failure)
    if failure == 'malformed':
        raw = b'{"hook_event_name": "PostToolUse", "tool_response": {"stdout": "' + HEX.encode()
    status, output, _ = _run_main(monkeypatch, capsys, client, mode, raw)
    assert status == 0
    assert 'continue' not in output
    assert output['decision'] == 'block'
    assert 'could not complete' in output['reason']
    if client == 'claude':
        assert output['hookSpecificOutput']['hookEventName'] == 'PostToolUse'
        assert 'updatedToolOutput' in output['hookSpecificOutput']


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_failed_error_check_does_not_stop_the_session(client, monkeypatch, capsys):
    monkeypatch.setattr(guard, 'response', lambda *a, **k: (_ for _ in ()).throw(KeyError('payload text')))
    status, output, _ = _run_main(monkeypatch, capsys, client, 'failure',
                                  json.dumps({'hook_event_name': 'PostToolUseFailure', 'error': HEX}).encode())
    assert status == 0
    assert 'continue' not in output
    assert output['decision'] == 'block'


def test_unknown_event_with_unreadable_input_exits_2(monkeypatch, capsys):
    status, output, err = _run_main(monkeypatch, capsys, 'codex', None, b'\xff\xfe')
    assert status == 2 and output is None
    assert 'could not complete' in err


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


def test_adapter_uses_the_shared_detector_module():
    assert Path(detector.__file__).resolve() == (ROOT / 'server/src/shared/secret_guard.py').resolve()
    assert not hasattr(guard, 'Detector') and not hasattr(guard, 'HEX')


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('tool_input', [
    {'command': 'cast receipt ' + HEX},
    {'command': 'tx ' + HEX},
    {'command': 'transaction hash ' + ZERO},
    {'tx_hash': HEX},
    {'content': 'tx_hash: ' + HEX},
    {'query': 'order ' + HEX},
])
def test_pretool_label_words_and_hash_fields_do_not_exempt_a_key(client, tool_input):
    result = invoke(client, 'PreToolUse', tool_name='Bash', tool_input=tool_input)
    assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert HEX not in result.stdout + result.stderr


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('output', [
    {'stdout': 'tx ' + HEX, 'stderr': ''},
    {'stdout': '', 'stderr': 'order ' + ZERO},
    {'isError': True, 'content': [{'type': 'text', 'text': 'receipt ' + HEX}]},
    'r:       ' + HEX,
])
def test_post_tool_free_text_uses_the_strict_scan(client, output):
    result = invoke(client, 'PostToolUse', tool_name='Bash', tool_response=output)
    assert json.loads(result.stdout)['decision'] == 'block'
    assert HEX not in result.stdout + result.stderr


def test_failure_free_text_uses_the_strict_scan():
    result = invoke('claude', 'PostToolUseFailure', error='Rejected: transaction ' + HEX)
    assert json.loads(result.stdout)['continue'] is False
    assert HEX not in result.stdout + result.stderr


def test_adapter_runs_without_eth_account(tmp_path):
    """The hook interpreter may lack eth_account; the repository wordlist is used."""
    blocker = tmp_path / 'eth_account'
    blocker.mkdir()
    (blocker / '__init__.py').write_text('raise ModuleNotFoundError("eth_account")\n')
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    for prompt, status in ((PHRASE, 2), ('Show my wallet balance', 0)):
        result = subprocess.run([sys.executable, str(SCRIPT), '--client', 'claude'],
                                input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'prompt': prompt}),
                                capture_output=True, text=True, env=env)
        assert result.returncode == status, result.stdout + result.stderr


@pytest.mark.parametrize('arguments', [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_getTransactionReceipt', 'params': [HEX]},
    {'body': json.dumps({'jsonrpc': '2.0', 'method': 'eth_getTransactionReceipt', 'params': [HEX]})},
    {'url': 'https://attacker.invalid', 'json': {'jsonrpc': '2.0', 'method': 'eth_getTransactionReceipt',
                                                 'params': [HEX]}},
    {'url': 'https://mainnet.base.org.attacker.invalid', 'json': {'jsonrpc': '2.0',
                                                                  'method': 'eth_getTransactionReceipt',
                                                                  'params': [HEX]}},
    {'url': 'https://mainnet.base.org/?k=' + HEX, 'json': {'jsonrpc': '2.0', 'method': 'eth_getTransactionReceipt',
                                                          'params': [HEX]}},
    {'url': 'http://mainnet.base.org', 'json': {'jsonrpc': '2.0', 'method': 'eth_getTransactionReceipt',
                                                'params': [HEX]}},
])
def test_json_rpc_exemption_requires_an_allowlisted_rpc_host(arguments):
    result = invoke('claude', 'PreToolUse', tool_name='WebFetch', tool_input=arguments)
    assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'


# --- Phrase detection in hook content ----------------------------------------

def _generated_phrase() -> list[str]:
    """A checksummed phrase with distinct words, generated for the test; it guards no funds."""
    import secrets

    words = sorted(detector.bip39_words())
    while True:
        phrase = [secrets.choice(words) for _ in range(11)]
        for last in words:
            candidate = phrase + [last]
            if len(set(candidate)) == 12 and detector.bip39_checksum_valid(candidate):
                return candidate


def _tracked_text_files():
    names = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, check=True).stdout
    for name in filter(None, names.decode().split('\0')):
        try:
            yield name, (ROOT / name).read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue


@pytest.mark.skipif(not (ROOT / '.git').exists(), reason='needs a git checkout')
def test_no_tracked_repository_file_trips_the_hook_phrase_scan():
    """Ordinary text never reads as a phrase. 64-hex values, integer keys and seeds are
    blanked first: the hook refuses those shapes by design (hashes in docs and fixtures)."""
    hits = []
    for name, text in _tracked_text_files():
        plain = detector._XRPL_SEED_RE.sub(' ', detector._DECIMAL_KEY_RE.sub(' ', detector._HEX_KEY_RE.sub(' ', text)))
        for output in (False, True):
            if detector.scan_hook_content(plain, output=output)[1]:
                hits.append((name, output))
    assert hits == []


@pytest.mark.parametrize('variant', ['valid', 'one_wrong_word', 'underscores'])
@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_phrases_are_caught_in_prompts_and_output(client, variant):
    words = _generated_phrase()
    if variant == 'one_wrong_word':
        words[4] = next(w for w in sorted(detector.bip39_words())
                        if w not in words and not detector.bip39_checksum_valid(words[:4] + [w] + words[5:]))
    text = ('_' if variant == 'underscores' else ' ').join(words)
    prompt = invoke(client, 'UserPromptSubmit', prompt='restore ' + text)
    assert prompt.returncode == 2 and text not in prompt.stdout + prompt.stderr
    output = invoke(client, 'PostToolUse', tool_name='Bash', tool_response={'stdout': text, 'stderr': ''})
    assert json.loads(output.stdout)['decision'] == 'block'
    assert text not in output.stdout + output.stderr


def test_repeated_wordlist_run_counts_only_under_a_key_named_field():
    run = ' '.join(['abandon'] * 12)
    assert not detector.scan_hook_content({'stdout': run}, output=True)[1]
    assert detector.scan_hook_content({'private_key': run}, output=True)[1]


def test_phrase_as_a_list_of_words_is_caught():
    words = _generated_phrase()
    clean, hit = detector.scan_hook_content({'words': words}, output=False)
    assert hit and words[0] not in json.dumps(clean)


# --- Large output ---------------------------------------------------------------

def _bars(count):
    return [{'timestamp': 1700000000 + i, 'open': 1.0 + i, 'high': 2.0, 'low': 0.5, 'close': 1.5,
             'volume': 1234.5, 'symbol': 'ETH'} for i in range(count)]


def _timed_output_hook(client, shape, count, secret):
    import time

    rows = _bars(count)
    if secret:
        rows[count // 2]['symbol'] = HEX
    text = json.dumps({'bars': rows})
    response = {'shell': {'stdout': text, 'stderr': '', 'interrupted': False},
                'mcp_text': {'content': [{'type': 'text', 'text': text}]},
                'mcp_structured': {'content': [{'type': 'text', 'text': text}], 'structuredContent': {'bars': rows}},
                }[shape]
    tool = 'Bash' if shape == 'shell' else 'mcp__mangrove-agent__get_ohlcv'
    started = time.perf_counter()
    result = invoke(client, 'PostToolUse', tool_name=tool, tool_response=response)
    return time.perf_counter() - started, result


@pytest.mark.parametrize('client', ['claude', 'codex'])
@pytest.mark.parametrize('shape', ['shell', 'mcp_text', 'mcp_structured'])
@pytest.mark.parametrize('secret', [False, True])
def test_multi_megabyte_json_output_is_scanned_quickly_without_halting(client, shape, secret):
    """About 2.7 MB of JSON output finishes well inside the hook timeout, in time linear in its size."""
    count = 11_000 if shape == 'mcp_structured' else 20_000
    half, _ = _timed_output_hook(client, shape, count // 2, secret)
    elapsed, result = _timed_output_hook(client, shape, count, secret)
    assert elapsed < 2.5
    assert elapsed < 3 * half + 0.3
    assert result.returncode == 0
    assert '"continue"' not in result.stdout
    if secret:
        assert json.loads(result.stdout)['decision'] == 'block'
        assert HEX not in result.stdout + result.stderr
    else:
        assert result.stdout == result.stderr == ''


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_large_plain_output_keeps_a_key_after_a_backslash_whole(client):
    """A plain-string output above the structured limit is scanned as it is, not only JSON-unescaped."""
    key = 'ab' + '3c' * 31
    line = 'saved C:\\wallets\\' + key + '\n'
    for output in (line, 'x' * (guard.STRUCTURED_BYTES + 10) + '\n' + line):
        result = invoke(client, 'PostToolUse', tool_name='Bash', tool_response=output)
        assert json.loads(result.stdout)['decision'] == 'block'
        assert key not in result.stdout + result.stderr


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_output_above_the_input_limit_is_withheld_without_halting(client):
    filler = 'x' * (guard.MAX_BYTES + 10)
    payload = json.dumps({'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                          'tool_response': {'stdout': filler, 'stderr': ''}})
    result = subprocess.run([sys.executable, str(SCRIPT), '--client', client],
                            input=payload, capture_output=True, text=True)
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output['decision'] == 'block' and 'continue' not in output


# --- RPC exemption scope ----------------------------------------------------------

ENVELOPE = {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_getTransactionReceipt', 'params': [HEX]}


@pytest.mark.parametrize('arguments', [
    {'url': 'https://mainnet.base.org', 'json': {'wrapper': ENVELOPE}},
    {'url': 'https://mainnet.base.org', 'json': {'a': {'b': {'c': ENVELOPE}}}},
    {'url': 'https://mainnet.base.org', 'json': [[ENVELOPE]]},
    {'url': 'https://mainnet.base.org', 'json': {'wrapper': json.dumps(ENVELOPE)}},
    {'url': 'https://mainnet.base.org', 'json': {'nested': {'url': 'https://example.invalid', 'body': ENVELOPE}}},
])
def test_rpc_exemption_is_not_inherited_by_nested_fields(arguments):
    result = invoke('codex', 'PreToolUse', tool_input=arguments)
    assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert HEX not in result.stdout + result.stderr


def test_rpc_batch_of_transaction_reads_passes():
    result = invoke('codex', 'PreToolUse', tool_input={'url': 'https://mainnet.base.org',
                                                       'json': [ENVELOPE, dict(ENVELOPE, id=2)]})
    assert result.returncode == 0 and result.stdout == ''
