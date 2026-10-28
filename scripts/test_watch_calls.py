"""Regression checks for log filtering; no running services required."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from watch_calls import summarize, visible, SessionSummary


class WatchCallsTests(unittest.TestCase):
    def test_http_failure_identity_survives_summary_without_query(self):
        session = SessionSummary()
        event = summarize('INFO "POST /mcp/?token=SYNTHETIC_SECRET HTTP/1.1" 503 error', 'AGENT')
        session.observe(event)
        report = session.render('0s', True)
        self.assertIn('AGENT HTTP POST /mcp/ 503: 1', report)
        self.assertNotIn('SYNTHETIC_SECRET', report)

    def test_actual_agent_renderers_capture_failures_without_sensitive_fields(self):
        import structlog
        for renderer in [structlog.dev.ConsoleRenderer(colors=True),
                         structlog.dev.ConsoleRenderer(colors=False),
                         structlog.processors.JSONRenderer()]:
            for event in ['x402.spend.settlement_unconfirmed', 'x402.payment.reconciliation_failed']:
                raw = renderer(None, 'warning', dict(event=event, level='warning',
                    timestamp='2026-10-28T20:15:41Z', status_code=503,
                    error_type='SYNTHETIC_SECRET', reservation_ids=['SYNTHETIC_SECRET']))
                output = summarize(raw, 'AGENT')
                self.assertIsNotNone(output)
                self.assertTrue(visible(output))
                self.assertNotIn('SYNTHETIC_SECRET', output)
                self.assertNotIn('\x1b', output)
                session = SessionSummary()
                session.observe(output)
                self.assertIn(event, session.render('0s', True))
                self.assertEqual(session.counts['agent_payment_failures'], 1)
        # An error token embedded in someone else's message is not an event.
        self.assertIsNone(summarize('[warning] something_else message=x402.payment.reconciliation_failed', 'AGENT'))

    def test_non_string_event_and_nested_json_do_not_crash_parser(self):
        for event in [{}, [], None, 10]:
            self.assertIsNone(summarize(json.dumps({'message': event, 'extra': {}}), 'SERVER'))
            self.assertIsNone(summarize(json.dumps({'event': event}), 'AGENT'))
        self.assertIsNone(summarize('[' * 2000 + ']' * 2000, 'SERVER'))

    def test_snapshot_reader_survives_malformed_and_oversized_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            docker = directory / 'docker'
            record = json.dumps({'message': 'mcp_tool_call', 'extra': {
                'tool': 'list_strategies', 'caller_auth_method': 'oauth', 'success': True}})
            docker.write_text(f'#!{sys.executable}\nimport sys\n'
                'if sys.argv[1] == "inspect": print("true")\n'
                'else:\n'
                ' print(\'{"message": {}, "extra": {}}\')\n'
                ' print("SYNTHETIC_SECRET" * 10000)\n'
                f' print({record!r})\n')
            docker.chmod(0o700)
            output = directory / 'summary.txt'
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('watch_calls.py')),
                '--snapshot', '--summary-file', str(output)],
                env=dict(os.environ, PATH=str(directory) + os.pathsep + os.environ.get('PATH', '')),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = output.read_text()
            self.assertIn('1 OK', report)
            self.assertIn('results may be incomplete', report)
            self.assertIn('oversized_lines=1', report)
            self.assertNotIn('SYNTHETIC_SECRET', result.stdout + result.stderr + report)

    def test_normal_challenge_not_failure_and_noise_hidden(self):
        output = summarize(json.dumps({'message': 'mcp_tool_call', 'extra': {
            'tool': 'query_signal_behavior', 'success': False, 'failure_reason': 'payment_required',
        }}), 'SERVER')
        session = SessionSummary()
        session.observe(output)
        self.assertEqual(session.counts['quotes'], 1)
        self.assertEqual(session.counts['failed'], 0)
        for message in [output, 'AGENT HTTP POST /mcp/ 200', 'SERVER USAGE meter=test',
                        'SERVER WORKFLOW list_strategies result=ok']:
            self.assertFalse(visible(message))
        self.assertTrue(visible('AGENT HTTP POST /mcp/ 503'))

    def test_payment_diagnostics_and_summary_do_not_copy_secrets(self):
        session = SessionSummary()
        for record in [
            {'message': 'x402 settlement refused', 'extra': {
                'reason': 'invalid_exact_evm_transaction_failed',
                'message_categories': ['nonce_conflict', 'secret'], 'error_message': 'Bearer secret'}},
            {'message': 'x402 payment settled', 'extra': {'tx_hash': 'private-tx', 'wallet_address': 'private-wallet'}},
            {'message': 'mcp_tool_call', 'extra': {'tool': 'query_signal_behavior',
                'caller_auth_method': 'x402', 'success': False, 'failure_reason': 'upstream_503'}},
        ]:
            output = summarize(json.dumps(record), 'SERVER')
            self.assertTrue(visible(output))
            session.observe(output)
        report = session.render('0s', True)
        self.assertIn('nonce_conflict', report)
        self.assertIn('upstream_503', report)
        self.assertIn('1 settlement confirmations', report)
        self.assertNotIn('secret', report)
        self.assertNotIn('private-', report)

    def test_malformed_diagnostics_are_safe(self):
        for reason in [{'token': 'secret'}, ['secret'], 'Bearer secret']:
            output = summarize(json.dumps({'message': 'mcp_tool_call', 'extra': {
                'tool': 'safe_tool', 'success': False, 'failure_reason': reason}}), 'SERVER')
            self.assertIn('reason=unknown', output)
            self.assertNotIn('secret', output)
        output = summarize(json.dumps({'message': 'x402 settlement refused', 'extra': {
            'reason': {'secret': True}, 'message_categories': [{'secret': True}]}}), 'SERVER')
        self.assertIn('evidence=unavailable', output)

    def test_empty_or_interrupted_summary_does_not_claim_success(self):
        report = SessionSummary().render('10m', False)
        self.assertIn('results may be incomplete', report)
        self.assertIn('Missing events do not prove success', report)

    def test_oauth_tool_result_without_private_fields(self):
        record = {'message': 'mcp_tool_call', 'user_id': 'private-user', 'extra': {
            'tool': 'list_strategies', 'caller_auth_method': 'oauth', 'side_effect': 'read',
            'success': True, 'duration_ms': 12, 'arguments': {'token': 'secret'},
        }}
        output = summarize(json.dumps(record), 'SERVER')
        self.assertIn('list_strategies auth=oauth', output)
        self.assertIn('result=OK', output)
        self.assertNotIn('secret', output)
        self.assertNotIn('private-user', output)

    def test_failure_is_not_http_success(self):
        output = summarize(json.dumps({'message': 'mcp_tool_call', 'extra': {
            'tool': 'list_strategies', 'caller_auth_method': 'oauth', 'success': False,
        }}), 'SERVER')
        self.assertIn('result=FAILED', output)

    def test_callback_query_and_raw_errors_not_exposed(self):
        output = summarize('INFO "GET /api/v1/agent/oauth/callback?code=secret&state=private HTTP/1.1" 200 OK', 'AGENT')
        self.assertEqual(output, 'AGENT HTTP GET /api/v1/agent/oauth/callback 200')
        self.assertIsNone(summarize('Error: Bearer secret', 'SERVER'))
        self.assertIsNone(summarize('"GET /unknown/secret HTTP/1.1" 401', 'SERVER'))
        self.assertIsNone(summarize('"GET /health HTTP/1.1" 200', 'SERVER'))
        self.assertEqual(summarize('"GET /health HTTP/1.1" 503', 'SERVER'),
                         'SERVER HTTP GET /health 503')

    def test_no_raw_result_or_terminal_control_output(self):
        output = summarize(json.dumps({'message': 'strategy_workflow_call', 'extra': {
            'tool': 'bad\x1b[31m', 'auth_method': 'oauth', 'outcome': 'ok',
            'result': {'count': 4, 'strategies': [{'name': 'private-data'}]},
        }}), 'SERVER')
        self.assertIn('count=4', output)
        self.assertNotIn('\x1b', output)
        self.assertNotIn('private-data', output)


if __name__ == '__main__':
    unittest.main()
