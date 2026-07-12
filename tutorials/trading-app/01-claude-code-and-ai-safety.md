# Chapter 01 — Claude Code + AI safety

*15 minutes. No funds required. No commands to run — this chapter is
orientation.*

Before you touch the trading bot, it's worth understanding two things:

1. **What Claude Code actually is**, because you'll be driving the bot
   through it and the mental model matters when things go weird.
2. **How this particular bot handles the dangerous parts** — keys,
   signed transactions, slippage — because trust is earned by
   understanding the seams, not by taking someone's word.

If you already know Claude Code inside-out, skim to "The three safety
nets." If you're new, read straight through.

## What Claude Code is, briefly

Claude Code is a terminal-based AI coding assistant. You type natural
language; Claude reads files, runs commands, edits code, and answers.
It runs locally on your laptop and connects to Anthropic's API for the
language model itself.

Five concepts are worth naming so you recognize them when they
show up.

### 1. The conversation is the interface

You don't click buttons. You type. When you say "create a momentum
strategy for ETH on 1h," Claude figures out which tools to call,
in what order, and hands you back the results. If you say "list my
trades," it calls a tool and summarizes. The whole session is a
conversation — which means the transcript builds up over time, and
you can refer back to anything.

### 2. Skills, rules, and hooks shape how the agent behaves

Open `.claude/` in this repo and you'll see three subdirectories:

- **`skills/`** — step-by-step playbooks the agent can invoke. When
  you say "I want to create a strategy," the agent loads
  `.claude/skills/create-strategy/SKILL.md` and follows it. Skills
  are deterministic recipes.
- **`rules/`** — global guardrails the agent reads at session start.
  `.claude/rules/trading-bot-workflow.md` is why the bot always
  tries strategies first and treats manual swaps as a fallback.
  `.claude/rules/wallet-presentation.md` is why it never echoes your
  private key back at you.
- **`hooks/`** — scripts that intercept Claude Code's inputs and
  outputs. `.claude/hooks/block-wallet-secrets.sh` scans every
  message you send for private-key patterns and blocks the submit
  if it finds one.

You won't need to edit these while working through the tutorial. But knowing they
exist is how you debug surprising behavior: if the bot refused to do
something, it's almost always a rule or hook, not a mystery.

### 3. MCP connects the bot's tools to Claude

Claude Code doesn't know about trading out of the box. It gets
trading capabilities from a **Model Context Protocol (MCP) server** —
in this repo, that's the local FastAPI process running on your
laptop. On session start, Claude fetches the list of tools the MCP
server exposes: `create_wallet`, `backtest_strategy`, `list_trades`,
and so on. Forty-one tools total in this setup.

If the MCP server isn't running, the bot has no tools and will fall
through to "I can't actually do anything" behavior. Chapter 03 gets
you past that.

### 4. Tool calls are visible

When the bot runs `create_wallet` or `execute_swap`, you see it
happen: the tool name, the arguments, the result. Nothing is
invisible. If the bot does something you didn't expect, the tool call
log tells you exactly what it did. **Read the tool calls.** This is
the single best habit for catching mistakes early.

### 5. `/clear` vs `/compact`

Two commands worth learning now:

- **`/clear`** wipes the conversation history. Use this when you want
  a fresh start — e.g., you've been exploring and the transcript is
  cluttered, and you want the bot to reorient without old context
  biasing it.
- **`/compact`** summarizes the conversation so far into a compressed
  context, keeping the important parts and throwing away the noise.
  Use this when the session is getting long (thousands of lines) and
  the bot is starting to forget things.

Neither affects your trading bot's state — strategies, trades, and
wallets live in the local database, not the conversation. You can
`/clear` and everything you've built is still there.

## The three safety nets

New users repeatedly make the same three mistakes, and each one
costs money or leaks secrets.
We rebuilt the system so those mistakes are structurally impossible,
not just "please don't" conventions. You should know what they are
because the bot's refusals will sometimes feel annoying, and the
annoyance is the feature.

Why so paranoid? Because "an AI agent that reads untrusted content
and can move funds" is the exact shape of nearly every agent-wallet
drain of 2025–2026: the Grok/Bankr drain (May 2026, ~$200K moved by
instructions hidden in Morse code in a tweet reply), the AiXBT
exploit (~$100K ETH via crafted social inputs), and the ElizaOS
memory-injection research that "gaslit" agents into bad trades.
The industry lesson from all of them is the same: **prompts are not
a security boundary — policy at the signing layer is.** That's why
this bot's guardrails live in hooks, validators, and a hard signing
allowlist (only known 1inch routers and token approvals to them;
EIP-7702 delegations refused outright), not in the LLM's judgment.
Security researchers call the dangerous combination the "lethal
trifecta" — private data + untrusted content + the ability to act
externally — and this repo's design goal is to never hand all three
to the model at once.

### Safety net 1 — the key-paste block

**Problem it prevents:** you accidentally paste your private key or
seed phrase into the chat, and it ends up in your transcript file,
Anthropic's API logs, and potentially your clipboard history.

**How it works:** `.claude/hooks/block-wallet-secrets.sh` runs on
every prompt you submit. It scans for the regex pattern of a 64-char
hex private key and common mnemonic shapes. If it matches, the hook
refuses the submit and prints a message pointing you at
`./scripts/stash-secret.sh` — a terminal-only tool that takes the
key with hidden input.

**What you'll see:** if you ever fat-finger a key, Claude Code shows
a refusal message and your prompt never reaches the agent. The bot
doesn't see it. The transcript doesn't contain it. Clean.

