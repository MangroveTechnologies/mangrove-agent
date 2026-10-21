"""Exercise the local stdio adapter over real HTTP using synthetic secrets."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BRIDGE = ROOT / 'scripts/local_mcp_bridge.py'
LOCAL_KEY = 'synthetic-bridge-local-key'
UPSTREAM_KEY = 'synthetic-upstream-key-never-forward'


@pytest.fixture
def local_agent():
    requests = []
    behavior = {'error_status': None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.path, dict(self.headers), message))
            if behavior['error_status']:
                self.send_response(behavior['error_status'])
                self.send_header('Location', 'http://must-not-be-contacted.invalid/mcp/')
                self.end_headers()
                self.wfile.write(b'private diagnostic body must not be printed')
                return
            if 'id' not in message:
                self.send_response(202)
                self.end_headers()
                return
            if message['method'] == 'initialize':
                result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'mangrove-agent', 'version': 'synthetic'}}
            elif message['method'] == 'tools/list':
                result = {'tools': [{'name': 'status', 'description': 'Free status',
                                     'inputSchema': {'type': 'object'}}]}
            else:
                result = {'content': [{'type': 'text', 'text': '{"ready":true}'}], 'isError': False}
            body = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', requests, behavior
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def run_bridge(tmp_path, url, messages, *, options=(), input_text=None):
    config = tmp_path / 'local-config.json'
    config.write_text(json.dumps({'AUTH_ENABLED': True, 'API_KEYS': LOCAL_KEY,
                                  'MANGROVE_API_KEY': UPSTREAM_KEY, 'LOCAL_AGENT_URL': url}))
    return subprocess.run([sys.executable, str(BRIDGE), '--config', str(config), *options],
                          input=input_text if input_text is not None else ''.join(json.dumps(m) + '\n' for m in messages),
                          text=True, capture_output=True, timeout=15,
                          env=dict(os.environ, HTTP_PROXY='http://must-not-be-contacted.invalid',
                                   NO_PROXY=''))


def test_stdio_client_reaches_agent_with_only_local_header(tmp_path, local_agent):
    url, requests, _ = local_agent
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'synthetic-perplexity', 'version': '1'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 'catalog', 'method': 'tools/list', 'params': {}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
         'params': {'name': 'status', 'arguments': {}}},
    ]
    result = run_bridge(tmp_path, url, messages)
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r['id'] for r in responses] == [1, 'catalog', 3]
    assert responses[1]['result']['tools'][0]['name'] == 'status'
    assert responses[2]['result']['content'][0]['text'] == '{"ready":true}'
    assert [r[2] for r in requests] == messages
    for path, headers, message in requests:
        assert path == '/mcp/'
        assert headers['X-Api-Key'] == LOCAL_KEY
        assert UPSTREAM_KEY not in json.dumps(headers)
        assert LOCAL_KEY not in json.dumps(message)
    assert requests[-1][1]['Mcp-Protocol-Version'] == '2025-06-18'
    assert LOCAL_KEY not in result.stdout + result.stderr
    assert UPSTREAM_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize('status', [302, 503])
def test_failure_is_not_redirected_or_retried_and_diagnostics_are_redacted(tmp_path, local_agent, status):
    url, requests, behavior = local_agent
    behavior['error_status'] = status
    result = run_bridge(tmp_path, url, [{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}])
    assert result.returncode == 1
    assert len(requests) == 1
    error = json.loads(result.stdout)['error']['message']
    assert f'HTTP {status}' in error
    assert 'No retry' in error
    if status >= 500:
        assert 'may have completed' in error
    assert 'private diagnostic' not in result.stdout + result.stderr
    assert LOCAL_KEY not in result.stdout + result.stderr
    assert UPSTREAM_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize('url', ['https://api.mangrovedeveloper.ai',
                                'http://127.0.0.1:9080?key=secret',
                                'http://user:password@127.0.0.1:9080',
                                'http://example.com:9080'])
def test_bridge_refuses_non_loopback_or_credential_bearing_destination(tmp_path, url):
    result = run_bridge(tmp_path, url, [])
    assert result.returncode == 1
    assert 'HTTP loopback origin' in result.stderr
    assert result.stdout == ''
    assert 'password' not in result.stderr
    assert LOCAL_KEY not in result.stderr
    assert UPSTREAM_KEY not in result.stderr


@pytest.mark.parametrize('tool', [None, 'status'])
def test_command_client_discovers_and_calls_the_same_mcp_tools(tmp_path, local_agent, tool):
    url, requests, _ = local_agent
    arguments = {'symbols': ['ETH', 'BTC'], 'limit': 2}
    path = tmp_path / 'arguments with spaces.json'
    path.write_text(json.dumps(arguments))
    options = ['--call', tool, '--arguments-file', str(path)] if tool else ['--list-tools']
    result = run_bridge(tmp_path, url, [], options=options)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)['result']
    assert [r[2]['method'] for r in requests] == ['initialize', 'notifications/initialized',
                                                'tools/call' if tool else 'tools/list']
    if tool:
        assert requests[-1][2]['params'] == {'name': tool, 'arguments': arguments}
        assert response['content'][0]['text'] == '{"ready":true}'
    else:
        assert response['tools'][0]['name'] == 'status'
    assert all(r[1]['X-Api-Key'] == LOCAL_KEY for r in requests)
    assert LOCAL_KEY not in result.stdout + result.stderr
    assert UPSTREAM_KEY not in result.stdout + result.stderr


def test_command_client_accepts_stdin_json(tmp_path, local_agent):
    url, requests, _ = local_agent
    result = run_bridge(tmp_path, url, [], options=['--call', 'status', '--arguments-file', '-'],
                        input_text='{"detail":true}')
    assert result.returncode == 0, result.stderr
    assert requests[-1][2]['params']['arguments'] == {'detail': True}


@pytest.mark.parametrize('body', ['[]', 'not-json'])
def test_invalid_command_arguments_fail_before_sending_a_request(tmp_path, local_agent, body):
    url, requests, _ = local_agent
    result = run_bridge(tmp_path, url, [], options=['--call', 'status', '--arguments-file', '-'],
                        input_text=body)
    assert result.returncode == 1
    assert requests == []
    assert 'JSON object' in result.stderr
    assert result.stdout == ''
