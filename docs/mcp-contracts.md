# MCP Contracts

The agent serves one MCP endpoint at `/mcp` (Streamable HTTP). The tools behind it come from
three owners, and each owner's contract is handled differently. This page describes who owns
what, how remote tools reach a client, and the offline check that keeps the agent's own tools
from drifting.

Run the `list_tools` tool, or call `GET /api/v1/agent/tools`, for the live catalog. This page
does not list every tool because the remote catalogs change without a change to the agent.

## Tool ownership

| Owner | Examples | How the agent serves them |
|-------|----------|---------------------------|
| The agent | `status`, `list_tools`, `set_agent_name`, `create_wallet`, `import_wallet`, `list_wallets`, `execute_swap`, `cex_*`, `marketplace_prepare`, `marketplace_submit`, `x402_spend_status`, `x402_spend_reset`, `agent_*` | Registered locally in `server/src/mcp/` |
| MangroveMarkets | `marketplace_*`, `dex_*`, `oneinch_*`, approved `wallet_*` tools | Discovered from the configured Markets MCP server; wrapped with local authentication and wallet coordination |
| MangroveAI | Strategies, backtests, signals, market and on-chain data, knowledge base, trade history, `load_skill`, `discover_tools` | Discovered from MangroveAI's MCP server and forwarded unchanged |

### Local tools

Local tools exist because they need things only the user's machine has: keys, wallets, the
local database, the scheduler and venue connections.

- Discovery and identity: `status`, `list_tools`, `set_agent_name`.
- Wallets: `create_wallet`, `import_wallet`, `list_wallets`. A secret is never an argument
  or a result; tools exchange a `vault_token`.
- DEX execution: `execute_swap` (needs `confirm=true` and an explicit `slippage_pct` of at
  most 0.0025).
- Kraken: `cex_status`, `cex_connect_kraken`, `cex_balances`, `cex_validate_order`,
  `cex_sync_fills`.
- Marketplace approvals: `marketplace_prepare`, `marketplace_submit`.
- Spend budget: `x402_spend_status`, `x402_spend_reset`.
- Robinhood venue: `agent_robinhood_status`, `agent_configure_execution_venue`,
  `agent_configure_strategy_market`, `agent_list_venue_orders`,
  `agent_reconcile_venue_orders`, `agent_cancel_venue_order`.
- Paper scheduling: `agent_start_paper_schedule`, `agent_list_paper_schedules`,
  `agent_get_paper_schedule`, `agent_pause_paper_schedule`,
  `agent_reconcile_paper_schedule`.

ID ownership follows each tool's contract. `agent_start_paper_schedule.strategy_id` is a
MangroveAI strategy UUID sent to `execution.paper_start`; the returned `id` is the local
schedule ID, passed as `schedule_id` to schedule-management tools. Do not treat every ID on an
`agent_` tool as local.

`hello_mangrove`, an x402 demo, is registered and callable but is hidden from `list_tools`.

Every local tool except `status`, `list_tools` and `hello_mangrove` requires the agent's API
key. A client sends it as the `X-API-Key` header, or as the optional `api_key` argument.

### MangroveMarkets tools

The agent reads the Markets catalog from `MANGROVEMARKETS_BASE_URL` and exposes tools whose
names begin with `marketplace_`, `dex_`, `oneinch_` or `wallet_` (except the two local
`marketplace_` tools above).

- Each exposed tool gains an `_agent` argument object holding transport controls: `api_key`,
  `wallet_address` and `operation_id`. These are never forwarded as Markets arguments.
- Wallet-owned actions (ownership mode) only prepare. Calling the tool returns a local
  approval preview; the signing and payment happen only after the user approves it through
  `marketplace_submit`. The `ownership_proof` and `payment` arguments are hidden from callers.
- Read tools are fetched with the configured API key, or paid per call from the payer wallet.
- Older agent-side wrappers for balances, portfolio, quotes and token reads are retired.
  Calling one returns `RETIRED_TOOL`; use the discovered Markets tool and its schema.

