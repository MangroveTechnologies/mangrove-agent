# B9 setup remediation — 2026-09-18

Four audit findings are fixed locally:

- Config staging uses ignored `local-config.json.setup-*` files with mode 0600.
  Legacy `.setup-*` remnants are also ignored; forced-exit regression coverage added.
- Backup helpers share loopback-only, no-proxy, no-redirect transport with bounded
  responses and sanitized errors. EVM/Solana backup support remains. Reveal requires
  terminal stdout; intentional terminal secret display remains part of backup.
- Foreground cleanup respects lock ownership and matching PID records; regression
  covers successor lock/PID preservation after the foreground session exits.
- MCP registration uses a target-validated dynamic header helper, keeping credentials
  out of CLI arguments. Unsafe `--api-key KEY` is rejected; hidden prompts and stdin
  are supported, with macOS/Windows bootstrap wrappers updated to use stdin.

Scheduler readiness is restored through local health verification. No payment,
signing, encryption or spending-ledger behavior changed.

Full Python 3.12 suite: **1,188 passed, 2 skipped**, 133 existing warnings,
46.82 seconds, live-payment flags disabled. Ruff, shell parsing, whitespace,
skills manifest and the **106-tool** offline contract check passed. The scoped
added-text scan across **18 changed/new files** found no personal paths, emails,
private-key literals or matches to configured sensitive values. Sensitive values
were compared in memory and never printed. This is not a historical or machine-wide
secret audit.

The user will test actual Claude registration/connection. Automated tests validate
registration arguments, header-helper output and rejection of wrong targets with
synthetic credentials. Real Windows/Docker install matrices and hosted API-key/x402
acceptance remain unverified. No live wallet operations, payments, agent restart,
Claude registration, commit or push performed. Production acceptance remains pending.
