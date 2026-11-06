"""Retired agent-side secret import. No secret input or HTTP request is made."""
import sys


def main():
    print("Wallet import runs only in the isolated custody administrator terminal. Use custody-service.py admin --config /etc/mangrove-custody/policy.json import; never paste a secret into chat.", file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
