# Risk Management — What Happens Behind the Scenes

This agent does **not** implement most risk controls itself — they are enforced
in two places, and you (Sage) must understand both so you can explain to the
user what the system is doing, cite the right reason when a trade is blocked,
and **never rebuild a control that already exists.**

There are two layers: the **MangroveAI engine** (per-strategy, inside
`evaluate`) and **this agent** (portfolio-wide). The engine decides *what* to
trade and applies per-strategy gates; the agent owns the tick, executes, keeps
the book, and enforces the one control the engine structurally cannot: the
aggregate portfolio kill switch.

## Layer 1 — Engine-side, per strategy (MangroveAI `RiskManager`)

Every `evaluate` call runs these gates **before** returning an entry order. When
a gate blocks, the engine returns **no entry order** (and records a denial with
a reason). You see this as a tick that produced no new orders — it is not a bug;
it is risk management working. The gates (all per-strategy, config-driven via
the strategy's `execution_config`):

- **`max_open_positions`** — caps concurrent open positions. Denial: `max_positions`.
- **`max_trades_per_day`** — daily entry cap. Denial: `daily_limit`.
- **Loss-streak cooldown** (`cooldown_config`, per timeframe) — after N losing
  trades on an asset within a rolling window, that asset is put on a cooldown
  (short and long windows). Denial: `cooldown`.
- **`max_hold_bars`** — force-exits a position held too long (a time-based exit,
  not an entry gate).
- **Max-drawdown circuit breaker** (`max_drawdown_limit`, default **20%**) — a
  binary halt: once a strategy's equity draws down 20% from its **high-water
  mark** (peak-relative, mark-to-market), new entries are denied. Denial:
  `max_drawdown`. **Remediation is automatic:** on trip it latches the
  strategy's per-timeframe long cooldown, then re-baselines the high-water mark
  to current equity and resumes — so a strategy is never permanently stuck, even
  if it is flat. This is *enforcement* (deny the entry), shared by backtest and
  live. It resizes nothing.

**Position sizing** (also engine-side): risk-per-trade budget, volatility
adjustment, and the `v1`/`v2` cash-reserve clamp. Not a halt — it scales the
size of an allowed trade.

You do not configure or re-implement any of this. To explain the knobs to a
user, they live in the strategy's `execution_config`; describe them, don't
duplicate them.

## Layer 2 — Agent-side, portfolio-wide (this repo — the kill switch)

The engine only ever sees **one strategy's account** per evaluation, so it
cannot see aggregate risk across everything the user is running live. This agent
can, because it owns the whole live book locally (allocations + trades +
positions in SQLite). So the **portfolio kill switch** lives here
(`portfolio_risk_service`):

- **What it measures:** live-book drawdown on a **realized-P&L** basis —
  `book_value = Σ active live allocations + Σ realized P&L of live strategies` —
  against a persisted high-water mark.
- **Trip:** when book drawdown reaches **`PORTFOLIO_MAX_DRAWDOWN_PCT`
  (default 30%)**, on the next live tick it **pauses ALL live strategies**
  (sets them `inactive`, cancels their crons, releases allocations).
- **Latched — no auto-resume.** Unlike the engine's per-strategy breaker, the
  portfolio switch stays tripped until a **human** clears it. This is
  deliberate: a 30% loss across the whole book warrants human review, not an
  automatic restart.
- **Re-baseline:** the high-water mark resets whenever the live set changes (a
  strategy enters/leaves live) so adding/removing capital is not misread as
  drawdown.

### How you observe and act on it

- **`GET /api/v1/agent/status`** includes a `portfolio_risk` block: `tripped`,
  `drawdown`, `high_water_mark`, `book_value`, `max_drawdown_limit`. Check it
  when the user asks "why did everything stop?" or at session start.
- If `tripped` is true, tell the user plainly: the portfolio drew down past the
  limit, all live strategies were paused, and it needs their explicit sign-off
  to resume. Show the numbers from `portfolio_risk`.
- **Re-activation is a human decision.** Clear the latch with
  **`POST /api/v1/agent/portfolio/risk/reset`** — only after the user
  understands the drawdown and confirms. After reset they can promote strategies
  back to live; the high-water mark starts fresh so it will not immediately
  re-trip.

## Layer 3 — Agent-side, the x402 spend budget (this repo — money going *out*)

Layers 1 and 2 protect **trading capital**. This one protects the wallet the
agent pays *with*. When the agent buys data from MangroveAI over x402
(signals, backtests, Oracle runs), each call is cents — but volume is the
risk, not price: an autonomous sweep is 99 backtests ≈ $2, and
`oracle_backtest_async` + `oracle_backtest_poll` form a poll loop against a
priced meter, so a 5-second poll on a two-minute backtest is 24 paid calls for
one result. Every one of those is individually reasonable.

Same argument as the kill switch, one layer over: the receiving server sees
one payment, the signing guard sees one payload, and only this agent sees the
running total. So `spend_service` owns it.

**It is a budget, not a circuit breaker, and you must talk about it that way.**
A breaker fires on an anomaly and demands review. A budget runs out through
normal use, and the remedy is a top-up. Spending $25 on data is not an
incident — it is a bill. Never describe it as a breach, a trip, or a halt.

- **What it measures:** every payment the agent has AUTHORIZED this budget
  period, in exact micro-USD, from a local ledger. Authorizations, not
  settlements — the agent controls what it signs, not what a receiver settles.
- **The gate:** budget is claimed inside the signing path, before the
  signature exists. A payment that does not fit is refused and nothing is
  signed. **A single over-budget payment does not exhaust the budget** — that
  amount comes from a remote server's 402 envelope, and treating it as
  exhaustion would let any server halt the agent's payments with one oversized
  quote. Read the ledger before assuming the budget is too small; an
  unexpectedly large quote usually means the resource is priced differently
  than you thought.
- **`unreconciled_count` should be zero.** It counts payments whose outcome
  was never recorded. Non-zero does not mean money was lost — it means the
  ledger has stopped being a reliable record of what settled, and is worth
  reporting rather than ignoring.
- **Default `X402_SPEND_CAP_USD` is $25** — roughly 10 sweeps, or ~25,000
  $0.001 signal reads.

### When the budget runs out

This is the one thing to get right. **The user's consent is the control; the
tool only records it.**

1. Call **`x402_spend_status`** and show them where the money went — the
   total, and what the biggest line items were.
2. Tell them what is still left to do and roughly what it will cost.
3. **Ask.** "That is the $25 budget gone, mostly the three sweeps. Want me to
   authorize $50 and carry on?"
4. Only after they say yes, call **`x402_spend_reset`** with `confirm=true`
   and the `cap_usd` *they* chose. It refuses without `confirm`.

Never call `x402_spend_reset` to get past your own refused payment. If there
is no human in the loop — a scheduler tick, an autonomous run — the correct
behaviour is to stop and leave it stopped until someone is asked. That is
precisely the case the budget exists for.

Outside a conversation the same thing is reachable at
`GET /api/v1/agent/x402/spend[/payments]` and
`POST /api/v1/agent/x402/spend/reset`, and `/status` carries an `x402_spend`
block beside `portfolio_risk`. Paper trading, local tools and anything reached
with an API key are unaffected — the budget only governs outbound x402
payments.

## Rules for you

1. A tick with no new orders is often a risk gate firing — check the denial
   reason / `portfolio_risk` before calling anything "broken."
2. Never build your own stop-loss, drawdown, cooldown, kill-switch, or
   spend-limit logic. It exists. Cite it, surface it, explain it.
3. Neither the portfolio kill switch nor the x402 budget refills itself. Do
   not "reset and continue" on the user's behalf — it is their call, made with
   the numbers in front of them. The difference is what you are asking for: a
   tripped kill switch needs their *review*, a spent budget needs their
   *permission*. One is grave, the other is routine; do not make a top-up
   sound like an incident.
4. "The agent stopped doing things" has two answers, and `/status` carries
   both: `portfolio_risk` for trading, `x402_spend` for paid data.
