# Chapter 08 - Monitor, troubleshoot, extend

*20 minutes. No funds required. The "what next" chapter.*

You have a working agent. This chapter is the reference for running it over
time: what to watch, how to diagnose problems, and where the code lives if you
want to change it.

## Day-to-day monitoring

Most of these are MCP tools. Several also have REST routes under
`/api/v1/agent/` if you want to script them with the local API key.

### "How is the agent doing?"

`status` is free and needs no authentication. It returns the agent name,
version, wallet count, counts of local strategy execution records by status,
`active_cron_jobs`, the agent database path, uptime, and two safety blocks:

- `x402_spend` - the outbound x402 budget (Chapter 05).
- `portfolio_risk` - the state of the portfolio kill switch, which concerns live
  strategy deployments. It stays idle when you only make manual swaps and run
  paper schedules.

Check `status` at the start of a session and whenever "the agent stopped doing
things." The answer is usually `x402_spend` or an unreachable server.

The strategy counts in `status` describe the agent's local execution records,
not the strategies you saved on MangroveAI. Use `list_strategies` for those.

### "What are my paper schedules doing?"

`agent_list_paper_schedules` shows every schedule's state, evaluation count,
pending slot and last result without an upstream call. `agent_get_paper_schedule`
with `refresh=true` also fetches MangroveAI's view. A schedule in `attention`
needs `agent_reconcile_paper_schedule` (Chapter 05).

For what the paper portfolio did, read MangroveAI's side: `list_positions`,
`get_trades_for_strategy` and `list_evaluations` with the strategy ID.

### "What did I spend?"

`x402_spend_status` shows the budget and the recent payment ledger. It covers
x402 payments only; API-key usage is tracked by MangroveAI against your
allowance.

### "What wallets and swaps do I have?"

`list_wallets` returns addresses and metadata, never secrets. Swaps made with
`execute_swap` are recorded in the agent's database; read them with:

```bash
KEY=$(python3 -c "import json; print(json.load(open('server/src/config/local-config.json'))['API_KEYS'].split(',')[0])")
curl -s -H "X-API-Key: $KEY" \
  "http://localhost:9080/api/v1/agent/trades?limit=20" | python3 -m json.tool
```

Add `&mode=live` to filter to live swaps. For balances, use
`/api/v1/agent/wallet/<address>/balances?chain_id=8453` (or the `portfolio`
route beside it for value, P&L and tokens).

## Reading the server log

When something is odd, the log is the first place to look:

```bash
tail -f agent-data/bare.log
```

Setup starts the server in its local mode, so the log is human-readable console
text, one event per line, with fields after the event name. Search for event
names with `grep`. The ones you are most likely to need:

| Event | What it means |
|---|---|
| `wallet.created`, `wallet.imported`, `wallet.backup_confirmed` | Wallet lifecycle changes |
| `order.executing` | A swap is starting (token pair and amount) |
| `order.live.signed`, `order.live.broadcast` | The approval or swap was signed and sent; `tx_hash` is in the event |
| `order.live.confirmed` | The status poll finished; the event carries the final `status` |
| `order.errored` | Order execution raised an error |
| `x402.payment.started`, `x402.payment.settled`, `x402.payment.refused` | An outbound payment to MangroveAI |
| `scheduler.job.fired`, `scheduler.job.errored` | A background job ran or failed |

```bash
grep -E "order\.|x402\.payment" agent-data/bare.log | tail -50
```

## Common failure modes

### Remote tools are missing

Local tools such as `status` and `list_wallets` work, but strategy and market
tools are not advertised. The agent could not read MangroveAI's tool catalog:
an upstream outage, a wrong API key, or no payment readiness. The agent does not
fall back to local substitutes. Check the configured access mode, retry
discovery, and report the limitation rather than inventing results.

### A paid call is refused

In x402 mode, read `x402_spend_status`. If the budget is exhausted or the payer
wallet lacks USDC, fund or re-authorize it yourself. The agent does not reset a
budget on its own.

### API key rejected

An expired or rotated MangroveAI key makes calls fail with an access error.
Re-run `./scripts/setup.sh --auth api-key` to enter the new key with hidden
input. Never paste the key into chat or a command argument.

