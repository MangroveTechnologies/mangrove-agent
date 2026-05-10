# Post-rename verification — 2026-05-09

Final smoke check after Tasks 8–15 landed on `feature/mangrove-agent-rebrand`.

## Static checks

| Check | Command | Result |
|-------|---------|--------|
| Full pytest | `PYTHONPATH=. pytest -q` | **351 passed, 2 skipped** (was 348/2 pre-rebrand; +3 new x402 bypass regression tests) |
| ruff | `ruff check .` | **All checks passed!** |
| Editable install | `pip install -e .` in fresh venv | **Successfully installed mangrove-agent-0.1.0** (pre-fix: `BackendUnavailable`) |
| Final grep — unwanted refs in tracked code | `grep -rl "app-in-a-box\|App-in-a-Box\|defi-agent\|defi_agent" .` (excluding intentional preserves) | **Zero hits** |

## Boot smoke

```
ENVIRONMENT=test python -m uvicorn src.app:app --port 9085
```

- `/health` → `{"status":"healthy", "timestamp":"2026-05-10T02:58:25.901043+00:00"}` ✓
- MCP server name asserted: `mangrove-agent` ✓
- Hooks syntax-valid: `block-wallet-secrets.sh`, `block-main-commits.sh`, `preflight-swap.sh` (`bash -n` all pass)

## x402 bypass regression — live server proof

The audit-flagged bypass (HIGH-1 in `security.md`) is fixed and verified against a real running server, not just unit tests:

| Request | Pre-fix behavior | Post-fix behavior |
|---------|-----------------|------------------|
| `GET /api/x402/hello-mangrove` (no header) | 402 ✓ | **402** ✓ |
| `GET /api/x402/hello-mangrove -H "X-API-Key: test-key-1"` (valid) | 200 ✓ | **200** ✓ |
| `GET /api/x402/hello-mangrove -H "X-API-Key: bogus"` (invalid) | **200** ❌ — bypass | **402** ✓ — properly gated |

Plus three regression tests in `tests/test_x402.py` (`test_invalid_api_key_does_not_bypass_x402`, `test_valid_api_key_bypasses_x402`, `test_no_api_key_returns_402`).

## Intentionally preserved name references

These files contain `app-in-a-box` / `defi-agent` references on purpose and are NOT part of the rename:

- `docs/incidents/2026-04-24-eip7702-drain.md` — historical post-mortem; the repo was named `app-in-a-box` at incident time and the references are accurate to that historical record.
- `docs/audits/*.md` — these audit reports describe the rebrand itself; rewriting them would erase the rationale.
- `docs/superpowers/plans/2026-05-08-mangrove-agent-rebrand.md` — the plan that drove this work.

## What's deliberately deferred

- **Workshop PDFs** (`docs/workshop/*.pdf`) — bundled binaries; the `.md` sources are renamed, but PDF regeneration is a follow-up PR.
- **Open dependabot PRs (#56 pydantic, #57 pytest, #58 keyring, #61 ruff, #62 cdp-sdk)** — to be merged after the rebrand PR lands and rebases automatically. Order: ruff → pytest → pydantic → keyring (post audit-recheck) → cdp-sdk (post manual review).
- **Persona name "Hank"** in `docs/user-stories.md` vs current "Sage" in `CLAUDE.md` — content inconsistency from the project's earlier life. Not rename-blocking.
- **structlog `format_exc_info` warning** — fires inside structlog's own `ConsoleRenderer`, not from our processor chain. Doesn't block; deferred.
- **pydantic deprecations from upstream `mangroveai` SDK schemas** (`daily_momentum_limit`, `weekly_momentum_limit`) — needs SDK release with `cooldown_config` migration; can't fix locally.

## Ready for Task 17

Branch `feature/mangrove-agent-rebrand` is at the point where the GitHub repo rename (`gh repo rename mangrove-agent`) and PR open are appropriate. All in-repo references are unified. Open the PR, merge, then rename the GH repo (or rename first then merge — order doesn't matter, but Task 17 in the plan does the GH rename first).
