"""Claude Code hook adapter over the agent's wallet-secret detector.

Detection is ``server/src/shared/secret_guard.py``, the detector the MCP
boundary uses, loaded from this installation by file path:

- ``UserPromptSubmit``: ``find_in_text`` on the typed prompt (strict).
- ``PreToolUse``: ``find_in_arguments`` on the tool input, plus a deny list of
  known export commands, reveal routes and legacy secret files.
- ``PostToolUse`` and ``PostToolUseFailure``: ``find_in_result`` on the tool
  output or error.

The hook supplements the custody boundary: a host can record tool output before
a hook runs. Feedback never includes the inspected payload, a matched value, a
path or exception text.
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
REDACTED = '[wallet-secret content withheld]'
REASON = ('Wallet-secret protection blocked this content or operation. '
          'Use the independently controlled custody administrator; never paste secrets into chat.')
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


def redact(value):
    """Same container shape with every string withheld."""
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return REDACTED
    return value


def response(event: str, payload: dict) -> tuple[dict | None, int]:
    guard = load_detector()
    if event == 'UserPromptSubmit':
        prompt = payload.get('prompt', payload)
        text = prompt if isinstance(prompt, str) else json.dumps(prompt)
        return ({'decision': 'block', 'reason': REASON}, 2) if guard.find_in_text(text) else (None, 0)
    if event == 'PreToolUse':
        arguments = payload.get('tool_input', {})
        if (guard.find_in_arguments(arguments) or known_secret_access(arguments)
                or known_secret_access(payload.get('tool_name', ''))):
            return {'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'deny',
                                           'permissionDecisionReason': REASON}}, 0
        return None, 0
    if event == 'PostToolUseFailure':
        if not guard.find_in_result(payload.get('error', '')):
            return None, 0
        # Claude documents no error replacement field for this event. Stop the
        # continuation without claiming the original error was removed.
        return {'continue': False, 'stopReason': REASON,
                'hookSpecificOutput': {'hookEventName': event, 'additionalContext': REASON}}, 0
    if event != 'PostToolUse':
        raise ValueError('Unsupported hook event')
    output = payload.get('tool_response', payload)
    if not guard.find_in_result(output):
        return None, 0
    return {'decision': 'block', 'reason': REASON, 'hookSpecificOutput': {
        'hookEventName': event, 'updatedToolOutput': redact(output), 'additionalContext': REASON}}, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=sorted(MODES))
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Detector input limit')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('Invalid hook payload')
        output, status = response(MODES.get(args.mode) or payload.get('hook_event_name'), payload)
    except (ValueError, TypeError, OSError, UnicodeError, RecursionError, ImportError):
        # Parser exceptions can contain payload text, so none is printed. A
        # global stop halts continuation when the content cannot be inspected.
        print(json.dumps({'continue': False, 'stopReason': REASON + ' The protection check could not complete.'}))
        return 0
    if output is not None:
        print(json.dumps(output, ensure_ascii=True))
    if status == 2:
        print(REASON, file=sys.stderr)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
