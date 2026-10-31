# Mangrove Agent user guide

Use Mangrove Agent from Claude Code, Codex, Grok Build or Perplexity Computer.
Your AI client handles the conversation; a separate Mangrove Agent process
holds the installation's name, configuration, wallets and trading records.

## Install with the client you already use

Choose a client that can run commands. In Perplexity, use **Computer**; in Grok,
use **Build**. An ordinary search/web chat cannot install software by reading
a link. You do not need to install Claude to use another client.

Tell your client:

> Install https://github.com/MangroveTechnologies/mangrove-agent. Follow its AI
> client installation instructions, verify the connection using free status,
> and let me choose the agent's name. Do not set up wallets or make paid calls.

The client should install in its own execution environment, load the shared
instructions and verify a real tool call. It should say plainly if its host
cannot run commands or connect tools. [Client setup](clients.md) contains the
exact instructions for the installing agent.

On a fresh installation, the unattended setup the client runs
(`./scripts/setup.sh --yes --no-mcp`) needs no MangroveAI account or upstream
API key. It creates a separate local credential automatically, selects x402
access and defers payment-wallet setup. This is enough for connection checks
and naming. Paid market data and backtests still require API-key access or x402
payments. Your chosen AI provider's own account and access requirements also
apply.

## What successful first use looks like

The client should confirm that the server is running, the shared instructions
are loaded, and tool discovery plus free status succeeded. A printed command
or a saved connection setting by itself does not prove that it works.

If there is no saved name, it asks:

> What would you like to name your agent?

Reply with your choice. It saves the name and uses it in later conversations.
You can ask to rename it later. There is no default personal name; `SAGE.md`
is the instruction filename, not the name of your agent. Skipping naming is OK.

No startup tour should automatically buy data, create a wallet, schedule a
strategy or trade. Ask what it can do before requesting those actions.

## Install manually

You need Git, Python 3.11+ and a bash-compatible shell. Windows users can use
Git Bash. A code editor is optional. See [SETUP.md](../SETUP.md) for prerequisites.

```bash
git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent
```

```bash
cd mangrove-agent
```

```bash
./scripts/setup.sh --yes --no-mcp
```

This installs dependencies and starts the server on `http://127.0.0.1:9080`. It
preserves existing access settings; on a fresh installation it selects x402 and
defers wallet setup. For an interactive choice of API-key or x402 access, run
`./scripts/setup.sh` without `--yes`. Enter credentials only in the hidden setup
prompt, never in chat. See [setup with an API key or x402](setup-x402.md).

Ask your chosen client to open this checkout and follow `docs/clients.md`.
Claude's plugin is a separate optional installation path; use one path for an
installation rather than running the plugin and a clone on the same port.

## Come back or switch clients

Keep using the same installation when you want the same name and records.
Opening another AI client does not require reinstalling or restarting a running
server. Ask it to load the shared instructions in the checkout, discover tools
and read free status. It should greet you using the saved name.

For native terminal sessions from the checkout:

| Client | Start after the agent is running |
| --- | --- |
| Codex | `.venv/bin/python3 scripts/client_session.py codex` |
| Grok Build | `.venv/bin/python3 scripts/client_session.py grok` |
| Claude restricted chat | `./scripts/chat.sh` |
| Perplexity Computer | Continue in the installation's Computer task; ask it to load policy and verify status with the command client. |

The Codex/Grok helpers preserve existing settings and pass the local credential
privately. If a config needs merging, follow the exact message and
[client setup](clients.md#codex-and-grok-build). Existing sessions may need to
reconnect. The restricted Claude launcher has different tool permissions from
normal developer sessions; other clients do not inherit those restrictions.

If the server stopped after a reboot, run `./scripts/setup.sh --yes --no-mcp`
from the existing checkout to start it again. Setup can apply code/config
changes and restart the process, so do not rerun it just to switch clients
while a live trading installation is running.

A fresh clone on another machine or inside a different hosted task is a separate
installation with a separate database. Chat memory and an AI provider account
do not synchronize names or wallets between those installations. Hosted files
and processes last only as long as the host retains them; unattended trading
needs a persistent host. Never delete `agent-data/` to repair a chat connection.

## Long conversations and missing context

The client should load the four shared instruction sources once, then load
task-specific skills and tool schemas as needed. It should not repeatedly load
the whole tutorial or every tool schema. After a fresh chat, compaction or a
reconnect, it must restore any missing policy and check current status before
operating. Your name and trading records come from the installation, not from
an old chat summary. See [what each client loads](clients.md#context-at-session-start).

For managed tools, ask for names and descriptions across all services or one
functional area, then request full schemas for the areas or individual tools you
need. You can select one tool or any number. MangroveAI provides this progressive
discovery through `discover_tools`; the agent forwards the request. See the
[discovery examples](clients.md#progressive-discovery-of-managed-services).
If the configured server does not advertise `discover_tools`, the client must
report the limitation and use the host's own tool search.

## If something is unavailable

- **Local status fails:** the connection is not verified. Keep the checkout and
  data; use [client troubleshooting](clients.md) to check the configured server.
- **Local status works but remote tools are unavailable:** the local agent is
  running, but those capabilities are not ready. The client must report that
  limitation rather than inventing a result or claiming complete access.
- **Setup reports marketplace ownership unavailable:** naming/status can still
  work. Marketplace ownership and payment readiness are separate checks.
- **A mutation fails or its outcome is uncertain:** inspect the recorded outcome
  before retrying. A failed reply is not evidence that nothing happened.

## Continue to trading when you choose

Use the [trading tutorial](../tutorials/trading-app/00-index.md) for strategies,
backtests and paper mode. Paper mode avoids real trade execution, but upstream
data and evaluation calls can still cost money. Set up access and a budget
before paid use; live trading is a separate explicit decision.

Never paste a private key or recovery phrase into any AI chat. Run
`./scripts/stash-secret.sh` in your own private terminal and enter the secret at
its hidden prompt. It stores the secret in the agent's short-lived vault and
writes a private, single-use handoff file; tell the agent only the path it prints,
and it imports the wallet from the file's vault token. Claude's configured secret
hook is an extra client-specific check, not a guarantee supplied by every
provider.
