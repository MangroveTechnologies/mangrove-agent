# Chapter 07 — Going live

*20 minutes. Funds required. This is the only chapter where real
money moves on-chain.*

You have a strategy running in paper, a wallet with USDC on it, and
the backup confirmed. This chapter takes one strategy from `paper`
to `live` with a capped allocation, watches the first real trade
happen, and verifies the on-chain settlement.

**Do NOT skip steps in this chapter.** Every refusal and gate exists
for a reason you'll appreciate the first time you almost make a
mistake.

## The four requirements

Before `update_strategy_status` will accept `status="live"`, four
things have to be true. The bot will check all of them before even
sending the call:

1. **You asked.** The bot does not auto-promote. You say "go live"
   or equivalent.
2. **Target wallet has `backup_confirmed_at` set.** From Chapter 06.
3. **Allocation block is complete.** Five fields: `wallet_address`,
   `token`, `token_address`, `amount`, `slippage_pct`.
4. **`confirm=true` is set on the call.** Pydantic validator rejects
   live-promotion otherwise.

Miss any one, the call fails. This is intentional: the only ways to
accidentally promote to live are ways we've already made impossible.

## 1. Pre-flight: balance + gas

Before spending money on a swap, make sure you have enough to cover
the swap plus gas.

> "Check my balance and the current gas on Base."

The bot calls `get_balances` and `get_gas_price`. Expected:

```
Balance on 0x5ff2aAb12Cd34eF567890AbCdEf1234567890aBcD:

  USDC: 2.0000000000
  ETH:  0.0001000000   ← a few cents worth, enough for gas

Base gas:
  slow:     0.03 gwei
  standard: 0.05 gwei
  fast:     0.08 gwei

A typical swap on Base costs ~0.0001-0.0003 ETH in gas, so you have
headroom.
```

**Important:** You need a small amount of native ETH (not USDC)
to pay transaction gas. Base gas is cheap (fractions of a cent in
normal conditions), but not free.

If you have zero native ETH:

- Send $0.50 of ETH on Base to the wallet address (from the same
  places you sent USDC from).
- Or: the first swap can fail cleanly with "insufficient gas" — no
  harm done, just a failed tx that didn't execute. Top up and
  retry.

## 2. Assemble the allocation

Before saying "go live," decide:

### `amount` — how much of your wallet's USDC the strategy can trade

For a workshop with 1–5 USDC total, **set `amount` to $1**. This
gives the strategy enough to actually trade (most DEXes have
minimum sizes around $0.50), while keeping your first real trade
small.

For real capital later: cap at 10–20% of the wallet's balance for
the first live allocation on a new strategy. If the bot decides to
scale up, you can raise the allocation later. If the bot does
something unexpected, you've capped the blast radius.

### `slippage_pct` — how much price deviation you'll accept

This is the big one. Slippage is in **decimal form** (0.002 = 0.2%,
NOT 0.2 and NOT 2%). The Pydantic validator caps it at **0.0025
(0.25%)**. Anything higher is rejected at the API boundary.

Guidance:

| Pair | Recommended slippage | Reasoning |
|---|---|---|
| USDC ↔ ETH (the workshop pair) | 0.001 (0.1%) | Deepest liquidity on Base. |
| USDC ↔ WBTC | 0.001–0.002 (0.1–0.2%) | Very liquid. |
| USDC ↔ cbETH / stETH | 0.002 (0.2%) | Liquid, slightly thinner pools. |
| USDC ↔ long-tail altcoin | Don't. | If you need > 0.25%, the liquidity isn't there. |

**Never ask for more than 0.25%** in a live strategy. The cap exists
because higher slippage values on illiquid pairs invite sandwich
attackers to extract the difference. The cap is protection, not
paternalism.

For this chapter's workshop trade: `slippage_pct: 0.002` (0.2%) on
USDC↔ETH. Conservative but not too tight.

## 3. Promote to live

In Claude Code:

> "Go live on my ETH momentum strategy with $1 allocation, 0.2%
> slippage, USDC as the quote token."

The bot will gather the details, check the gates, and assemble the
call. You should see (approximately):

```
I'm about to promote "ETH momentum with RSI + SMA crossover" to
live with this allocation:

  wallet_address:  0x5ff2aAb12Cd34eF567890AbCdEf1234567890aBcD
  token:           USDC
  token_address:   0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913  (Base USDC)
  amount:          1.0
  slippage_pct:    0.002  (0.2%)

Backup confirmed: ✓
Balance check:    ✓ (2.0 USDC available, 1.0 allocated)
Gas check:        ✓ (~0.0001 ETH available)

Confirm go-live? (yes/no)
```

**Read it.** Check the wallet address. Check the amount. Check the
slippage. This is the last confirmation before real money moves.

If everything looks right: **"Yes, confirm."**

Expected response:

```
Promoted to live. update_strategy_status call accepted with
confirm=true. Scheduler registered the same cron job; it just runs
in live mode now when it fires.

Active cron jobs: 1 (same one as before — just different mode).

First live evaluation will fire at the next hour boundary, or force
one now with "evaluate the strategy."
```

The strategy is now live. Congratulations. Nothing happens yet —
there's no immediate trade — because the cron has to tick, and even
when it does, it may or may not produce an order intent.

## 4. Watch for the first live tick

You can wait for the cron (up to 1 hour) or force a tick now:

> "Run evaluate_strategy on it."

Three outcomes:

### 4a. "No orders produced"

```
Evaluated. Duration: 412 ms. Orders produced: 0.

No entry signals fired at this tick.
```

Expected most of the time. The strategy is running, watching, but
not triggering. Either wait for the next cron, or rerun
evaluate_strategy later.

