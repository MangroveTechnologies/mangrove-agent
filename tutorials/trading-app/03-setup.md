# Chapter 03 - Setup

*10 minutes. No trading funds required. This chapter uses only installation,
free discovery/status and naming.*

Goal: get the server running, connect your chosen AI client, verify an actual
free status call, and choose the agent's name. The [user guide](../../docs/user-guide.md)
is the common reference for Claude Code, Codex, Grok Build and Perplexity Computer.

## 1. Choose an installation environment

You can ask any of those command-capable clients to install the repository:

> Install https://github.com/MangroveTechnologies/mangrove-agent using its AI
> client instructions. Verify free status, then ask me what to name my agent.
> Do not create wallets, spend money or schedule trading.

Perplexity needs Computer and Grok needs Build. Their ordinary web/search
modes cannot install a server. An installation inside a hosted task runs there,
not automatically on your laptop; use a persistent host for unattended trading.

For manual setup, you need Git, Python 3.11+ and a bash-compatible shell. See
[SETUP.md](../../SETUP.md) for prerequisites. Claude and Node.js are only needed
for the optional Claude installation method, not the Python server itself.

## 2. Clone and start

Skip cloning if you already have an installation. Keep its config and database.
From the directory where you want the project:

```bash
git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent
```

```bash
cd mangrove-agent
```

```bash
./scripts/setup.sh --yes --no-mcp
```

On a fresh installation, this installs dependencies, creates a protected local
credential and starts the server in x402 mode with wallet setup deferred. It
creates no wallet and makes no payment. Existing access settings are preserved.
Use interactive `./scripts/setup.sh` if you want to choose API-key access instead.
Never put a key in chat or a literal command argument.

Setup prints the actual local address. Port 9080 is the default. To choose a
free port on a fresh installation:

```bash
BARE_PORT=9085 ./scripts/setup.sh --yes --no-mcp
```

Setup preserves the chosen port for later runs. It refuses an unrelated busy
port rather than claiming success. Re-running setup can restart the server
when code or config changed; do not do that merely to switch clients while
live strategies are running.

## 3. Verify through real tools

Ask the installing client to load all four shared policy sources. For a client
without native repository instruction loading, it reads the complete output:

```bash
.venv/bin/python3 scripts/agent_context.py --stdout
```

If the sources were already loaded natively, do not load a second copy. The
human tutorial is not part of the startup instruction bundle.

Discover tool names without filling the conversation with every full schema:

```bash
.venv/bin/python3 scripts/local_mcp_bridge.py --list-tools --names-only
```

Read the live schema for the selected tool:

```bash
.venv/bin/python3 scripts/local_mcp_bridge.py --describe-tool status
```

Then call free status:

```bash
.venv/bin/python3 scripts/local_mcp_bridge.py --call status
```

The command client handles the local credential privately. A successful tool
call exits 0 and returns a non-error MCP result. Fresh status has
`agent_name: null`, zero wallet/strategy/user-job counts and no payments.
Existing installations should show their saved state instead.

A server health response, a tool count or a saved MCP setting alone does not
verify the complete connection. If remote tool discovery is unavailable,
report that separately from local status. There is no fixed required tool
count, and there is no automatic paid tour.

## 4. Choose a name and keep the same installation

The client should ask what you want to name your agent. Give your choice; it
saves the name using `set_agent_name`. It must not choose Sage or another name
for you. If you skip naming, it continues without a personal name.

For returning sessions and optional native MCP launchers, follow
[the user guide](../../docs/user-guide.md#come-back-or-switch-clients). Connect
the new client to the same installation to retain its name and records. A new
clone has a separate database; an AI account does not synchronize it.

## Troubleshooting

- **No status result:** check the configured local address and whether the
  existing server is running. Restart a stopped instance from its own checkout
  with `./scripts/setup.sh --yes --no-mcp`.
- **Claude registration fails:** from the running installation, use
  `./scripts/setup-mcp.sh`, then reconnect from `/mcp`. Other clients use
  [their own connection setup](../../docs/clients.md).
- **Remote tools missing:** use current discovery. An upstream outage is not
  fixed by deleting local state or inventing substitute tool results.
- **Name differs in another client:** verify it is using the same server and
  database, rather than a second clone or another hosted task.

Do not wipe `agent-data/` as a troubleshooting step. It can hold wallets,
spending records, trades and the saved name. Preserve it and diagnose the
specific failed check.

## Continue when you choose

Installation and naming require no paid market data. Strategy/backtest calls
can consume an API-key allowance or x402 payments even in paper mode. Set up
that access before requesting the next chapter's workflow.

[Chapter 04 - Your first strategy](04-your-first-strategy.md)