**The tradeoff:** occasionally the regex matches something that
looks like a key but isn't. If you hit that, rephrase your message
— the refusal is intentional.

### Safety net 2 — the backup gate

**Problem it prevents:** you create a wallet, the bot gives you a
`reveal_cmd`, and you promote a strategy to live *before* saving
the secret. The strategy fires a real swap. The swap succeeds. Then
your laptop dies. You lose the funds because you never backed up
the key.

**How it works:** every wallet has a `backup_confirmed_at` column
in the local database, initially NULL. You create a wallet, the bot
returns a `vault_token`; you run `./scripts/reveal-secret.sh <id>`
in a terminal, save the key somewhere durable (password manager,
paper, hardware wallet), then run `./scripts/confirm-backup.sh
<address>` to flip the flag. Only after that flag is set will
`execute_swap` or live-strategy promotion actually run for that
wallet.

**What you'll see:** if you try to promote a strategy to live before
confirming backup, the bot refuses with a clear message pointing at
the two commands. Not a bug — that's the gate working.

### Safety net 3 — the slippage cap

**Problem it prevents:** you tell the bot "swap $100 of USDC for ETH
with 10% slippage" on an illiquid pair, and a sandwich attacker
extracts nearly all your value because 10% is enough room to front-run
you.

**How it works:** the `execute_swap` API (and the live strategy
allocation block) has a Pydantic validator that caps `slippage_pct`
at `0.0025` — that's 0.25%. Anything higher is rejected at the HTTP
boundary, before a quote is even fetched. The value is in decimal
form (0.002 = 0.2%) and the bot always suggests 0.001–0.002 for
liquid pairs.

**What you'll see:** if you (or a bot confused about units) ask for
0.01 thinking it's "1 basis point," you get a 422 error. If you
genuinely need more slippage, you're trading an illiquid pair and
should reconsider rather than raise the cap.

## Paper before live, always

Every new strategy starts in `draft` state (unscheduled) or `paper`
state (scheduled, but fills are simulated at current market price —
no real swap, no real funds). You can watch paper evaluations fire
on the strategy's cron for as long as you want, see exactly when and
why the bot would have traded, and refine before any money moves.

Promotion to `live` is a separate, explicit step with four
requirements: user asks for it, wallet is backup-confirmed, allocation
block is complete, and `confirm=true` is set on the call. All four
or it doesn't happen.

The time-saving version of this advice: if a strategy works in paper
for at least a full cycle of its timeframe (e.g., a few hours for a
1h strategy, a day or two for a 4h), and the evaluations look
reasonable, *then* consider live. Not before.

This isn't just our house rule — it's the standard across mature
open-source trading bots. freqtrade's docs put it flatly: "always
dry run your strategy after backtesting it to see if backtesting and
dry run results are sufficiently similar", because live behavior
reveals latency, partial fills, and rejections that backtests don't.
The comparison matters as much as the paper run itself: if paper
results diverge badly from the backtest, that gap is information —
investigate it before real funds, don't average it away. And set
expectations like a regulator would: the CFTC's standing advisory on
AI trading bots is that "AI technology can't predict the future or
sudden market changes," and a 2026 study of 925K wallets found AI
trading agents lost their users ~$192M net in aggregate. Backtests
and paper runs earn a strategy a small live allocation — nothing
earns it blind trust.

## The audit trail

Everything the bot does is logged to SQLite in `agent-data/agent.db`:

- Every strategy evaluation (what the bot saw, what it decided)
- Every trade (paper or live, including the on-chain tx hash for
  live ones)
- Every wallet creation / import / backup-confirmation
- The scheduler's registered jobs (so it survives restart)

You can inspect any of this via the bot (`list_evaluations`,
`list_trades`, `list_wallets`) or directly with `sqlite3
agent-data/agent.db` if you want to run your own queries.

**Nothing is hidden.** If the bot claims to have done something, the
database row is there. If a row is missing, it didn't happen.

## Transcript hygiene

Two habits worth forming early:

1. **`/clear` between major phases.** Finish authoring a strategy
   and promoting it to paper? Start a fresh conversation for the
   next task. Keeps the bot focused and the transcript readable.
2. **Don't share transcripts publicly without reviewing them.** Even
   with the key-paste block, transcripts can contain addresses,
   position sizes, and strategy logic you may not want on the
   internet.

Claude Code stores transcripts at `~/.claude/projects/<hash>/` —
readable JSONL, one file per session. Handy for reviewing what
happened; also a thing to be aware of.

## What this bot is NOT

- **Not financial advice.** Mangrove's signals and strategies are
  informational. The bot will happily backtest, paper, and execute —
  it does not vouch for any strategy's future performance, and neither
  do we.
- **Not a high-frequency trader.** The minimum timeframe is 5m.
  Cron granularity and SDK latency mean you're looking at dozens of
  trades per month at most for a typical momentum strategy — not
  hundreds per day.
- **Not a portfolio manager.** It executes the strategy you tell it
  to, on the allocation you give it. It doesn't rebalance, doesn't
  optimize across strategies, doesn't tax-loss-harvest.
- **Not production software.** This is a local starter you own and
  extend — local first, single user, beta-quality. Audit it before
  trusting it with meaningful funds. If you end up running this with
  serious capital, you're operating it, not a team of pager-carrying
  SREs.

That's the mental model. Next up: what you actually have on your
machine, in more concrete terms.

→ [Chapter 02 — What you have](02-overview.md)
