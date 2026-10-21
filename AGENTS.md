# Mangrove Agent - Codex and compatible clients

Before responding, read `SAGE.md` in this repository and follow its startup
contract, including the three mandatory rule documents. Resolve their paths
from the repository root. `SAGE.md` is the single shared source for the agent
persona, project context, and operating instructions; do not maintain a separate
copy here. Grok Build and other clients that discover `AGENTS.md` use this same
entry point. If another native entry point already loaded these sources, do not
load them twice.

For clients without local file access, use the complete generated bundle at
`docs/clients/sage-instructions.md`, as described in `docs/clients.md`. A pointer
alone does not load its target, and instructions alone do not connect MCP tools.
