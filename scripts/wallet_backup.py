"""Terminal backup operations using the setup transport's local trust boundary."""
import argparse
import os
import re
import sys
from pathlib import Path

from setup_support import SetupError, origin, read_config, request, require_auth


def wallet_address(value):
    # Both wallet families supported by the existing backup endpoints.
    if not isinstance(value, str) or not re.fullmatch(r'(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})', value):
        raise SetupError('Invalid wallet address.')
    return value


def run(action, value, by_address=False):
    cfg = read_config(Path(os.environ['CONFIG_FILE']))
    cfg['LOCAL_AGENT_URL'] = origin(os.environ['LOCAL_AGENT_URL'])
    require_auth(cfg)
    if action == 'confirm' or by_address:
        value = wallet_address(value)
    elif not re.fullmatch(r'[a-zA-Z0-9_-]{20,128}', value):
        raise SetupError('Invalid vault token.')
    if action == 'reveal':
        if not sys.stdout.isatty():
            raise SetupError('Reveal requires a private terminal; redirecting secrets is disabled.')
        path = f'/api/v1/agent/wallet/{value}/reveal' if by_address else f'/api/v1/agent/wallet/reveal-secret/{value}'
        result = request(cfg, path)
        secret = result.get('secret')
        if not isinstance(secret, str) or not secret or len(secret) > 2048 or not re.fullmatch(r'[a-zA-Z0-9 ,\[\]]+', secret):
            raise SetupError('Invalid secret response; no response content displayed.')
        addr = wallet_address(result['address']) if result.get('address') else None
        if by_address and addr != value:
            raise SetupError('Returned wallet address does not match the requested wallet.')
        print('Wallet secret: save this outside the agent in your private backup.\n')
        print(secret)
        print('\nClear terminal scrollback after saving your backup.')
        print('Then run: ./scripts/confirm-backup.sh ' + (addr or '<wallet-address>'))
    else:
        result = request(cfg, f'/api/v1/agent/wallet/{value}/confirm-backup', {})
        if not result.get('backup_confirmed_at'):
            raise SetupError('Backup confirmation was not acknowledged.')
        print('Backup confirmed for ' + value + '. No payment was made.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['reveal', 'confirm'])
    parser.add_argument('--address', action='store_true')
    parser.add_argument('value')
    args = parser.parse_args()
    try:
        run(args.action, args.value, args.address)
        return 0
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print('Backup operation failed; check local configuration and agent availability.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
