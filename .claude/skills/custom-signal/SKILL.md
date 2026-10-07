---
name: custom-signal
description: Build a custom signal stack (entry and/or exit) for a trading strategy by composing atomic signals from the Mangrove signal library. Use when the user describes a rule in natural language that doesn't match a curated reference — "buy when RSI is low and volume spikes", "exit when price drops below 20 EMA", "only enter when ADX > 25 filter is true" — OR when `/create-strategy` Phase C territory is hit. Outputs a `agent_create_strategy_manual`-compatible `entry` and/or `exit` payload ready to hand off to `/backtest`.
user_invocable: true
argument-hint: "<natural-language rule>"
---

# Build a Custom Signal Stack

Parse `$ARGUMENTS` as a natural-language description of the trading rule the user wants to express. Compose it out of atomic signals already in the Mangrove library — do NOT fabricate new signal logic.

This skill scopes **narrower** than `/create-strategy` — it builds just the `entry` / `exit` signal payload, not a whole strategy. Use it inside `/create-strategy`'s Phase C when a reference doesn't match, or standalone when the user wants to iterate on signals in isolation before committing to a full strategy.

## Flow

### 1. Enumerate what's available

Call `list_signals()` (MCP tool or REST `/api/v1/agent/signals`) to get the atomic catalog. Signals come back with:
- `name` (e.g. `rsi_cross_up`, `sma_cross_down`, `stoch_oversold`)
- `category` (`trend`, `momentum`, `oscillator`, `volume`, `volatility`, `pattern`)
- `signal_type` (`TRIGGER` or `FILTER`)
- `metadata.params` (param names, types, min/max, descriptions)

Group the catalog by `category` in your head so the mapping step goes fast.

### 2. Map the user's rule to atomics

For each clause in the rule, find the closest atomic. Examples of good mappings:

| User says | Atomic mapping |
|---|---|
| "buy when RSI is low / oversold" | `rsi_cross_up(window=14, threshold=30)` as TRIGGER |
| "sell when RSI is high / overbought" | `rsi_cross_down(window=14, threshold=70)` as TRIGGER |
| "buy when price dips below the 10 MA" | `sma_cross_down(window_fast=1, window_slow=10)` as TRIGGER (SMA(1) = close price) |
| "buy when MACD crosses up" | `macd_bullish_cross(12, 26, 9)` as TRIGGER |
| "only trade when trend is strong" | `adx_strong_trend(14, 25)` as FILTER |
| "only trade when price is above the 50 MA" | `is_above_sma(50)` as FILTER |
| "confirm with stochastic oversold" | `stoch_oversold(14, 3, 20)` as FILTER |

Not every English phrase has a clean atomic mapping. If you can't map cleanly, say so explicitly — do not fabricate. Offer alternatives ("RSI-bounce is the closest atomic to 'bought dip', OK to use?").

### 3. Enforce composition rules

From the MangroveAI signal spec:

- **Entry group**: EXACTLY ONE TRIGGER + ZERO OR MORE FILTERS.
- **Exit group**: ZERO OR ONE TRIGGER + ZERO OR MORE FILTERS.
- **NEVER** a FILTER without a TRIGGER in the same group.
- **Single-responsibility**: each signal does ONE thing. Don't stack two TRIGGERS in the same group. Don't mix concepts (no "RSI oversold AND MACD bullish cross" as two TRIGGERs — pick the primary driver, demote the other to a FILTER or drop it).

If the user's rule implies more than one TRIGGER, flag it, pick the primary one, and ask before proceeding.

### 4. Propose parameters — KB-grounded

For each signal, if the user didn't specify parameters explicitly:

- If you're using the catalog default, say so one-line ("RSI(14, 30/70) — catalog default, standard mean-reversion").
- If you're proposing tighter/looser parameters, call `kb_search("<signal_name> parameters <asset> <timeframe>")` and cite a KB entry for the choice.
- Never silently swap in "better" defaults without saying what you did and why.

### 5. Output the payload

Produce a `agent_create_strategy_manual`-compatible block. Example output:

```json
{
  "entry": [
    {
      "name": "rsi_cross_up",
      "signal_type": "TRIGGER",
      "timeframe": "1h",
      "params": {"window": 14, "threshold": 30}
    },
    {
      "name": "adx_strong_trend",
      "signal_type": "FILTER",
      "timeframe": "1h",
      "params": {"window": 14, "threshold": 25}
    }
  ],
  "exit": [
    {
      "name": "rsi_cross_down",
      "signal_type": "TRIGGER",
      "timeframe": "1h",
      "params": {"window": 14, "threshold": 70}
    }
  ]
}
```

State the `timeframe` once per rule (the skill's caller decides — 1d for daily-MA rules, 5m for scalp, etc.) and apply it to every signal in the payload for consistency.

### 6. Hand off

Three options for the user after the payload is produced:

1. **Straight to `agent_create_strategy_manual`** — wrap the payload with `name`, `asset`, `timeframe`, `execution_config` and call the MCP tool. Use defaults from `trading_defaults.json` for `execution_config` (initial_balance: 10000, etc.).
2. **`/backtest` on an existing strategy id** — if the user is refining signals on a strategy that already exists.
3. **Explore alternatives** — "want me to try a tighter RSI threshold and see how the backtest changes?"

## What this skill does NOT do

- Does NOT upload new signal LOGIC to the backend. Custom formulas that can't be expressed as compositions of existing atomics require upstream MangroveAI support (a user-defined-signal registry) which doesn't exist yet.
- Does NOT modify reference-strategy entries. References are immutable provenance — if the user wants to tweak a ref, build a new custom stack here instead.
- Does NOT skip the TRIGGER/FILTER composition rules. Those are enforced by the MangroveAI executor; a payload that violates them will be rejected at create time.
- Does NOT default to invented parameters. KB-grounded or catalog-default only.

## Failure modes

- **No atomic matches the rule.** Tell the user plainly. Offer the closest alternative and ask if that's acceptable. Don't build something that doesn't match what they asked.
- **Rule implies multiple TRIGGERs.** Flag the composition violation, propose which one should be the TRIGGER, and ask before building.
- **Rule references a concept not in the catalog at all** (e.g. "orderbook imbalance", "funding rate delta"). Say so. Point at the KB if it has relevant docs, otherwise report INSUFFICIENT_SIGNALS.
