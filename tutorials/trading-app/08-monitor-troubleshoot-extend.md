# Chapter 08 — Monitor, troubleshoot, extend

*20 minutes. No funds required. The "what next" chapter.*

You've got a working bot. This chapter is the reference for running
it over time: what to watch, how to diagnose what's wrong when
something breaks, and where the code lives if you want to modify
it.

## Day-to-day monitoring

Four commands worth knowing. All of them are MCP tools, but the
underlying REST endpoints work the same way if you want to script
them.

### "Show me my trades"

`list_trades` or `list_all_trades` — everything the bot has
executed, across all strategies and modes (paper / live).

```
> "Show me all my trades from the past day."
```

The bot filters by time and renders a table. Useful for end-of-day
reviews.

### "Show me my evaluations"

`list_evaluations(strategy_id=...)` — every tick the strategy ran,
whether it produced orders or not.

```
> "Show me the last 20 evaluations for my live strategy."
```

If you see `status="error"` rows, something's wrong (see
Troubleshooting). If you see lots of `orders_produced: 0` rows with
no signal activity, the strategy is just quiet — normal during
sideways markets.

### "What's my portfolio worth?"

`portfolio_value(wallet_address=...)` — sum of all token balances
priced at current spot.

```
> "What's the total value of my wallet right now?"
```

```
Portfolio value for 0x5ff2aAb12Cd34eF567890AbCdEf1234567890aBcD:

  USDC:  1.00    ($1.00)
  ETH:   0.000498 ($1.25)

Total:   $2.25
```

### "What's my P&L?"

`portfolio_pnl(wallet_address=..., strategy_id=...)` — realized and
unrealized P&L attributed to a specific strategy.

```
> "What's my P&L on the ETH momentum strategy?"
```

```
P&L for "ETH momentum with RSI + SMA crossover":

  Trades:       3 (1 entry, 1 exit, 1 open position)
  Realized:     +$0.012  (from the closed round-trip)
  Unrealized:   +$0.04   (current open ETH position vs entry price)
  Total:        +$0.052

Time in strategy: 18h 42m
Cumulative $ returned: 2.6% of allocated $2.00
```

## Reading the server log

When something's weird, the log is the first place to look:

```bash
tail -f agent-data/bare.log
```

Structured JSON logs, one event per line. The important event
names:

| Event | What it means |
|---|---|
| `strategy.tick.started` | Cron fired, tick function entered |
| `strategy.tick.completed` | Tick finished cleanly, with `order_count` |
| `strategy.tick.errored` | Tick hit an exception (details in `exception`) |
| `scheduler.job.registered` | Strategy promoted to paper/live, cron added |
| `scheduler.job.removed` | Strategy paused/archived, cron removed |
| `order.paper.filled` | Simulated fill logged |
| `order.live.broadcast` | Real tx broadcast, tx_hash in event |
| `order.live.confirmed` | tx_status poll returned confirmed |
| `order.live.failed` | Broadcast failed or tx reverted |

Filter with `jq` for more structure:

```bash
tail -f agent-data/bare.log | jq 'select(.event | startswith("strategy"))'
```

## Common failure modes

### Strategy shows `status="error"` in list_evaluations

Means the call to `mangroveai.execution.evaluate()` raised an
exception. Check `evaluation.error_msg` or the server log for the
exception text.

Usual suspects:

1. **Mangrove API down.** Rare. Try `curl -H "X-API-Key: <your-key>"
   <MANGROVEMARKETS_BASE_URL>/health`.
2. **API key expired or rotated.** Edit
   `server/src/config/local-config.json`, update
   `MANGROVE_API_KEY`, restart the server.
3. **Strategy references a signal that no longer exists.** The KB
   evolves; if a signal was deprecated, the strategy's
   `evaluate_strategy` will fail until you rebuild it. Archive the
   old one, create a fresh one via the reference-first flow.

### Server won't start

```bash
./scripts/setup.sh --yes --no-mcp --no-verify
# → /health did not respond
```

Read the tail of `agent-data/bare.log`. Usual suspects:

