---
name: strategy-management
description: >-
  What happens to a strategy after it exists: what draft means and how one leaves it,
  archiving instead of deleting, and offering a deploy the person has to press. Reach for
  it whenever they ask about a strategy they already have rather than about building one:
  "is it live?", "why can't I deploy this?", "delete that one", "get rid of it", "put it
  into paper trading", "what state is it in", "activate it".
uses-tools: [list_strategies, list_platform_strategies, get_strategy, archive_strategy, propose_deploy]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/strategy-management/SKILL.md by scripts/sync-michael-skills.py. Edit the upstream skill and regenerate. -->

These instructions describe MangroveAI server tools and server-owned records. Discover current tools and input schemas through MCP before calling them; report unavailable capabilities without substituting a local implementation. Local execution workflows use agent_ tools and local strategy IDs. Do not pass IDs between those stores.

# A strategy the user already has

Every claim here is about money and permanence, so the standard is that you say what a state
means rather than what it sounds like. A draft is not a strategy waiting to be switched on, and
archived is not deleted, and a deploy button is not a deploy.

## Two stores, and which one answers which question

Strategies live in two places, and answering from the wrong one is how a person with fifteen
live strategies gets told they have none. This service's own store holds DEFINITIONS -- what was
composed and backtested here -- and it never learns what is deployed. The PLATFORM is the source
of record for deployment: what is live, what is paper, what money is behind it.

So: "my strategies", "my live strategies", "what's running", "how are they doing", or anything
about deployed money -- `list_platform_strategies`, always. It returns each strategy's status and
deployed_mode with balances, returns and open positions, plus live/paper counts you can quote
directly. `list_strategies` is for the drafts and compositions authored in this chat -- rules and
signals, never deployment. When the platform tool answers `available: false`, relay its reason
rather than falling back to the local list as if it were the same thing -- it is not.

## Review activity from one account snapshot

For "review my strategies", deployed mode, last trade, last evaluation, or stalled
activity, start with `list_platform_strategies`. It includes last_trade_at,
performance_updated_at and the latest recorded evaluation's time/status/error in
one batch. Use those facts to answer first; do not call trade and evaluation tools
once per strategy merely to rediscover these fields. Read detailed history only
for a specific unresolved question.

A missing evaluation timestamp or unavailable history means UNKNOWN, not "never
ran". Zero trades alone does not mean stalled: a strategy can evaluate regularly
without its entry conditions firing. To call something stalled, compare an actual
last evaluation with its expected schedule, or name a recorded evaluation error.
If the schedule or history is unavailable, say what cannot be determined. Do not
replace missing activity with backtest metrics or infer trading health from Sharpe.

## Say which strategy, and read it before you speak about it

`list_strategies` when they refer to one without naming it -- "my SOL strategy", "the one from
yesterday", "that one". `get_strategy` for the one you found, before describing its state: the
status, the asset and the signals are facts to be read, not recalled. Those are the stored rules;
whether it is live is the platform's answer, above.

## Draft means unmeasured, and it leaves draft on its own

A draft is composed, not measured. Nothing trades, no money moves, nothing is scheduled, and the
platform refuses to put it into paper or live trading while it is a draft.

It leaves draft when a backtest of it meets the platform's threshold spec. You do not do that and
cannot ask for it, so the honest line is what the state means and what would change it -- "it's a
draft, so it hasn't been measured yet; back it over a real window and if it clears the bar it
becomes a normal strategy". Never describe a draft as live, active, running or finished.

## Nothing is ever deleted

There is no way to delete a strategy or a backtest, and you should not offer one. Asked to delete
or get rid of one, use `archive_strategy`: it hides it from their default lists, keeps the
strategy and every backtest of it exactly as they are, and is reversible with `archived=false`.
Say so plainly -- "archived, so it's out of your list but nothing is lost".

Archiving is refused while a strategy is trading or holding a position, and that refusal is the
right answer rather than an obstacle: tell them it has to be stopped and flat first.

## Deploying is the person's decision, and you only offer it

`propose_deploy` puts a button in front of them. It does not deploy. Nothing happens until they
press it, and if they never do, nothing ever happens.

So describe what you are offering and stop. "I've put a deploy button up for you -- press it and
it goes to paper trading" is right. "Deployed", "deploying", "it's live now", or anything in the
past tense is a claim about something that has not occurred, and they will believe you.

Paper only. A live deploy is done in the deploy UI, and asking for one here is refused -- say
that plainly rather than implying you could if they insisted.

A draft cannot be deployed, and the refusal is worth explaining rather than relaying: draft means
no backtest of it has cleared the platform's bar, so there is nothing yet to put money behind.

## Saving twice does not make two strategies

`created` tells you what happened. `false` means a content-identical or name-identical strategy
already existed and you were handed it back. Say that -- "you already have this one" -- rather
than reporting a new strategy that was not created. Two strategies with the same rules and
different ids is a mess the user has to clean up.

It saves as a **draft**: composed, not measured. Nothing trades and nothing is scheduled. The
next step is a backtest over a real window -- the backtesting skill -- and what draft means, how
a strategy leaves it, and what archiving and deploying do is strategy-management.

## The marketplace: finding a strategy and offering to adopt it

When the task is building a strategy rather than browsing or managing one they already have,
the entry point is strategy-composition, which searches the marketplace first and composes
only when nothing listed fits -- what follows here is the mechanics those calls share.

`search_marketplace` is the marketplace page in a tool: every filter the page has, applied by
the platform. Pass the person's ask as filters rather than fetching a page and picking through
it. Report what came back with its numbers, and name the total so they know how much they have
not seen.

The listings are the platform's own digital-asset strategies on crypto pairs. The page also
merges NexusTrade partner listings (stocks, options, futures; ids start with `nt:`), which you
cannot search or adopt. When a total is compared with the page, say exactly that.

`get_marketplace_strategy` reads one listing in full before you describe or offer it: metrics,
the signals it is built from (names only -- the creator's tuning is never shown), its backtest,
and whether it is already in their account.

**A listing carries two sets of numbers, and they are supposed to differ.** `performance` (on
the listing, and the card the marketplace page shows) is the strategy's tracked record since it
was listed: live trading where it exists, otherwise paper. `backtest` is the historical
simulation it was listed on, over its own earlier window. Different windows, different data, so
a gap between them is not a discrepancy and never a bug to report. Name each for what it is --
"since listing it has returned X live; the backtest it was listed on returned Y over
start..end" -- and if they diverge sharply, that is information about the strategy (it may not
be trading the way it tested), not about the platform.

`propose_adopt` puts a button in front of them, exactly like `propose_deploy`. Pressing it copies
the listing into their account, where it starts paper trading at once with simulated funds.
Nothing happens until they press it. Free listings only: a paid one is adopted from its
marketplace page, and you say so and name the page. "I've put an adopt button up -- press it and
a copy starts paper trading in your account" is right; "adopted", "added", "it's paper trading
now" is a claim about something that has not occurred.
