# Security Audit — 2026-05-08

**Scope:** `/tmp/mangrove-agent-rebrand` (app-in-a-box → mangrove-agent rebrand candidate).
**Tooling:** bandit (severity-low through high), pip-audit, manual review of wallet/auth/x402 surface.
**Auditor note:** Performed inline (subagent dispatch failed permissions). Findings below are reproducible via the commands referenced; this doc is the single source for the rebrand triage.

## Executive summary

The post-EIP-7702 hardening work is real and enforced in code: `wallet_manager.sign()` calls `_validate_sign_target()` before key decryption, refusing tx type 4 / authorization lists / non-1inch routers; `sign_message()` is disabled outright. `pip-audit` and `bandit` are clean of medium+/high findings. **The blocking finding is a high-severity x402 bypass that is independent of the EIP-7702 work** — any value in the `X-API-Key` header skips x402 payment without validation.

## Findings

### HIGH-1 — x402 middleware bypass: unvalidated `X-API-Key` skips payment
- **File:** `server/src/app.py:111-117`
- **CWE:** CWE-287 (Improper Authentication), CWE-288 (Authentication Bypass Using an Alternate Path or Channel)
- **Detail:** The HTTP middleware at `app.py:111` short-circuits the x402 payment handler whenever **any non-empty** `X-API-Key` header is present:
  ```python
  @application.middleware("http")
  async def x402_middleware(request: Request, call_next):
      api_key = request.headers.get("x-api-key")
      if api_key:
          return await call_next(request)
      return await x402_handler(request, call_next)
  ```
  No call to `has_valid_api_key`. The downstream `hello_mangrove` route at `server/src/api/routes/hello_mangrove.py:36-41` does not declare `Depends(require_api_key)` either — it captures the header but never validates. Net effect: `curl -H "X-API-Key: anything" /api/x402/hello-mangrove` returns 200 and the message body without payment.
- **Proposed fix:** middleware validates the key before bypassing — call `has_valid_api_key(api_key)` and only bypass if true; otherwise fall through to `x402_handler`. Add a regression test:
  ```python
  def test_x402_unvalidated_header_does_not_bypass(client):
      r = client.get("/api/x402/hello-mangrove", headers={"X-API-Key": "wrong"})
      assert r.status_code == 402
  ```
- **Severity rationale:** demo route only ($0.05 USDC), but the same middleware pattern is on the path for any future x402-gated endpoint. Easy to fix; high downside if reproduced for higher-value gates.

### MEDIUM-1 — API key comparison non-constant-time
- **File:** `server/src/shared/auth/middleware.py:31-33` and `44-46`
- **CWE:** CWE-208 (Observable Timing Discrepancy)
- **Detail:** `api_key in valid_keys` (set membership) does hash-based lookup but the underlying `str.__eq__` is byte-by-byte and short-circuits on first mismatch. With a remote attacker, a small valid_keys set, and short keys, a timing oracle could leak a prefix.
- **Proposed fix:** use `secrets.compare_digest` against each configured key:
  ```python
  import secrets
  return any(secrets.compare_digest(api_key, k) for k in valid_keys)
  ```
- **Severity rationale:** mostly theoretical for single-tenant local dev (network jitter dominates), but the same code runs in `dev-config.json` / `prod-config.json` deployments. Cheap to fix; no behavior change.

### MEDIUM-2 — Secret resolver crashes on malformed `secret:` value
- **File:** `server/src/config.py:99-103`
- **CWE:** CWE-754 (Improper Check for Unusual Conditions)
- **Detail:**
  ```python
  if str_val.startswith("secret:"):
      parts = str_val.split(":")
      secret_id = parts[1]
      secret_property = parts[2]
  ```
  No bounds check. A config value like `"API_KEYS": "secret:malformed"` (missing `:property`) raises `IndexError` mid-startup with no informative message. Misconfiguration becomes a stack trace instead of a clear "expected `secret:<name>:<property>`, got `secret:malformed`."
- **Proposed fix:** validate `len(parts) == 3`; raise a `ConfigError` with the expected shape in the message.
- **Severity rationale:** failure mode is loud, not silent — but obscure. Easy fix; improves operability.

