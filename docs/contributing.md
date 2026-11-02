# Extending the Agent

This guide explains how the agent's code is organized and how to add a route, an MCP tool, a
database migration or a configuration key. For the pull request process, code style and
local checks, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## What belongs in the agent

The agent keeps only what has to run on the user's machine: wallet custody and signing, the
local database, the scheduler, venue connections and the approval and spending controls
around them. Strategies, backtests, signals, market data, on-chain data and the knowledge
base belong to MangroveAI, which the agent forwards over MCP. Do not add a local copy of a
MangroveAI capability. See [MCP contracts](mcp-contracts.md) for the ownership split.

## Service layer pattern

REST routes (`server/src/api/routes/`) and MCP tools (`server/src/mcp/`) are thin adapters.
Both call shared services in `server/src/services/`, so a capability reachable both ways has
one implementation. For example, `POST /api/v1/agent/wallet/create` and the `create_wallet`
tool both call `wallet_manager.create_wallet`.

Some read-only routes (for example the DEX venue and gas-price routes) call the SDK client from
`server/src/shared/clients/mangrove.py` directly. Create a service when the capability adds
orchestration, state or safety checks the SDK does not provide.

## Adding a REST route

1. Create `server/src/api/routes/{resource}.py` with an `APIRouter`. For an authenticated
   route, set `dependencies=[Depends(require_api_key)]` on the router, as the existing route
   files do.
2. Put the logic in `server/src/services/{resource}.py` and call it from the handler.
3. Register the router in `server/src/api/router.py`. Routes under `/api/v1/agent` are
   included on `agent_router`.
4. Write tests under `server/tests/`.

An x402 payment-gated route goes on `x402_router` instead, which serves `/api/x402`, and also
needs an entry in the route table in `server/src/app.py`'s `_setup_x402`. See
`server/src/api/routes/hello_mangrove.py` for the pattern.

## Adding an MCP tool

1. Write or reuse the service function.
2. Register the tool in `server/src/mcp/tools.py`, or in a new module whose `register(server)`
   function is called from `register()` there, as `broker_tools.py` and `paper_tools.py` are.
   Use `@server.tool()`.
3. Authenticate with `_require(api_key)` at the top of the tool and return `_auth_error()` on
   failure. Accept `api_key` as an optional argument so a client can send the `X-API-Key`
   header instead. Free discovery tools such as `status` skip this check.
4. Describe the tool in the discovery registry with `register_tool(ToolEntry(...))`, listing
   every parameter and an access tier of `free`, `auth` or `x402`.
5. Return errors in the standard shape: `_handle_agent_error` turns an `AgentError` into the
   JSON error body.
6. Gate anything that moves money or changes live state behind an explicit `confirm`
   argument that defaults to false.
7. Update the contract artifacts. Every local tool must have a price binding in
   `server/src/mcp/pricing_bindings.py` or an entry with a reason in
   `without_upstream_pricing` in `scripts/contracts/policy.json`. Then regenerate
   `scripts/contracts/local.json`. See [MCP contracts](mcp-contracts.md).

The `/tool-spec <purpose>` skill drafts a specification for a new tool before you write it.

## Adding a database migration

Schema changes are SQL files in `server/src/shared/db/migrations/`, named
`NNN_description.sql`. `init_db()` applies unapplied files in filename order at startup and
records each in the `_migrations` table. Use the next unused number, and never edit a
migration that has shipped; add a new one.

## Adding a configuration key

Configuration lives in per-environment JSON files in `server/src/config/`. A key must be
declared in `server/src/config/configuration-keys.json` as `required` (startup fails without
it), `optional` (loaded when present) or `full_app_keys` (validated only when present). Add it
to `local-example-config.json` and the other environment files as appropriate, and document it
in the [configuration guide](configuration.md).

## Testing

Tests live in `server/tests/`, split into `unit/`, `integration/` and `e2e/`, with a few
top-level files for the auth, health, MCP and x402 paths. Run them from `server/`:

```bash
cd server && ENVIRONMENT=test pytest tests/ -v
```

CI (`.github/workflows/ci.yml`) is the authoritative list of checks. It lints `server/src/`
with ruff, verifies the shared agent instructions, runs the offline MCP contract check and
runs the test suite. A change to an MCP tool must pass `python -B scripts/mcp_contract.py`
from the repository root; never refresh a snapshot just to make an unexplained failure
disappear.

The test configuration (`server/src/config/test-config.json`) uses an in-memory database and
writes a test master key under `agent-data-test/`, which is gitignored. The opt-in live swap
tests in `server/tests/e2e/` are skipped unless the environment variables named in the
[README](../README.md#tests) are set.

## Contributor skills

The repository ships Claude Code skills that help while changing the agent. Run them from a
Claude Code session in the repository:

- `/tool-spec <purpose>` drafts a complete MCP tool specification: name, access tier,
  parameters, service-layer binding, response shape and error envelope.
- `/check-alignment <change>` reviews a proposed change, read-only, against `SAGE.md`, the
  rules in `.claude/rules/` (trading workflow, wallet presentation, risk management) and this
  guide.
- `/audit-security` runs a read-only security audit covering wallet and signing surfaces, the
  MCP tool layer, REST routes and dependencies. It writes its report to
  `agent-data/audits/`, which is local and gitignored.

The other skills in `.claude/skills/` (`trading-bot`, `setup-kraken`, `connect-kraken`,
`setup-robinhood`) guide users of the agent rather than contributors.

## Git workflow

Branch off `main`, make one change per pull request, and merge only after CI passes. Do not
commit or push to `main` directly; in a Claude Code session in this repository, a hook blocks
commits and pushes to `main`. When working in a git worktree, run git as `git -C <path>` so
the hook can see the target branch.
