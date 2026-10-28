# Server-owned strategy workflows

MangroveAI is the source of truth for strategy authoring and backtesting. This agent
is a client. Discover the live MangroveAI tools through the MCP proxy, then call
`load_skill` with `skill="strategy-composition"` before a strategy build, or
`skill="backtesting"` before a backtest. Follow the returned server instructions
and load any named resources through that same tool. Check the returned required
capabilities against the advertised catalog. Tools listed in `optional_tools` are
not prerequisites: use them only when advertised, and follow the server instructions
for their alternatives. If a required tool or the skill cannot
be loaded, report the unavailable capability; do not substitute local templates,
remembered instructions, reference seeds, or an autonomous candidate generator.

Use the server's strategy ids with its tools. Local `agent_*` strategy ids belong
to existing execution records and are not interchangeable with server draft ids.
Do not copy, cache to disk, or regenerate server skill files inside this client.
Wallet custody, credential handling and local execution monitoring remain client
responsibilities; follow wallet-presentation.md and risk-management.md for those.

For listing or managing saved strategies, load the server's `strategy-management`
skill and use MangroveAI `list_strategies` and `get_strategy`. API-key, OAuth and
x402 connections all use MangroveAI for saved definitions; platform inventory and
platform management belong to the separate platform experience. Do not query the
platform or use platform record IDs as a fallback for missing MangroveAI records.
Keep server results separate from local execution records. An empty MangroveAI
result says nothing about records in another service. Local `status` strategy counts describe this agent's
execution records, not the signed-in account's server inventory. Never substitute
a shell or SQLite read for a server strategy result, or infer an identity mismatch
from different counts. If access is unavailable, report it; do not change access
mode, server destination, or account to get more records without the user's request.

For a new sweep, load the server's `sweeps` skill. To find or inspect existing
sweep results, load `sweep-results`. Sieve screening uses the discovered
`screen_candidates` tool and the server's `backtesting` guidance. Use the live
schemas and prices; do not build candidates, calculate sweep sizes, or duplicate
screening logic locally. Show the server's estimated run count and obtain the
user's agreement before launching compute. An uncertain launch must not become
a new paid launch; preserve its original operation identity for recovery.
