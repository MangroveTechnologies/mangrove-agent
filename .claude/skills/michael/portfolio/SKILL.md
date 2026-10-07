---
name: portfolio
description: >-
  Report what a person's strategies have actually done with money: the positions they are
  holding now and the trades they have closed, with the costs that came out of each. Reach
  for it whenever the question is about real activity rather than a simulation: "how am I
  doing", "what am I in", "what has it traded", "has it made money", "why did it sell",
  "what's my exposure". Also covers the difference between a real trade and a backtest, and
  what to say when a strategy has done nothing yet. Uses list_positions and list_trades.
uses-tools: [list_positions, list_trades]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/portfolio/SKILL.md by scripts/sync-michael-skills.py. Edit the upstream skill and regenerate. -->

These instructions describe MangroveAI server tools and server-owned records. Discover current tools and input schemas through MCP before calling them; report unavailable capabilities without substituting a local implementation. Local execution workflows use agent_ tools and local strategy IDs. Do not pass IDs between those stores.

# Real money is not a backtest

A backtest is what a strategy would have done. These tools are what it DID. They are not
comparable and must never be added together, averaged, or quoted as one figure. Say which one
you are talking about, every time, in the sentence itself: "in live trading" or "in the
backtest".

If someone asks how a strategy is doing and it has both, give both and label them. A strategy
that backtested +18% and has lost 3% live is one sentence, not a choice of which number to
report.

## Open positions are now; trades are over

`list_positions` is live state -- what is held, at what entry, with what stop and target.
`list_trades` is history -- what closed, for how much, and why it exited.

A position that has been closed is not in `list_positions` by default, and that is not a gap:
it became a trade. When someone says "what happened to my BTC position", the answer is usually
in `list_trades`, not in an empty position list.

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

Both tools take `strategy_id`. Use it when the person is talking about one strategy, and leave
it off when they ask about themselves. Reporting every trade across every strategy when they
asked about one buries the answer.
