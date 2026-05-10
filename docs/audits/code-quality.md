# Code Quality Audit — 2026-05-08

**Scope:** `server/src/`, `server/tests/`, `.claude/`, `plugin/`, `scripts/`.
**Tooling:** ruff, vulture, radon (cc + mi), pytest --cov, manual review.

## Executive summary

Layering is honored, ruff is clean, vulture finds no high-confidence dead code. The repo's blocking issue for fresh installs is a broken `build-backend` declaration in `pyproject.toml`. Coverage at 74% has critical gaps around the secret/payment surface. 98 pytest warnings (pydantic deprecations, datetime.utcnow, structlog config) need a pre-launch sweep.

## Findings

### HIGH-1 — `pyproject.toml` build backend is broken; `pip install -e .` fails
- **File:** `server/pyproject.toml:3`
- **Detail:**
  ```toml
  [build-system]
  requires = ["setuptools>=68.0"]
  build-backend = "setuptools.backends._legacy:_Backend"
  ```
  `setuptools.backends._legacy:_Backend` is not a valid module path in current setuptools. `pip install -e .` errors with `pip._vendor.pyproject_hooks._impl.BackendUnavailable: Cannot import 'setuptools.backends._legacy'`. Workaround used by the audit: `pip install -r requirements.txt` then `PYTHONPATH=server pytest`. **Real users following the README will hit this error.**
- **Proposed fix:** change to the standard backend:
  ```toml
  build-backend = "setuptools.build_meta"
  ```
  Verify with a fresh `pip install -e .[dev]` in a clean venv.

### HIGH-2 — Three-way naming clash across user-facing surfaces
- **Files (locations of each name):**
  - `defi-agent`:
    - `README.md:6` (the `<h1>defi-agent</h1>` headline)
    - `.claude/rules/trading-bot-workflow.md` (24 refs)
    - `docs/architecture.md` (7 refs), `docs/specification.md` (4 refs), `docs/implementation-plan.md` (4 refs)
    - `docs/workshop/setup-guide.md` (10 refs), `docs/workshop/facilitator-runbook.md` (5 refs)
    - `tutorials/trading-app/{00,02,03,05,06,07,08}.md` (multiple)
    - `server/src/api/router.py` (2), `server/src/shared/errors.py` (1), `server/src/services/backtest_service.py` (1), `server/src/mcp/tools.py:1` (docstring), all four `server/src/config/*-config.json` (1 each)
    - `scripts/{setup-mcp.sh,setup.sh,run-bare.sh,verify_quickstart.sh}` — 13 refs total
    - `.claude/settings.json:50` (`mcp__defi-agent__execute_swap` matcher), `.claude/hooks/preflight-swap.sh` (2)
    - `docker-compose.yml:1` (service name)
  - `app-in-a-box`:
    - `server/src/mcp/server.py:34` (FastMCP server name — what determines the `mcp__<name>__<tool>` namespace)
    - `plugin/.claude-plugin/plugin.json` (3 refs incl. `name`)
    - `plugin/.mcp.json:3` (server key), `plugin/README.md` (3)
    - `server/pyproject.toml:7` (project name)
    - `CLAUDE.md` (4), `README.md` (6), `CONTRIBUTING.md` (2)
    - 6 .claude/skills/{architecture,audit-security,check-alignment,onboard,specification,tool-spec,tutorial}/SKILL.md (~21 refs)
    - `.claude/agents/product-owner.md` (1)
    - `docs/{architecture,specification,verification-checklist}.md`, `docs/workshop/{setup-guide,run-of-show,prereqs,facilitator-runbook}.md`
    - `docs/incidents/2026-04-24-eip7702-drain.md` (5 — historical, KEEP)
