# Chapter 07 - Going live

*20 minutes. Funds required. This is the only chapter where real money moves
on-chain.*

You have a funded, backup-confirmed wallet from Chapter 06. This chapter makes
one small real swap on Base, verifies it on-chain, and shows how to stop
and unwind. Do not skip steps. Every refusal and gate exists for a reason you
will appreciate the first time you nearly make a mistake.

## What "live" means in this agent

Live execution is something you ask for. The agent exposes these live paths:

- **A DEX swap on a chain you name** through `execute_swap`. This is the path
  this chapter walks through.
- **Kraken**, through your own API key or keyless OAuth. The agent can read
  balances and dry-run orders, and the OAuth execute mode can place orders. See
  "Other venues" below.
- **Robinhood**, authorized with `./scripts/connect-robinhood.sh`. See "Other
  venues" below.

The agent does not expose an MCP tool that turns a saved MangroveAI strategy
into a scheduled live deployment. Local strategy creation is retired, and the
MCP tool that used to promote a strategy to `live` no longer exists. Paper
schedules (Chapter 05) are the scheduled strategy execution that the agent's
tools set up. Everything live in this chapter is initiated by you, one action
at a time.

## The requirements for a live swap

`execute_swap` signs a real transaction only when all of these are true:

1. **You asked.** The client does not trade on its own initiative.
2. **The wallet's backup is confirmed.** `backup_confirmed_at` is set (Chapter
   06). Otherwise the call is refused with the two commands to run.
3. **`slippage_pct` is given and in range.** There is no default. It is a
   decimal greater than 0 and at most `0.0025` (0.25%).
4. **`confirm=true` is set on the call.** Without it the call is refused.
5. **The signing guard accepts the transaction.** Only 1inch router calls and
   approvals to a 1inch router are signed.

In Claude Code, the `preflight-swap.sh` hook also blocks the call when the
wallet holds none of the input token on that chain.

## 1. Pre-flight: balance and gas

Before spending money, check that the wallet has enough USDC and a little native
ETH for gas. Ask the client to check your balance (Chapter 06 shows the REST
route), then check gas:

```bash
KEY=$(python3 -c "import json; print(json.load(open('server/src/config/local-config.json'))['API_KEYS'].split(',')[0])")
curl -s -H "X-API-Key: $KEY" \
  "http://localhost:9080/api/v1/agent/dex/gas-price?chain_id=8453" \
  | python3 -m json.tool
```

Base gas is cheap, usually a fraction of a cent per transaction, but it is not
free, and it is paid in native ETH, not USDC. A swap from USDC may also need a
one-time approval transaction, which costs gas too. If the wallet holds no ETH,
send a small amount (for example $0.50 worth) on Base to the wallet address
before you swap.

## 2. Decide the amount and slippage

### Amount

For a first live swap with 1-5 USDC in the wallet, spend about $1. That is
large enough to execute and small enough that a mistake is cheap. Later, keep
any single new position to a small fraction of the wallet and scale up only as
live results justify it.

### Slippage

`slippage_pct` is in **decimal form**: 0.002 means 0.2%, not 0.2 and not 2%. The
cap is 0.0025. A value above it, or zero or negative, is refused before a quote
is requested.

| Pair | Typical value | Note |
|---|---|---|
| USDC and ETH | 0.001 to 0.002 | Deepest liquidity on Base |
| USDC and a major token such as WBTC | 0.001 to 0.002 | Very liquid |
| Anything thinner | Do not trade it here | If you need more than 0.25%, the liquidity is not there |

The cap exists because loose slippage on a thin pair invites sandwich attacks.
It is protection, not paternalism. For this chapter use `0.002`.

## 3. Understand how `execute_swap` reads the amount

`execute_swap` takes `input_token`, `output_token`, `amount`, `chain_id`,
`wallet_address`, `slippage_pct` and `confirm`. It treats USDC as the quote
currency and works with one other asset at a time:

- **Selling** (the output token is USDC): the input token is the asset and
  `amount` is the quantity of that asset to sell.
- **Buying** (the output token is anything else): the agent spends USDC. It
  reads `amount` as the quantity of the asset you want to buy, fetches that
  asset's current price, and spends `amount` times that price in USDC. The
  `input_token` you pass is not used.

So a request to spend about 1 USDC on ETH needs an `amount` of roughly 1
divided by the current ETH price, not 1. Passing 1 would try to buy one whole
ETH. Before approving the call, make the client state the `amount` it will pass
and the USDC it expects to spend, and check `input_amount` in the result.

If you want to set the exact USDC input yourself, use the REST route instead,
which treats `amount` as the input amount verbatim:

```bash
curl -s -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  http://localhost:9080/api/v1/agent/dex/swap \
  -d '{"input_token":"USDC","output_token":"ETH","amount":1.0,"chain_id":8453,
       "wallet_address":"0x<your wallet address>","slippage_pct":0.002,
       "confirm":true}' | python3 -m json.tool
```

Running that command is the same real swap, with the same gates. Only run it
when you mean it.

## 4. Make the swap

In your AI client, state the swap and let the client show you the parameters
before it calls the tool:

> "Swap about 1 USDC for ETH on Base from my wallet, with 0.2% slippage. Show
> me the exact arguments, including amount, before you call execute_swap."