### MangroveAI tools

The agent reads MangroveAI's `tools/list` and calls tools with `tools/call`. It preserves the
server's names, descriptions, input and output schemas, annotations and result envelopes.
There are no local registrations, SDK adapters, price tables or fallback definitions for these
tools. When MangroveAI adds or changes a tool, the agent serves it without a code change.

- **Destination.** Derived from the configured MangroveAI API origin by replacing `/api/v1`
  with `/mcp/`. The destination must be HTTPS, or loopback HTTP.
- **Discovery.** `tools/list` is anonymous and never signs or pays. The result is cached for
  up to 60 seconds.
- **Progressive discovery.** `discover_tools` is a MangroveAI tool. Area taxonomy, selection
  by name or area, compact summaries and full-schema selection belong to that service; the
  agent forwards its schema, arguments and result without a second selector. Native
  `tools/list` remains the full catalog for MCP registration.
- **Calls.** Arguments are validated against the discovered input schema before sending. A
  configured API key is used when present. Otherwise the call goes through the agent's
  x402 payer wallet, with optional Sign-In-With-X identity in wallet mode. Public, free,
  read-only tools never authorize a payment. An API-key failure never switches to wallet
  payment.
- **Agent-owned controls.** Wallet selection, signing, the spend budget, payment receipts and
  uncertain-payment recovery stay in the agent. The local API key is not part of any remote
  tool schema and is never forwarded upstream.
- **Unavailable catalog.** If discovery fails, local tools and Markets tools remain
  available, `GET /api/v1/agent/tools` reports `mangroveai_status: unavailable`, and remote
  calls fail explicitly. The agent does not serve a stale catalog or fall back to an SDK.
- **Client refresh.** After server changes, reconnect the MCP client so it refreshes its own
  cached tool list.

Saved strategies are listed and read through MangroveAI's `list_strategies` and
`get_strategy`. The agent has no local strategy list or get tool, and no local strategy
status or evaluation tool. Local execution records are read through the REST routes in the
[API reference](api-reference.md).

### Price hints

`list_tools` appends a plain-text price hint to tools that have a published upstream price
binding. Today those are the four `agent_*_paper_schedule` tools that call MangroveAI
(`start`, `get`, `pause` and `reconcile`). Prices are read anonymously from MangroveAI and cached
for five minutes. When a price cannot be read, the hint says it is unavailable and not to
assume the tool is free; prices are never guessed.

## Offline contract check

`scripts/mcp_contract.py` guards the agent-owned tools. Run it from the repository root with
the locked server dependencies installed:

```bash
python -B scripts/mcp_contract.py
```

CI runs this check on every pull request. It is offline: it does not claim that any receiver
is running or that a tool has passed live execution.

### What it checks

- The tools FastMCP registers, the names it ends up with, and the independent discovery
  registry in `server/src/mcp/registry.py` all agree. Registration attempts are recorded
  before the SDK silently ignores duplicate names.
- Local discovery exposes titles and explicit read-only, destructive, idempotent
  and open-world hints, tested through the SDK HTTP transport in
  `server/tests/unit/test_mcp_metadata.py`. These hints describe possible tool
  effects; they do not authorize calls, assert zero cost, replace user approval,
  or enforce Mangrove risk policy. Forwarded receiver hints and marketplace
  interaction metadata remain intact during price enrichment.
- Parameter names, primitive types and required flags agree between the runtime schema and
  the registry. Authentication stays required for auth tools, but `api_key` is optional
  because a client may send the header instead. An omitted or false confirmation still
  refuses execution.
- The complete runtime input schemas and access labels match a reviewed snapshot,
  `scripts/contracts/local.json`. Nested constraints, defaults, enums and nullability count;
  titles and descriptions do not.
- Registration with the x402 payment wrapper and registration when the payment facilitator
  is unreachable expose the same schema and access contract. The demo tool advertises the
  configured payment network.
