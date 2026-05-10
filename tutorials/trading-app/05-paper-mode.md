# Chapter 05 — Paper mode

*15 minutes. No funds required. The first chapter where the bot is
actually running on a schedule.*

You ended Chapter 04 with a strategy in `draft` status — saved but
dormant. In this chapter you promote it to `paper`, which schedules
it to evaluate on a cron and simulates fills at current market price.
No wallet, no funds, no on-chain activity.

This is the last fund-free chapter. If you want to stop here, you
will still have accomplished the full author → backtest → paper →
evaluate loop. Everything from Chapter 06 onward is for attendees who
want to move real money.

## Promote to paper

In Claude Code:

> "Promote it to paper."

The bot calls `update_strategy_status(strategy_id=..., status="paper")`.
Expected response:

```
Promoted to paper. Scheduler registered a cron job on the 1h
timeframe — evaluations will fire at the top of each hour. Active
cron jobs: 1.

Check `list_evaluations` any time to see what the strategy saw.
```

Verify the cron registered:

```bash
curl -s -H 'X-API-Key: dev-key-1' \
  http://localhost:9080/api/v1/agent/status | python3 -m json.tool \
  | grep active_cron
```

Expected: `"active_cron_jobs": 1`.

## How paper mode works

Every strategy timeframe has a corresponding cron schedule:

| Timeframe | Fires at |
|---|---|
| `5m` | every 5 minutes (`*/5 * * * *`) |
| `15m` | every 15 minutes |
| `30m` | every 30 minutes |
| `1h` | top of every hour (`0 * * * *`) |
| `4h` | every 4 hours (`0 */4 * * *`) |
| `1d` | midnight UTC (`0 0 * * *`) |

When the cron fires, the scheduler calls a tick function inside the
mangrove-agent process. That tick:

1. Pulls the current OHLCV for the strategy's asset.
2. Calls `mangroveai.execution.evaluate()` with the strategy and
   the current market snapshot.
3. Logs an `evaluations` row regardless of outcome (what the bot
   saw, duration, errors).
4. If the evaluation returned order intents, dispatches them to
   the executor in **paper mode**: a simulated fill at the current
   market price, logged as a row in `trades` with `mode="paper"`
   and `status="confirmed"`. No network call to 1inch, no signed
   tx, no funds moved.

**The key word is "simulated."** A paper trade looks identical to a
live trade in `list_trades` — same schema, same fields — except
the `mode` column is `"paper"` instead of `"live"` and the `tx_hash`
is `null`. This lets you verify your strategy's trade pattern
(frequency, size, direction) against the same surface you'll read
later in live mode.

## Don't wait for the cron — force a tick

If your timeframe is 1h, you don't want to wait an hour to see
anything happen. Force an evaluation now:

> "Run evaluate_strategy on it so I don't have to wait for the cron."

The bot calls `evaluate_strategy(strategy_id=...)`. This runs the
same tick logic the cron would have run, right now. Response:

```
Evaluated. Duration: 342 ms. Orders produced: 0.

No entry signals fired — the strategy is watching, but nothing is
true right now.
```

Or, if signals did fire:

```
Evaluated. Duration: 487 ms. Orders produced: 1.

Paper trade logged:
  side:        buy
  symbol:      ETH
  input:       100 USDC
  output:      0.0398 ETH (at fill_price $2,509.21)
  mode:        paper
  reason:      rsi_cross_up + sma_cross_up both fired

Check `list_trades` to see the row.
```

## Reading evaluations

> "Show me my evaluations."

The bot calls `list_evaluations(strategy_id=...)` and returns the
most recent ones.

```
ID       Timestamp              Status   Orders  Duration  Notes
ev-ab12  2026-04-23 15:00:00Z   ok       0       287 ms    (no entry signal)
ev-cd34  2026-04-23 16:00:00Z   ok       1       342 ms    rsi_cross_up
ev-ef56  2026-04-23 17:00:00Z   ok       0       298 ms    (already in position)
ev-gh78  2026-04-23 18:00:00Z   ok       1       401 ms    sma_cross_down (exit)
```

Each row tells you:

- **Status** — `ok` means the evaluation completed cleanly. `error`
  means the SDK call failed (Mangrove down, API key invalid,
  etc.) — the `notes` or `error_msg` field has details.
- **Orders** — how many order intents the strategy produced.
  `0` is common; the strategy is watching, waiting for conditions.
