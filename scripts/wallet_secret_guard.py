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

Tool output and errors larger than ``STRUCTURED_BYTES``, or too deep or large
to walk, get the linear ``hook_raw_text_hit`` scan instead. Input larger than
``MAX_BYTES`` is not scanned.

When the check cannot complete (unreadable or oversize input, a missing
detector, a traversal limit on input, or any other error), each event gets a
safe decision instead of an allow: ``PreToolUse`` denies the tool call,
``UserPromptSubmit`` blocks the prompt, ``PostToolUse`` withholds the output
(Claude replaces it, Codex blocks its feedback) and ``PostToolUseFailure`` adds
a block reason. Output events never stop the session for a failed check. When
the event is unknown, the adapter exits 2, which blocks an input event.

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
MAX_BYTES = 8 * 1024 * 1024
STRUCTURED_BYTES = 1024 * 1024
REDACTED = '[wallet-secret content withheld]'
REASON = ('Wallet-secret protection blocked this content or operation. '
          'To import a wallet, run ./scripts/stash-secret.sh in your own terminal; never paste secrets into chat.')
INCOMPLETE = REASON + ' The protection check could not complete.'
EVENT_HINT = re.compile(rb'"hook_event_name"\s*:\s*"([A-Za-z]{1,40})"')
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


def withheld_shape(value):
    """The output with every value except booleans and nulls replaced by the withheld marker."""
    if isinstance(value, dict):
        return {key: item if isinstance(item, bool) or item is None else REDACTED for key, item in value.items()}
    return REDACTED


def scan_output(guard, value, size: int, structured: bool):
    """Scan tool output or an error; fall back to the linear scan when it is too large or deep to walk."""
    if size <= STRUCTURED_BYTES:
        try:
            return guard.scan_hook_content(value, output=True, structured=structured)
        except guard.HookLimitError:
            pass
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return withheld_shape(value), guard.hook_raw_text_hit(text)


def withheld_output(client: str, event: str, clean, reason: str) -> dict:
    if client == 'codex':
        # Codex does not support updatedMCPToolOutput or suppressOutput. A native
        # block replaces model feedback and rejects nested code-mode promises.
        return {'decision': 'block', 'reason': reason}
    return {'decision': 'block', 'reason': reason, 'hookSpecificOutput': {
        'hookEventName': event, 'updatedToolOutput': clean, 'additionalContext': reason}}


def failure_response(client: str, event, payload) -> tuple[dict | None, int]:
    """The safe decision for an event whose check could not complete."""
    if event == 'PreToolUse':
        return {'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'deny',
                                       'permissionDecisionReason': INCOMPLETE}}, 0
    if event == 'UserPromptSubmit':
        return {'decision': 'block', 'reason': INCOMPLETE}, 2
    if event == 'PostToolUse':
        value = payload.get('tool_response', payload) if isinstance(payload, dict) else None
        return withheld_output(client, event, withheld_shape(value), INCOMPLETE), 0
    if event == 'PostToolUseFailure':
        if client == 'codex':
            return {'decision': 'block', 'reason': INCOMPLETE}, 0
        return {'decision': 'block', 'reason': INCOMPLETE,
                'hookSpecificOutput': {'hookEventName': event, 'additionalContext': INCOMPLETE}}, 0
    return None, 2


def response(client: str, event: str, payload: dict, size: int = 0) -> tuple[dict | None, int]:
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
        _, hit = scan_output(guard, payload.get('error', ''), size, structured=False)
        # Claude documents no error replacement field for this event. Stop the
        # continuation without claiming the original error was removed.
        return ({'continue': False, 'stopReason': REASON,
                 'hookSpecificOutput': {'hookEventName': event, 'additionalContext': REASON}}, 0) if hit else (None, 0)
    if event != 'PostToolUse':
        raise ValueError('Unsupported hook event')
    structured = str(payload.get('tool_name', '')).startswith('mcp__')
    clean, hit = scan_output(guard, payload.get('tool_response', payload), size, structured)
    if not hit:
        return None, 0
    return withheld_output(client, event, clean, REASON), 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', choices=['claude', 'codex'], default='claude')
    parser.add_argument('--mode', choices=sorted(MODES))
    args = parser.parse_args()
    event, payload = MODES.get(args.mode), None
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if event is None:
            hint = EVENT_HINT.search(raw, 0, 65536)
            event = hint.group(1).decode() if hint else None
        if len(raw) > MAX_BYTES:
            raise ValueError('Detector input limit')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('Invalid hook payload')
        event = MODES.get(args.mode) or payload.get('hook_event_name')
        output, status = response(args.client, event, payload, len(raw))
    except Exception:  # noqa: BLE001 - every failure gets the event's safe decision
        # Exception text can contain payload text, so none is printed.
        output, status = failure_response(args.client, event, payload)
        if output is not None:
            print(json.dumps(output, ensure_ascii=True))
        if status == 2:
            print(INCOMPLETE, file=sys.stderr)
        return status
    if output is not None:
        print(json.dumps(output, ensure_ascii=True))
    if status == 2:
        print(REASON, file=sys.stderr)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