### A paper schedule is in `attention`

An evaluation failed or its outcome was uncertain. Read the schedule's `error`
and `pending_slot`, fix the cause (for example a refused payment), then call
`agent_reconcile_paper_schedule`. Chapter 05 explains why this stops automatic
requests.

### Paper start is refused

The strategy is not eligible: it lacks a passing backtest of its exact
configuration and market, is archived, uses unsupported sizing, or belongs to
another execution flow. Backtest it again, or save a new strategy.

### Server will not start

```bash
./scripts/setup.sh --yes --no-mcp --no-verify
```

If `/health` does not respond, read the tail of `agent-data/bare.log`. Usual
causes:

1. **Port conflict on 9080.** Check with `lsof -iTCP:9080 -sTCP:LISTEN`. Free
   the port, or choose another with `BARE_PORT=9082 ./scripts/setup.sh --yes
   --no-mcp --no-verify`.
2. **Config file missing keys.** If you edited
   `server/src/config/local-config.json`, compare it with
   `local-example-config.json` and restore any removed keys.
3. **Another setup is running.** Setup uses a lock directory at
   `agent-data/.setup.lock`. Remove it only after confirming no setup is
   running.
4. **Master key problems.** The agent refuses to use a `master.key` that is
   group- or world-readable (fix with `chmod 600`) or has the wrong length. See
   Chapter 06.

### A swap is refused or fails

- **Backup gate:** run `reveal-secret.sh --address` and `confirm-backup.sh`.
- **Confirmation required:** the call lacked `confirm=true`.
- **Slippage out of range:** use a value in (0, 0.0025].
- **`status: failed`:** the price moved beyond your tolerance between quote and
  execution, or the transaction reverted. Retry in calmer conditions; do not
  raise slippage past the cap.
- **Wrong amount:** when buying, `execute_swap` reads `amount` as the asset
  quantity (Chapter 07). Check `input_amount` in the result.

### A wallet shows a zero balance unexpectedly

1. **Wrong address.** Compare with `list_wallets`; a typo returns an empty
   result.
2. **Wrong chain.** The balance query uses the chain ID you pass. USDC sent on
   Ethereum is not visible on Base. Bridge it.
3. **A transient upstream error.** Retry after a few seconds.

## Where the code lives

If you want to extend or modify the agent, here is the map.

### Business logic

