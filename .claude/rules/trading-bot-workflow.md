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
