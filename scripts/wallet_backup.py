"""Retired agent-side backup transport; custody administration is human-only."""
import sys


def run(*_args, **_kwargs):
    from setup_support import SetupError
    raise SetupError("Wallet backup and confirmation have moved to the isolated custody administrator terminal. Ordinary API credentials cannot authorize them. Use custody-service.py admin --config /etc/mangrove-custody/policy.json backup ADDRESS (or confirm-backup ADDRESS).")


def main():
    from setup_support import SetupError
    try:
        run()
    except SetupError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
