---
name: tool-spec
description: Draft an MCP tool specification for an mangrove-agent extension. Use when the user asks to "draft an MCP tool", "spec out a new tool", "add a tool for X", or "/tool-spec <purpose>". Produces a JSON-schema-shaped spec that matches the mangrove-agent service-layer pattern and auth-tier conventions, ready to paste into `server/src/mcp/tools.py`. Adapted from the Mangrove workspace `tool-spec` skill with mangrove-agent conventions.
user_invocable: true
argument-hint: "<tool name and purpose>"
---

# Draft MCP Tool Specification

Parse `$ARGUMENTS` as a natural-language description of the tool you want to add (no project prefix — this skill is scoped to mangrove-agent).

Produce a complete MCP tool specification following the conventions already in `server/src/mcp/tools.py` and the service-layer pattern documented in `docs/contributing.md` and `CLAUDE.md`.

## Output sections

### 1. Tool name
Lowercase snake_case. No prefix (the MCP server is single-tenant; all tools live under `mcp__mangrove-agent__*`). Match existing patterns: `get_balances`, `create_strategy_autonomous`, `list_trades`, `evaluate_strategy`.

### 2. Access tier
Pick one:
- **free** — no API key required. Reserve for `status`, `list_tools`, read-only discovery endpoints that can't leak wallet/strategy data.
- **auth** — requires `api_key` param matching `API_KEYS` in config. Default tier for everything else.
- **x402** — payment-gated via the x402 protocol. Currently demo-only (`hello_mangrove`). Add this tier only on explicit product decision.

### 3. Description
One sentence. Match the tone of existing tool descriptions — direct, imperative, and it should state any guard or precondition ("Requires backup-confirmed wallet", "Paper mode only", etc.).

### 4. Parameters
For each parameter give: `name`, `type`, `required`, `description`. Standard patterns:
- `api_key: str = ""` is always last in the param list and required when access != free.
- Addresses use `str` with the natural 0x-prefixed hex convention; chain_id uses `int`.
- Slippage / fee params are `float` in **decimal form** (0.002 = 0.2%), with the cap in the description.
- Token params accept either symbol or contract address (document which is preferred for the route).

### 5. Service-layer function
Every MCP tool calls into a function in `server/src/services/<resource>.py`. Give the function signature the tool will call — the service layer owns the business logic, the tool layer only authenticates + marshals.

### 6. Response shape
JSON dict with explicit keys. Never return raw model objects without `.model_dump(mode="json")`. Never return encrypted secrets, Fernet ciphertext, or plaintext key material (the signing guard blocks that at the wallet layer, but your tool should not be a bypass).

### 7. Error shape
Match the existing error envelope:
```json
{"error": true, "code": "ERROR_CODE", "message": "human-readable", "suggestion": "what to try", "correlation_id": "<uuid>"}
```
Standard codes: `AUTH_INVALID_API_KEY`, `CHAIN_NOT_SUPPORTED_IN_V1`, `WALLET_NOT_FOUND`, `SDK_ERROR`, `VALIDATION_ERROR`.

### 8. Example invocation + response
One positive example with realistic values (e.g. a real Base contract address, a non-zero amount). Helps the implementer quickly validate shape.

## Safety-layer reminders

When drafting tools that touch signing, wallets, or execution, state explicitly in the spec's description / preconditions:

- Signing flows MUST route through `wallet_manager.sign()` — the hard guard there refuses non-1inch payloads (see `.claude/rules/wallet-presentation.md`).
- New tools that take a wallet address MUST check `backup_confirmed_at` before executing anything that moves real funds.
- New tools that take a chain_id MUST default to Base Sepolia (84532); mainnet (8453) requires an explicit user signal.

## Output

Do NOT create any files. Output the spec as markdown with a JSON schema block for parameter + response + error, ready for review or paste into `server/src/mcp/tools.py`. End with a checklist:

```
[ ] Service-layer function exists in server/src/services/<resource>.py
[ ] Pydantic models for request + response defined
[ ] Route added to server/src/api/routes/<resource>.py (auth dependency wired)
[ ] Route registered in server/src/api/router.py
[ ] MCP tool registered in server/src/mcp/tools.py via register_tool()
[ ] Tests added in server/tests/unit/ and/or server/tests/integration/
[ ] Documentation updated (CLAUDE.md if user-facing, docs/contributing.md if dev-facing)
```
