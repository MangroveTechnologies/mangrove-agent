---
name: strategy-management
description: >-
  What happens to a strategy after it exists: what draft means and how one leaves it,
  archiving instead of deleting, and deploying to paper or live only on the person's say-so. Reach for
  it whenever they ask about a strategy they already have rather than about building one:
  "is it live?", "why can't I deploy this?", "delete that one", "get rid of it", "put it
  into paper trading", "what state is it in", "activate it". 
uses-tools: [list_strategies, get_strategy, update_strategy_status, delete_strategy, list_evaluations, list_trades]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/strategy-management/SKILL.md by scripts/sync-michael-skills.py. Do not edit here: change the skill upstream, or the script's adaptation tables, and re-run the sync. -->

# A strategy the user already has

Every claim here is about money and permanence, so the standard is that you say what a state
means rather than what it sounds like. A draft is not a strategy waiting to be switched on, and
archived is not deleted, and a status change is a real change, not an offer.

## One record, and it answers deployment too

This agent keeps every strategy it authored in its own local database, and that record is also where
deployment lives: `status` is `draft`, `inactive`, `paper`, `live` or `archived`, and a live strategy
carries its allocation. So "my strategies", "what's running", "what's live" is `list_strategies`
(filter with `status`), and "how are they doing" is what they actually did -- `list_evaluations` and
`list_trades` -- never a backtest.

## Say which strategy, and read it before you speak about it

`list_strategies` when they refer to one without naming it -- "my SOL strategy", "the one from
yesterday", "that one". `get_strategy` for the one you found, before describing its state: the
status, the asset and the signals are facts to be read, not recalled. Those are the stored rules;
in this agent the same record says whether it is live.

## What each status means

Read `status` off `get_strategy`; never describe it from memory. `draft` and `inactive` are
created but not running: nothing trades and nothing is scheduled. `paper` evaluates on the strategy's
timeframe with simulated fills and no funds. `live` executes real swaps from its allocation.
`archived` is retired. A strategy moves only through `update_strategy_status`, along the transitions
it allows, and a refused transition names the valid ones.

A strategy with no backtest behind it has not been measured, whatever its status. Say that plainly
rather than calling it ready, and back it over a real window before recommending paper. Never
describe a strategy as live, active or running unless its status says so.

## Archive rather than delete

Asked to delete or get rid of a strategy, archive it: `update_strategy_status` with
`status="archived"`. Its local record, evaluations and trades stay, and every backtest of it stays
stored. Say so plainly -- "archived, so it's retired but nothing is lost" -- and say that archiving
here is one-way: an archived strategy cannot be reactivated, only rebuilt as a new one. Taking a
strategy off `live` needs `confirm=true`, and that is the user's call.

`delete_strategy` exists in this agent and removes the strategy upstream on MangroveAI (the local
audit trail is kept). Use it only when the user explicitly asks for deletion after hearing that
archiving is the non-destructive option.

## Deploying is the person's decision

There is no button here: `update_strategy_status` changes the status the moment you call it. So
never move a strategy to `paper` or `live` on your own initiative -- ask, get a clear yes, then call
it, and report what the response says happened.

`paper` needs nothing more. `live` is gated: the user explicitly asked, the wallet's secret is backed
up, the allocation block is complete, and `confirm=true` -- the full rules are in the `trading-bot`
skill. Do not describe a strategy as live until the call returned it live.

## Creating twice makes two strategies

`create_strategy_manual` does not look for an existing strategy with the same rules or name: every
call creates a new one. `list_strategies` before creating, and if an identical one exists, use it.
Two strategies with the same rules and different ids is a mess the user has to clean up.

A new strategy has not been measured. The next step is a backtest over a real window -- the
backtesting skill -- before any talk of paper or live.
