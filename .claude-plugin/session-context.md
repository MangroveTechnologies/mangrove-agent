# Claude Code plugin paths

The SessionStart hook injects the complete shared Sage instructions above. This
section only adapts paths and namespaces for the installed Claude Code plugin.

- Repository-relative instruction and terminal-script paths are rooted at
  `${CLAUDE_PLUGIN_ROOT}`. The user runs secret-handling scripts in their private
  terminal; do not run them through the assistant.
- Durable config and wallet state live at `${MANGROVE_AGENT_HOME}`, outside the
  versioned plugin. Do not rewrite or delete that state during plugin updates.
- Discover tools in the `mcp__plugin_mangrove-agent_mangrove-agent__` namespace;
  use the exact names advertised by the active host.

Client-only skills: `trading-bot`, `setup-kraken`, `connect-kraken`, `setup-robinhood`.