1. **Port conflict on 9080.** Check: `lsof -iTCP:9080 -sTCP:LISTEN`.
   If something else is on it, either kill that or bind on a
   different port: `BARE_PORT=9082 ./scripts/run-bare.sh`.
2. **Config file missing fields.** If you edited
   `local-config.json` and removed something, the server won't
   start. Diff against `local-example-config.json` and add back any
   missing keys.
3. **Database file locked.** If the server was killed ungracefully,
   the SQLite WAL may have left a lock. `ls agent-data/*.db*` — if
   you see `agent.db-journal` or `agent.db-wal`, they should clear
   on restart. If they persist, `rm agent-data/agent.db-journal
   agent-data/agent.db-wal` to force-unlock.

### Strategy doesn't tick

The cron is registered (`active_cron_jobs: 1`) but nothing fires.

Check:

1. **Is the server actually running?** `curl
   http://localhost:9080/health`. If no, restart.
2. **Is the timeframe what you think?** A `1d` strategy only fires
   at midnight UTC. A `4h` strategy fires at 00:00, 04:00, 08:00,
   12:00, 16:00, 20:00 UTC. If you expected a 1h and got a 4h,
   you'll be waiting a while.
3. **Is the strategy in `paper` or `live` state?** `list_strategies`
   — `draft` and `inactive` don't tick. `paper` and `live` do.

### Live swap rejected with "slippage exceeded"

Market moved between when the quote was fetched and when the swap
broadcast. Common during volatile periods.

Two responses:

1. **Raise slippage** (still capped at 0.0025). Re-promote the
   strategy with `slippage_pct: 0.0025` instead of 0.002.
2. **Wait for calmer markets.** If volatility drops, the same
   strategy will execute cleanly at 0.002.

### "Confirmation required" error on execute_swap

You called `execute_swap` without `confirm=true`. That's the point
— the bot won't execute without explicit approval. Retry with
`confirm=true` in the parameters.

### Wallet shows zero balance unexpectedly

Either:

1. **You're checking from a different address.** Wallets are keyed
   by address string; typos return empty. `list_wallets` to see
   the exact stored value.
2. **Funds are on a different chain.** The bot only queries the
   chain_id you created the wallet for. If you sent USDC on
   Ethereum instead of Base, the bot can't see it. Bridge it to
   Base.
3. **`get_balances` is cached.** Rare but possible during Mangrove
   API hiccups. Retry after 10 seconds.

## Where the code lives

If you want to extend or modify the bot, here's the map.

### Business logic

```
server/src/services/
├── strategy_service.py    ← tick() + status transitions + backtests
├── order_executor.py      ← paper + live swap execution
├── wallet_manager.py      ← encrypt/decrypt + sign + backup gate
├── trade_log.py           ← writes to evaluations + trades tables
├── scheduler_service.py   ← APScheduler wrapper
├── allocation_service.py  ← live-mode allocation rows
└── reference_strategies_service.py  ← the curated JSON library
```

Each service file has a corresponding test in `server/tests/`.

### REST routes

```
server/src/api/routes/
├── strategies.py   ← /api/v1/agent/strategies/*
├── wallet.py       ← /api/v1/agent/wallet/*
├── dex.py          ← /api/v1/agent/dex/* (swaps, quotes, tx-status)
├── market.py       ← /api/v1/agent/market/*
└── ... a dozen others
```

Each route file is thin — parse request, call a service, return a
response. No business logic.

### MCP tools

```
server/src/mcp/tools.py   ← one big file, ~1500 lines, 41 tools
```

Organized by domain with `_register_*` helpers. If you want to add
an MCP tool, pattern-match an existing one and register it in the
bottom of the file.

### Skills, rules, hooks