- Every local tool is classified: it either has a price binding in
  `server/src/mcp/pricing_bindings.py` or an explicit reason in `policy.json` for having
  none. Bindings with no known price need a stated reason and are never treated as zero.
  Bound billing IDs must exist in the receiver snapshot.
- No `@server.tool()` function in `server/src/mcp/tools.py` calls an SDK client or HTTP
  library directly unless `policy.json` lists it with a reason and a fingerprint of its
  code. New direct adapters, changed ones and obsolete entries all fail. The list is
  empty because tools delegate to shared services.

Registration runs in a fresh child process with the repository's test configuration. It does
not start the app lifespan, the scheduler, the database or a wallet. Audit hooks block network
connections, database connections, subprocesses and file writes during collection. This is a
guard against accidental side effects, not a sandbox for hostile code.

### Reviewed artifacts

All three files live in `scripts/contracts/`:

| File | Purpose |
|------|---------|
| `local.json` | Generated local input schemas, access labels and price bindings; no amounts |
| `upstream.json` | Public MangroveAI input schemas and billing IDs from an anonymous `tools/list`, with its source URL and retrieval time; no amounts or descriptions |
| `policy.json` | Reasons for tools without upstream pricing, reasons for unknown prices, and fingerprinted direct-SDK exceptions |

`upstream.json` is an observation of one running receiver at one time. It does not assert
that the receiver ran a particular commit or represents every hosted environment.

### Live receiver comparison

Pass an explicit destination to compare the snapshot with a running receiver:

```bash
python -B scripts/mcp_contract.py --upstream-url http://127.0.0.1:5002/mcp/
```

This sends only an anonymous `tools/list` that asks for pricing metadata. It uses no SDK or
payment client, credentials, wallet identity or payment headers. HTTPS is required except for
loopback HTTP. Credential-bearing URLs, query strings, fragments, redirects and compressed
responses are rejected, and pages, entries, bytes and elapsed time are bounded. A 401, 402,
timeout, malformed response or unavailable catalog fails the check; there is no payment retry
and no fallback to a cached pass.

The comparison fails when a bound billing ID is missing, or when a receiver schema changed or
disappeared for a tool whose name also exists locally. It compares the receiver with its own
earlier snapshot, not with the local wrapper schema, because local argument names and
authentication arguments can differ on purpose. Unrelated new receiver tools do not fail it.

It guards catalog compatibility, not the meaning of every mapping. Renamed or private
receiver capabilities need an explicit review. The offline check cannot notice receiver
changes after the snapshot was taken, so run the live comparison during coordinated receiver
and agent changes and before rollout to each environment.

## Changing a contract on purpose

1. Review the change. Keep local authentication, confirmation, custody and spending
   protections intact. Prefer a shared service over a direct SDK call in a tool.
2. Update only the relevant `policy.json` entries. Remove an exception once its function uses
   shared logic. A changed fingerprint needs a review of validation, error handling,
   pagination and payment behavior; CI never refreshes it.
3. Regenerate the local snapshot explicitly:

   ```bash
   python -B scripts/mcp_contract.py --refresh-local
   ```

4. For an intentional receiver change, inspect the compatibility impact, then refresh the
   receiver snapshot:

   ```bash
   python -B scripts/mcp_contract.py --upstream-url http://127.0.0.1:5002/mcp/ --refresh-upstream
   ```

   This replaces the observed receiver contract, so review its diff. It does not prove
   compatibility with other deployed receiver versions.
5. Review the generated diffs, then run the default check, targeted regression tests and the
   full test suite. Neither the default check nor CI refreshes a snapshot.

`--refresh-local` and `--refresh-upstream` cannot be combined, and `--refresh-upstream`
requires `--upstream-url`. To inspect a function's current fingerprint, import
`direct_sdk_tools` from `scripts/mcp_contract.py` and pass it the text of
`server/src/mcp/tools.py`; it returns `{tool_name: sha256}`. There is deliberately no command
that accepts every exception change at once.
