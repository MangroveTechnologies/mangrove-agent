"""Use a loopback mangrove-agent from a command-capable AI client or stdio MCP.

Use --list-tools and --call from Claude, Codex, Grok, or Perplexity Computer.
Without these options, forward stdio JSON-RPC to the same agent. Credentials
are read locally and sent only to the configured loopback agent. This script
never starts the agent, changes its database, or contacts MangroveAI directly.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from agent_context import render_context
from setup_support import NoRedirect, SetupError, local_key, origin, read_config, require_auth

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 8 * 1024 * 1024


class Bridge:
    def __init__(self, config: dict):
        require_auth(config)
        self.url = origin(config.get('LOCAL_AGENT_URL', '')) + '/mcp/'
        self.headers = {'Content-Type': 'application/json',
                        'Accept': 'application/json, text/event-stream',
                        'X-API-Key': local_key(config)}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def forward(self, message: dict) -> dict | None:
        request = urllib.request.Request(self.url, data=json.dumps(message).encode(),
                                         headers=self.headers, method='POST')
        try:
            with self.opener.open(request, timeout=300) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise SetupError('Agent response exceeded the bridge limit.')
                if not body:
                    if 'id' in message:
                        raise SetupError('Agent returned no response to an MCP request.')
                    return None
                if response.headers.get_content_type() != 'application/json':
                    raise SetupError('The bridge requires the agent stateless JSON HTTP transport.')
                result = json.loads(body)
                if not isinstance(result, dict) or result.get('jsonrpc') != '2.0':
                    raise SetupError('Agent returned an invalid MCP response.')
                if result.get('id') != message.get('id'):
                    raise SetupError('Agent returned an unexpected MCP response identifier.')
                if message.get('method') == 'initialize' and 'result' in result:
                    version = result['result'].get('protocolVersion')
                    if isinstance(version, str):
                        self.headers['MCP-Protocol-Version'] = version
                return result
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            uncertain = ' The request may have completed; inspect its outcome before retrying.' if code >= 500 else ''
            raise SetupError(f'Local agent returned HTTP {code}.{uncertain} No retry was attempted.') from None
        except (OSError, ValueError, TypeError):
            raise SetupError('Cannot communicate with the local agent. The request may have completed; '
                             'inspect its outcome before retrying. No server was started or request retried.') from None


def serve(bridge: Bridge, source, target) -> int:
    while line := source.readline(MAX_BYTES + 1):
        request_id = None
        try:
            if len(line) > MAX_BYTES:
                raise SetupError('MCP request exceeded the bridge limit.')
            message = json.loads(line)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                raise SetupError('Invalid MCP request.')
            request_id = message.get('id')
            response = bridge.forward(message)
            if response is not None:
                target.write(json.dumps(response, ensure_ascii=True) + '\n')
                target.flush()
        except (SetupError, ValueError, TypeError) as exc:
            # Never echo a request, endpoint, credential, or raw transport error.
            message = str(exc) if isinstance(exc, SetupError) else 'Invalid MCP JSON message.'
            if request_id is not None:
                target.write(json.dumps({'jsonrpc': '2.0', 'id': request_id,
                                         'error': {'code': -32603, 'message': message}}) + '\n')
                target.flush()
            print(message, file=sys.stderr)
            return 1
    return 0


def command(bridge: Bridge, tool: str | None, arguments_file: str | None) -> int:
    arguments = {}
    if arguments_file:
        if arguments_file == '-':
            body = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            with Path(arguments_file).open('rb') as stream:
                body = stream.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise SetupError('Tool arguments exceeded the client limit.')
        try:
            arguments = json.loads(body)
        except (ValueError, UnicodeError):
            raise SetupError('Tool arguments must be a JSON object.') from None
        if not isinstance(arguments, dict):
            raise SetupError('Tool arguments must be a JSON object.')
    response = bridge.forward({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                               'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                                          'clientInfo': {'name': 'mangrove-agent-client', 'version': '1'}}})
    if 'error' not in response:
        bridge.forward({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        request = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}
        if tool:
            request.update(method='tools/call', params={'name': tool, 'arguments': arguments})
        response = bridge.forward(request)
    print(json.dumps(response, ensure_ascii=True))
    return 1 if 'error' in response or response.get('result', {}).get('isError') else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'server/src/config/local-config.json',
                        help='Local agent configuration file; never pass a key as an argument.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list-tools', action='store_true', help='Print the live MCP tool catalog as JSON.')
    mode.add_argument('--call', metavar='TOOL', help='Call an advertised MCP tool and print its JSON response.')
    parser.add_argument('--arguments-file', metavar='PATH',
                        help='JSON object for --call; use - to read from stdin. Defaults to {}.')
    args = parser.parse_args()
    if args.arguments_file and not args.call:
        parser.error('--arguments-file requires --call')
    try:
        render_context(ROOT)
        bridge = Bridge(read_config(args.config))
        if args.list_tools or args.call:
            return command(bridge, args.call, args.arguments_file)
        return serve(bridge, sys.stdin.buffer, sys.stdout)
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, TypeError, AttributeError):
        print('Cannot load the local agent configuration or policy. No server was started.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
