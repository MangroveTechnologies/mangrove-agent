---
name: sweep
description: Search a strategy parameter space and review measured results using the connected MangroveAI server's sweep tools.
---

# Search a parameter space

Read the synced server workflow at
`${CLAUDE_PLUGIN_ROOT}/.claude/skills/michael/sweeps/SKILL.md`, then its result-reading
guidance at `${CLAUDE_PLUGIN_ROOT}/.claude/skills/michael/sweep-results/SKILL.md`.

Discover the current MCP tools and use the server's schemas and prices. The old
agent Oracle experiment wrappers and their request templates are retired; do not
construct requests from those templates or translate the server contract locally.
If a required capability is unavailable, report that limitation.

A sweep produces server-owned records. Read and verify the winning result before
proposing further work. Local execution uses `agent_` tools and local strategy IDs;
do not pass a server strategy ID to a local execution tool. Wallet signing and
spending controls stay in the agent.
