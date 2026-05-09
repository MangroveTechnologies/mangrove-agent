# Architecture Audit — 2026-05-08

**Scope:** `server/src/` overall structure, layering, MCP/REST coupling, signing path, configuration loading.
**Tooling:** grep-based import-graph checks, manual review against documented architecture.

## Executive summary

The documented architecture (service-layer pattern, three-tier auth, dual REST/MCP) is honored in code with two notable exceptions: (1) the x402 middleware bypass design conflates "header present" with "key validated" — see security audit HIGH-1; (2) the secret resolver couples to process lifecycle (`sys.exit`), making it untestable. Layering boundaries are clean. The MCP/REST split is honest — both protocols call the same services with no business-logic duplication observed in spot checks.

## Findings

### MEDIUM-1 — Secret resolver couples to process lifecycle
- **Files:** `server/src/shared/gcp_secret_utils.py`, `server/src/config.py:36-49`
- **Detail:** `SecretUtils.get_secret` calls `sys.exit(1)` on every error path (5 places). `_Config.__init__` also exits on missing required keys, missing config file, missing env. Net effect: any failure during config load kills the process before logging is initialized — the user sees a `print()` line and a non-zero exit code. Tests can't exercise these paths without subprocesses.
- **Proposed refactor:** raise a `ConfigError` exception hierarchy; let `app.py` catch and exit cleanly with a structured log entry. Leave the exit-on-failure semantics intact, but pull them up to a single boundary.
- **Effort:** small (1-2 hours).

### MEDIUM-2 — Middleware-level x402 bypass conflates "header present" with "key valid"
- **File:** `server/src/app.py:111-117`
- **Detail:** Already covered in security audit HIGH-1. Architecturally, the bypass decision is made at the middleware layer where it cannot see the route's own auth declaration (`Depends(require_api_key)` or none). This is a layering smell: payment vs auth are two concerns being entangled by a generic middleware. A route that legitimately wants to be free with no auth at all is not distinguishable from a route that needs auth-or-payment.
- **Proposed refactor:** the middleware validates the key (`has_valid_api_key`) before bypassing; the route declares its own auth requirements via FastAPI dependencies. The middleware's job is "decide whether to invoke x402" not "decide auth."

### MEDIUM-3 — `mcp/registry.py` is shared by FastMCP registration and `/discovery` JSON catalog — coherent but overloaded
- **Files:** `server/src/mcp/registry.py`, `server/src/api/routes/discovery.py`
- **Detail:** A single `register_tool(ToolEntry(...))` call both registers a callable with FastMCP and adds metadata to the discovery catalog. This is convenient and avoids drift, but it means the registry knows about FastMCP types AND about discovery JSON shape. Adding a new transport (e.g., a future stdio-MCP variant) would require the registry to grow another bound.
- **Proposed refactor:** acceptable for v1. Note in v2 backlog: split `ToolEntry` (transport-agnostic metadata) from a transport adapter that consumes it.

### LOW-1 — `wallet_manager` is 715 lines, mixed concerns
- **File:** `server/src/services/wallet_manager.py`
- **Detail:** Same as code-quality LOW-2. Architecturally: `Manager` suffix doing 5 things — signing, vault, import, confirm-backup, normalize. Splits naturally into `sign/`, `vault/`, `import/`, with a thin `wallet_manager` facade.
- **Proposed refactor:** v2.

### LOW-2 — External SDK coupling is direct, not behind a port
- **Files:** `server/src/services/order_executor.py` (imports `mangrovemarkets`), `server/src/services/backtest_service.py` (imports `mangroveai`), `server/src/services/strategy_service.py`
- **Detail:** Service code imports the SDK packages directly. The 0.1 → 0.2 SDK migration (PRs #59, #66) touched several service files because there's no thin adapter layer.
- **Proposed refactor:** for v2, introduce `server/src/clients/{mangrove_markets,mangrove_ai}.py` as a port — services depend on the port, the port wraps the SDK. Reduces blast radius of future SDK majors.

### INFO — clean

- **Layering:** `services/` does not import `api/`. `shared/` does not import `services/` or `api/`. `models/` is a pure domain layer. (Verified with `grep -rn '^from src.api' src/services/ src/shared/`.)
- **Service-layer pattern honored:** spot-checked `dex.py` route + `_register_dex` MCP tool group — both call `services/order_executor.py` and `services/wallet_manager.py`; no duplicated business logic.
- **`order_executor` doesn't reach into `secret_vault` directly** — calls `wallet_sign` which encapsulates key access.
- **`shared/x402/server.py` singleton** is the right pattern for a stateful payment-server connection.
- **Configuration model:** single `app_config` singleton, no per-env globals scattered through services.

## Architectural-rename note

The rebrand renames the FastMCP server (`server/src/mcp/server.py:34`). The MCP namespace is therefore part of the public API of this server. **Plugin consumers and Claude Code hook matchers depend on the namespace.** The rebrand is not a no-op rename — it breaks any external matcher (e.g., the in-repo `preflight-swap.sh` hook) that hard-codes `mcp__defi-agent__*`. Inventory in code-quality.md HIGH-2.
