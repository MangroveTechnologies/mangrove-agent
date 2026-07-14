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

## Rules for you

1. A tick with no new orders is often a risk gate firing — check the denial
   reason / `portfolio_risk` before calling anything "broken."
2. Never build your own stop-loss, drawdown, cooldown, or kill-switch logic. It
   exists. Cite it, surface it, explain it.
3. The portfolio kill switch never auto-resumes. Do not "reset and continue" on
   the user's behalf — resetting is their call, made with the numbers in front
   of them.