- **Detail:** users see a different name in the README (`defi-agent`), in their MCP-tools list (`mcp__app-in-a-box__*`), and in the plugin registry. Internally, hooks key off `mcp__defi-agent__execute_swap` while the actual server registers as `app-in-a-box` — meaning **`preflight-swap.sh` does not currently match the real MCP namespace**. This is a latent defect today, not introduced by the rebrand.
- **Proposed fix:** unify all three to `mangrove-agent` per the rebrand plan. Verify the hook actually fires after the rename.

### MEDIUM-1 — Test coverage gaps on auth/payment-critical surfaces
- **Files (coverage %):**
  - `server/src/shared/gcp_secret_utils.py` — **31%** (resolves every config secret in dev/prod)
  - `server/src/shared/x402/server.py` — **49%** (payment singleton)
  - `server/src/shared/crypto/fernet.py` — **64%** (master-key keyfile resolver — security-critical post-incident)
- **Proposed fix:** before launch, target ≥80% on each of these. Tests can mock the GCP client, x402 facilitator, and the keyring layer.

### MEDIUM-2 — 98 pytest warnings; pydantic / datetime / structlog deprecations
- **Source counts (from `pytest -q` warnings summary):**
  - `daily_momentum_limit` and `weekly_momentum_limit` deprecated top-level pydantic fields — surfaced from the `mangroveai` SDK schema. Use `cooldown_config` instead. Affects `tests/integration/test_strategy_routes.py`, `tests/integration/test_strategy_service.py`, `tests/unit/test_backtest_service.py`, `tests/e2e/test_paper_lifecycle.py`.
  - `datetime.datetime.utcnow()` deprecated — `tests/e2e/test_paper_lifecycle.py:180`. Replace with `datetime.now(datetime.UTC)`.
  - `structlog._base` warning: `Remove 'format_exc_info' from your processor chain` — surfaces in `test_strategy_service.py::test_tick_catches_sdk_errors`, `test_order_executor.py::test_execute_many_failure_does_not_block_others`, `test_scheduler_service.py::test_event_listener_emits_scheduler_job_errored_on_failure`. Adjust `server/src/shared/logging.py` processor chain.
- **Proposed fix:** clean before launch. Pre-1.0, deprecation warnings turn into hard errors and break workshop attendees on a future python/lib bump.

### LOW-1 — Eight functions at radon C complexity (CC 11-20)
- `_Config.load_configuration` — `server/src/config.py:25` — multi-step env detection, key validation, secret resolution.
- `tick` — `server/src/services/strategy_service.py:455` — main strategy evaluator.
- `update_status` — `server/src/services/strategy_service.py:375`.
- `generate` — `server/src/services/candidate_generator.py:166` — strategy candidate generation.
- `search`, `_detect_category` — `server/src/services/reference_strategies_service.py:113,94`.
- `_live_swap` — `server/src/services/order_executor.py:131` — main live-swap orchestration.
- `_normalize_payload` — `server/src/services/wallet_manager.py:685` — pre-sign payload normalization.
- **Proposed fix:** acceptable as-is for v1; flag for refactor if any of these gains another branch.

### LOW-2 — `services/wallet_manager.py` is 715 lines and mixes concerns
- Signing guard, key vault interactions, MetaMask import flow, confirm-backup gating, and the pre-sign normalizer all in one file. Split candidates: `wallet/sign.py` (sign + guard), `wallet/import.py` (import flow + reveal/confirm), `wallet/keys.py` (vault interactions). v2 refactor.

### INFO — clean

- `ruff check .` — clean.
- `vulture src/ --min-confidence 80` — no findings.
- Layering — `services/` does not import `api/`, `shared/` does not import `services/` or `api/`.
- 348 tests passing, 2 skipped.
- `radon mi src/`: every file rated **A** (most ≥ 70 maintainability).

## Tools / commands to reproduce

```bash
cd /tmp/mangrove-agent-rebrand/server && source .venv/bin/activate
ruff check .
vulture src/ --min-confidence 80
radon cc src/ -a -nc        # complexity
radon mi src/ -s             # maintainability
PYTHONPATH=. pytest --cov=src --cov-report=term-missing -q
```
