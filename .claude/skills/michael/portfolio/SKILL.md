---
name: portfolio
description: >-
  Report what a person's strategies have actually done with money: the positions they are
  holding now and the trades they have closed, with the costs that came out of each. Reach
  for it whenever the question is about real activity rather than a simulation: "how am I
  doing", "what am I in", "what has it traded", "has it made money", "why did it sell",
  "what's my exposure". Also covers the difference between a real trade and a backtest, and
  what to say when a strategy has done nothing yet. Uses list_account_positions and list_account_trades for MangroveAI's
  execution record, and this agent's local list_trades, list_all_trades and list_evaluations.
uses-tools: [list_account_positions, list_account_trades, list_trades, list_all_trades, list_evaluations]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/portfolio/SKILL.md by scripts/sync-michael-skills.py. Do not edit here: change the skill upstream, or the script's adaptation tables, and re-run the sync. -->

# Real money is not a backtest

A backtest is what a strategy would have done. These tools are what it DID. They are not
comparable and must never be added together, averaged, or quoted as one figure. Say which one
you are talking about, every time, in the sentence itself: "in live trading" or "in the
backtest".

If someone asks how a strategy is doing and it has both, give both and label them. A strategy
that backtested +18% and has lost 3% live is one sentence, not a choice of which number to
report.

## Two records in this agent

`list_account_positions` and `list_account_trades` read MangroveAI's execution record: activity
from strategies deployed through MangroveAI itself. Strategies this agent runs keep their own record
locally: `list_trades` for one strategy's fills, `list_all_trades` across all of them, and
`list_evaluations` for what each tick saw. For "how is my paper or live strategy doing" here, the
local record is the answer. When the user runs strategies in both places read both, and say which
record every figure came from.

## Open positions are now; trades are over

`list_account_positions` is live state -- what is held, at what entry, with what stop and target.
`list_account_trades` is history -- what closed, for how much, and why it exited.

A position that has been closed is not in `list_account_positions` by default, and that is not a gap:
it became a trade. When someone says "what happened to my BTC position", the answer is usually
in `list_account_trades`, not in an empty position list.

## Never state a profit on an open position

A position carries its entry price, its size, its stop and its target. It does NOT carry a
current price or an unrealised gain, and there is nothing here to compute one from -- a price
you fetched a moment ago is not the price the position would close at. Describe the position as
it stands: size, entry, stop, target, and what it was sized to risk. If the person wants to
know whether they are up, say plainly that the open position's value is not something you can
state, and offer the closed record instead.

## A profit and loss without costs is a fiction

Every trade carries `fee_cost`, `slippage_cost` and `gas_cost`. `profit_loss` is what the
trade made or lost; the costs are what it took to make it, and on a small move they are the
difference between a winner and a loser. When you total up performance, say what the costs
came to. Never quote a P&L as if trading were free.

## Say why it exited

`exit_reason` is on every trade -- a stop, a target, a signal, a time-based exit, the end of
the run, or a manual close. "It sold" is not an answer when the record says which. A pattern in
the exit reasons is often the most useful thing you can tell someone: a strategy stopped out
nine times in a row is a strategy whose stop is too tight, and that is visible here and nowhere
else.

## Nothing yet is a real answer

A strategy that has not traded returns an empty list, and that is information: it is deployed
and waiting, or its entry conditions have not been met. Say that. Do not fill the silence with
backtest numbers, and do not imply something is wrong -- a selective strategy not firing is a
strategy working as designed.

## One strategy or all of them

`list_trades` takes a `strategy_id` and `list_all_trades` covers every local strategy; the
account tools filter by `account_id` and `asset`. Scope to one strategy when the person is talking
about one, and leave the scope off when they ask about themselves. Reporting every trade across every strategy when they
asked about one buries the answer.
