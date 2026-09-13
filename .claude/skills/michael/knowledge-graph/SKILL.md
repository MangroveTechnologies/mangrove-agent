---
name: knowledge-graph
description: >-
  Query the knowledge graph -- every indicator and signal in the library (what each computes,
  what it consumes and produces, which signals read which of its outputs, what part each plays
  in a strategy) joined to the trading knowledge base: market structure, instruments, risk,
  chart patterns, quantitative method. Reach for it before answering from memory or guessing at
  a name, and before answering a trading question at all: "which indicators produce a bounded
  oscillator", "what reads RSI", "is there already a signal for X", "how far should the stop
  go", "what are the odds I wipe out the account". Uses the query_knowledge tool
  (stats, find, ask, get, neighbors, outputs, path).
uses-tools: [query_knowledge]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/knowledge-graph/SKILL.md by scripts/sync-michael-skills.py. Do not edit here: change the skill upstream, or the script's adaptation tables, and re-run the sync. -->

# Ask the graph before you answer

You have a knowledge graph with two halves on one schema.

**The library, compiled from its own source.** Every indicator and signal: what each computes,
what it consumes and produces, which signals read which of its outputs. Read from the code, so
it is exact -- every answer here is a fact about the library as it is.

**The trading knowledge base, ingested from its chapters.** Market foundations, instruments and
mechanics, core concepts, strategy design, risk management, indicators, chart patterns,
quantitative analysis. Authored prose, so it answers *why* and *when* rather than *what
signature* -- and, being prose, it is reached by meaning rather than by exact wording.

**They are joined, and that is the point.** `atr-based stop` is a rule the risk chapter states,
and it `uses` an indicator the code defines. One query crosses from advice to implementation.

It answers things a keyword search cannot: what reads this indicator's third output, which
signals produce a bounded value, every way this signal connects to that class and which of them
is the reason -- and, because half of it is prose, what to do when the market goes quiet.

Answering from memory is the failure mode this exists to prevent. If you name a signal the user
then cannot find, you have invented it. The same applies to trading advice: if the graph holds a
judgment on it, quote that rather than your own recollection.

## Start here

```
query_knowledge  op=stats
```

Returns the counts and the **complete vocabulary** every other call accepts as a filter --
relation names, class names, role names, primitives, statuses, input columns, output units. Call
it first. The one reliable way to get a wrong answer from this graph is to invent a class or
relation name. An invented filter comes back as an **error pointing you at the vocabulary**,
never as an empty result you would read as "there are none" -- so an error here is the
correction, not a dead end. Fix the filter and call again.

## Two searches, and they are not the same

`find` matches the **words** you give it. `ask` matches what you **mean** -- it seeds from two
indices, one built from this corpus and one from a pretrained sentence model, fuses them, and
then walks a hop along the edges.

> In this agent `ask` has the corpus index but not the pretrained one -- that needs the
> `mangrove-kb[semantic]` extra, which is not installed -- and its `note` says so. The figures
> below were measured with both; expect fewer paraphrased questions to land, and fall back to
> `op=find` sooner.

```
query_knowledge  op=find  q="divergence"                         # a term you can name
query_knowledge  op=ask   q="how far away from my entry should the stop go"
```

Use `ask` whenever you are holding a question rather than a name -- which is most of the time a
person asks you something. The words of a question are rarely in the node that answers it: *"what
are the odds I wipe out the account"* shares no word with `risk of ruin`, whose definition is
"the probability of losing a specified percentage of capital". Word search does not reach it;
meaning search does.

Measured on twenty-five questions phrased the way a trader asks them, `find` answers 5 and `ask`
answers 18.

**Every row `ask` returns carries `reached`**: which match it came from, how many hops, along
which relation, and that edge's own stated reason. That is the grounds for the answer. A row at
`hops: 0` was retrieved; a row at `hops: 1` was reasoned to, and the `why` says on what basis --
quote it rather than asserting the connection yourself.

**It is wrong about one time in four and does not know when.** The misses come back as
plausible-looking neighbours with nothing marking them wrong. So if what returns looks
off-topic, it is: ask again with a domain term, or fall back to `find` on a word you can guess.
Both are cheap.

## The two axes -- the thing to understand

Every signal is classified two ways at once, and they mean different things.

| axis | question it answers | inherited? |
|---|---|---|
| **class** (`kind=`) | what character is this computation concerned with? | yes |
| **role** (`role=`) | what part does it play in a strategy? | **never** |

Classes are the characters a computation can measure -- averaging, flow, momentum, oscillator,
pattern, volatility. Roles are `trigger` and `filter`.

**A role is not a type.** `filter` is not a kind of signal; it is a part some signals play, and
the same computation could play another part in another strategy. So role is never inherited and
never appears as a class. "Is a momentum signal" and "is being used as a filter" are answers to
different questions, and the graph treats them that way.

`kind` and `role` **intersect**, they do not union:

