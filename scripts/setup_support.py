"""Internal helpers for setup.sh. No application imports or implicit wallet actions."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
import re
import secrets
import select
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
import webbrowser
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    import termios
    import tty
except ImportError:  # Windows: menus fall back to numbered choices.
    termios = tty = None

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'server/src/config/local-config.json'
EXAMPLE = ROOT / 'server/src/config/local-example-config.json'
PUBLISHED = {'dev-key-1', 'GENERATED_BY_SETUP'}
PLACEHOLDER = 'REPLACE_WITH_YOUR_DEV_OR_PROD_KEY'
NETWORKS = {'eip155:84532': (84532, 'testnet', 'Base Sepolia (test USDC)'),
            'eip155:8453': (8453, 'mainnet', 'Base mainnet (real USDC)')}


class SetupError(Exception):
    pass


def read_config(path=CONFIG):
    if path.is_symlink():
        raise SetupError('Config must be a regular file, not a symlink.')
    try:
        cfg = json.loads(path.read_text())
        if not isinstance(cfg, dict):
            raise ValueError
        return cfg
    except (OSError, ValueError):
        raise SetupError('Cannot read configuration JSON; repair it before running setup.') from None


def atomic_config(cfg, path=CONFIG):
    if path.is_symlink():
        raise SetupError('Refusing to replace a symlink config.')
    fd, name = tempfile.mkstemp(prefix='local-config.json.setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.chmod(name, 0o600)
            json.dump(cfg, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def upstream_key(cfg, *, include_saved=False):
    if not include_saved and cfg.get('MANGROVE_ACCESS_MODE') in {'x402', 'oauth'}:
        return ''
    value = cfg.get('MANGROVE_API_KEY')
    if value is None:
        return ''
    if not isinstance(value, str):
        raise SetupError('MANGROVE_API_KEY must be a string or null.')
    value = value.strip()
    if not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SetupError('MANGROVE_API_KEY contains invalid characters.')
    return '' if value.lower() in {'', 'none', 'null'} or value == PLACEHOLDER else value


def local_key(cfg):
    value = cfg.get('API_KEYS')
    if not isinstance(value, str):
        raise SetupError('API_KEYS must be a comma-separated string.')
    keys = [k.strip() for k in value.split(',') if k.strip()]
    if not keys or any(k in PUBLISHED for k in keys):
        raise SetupError('Local credentials are missing or published defaults; run setup first.')
    if any(not k.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in k) for k in keys):
        raise SetupError('Local credentials contain invalid characters.')
    return keys[0]


def origin(value):
    try:
        u = urllib.parse.urlsplit(value)
        host = '127.0.0.1' if u.hostname == 'localhost' else u.hostname
        if (u.scheme != 'http' or not ipaddress.ip_address(host).is_loopback
                or u.username is not None or u.password is not None
                or u.query or u.fragment or u.path not in {'', '/'}):
            raise ValueError
        port = u.port or 80
        authority = f'[{host}]' if ':' in host else host
        return f'http://{authority}:{port}'
    except (ValueError, TypeError):
        raise SetupError('Agent URL must be an HTTP loopback origin with no path or credentials.') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


UPSTREAM_METRICS_TIMEOUT_SECONDS = 5


def upstream_api_base(cfg):
    """Resolve the same REST base URL precedence as the MangroveAI SDK."""
    key = upstream_key(cfg)
    environment = os.environ.get('MANGROVE_ENVIRONMENT')
    if environment and environment not in {'dev', 'prod', 'local'}:
        raise SetupError('MANGROVE_ENVIRONMENT must be dev, prod or local.')
    if not environment:
        environment = ('dev' if key.startswith('dev_') else
                       'local' if key.startswith('local_') else 'prod')
    value = cfg.get('MANGROVEAI_BASE_URL') or os.environ.get('MANGROVE_BASE_URL')
    if not value:
        if environment == 'dev' or key.startswith('dev_'):
            raise SetupError(
                'Development API key checks require MANGROVEAI_BASE_URL or MANGROVE_BASE_URL, '
                'even when MANGROVE_ENVIRONMENT is overridden; there is no verified '
                'development API default.'
            )
        if key.startswith('test_') and environment == 'prod':
            raise SetupError(
                'Test API keys require MANGROVEAI_BASE_URL, MANGROVE_BASE_URL, '
                'or MANGROVE_ENVIRONMENT=local; production is not used as a fallback.'
            )
        try:
            endpoints = json.loads((ROOT / 'server/src/config/mangrove-endpoints.json').read_text())
            value = endpoints['environments'][environment]['base_url']
        except (OSError, ValueError, KeyError, TypeError):
            raise SetupError('Cannot resolve the configured MangroveAI API destination.') from None

    try:
        parsed = urllib.parse.urlsplit(value.strip())
        host = parsed.hostname
        _ = parsed.port
        loopback_http = (parsed.scheme == 'http' and host in {'localhost', '127.0.0.1', '::1'})
        if (not host or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip('/') != '/api/v1'
                or not (parsed.scheme == 'https' or loopback_http)):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise SetupError('MangroveAI API destination must be HTTPS (or loopback HTTP) and end in /api/v1.') from None
    return value.strip().rstrip('/')


def probe_upstream_key(cfg):
    """Return verified, rejected, forbidden, rate_limited, or unknown.

    This is a single authenticated, non-billable usage-metrics read. It never
    redirects, retries, logs the key, or falls back to an x402 payment.
    """
    key = upstream_key(cfg)
    if not key:
        return 'unknown'
    url = upstream_api_base(cfg) + '/users/me/metrics'
    req = urllib.request.Request(
        url,
        headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=UPSTREAM_METRICS_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return 'unknown'
            body = response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        exc.close()
        return {401: 'rejected', 403: 'forbidden', 429: 'rate_limited'}.get(exc.code, 'unknown')
    except (OSError, ValueError, urllib.error.URLError):
        return 'unknown'
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return 'unknown'
    if (isinstance(payload, dict) and payload.get('success') is True
            and isinstance(payload.get('metrics'), dict)):
        return 'verified'
    return 'unknown'


def request(cfg, path, payload=None, *, authenticated=True, timeout=10):
    url = origin(cfg.get('LOCAL_AGENT_URL', '')) + path
    headers = {'Content-Type': 'application/json'}
    if authenticated:
        headers['X-API-Key'] = local_key(cfg)
    req = urllib.request.Request(url, headers=headers,
                                 data=None if payload is None else json.dumps(payload).encode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    # Do not retry mutations: a lost response is not evidence of a failed operation.
    try:
        with opener.open(req, timeout=timeout) as response:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise SetupError('Agent response exceeded the setup limit.')
            return json.loads(data)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise SetupError(f'Agent returned HTTP {code}; check local authentication and server logs.') from None
    except (OSError, ValueError):
        raise SetupError('No valid response from the local agent. For wallet operations, list wallets before retrying.') from None


def require_auth(cfg):
    if cfg.get('AUTH_ENABLED') is not True:
        raise SetupError('Setup requires AUTH_ENABLED=true to protect local wallet operations.')
    local_key(cfg)


def check(cfg):
    require_auth(cfg)
    wallets = request(cfg, '/api/v1/agent/wallet/list')
    if not isinstance(wallets, list):
        raise SetupError('Agent returned an invalid wallet list.')
    try:
        request(cfg, '/api/v1/agent/wallet/list', authenticated=False)
    except SetupError as exc:
        if not str(exc).startswith(('Agent returned HTTP 401;', 'Agent returned HTTP 403;')):
            raise
    else:
        raise SetupError('Agent accepted unauthenticated wallet access; refusing to report readiness.')
    return wallets


def check_upstream(cfg):
    """Check upstream authentication separately from local liveness/discovery."""
    if cfg.get('MANGROVE_ACCESS_MODE') == 'oauth':
        result = request(cfg, '/api/v1/agent/oauth/status')
        if not result.get('connected'):
            raise SetupError('Mangrove sign-in is required. Run ./scripts/setup.sh --auth oauth.')
        print('OAuth credentials are saved. Live tool access is checked through MCP discovery below.')
        return True
    if cfg.get('MANGROVE_ACCESS_MODE') == 'x402':
        print('Wallet mode selected: no upstream API key to verify.')
        return True
    result = probe_upstream_key(cfg)
    if result == 'rejected':
        raise SetupError(
            'MangroveAI rejected the configured API key (HTTP 401). Mint or check '
            'the key at https://mangrove.io/developer/api-keys, then rerun setup.'
        )
    if result == 'verified':
        print('Upstream API key verified with an authenticated usage-metrics read.')
        return True
    messages = {
        'forbidden': 'MangroveAI denied the metrics read (HTTP 403); key scope or account access is unresolved.',
        'rate_limited': 'MangroveAI rate-limited the metrics read (HTTP 429); key validity was not verified.',
        'unknown': 'MangroveAI key readiness is unknown: the metrics endpoint was unavailable or returned an invalid response.',
    }
    print(messages[result] + ' Local setup remains complete; rerun ./scripts/verify_quickstart.sh later.')
    return False


def check_markets(cfg):
    try:
        result = request(cfg, '/api/v1/agent/marketplace/readiness')
        if isinstance(result, dict):
            status = result.get('marketplace')
            if (isinstance(status, dict) and type(status.get('version')) is int
                    and status['version'] == 1
                    and status.get('product') == 'agent_to_agent_marketplace'
                    and status.get('availability') == 'under_construction'
                    and status.get('message') == 'Agent to agent marketplace currently under construction.'):
                print(status['message'])
        if not isinstance(result, dict) or result.get('ownership_discovery') != 'ready':
            raise SetupError('Invalid ownership readiness response.')
    except SetupError:
        print('Marketplace ownership discovery is unavailable. Check the configured Markets server; '
              'other agent tools remain available.')
        return
    print('Marketplace ownership discovery verified. Wallet backup and network are checked when selected; '
          'no signing or payment was performed.')


def hidden(prompt):
    if not sys.stdin.isatty():
        raise SetupError('Secret entry requires an interactive terminal. No secret was read.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt).strip()


# -- terminal presentation -----------------------------------------------------
# Stdlib only: this module runs on the system interpreter before .venv exists.

# Brand mark sampled from assets/logo.png at 34x18 px; two px per terminal cell
# using half blocks. Letters are the ribbon colours from assets/logo.svg.
_LOGO = (
    '          BBBLL     RRROO         ',
    '         BBBBLL    RRRROO         ',
    '         BBBBLL    RRRROO         ',
    '         BBBBLL    RRRROO         ',
    '        BBBBLLLL  RRRROOOO        ',
    '       BBBBBLLLLLRRRRROOOOO       ',
    '       BBBB  LLLLRRRR  OOOOO      ',
    '      BBBBB   LLLLRRR   OOOO      ',
    '     BBBBB    LLLLLR    OOOOO     ',
    '    BBBBB     RLLLLL     OOOOO    ',
    '   BBBBB     RRRLLLLL     OOOOO   ',
    '  BBBBB     RRRRRLLLLL     OOOOO  ',
    '  BBBB      RRRR  LLLLL     OOOOO ',
    ' BBBB      RRRR    LLLL      OOOO ',
    ' BBBB      RRRR    LLLL      OOOO ',
    'BBBB      RRRR      LLLL      OOOO',
    'BBBB      RRRR      LLLL      OOOO',
    'BBBB      RRRR      LLLL      OOOO',
)
_LOGO_COLORS = {'B': (0x42, 0xa7, 0xc6), 'L': (0x74, 0xc3, 0xd5),
                'R': (0xff, 0x47, 0x13), 'O': (0xff, 0x9e, 0x18)}

# "ANSI Shadow" block letters, limited to the glyphs the wordmark needs.
_GLYPHS = {
    'A': (' █████╗ ', '██╔══██╗', '███████║', '██╔══██║', '██║  ██║', '╚═╝  ╚═╝'),
    'E': ('███████╗', '██╔════╝', '█████╗  ', '██╔══╝  ', '███████╗', '╚══════╝'),
    'G': (' ██████╗ ', '██╔════╝ ', '██║  ███╗', '██║   ██║', '╚██████╔╝', ' ╚═════╝ '),
    'M': ('███╗   ███╗', '████╗ ████║', '██╔████╔██║', '██║╚██╔╝██║', '██║ ╚═╝ ██║', '╚═╝     ╚═╝'),
    'N': ('███╗   ██╗', '████╗  ██║', '██╔██╗ ██║', '██║╚██╗██║', '██║ ╚████║', '╚═╝  ╚═══╝'),
    'O': (' ██████╗ ', '██╔═══██╗', '██║   ██║', '██║   ██║', '╚██████╔╝', ' ╚═════╝ '),
    'R': ('██████╗ ', '██╔══██╗', '██████╔╝', '██╔══██╗', '██║  ██║', '╚═╝  ╚═╝'),
    'T': ('████████╗', '╚══██╔══╝', '   ██║   ', '   ██║   ', '   ██║   ', '   ╚═╝   '),
    'V': ('██╗   ██╗', '██║   ██║', '██║   ██║', '╚██╗ ██╔╝', ' ╚████╔╝ ', '  ╚═══╝  '),
}
# Top-to-bottom shading for solid blocks, then the outline/shadow colour.
_WORD_SHADES = ((0xee,) * 3, (0xd4,) * 3, (0xba,) * 3, (0xa0,) * 3, (0x86,) * 3, (0x6c,) * 3)
_WORD_SHADOW = (0x58,) * 3
_RESET = '\x1b[0m'


def _terminal_columns():
    return shutil.get_terminal_size((80, 24)).columns


def _supports_color(stream):
    return stream.isatty() and not os.environ.get('NO_COLOR') and os.environ.get('TERM') != 'dumb'


def _supports_unicode(stream, sample):
    try:
        sample.encode(getattr(stream, 'encoding', None) or 'ascii')
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _xterm256(rgb):
    levels = (0, 95, 135, 175, 215, 255)
    cube = [min(range(6), key=lambda i: abs(levels[i] - c)) for c in rgb]
    grey = min(range(24), key=lambda i: abs(8 + 10 * i - sum(rgb) / 3))
    candidates = [(16 + 36 * cube[0] + 6 * cube[1] + cube[2], tuple(levels[i] for i in cube)),
                  (232 + grey, (8 + 10 * grey,) * 3)]
    return min(candidates, key=lambda c: sum((a - b) ** 2 for a, b in zip(c[1], rgb)))[0]


def _sgr(rgb, *, background=False):
    base = 48 if background else 38
    if os.environ.get('COLORTERM', '').lower() in {'truecolor', '24bit'}:
        return f'\x1b[{base};2;{rgb[0]};{rgb[1]};{rgb[2]}m'
    return f'\x1b[{base};5;{_xterm256(rgb)}m'


def _logo_lines(color):
    lines = []
    for top_row, bottom_row in zip(_LOGO[::2], _LOGO[1::2]):
        cells, current = [], ''
        for top, bottom in zip(top_row, bottom_row):
            if top == ' ' and bottom == ' ':
                style, char = _RESET, ' '
            elif bottom == ' ':
                style, char = _RESET + _sgr(_LOGO_COLORS[top]), '▀'
            elif top == ' ':
                style, char = _RESET + _sgr(_LOGO_COLORS[bottom]), '▄'
            elif top == bottom or not color:
                style, char = _RESET + _sgr(_LOGO_COLORS[top]), '█'
            else:  # Two ribbons share the cell: top colour in front, bottom behind.
                style, char = _RESET + _sgr(_LOGO_COLORS[top]) + _sgr(_LOGO_COLORS[bottom], background=True), '▀'
            if color and style != current:
                cells.append(style)
                current = style
            cells.append(char)
        line = ''.join(cells).rstrip()
        lines.append(line + _RESET if color else line)
    return lines


def _wordmark(*words):
    rows = [''] * 6
    for number, word in enumerate(words):
        if number:
            rows = [row + '   ' for row in rows]
        for letter in word:
            glyph = _GLYPHS[letter]
            width = max(map(len, glyph))
            rows = [row + part.ljust(width) for row, part in zip(rows, glyph)]
    return [row.rstrip() for row in rows]


def _shade(rows, color):
    if not color:
        return rows
    shaded = []
    for index, row in enumerate(rows):
        shade = _WORD_SHADES[index % len(_WORD_SHADES)]
        out, current = [], None
        for ch in row:
            wanted = None if ch == ' ' else shade if ch == '█' else _WORD_SHADOW
            if wanted != current:
                out.append(_RESET if wanted is None else _sgr(wanted))
                current = wanted
            out.append(ch)
        shaded.append(''.join(out) + _RESET)
    return shaded


def banner(stream=None):
    """Print the setup banner. Decorative only: silent unless writing to a terminal."""
    stream = stream or sys.stdout
    if not stream.isatty():
        return
    width = _terminal_columns()
    color = _supports_color(stream)
    title, subtitle = 'MANGROVE AGENT', 'Local Mangrove-powered trading agent'
    plain_title = [f'\x1b[1m{title}{_RESET}' if color else title]
    if not _supports_unicode(stream, '█▀▄╔╗╚╝║═') or width < len(_LOGO[0]) + 4:
        logo, text, text_width = [], plain_title, len(title)
    else:
        logo = _logo_lines(color) + ['']
        full, top = _wordmark('MANGROVE', 'AGENT'), _wordmark('MANGROVE')
        if width >= len(full[0]) + 4:
            text, text_width = _shade(full, color), len(full[0])
        elif width >= len(top[0]) + 4:
            agent = _wordmark('AGENT')
            indent = ' ' * ((len(top[0]) - len(agent[0])) // 2)
            text, text_width = _shade(top + [indent + row for row in agent], color), len(top[0])
        else:
            text, text_width = plain_title, len(title)
    # Centre every part within the widest one; widths are measured before styling.
    block = max(text_width, len(_LOGO[0]) if logo else 0, len(subtitle))

    def centred(lines, part_width):
        return [' ' * (2 + (block - part_width) // 2) + line if line else '' for line in lines]

    lines = ([''] + centred(logo, len(_LOGO[0])) + centred(text, text_width)
             + centred([f'\x1b[2m{subtitle}{_RESET}' if color else subtitle], len(subtitle)) + [''])
    stream.write('\n'.join(lines) + '\n')
    stream.flush()


def _read_key(fd):
    ch = os.read(fd, 1)
    if not ch or ch == b'\x04':
        raise EOFError
    if ch == b'\x03':
        raise KeyboardInterrupt
    if ch in {b'\r', b'\n'}:
        return 'enter'
    if ch == b'\x1b':
        # Arrow keys arrive as ESC [ A or ESC O A; drain the whole sequence so
        # unrecognised keys (F-keys, modified arrows) are ignored cleanly.
        seq = b''
        while select.select([fd], [], [], 0.05)[0]:
            seq += os.read(fd, 1)
            if seq[:1] not in {b'[', b'O'} or (len(seq) >= 2 and (seq[:1] == b'O' or 0x40 <= seq[-1] <= 0x7e)):
                break
        return {b'[A': 'up', b'OA': 'up', b'[B': 'down', b'OB': 'down'}.get(seq, '')
    return ch.decode('ascii', 'ignore')


def _menu_supported():
    return (termios is not None and sys.stdout.isatty()
            and os.environ.get('TERM', 'dumb') != 'dumb')


def _select(prompt, options, default):
    keys = list(options)
    index = keys.index(default) if default in options else 0
    fd, out = sys.stdin.fileno(), sys.stdout
    color = _supports_color(out)
    if _supports_unicode(out, '◆◇●○│└↑↓•…'):
        active, done, on, off, bar, end, more = '◆', '◇', '●', '○', '│', '└', '…'
        hint = '↑/↓ to navigate • Enter: confirm'
    else:
        active, done, on, off, bar, end, more = '?', '+', '>', '-', '|', '`', '.'
        hint = 'Up/Down to navigate, Enter to confirm'
    room = max(20, _terminal_columns() - 1)

    def paint(code, text):
        return f'\x1b[{code}m{text}{_RESET}' if color else text

    def fit(text, limit):
        # Truncate before styling so no line wraps; wrapping breaks the redraw.
        return text if len(text) <= limit else text[:max(0, limit - 1)] + more

    drawn = 0

    def draw(lines):
        nonlocal drawn
        if drawn:
            out.write(f'\x1b[{drawn}F\x1b[J')
        out.write(''.join(line + '\n' for line in lines))
        out.flush()
        drawn = len(lines)

    def menu():
        lines = [f'{paint("36", active)}  {paint("1", fit(prompt, room - 3))}']
        for position, key in enumerate(keys):
            label = fit(options[key], room - 5)
            if position == index:
                lines.append(f'{paint("2", bar)}  {paint("36", on)} {label}')
            else:
                lines.append(f'{paint("2", bar)}  {paint("2", off)} {paint("2", label)}')
        lines.append(f'{paint("2", end)}  {paint("2", fit(hint, room - 3))}')
        return lines

    saved = termios.tcgetattr(fd)
    typed = ''
    out.write('\x1b[?25l')
    try:
        # TCSADRAIN keeps typeahead (e.g. "2<Enter>" typed early, as with the old prompt).
        tty.setcbreak(fd, termios.TCSADRAIN)
        draw(menu())
        while True:
            key = _read_key(fd)
            if key == 'enter':
                break
            if key in {'up', 'k'}:
                index, typed = (index - 1) % len(keys), ''
            elif key in {'down', 'j'}:
                index, typed = (index + 1) % len(keys), ''
            elif key.isdigit():
                # Number keys still work: they move the highlight, Enter confirms.
                typed = typed + key if any(k.startswith(typed + key) for k in keys) else key
                if typed in options:
                    index = keys.index(typed)
                elif not any(k.startswith(typed) for k in keys):
                    typed = ''
            else:
                continue
            draw(menu())
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write('\x1b[?25h')
        out.flush()
    draw([f'{paint("32", done)}  {fit(prompt, room - 3)}',
          f'{paint("2", bar)}  {paint("2", fit(options[keys[index]], room - 3))}'])
    return keys[index]


def choose(prompt, options, default):
    if not sys.stdin.isatty():
        raise SetupError('Use --yes and explicit options when running without an interactive terminal.')
    if _menu_supported():
        return _select(prompt, options, default)
    print(prompt)
    for key, label in options.items():
        print(f'  {key}. {label}')
    while True:
        value = input(f'Choice [{default}]: ').strip() or default
        if value in options:
            return value
        print('Choose one of the listed options.')


def configure_reconciliation(cfg):
    network(cfg)
    selected = cfg['X402_NETWORK']
    path = ROOT / 'server/src/config/mangrove-endpoints.json'
    defaults = json.loads(path.read_text())['reconciliation_rpc_urls']
    urls = cfg.get('X402_RECONCILIATION_RPC_URLS')
    if urls is None or urls == '':
        urls = {}
    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except ValueError:
            raise SetupError('Reconciliation RPC configuration must be a JSON object.') from None
    if not isinstance(urls, dict):
        raise SetupError('Reconciliation RPC configuration must map networks to URLs.')
    urls = dict(urls)
    for chain, value in urls.items():
        if not isinstance(value, str):
            raise SetupError('Invalid reconciliation RPC URL.')
        parsed = urllib.parse.urlsplit(value)
        if (chain not in defaults or not parsed.hostname or parsed.username or parsed.password or parsed.fragment
                or not (parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}))):
            raise SetupError('Invalid reconciliation RPC URL.')
    urls.setdefault(selected, defaults[selected])
    cfg['X402_RECONCILIATION_RPC_URLS'] = urls


def configure(args):
    exists = CONFIG.exists()
    cfg = read_config(CONFIG if exists else EXAMPLE)
    old = dict(cfg)
    key = upstream_key(cfg, include_saved=True)
    mode = args.auth
    if args.api_key_stdin and mode not in {None, 'api-key'}:
        raise SetupError('--api-key-stdin requires api-key mode.')
    if args.api_key_stdin:
        supplied = sys.stdin.read().strip()
        if not supplied or upstream_key({'MANGROVE_API_KEY': supplied}) != supplied:
            raise SetupError('Provide a nonempty API key, not a placeholder.')
        key, mode = supplied, 'api-key'
    while True:
        # Interactive runs offer sign-in, API-key and wallet access; existing numbering is stable.
        if mode is None:
            if not args.yes:
                mode = {'1': 'api-key', '2': 'x402', '3': 'oauth'}[choose(
                    'How would you like to access MangroveAI?',
                    {'1': 'Use an API key', '2': 'Pay with a wallet (x402; no signup)',
                     '3': 'Sign in to Mangrove in your browser (OAuth)'},
                    '1')]
            else:
                mode = cfg.get('MANGROVE_ACCESS_MODE') or ('api-key' if key else 'x402')
        if mode == 'api-key' and not key and not args.yes and not args.api_key_stdin:
            choice = choose('No MangroveAI API key is saved in this checkout.', {
                '1': 'Enter a new API key',
                '2': 'Choose another access method'}, '1')
            if choice == '2':
                mode = None
                continue
        break
    if mode == 'api-key' and key and not args.yes and not args.api_key_stdin:
        choice = choose(
            'An upstream API key is already configured. Which key should setup use?',
            {'1': 'Use the existing API key', '2': 'Enter a new API key'},
            '1',
        )
        if choice == '2':
            # Clear only the in-memory candidate. Cancellation or invalid input
            # must leave the saved configuration untouched.
            key = None
    if mode == 'api-key' and not key:
        if args.yes:
            raise SetupError('API-key mode needs a configured key; run interactively with --auth api-key.')
        key = hidden('MangroveAI API key (hidden): ')
        if not key or not upstream_key({'MANGROVE_API_KEY': key}):
            raise SetupError('A real API key is required for API-key mode.')
    cfg['MANGROVE_API_KEY'] = key or ''
    cfg['MANGROVE_ACCESS_MODE'] = mode
    if mode == 'x402':
        if not args.yes and cfg.get('X402_NETWORK') != 'eip155:8453':
            selection = choose('Payment network (mainnet payments use real USDC):', {
                '1': 'Base Mainnet — USDC on Base',
                '2': 'Keep the currently configured network'}, '1')
            if selection == '1':
                cfg['X402_NETWORK'] = 'eip155:8453'
                cfg['X402_USDC_CONTRACT'] = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
        configure_reconciliation(cfg)
    raw = cfg.get('API_KEYS', '')
    if raw is None:
        raw = ''
    if not isinstance(raw, str):
        raise SetupError('API_KEYS must be a comma-separated string.')
    kept = [k.strip() for k in raw.split(',') if k.strip() and k.strip() not in PUBLISHED]
    cfg['API_KEYS'] = ','.join(kept) if kept else secrets.token_urlsafe(32)
    require_auth(cfg)
    cfg['LOCAL_AGENT_URL'] = origin(args.url)
    if args.markets_url:
        parsed = urllib.parse.urlsplit(args.markets_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise SetupError('Markets URL must be HTTP(S) without credentials.')
        cfg['MANGROVEMARKETS_BASE_URL'] = args.markets_url
    elif cfg.get('MANGROVEMARKETS_BASE_URL') == 'http://localhost:9081':
        cfg['MANGROVEMARKETS_BASE_URL'] = 'https://mangrovemarkets-pcqgpciucq-uc.a.run.app'
    if not exists or cfg != old:
        atomic_config(cfg)
    else:
        CONFIG.chmod(0o600)
    print(f'Access mode: {mode}. Local authentication remains enabled.')
    if mode == 'api-key':
        print('Upstream API-key validity is checked by the verify step below. Failed keys never trigger wallet payments.')
    elif mode == 'oauth':
        print('Browser sign-in follows after local startup. API keys and wallet payments will not be used.')
        print('OAuth tool access follows your approved permissions and current account role.')
    else:
        print(f'Payment network: {network(cfg)[2]}. Existing network selections are preserved on reruns.')
        print('Automatic payment reconciliation configured for the selected network; RPC availability is checked during recovery.')
        print('Interactive wallet setup follows after local startup. Wallet actions require confirmation; no payment is made.')


def network(cfg):
    try:
        return NETWORKS[cfg['X402_NETWORK']]
    except (KeyError, TypeError):
        raise SetupError('Choose a supported X402_NETWORK in configuration before wallet onboarding.') from None


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', value) or int(value[2:], 16) == 0:
        raise SetupError('Agent returned an invalid wallet address.')
    return value


def guide(cfg, *, yes=False, docker=False):
    if cfg.get('MANGROVE_ACCESS_MODE') == 'oauth':
        print('OAuth setup complete. No separate sign-in script is required.')
        return
    if upstream_key(cfg):
        print('MangroveAI calls use your API key. Start ./scripts/chat.sh when ready.')
        return
    chain, _, label = network(cfg)
    suffix = ' --docker' if docker else ''
    cmd = './scripts/setup.sh'
    selected = '4' if yes else choose('Payment wallet instructions (nothing is executed):', {
        '1': 'Create a new wallet', '2': 'Import an existing wallet',
        '3': 'Use a wallet already saved in this agent', '4': 'Finish wallet setup later'}, '1')
    print(f'Configured payment network: {label} (chain {chain}).')
    print('Payment readiness is not verified. No funding or paid request was checked.')
    if selected == '4':
        print('Step 1. Resume later - rerun setup when you are ready for wallet instructions.')
        print(f'  {cmd}{suffix}')
    else:
        steps = []
        if selected == '1':
            steps.append(('Create your wallet - creates an encrypted local wallet and prints its public address.',
                          f'{cmd} --wallet create'))
        elif selected == '2':
            steps.append(('Import your wallet - securely saves your existing wallet in this agent.',
                          f'{cmd} --wallet import'))
        else:
            steps.append(('List saved wallets - shows addresses on this network and their backup status.',
                          f'{cmd} --wallet list'))
        if selected != '2':
            steps.extend([
                ('Save a backup - reveals the secret in your terminal so you can save it securely; skip if already backed up.',
                 './scripts/reveal-secret.sh --address WALLET_ADDRESS'),
                ('Confirm your backup - records that you have saved the secret outside this agent; skip if already confirmed.',
                 './scripts/confirm-backup.sh WALLET_ADDRESS'),
            ])
        steps.extend([
            ('Select the payer - choose the saved wallet and review its spending limit.', f'{cmd} --wallet select'),
            ('Apply the settings - restarts the agent if needed and verifies local access.', f'{cmd} --yes{suffix}'),
            (f'Fund the selected address - send USDC on {label} only after checking the service accepts this network.', None),
            ('Open restricted chat; start with your wallet list and spending status.', './scripts/chat.sh'),
        ])
        print('\nRun each command yourself in this checkout, in order:')
        for number, (description, command) in enumerate(steps, 1):
            print(f'\nStep {number}. {description}')
            if command:
                print(f'  {command}')
            if selected == '2' and number == 1:
                print('  Type IMPORT, then BACKED UP if you have saved the secret safely.')
                print('  Enter your private key or recovery phrase only at the hidden terminal prompt.')
                print('  No address entry is needed: the agent derives and displays it. Check it matches your wallet.')
        if selected != '2':
            print('\nReplace WALLET_ADDRESS with the public address from the wallet command.')
    print('\nThese setup commands do not transfer funds or make a paid test call.')
    print('Base Sepolia uses test USDC; it cannot pay for a Base mainnet service.')
    print('Once configured, paid tool calls can spend automatically within the cap.')
    print('Paper trades are simulated, but their upstream data/backtests may still cost money.')
    print('See docs/setup-x402.md for the complete steps and recovery instructions.')


@contextmanager
def flow_busy(label):
    """Transient progress in a terminal; never animate redirected output."""
    if not _menu_supported():
        yield
        return
    out = sys.stdout
    frames = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' if _supports_unicode(out, '⠋') else '|/-\\'
    stopped = threading.Event()

    def animate():
        index = 0
        while not stopped.is_set():
            text = f'  {frames[index % len(frames)]} {label}'
            if _supports_color(out):
                text = '\x1b[36m' + text + _RESET
            out.write('\r\x1b[2K' + text)
            out.flush()
            index += 1
            stopped.wait(0.1)

    worker = threading.Thread(target=animate, daemon=True)
    out.write('\x1b[?25l')
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
        out.write('\r\x1b[2K\x1b[?25h')
        out.flush()


def flow_text(text, *, tone=None):
    """Small, readable blocks; ANSI is optional and plain terminals stay usable."""
    width = max(24, min(76, _terminal_columns() - 4))
    for line in textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False) or ['']:
        rendered = f'\x1b[{tone}m{line}{_RESET}' if tone and _supports_color(sys.stdout) else line
        print('  ' + rendered)


def flow_step(number, title, note=None):
    print()
    flow_text(f'WALLET SETUP  {number} / 4', tone='2')
    flow_text(title, tone='1;36')
    if note:
        print()
        flow_text(note)
    print()


def flow_success(text):
    mark = '✓' if _supports_unicode(sys.stdout, '✓') else '+'
    flow_text(f'{mark} {text}', tone='32')


def payment_network(cfg):
    return 'Base Mainnet' if network(cfg)[0] == 8453 else 'Base Sepolia'


def onboarding_path():
    return ROOT / 'agent-data/wallet-setup.json'


def save_onboarding(state):
    # Public addresses and workflow state only: never secrets, tokens or credentials.
    atomic_config(state, path=onboarding_path())


def saved_wallets(cfg):
    wallets = check(cfg)
    result = []
    for item in wallets:
        if not isinstance(item, dict):
            raise SetupError('Invalid saved wallet response.')
        if item.get('chain') == 'evm':
            address(item.get('address'))
            result.append(item)
    return result


def choose_saved_wallet(wallets, *, payer=None):
    if not wallets:
        raise SetupError('No saved EVM wallet is available yet.')
    choices, addresses = {}, {}
    default = '1'
    for i, wallet in enumerate(wallets, 1):
        key = str(i)
        addr = address(wallet['address'])
        addresses[key] = addr
        status = 'Backed up' if wallet.get('backup_confirmed_at') else 'Backup needed'
        if addr.lower() == str(payer).lower():
            status = 'Current · ' + status
            default = key
        # Status comes first so it remains visible in narrow terminals. The
        # selected wallet's full address is shown again before payer approval.
        choices[key] = f'{status} · {addr}'
    choices['l'] = 'Finish later'
    print()
    choice = choose('Choose a saved wallet:', choices, default)
    return None if choice == 'l' else addresses[choice]


def wait_for_backup(cfg, addr):
    shown = False
    while True:
        with flow_busy('Checking backup…'):
            wallet = next((w for w in saved_wallets(cfg) if w['address'].lower() == addr.lower()), None)
        if wallet is None:
            raise SetupError('Wallet is no longer available. No payer was selected.')
        if wallet.get('backup_confirmed_at'):
            flow_success('Backup confirmed')
            return True
        if not shown:
            flow_step(2, 'Save your backup', 'Keep this window open. In a second private terminal:')
            flow_text('1. Open this checkout', tone='2')
            print('     cd ' + shlex.quote(str(ROOT)))
            print()
            flow_text('2. Reveal your secret and save it somewhere safe', tone='2')
            print('     ./scripts/reveal-secret.sh --setup')
            print()
            flow_text('3. Confirm only after you have saved it', tone='2')
            print('     ./scripts/confirm-backup.sh --setup')
            print()
            shown = True
        else:
            print()
            flow_text('Backup not confirmed yet. Complete step 3 in your other terminal.', tone='33')
            print()
        reply = choose('Ready to continue?', {
            '1': 'Check backup and continue', '2': 'Finish later'}, '1')
        if reply == '2':
            return False


def onboard(cfg, *, yes=False):
    # Return to the menu without recursive stack growth on invalid confirmation.
    while _onboard(cfg, yes=yes) == 'menu':
        pass


def _onboard(cfg, *, yes=False):
    """One interactive session; read-only checks may retry, wallet mutations never do."""
    if cfg.get('MANGROVE_ACCESS_MODE') == 'oauth' or upstream_key(cfg):
        return
    if yes:
        print('Wallet onboarding deferred (--yes). No wallet action or payment was made.')
        return
    if not sys.stdin.isatty():
        raise SetupError('Wallet setup requires an interactive terminal; use --yes to defer it.')
    print()
    flow_text('PAYMENT WALLET', tone='1;36')
    flow_text(f'{payment_network(cfg)} · USDC', tone='2')
    print()
    path = onboarding_path()
    state = read_config(path) if path.exists() or path.is_symlink() else None
    if state is not None:
        if (state.get('stage') not in {'pending', 'wallet', 'selected'}
                or state.get('action') not in {'create', 'import', 'existing'}
                or state.get('network') not in NETWORKS):
            raise SetupError('Invalid wallet setup checkpoint; inspect agent-data/wallet-setup.json.')
        if state.get('address') is not None:
            address(state['address'])
        if state['stage'] == 'wallet':
            flow_text('Unfinished wallet setup', tone='1')
            flow_text(address(state.get('address')), tone='2')
            flow_text('Choosing another option keeps this wallet saved locally.', tone='2')
            print()
            choice = choose('How would you like to continue?', {
                '1': 'Resume this wallet',
                '2': 'Choose a different setup option',
                '3': 'Finish later'}, '1')
            if choice == '3':
                return
            if choice == '2':
                # Only dismiss a confirmed wallet checkpoint. An uncertain
                # mutation must still go through recovery, never be forgotten.
                path.unlink()
                return 'menu'
        if state['network'] != cfg['X402_NETWORK']:
            choice = choose('Wallet setup was started on another network.', {
                '1': 'Review that saved wallet on the currently configured network',
                '2': 'Finish wallet setup later'}, '2')
            if choice == '2':
                return
            state['network'] = cfg['X402_NETWORK']
            state['stage'] = 'wallet' if state.get('address') else 'pending'
            save_onboarding(state)
    wallets = saved_wallets(cfg)
    if state is None:
        payer = cfg.get('X402_PAYER_WALLET')
        active = next((w for w in wallets if w['address'].lower() == str(payer).lower()), None)
        options = {'1': 'Create a new wallet', '2': 'Import an existing wallet',
                   '3': 'Use a wallet already saved in this agent', '4': 'Finish wallet setup later'}
        choice = choose('Set up your payment wallet:', options, '1')
        if choice == '4':
            print('Local setup continues. Wallet configuration is incomplete.')
            return
        action = {'1': 'create', '2': 'import', '3': 'existing'}[choice]
        state = {'stage': 'wallet', 'action': action, 'network': cfg['X402_NETWORK']}
        if action == 'existing':
            if not wallets:
                print('No saved EVM wallet is available. Choose create, import, or finish later.')
                return 'menu'
            addr = choose_saved_wallet(wallets, payer=payer)
            if addr is None:
                return
            if active and active['address'].lower() == addr.lower() and active.get('backup_confirmed_at'):
                flow_success('Keeping your current wallet and spending settings')
                flow_text(addr, tone='2')
                return
        else:
            flow_step(1, 'Create your wallet' if action == 'create' else 'Import your wallet',
                      f'{payment_network(cfg)} · Encrypted on this computer. No payment now.')
            def pending():
                state['stage'] = 'pending'
                save_onboarding(state)
            try:
                addr = wallet_action(action, cfg, guided=True, before_mutation=pending)
            except SetupError as exc:
                print(str(exc))
                if state['stage'] != 'pending':
                    print('No wallet mutation was sent. Returning to the wallet menu.')
                    return 'menu'
                # Remain in the session, but require explicit identification of
                # the wallet. Never infer which wallet a timed-out import meant.
                print('The result is uncertain. No automatic create/import retry will be made.')
                return 'menu'
        state.update(stage='wallet', address=addr)
        save_onboarding(state)
    else:
        print('Resuming unfinished wallet setup; no wallet will be created or imported again.')
        if state['stage'] == 'pending':
            print('The previous operation did not return a confirmed result. Review saved wallets.')
            while True:
                choice = choose('Recover the existing operation:', {
                    '1': 'Refresh saved wallets and choose the intended address',
                    '2': 'Finish wallet setup later'}, '1')
                if choice == '2':
                    return
                wallets = saved_wallets(cfg)
                if not wallets:
                    print('No saved wallet found yet. The operation may still be completing.')
                    continue
                addr = choose_saved_wallet(wallets)
                if addr is None:
                    return
                state.update(stage='wallet', address=addr)
                save_onboarding(state)
                break
        else:
            addr = address(state.get('address'))
    print()
    flow_text('Wallet address', tone='2')
    flow_text(addr)
    print()
    if (state['stage'] == 'selected' and cfg.get('X402_PAYER_WALLET') == addr):
        print('Payment settings already confirmed. Continuing application and verification.')
        return
    if state['action'] == 'import':
        choice = choose('Does this address match the wallet you intended to import?', {
            '1': 'Yes, continue', '2': 'No, leave it saved but do not select it as payer'}, '1')
        if choice == '2':
            path.unlink()
            print('Wallet remains saved locally. No new payer was selected. Returning to wallet options.')
            return 'menu'
    while True:
        try:
            if not wait_for_backup(cfg, addr):
                print('Progress saved. Next time, you can resume or choose another setup option.')
                return
            flow_step(3, 'Review payment settings',
                      'Paid calls use real USDC within your spending cap.' if network(cfg)[0] == 8453
                      else 'Paid calls use test USDC within your spending cap.')
            wallet_action('select', cfg, guided=True, selected_address=addr)
            state['stage'] = 'selected'
            save_onboarding(state)
            flow_step(4, 'Applying your settings', 'Setup will restart the agent if needed.')
            return
        except SetupError as exc:
            print(str(exc))
            choice = choose('Wallet is saved. Continue from this step?', {
                '1': 'Retry backup/settings verification', '2': 'Finish wallet setup later'}, '1')
            if choice == '2':
                return


def finish_onboarding(cfg):
    path = onboarding_path()
    if cfg.get('MANGROVE_ACCESS_MODE') != 'x402' or not path.exists():
        return
    state = read_config(path)
    if (state.get('stage') == 'selected'
            and state.get('address') == cfg.get('X402_PAYER_WALLET')
            and state.get('network') == cfg.get('X402_NETWORK')):
        # Called only after the shell has applied config and verified local access.
        path.unlink()
        print()
        flow_success('Wallet configuration complete.')
        flow_text('Fund with USDC on ' + payment_network(cfg) + ' when ready.', tone='2')
        flow_text('Funding and paid access have not been verified.', tone='2')
        print()


def wallet_action(action, cfg, *, guided=False, selected_address=None, before_mutation=None):
    if not sys.stdin.isatty():
        raise SetupError('Run wallet commands yourself in an interactive terminal.')
    if upstream_key(cfg):
        raise SetupError('These payment-wallet commands require x402 mode; run setup --auth x402 first.')
    wallets = check(cfg)
    chain, net, label = network(cfg)
    if not guided:
        print(f'Payment network: {label} (chain {chain}).')
        print('Saved EVM wallets can be selected on either Base network; balances are network-specific.')
    if action in {'list', 'select'}:
        eligible = []
        for item in wallets:
            addr = address(item.get('address'))
            if item.get('chain') == 'evm':
                eligible.append(item)
                if selected_address is None:
                    print(f'{len(eligible)}. {addr} | backup {"confirmed" if item.get("backup_confirmed_at") else "needed"}')
        if action == 'list':
            if not eligible:
                print('No saved wallet matches this payment network.')
            return
        if not eligible:
            raise SetupError('No matching wallet. Create/import one using the printed instructions first.')
        if selected_address is not None:
            item = next((w for w in eligible if address(w['address']).lower() == selected_address.lower()), None)
            if item is None:
                raise SetupError('The selected wallet is no longer available. No settings were saved.')
        else:
            choice = choose('Select a payment wallet:', {str(i): address(w['address']) for i, w in enumerate(eligible, 1)}, '1')
            item = eligible[int(choice) - 1]
        addr = address(item['address'])
        if not item.get('backup_confirmed_at'):
            raise SetupError('Back up this wallet and run confirm-backup.sh with its address before selecting it.')
        budget = request(cfg, '/api/v1/agent/x402/spend')
        try:
            active_cap = Decimal(str(budget['cap_usd']))
            source = budget['cap_source']
            if not active_cap.is_finite() or active_cap < 0 or source not in {'authorized', 'config', 'default'}:
                raise ValueError
        except (KeyError, InvalidOperation, ValueError, TypeError):
            raise SetupError('Cannot verify the active spending cap; no configuration was changed.') from None
        if guided:
            flow_text('Network   ' + payment_network(cfg))
            flow_text('Wallet    ' + addr)
            flow_text(f'Cap       ${active_cap:g} USDC')
            print()
        else:
            print(f'Current effective cap: {active_cap} USD ({source}).')
        if budget.get('exhausted'):
            print('The budget is exhausted. Setup will not reset or unlock it.')
        raw = input(f'Spending cap in USD [{active_cap}]: ').strip()
        try:
            cap = Decimal(raw or str(active_cap))
            if not cap.is_finite() or cap <= 0 or cap > Decimal('1000000') or cap != cap.quantize(Decimal('.000001')):
                raise ValueError
        except (InvalidOperation, ValueError):
            raise SetupError('Use a positive cap up to 1000000 USD with at most six decimal places.') from None
        if source == 'authorized' and cap != active_cap:
            raise SetupError('An authorized period cap overrides config. Use the explicit budget-management workflow '
                             'to change it, or rerun selection keeping the displayed active cap. Nothing was saved.')
        if guided:
            print()
            flow_text(f'Use this wallet with a ${cap:g} USDC cap?')
            flow_text('Existing spending and budget authorizations stay unchanged.', tone='2')
            print()
        else:
            print(f'Payer: {addr}\nNetwork: {label}\nConfigured cap: {cap} USD. Existing spend and latches are preserved.')
            print('A previously authorized period cap takes precedence over this config value.\n'
                  'Check x402_spend_status with your AI client before paid use; setup never resets the budget.')
        prompt = '  Type SELECT to confirm: ' if guided else 'Type SELECT to save this payment configuration: '
        if input(prompt).strip() != 'SELECT':
            raise SetupError('Selection cancelled; configuration unchanged.')
        cfg['X402_PAYER_WALLET'] = addr
        cfg['X402_SPEND_CAP_USD'] = float(cap)
        atomic_config(cfg)
        if guided:
            flow_success('Payment settings saved')
        else:
            print('Saved. Run ./scripts/setup.sh --yes (add --docker for Docker) to apply it.')
        if not guided:
            print(f'Funding address: {addr}\nAsset/network: USDC on {label}. Funding and receiver compatibility are not verified.')
        return
    if not guided:
        print('This imports or creates a wallet only when you confirm below. No payment is made.')
        print('Step 1. Confirm the wallet operation - type the word shown to continue, or Ctrl+C to cancel.')
    prompt = f'  Type {action.upper()} to continue: ' if guided else f'Type {action.upper()} to {action} a payment wallet: '
    if input(prompt).strip() != action.upper():
        raise SetupError('Cancelled; no wallet request was sent.')
    payload = {'chain': 'evm', 'network': net, 'chain_id': chain, 'label': 'x402 payments'}
    if action == 'import':
        if guided:
            print()
            flow_text('You need a backup saved outside this agent.')
        else:
            print('Step 2. Confirm your backup - continue only if the secret is safely saved outside this agent.')
        if input('Type BACKED UP to confirm you have saved its secret outside the agent: ').strip() != 'BACKED UP':
            raise SetupError('Import cancelled; backup was not confirmed.')
        if guided:
            print()
            flow_text('Paste your secret below. Input is hidden; never paste it into chat.')
            print()
        else:
            print('Step 3. Enter the wallet secret - paste it at the hidden prompt below, in this terminal only.')
            print('You do not enter an address; the agent derives it from the secret and displays it after import.')
        secret = hidden('Private key or recovery phrase (hidden; never paste into chat): ')
        if not secret:
            raise SetupError('Empty secret; nothing was sent.')
        try:
            result = request(cfg, '/api/v1/agent/wallet/stash-secret', {'secret': secret})
        finally:
            del secret
        if not isinstance(result, dict):
            raise SetupError('Invalid vault response; no wallet import was sent.')
        token = result.get('vault_token')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', token):
            raise SetupError('Invalid vault handoff; nothing was imported.')
        payload['vault_token'] = token
    # Persist only public recovery metadata before sending a mutation. An uncertain
    # response must never result in an automatic second create/import request.
    if before_mutation is not None:
        before_mutation()
    if guided:
        with flow_busy('Creating wallet…' if action == 'create' else 'Importing wallet…'):
            result = request(cfg, '/api/v1/agent/wallet/' + action, payload)
    else:
        result = request(cfg, '/api/v1/agent/wallet/' + action, payload)
    if not isinstance(result, dict):
        raise SetupError('Invalid wallet result. Inspect saved wallets before retrying.')
    addr = address(result.get('address'))
    # Never print arbitrary server responses, vault tokens or reveal commands.
    if guided:
        print()
        flow_success('Wallet created' if action == 'create' else 'Wallet imported')
        return addr
    print(f'Wallet {action} completed. Public address: {addr}')
    if action == 'import':
        print('Step 4. Check the address above - it should match the wallet you intended to import.')
        if result.get('backup_required'):
            print('This saved wallet still needs backup confirmation. After saving its secret securely:')
            print(f'  ./scripts/confirm-backup.sh {addr}')
    if action == 'create':
        print('Save your backup yourself in a private terminal:')
        print(f'  ./scripts/reveal-secret.sh --address {addr}')
        print('Only after saving it:')
        print(f'  ./scripts/confirm-backup.sh {addr}')
    print('Next: select this wallet as payer and review its spending limit:')
    print('  ./scripts/setup.sh --wallet select')
    print('No payment was made.')


def chat_settings() -> dict:
    for asset in ('scripts/wallet_secret_guard.py', 'server/src/shared/secret_guard.py',
                  '.claude/hooks/bip39-english.txt'):
        if not (ROOT / asset).is_file():
            raise SetupError('Required secret detector assets are missing; repair the installation.')
    def hook(name: str, *args: str) -> dict:
        path = ROOT / '.claude/hooks' / name
        if not path.is_file():
            raise SetupError('Required chat hook is missing; repair the installation.')
        return {'type': 'command', 'command': shlex.join(['bash', str(path), *args]), 'timeout': 5}

    return {
        'autoMemoryEnabled': False,
        'permissions': {
            'allow': ['mcp__mangrove-agent__*'],
            'deny': ['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'NotebookEdit',
                     'Agent', 'Task', 'Skill', 'WebFetch', 'WebSearch', 'LSP'],
            'disableBypassPermissionsMode': 'disable',
        },
        'hooks': {
            'UserPromptSubmit': [{'hooks': [hook('block-wallet-secrets.sh', '--mode', 'user')]}],
            'PostToolUse': [{'hooks': [hook('block-wallet-secrets.sh', '--mode', 'tool')]}],
            'PostToolUseFailure': [{'hooks': [hook('block-wallet-secrets.sh', '--mode', 'failure')]}],
            'PreToolUse': [{'hooks': [hook('block-wallet-secrets.sh', '--mode', 'pre')]}, {
                'matcher': 'mcp__mangrove-agent__execute_swap',
                'hooks': [hook('preflight-swap.sh')],
            }],
        },
    }


def chat_command(claude: str, cfg: dict, settings_path: Path, context_path: Path) -> list[str]:
    require_auth(cfg)
    connection = {
        'type': 'http', 'url': origin(cfg.get('LOCAL_AGENT_URL', '')) + '/mcp/',
        'headersHelper': shlex.join([sys.executable, str(ROOT / 'scripts/setup_support.py'), 'headers']),
    }
    args = [
        claude, '--tools', '', '--strict-mcp-config',
        '--mcp-config', json.dumps({'mcpServers': {'mangrove-agent': connection}}),
        '--setting-sources', '', '--settings', str(settings_path),
        '--permission-mode', 'default', '--no-chrome',
        '--append-system-prompt-file', str(context_path),
    ]
    return args


def launch_chat(cfg: dict) -> int:
    claude = shutil.which('claude')
    if claude is None:
        raise SetupError('Install Claude Code before starting chat.')
    profile = chat_settings()
    require_auth(cfg)
    try:
        from agent_context import render_context
        context = render_context(ROOT)
    except (ImportError, OSError, UnicodeError):
        raise SetupError('Required agent instructions are missing; repair the installation.') from None
    workspace = ROOT / 'agent-data/chat'
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = dict(os.environ, ENABLE_TOOL_SEARCH='false',
               LOCAL_AGENT_URL=origin(cfg.get('LOCAL_AGENT_URL', '')))
    with tempfile.TemporaryDirectory(prefix='mangrove-chat-') as directory:
        path = Path(directory) / 'settings.json'
        path.write_text(json.dumps(profile))
        os.chmod(path, 0o600)
        context_path = Path(directory) / 'sage-instructions.md'
        context_path.write_text(context)
        os.chmod(context_path, 0o600)
        return subprocess.run(chat_command(claude, cfg, path, context_path),
                              cwd=workspace, env=env, check=False).returncode


def verify_oauth_connection(cfg):
    catalog = request(cfg, '/api/v1/agent/tools', timeout=40)
    if catalog.get('mangroveai_status') != 'available':
        raise SetupError('Mangrove OAuth connection verification failed. Setup is incomplete. '
                         'Run ./scripts/setup.sh --auth oauth --oauth login to reconnect. '
                         'No API-key or wallet fallback was attempted.')


def oauth_command(cfg, command, scopes, *, yes=False):
    require_auth(cfg)
    if command == 'oauth-setup' and cfg.get('MANGROVE_ACCESS_MODE') != 'oauth':
        return
    if command == 'oauth-disconnect':
        result = request(cfg, '/api/v1/agent/oauth/disconnect', {}, timeout=40)
        print('Disconnected locally. ' + ('Remote grant revoked.' if result.get('revoked') else
              result.get('message') or 'Remote revocation could not be confirmed; rerun this command when the original issuer is available.'))
        return
    current = request(cfg, '/api/v1/agent/oauth/status')
    if command == 'oauth-status':
        print('Connected.' if current.get('connected') else 'Sign-in required.')
        if current.get('action'):
            print(current['action'])
        elif not current.get('connected'):
            print('Run ./scripts/setup.sh --auth oauth --oauth login to reconnect.')
        return
    if command == 'oauth-setup' and current.get('connected'):
        verify_oauth_connection(cfg)
        print('Already signed in to Mangrove. Using your saved OAuth connection.')
        return
    if cfg.get('MANGROVE_ACCESS_MODE') != 'oauth':
        raise SetupError('Select --auth oauth in setup before signing in.')
    payload = {}
    if scopes:
        wanted = set(current.get('scopes', [])) | set(scopes) | {'offline_access'}
        payload['scopes'] = ' '.join(sorted(wanted))
    if current.get('pending'):
        request(cfg, '/api/v1/agent/oauth/cancel', {})
    result = request(cfg, '/api/v1/agent/oauth/start', payload, timeout=20)
    url = result.get('authorization_url', '')
    expected = (cfg.get('MANGROVE_OAUTH_ISSUER') or 'https://api.mangrovedeveloper.ai').rstrip('/')
    parsed = urllib.parse.urlsplit(url)
    if url.split('?', 1)[0] != expected + '/oauth/authorize' or parsed.fragment:
        raise SetupError('Invalid authorization destination returned by the local agent.')
    print('Approve the requested access in your browser. Sign-in never authorizes wallet spending.')
    print('If the browser does not open, open this URL on this computer:')
    print(url)
    webbrowser.open(url)
    deadline = time.monotonic() + 600
    try:
        while time.monotonic() < deadline:
            state = request(cfg, '/api/v1/agent/oauth/status', timeout=40)
            if not state.get('pending'):
                if state.get('connected') and state.get('expires_at') != current.get('expires_at'):
                    verify_oauth_connection(cfg)
                    print('Connected to Mangrove. No API key was needed.')
                    if state.get('revocation_pending'):
                        print('The previous grant could not be revoked. Run ./scripts/setup.sh --oauth disconnect when the original issuer is available and follow its cleanup instructions. This also disconnects the new grant; sign in again after cleanup. Portal revocation alone does not clear the local cleanup receipt.')
                    return
                raise SetupError('Sign-in did not complete. Your prior connection was preserved; rerun ./scripts/setup.sh --auth oauth --oauth login.')
            time.sleep(1)
        raise SetupError('Sign-in timed out. Rerun ./scripts/setup.sh --auth oauth --oauth login.')
    finally:
        try:
            request(cfg, '/api/v1/agent/oauth/cancel', {})
        except SetupError:
            pass


def robinhood_setup(cfg, *, action=None, yes=False, docker=False):
    later = './scripts/setup.sh --robinhood connect'
    if docker:
        if action == 'connect':
            raise SetupError('Robinhood browser connection is currently supported by bare-metal setup only.')
        flow_text('Robinhood: connection deferred. Docker browser login needs container callback routing; '
                  'the host connector cannot configure Docker credentials.')
        return
    if action == 'skip' or (action is None and (yes or not sys.stdin.isatty())):
        flow_text(f'Robinhood: skipped; existing connection unchanged. Connect later: {later}')
        return
    if yes or not sys.stdin.isatty():
        raise SetupError('Run --robinhood connect in an interactive terminal without --yes.')
    flow_text('Robinhood is optional and separate from Mangrove sign-in. '
              'Connecting does not activate a strategy or place orders.')
    if cfg.get('MANGROVE_ACCESS_MODE') == 'oauth':
        flow_text('Saved-strategy deployment currently requires Mangrove API-key or x402 access. '
                  'Connecting Robinhood does not change your Mangrove access mode.')
    if action != 'connect' and choose('Connect Robinhood?', {
            '1': 'Connect now / check saved connection', '2': 'Skip for now'}, '1') == '2':
        flow_text(f'Robinhood: skipped; existing connection unchanged. Connect later: {later}')
        return
    while True:
        # Use the running agent for verification so saved grants are read and
        # refreshed with its own encryption key and existing broker lock.
        try:
            with flow_busy('Checking Robinhood connection'):
                ready = request(cfg, '/api/v1/agent/brokers/robinhood/status', timeout=60)
            if isinstance(ready, dict) and ready.get('connected') is True:
                flow_success('Robinhood: connected and verified. No strategy activated or order placed.')
                return
        except SetupError:
            pass
        try:
            result = subprocess.run(
                [str(ROOT / '.venv/bin/python3'), str(ROOT / 'scripts/robinhood_connect.py')],
                cwd=ROOT, env=dict(os.environ, ENVIRONMENT='local',
                                   PYTHONPATH=str(ROOT / 'server') +
                                   (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')),
                check=False, timeout=360,
            )
            if result.returncode == 2:
                flow_text(f'Robinhood: skipped; any saved grant is retained. Connect later: {later}')
                return
            if result.returncode == 0:
                with flow_busy('Verifying Robinhood access'):
                    ready = request(cfg, '/api/v1/agent/brokers/robinhood/status', timeout=60)
                if isinstance(ready, dict) and ready.get('connected') is True:
                    flow_success('Robinhood: connected and verified. No strategy activated or order placed.')
                    return
        except (SetupError, OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
            # Do not expose upstream error bodies or authorization credentials.
            pass
        flow_text('Robinhood connection was not verified. Setup can continue without it.')
        if choose('Robinhood connection', {'1': 'Retry connection', '2': 'Skip for now'}, '2') == '2':
            flow_text(f'Robinhood: not verified; any saved grant is retained. Try later: {later}')
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['banner', 'configure', 'check', 'markets-check', 'verify', 'guide', 'onboard', 'onboard-finish', 'wallet', 'fingerprint', 'headers', 'register', 'chat', 'oauth-login', 'oauth-status', 'oauth-disconnect', 'oauth-setup', 'robinhood-setup'])
    parser.add_argument('--auth', choices=['api-key', 'x402', 'oauth'])
    parser.add_argument('--scope', action='append', choices=['strategies:read', 'strategies:write', 'strategies:archive',
                                                          'strategies:evaluate', 'backtests:read', 'backtests:run',
                                                          'sweeps:read', 'sweeps:run', 'signals:read',
                                                          'signals:evaluate', 'positions:read', 'data:read',
                                                          'offline_access'], default=[])
    parser.add_argument('--api-key-stdin', action='store_true')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--docker', action='store_true')
    parser.add_argument('--robinhood', choices=['connect', 'skip'])
    parser.add_argument('--url', default='http://127.0.0.1:9080')
    parser.add_argument('--markets-url')
    parser.add_argument('--action', choices=['create', 'import', 'list', 'select'])
    args = parser.parse_args()
    try:
        if args.command == 'banner':
            banner()
        elif args.command == 'configure':
            configure(args)
        elif args.command == 'fingerprint':
            digest = hashlib.sha256()
            for path in [CONFIG, ROOT / 'server/requirements.lock', *sorted((ROOT / 'server/src').rglob('*.py'))]:
                digest.update(str(path.relative_to(ROOT)).encode())
                digest.update(path.read_bytes())
            print(digest.hexdigest())
        else:
            cfg = read_config()
            if args.command == 'robinhood-setup':
                robinhood_setup(cfg, action=args.robinhood, yes=args.yes, docker=args.docker)
            elif args.command.startswith('oauth-'):
                oauth_command(cfg, args.command, args.scope, yes=args.yes)
            elif args.command == 'chat':
                return launch_chat(cfg)
            elif args.command == 'headers':
                require_auth(cfg)
                expected = origin(cfg.get('LOCAL_AGENT_URL', '')) + '/mcp/'
                output_mode = os.fstat(sys.stdout.fileno()).st_mode
                # Node/Bun subprocess stdio can use a socketpair rather than a
                # FIFO. Both are IPC channels; neither proves peer identity.
                if (not (stat.S_ISFIFO(output_mode) or stat.S_ISSOCK(output_mode))
                        or os.environ.get('CLAUDE_CODE_MCP_SERVER_NAME') != 'mangrove-agent'
                        or os.environ.get('CLAUDE_CODE_MCP_SERVER_URL') != expected):
                    raise SetupError('Headers require a pipe or socket to the configured Claude MCP connection.')
                # Intentional credential protocol output to Claude's IPC channel,
                # not a log. The guard above rejects terminals and regular files;
                # the configured server name/URL must also match. Do not move this
                # payload into diagnostics or remove it: Claude needs the raw header.
                print(json.dumps({'X-API-Key': local_key(cfg)}))
            elif args.command == 'register':
                check(cfg)
                registration = {'type': 'http', 'url': origin(cfg['LOCAL_AGENT_URL']) + '/mcp/',
                                'headersHelper': shlex.join([sys.executable, str(Path(__file__).resolve()), 'headers'])}
                subprocess.run(['claude', 'mcp', 'remove', 'mangrove-agent', '-s', 'local'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                result = subprocess.run(['claude', 'mcp', 'add-json', '--scope', 'local', 'mangrove-agent',
                                         json.dumps(registration)], capture_output=True, check=False)
                if result.returncode:
                    raise SetupError('Claude MCP registration failed; update Claude Code and retry setup.')
                print('Registration saved. Approve the header helper in Claude if prompted.')
            elif args.command == 'markets-check':
                check_markets(cfg)
            elif args.command in {'check', 'verify'}:
                check(cfg)
                if args.command == 'verify':
                    if request(cfg, '/health', authenticated=False).get('scheduler_running') is not True:
                        raise SetupError('Scheduler is not ready; inspect agent startup logs.')
                    # Before the catalog assertion: when the platform is down, the
                    # upstream-derived half of the catalog is legitimately absent, and
                    # "onboarding tools are missing" is a symptom of that outage rather
                    # than a diagnosis. Checking first means the user is told which it is.
                    upstream_reachable = check_upstream(cfg)
                    # Cold discovery can spend 8s on prices, 8s on Markets,
                    # then 15s on MangroveAI. Let those bounded reads finish;
                    # wallet mutations keep the short timeout and no retries.
                    catalog = request(cfg, '/api/v1/agent/tools', timeout=45)
                    if (cfg.get('MANGROVE_ACCESS_MODE') == 'oauth'
                            and catalog.get('mangroveai_status') != 'available'):
                        raise SetupError('Mangrove OAuth verification failed; setup is incomplete. '
                                         'Run ./scripts/setup.sh --auth oauth --oauth login to reconnect.')
                    names = {item['name'] for item in catalog['tools']}
                    required = {'create_wallet', 'import_wallet', 'list_wallets'}
                    if upstream_reachable and catalog.get('mangroveai_status') != 'unavailable':
                        # list_signals is served by the platform, not locally.
                        required.add('list_signals')
                    else:
                        print('MangroveAI tools are unavailable; platform tool access is NOT verified. '
                              'Skipping the platform tool checks while it is unreachable; '
                              'local wallet tools are verified below.')
                    if not required <= names:
                        raise SetupError(
                            'Required onboarding tools are missing from discovery: '
                            + ', '.join(sorted(required - names))
                        )
                print('Local wallet access authenticated; unauthenticated access refused. No payment made.')
            elif args.command == 'onboard-finish':
                finish_onboarding(cfg)
            elif args.command == 'onboard':
                onboard(cfg, yes=args.yes)
            elif args.command == 'guide':
                guide(cfg, yes=args.yes, docker=args.docker)
            elif args.command == 'wallet':
                if not args.action:
                    raise SetupError('Choose an explicit wallet action.')
                wallet_action(args.action, cfg)
        return 0
    except SetupError as exc:
        print(f'Setup: {exc}', file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print('Setup cancelled. Rerun to resume; list wallets before retrying a wallet mutation.', file=sys.stderr)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, getpass.GetPassWarning):
        print('Setup could not complete. Check config/permissions and local agent availability; no automatic retry.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
