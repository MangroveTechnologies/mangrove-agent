# Restricted user chat

`./scripts/chat.sh` starts a Claude Code session that can talk to your Mangrove agent and nothing else. It is the user-facing way to use the agent with Claude: the session has no shell, file, web or subagent tools, so the assistant can act only through the agent's MCP tools.

## Starting a session

Run `./scripts/setup.sh` first to configure and start the agent. Setup finishes without opening Claude. Then run `./scripts/chat.sh` whenever you want a restricted session. Both scripts work when invoked by absolute path from any directory. Rerunning setup preserves the selected configuration and port. Each installation has its own chat working directory at `agent-data/chat`, and keys and databases are never copied between users. `./scripts/setup.sh --foreground` remains available for server diagnostics.

`chat.sh` accepts no arguments, so no command-line flag can re-enable a tool it removed.

## What the launcher sets up

The agent's shared policy comes from `SAGE.md` and the three mandatory local rule documents. Because the assistant's file-reading tools are disabled, the launcher renders their complete contents and passes them through a private temporary `--append-system-prompt-file`. If any piece of policy is missing, the launcher refuses to start. See [other AI clients and the shared instruction source](clients.md).

The launcher runs Claude Code with:

- `--tools ""`, which removes every built-in tool.
- `--strict-mcp-config` and an inline MCP configuration, so only the `mangrove-agent` server is loaded.
- `--setting-sources ""`, which excludes user, project and local settings.
- `--no-chrome`, which disables browser integration.
- `--permission-mode default`, which keeps manual permission prompts.
- A private temporary settings file that turns automatic memory off, denies the shell, file, search, web, skill and subagent tools (`Bash`, `Read`, `Write`, `Edit`, `Glob`, `Grep`, `NotebookEdit`, `Agent`, `Task`, `Skill`, `WebFetch`, `WebSearch`, `LSP`), allows the Mangrove MCP namespace (`mcp__mangrove-agent__*`), and disables permission bypass mode.
- `ENABLE_TOOL_SEARCH=false` in the environment, so Mangrove's tools are loaded directly rather than through the client's tool search.

Model-invoked Skill tools stay denied; slash-command availability is separate from the shell and file permissions. Managed administrator policy still applies on top of all this.

## Approvals

The launcher uses Claude's default permission mode. `marketplace_submit` publishes `anthropic/requiresUserInteraction: true`, so Claude asks for confirmation inline before the agent signs or pays, even though the MCP namespace is allowed. Repository sessions also default to manual permission prompts; an explicit CLI mode or administrator policy can override that default. This does not bypass Claude's security checks or guarantee that every host policy permits a preparation step. The agent still requires a valid, unexpired approval and `confirm=true`, and purchase terms and spending limits stay enforced in code.

## Credentials and hooks

The local MCP credential reaches the agent through the same header helper that `setup.sh` registers (`setup_support.py headers`), never through command-line arguments or the generated settings. The temporary settings contain only the policy and the hook paths, and are deleted when chat exits. Wallet signing and approvals stay inside the local agent, and MangroveMarkets still owns marketplace schemas, offer records, status and authorization.

The launcher wires three hooks with absolute, shell-quoted paths derived from the installation, and refuses to start if any is missing:

- `block-wallet-secrets.sh` on `UserPromptSubmit` rejects a message that contains a private key or recovery phrase.
- `block-wallet-secrets.sh` on `PostToolUse` checks tool output for key material. It is defense in depth after execution, not a replacement for tool permissions.
- `preflight-swap.sh` on `PreToolUse` for `execute_swap` stops a swap whose wallet holds none of the input token.

Developer sessions in the repository use the quoted `CLAUDE_PROJECT_DIR` path for the same hooks, so changing directories during a session does not break them.

## What this does and does not protect

This is an enforced configuration for this launcher, not a security boundary against the machine owner. Someone can edit their own installation or start an unrestricted Claude session; a bare `claude` run is the developer workflow. No client configuration, local SQLite edit or prompt instruction can override MangroveMarkets' API scope checks and wallet-signature verification. Controlling an entire client environment takes a centrally managed client or an isolated hosted interface.

## Checking that it works

After setup, run `./scripts/chat.sh` to open a fresh session.

1. Type `/mcp` to inspect the connection. Built-in slash commands remain available; the launcher does not use `--disable-slash-commands`, because that also disables `/mcp`.
2. Ask the agent for its status. It should call the free `status` tool and answer from the result.
3. Ask it to "Read the local SQLite database." The session has no shell or file tool, so it should either query the agent's own tools or say that it cannot inspect local files.

Claude's documented controls are in the [CLI reference](https://code.claude.com/docs/en/cli-reference) and the [hooks reference](https://code.claude.com/docs/en/hooks). Validate the interaction with your installed Claude Code version before you distribute the client to others.
