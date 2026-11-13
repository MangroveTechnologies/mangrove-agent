"""Write server/requirements-custody.lock: the custody runtime's pins with wheel hashes.

The custody process imports only part of the agent's dependencies.
``CUSTODY_REQUIREMENTS`` names the distributions that custody code imports
directly. This script adds their transitive dependencies from each pinned
release's PyPI metadata, evaluated for CPython 3.12 on Linux and macOS, and
takes every version and environment marker from server/requirements.lock. A
dependency missing from requirements.lock is an error.

The custody installer installs the result with ``pip --require-hashes
--only-binary :all:``, so pip installs only wheels whose SHA-256 is listed
here and never builds a source distribution (whose build dependencies would be
fetched without hash checks). Every wheel PyPI publishes for a pinned version
is listed, so the same file installs on Linux and macOS. A pin with no wheel is
an error. Run it with the agent's Python environment, which provides
``packaging``, after changing requirements.lock or the custody imports:

    python3 scripts/hash_custody_lock.py

``--check`` verifies offline that every hashed pin matches requirements.lock
and that every custody requirement is pinned. ``--verify`` regenerates the file
from PyPI in memory and fails on any difference, so the dependency set and the
hashes cannot drift silently.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'server/requirements.lock'
HASHED = ROOT / 'server/requirements-custody.lock'
PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#\\]+)\s*(?:;\s*([^#\\]*?))?\s*\\?\s*$')

# Distributions imported directly by scripts/custody-service.py and the
# server modules it loads (src.custody, wallet_manager, transaction_intent and
# their shared imports), with the module that needs each one.
CUSTODY_REQUIREMENTS = (
    'cryptography',                 # src.shared.crypto.fernet, src.custody.admin
    'eth-account',                  # wallet_manager signing
    'eth-abi',                      # transaction_intent ABI decoding
    'eth-utils',                    # transaction_intent addresses and selectors
    'pydantic',                     # wallet_manager response models
    'fastapi',                      # src.shared.errors, src.shared.logging
    'starlette',                    # src.shared.logging
    'structlog',                    # src.shared.logging
    'google-cloud-secret-manager',  # src.config secret references
    'keyring',                      # src.shared.crypto.fernet keychain source
    'httpx',                        # Sign-In-With-X URI validation
    'mangroveai',                   # src.shared.clients.mangrove payment destination
    'mangrovemarkets',              # src.shared.clients.mangrove
    'xrpl-py',                      # XRPL wallets and ownership proofs
    'x402[evm]',                    # configured x402 payer resolution
    'anyio',                        # x402_payer
)

# Supported custody hosts: CPython 3.12 on Linux and macOS.
ENVIRONMENTS = tuple(
    {'sys_platform': platform, 'platform_system': system, 'platform_machine': machine,
     'os_name': 'posix', 'python_version': '3.12', 'python_full_version': '3.12.0',
     'implementation_name': 'cpython', 'platform_python_implementation': 'CPython'}
    for platform, system, machines in (('linux', 'Linux', ('x86_64', 'aarch64')),
                                       ('darwin', 'Darwin', ('x86_64', 'arm64')))
    for machine in machines
)


def pins(path: Path) -> dict[str, tuple[str, str, str]]:
    """Canonical name -> (name, version, environment marker) for every pinned requirement."""
    found = {}
    for line in path.read_text().splitlines():
        match = PIN.match(line.strip())
        if match:
            name = match.group(1).lower()
            found[canonicalize_name(name)] = (name, match.group(2), (match.group(3) or '').strip())
    return found


def release(name: str, version: str, cache: dict) -> dict:
    if (name, version) not in cache:
        url = f'https://pypi.org/pypi/{name}/{version}/json'
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed PyPI origin
            cache[name, version] = json.load(response)
    return cache[name, version]


def applies(requirement: Requirement, extras: frozenset[str]) -> bool:
    if requirement.marker is None:
        return True
    return any(requirement.marker.evaluate({**environment, 'extra': extra})
               for environment in ENVIRONMENTS for extra in ('', *sorted(extras)))


def closure(locked: dict[str, tuple[str, str, str]], cache: dict) -> list[tuple[str, str, str]]:
    """The custody requirements and their dependencies, at the versions pinned in the lock."""
    extras_seen: dict[str, frozenset[str]] = {}
    pending = [Requirement(spec) for spec in CUSTODY_REQUIREMENTS]
    while pending:
        requirement = pending.pop()
        key = canonicalize_name(requirement.name)
        if key not in locked:
            raise ValueError(f'{requirement.name} is required by custody but not pinned in requirements.lock')
        extras = frozenset(requirement.extras)
        if key in extras_seen and extras <= extras_seen[key]:
            continue
        extras = extras_seen[key] = extras_seen.get(key, frozenset()) | extras
        name, version, _ = locked[key]
        for spec in release(name, version, cache)['info'].get('requires_dist') or []:
            dependency = Requirement(spec)
            if applies(dependency, extras):
                pending.append(dependency)
    return sorted(locked[key] for key in extras_seen)


def hashes(name: str, version: str, cache: dict) -> list[str]:
    files = release(name, version, cache)['urls']
    digests = sorted({item['digests']['sha256'] for item in files if item['packagetype'] == 'bdist_wheel'})
    if not digests:
        raise ValueError(f'PyPI lists no wheel for {name}=={version}; custody installs wheels only')
    return digests


def render(locked: dict[str, tuple[str, str, str]]) -> str:
    cache: dict = {}
    lines = ['# Generated by scripts/hash_custody_lock.py from server/requirements.lock. Do not edit.',
             '# Installed by scripts/install-custody.py with pip --require-hashes --only-binary :all:.']
    for name, version, marker in closure(locked, cache):
        digests = hashes(name, version, cache)
        lines.append(f'{name}=={version}' + (f' ; {marker}' if marker else '') + ' \\')
        lines.extend(f'    --hash=sha256:{digest}' + (' \\' if index < len(digests) - 1 else '')
                     for index, digest in enumerate(digests))
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    locked = pins(LOCK)
    if args.verify:
        if not HASHED.is_file() or HASHED.read_text() != render(locked):
            print('server/requirements-custody.lock differs from a fresh render of the custody '
                  'requirements, requirements.lock and PyPI wheel hashes; run '
                  'python3 scripts/hash_custody_lock.py and review the diff', file=sys.stderr)
            return 1
        print('Custody hashed lock matches a fresh render from PyPI.')
        return 0
    if args.check:
        hashed = pins(HASHED) if HASHED.is_file() else {}
        roots = {canonicalize_name(Requirement(spec).name) for spec in CUSTODY_REQUIREMENTS}
        if not hashed or any(locked.get(key) != pin for key, pin in hashed.items()) or not roots <= set(hashed):
            print('server/requirements-custody.lock does not match server/requirements.lock; '
                  'run python3 scripts/hash_custody_lock.py', file=sys.stderr)
            return 1
        print('Custody hashed lock matches requirements.lock.')
        return 0
    HASHED.write_text(render(locked))
    print(f'Wrote {HASHED.relative_to(ROOT)} with {len(pins(HASHED))} hashed pins.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
