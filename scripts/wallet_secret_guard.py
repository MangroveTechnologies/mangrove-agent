"""Client-specific hook adapters over a shared, bounded wallet-secret detector.

Defense in depth only: a host can record tool output before a hook runs. Never
include inspected payloads, matched values, paths, or exception text in feedback.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 32
MAX_NODES = 100_000
REDACTED = '[wallet-secret content withheld]'
REASON = ('Wallet-secret protection blocked this content or operation. '
          'Use the independently controlled custody administrator; never paste secrets into chat.')
HEX = re.compile(r'(?<![a-zA-Z0-9])(?:0[xX])?[a-fA-F0-9]{64}(?![a-zA-Z0-9])')
XRPL = re.compile(r'(?<![a-zA-Z0-9])s(?:Ed[1-9A-HJ-NP-Za-km-z]{28}|[1-9A-HJ-NP-Za-km-z]{28})(?![a-zA-Z0-9])')
SECRET_FIELDS = {'privatekey', 'secret', 'walletsecret', 'seed', 'seedphrase',
                 'recoveryphrase', 'mnemonic', 'masterkey', 'secretkey'}
PUBLIC_HASH_FIELDS = {'txhash', 'transactionhash', 'blockhash', 'sha256', 'digest',
                      'contractdigest', 'codehash', 'requesthash'}
PUBLIC_URL_FIELDS = {'url', 'uri', 'refid', 'targeturl'}
EXPLORER_HOSTS = frozenset({'basescan.org', 'sepolia.basescan.org', 'etherscan.io',
                           'sepolia.etherscan.io', 'arbiscan.io', 'sepolia.arbiscan.io'})
TRANSACTION_READ_METHODS = frozenset({'eth_getTransactionByHash', 'eth_getTransactionReceipt'})
PUBLIC_HASH_LINE = re.compile(
    r'(?im)^\s*(?:tx[_ ]?hash|transaction[_ ]?hash|block[_ ]?hash|sha256|digest)\s*[:=]\s*'
    r'((?:0[xX])?[a-fA-F0-9]{64})\s*$')
KNOWN_ACCESS = re.compile(
    r'reveal-secret\.(?:sh|py)|custody[-_]admin(?:\.py)?|'
    r'/wallet/(?:reveal-secret|[^\s/"\']+/reveal)(?:[/\s?"\']|$)|'
    r'agent-data[/\\]+(?:master\.key|agent\.db(?:-wal|-shm)?)(?![\w.-])|'
    r'\b(?:reveal_wallet_secret|_load_secret)\s*\(|'
    r'\bsecurity\s+find-generic-password\b', re.IGNORECASE)


def normalized(name: str) -> str:
    return re.sub('[^a-z0-9]', '', name.lower())


def public_transaction_url(value: str) -> bool:
    """Recognize only exact HTTPS transaction links on configured explorers."""
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and url.hostname in EXPLORER_HOSTS and
                url.netloc == url.hostname and not url.query and not url.fragment and
                re.fullmatch(r'/tx/0x[a-fA-F0-9]{64}/?', url.path) is not None)
    except ValueError:
        return False


def transaction_read_request(value: dict) -> bool:
    """Recognize the exact JSON-RPC envelope/argument position, not a method label."""
    params = value.get('params')
    return (value.get('jsonrpc') == '2.0' and
            isinstance(value.get('method'), str) and value['method'] in TRANSACTION_READ_METHODS and
            set(value) <= {'jsonrpc', 'method', 'params', 'id'} and
            isinstance(params, list) and len(params) == 1 and isinstance(params[0], str) and
            re.fullmatch(r'0x[a-fA-F0-9]{64}', params[0]) is not None)


class Detector:
    def __init__(self) -> None:
        self.words = frozenset((ROOT / '.claude/hooks/bip39-english.txt').read_text().split())
        if len(self.words) != 2048:
            raise ValueError('Invalid detector wordlist')
        self.nodes = 0

    def mnemonic(self, text: str) -> bool:
        run = 0
        for word in re.findall(r'[a-z]+', text.lower()):
            run = run + 1 if word in self.words else 0
            if run >= 12:
                return True
        return False

    def scan(self, value, *, secret: bool = False, field: str = '', depth: int = 0):
        """Return (shape-preserving sanitized value, detected); never retain matches."""
        self.nodes += 1
        if depth > MAX_DEPTH or self.nodes > MAX_NODES:
            raise ValueError('Detector traversal limit')
        if isinstance(value, dict):
            result, hit = {}, False
            rpc_read = not secret and transaction_read_request(value)
            for key, item in value.items():
                # Keys are also untrusted strings; redacting one preserves JSON shape.
                clean_key, key_hit = self.scan(key, depth=depth + 1)
                name = normalized(key)
                clean, found = self.scan(item, secret=secret or name in SECRET_FIELDS,
                                         field='transactionhash' if rpc_read and key == 'params' else name,
                                         depth=depth + 1)
                result[clean_key] = clean
                hit |= found or key_hit
            return result, hit
        if isinstance(value, list):
            result, hit = [], False
            for item in value:
                clean, found = self.scan(item, secret=secret, field=field, depth=depth + 1)
                result.append(clean)
                hit |= found
            return result, hit
        if not isinstance(value, str):
            return value, False
        # MCP content frequently contains another serialized JSON result. Decode it
        # before matching so public hash context and escaped key fields survive.
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, (dict, list, str)) and decoded != value:
            clean, hit = self.scan(decoded, secret=secret, field=field, depth=depth + 1)
            if hit:
                return json.dumps(clean, ensure_ascii=True), True
            return value, False
        if XRPL.search(value) or self.mnemonic(value):
            return REDACTED, True
        candidate = value
        if not secret:
            if field in PUBLIC_HASH_FIELDS and HEX.fullmatch(value.strip()):
                return value, False
            if field in PUBLIC_URL_FIELDS and public_transaction_url(value):
                return value, False
            candidate = PUBLIC_HASH_LINE.sub('[public transaction or digest hash]', candidate)
        if HEX.search(candidate):
            return REDACTED, True
        return value, False


def known_secret_access(value) -> bool:
    """Best-effort prevention of known exports/files, not a shell sandbox."""
    if isinstance(value, dict):
        return any(known_secret_access(k) or known_secret_access(v) for k, v in value.items())
    if isinstance(value, list):
        return any(known_secret_access(v) for v in value)
    return isinstance(value, str) and bool(KNOWN_ACCESS.search(value.replace('\\/', '/')))


def response(client: str, event: str, payload: dict) -> tuple[dict | None, int]:
    detector = Detector()
    if event == 'UserPromptSubmit':
        _, hit = detector.scan(payload.get('prompt', payload))
        return ({'decision': 'block', 'reason': REASON}, 2) if hit else (None, 0)
    if event == 'PreToolUse':
        arguments = payload.get('tool_input', {})
        _, hit = detector.scan(arguments)
        if hit or known_secret_access(arguments) or known_secret_access(payload.get('tool_name', '')):
            return {'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'deny',
                                           'permissionDecisionReason': REASON}}, 0
        return None, 0
    if event == 'PostToolUseFailure':
        _, hit = detector.scan(payload.get('error', ''))
        # Claude documents no error replacement field for this event. Stop the
        # continuation, but do not claim the original error was removed.
        return ({'continue': False, 'stopReason': REASON,
                 'hookSpecificOutput': {'hookEventName': event, 'additionalContext': REASON}}, 0) if hit else (None, 0)
    if event != 'PostToolUse':
        raise ValueError('Unsupported hook event')
    output = payload.get('tool_response', payload)
    clean, hit = detector.scan(output)
    if not hit:
        return None, 0
    if client == 'codex':
        # updatedMCPToolOutput/suppressOutput are unsupported by Codex. Native
        # block replaces model feedback and rejects nested code-mode promises.
        return {'decision': 'block', 'reason': REASON}, 0
    return {'decision': 'block', 'reason': REASON, 'hookSpecificOutput': {
        'hookEventName': event, 'updatedToolOutput': clean, 'additionalContext': REASON}}, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', choices=['claude', 'codex'], required=True)
    parser.add_argument('--mode', choices=['user', 'pre', 'tool', 'failure'])
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Detector input limit')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('Invalid hook payload')
        event = {'user': 'UserPromptSubmit', 'pre': 'PreToolUse', 'tool': 'PostToolUse',
                 'failure': 'PostToolUseFailure'}.get(args.mode, payload.get('hook_event_name'))
        output, status = response(args.client, event, payload)
    except (ValueError, TypeError, OSError, UnicodeError, RecursionError):
        # Never print parser exceptions: they may contain source payload text.
        # Exit 2 alone only adds Claude post-tool feedback. A global stop also
        # stops continuation when the output cannot safely be inspected/replaced.
        if args.client == 'claude':
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