Read the arguments. Check the wallet address, the chain (8453 for Base), the
amount and the slippage. This is your last chance to stop before real funds move.
If everything is right, approve the call with `confirm=true`.

On success the tool returns:

| Field | Meaning |
|---|---|
| `tx_hash` | The on-chain transaction hash |
| `status` | `confirmed`, `pending` or `failed` |
| `input_token`, `input_amount` | What was spent |
| `output_token`, `output_amount` | What was received, per the quote |
| `fill_price` | Price of the asset in USDC |
| `fees` | Venue fee, price impact and the approval transaction hash if one was needed |
| `trade_log_id` | The ID of the local record of this swap |

Behind that, the agent requested a quote, sent an approval transaction first if
the router needed an allowance, prepared the swap with your slippage, signed
locally, broadcast, and polled until the transaction left `pending`. If it
returns `failed`, for example because the price moved beyond your tolerance,
no swap happened. A failed attempt still costs gas.

Common refusals:

- **A "not backed up" error** - run the two commands it prints (Chapter 06).
- **Confirmation required** - the call lacked `confirm=true`.
- **Slippage outside (0, 0.0025]** - lower the value.
- **Refused to sign** - the transaction was not a 1inch swap or approval; treat
  this as a bug or a security problem and do not work around it.

## 5. Verify on-chain

Confirm independently of the agent's own report. Open
`https://basescan.org/tx/<tx_hash>`, or query the status from a terminal:

```bash
curl -s -H "X-API-Key: $KEY" \
  "http://localhost:9080/api/v1/agent/dex/tx-status?tx_hash=0x<hash>&chain_id=8453" \
  | python3 -m json.tool
```

`status: "confirmed"` with a block number means the swap settled. No `tx_hash`
means no on-chain trade happened.

## 6. Review what happened

The agent records each swap locally. List live swaps with:

```bash
curl -s -H "X-API-Key: $KEY" \
  "http://localhost:9080/api/v1/agent/trades?mode=live&limit=10" \
  | python3 -m json.tool
```

Then check balances again. USDC should be down by about the input amount and the
asset balance up by about the output amount, less gas paid in ETH.

## 7. Selling back and stopping

A swap you make with `execute_swap` is a one-off. Nothing recurs on its own, so
there is no strategy to pause. To exit the position, ask for the reverse swap:

> "Swap my ETH back to USDC on Base with 0.2% slippage."

For a sell, `amount` is the quantity of ETH to sell, so state exactly how much,
leaving a little ETH for gas.

To stop the agent doing anything else, pause any paper schedules
(`agent_pause_paper_schedule`). If your database holds a local strategy record
in `live` status from an earlier version of the agent, its scheduled job keeps
running and can trade; set it to `inactive` first:

```bash
curl -s -X PATCH -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  http://localhost:9080/api/v1/agent/strategies/<strategy_id>/status \
  -d '{"status":"inactive","confirm":true}' | python3 -m json.tool
```

Then, if you want, stop the server:

```bash
kill $(cat agent-data/bare.pid)
```

Stopping the server halts paper-schedule requests and background reconciliation.
Your funds stay in your wallet regardless, and you can move them out with the
secret you saved in Chapter 06, independent of this agent.

## Other venues

### Kraken

Two ways to connect, each with a skill that walks through it:

- **Bring your own key** (`setup-kraken`). Create a least-privilege Kraken API
  key with withdrawals off, enter it with `./scripts/stash-kraken-secret.sh`
  (never in chat), and have the client call `cex_connect_kraken` with the vault
  token. The key stays on your machine and is stored encrypted. The tools
  `cex_status`, `cex_balances`, `cex_validate_order` (a dry run that places no
  order) and `cex_sync_fills` work with it.
- **Keyless OAuth** (`connect-kraken`). You consent once in a browser, choosing
  `view` or `execute` mode, through the REST routes under
  `/api/v1/agent/cex/oauth/`. In execute mode, `POST /cex/oauth/orders` places
  an order, and `validate_only` dry-runs it.

Kraken spot has no testnet, so validate first and start small.

### Robinhood

Authorize the agent once with `./scripts/connect-robinhood.sh` (it opens a
browser; you never type credentials into chat), then call
`agent_robinhood_status` to check readiness and buying power. The other
Robinhood tools (`agent_configure_execution_venue`,
`agent_list_venue_orders`, `agent_reconcile_venue_orders`,
`agent_cancel_venue_order`) manage a capital mandate and order journal for an
existing local strategy record. Activating a saved strategy on that route is
not available through MCP. See [Robinhood execution](../../docs/robinhood-execution.md)
before relying on it.

## What to take away

- A live DEX swap needs your request, a backup-confirmed wallet, an explicit
  `slippage_pct` of at most 0.0025, and `confirm=true`.
- `execute_swap` reads `amount` as the asset quantity when buying. Check the
  USDC it will spend before you approve, or use the REST route for an exact
  input.
- A real swap returns a `tx_hash`; verify it on basescan or with `tx-status`.
- Your first live swap is $1, not your whole balance.
- Funds remain yours whatever happens to the agent.

[Chapter 08 - Monitor, troubleshoot, extend](08-monitor-troubleshoot-extend.md)