### 4b. "Orders produced → live swap executed"

```
Evaluated. Duration: 487 ms. Orders produced: 1.

Live trade executed:
  side:        buy
  symbol:      ETH
  input:       1.0 USDC
  output:      0.000398 ETH
  fill_price:  $2,513.44
  slippage:    0.17% (within tolerance)
  gas:         $0.0004
  tx_hash:     0x7d5a...d4e2
  status:      confirmed
  block:       15,287,104

View on basescan:
  https://basescan.org/tx/0x7d5a...d4e2
```

**This is a real trade.** 1 USDC actually left your wallet. 0.000398
ETH actually arrived. The tx_hash is settled on Base, visible on
basescan.

### 4c. "Orders produced → execution failed"

```
Evaluated. Duration: 612 ms. Orders produced: 1.

Live trade failed:
  side:        buy
  symbol:      ETH
  input:       1.0 USDC
  error:       SigningError: slippage exceeded tolerance (0.3% vs 0.2%)
  tx_hash:     (not broadcast)
  status:      failed
```

The strategy tried to trade, the slippage between quote and execute
exceeded your 0.2% tolerance, and the transaction never broadcast.
No funds moved. The evaluation is logged, the "failed" trade row is
logged, you have a cost-free data point.

You can bump slippage slightly (still capped at 0.25%) or wait for
better market conditions.

## 5. Verify on-chain

After a successful live trade, verify independently:

```bash
# From a terminal, use the tx_hash the bot reported
curl -s -H 'X-API-Key: dev-key-1' \
  "http://localhost:9080/api/v1/agent/dex/tx-status?tx_hash=0x7d5a...d4e2&chain_id=8453" \
  | python3 -m json.tool
```

Expected:

```json
{
  "status": "confirmed",
  "block_number": 15287104,
  "error_message": null
}
```

Or in the bot:

> "Get the tx status for 0x7d5a...d4e2 on chain 8453."

Or just click the basescan link from the trade output.

`status: "confirmed"` with a block number is the gold standard. Your
trade settled.

## 6. Monitor the position

> "Show me my trades."

```
ID       Timestamp              Mode   Side  Symbol  Input     Output         Status     Tx
tr-11    2026-04-23 16:00:12Z   paper  buy   ETH     100 USDC  0.0398 ETH     confirmed  -
tr-22    2026-04-23 18:00:08Z   paper  sell  ETH     0.0398    101.3 USDC     confirmed  -
tr-33    2026-04-23 22:04:17Z   live   buy   ETH     1.0 USDC  0.000398 ETH   confirmed  0x7d5a...d4e2
```

Two paper rows from before, one live row from today. Same schema.
The `mode` column tells you which is which.

Check the wallet:

> "Check my balance."

```
Balances for 0x5ff2aAb12Cd34eF567890AbCdEf1234567890aBcD:

  USDC: 1.0000000000  (down from 2.0 — 1.0 consumed by the trade)
  ETH:  0.000498      (0.0001 starting gas + 0.000398 from the swap)
```

## 7. If something looks wrong

### Pause the strategy

> "Pause my live strategy."

The bot calls `update_strategy_status(strategy_id=..., status="inactive")`.
Cron job deregisters. No further evaluations fire. Existing
positions stay put — pausing doesn't unwind.

To resume: `update_strategy_status(status="paper")` to go back to
paper, or `status="live"` (with a fresh allocation block and
confirm=true) to continue live.

### Emergency stop — kill the server

If something's really going wrong — runaway trading, unexpected
losses, the bot seems confused — stop the whole thing:

```bash
kill $(cat agent-data/bare.pid)
```

The strategy's cron job is in APScheduler, which only runs inside
the mangrove-agent process. Killing the process stops ticks immediately.
No further trades will fire until you restart.

Your funds are still yours (self-custody). You can withdraw them to
MetaMask using the address you saved, at any time, independent of
this bot.

### Sell everything back to USDC

If you want to flatten a position after pausing the strategy, just
do a manual swap:

> "Swap all my ETH back to USDC with 0.2% slippage. Use my
> backup-confirmed wallet."

The bot calls `execute_swap` directly. Not a strategy call — just a
one-off swap back to your quote currency. Useful for emergency exit.

## How long should you let it run?

Depends on what you're testing:

- **First hour after going live** — stay at your keyboard. Watch
  `list_trades` every 15 minutes. Confirm everything looks
  reasonable.
- **First day** — check periodically. By day's end you should have
  a clear sense of whether the strategy is firing at the rate you
  saw in the backtest.
- **First week** — let it run. Check P&L, win rate, and whether the
  real-world behavior matches the backtest's metrics.

If after a week the strategy's live behavior significantly diverges
from the backtest (e.g., backtest said 20 trades/month but you see
5 or 50, or win rate tanks), pause and iterate. Something about the
current regime has moved against the strategy.

## What to take away

- Going live requires four gates: you ask, backup confirmed, full
  allocation block, `confirm=true`. The bot checks all four before
  sending the call.
- Slippage is in decimals (0.002 = 0.2%), capped at 0.0025 (0.25%).
- Real trades return a `tx_hash`; verify via basescan or
  `get_tx_status`. No `tx_hash` = no on-chain trade happened.
- Pause with `status="inactive"` or kill the server — either stops
  ticks. Funds remain yours regardless.
- First live allocation is $1, not your whole balance. Always.

You've now executed a real on-chain trade driven by a strategy you
authored. Last chapter covers how to monitor, troubleshoot, and
extend the bot from here.

→ [Chapter 08 — Monitor, troubleshoot, extend](08-monitor-troubleshoot-extend.md)