- **Duration** — how long the evaluation took. Normal range is
  200–800 ms. If you see something over 5 seconds, Mangrove is
  likely under load.
- **Notes** — the bot's summary of what the strategy saw.

## Reading trades

> "Show me my trades."

```
ID       Timestamp              Mode   Side  Symbol  Input       Output      Status
tr-11    2026-04-23 16:00:12Z   paper  buy   ETH     100 USDC    0.0398 ETH  confirmed
tr-22    2026-04-23 18:00:08Z   paper  sell  ETH     0.0398 ETH  101.3 USDC  confirmed
```

Two entries — one paper entry and one paper exit. `confirmed` in
paper mode just means "simulated fill at the mark price recorded."

The gap between input and output in the exit tells you the
strategy's P&L on that round trip: bought 0.0398 ETH at ~$2,509 and
sold at ~$2,545, plus or minus whatever the simulated slippage and
fees were. About 1.3% gain before fees, not bad for a 2-hour hold.

## Restart the server — watch the cron survive

One of the nice properties of this setup: the scheduler persists
across restarts because the job store is the same SQLite file as
everything else.

Try it:

```bash
# Stop the server
kill $(cat agent-data/bare.pid)

# Wait a second, confirm it's gone
sleep 2
curl -s http://localhost:9080/health 2>&1 | head -1
# → "Failed to connect" / connection refused — expected, server is down

# Start it back up
./scripts/setup.sh --yes --no-mcp --no-verify

# Confirm the cron came back automatically
curl -s -H 'X-API-Key: dev-key-1' \
  http://localhost:9080/api/v1/agent/status | python3 -m json.tool \
  | grep active_cron
# → "active_cron_jobs": 1
```

No manual re-promotion needed. The strategy picks up ticking from
wherever it left off. If the crash happened mid-tick, you lose that
one tick; if it happened between ticks, you lose nothing.

## How long to paper before going live?

Depends on your timeframe and risk appetite. Rough heuristic:

| Timeframe | Minimum paper duration | Reasoning |
|---|---|---|
| `5m` / `15m` | 1–2 days | Hundreds of evaluations, dozens of trades. Fast feedback. |
| `1h` | 2–4 days | Want to see at least 10–20 evaluations cross the strategy's trigger region. |
| `4h` / `1d` | 1–2 weeks | Slow enough that the sample size matters. |

For a workshop, you'll paper for a couple of hours at most — not
long enough to truly judge the strategy, but long enough to see the
mechanics work. That's fine as a demo. For real capital, longer.

## What not to do in paper

### Don't mistake a quiet day for a broken strategy

If your strategy has 0 trades after 5 ticks, that's not a
malfunction — the signal conditions just haven't been met. Quiet
markets are the norm, not the exception.

Let it run. If after a full day (for 1h timeframes) you still have
zero evaluations with orders, that's your sign the signals are too
strict for current conditions. Iterate.

### Don't promote to live after one successful paper trade

One fill is an anecdote, not a pattern. The minimum-viable sample
size before live is "enough trades that you've seen both winners
and losers, at both of the strategy's entry/exit paths."

### Don't edit the strategy mid-paper

If you want to try different parameters, create a new strategy
and let both paper-run in parallel. Editing a strategy's params
mid-run invalidates the history you've accumulated — you no longer
know which fills came from which version.

(Today the bot doesn't even support mid-run edits cleanly; you'd
have to archive and recreate. That restriction is deliberate.)

### Don't promote to live on a Friday night

If something goes wrong with your live strategy, you want to be
awake, sober, and in front of your laptop to pause it. The bot
pauses on `update_strategy_status(status="inactive")`, which is a
one-sentence command, but you have to be there to issue it.

## What to take away

- Paper mode is the full author → schedule → evaluate → fill loop
  with zero on-chain exposure.
- Evaluations are logged whether or not the strategy fires.
- Simulated fills surface in `list_trades` with `mode="paper"` —
  identical schema to live trades, different mode flag.
- The scheduler persists across server restarts. Closing the
  laptop stops ticks; restarting the server resumes them from the
  SQLite jobstore.

You now have a strategy that's running on a cron, logging
evaluations, and simulating trades. From here, you can:

- **Stop.** Tinker with more strategies, more timeframes, more
  reference picks. Everything in Chapters 06–08 is optional.
- **Continue to Chapter 06** if you want to set up a wallet and
  eventually go live with real funds.

→ [Chapter 06 — Wallet setup](06-wallet-setup.md)
