# MCP contract guards (B7)

Run from the repository root with the locked server dependencies installed:

```bash
python -B scripts/mcp_contract.py
```

This is the required **offline** CI check. It does not claim that a receiver is
running or that every provider/tool has passed live execution acceptance.

## What is checked

- Actual FastMCP registration attempts (before the SDK silently ignores duplicate
  names), runtime names, and the independent discovery registry all agree.
- Parameter names, primitive types and argument requiredness agree. Authentication
  remains required for auth tools, but `api_key` is optional when sent in the
  local HTTP header. A false/omitted confirmation still refuses execution.
- Complete runtime JSON input schemas and access labels match a reviewed local
  snapshot. Nested constraints, defaults, enum values and nullability count;
  presentation titles/descriptions do not. The compact REST parameter catalog
  does not represent every JSON Schema feature, so it is checked as a projection
  while the snapshot guards the full schema. This does not create a second
  schema implementation or change the published REST response shape.
- Healthy x402-wrapper and degraded registrations expose the same schema/access
  contract. The local demo advertises the configured payment network.
- Every local tool is classified for upstream pricing or has an explicit reason
  for exclusion. Empty price bindings have explicit unknown-price reasons; they
  are never treated as zero. Bound IDs resolve in the receiver snapshot, with
  REST, skill and proxy namespaces preserved. Hidden REST meters need not be
  advertised as callable receiver tools.
- Existing MCP functions that call SDK clients directly are recorded in a scoped
  exception list with reasons and AST fingerprints. New adapters, changed
  exceptions and obsolete exceptions fail. This prevents silently expanding or
  changing the existing duplication; it does not claim to prove arbitrary
  interprocedural business-logic equivalence. Shared services and route delegation
  need no direct-SDK exception. The separate MangroveAI business-SSOT consolidation
  is deferred; B7 does not migrate strategy/reference/benchmark/verdict algorithms.

Registration runs in a fresh child process using repository **test** configuration.
It does not start an application lifespan, scheduler, database or wallet. Audit
hooks prohibit network connections, database connections, subprocesses and file
mutations during collection. Only the external facilitator initialization is
substituted; the healthy registration still uses the actual x402 payment wrapper.
The collector is an accidental-side-effect guard, not a sandbox for hostile Python.

## Reviewed artifacts

All three files live under `scripts/contracts/`:

| File | Purpose |
| --- | --- |
| `local.json` | Complete generated local input schemas, access labels and billing bindings; no amounts |
| `upstream.json` | Public receiver input schemas and billing IDs, captured through anonymous MCP discovery; no amounts or descriptions |
| `policy.json` | Reasons for non-upstream/unknown pricing and fingerprinted direct-SDK exceptions |

The initial receiver capture contains 84 callable tools and 99 billing IDs.
Its source URL and retrieval timestamp are recorded in the file. It is an
observation of that running receiver, **not** an assertion that it loaded a
particular Git commit or represents every hosted environment. Initial agent
baseline: 106 tools, 69 pricing bindings (57 nonempty, 12 unknown), 37 explicitly
excluded tools and 55 remaining direct-SDK adapters after the two market fixes.
Counts describe this baseline; they are not hardcoded runtime limits.

## Live receiver comparison

Use an explicit destination, for example the local development receiver:

```bash
python -B scripts/mcp_contract.py --upstream-url http://127.0.0.1:5002/mcp/
```

This requests **only** anonymous `tools/list` with the opt-in pricing metadata.
No SDK/payment client, credentials, wallet identity or payment headers are used.
HTTPS is required except for loopback HTTP; credential-bearing URLs, query
strings, fragments, redirects and compressed responses are rejected. Pages,
entries, bytes, socket waits and elapsed duration are bounded. A 401/402, timeout,
malformed response or unavailable catalog fails the check; there is no payment
retry or fallback to a cached PASS.

A live comparison checks bound billing IDs and changes/removals in receiver
schemas whose tool names overlap the reviewed local tool set. It compares the
receiver's schema to its **own** prior schema, not to the local wrapper schema:
local argument names, authentication arguments and aggregation can intentionally
be different. Added unrelated receiver tools do not fail. Missing billing IDs
fail even when the corresponding REST route is intentionally hidden from MCP.

This guards catalog compatibility, not the semantic correctness of every SDK
route mapping. Renamed, differently named or private/context-dependent receiver
capabilities need explicit adapter review; a names-only diff cannot infer those
relationships. No live provider execution or payment is performed by this check.

The offline snapshot cannot notice receiver changes after capture. Run the live
comparison during coordinated receiver/agent changes and before rollout against
each intended environment. CI stays offline so ordinary PRs need neither private
repository access nor a healthy external receiver.

## Intentional contract changes

1. Review the changed capability and preserve local authentication, confirmation,
   custody and spending protections. Prefer a shared route/service when updating
   an existing direct SDK adapter.
2. Update only the relevant `policy.json` classifications or exceptions. Remove
   an exception after migrating its function to shared logic. A changed fingerprint
   requires review of validation, error handling, pagination and payment behavior;
   it must not be refreshed automatically by CI.
3. Generate the local snapshot explicitly after the change:

   ```bash
   python -B scripts/mcp_contract.py --refresh-local
   ```

4. For an intentional receiver change, inspect its compatibility impact and
   explicitly refresh the receiver snapshot:

   ```bash
   python -B scripts/mcp_contract.py --upstream-url http://127.0.0.1:5002/mcp/ --refresh-upstream
   ```

   This replaces the observed receiver contract, so reviewing its diff is essential.
   Refresh does not prove compatibility with other deployed receiver versions.
5. Review generated diffs and run the default check, targeted regression tests and
   the full normal CI suite. No refresh occurs during the default check or CI.

To inspect a function's current AST fingerprint locally, import
`direct_sdk_tools` from `scripts/mcp_contract.py` and pass the text of
`server/src/mcp/tools.py`. The function returns `{tool_name: sha256}`. There is
intentionally no bulk command that silently accepts every exception change.

## Focused runtime corrections

B7 corrects catalog argument requiredness and array type notation without relaxing
any execution gate. The local demo network is read from configuration.
`get_market_data` and `get_ohlcv` delegate to their existing REST handlers, after
local MCP authentication, so safe upstream error handling is shared. Domain/payment
error codes and correlation IDs are preserved. Business algorithms and upstream
payment prices remain unchanged.
