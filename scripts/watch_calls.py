#!/usr/bin/env python3
"""Watch local MCP calls without printing raw logs, arguments, or credentials."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTAINER = 'mangrove-benchmark-local-mangrove-app-1'
NAME = re.compile(r'^[a-zA-Z0-9_.:-]{1,100}$')
ACCESS = re.compile(r'"(GET|POST|DELETE|PUT|PATCH|OPTIONS) ([^ ]+) HTTP/[\d.]+" (\d{3})')
ROUTES = re.compile(r'^/(?:mcp(?:/connect)?/?|health|oauth/(?:authorize|token|revoke|register)|api/v1/agent/(?:status|oauth/(?:status|start|callback|cancel)))$')
PAYMENT_REASONS = {'invalid_exact_evm_transaction_failed', 'unknown', 'facilitator_error',
                   'transaction_failed', 'settlement_pending', 'insufficient_funds',
                   'invalid_signature', 'expired', 'verification_failed'}
PAYMENT_CATEGORIES = {'nonce_conflict', 'insufficient_funds', 'insufficient_allowance',
                      'authorization_used', 'authorization_expired', 'transaction_reverted',
                      'rate_limited', 'timeout'}
AGENT_FAILURES = frozenset({
    'x402.spend.settlement_unconfirmed', 'x402.payment.reconciliation_failed',
    'x402.payment.unsettled', 'x402.payment.refused', 'x402.payment.errored',
    'x402.payment.payer_mismatch', 'x402.settlement.undecodable',
    'x402.settlement.invalid', 'x402.spend.refused', 'x402.spend.exhausted',
    'x402.payment.over_budget',
})
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
CONSOLE_EVENT = re.compile(
    r'^(?:\d{4}-\d{2}-\d{2}T[^ ]+\s+)?'
    r'\[(?:debug|info|warning|error|critical)\s*\]\s+'
    r'(?P<event>x402\.[a-z_.]+)(?:\s|$)')
MAX_LINE = 65536


def agent_failure(event, status=None):
    if not isinstance(event, str) or event not in AGENT_FAILURES:
        return None
    safe_status = str(status) if type(status) is int and 100 <= status <= 599 else '-'
    return f'AGENT PAYMENT failure event={event} http={safe_status}'


def label(value):
    return value if isinstance(value, str) and NAME.fullmatch(value) else '-'


def oauth_context(extra):
    if extra.get('caller_auth_method') != 'oauth':
        return ''
    fields = (('workspace', 'oauth_workspace', {'personal', 'organization'}),
              ('role', 'oauth_role', {'owner', 'admin', 'member', 'viewer'}),
              ('effective_role', 'oauth_effective_role', {'owner', 'admin', 'member', 'viewer'}))
    parts = []
    for name, key, allowed in fields:
        value = extra.get(key)
        parts.append(f"{name}={value if isinstance(value, str) and value in allowed else '-'}")
    return ' ' + ' '.join(parts)


def number(value):
    return str(value) if type(value) is int and 0 <= value <= 10**12 else '-'


def summarize(line: str, source: str) -> str | None:
    """Emit only explicitly permitted fields; unknown records are discarded."""
    if not isinstance(line, str) or len(line) > MAX_LINE:
        return None
    try:
        record = json.loads(line)
    except (ValueError, TypeError, RecursionError):
        record = None
    if isinstance(record, dict):
        if source == 'AGENT':
            return agent_failure(record.get('event'), record.get('status_code'))
        extra = record.get('extra')
        if not isinstance(extra, dict):
            return None
        event = record.get('message')
        if not isinstance(event, str):
            return None
        if event == 'mcp_tool_call':
            if extra.get('failure_reason') == 'payment_required':
                return 'SERVER QUOTE payment_required (normal x402 handshake)'
            outcome = {True: 'OK', False: 'FAILED'}.get(extra.get('success')) if type(extra.get('success')) is bool else 'UNKNOWN'
            reason = extra.get('failure_reason')
            safe_failure = reason if isinstance(reason, str) and (reason in {'bad_arguments', 'mcp_error', 'unhandled_exception', 'access_denied', 'unknown_tool'} or re.fullmatch(r'upstream_[45][0-9]{2}', reason)) else 'unknown'
            return (f"SERVER TOOL {label(extra.get('tool'))} auth={label(extra.get('caller_auth_method'))} "
                    f"effect={label(extra.get('side_effect'))} result={outcome} ms={number(extra.get('duration_ms'))}"
                    + oauth_context(extra)
                    + (f' reason={safe_failure}' if outcome != 'OK' else ''))
        if event == 'x402 payment settled':
            return 'SERVER PAYMENT settlement_confirmed'
        if event in {'x402 settlement refused', 'x402 verification refused',
                     'x402 settle_payment raised', 'x402 verify_payment raised'}:
            reason = extra.get('reason')
            reason = reason if isinstance(reason, str) and reason in PAYMENT_REASONS else 'unknown'
            values = extra.get('message_categories', [])
            categories = sorted({v for v in values if isinstance(v, str) and v in PAYMENT_CATEGORIES}) if isinstance(values, list) else []
            stage = 'verify' if event in {'x402 verification refused', 'x402 verify_payment raised'} else 'settle'
            return f"SERVER PAYMENT failure stage={stage} reason={reason} evidence={','.join(categories) or 'unavailable'}"
        if event == 'x402 settlement unconfirmed; resource withheld':
            return 'SERVER PAYMENT unresolved resource_withheld'
        if event == 'strategy_workflow_call':
            result = extra.get('result')
            count = result.get('count', result.get('total_count')) if isinstance(result, dict) else None
            return (f"SERVER WORKFLOW {label(extra.get('tool'))} auth={label(extra.get('auth_method'))} "
                    f"result={label(extra.get('outcome'))} count={number(count)}")
        if event == 'api_call_metered':
            return (f"SERVER USAGE meter={label(extra.get('meter'))} auth={label(extra.get('auth_method'))} "
                    f"origin={label(extra.get('origin'))} http={number(extra.get('status'))}")
        if event in {'oauth_authorize_started', 'oauth_consent_decided', 'oauth_token_issued', 'oauth_token_revoked'}:
            return f"SERVER AUTH {event}"
        return None
    if source == 'AGENT':
        plain = ANSI.sub('', line)
        event_match = CONSOLE_EVENT.match(plain)
        if event_match:
            # Only the event token is parsed from pretty output. Remaining
            # fields may contain arbitrary quoted text; never scan them for
            # status or reasons that could be injected by that text.
            return agent_failure(event_match['event'])
    match = ACCESS.search(line)
    if match:
        method, path, status = match.groups()
        path = path.split('?', 1)[0]
        if status == '200' and path in {'/health', '/api/v1/agent/oauth/status'}:
            return None
        if ROUTES.fullmatch(path):
            return f'{source} HTTP {method} {path} {status}'
    return None


class SessionSummary:
    """Bounded, sanitized event summary; counts are log events, not user requests."""
    def __init__(self):
        self.started = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.counts = Counter()
        self.tools = Counter()
        self.failures = Counter()
        self.http_failures = Counter()
        self.oauth_contexts = Counter()

    def observe(self, message):
        if message.startswith('SERVER TOOL '):
            self.counts['tool_events'] += 1
            context = re.search(r' workspace=(personal|organization|-) role=(owner|admin|member|viewer|-) effective_role=(owner|admin|member|viewer|-)(?= |$)', message)
            if ' auth=oauth ' in message and context:
                self.oauth_contexts[context.group().strip()] += 1
            self.counts['ok' if 'result=OK' in message else 'failed' if 'result=FAILED' in message else 'unknown'] += 1
            key = ' '.join(message.split()[2:4])
            self.tools[key if key in self.tools or len(self.tools) < 100 else '(other tools)'] += 1
            if 'result=FAILED' in message:
                reason = message.rsplit(' reason=', 1)[-1]
                self.failures['tool:' + reason] += 1
        elif message.startswith('SERVER QUOTE '):
            self.counts['quotes'] += 1
        elif message == 'SERVER PAYMENT settlement_confirmed':
            self.counts['settlements'] += 1
        elif message.startswith('SERVER PAYMENT failure '):
            self.counts['payment_failures'] += 1
            self.failures[message.removeprefix('SERVER PAYMENT failure ')] += 1
        elif message == 'SERVER PAYMENT unresolved resource_withheld':
            self.counts['unresolved'] += 1
        elif message.startswith('AGENT PAYMENT failure '):
            self.counts['agent_payment_failures'] += 1
            self.failures[message] += 1
        elif ' HTTP ' in message and re.search(r' [45][0-9]{2}$', message):
            self.counts['http_errors'] += 1
            key = message if message in self.http_failures or len(self.http_failures) < 100 else '(other HTTP errors)'
            self.http_failures[key] += 1

    def render(self, since, complete):
        c = self.counts
        lines = ['WATCH SUMMARY', f'Started UTC: {self.started}; server history: {since}',
                 'Coverage: ' + ('watch stopped normally' if complete else 'log stream interrupted; results may be incomplete'),
                 f"Tool events: {c['ok']} OK, {c['failed']} failed, {c['unknown']} unknown",
                 f"Payments: {c['settlements']} settlement confirmations, {c['payment_failures']} failure diagnostics, {c['unresolved']} unresolved events",
                 f"Agent payment errors: {c['agent_payment_failures']}",
                 f"Normal payment quotes: {c['quotes']}; HTTP error events: {c['http_errors']}"]
        lines += [f'  {name}: {count}' for name, count in self.tools.most_common(10)]
        lines += [f'  OAuth {context}: {count} tool events' for context, count in self.oauth_contexts.most_common(10)]
        lines += [f'  Failure {reason}: {count}' for reason, count in self.failures.most_common(10)]
        lines += [f'  HTTP error {reason}: {count}' for reason, count in self.http_failures.most_common(10)]
        if any(len(items) > 10 for items in (self.tools, self.failures, self.http_failures, self.oauth_contexts)):
            lines.append('Detail lists show the 10 most frequent entries; totals include all observed events.')
        lines.append('Counts are observed log events, not unique requests or ledger totals. Failure diagnostics may describe the same failed call.')
        lines.append('The watcher did not retry payments or change ledger records. Missing events do not prove success.')
        return '\n'.join(lines)


def visible(message):
    # Quotes, metering, workflow duplicates and successful HTTP transport are noise.
    return (message.startswith(('SERVER TOOL ', 'SERVER PAYMENT ', 'SERVER AUTH ', 'AGENT PAYMENT '))
            or (' HTTP ' in message and re.search(r' [45][0-9]{2}$', message) is not None))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default=DEFAULT_CONTAINER, help='Local MangroveAI Docker container')
    parser.add_argument('--since', default='0s', help='Docker history window (default: new events only)')
    parser.add_argument('--agent-log', type=Path, default=ROOT / 'agent-data/bare.log')
    parser.add_argument('--snapshot', action='store_true', help='Show recent server events and exit')
    parser.add_argument('--summary-file', type=Path, default=ROOT / 'agent-data/watch-calls-summary.txt',
                        help='Save sanitized summary here (overwritten each run)')
    args = parser.parse_args()
    if not shutil.which('docker'):
        parser.error('Docker is not available on PATH.')
    check = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', args.container],
                           capture_output=True, text=True)
    if check.returncode or check.stdout.strip() != 'true':
        parser.error('Cannot access the running API container. Start Docker/the API or use --container NAME.')
    print(f'Watching API container: {args.container}', flush=True)
    print('Only selected log fields are shown; bodies, tokens and account IDs are omitted.', flush=True)
    print('Only tool outcomes and payment/auth problems; normal HTTP/quotes/metering hidden.', flush=True)
    print('Ctrl+C stops watching only, then prints and saves a compact summary.', flush=True)
    commands = [('SERVER', ['docker', 'logs', '--since', args.since,
                             *([] if args.snapshot else ['--follow']), args.container])]
    if not args.snapshot:
        if args.agent_log.is_file():
            commands.append(('AGENT', ['tail', '-n', '0', '-F', str(args.agent_log)]))
        else:
            print('Agent log not found; watching the server only.', flush=True)
    events = queue.Queue(maxsize=1024)
    stopping = threading.Event()
    reader_issues = Counter()
    issue_lock = threading.Lock()
    processes = []
    readers = []
    summary = SessionSummary()
    complete = False

    def consume(message):
        summary.observe(message)
        if visible(message):
            print(message, flush=True)

    def read_stream(source, process):
        def send(message):
            while not stopping.is_set():
                try:
                    events.put(message, timeout=0.1)
                    return
                except queue.Full:
                    pass
            try:
                events.put_nowait(message)
            except queue.Full:
                with issue_lock:
                    reader_issues['shutdown_queue_overflow'] += 1

        code = -1
        try:
            while True:
                line = process.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    while line and not line.endswith('\n'):
                        line = process.stdout.readline(MAX_LINE + 1)
                    with issue_lock:
                        reader_issues['oversized_lines'] += 1
                    continue
                message = summarize(line, source)
                if message:
                    send(message)
            code = process.wait()
        except Exception:
            # Report coverage failure, never raw log content or traceback.
            with issue_lock:
                reader_issues['reader_errors'] += 1
        finally:
            process.stdout.close()
            send((source, code))

    try:
        for source, command in commands:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, errors='replace', start_new_session=True)
            processes.append(process)
            reader = threading.Thread(target=read_stream, args=(source, process), daemon=True)
            readers.append(reader)
            reader.start()
        while True:
            message = events.get()
            if isinstance(message, tuple):
                source, code = message
                if not args.snapshot or code:
                    print(f'{source} log stream ended (exit {code}); restart this watcher after the service is ready.', flush=True)
                complete = args.snapshot and code == 0
                return 0 if args.snapshot and code == 0 else 1
            consume(message)
    except KeyboardInterrupt:
        complete = True
        print('\nStopped watching. Services remain running.')
        return 0
    finally:
        stopping.set()
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for reader in readers:
            reader.join(timeout=3)
            if reader.is_alive():
                with issue_lock:
                    reader_issues['reader_shutdown_incomplete'] += 1
        while True:
            try:
                message = events.get_nowait()
            except queue.Empty:
                break
            if isinstance(message, str):
                consume(message)
        report = summary.render(args.since, complete and not reader_issues)
        report += '\nSources: SERVER history/live' + ('; AGENT live only' if len(commands) > 1 else '; agent log not included')
        if reader_issues:
            report += '\nCoverage gaps: ' + ', '.join(f'{k}={v}' for k, v in sorted(reader_issues.items()))
        print('\n' + report, flush=True)
        temporary = None
        try:
            args.summary_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=args.summary_file.parent, delete=False) as output:
                temporary = output.name
                output.write(report + '\n')
            os.replace(temporary, args.summary_file)
            temporary = None
            print(f'Summary saved: {args.summary_file}', flush=True)
        except OSError:
            print('Could not save summary; the summary above is still available.', flush=True)
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
