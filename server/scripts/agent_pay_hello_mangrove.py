"""Noninteractive REST payment from the configured custodied wallet.

Run with ENVIRONMENT matching the running agent; use --help for options.
See docs/x402-payment-scripts.md for setup and payment-result semantics.
"""
from _x402_demo import main

if __name__ == "__main__":
    raise SystemExit(main("rest"))
