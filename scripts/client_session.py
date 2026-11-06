"""Launch a repository client with local MCP credentials kept out of its arguments.

These are developer sessions, not the restricted Claude chat profile. Existing
client configuration is never overwritten. No server is started or restarted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from agent_context import render_context
from setup_support import SetupError, local_key, origin, read_config, require_auth

ROOT = Path(__file__).resolve().parents[1]


def prepare_config(client: str, root: Path, url: str) -> Path:
    path = root / f'.{client}/config.toml'
    if path.is_symlink() or path.parent.is_symlink():
        raise SetupError('Client configuration must not be a symlink; existing settings were preserved.')
    template = (root / f'.{client}/config.toml.example').read_text()
    if client == 'codex':
        template = template.replace('"http://127.0.0.1:9080/mcp/"', json.dumps(url))
    if not path.exists():
        path.parent.mkdir(mode=0o700, exist_ok=True)
        # Exclusive creation preserves another process's concurrent setup.
        with path.open('x') as stream:
            stream.write(template)
    config = tomllib.loads(path.read_text())
    server = config.get('mcp_servers', {}).get('mangrove-agent', {})
    if client == 'codex':
        valid = (server.get('url') == url and
                 server.get('env_http_headers', {}).get('X-API-Key') == 'MANGROVE_LOCAL_API_KEY')
    else:
        valid = (server.get('url') == '${MANGROVE_AGENT_MCP_URL:-http://127.0.0.1:9080/mcp/}' and
                 server.get('headers', {}).get('X-API-Key') == '${MANGROVE_LOCAL_API_KEY}')
    if not valid or server.get('enabled') is False:
        raise SetupError(f'Existing {path.relative_to(root)} needs the mangrove-agent example table merged '
                         'with this installation\'s URL. Existing settings were preserved.')
    return path


def launch(client: str) -> int:
    executable = shutil.which(client)
    if executable is None:
        raise SetupError(f'Install {client} before starting its agent session.')
    cfg = read_config(ROOT / 'server/src/config/local-config.json')
    require_auth(cfg)
    try:
        render_context(ROOT)
    except (OSError, UnicodeError):
        raise SetupError('Required agent instructions are missing; repair the installation.') from None
    url = origin(cfg.get('LOCAL_AGENT_URL', '')) + '/mcp/'
    prepare_config(client, ROOT, url)
    env = dict(os.environ, MANGROVE_LOCAL_API_KEY=local_key(cfg), MANGROVE_AGENT_MCP_URL=url)
    return subprocess.run([executable], cwd=ROOT, env=env, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('client', choices=['codex', 'grok'])
    args = parser.parse_args()
    try:
        return launch(args.client)
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, TypeError, AttributeError):
        print('Cannot load client configuration; existing settings were preserved.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