```
query_knowledge  op=find  kind="momentum"  role="trigger"     momentum-class signals used as triggers
query_knowledge  op=find  kind="oscillator"                   everything in the oscillator class
query_knowledge  op=find  role="filter"                       signals playing the filter part
```

An indicator **measures** its class; a signal is **about** its class -- different relations,
because they are different claims. A signal that emits a boolean does not measure rate of change,
it is concerned with it, and the indicator it reads is the reason. Both are returned by
`kind=`, and the reason is one hop away through `neighbors`.

Do not assume class is single-valued. A signal that reads two indicators can genuinely be about
both.

## Which call

| question | call |
|---|---|
| what is in here at all? | `op=stats` -- always first |
| is there already a signal or indicator for X? | `op=find q="keyword"` |
| everything of a class, or in a role, or both | `op=find kind=... role=...` |
| what needs a volume column? what is retired? | `op=find requires="volume"`, `op=find status="deprecated"` |
| what does this thing compute -- formula, params, outputs? | `op=get q="rsi"` |
| which values are bounded, or in given units? | `op=outputs bounded=true units=...` |
| what produces an output called X? | `op=outputs q="histogram"` |
| what reads this indicator? | `op=neighbors q=... relation="uses" direction="in"` |
| what does this signal depend on? | `op=neighbors q=... relation="uses" direction="out"` |
| what breaks if this changes? | `op=neighbors q=... direction="in"` |
| how are these two related? | `op=path q=... to=...` |

The node's `name` is the registered signal name. That is the join between the graph and the
library, and it is what makes this a map of something runnable rather than an encyclopedia. Use
it verbatim when you propose a strategy.

## The typed detail is the point

`op=get` returns what the code actually does, not a description of it: `formula`, `inputs`,
`params`, `outputs`, `warmup_bars`, `reference`, `usage_example`. Outputs carry `units` and
`range`, which is what makes "can I compare these two directly" a question with an exact answer.

All of that authored text is searchable, not just names and summaries. A term that appears only
in a formula or an output description still matches -- it just ranks below the things named for
it.

The edges carry data too. A `uses` edge records **which specific output** the signal reads, which
matters for signals that read more than one indicator.

## Rules of use

- **Results are capped, and say so.** A truncated result carries `truncated` and a note reading
  "showing 10 of 49". **Read it.** A short list is not evidence that there are only ten; raise
  `limit` when you need the total, and never tell the user "there are ten" from a capped result.
- **A miss offers candidates, not a dead end.** A failed lookup comes back with suggestions. If
  you get one, the next move is in the message.
- **Widen before concluding absence.** Try a shorter stem before telling anyone something does
  not exist. If a widened search still returns nothing, that is close to real evidence.
- **`warmup_bars` is an expression, not a number.** It is written in the node's own parameters,
  because warmup depends on how it is configured. Never compare it numerically; evaluate it
  against the parameters you intend to use, and say so when it matters to the user.
- **Units are heterogeneous by design.** A percentage change, a price and an index number are
  different things and are labelled differently. `units=` is an exact match, so read `op=stats`
  and filter on what is actually there.
- **The graph says what the library does, never whether it is a good idea.** A signal existing,
  or carrying the `trigger` role, says nothing about whether it works on the user's data or suits
  their risk. That judgement is yours and it is not in here.

## Worked examples

**"Is there already something that does X?"**

```
query_knowledge  op=find  q="divergence"
query_knowledge  op=find  kind="volatility"  role="trigger"
```

**"What breaks if RSI's output changes?"**

```
query_knowledge  op=neighbors  q="rsi"  relation="uses"  direction="in"  limit=100
```

Every signal that reads it, and each edge names which output.

**"What does this signal actually need to run?"**

```
query_knowledge  op=get  q="rsi_oversold"
query_knowledge  op=neighbors  q="rsi_oversold"  relation="uses"  direction="out"
```

`params` are its knobs with their ranges; `warmup_bars` is an expression in those params; the
neighbours are the indicators beneath it.

**"How is this signal connected to that class?"**

```
query_knowledge  op=path  q="adosc_bearish"  to="momentum"
```

The shortest route is the claim itself. The reason is the longer one: the signal uses an
indicator, and that indicator measures the class.

**"What can I put on one panel, and what runs without a volume feed?"**

```
query_knowledge  op=outputs  bounded=true  kind="oscillator"  limit=100
query_knowledge  op=find     role="trigger"  requires="volume"  limit=100
query_knowledge  op=find     status="deprecated"  limit=100
```

Bounded values share an axis. Deprecated ones stay out of anything new.

## Do not

- **Do not guess an id.** `op=find` first; lookups resolve names, but `find` shows you the real
  one and tells you whether it exists at all.
- **Do not read a capped list as a complete one.** See the rules above; this is the single
  easiest way to tell a user something false.
- **Do not treat a class as a role, or a role as a class.** They intersect. Asking for one when
  you mean the other returns a plausible, wrong set.
- **Do not answer a "what exists" question from memory.** That is what this is for.
