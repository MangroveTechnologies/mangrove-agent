# Chapter 01 - AI clients + safety

*15 minutes. No funds required. No commands to run - this chapter is
orientation.*

Claude Code, Codex, Grok Build and Perplexity Computer can drive this agent.
The Claude Code controls described below are examples of what one client adds
on top of the agent's own checks. Other clients have their own interfaces and
do not inherit Claude's hooks or its restricted launcher. Read the
[user guide](../../docs/user-guide.md) for your client.

Before you touch the agent, it helps to understand two things:

1. **What an AI client does for you**, because you drive the agent through it
   and the mental model matters when something behaves unexpectedly.
2. **Which safety checks run where**, because trust comes from understanding
   the seams, not from taking anyone's word.

If you already know Claude Code well, skip to "Where each safety check runs".

## What an AI client is, briefly

Claude Code is a terminal-based AI coding assistant. You type natural
language; it reads files, runs commands and calls tools. It runs on your
machine and connects to Anthropic's API for the language model. The other
supported clients work the same way in outline: you talk, the client chooses
tools, and the tool calls are visible to you.

Five concepts are worth naming so you recognize them.

### 1. The conversation is the interface

You do not click buttons. You type. When you say "Build me a BTC 1h strategy,"
the client decides which tools to call and in what order, then summarizes the
results. The conversation accumulates, so you can refer back to anything in it.

### 2. Skills, rules and hooks shape behavior

The `.claude/` directory in this repository holds four things:

- **`skills/`** - task playbooks the client loads when relevant, for example
  `setup-kraken`, `connect-kraken` and `setup-robinhood` for venue setup and
  `audit-security` for reviewing custody code. Strategy and backtest workflows
  are not local files: they come from MangroveAI at run time through the
  `load_skill` tool.
- **`rules/`** - shared guardrails that are loaded once at session start along
  with `SAGE.md`: `trading-bot-workflow.md` (how to load MangroveAI's
  workflows), `wallet-presentation.md` (how to create, import and present
  wallets) and `risk-management.md` (the risk controls that already exist).
  Codex and other clients read the same sources; the directory name does not
  make them Claude-only policy.
- **`hooks/`** - scripts that Claude Code runs automatically around prompts and
  tool calls. They are described below.
- **`settings.json`** - registers the hooks for Claude Code sessions opened in
  this repository.

You do not need to edit these while following the tutorial. Knowing they exist
helps you explain why the agent refused something: it is usually a rule, a
hook or a server-side check, not a mystery.

### 3. MCP connects the agent's tools to the client

The client gets trading capabilities from a **Model Context Protocol (MCP)
server**, which here is the local FastAPI process (the mangrove-agent). There
are two kinds of tools:

- **Local tools** that the agent implements itself: `status`,
  `set_agent_name`, `list_tools`, wallet tools (`create_wallet`,
  `import_wallet`, `list_wallets`), `execute_swap`, Kraken tools (`cex_*`),
  Robinhood venue tools (`agent_robinhood_status` and related), paper-schedule
  tools (`agent_start_paper_schedule` and related) and spend-budget tools
  (`x402_spend_status`, `x402_spend_reset`).
- **MangroveAI tools** that the agent forwards without keeping a copy:
  `load_skill`, `build_strategy_for_regime`, `save_strategy`, `run_backtest`,
  `get_backtest`, signal, market-data and knowledge-base tools, and
  `discover_tools` for compact discovery.

Ask for `list_tools` to see the live catalog. If the MCP server is not
running, the client has no tools; Chapter 03 gets you past that.

### 4. Tool calls are visible

When the agent runs `create_wallet` or `execute_swap`, you see the tool name,
the arguments and the result. If something unexpected happens, the tool call
log tells you exactly what ran. **Read the tool calls.** It is the best habit
for catching mistakes early.

### 5. Managing long conversations

