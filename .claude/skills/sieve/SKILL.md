---
name: sieve
description: Screen candidate strategies before paying for full backtests. Use when the user asks which candidates are worth testing; screening is not a performance result.
---

# Screen candidates

Read the synced MangroveAI guidance at
`${CLAUDE_PLUGIN_ROOT}/.claude/skills/michael/backtesting/SKILL.md`.
Discover `screen_candidates` through the connected MCP server and use its current
input schema, limits and price. Do not use the retired agent `sieve_score` wrapper
or substitute a local calculation when discovery fails.

Keep screening scores separate from measured backtest performance. Report the
actual response and its uncertainty. Use the server's backtest workflow for
server-owned strategies. For a strategy created locally, use
`agent_backtest_strategy` with its local ID; never interchange strategy IDs.
