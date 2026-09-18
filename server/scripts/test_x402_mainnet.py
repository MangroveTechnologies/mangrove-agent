"""Live REST payment smoke test. Historical filename; network comes only from config.

Run with ENVIRONMENT matching the running agent; use --help for options.
See docs/x402-payment-scripts.md for setup and payment-result semantics.
"""
from _x402_demo import main

if __name__ == "__main__":
    raise SystemExit(main("smoke"))