Claude Code's `/clear` starts a fresh conversation and `/compact` summarizes a
long one. Other clients have equivalents. After compaction or a new
conversation, restore any missing shared policy and check free `status` before
operating, and avoid loading every full tool schema; use targeted discovery as
described in the [context reference](../../docs/clients.md#context-at-session-start).

Neither command changes the agent's state. Wallets, paper schedules and
payment records live in the local database, not in the conversation.

## Where each safety check runs

The checks fall into two groups. Server-side checks run inside the agent
process and apply to every client. Client-side checks run in Claude Code only.

| Check | Where it runs | Applies to |
|---|---|---|
| Wallet backup gate | Agent (`wallet_manager`) | Every client |
| Slippage cap and explicit `confirm` on swaps | Agent (`execute_swap`) | Every client |
| Signing allowlist (1inch routers only) | Agent (`wallet_manager`) | Every client |
| x402 spend budget | Agent (`spend_service`) | Every client |
| Key-paste block | Claude Code hook | Claude Code sessions with the hooks configured |
| Empty-wallet swap pre-check | Claude Code hook | Claude Code sessions with the hooks configured |
| No shell or file tools | `./scripts/chat.sh` launcher | The restricted Claude session only |

The reason for this layering is that an AI agent that reads untrusted content
and can move funds is a recognized attack shape. A prompt is not a security
boundary; policy at the signing layer is. That is why the agent's strongest
guardrails are validators and a hard signing allowlist in code, with the hooks
as an additional layer rather than the foundation. The
[EIP-7702 drain post-mortem](../../docs/incidents/2026-04-24-eip7702-drain.md)
shows the attack these controls are built to stop.

### Server-side check 1 - the backup gate

**Problem it prevents:** you create a wallet and fund it, but never save its
secret. The machine fails and the funds are unrecoverable.

**How it works:** every wallet has a `backup_confirmed_at` value in the local
database, empty at creation. `create_wallet` returns a `vault_token` and a
`reveal_cmd`. You run `./scripts/reveal-secret.sh <vault_token>` in a terminal,
save the secret somewhere durable, then run
`./scripts/confirm-backup.sh <address>` to set the value. Until it is set,
`execute_swap` refuses to sign for that wallet.

**What you see:** a refusal that names the two commands. That is the gate
working, not a bug. Chapter 06 walks through the steps.

### Server-side check 2 - slippage cap and confirmation

**Problem it prevents:** a swap with very loose slippage on an illiquid pair
can be sandwiched for most of its value.

**How it works:** `execute_swap` requires `confirm=true` and an explicit
`slippage_pct` with no default. The value is a decimal (0.002 is 0.2%) and must
be greater than 0 and at most `0.0025` (0.25%). Anything else is refused before
a quote is requested.

### Server-side check 3 - the signing allowlist

The agent signs only a call to a 1inch AggregationRouter, or an ERC-20
`approve()` whose spender is a 1inch router. Transfers to other addresses,
other contracts, EIP-7702 set-code transactions and authorization lists are
refused before the private key is decrypted. This limits what a compromised
dependency or a manipulated prompt could get the agent to sign.

### Server-side check 4 - the spend budget

When the agent pays MangroveAI over x402, `X402_SPEND_CAP_USD` (default 25)
bounds what it authorizes in a budget period. A payment that does not fit is
refused before it is signed. `x402_spend_status` shows the ledger and
`x402_spend_reset` records a fresh budget that you authorize. Chapter 05 and
Chapter 08 cover what this means in practice.

### Client-side check - the Claude Code hooks

Three scripts in `.claude/hooks/` are registered in `.claude/settings.json`.
All three block by exiting with status 2, and each block message names the hook
file. Only a person should edit or remove a hook.

- **`block-wallet-secrets.sh`** runs on every prompt you submit and on tool
  output. It blocks a private-key shape (`0x` plus 64 hex characters) and any
  run of 12 or more consecutive words from the BIP-39 word list. In tool output
  it blocks only when such a value sits in a key-named field, so transaction
  hashes do not trip it. Its message points you to `./scripts/stash-secret.sh`.
  If you paste a key by mistake, the prompt never reaches the model. Prose
  rarely contains 12 BIP-39 words in a row, but if a message is blocked
  unexpectedly, rephrase it.
- **`preflight-swap.sh`** runs before `execute_swap` and blocks the call when
  the wallet holds none of the input token on the target chain. It passes the
  call through if the balance is non-zero or cannot be read, and the agent
  reports its own error in that case.
- **`block-main-commits.sh`** applies to developers: it blocks commits and
  pushes to `main` from a Bash tool call.

See the [hooks README](../../.claude/hooks/README.md) for the full table.

**Other clients do not run these hooks.** Codex, Grok Build and Perplexity
Computer have their own permission models. In any client, never paste a
private key or recovery phrase, and use the terminal-only scripts for secrets.

### The restricted Claude session

`./scripts/chat.sh` starts Claude Code with a locked-down profile. It takes no
arguments. It removes the built-in tools, denies shell, file, search, web and
skill access, loads only the Mangrove MCP server, allows only that server's
tools, and registers the key-paste and swap pre-check hooks by absolute path.
It refuses to start if a required hook or policy file is missing. The session's
working directory is `agent-data/chat`.

Use it when you want a session that can talk to the agent but cannot touch your
files or run commands. It is a configuration of Claude Code, not a security
boundary against the machine's owner, who can edit the installation or start an
unrestricted session. See [restricted chat](../../docs/restricted-chat.md).

## Paper before live, always

Paper mode in this agent is a schedule of evaluations of a backtested strategy
that MangroveAI runs against a simulated portfolio. It moves no funds and needs
no wallet. Chapter 05 sets one up.

Going live is separate and explicit. The only live execution path the agent
exposes as an MCP tool for DEX trading is `execute_swap`, which needs your
explicit request, a backup-confirmed wallet, a slippage tolerance and
`confirm=true`. Chapter 07 covers it.

Treat paper results as evidence, not proof. If paper behavior diverges from the
backtest, investigate the gap before committing real funds. Live trading
reveals latency, partial fills and rejections that neither a backtest nor a
simulation shows. Start with a small amount and scale only as live results
justify it.

## The audit trail

The agent records what it does in a SQLite database at `agent-data/agent.db`:

- Wallet metadata, including when each wallet was created and when its backup
  was confirmed (secrets are stored only as ciphertext)
- Swaps you execute, with the transaction hash
- Paper schedule state: interval, evaluation count, pending slot and the last
  result
- The x402 payment ledger behind `x402_spend_status`

You can inspect wallets with `list_wallets`, payments with `x402_spend_status`,
and paper schedules with `agent_list_paper_schedules`. You can also query the
database directly with `sqlite3 agent-data/agent.db` if you want your own
reports. Strategy definitions, backtests and the paper portfolio's trades live
on MangroveAI and are read through its tools.

## Transcript hygiene

1. **Start a fresh conversation between major phases.** It keeps the client
   focused and the transcript readable.
2. **Review transcripts before sharing them.** Even with the key-paste block,
   transcripts can contain addresses, position sizes and strategy logic.

Claude Code stores transcripts under `~/.claude/projects/` by default as
readable JSONL files. Other clients store theirs elsewhere.

## What this agent is not

- **Not financial advice.** Strategies and signals are informational.
- **Not a high-frequency trader.** Strategy candle sizes start at 5 minutes and
  paper evaluations run at most once a minute.
- **Not a portfolio manager.** It does not rebalance or optimize across
  strategies.
- **Not production software.** It is a local starter that you own and extend:
  single user and beta quality. Audit it before trusting it with meaningful
  funds.

Next: what you actually have on your machine.

[Chapter 02 - What you have](02-overview.md)
