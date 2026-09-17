# Contributing

Guide for extending the scaffold — adding endpoints, services, and MCP tools on top of what ships.

## Service layer pattern

Routes (`server/src/api/routes/`) and MCP tools (`server/src/mcp/tools.py`) both call shared services in `server/src/services/`. Never duplicate business logic — if the same capability needs to be reachable from both REST and MCP, it lives in a service and both call in.

## Adding a free endpoint

1. Create the route in `server/src/api/routes/{resource}.py`.
2. Create the service in `server/src/services/{resource}.py`.
3. Register the route in `server/src/api/router.py` under `api_router`.
4. Register the MCP tool in `server/src/mcp/tools.py` via `register_tool()`.
5. Write tests in `server/tests/test_{resource}.py`, mirroring the `src/` structure.

## Adding an auth-gated endpoint

Same five steps, plus:

- Call `validate_api_key()` in the route handler.
- Call `has_valid_api_key()` in the MCP tool before proceeding.

## Adding an x402 payment-gated endpoint

Register the route under `x402_router` (not `api_router`). See `server/src/api/routes/hello_mangrove.py` for the canonical pattern.

## Testing

MCP changes must also pass `python -B scripts/mcp_contract.py` from the repo root.
This offline guard checks registration, schemas, price bindings and existing
direct-SDK adapter exceptions. See [MCP contracts](mcp-contracts.md) for intentional
contract updates and opt-in live receiver comparison. Never refresh snapshots
automatically to make an unexplained failure disappear.

Tests live in `server/tests/` and mirror the `src/` layout. CI (`.github/workflows/ci.yml`) runs ruff + pytest on every PR — that's the authoritative invocation.

Test runs set `MASTER_KEY_PATH` to a test-scoped path; the resulting `agent-data-test/` directory is gitignored.

## Skills for extension work

Claude Code skills that help while extending the scaffold:

- **`/tool-spec <purpose>`** — drafts a complete MCP tool specification (name, access tier, parameters, service-layer binding, response shape, error envelope) ready to paste into `server/src/mcp/tools.py`.
- **`/check-alignment <change>`** — read-only review of a proposed change against `CLAUDE.md`, `trading-bot-workflow.md` (the 9 operating principles), `wallet-presentation.md`, `git-workflow.md`, and this file.
- **`/audit-security`** — focused security audit covering wallet + signing surfaces, MCP tool layer, REST routes, dependency CVEs, and git-history secret leaks. Read-only, writes a report to `docs/audits/`.
- **`/custom-signal <rule>`** — composes a custom entry/exit signal stack from atomic signals when a reference-strategy match isn't available.

## Synced MangroveAI copilot skills

The judgement skills under `.claude/skills/michael/` (backtesting, strategy-composition,
strategy-management, sweeps, sweep-results, market-intelligence, knowledge-graph, portfolio)
are generated from MangroveAI's copilot, which is their source of truth. Do not edit them
here — CI (`--verify-manifest`) fails on a hand-edited copy. To pick up upstream changes:

```bash
python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI --ref origin/main
python scripts/sync-michael-skills.py --source ~/mangrove-workspace/MangroveAI --ref origin/main --check
```

The script maps Michael's tool names to this agent's MCP tools (`TOOL_MAP`), annotates
passages that need a tool this agent does not have yet (`UNAVAILABLE`), and rewrites
Michael-runtime-only passages (`PATCHES`, anchored on upstream wording — a changed anchor
fails the sync loudly). When a new agent tool fills a gap, move its entry from
`UNAVAILABLE` to `TOOL_MAP` and re-sync. The script's docstring shows how to sync from a
`gh api` tarball when there is no local checkout.

## Git workflow

See `.claude/rules/git-workflow.md`. Feature branch off `main`, one concern per PR, CI must pass.
