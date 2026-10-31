# Chapter 03 - Setup

*10 minutes. No trading funds required. This chapter uses only installation,
free discovery and status calls, and naming.*

Goal: get the server running, connect your chosen AI client, verify a real free
status call, and choose the agent's name. The [user guide](../../docs/user-guide.md)
is the common reference for Claude Code, Codex, Grok Build and Perplexity
Computer.

## 1. Choose an installation environment

You can ask any of those command-capable clients to install the repository:

> Install https://github.com/MangroveTechnologies/mangrove-agent using its AI
> client instructions. Verify free status, then ask me what to name my agent.
> Do not create wallets, spend money or schedule trading.

Perplexity needs Computer and Grok needs Build. Their ordinary web and search
modes cannot install a server. An installation inside a hosted task runs there,
not automatically on your machine; use a persistent host for unattended work.

For manual setup you need Git, Python 3.11+ and a bash-compatible shell. See
[SETUP.md](../../SETUP.md) for prerequisites. Claude and Node.js are needed only
for the optional Claude installation method, not for the Python server.

## 2. Clone and start

Skip cloning if you already have an installation, and keep its config and
database. From the directory where you want the project:

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
Run `./scripts/setup.sh` interactively if you want to choose API-key access
instead (`./scripts/setup.sh --auth api-key` prompts for the key with hidden
input). Never put a key in chat or in a literal command argument.

Setup prints the actual local address. Port 9080 is the default. To choose a
free port on a fresh installation:

```bash
BARE_PORT=9085 ./scripts/setup.sh --yes --no-mcp
```

Setup preserves the chosen port for later runs and refuses an unrelated busy
port rather than reporting success. Re-running setup can restart the server
when code or config changed, so do not run it merely to switch clients.

Claude Code users can omit `--no-mcp` to have setup register the MCP server, or
run `./scripts/setup-mcp.sh` later. `./scripts/chat.sh` then opens the
restricted session described in Chapter 01.

## 3. Verify through real tools

Ask the installing client to load all four shared policy sources. For a client
without native repository instruction loading, it reads the complete output of:

```bash
.venv/bin/python3 scripts/agent_context.py --stdout
```

If the sources were already loaded natively, do not load a second copy. The
tutorial itself is not part of the startup instruction bundle.

Discover MangroveAI's tools without printing every full schema:

```bash
.venv/bin/python3 scripts/local_mcp_bridge.py --call discover_tools
```

`discover_tools` returns names and descriptions by default. To read full
schemas for particular tools, write a JSON object such as
`{"detail":"schema","names":["load_skill"]}` to a file and pass it with
`--arguments-file`. See the
[progressive discovery reference](../../docs/clients.md#progressive-discovery-of-managed-services).
The diagnostic `--list-tools` option prints the complete catalog and is large;
avoid putting it in a conversation.

Then call free status:

```bash
.venv/bin/python3 scripts/local_mcp_bridge.py --call status
```

The command client reads the local credential privately. A successful tool call
exits 0 and returns a non-error MCP result. Fresh status has `agent_name: null`
and zero wallets, zero strategies and zero active cron jobs; existing installations show
their saved state instead.

A health response, a tool count or a saved MCP setting alone does not verify
the full connection. If remote tool discovery is unavailable, report that
separately from local status. There is no fixed required tool count, and there
is no automatic paid tour.

## 4. Choose a name and keep the same installation

The client should ask what you want to name your agent. Give your choice; it
saves the name with `set_agent_name`. The client must not choose a name for
you. If you skip naming, it continues without a personal name.

For returning sessions and optional native MCP launchers, follow
[the user guide](../../docs/user-guide.md#come-back-or-switch-clients). Connect
the new client to the same installation to keep its name and records. A new
clone has a separate database, and an AI account does not synchronize it.

## Troubleshooting

- **No status result:** check the configured local address and whether the
  server is running. Restart a stopped instance from its own checkout with
  `./scripts/setup.sh --yes --no-mcp`.
- **Claude registration fails:** from the running installation, use
  `./scripts/setup-mcp.sh`, then reconnect from `/mcp`. Other clients use
  [their own connection setup](../../docs/clients.md).
- **Remote tools missing:** use current discovery. An upstream outage is not
  fixed by deleting local state or inventing substitute tool results.
- **Name differs in another client:** verify that client uses the same server
  and database, not a second clone or another hosted task.

Do not wipe `agent-data/` as a troubleshooting step. It can hold wallets,
spending records and the saved name. Preserve it and diagnose the specific
failed check.

## Continue when you choose

Installation and naming require no paid market data. Strategy and backtest
calls can use an API-key allowance or x402 payments, and paper evaluations do
too. Set up that access before starting the next chapter's workflow; in x402
mode, follow the payment-wallet instructions that setup prints (see
[x402 setup](../../docs/setup-x402.md)).

[Chapter 04 - Your first strategy](04-your-first-strategy.md)