### LOW-1 — `SecretUtils.get_secret` calls `sys.exit(1)` on errors
- **File:** `server/src/shared/gcp_secret_utils.py:21,25,35,37`
- **CWE:** CWE-755 (Improper Handling of Exceptional Conditions)
- **Detail:** Five distinct `sys.exit(1)` calls — empty `secret_id`, missing `project_id`, `NotFound`, generic exception. This couples the secret loader to process lifecycle, makes testing in isolation difficult (test process exits), and prevents graceful degradation (e.g., dev mode falling back to a default).
- **Proposed fix:** raise a typed `SecretResolutionError` exception. Let `_Config.__init__` decide whether to exit (it already does, with better messages).
- **Severity rationale:** intentional fail-loud, but the implementation is brittle and untestable. 31% test coverage on this file is a direct symptom.

### LOW-2 — Bare `except Exception: pass` in shutdown paths
- **Files:**
  - `server/src/services/scheduler_service.py:90`
  - `server/src/shared/db/sqlite.py:60`
- **CWE:** CWE-703 (Improper Check or Handling of Exceptional Conditions)
- **Detail:** Both paths swallow shutdown errors silently. `scheduler.shutdown(wait=False)` failing isn't critical, but a hung scheduler thread can leak. Same for sqlite connection close.
- **Proposed fix:** narrow the exception type and log at WARN with structlog. Don't reraise.

### LOW-3 — `try/except/continue` swallows order-intent validation errors
- **File:** `server/src/services/strategy_service.py:535`
- **Detail:** `OrderIntent.model_validate(o)` failure → continue. A malformed order intent silently dropped means the agent skipped a trade for unknown reasons.
- **Proposed fix:** log at WARN with the offending payload (after PII scrub), then continue.

### LOW-4 — 1inch router allowlist hard-coded to V5/V6
- **File:** `server/src/services/wallet_manager.py:71-72`
- **Detail:** When 1inch ships V7, every swap fails with `Refused to sign`. Documented behavior (`"the guard's allowlist must be explicitly expanded with review"`).
- **Proposed fix:** acceptable as-is, but add a periodic check (or CI job) that fetches the current 1inch deployment list and warns when a new version drops.

### LOW-5 — `random.Random` flagged by bandit B311 — false positive
- **File:** `server/src/services/candidate_generator.py:186`
- **Detail:** Used for strategy candidate sampling, not crypto. Acceptable.
- **Proposed fix:** add `# nosec B311` with a one-line justification, or wrap in a comment so future linting runs don't re-flag.

### INFO — what was checked and is clean

- `pip-audit` against `requirements.txt`: **No known vulnerabilities found.**
- `bandit -r src/ -ll -i`: **No medium+ severity findings.**
- `safety check`: errored on a beta version specifier (`=8.0.0b1`) and is deprecated upstream — replace with `safety scan` in CI before launch.
- **EIP-7702 defenses verified in code:**
  - `wallet_manager.sign()` line 715 calls `_validate_sign_target()` BEFORE `_load_secret()`.
  - `_validate_sign_target` rejects tx type 4 (`server/src/services/wallet_manager.py:115`), authorization lists (`:121`), and any `to` address not in `_ONEINCH_ROUTERS` or matching the approve-for-1inch shape (`:146`).
  - `sign_message()` line 760 raises `SigningError` unconditionally — `personal_sign` is disabled.
  - `block-wallet-secrets.sh` regex-matches 0x+64-hex and 12/24-word mnemonics in user input, with mode-aware behavior in tool output (only blocks when adjacent to a key field name).
  - `preflight-swap.sh` queries `/api/v1/agent/wallet/<addr>/balances` and refuses execute_swap when input_token balance is 0.
- **Layering:** `services/` does not import from `api/`. `shared/` does not import from `services/` or `api/`. `order_executor` does not directly import `secret_vault` (goes through `wallet_sign`).

## Rename-related security note

`preflight-swap.sh` matches the literal `mcp__defi-agent__execute_swap`. After Task 15 (MCP server name → `mangrove-agent`), this hook will silently no-op on every swap. Listed as **must-fix** in the rename phase because failing to update it removes a defense layer.

## Tools / commands to reproduce

```bash
cd /tmp/mangrove-agent-rebrand/server && source .venv/bin/activate
bandit -r src/ -ll -i             # high+medium severity
bandit -r src/ -l                  # include low
pip-audit
pytest --cov=src --cov-report=term-missing -q  # coverage gaps
```
