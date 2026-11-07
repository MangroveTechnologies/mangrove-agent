"""Claude Code and Codex hook adapters over the agent's wallet-secret detector.

Detection is ``server/src/shared/secret_guard.py``, the detector the MCP
boundary uses, loaded from this installation by file path. Hook content gets
its strict ``scan_hook_content`` rules:

- ``UserPromptSubmit`` and ``PreToolUse``: any secret shape is refused; a 64-hex
  value passes only as an exact explorer transaction link or the hash of an
  exact JSON-RPC transaction read sent to an allowlisted RPC host. ``PreToolUse`` also denies known export
  commands, reveal routes and legacy secret files.
- ``PostToolUse`` and ``PostToolUseFailure``: the same, plus lines holding
  only a hash label and one hash; MCP tool results (``mcp__`` tools) also
  allow whole-value public-hash fields.

The hooks supplement the agent's server-side secret guard: a host can record
tool output before a hook runs. Feedback never includes the inspected payload, a matched
value, a path or exception text.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DETECTOR = ROOT / 'server/src/shared/secret_guard.py'
MAX_BYTES = 4 * 1024 * 1024
REASON = ('Wallet-secret protection blocked this content or operation. '
          'To import a wallet, run ./scripts/stash-secret.sh in your own terminal; never paste secrets into chat.')
KNOWN_ACCESS = re.compile(
    r'reveal-secret\.(?:sh|py)|custody[-_]admin(?:\.py)?|'
    r'/wallet/(?:reveal-secret|[^\s/"\']+/reveal)(?:[/\s?"\']|$)|'
    r'agent-data[/\\]+(?:master\.key|agent\.db(?:-wal|-shm)?)(?![\w.-])|'
    r'\b(?:reveal_wallet_secret|_load_secret)\s*\(|'
    r'\bsecurity\s+find-generic-password\b', re.IGNORECASE)
MODES = {'user': 'UserPromptSubmit', 'pre': 'PreToolUse', 'tool': 'PostToolUse',
         'failure': 'PostToolUseFailure'}


def load_detector():
    spec = importlib.util.spec_from_file_location('mangrove_secret_guard', DETECTOR)
    if spec is None or spec.loader is None:
        raise OSError('Detector unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.bip39_words()
    return module


def known_secret_access(value) -> bool:
    """Known export commands, reveal routes and legacy secret files; not a shell sandbox."""
    if isinstance(value, dict):
        return any(known_secret_access(k) or known_secret_access(v) for k, v in value.items())
    if isinstance(value, list):
        return any(known_secret_access(v) for v in value)
    return isinstance(value, str) and bool(KNOWN_ACCESS.search(value.replace('\\/', '/')))


def response(client: str, event: str, payload: dict) -> tuple[dict | None, int]:
    guard = load_detector()
    if event == 'UserPromptSubmit':
        _, hit = guard.scan_hook_content(payload.get('prompt', payload), output=False)
        return ({'decision': 'block', 'reason': REASON}, 2) if hit else (None, 0)
    if event == 'PreToolUse':
        arguments = payload.get('tool_input', {})
        _, hit = guard.scan_hook_content(arguments, output=False)
        if hit or known_secret_access(arguments) or known_secret_access(payload.get('tool_name', '')):
            return {'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'deny',
                                           'permissionDecisionReason': REASON}}, 0
        return None, 0
    if event == 'PostToolUseFailure':
        _, hit = guard.scan_hook_content(payload.get('error', ''), output=True)
        # Claude documents no error replacement field for this event. Stop the
        # continuation without claiming the original error was removed.
        return ({'continue': False, 'stopReason': REASON,
                 'hookSpecificOutput': {'hookEventName': event, 'additionalContext': REASON}}, 0) if hit else (None, 0)
    if event != 'PostToolUse':
        raise ValueError('Unsupported hook event')
    structured = str(payload.get('tool_name', '')).startswith('mcp__')
    clean, hit = guard.scan_hook_content(payload.get('tool_response', payload), output=True,
                                         structured=structured)
    if not hit:
        return None, 0
    if client == 'codex':
        # Codex does not support updatedMCPToolOutput or suppressOutput. A native
        # block replaces model feedback and rejects nested code-mode promises.
        return {'decision': 'block', 'reason': REASON}, 0
    return {'decision': 'block', 'reason': REASON, 'hookSpecificOutput': {
        'hookEventName': event, 'updatedToolOutput': clean, 'additionalContext': REASON}}, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', choices=['claude', 'codex'], default='claude')
    parser.add_argument('--mode', choices=sorted(MODES))
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Detector input limit')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('Invalid hook payload')
        output, status = response(args.client, MODES.get(args.mode) or payload.get('hook_event_name'), payload)
    except (ValueError, TypeError, OSError, UnicodeError, RecursionError, ImportError):
        # Parser exceptions can contain payload text, so none is printed.
        if args.client == 'claude':
            # Exit 2 alone only adds Claude post-tool feedback; a global stop
            # halts continuation when the content cannot be inspected.
            print(json.dumps({'continue': False, 'stopReason': REASON + ' The protection check could not complete.'}))
            return 0
        print(REASON + ' The protection check could not complete.', file=sys.stderr)
        return 2
    if output is not None:
        print(json.dumps(output, ensure_ascii=True))
    if status == 2:
        print(REASON, file=sys.stderr)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