```
.claude/
├── skills/create-strategy/SKILL.md  ← strategy authoring workflow
├── skills/backtest/SKILL.md         ← bar-window sizing + verdict
├── skills/custom-signal/SKILL.md    ← compose entry/exit signal stacks
├── skills/audit-security/SKILL.md   ← wallet/EIP-7702 security audit
├── skills/check-alignment/SKILL.md  ← read-only "does this fit" check
├── skills/tool-spec/SKILL.md        ← draft new MCP tool specs
├── rules/trading-bot-workflow.md    ← Stage 0-6 of the agent's loop
├── rules/wallet-presentation.md     ← how to present wallet output
├── rules/git-workflow.md            ← branch / PR conventions
├── hooks/block-wallet-secrets.sh    ← regex key-paste block
├── hooks/block-main-commits.sh      ← refuse direct commits to main
└── hooks/preflight-swap.sh          ← refuse swap if input_token balance is 0
```

These are all Markdown and shell — no build step. Edit in place,
restart Claude Code, changes take effect.

### Config

```
server/src/config/
├── local-example-config.json   ← committed template
├── local-config.json           ← your real config, gitignored
├── dev-config.json             ← staging
├── prod-config.json            ← production
└── test-config.json            ← pytest
```

All keys have docstrings in `server/src/config.py` if you want to
see what they do.

### The data files you might care about

```
server/src/services/data/
├── trading_defaults.json     ← execution config defaults (copied from upstream)
├── strategy_schema.json      ← validates create_strategy_manual payloads
├── reference_strategies.json ← the curated library for Phase B / ref-first
├── threshold_spec.json       ← PASS/FAIL thresholds for backtest verdicts
└── example_bundle.txt        ← bundled example strategy for KB cite-first tests
```

To add a reference strategy, edit `reference_strategies.json` —
append an entry with the same schema as the existing ones. The file
loads at server start.

To change backtest thresholds (e.g., tighten Sharpe to 1.5 from
1.2), edit `threshold_spec.json`. Applies to all subsequent
backtests.

## Post-workshop: things to try

If you finished the workshop and want to keep going:

1. **Let your strategy run for a week.** Check back with
   `list_trades`, `portfolio_pnl`. See how backtest expectations
   hold up against real market data.
2. **Try autonomous mode.** In Chapter 04 we used reference-first
   because it's beginner-friendly. Try `"Generate 7 autonomous
   candidates for ETH on 4h optimized for Sortino, backtest all."`
   Compare the winner against your reference-first strategy.
3. **Build a mean-reversion strategy alongside the momentum one.**
   Two strategies can run in parallel on the same wallet if you
   allocate carefully (don't over-commit).
4. **Vary the timeframe.** Same strategy structure on 4h and 1d —
   see how trade frequency and P&L change.
5. **Explore the KB.** `kb_list_indicators` + `kb_search` for
   anything that catches your eye. If you find a signal you want
   to build around, ask the bot: `"Tell me how <signal> works and
   build a strategy around it for ETH."`
6. **Read the SDK.** `from mangroveai import ...` in a Python REPL
   — the SDK is introspectable and well-typed. You'll find
   capabilities you didn't know the bot had.

## Getting help

- **Mangrove developer portal** — https://mangrovedeveloper.ai
- **This repo's issues** —
  https://github.com/MangroveTechnologies/mangrove-agent/issues
- **In the bot** — `"What tools do you have for X?"` or `"Search the
  KB for Y."` The bot knows its own capabilities.

## What to take away

- The audit trail (`list_trades`, `list_evaluations`,
  `agent-data/bare.log`) is your source of truth. If the bot
  claims something, the log confirms.
- Most failures are one of: server not running, MCP not
  registered, slippage too tight, Mangrove API blip, or timeframe
  confusion. Run through those five before escalating.
- Everything is extendable. The services / routes / tools / skills
  / rules all live in plain text files you can edit and reload.
- Self-custody means your funds are yours no matter what happens
  to the bot. If something goes sideways, you can always withdraw
  to MetaMask using the private key you saved in Chapter 06.

That's the workshop. You went from zero to a working,
Mangrove-powered, self-custody trading bot running on your own
laptop, with a paper strategy or a live strategy — whichever you
stopped at. Build things. Trade carefully. Come back when you want
to level up.

— End of the workshop tutorial.
