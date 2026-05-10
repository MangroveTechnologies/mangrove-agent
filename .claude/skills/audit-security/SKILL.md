---
name: audit-security
description: Run a focused security audit of the mangrove-agent trading bot. Covers wallet signing surfaces, EIP-7702 / arbitrary-signing risks, MCP tool permissions, Fernet key handling, DEX executor paths, hook-enforcement gaps, OWASP Top 10, and dependency CVEs. Use when the user asks for "security audit", "audit security", "security review", "check for vulnerabilities", "OWASP audit", or "/audit-security". Adapted from the Mangrove workspace `audit-security` skill with a trading-bot lens added after the 2026-04-24 EIP-7702 incident.
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash
---

# Security Audit — mangrove-agent trading bot

Read-only audit against the mangrove-agent repo. Covers:

- **Wallet + signing surfaces** (post-2026-04-24 incident focus)
- **Web3 / crypto-specific threats** (EIP-7702 delegation, permit abuse, tx replay, router allowlist drift, private-key lifetime, Fernet at rest)
- **MCP tool layer** (auth tier assignments, tool input validation, response leakage)
- **OWASP Top 10 + CWE/SANS Top 25** (SQL injection via SQLite, SSRF via webhook URLs, path traversal via config files, etc.)
- **Dependency CVEs** (`pip-audit` / `safety check` against `server/requirements.txt`)
- **Infrastructure** (hook registration, docker-compose exposure, default API key shipping)

## Process

1. **Check for prior audits** at `docs/audits/*-security-audit.md`. If a recent one exists, read it to avoid re-flagging fixed issues and to track remediation.

2. **Wallet + signing invariants** (CRITICAL — given 2026-04-24). Verify:
   - `server/src/services/wallet_manager.py::_validate_sign_target` still refuses: tx type 3, tx type 4, `authorizationList` field, non-1inch `to`, non-1inch-spender approves, bare tx (no `to`).
   - `_ONEINCH_ROUTERS` set matches known 1inch V5 + V6 addresses.
   - `sign_message()` still raises unconditionally — no EIP-191 personal_sign path.
   - No other code path in `server/src/` calls `Account.from_key`, `Account.from_mnemonic`, `sign_transaction`, `sign_message`, `sign_typed_data`, `sign_authorization`, or `setCode` outside of `wallet_manager.py`.
   - Fernet `decrypt()` is only called from `wallet_manager._load_secret` and `reveal_wallet_secret`.
   - `reveal_wallet_secret` response stays on the localhost REST endpoint, never exposed via MCP.

3. **Hook-enforcement gaps.** For each hook in `.claude/hooks/*.sh`:
   - Confirm it's registered in `.claude/settings.json` under the right matcher.
   - Confirm it `set -uo pipefail`, handles malformed JSON without crashing, and uses proper exit codes (0 = allow, 2 = block).
   - Confirm it doesn't log or exfiltrate secrets in its stderr message (particularly `block-wallet-secrets.sh`).

4. **MCP tool layer.** For each tool in `server/src/mcp/tools.py`:
   - Auth tier matches the sensitivity (no auth-gated tool slipped into free tier).
   - No tool returns `encrypted_secret`, raw Fernet ciphertext, or `secret` fields in its response.
   - `api_key` param is validated via `_require()` before the tool body runs.

5. **REST routes.** For each route in `server/src/api/routes/*.py`:
   - Auth dependency is wired correctly.
   - No endpoint returns plaintext key material over HTTP except the two intentional localhost-only reveal routes (`/wallet/reveal-secret/{vault_token}` and `/wallet/{addr}/reveal`), which MUST NOT be called by MCP tools.
   - Pydantic validators enforce slippage caps, chain allowlists, and amount bounds.

6. **Dependency CVEs.**
   ```bash
   cd server && pip install pip-audit && pip-audit -r requirements.txt
   ```
   Report HIGH / CRITICAL findings only.

7. **Secret handling.**
   - `grep -rE "(private_key|seed_phrase|mnemonic|secret|API_KEY)" server/src/` — every hit should be a name, not a plaintext value.
   - Check `.env.example` and `server/src/config/*-config.json` for accidentally committed real keys.
   - `git log --all -p -- '*.env*' '*config*'` — check git history for leaked secrets.

8. **Infrastructure.**
   - `docker-compose.yml` — no bind mounts exposing host secrets; port bindings match the documented `9080`.
   - `.github/workflows/ci.yml` — no secret leakage in logs; `${{ secrets.* }}` usage is minimal.
   - `scripts/*.sh` — input validated; no command injection via user input.

## Output

Write a report to `docs/audits/YYYY-MM-DD-mangrove-agent-security-audit.md` with this shape:

```
# Security Audit — YYYY-MM-DD

## Summary
Overall severity + high-level findings count by CRITICAL / HIGH / MEDIUM / LOW.

## Findings
### [CRITICAL] <short title>
- **Location:** `path/to/file.py:LN`
- **CWE:** CWE-N — Name
- **Description:** what's wrong
- **Impact:** what could happen
- **Remediation:** concrete fix

(repeat per finding)

## Done Well
- Signing guard at `wallet_manager._validate_sign_target` correctly refuses EIP-7702
- (etc.)

## Dependencies
List of CVEs from pip-audit with fix recommendations.
```

## Constraints

- **Read-only.** Never modify source code, tests, or configuration.
- **Redact secrets.** If you encounter a plaintext key or API token in any file, replace with `***` in the report.
- **No exploits.** Never run network-level attack tests or modify running services.
- **Cite every finding.** File path + line number + CWE ID. No generic "this could be exploited" language without a specific pointer.
- **Acknowledge good practices.** The "Done Well" section matters — the Fernet + SecretVault + signing-guard layers exist precisely because of past incidents, and the audit should record what's working.
