---
name: market-intelligence
description: >-
  Read what an asset is doing right now and what regime it has been in, and turn that
  into a recommendation. Reach for it whenever a price, a market condition or a strategy
  style is at stake: "how is ETH doing", "is BTC trending", "should I run mean reversion
  on SOL", "what's the volatility like", "what can I trade". Uses get_market_data for a
  live price, get_ohlcv for the price history direction and volatility are read from,
  oracle_list_datasets for how the sweep catalog labelled a stretch of the past, and
  list_approved_assets for what the platform allows a strategy to be built on.
uses-tools: [get_market_data, list_approved_assets, get_ohlcv, oracle_list_datasets]
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/market-intelligence/SKILL.md by scripts/sync-michael-skills.py. Do not edit here: change the skill upstream, or the script's adaptation tables, and re-run the sync. -->

# Read the market before you recommend anything

Two tools tell you what an asset is doing. `get_market_data` is a spot reading: the price
right now. `get_market_regime` is the shape of the last year: which way the asset has been
going over three horizons, and how volatile it has been against its own history.

> **Not in mangrove-agent yet: `get_market_regime`.** Read direction and volatility yourself: `get_ohlcv` daily closes over 90, 180 and 365 days (returns, and realised volatility against the asset's own longer history), plus `get_market_data` for today. Say the reading is yours, not a platform regime label.

They answer different questions and neither substitutes for the other. A price tells you
nothing about whether a strategy style fits. A regime tells you nothing about what the
asset costs today.

## Start here

A live number is never answered from memory. Prices move; whatever you remember is stale
and stating it is worse than saying you will look. Call the tool.

Before recommending a strategy STYLE, call `get_market_regime` *(not in this agent yet)*. Mean reversion and trend
following suit opposite conditions, so a recommendation made without the regime is a guess
dressed as advice.

```
get_market_regime  asset=BTC
get_market_data    symbol=BTC
```

`get_market_data` is rate limited and says so in its own response. One call per asset per
conversation, and reuse the answer. If you called it two turns ago, the price is still the
price you were given; do not call it again to be sure.

## What the regime actually says

`asof` is the day it was computed. Then two independent readings.

**Direction, over three horizons.** Each carries a return and a band:

```
"direction": {"90d":  {"return_pct": -17.6, "band": "bear"},
              "180d": {"return_pct":  -4.6, "band": "neutral"},
              "365d": {"return_pct":  -4.6, "band": "neutral"}}
```

**Volatility, against the asset's own baseline:**

```
"volatility": {"vol_ann_pct": 22.5, "z_vs_baseline": -1.78, "bucket": "low"}
```

`vol_ann_pct` is annualised volatility as a percentage. `z_vs_baseline` is how unusual
that is FOR THIS ASSET -- negative means calmer than its own normal, positive means
rougher. The z-score is the part worth reading: 22% annualised is placid for a small cap
and elevated for a large one, and the z-score already accounts for which this is. Quote
the bucket and the z-score together, never the raw percentage alone.

## Classifying one stretch of the past

`get_market_regime` *(not in this agent yet)* answers "what is this asset like now". `classify_market_segment`
answers "what was this particular stretch like", for a date range someone names or for a
catalog window by its file name.

> **Not in mangrove-agent yet: `classify_market_segment`.** For a sweep-catalog window, `oracle_list_datasets` rows carry the same classifier's `direction`, `volatility`, `trend`, `regime_composite` and `market_era`. For any other date range there is no classifier here: describe it from `get_ohlcv` and do not present that as a catalog label.

```
classify_market_segment  asset=BTC start_date=2026-02-01 end_date=2026-05-01
classify_market_segment  window_file=btc_kraken_2026-06-09_2026-09-09_15m.csv
```

Reach for it when the person asks about a period rather than about today, when a sweep has
selected windows and they want to know what is in them, or when a strategy behaved
differently across two stretches and the difference needs a name.

It returns one direction band over the whole stretch, one volatility band, a trend reading
of clean, mixed or choppy, the era it starts in, and the three numbers behind them. The
trend reading is the one the other tool does not have: it says whether the move was orderly
or noisy, which is what separates a market a trend strategy could hold through from one
that would have whipsawed it.

**These labels are the catalog's.** A stretch classified here can be set beside the windows
a sweep runs over, because both were read by the same classifier. `get_market_regime` *(not in this agent yet)*
cannot be compared that way -- it measures volatility against the asset's own baseline, so
its buckets mean something different.

**`volatility: unscored` is a limit of the model, not a gap in the data.** Volatility is
fitted per stretch length, and only some lengths have a fitted band. The response carries
`scale_bands`, so when it comes back unscored you can say which lengths do score rather
than implying something is wrong with the market. Direction and trend are always there.

## When the horizons disagree

They often will, and the disagreement is the finding, not a problem to resolve. The
example above is down 17.6% over 90 days and down 4.6% over a year: a recent leg down
inside a flat year. That is a different situation from a steady decline, and it changes
what you would recommend.

Read them shortest to longest and say what the shape is. Do not average them, and do not
pick the one that supports the recommendation you were already going to make.

## What these tools do not give you

- No percentage move over any window shorter than 90 days.
- No candles. (In this agent `get_ohlcv` does return candles -- provider-native bars, daily by
  default -- and `get_market_data` carries the 24h change; the limits in this list are the
  regime reading's own.)
- No order book, no liquidity, no spread.
- No forecast. The regime is a description of what has happened, and nothing in it
  predicts what happens next. Say what it shows; do not extend it into a prediction.

If a question needs any of that, say what you can answer with and what you cannot, and
stop there. Filling the gap from training knowledge is the failure this skill exists to
prevent.

## Rules of use

- Regime before style. Every time.
- A question about a named period gets `classify_market_segment` *(not in this agent yet)*, not `get_market_regime` *(not in this agent yet)*
  with a lookback that roughly covers it. The two measure volatility differently and only
  one is comparable with the sweep catalog.
- One `get_market_data` call per asset, then reuse it.
- Volatility is the bucket plus the z-score, never the bare percentage.
- Horizons that disagree get reported as a shape, not collapsed into one number.
- `lookback_days` caps at 365, and fewer than 15 daily bars returns an error rather than a
  guess. A thinly traded asset may simply not have enough history, and that is the answer.
- An asset that fails to resolve comes back as an error naming the asset. Report it; do not
  substitute a similar symbol.

## Do not

- Do not state a price, market cap or volume from memory.
- Do not call an asset "volatile" or "calm" from the annualised percentage without the
  z-score, and do not call it either from the price alone.
- Do not turn a regime into a forecast, or a single horizon into "the trend".
- Do not read `unscored` volatility as missing data, and do not substitute the baseline
  volatility from `get_market_regime` *(not in this agent yet)* in its place. They are different measurements.
- Do not recommend a strategy style before reading the regime, and do not recommend one
  the regime contradicts without saying plainly that you are doing so and why.

## Check what may be traded before proposing anything

`list_approved_assets` is the platform's list, not a preference. An asset that is not on it
cannot be traded here, so a strategy composed on it is a strategy nobody can run.

Check it when the person names something unusual, and check it before you suggest an asset
yourself -- recommending one that turns out to be unavailable wastes the whole exchange.
Each entry carries the risk score the approval rested on plus the market cap and volume
behind it, so if someone asks why an asset is or is not there you can say what the number
was rather than that a list said so.

Stablecoins and tokenized assets are deliberately absent: they are not strategy assets.
An empty list means nothing is approved, which is an answer -- say it plainly rather than
treating it as a failure or falling back on an asset you remember.