```
server/src/services/
|-- wallet_manager.py          encrypt/decrypt, signing guard, backup gate
|-- order_executor.py          live swap execution
|-- dex_service.py             human <-> base-unit amounts, quotes
|-- paper_schedule_service.py  paper schedules and the evaluation clock
|-- scheduler_service.py       APScheduler wrapper and background jobs
|-- strategy_service.py        local strategy execution records and status
|-- broker_execution.py        Robinhood venue routing
|-- cex_service.py             Kraken (bring your own key)
|-- cex_oauth_service.py       Kraken (keyless OAuth)
|-- spend_service.py           x402 spend budget
|-- x402_payer.py              outbound x402 payments
|-- trade_log.py               trades and evaluations tables
`-- agent_identity.py          the saved agent name
```

Tests live in `server/tests/`.

### REST routes

```
server/src/api/routes/
|-- wallet.py       /api/v1/agent/wallet/*
|-- dex.py          /api/v1/agent/dex/*  (quote, swap, tx-status, gas-price)
|-- strategies.py   /api/v1/agent/strategies/*  (paper schedules, status)
|-- cex.py          /api/v1/agent/cex/*
|-- brokers.py      /api/v1/agent/brokers/*
|-- logs.py         trades and evaluations
|-- x402_spend.py   /api/v1/agent/x402/spend
`-- discovery.py    status, tool catalog, agent name
```

Each route file is thin: parse the request, call a service, return the
response.

There are no local routes for MangroveAI's tools (market data, signals,
knowledge base, backtests, the Strategy Engine). The agent forwards those to
MangroveAI over MCP and keeps no copy.

### MCP tools

```
server/src/mcp/
|-- tools.py             status, naming, wallets, swaps, Kraken, spend budget
|-- paper_tools.py       paper-schedule tools
|-- broker_tools.py      Robinhood venue tools
|-- marketplace_tools.py marketplace prepare/submit
|-- mangrove_proxy.py    forwards MangroveAI's tool catalog and calls
`-- marketplace_proxy.py forwards Markets tools
```

To add a local tool, follow an existing one in `tools.py` and register it with
the discovery registry. [`docs/contributing.md`](../../docs/contributing.md)
describes the service-layer pattern.

### Skills, rules, hooks

```
.claude/
|-- skills/trading-bot/SKILL.md      operating rules entry point
|-- skills/setup-kraken/SKILL.md     connect Kraken with your own key
|-- skills/connect-kraken/SKILL.md   connect Kraken with OAuth
|-- skills/setup-robinhood/SKILL.md  connect Robinhood
|-- skills/audit-security/SKILL.md   wallet and signing security audit
|-- skills/check-alignment/SKILL.md  read-only alignment check
|-- skills/tool-spec/SKILL.md        draft new MCP tool specs
|-- rules/trading-bot-workflow.md    load MangroveAI's skills first
|-- rules/wallet-presentation.md     how to present wallet output
|-- rules/risk-management.md         engine gates, kill switch, spend budget
|-- hooks/block-wallet-secrets.sh    key-paste block
|-- hooks/block-main-commits.sh      refuse commits to main
`-- hooks/preflight-swap.sh          refuse a swap from an empty wallet
```

Strategy building and backtest instructions are not here; the client loads them
from MangroveAI with `load_skill`. The local files are Markdown and shell with
no build step. Edit them in place and restart your client to pick up changes.

### Config

```
server/src/config/
|-- local-example-config.json   committed template
|-- local-config.json           your real config, gitignored
|-- dev-config.json             staging
|-- prod-config.json            production
`-- test-config.json            tests
```

See [`docs/configuration.md`](../../docs/configuration.md) for the keys.

### Data files

The agent ships no strategy data files. Trading defaults, strategy schemas,
backtest thresholds and signal definitions all live in MangroveAI.

## Where to go next

1. **Run a longer paper schedule** and read the paper portfolio's trades and
   positions. See how the backtest's expectations hold up.
2. **Screen several ideas** with `screen_candidates` before spending backtests
   on them (Chapter 09).
3. **Compose a different style of strategy** alongside the first, such as a
   mean-reversion strategy next to a momentum one, and give each its own paper
   schedule.
4. **Vary the candle size.** Build the same idea on 4h and 1D and compare trade
   frequency and results.
5. **Trade on Kraken or Robinhood.** Follow the `setup-kraken`,
   `connect-kraken` or `setup-robinhood` skill, validating before any real
   order (Chapter 07).
6. **Explore the knowledge base.** `kb_search` and `kb_get_document` query the
   same corpus as the Mangrove docs. Ask: "Tell me how <signal> works and build
   a strategy around it for ETH."

## Getting help

- **Mangrove docs and knowledge base** - https://mangrove.io/docs (the same
  corpus `kb_search` queries)
- **Mangrove developer portal** - https://mangrovedeveloper.ai
- **This repository's issues** -
  https://github.com/MangroveTechnologies/mangrove-agent/issues
- **Security background** - the
  [EIP-7702 drain post-mortem](../../docs/incidents/2026-04-24-eip7702-drain.md)
  and the defenses it led to.
- **In the client** - ask "What tools do you have for X?" or "Search the
  knowledge base for Y."

## What to take away

- The audit trail is `status`, the paper schedule tools, `x402_spend_status`,
  the local swap record and `agent-data/bare.log`. If the agent claims
  something, one of them confirms it.
- Most failures are one of: the server is not running, MangroveAI's catalog is
  unreachable, a payment or budget refusal, an ineligible strategy, or a gate
  such as the backup gate or slippage cap. Check those before escalating.
- Everything is extendable. Services, routes, tools, skills and rules are plain
  text files you can edit.
- Self-custody means your funds are yours whatever happens to the agent. If
  something goes wrong, you can withdraw using the secret you saved in
  Chapter 06.

That is the tutorial. You went from a fresh clone to a paper-trading agent and,
if you chose, a verified live swap from a wallet you control. Build carefully
and trade carefully.
