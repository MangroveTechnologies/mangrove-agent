# Restricted user chat

Run `./scripts/setup.sh` to configure and start the agent. Setup finishes without
opening Claude. Then run `./scripts/chat.sh` when you want a restricted session.
Both scripts can be invoked by absolute path from any directory. Rerunning setup
preserves the selected configuration and port. Each installation has its own
chat working directory under `agent-data/chat`; keys and databases are not copied
between users. `--foreground` remains available for server diagnostics.

The launcher removes all built-in Claude tools with `--tools ""`, disables browser integration, loads only the agent MCP server with
`--strict-mcp-config`, and excludes user/project/local settings with
`--setting-sources ""`. Its explicit settings deny file, shell, search, skill and
subagent tools, allow the Mangrove MCP namespace, and disable permission bypass.
MCP tool search is disabled so Mangrove's tools are loaded directly. It accepts
no arbitrary CLI flags that could re-enable tools. Model-invoked Skill tools
remain denied; slash-command availability is separate from shell/file permissions. Managed administrator policy
still applies.

The launcher uses Claude's default permission mode. `marketplace_submit` publishes
`anthropic/requiresUserInteraction: true`, so confirmation is requested inline
before signing or paying, including when the MCP namespace is allowed. Preparation
is described as preview-only. Repository sessions also default to manual permission
prompts; an explicit CLI mode or administrator policy can override that default.
This does not bypass Claude's security checks or guarantee that every host policy
will permit preparation. The agent still requires a valid, unexpired approval and
`confirm=true`; purchase terms and spending limits remain enforced in code.

The local MCP credential is supplied through the existing authenticated header
helper, not command-line arguments or the generated profile. The temporary
profile contains only policy and hook paths and is removed when chat exits.
Wallet signing and approvals stay inside the local agent. Markets still owns
marketplace schemas, offer records, status and authorization.

Project hooks now use the quoted `CLAUDE_PROJECT_DIR` path, so changing directories
during a development session does not break them. Restricted chat uses absolute,
shell-quoted hook paths derived from its installation and refuses to launch if a
required hook is missing. UserPromptSubmit checks key pastes; PostToolUse is
defense in depth after execution, not a replacement for tool permissions.

This is an enforced configuration for this launcher, not a security boundary
against the machine owner. Someone can edit their own installation or start an
unrestricted Claude session. Bare `claude` remains the developer workflow. No
client configuration, local SQLite edit, or prompt instruction can override
Markets' API scope checks and wallet-signature verification. A centrally managed
client or isolated hosted interface is needed to control the entire client
environment.

## Test

After setup, run `./scripts/chat.sh` to open a fresh session. Type `/mcp` to
inspect the connection. Built-in slash commands remain available: the launcher
does not use `--disable-slash-commands`, which also disables `/mcp`. Ask “Have I made any marketplace offers?” The new server history tools
use a free wallet-signed read, so review and approve that authorization when
prompted. This is not a payment. The relevant wallet is required. API-key mode also checks `execution:read`; wallet-only mode proves identity by signature. Discovery does not disclose private offers.

Then ask “Read the local SQLite database to answer that.” The session must have
no shell or file tool available. It should query the server or say it cannot
inspect local files. Test User A and User B separately. Offer history belongs to
the proven wallet; public listings remain readable by other authorized users.

The automated launcher tests capture the actual process arguments with a fake
CLI, exercise hooks from paths containing spaces and quotes, and reject policy
override flags. They do not invoke a paid model session. Validate the interaction
with your installed Claude Code before distributing the client.

Claude's documented controls:
[CLI tool and MCP restrictions](https://code.claude.com/docs/en/cli-reference),
[hook project paths](https://code.claude.com/docs/en/hooks).
