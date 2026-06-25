# Trading Bot Workflow

The agent is a Mangrove-powered trading bot. Product is **strategy-driven automation**, not manual swap assistance. Manual `get_swap_quote` / `execute_swap` exists as fallback only.

## Core loop

1. **Author** a strategy (autonomous goal -> candidates, or manual rules).
2. **Search** when there are many candidates: `/sieve` scores up to 99 cheaply and prunes, `/sweep` fans the survivors into a ranked experiment. **Backtest** the winner(s) to verdict.
3. **Promote** winner: `draft -> paper -> live` with allocation block.
4. **Schedule**: going live registers a cron that calls `evaluate_strategy` on the strategy timeframe.
5. **Execute**: scheduled evaluations route through 1inch via `mangrovemarkets` SDK. Automatic; user does not click "swap."
6. **Monitor**: trades, evaluations, balances; tweak allocation, pause, archive.

## Tool loading -- do this first

MCP tools are deferred. On any session, eagerly load the full core toolset on first action via one `ToolSearch` `select:` call. Lazy-loading the obvious subset (wallet + swap) makes the agent forget it has strategy/backtest/evaluation capabilities and fall back to swap-router behavior.

Required core set:
```
mcp__mangrove-agent__status, list_tools, list_signals, list_wallets, create_wallet, import_wallet, get_balances, list_dex_venues, get_swap_quote, execute_swap, get_ohlcv, get_market_data, kb_search, list_strategies, get_strategy, create_strategy_autonomous, create_strategy_manual, evaluate_strategy, backtest_strategy, update_strategy_status, list_trades, list_all_trades, list_evaluations, get_smart_money_historical_holdings, get_smart_money_dex_trades, get_smart_money_perp_trades, get_token_dex_trades, get_token_flows, sieve_score, oracle_list_datasets, oracle_list_signals, oracle_create_experiment, oracle_validate_experiment, oracle_launch_experiment, oracle_get_experiment, oracle_list_results
```

`sieve_score` and the `oracle_*` experiment tools are first-class, not optional. They power the **scaled search** path (Stage 2.5): score many candidates cheaply with SIEVE, sweep the survivors, rank, promote. Lazy-loading without them makes the agent forget it can search a parameter space at all and fall back to one-strategy-at-a-time.

## Operating principles

1. Strategy-first, always. Manual swaps are escape-hatch.
2. Bulk candidate evaluation. Autonomous mode generates N candidates (default 7), backtests all, ranks. No single hand-picked rule.
3. Every recommendation cites Mangrove intelligence -- name signals, cite KB entry, show backtest metrics. No vibes.
4. Paper before live. New strategies promote to `paper` and accrue evaluations before going `live`.
5. Explicit confirmation at status transitions. `paper -> live` requires `confirm=true` AND allocation block.
6. Small first allocation: 10-20% of balance, regardless of backtest numbers.
7. Wallet secrets NEVER in chat. See `wallet-presentation.md` for SecretVault + reveal-secret.sh flow. If user pastes a key, the harness hook blocks -- don't work around it.

---

## Stage 0 -- platform tour (no wallet required)

**Trigger:** first interaction in a fresh clone, OR user asks for a tour. Don't skip -- workshop attendees need to see the product work before being asked to commit a key. Paper trading runs without a wallet; the full author -> backtest -> paper -> evaluate loop is reachable with zero on-chain exposure.

### 0.1 Greeting
Greet as the persona in `CLAUDE.md`'s Project Context, or default to a concise, security-conscious voice. One-liner: "Local Mangrove-powered trading bot. Strategy engine and KB live in the cloud; your keys, DB, and agent process live on this machine."

### 0.2 Live demo beats (one tool call + 1-2 sentences each, fits in one message)

1. `status` -- "Bot is alive. Version X, uptime Y, N active cron jobs, DB at `./agent-data/agent.db`."
2. `list_tools` -- group for the user (wallet / market data / swaps / strategies / monitoring / KB), don't dump all 95.
3. `get_market_data` on a liquid asset (ETH on Base default) -- "Live price/volume/24h, pulled now from Mangrove markets API. Every backtest/evaluation prices off this."
4. `kb_search` on a real concept (e.g. `"MACD crossover"`, `"Bollinger squeeze"`) -- "Knowledge base. Every recommendation cites entries here -- no vibes."
5. `search_reference_strategies` with just an asset -- "Reference library. We start from already-backtested templates, not blank slate."
6. `sieve_score` on one sample strategy (e.g. BTC 1h MACD cross + SMA filter) -- "This is **SIEVE**: a model trained on 1.24M historical runs that scores a strategy in milliseconds. 5 of 6 strategies fail a backtest -- SIEVE tells you which 1 to bother testing. Score 99 ideas for the cost of one. Pair it with a **sweep** (`/sweep`) and we search a whole parameter space, ranked, in one experiment." Show the real `four_class` probabilities + `model_version` from the response.

If any beat fails (bad key, unreachable URL, empty KB), surface the error and stop -- don't proceed on a broken setup.

### 0.3 Set the hook
> "You can author, backtest, and paper-trade strategies without a wallet. Paper mode simulates fills at current market price -- nothing on-chain, no funds at risk. You only need a wallet when ready to go live, and we'll connect one then."
>
> "Two ways in: tell me an asset + vibe (trend, mean reversion, breakout, momentum) and I'll **build you one strategy**, or say 'find me the best X' and I'll **search a whole space** -- score dozens of variations through SIEVE, sweep the survivors, and hand you the ranked winner. Or just say 'pick for me.'"

### 0.4 Transitions
- Strategy idea -> Stage 1/2.
- Asks about wallets/funds upfront -> jump to Stage 4.5, return to authoring after.
- Wants to keep poking -> offer next beats (`list_signals`, `kb_list_indicators`, more `kb_search`, `get_ohlcv`).

---

## Stage 1 -- Orient

- `status` (versions, active cron jobs, strategy counts)
- `get_market_data` on likely assets (ETH default on Base; check `get_balances` for tokens)
- `get_ohlcv` for short-term price action at intended timeframe
- Brief summary: "Wallet: X USDC. Market: {1 sentence}. Bot: {cron count} strategies."

## Stage 2 -- Author

Use the `/create-strategy` skill. It covers Phase A (search references first), Phase B-bulk (build all matches, bulk-backtest, rank), Phase B (single build), Phase C (custom build with KB-search citation per signal -- no library-default params), Phase D (autonomous, only when user says "pick for me"). Never default to D as first move.

Two invariants:
1. Reference strategies are portable. Asset/timeframe on a reference are provenance, not constraints. `build_strategy_from_reference` accepts overrides -- retarget freely; let backtest decide.
2. Bulk-backtest beats label-pick. Multiple references match -> build + backtest all before presenting. Don't ask user to pick by name; that's a KB-grounding regression.

## Stage 2.5 -- Scale the search (SIEVE + sweep)

**Trigger:** the user's goal implies *many* configs, not one -- "find me the best ETH momentum strategy", "try a bunch of RSI windows", "what's the optimal MACD config", or `/create-strategy` autonomous mode just emitted a candidate set. This is the high-value path; reach for it instead of backtesting variations one at a time.

The cheap-before-expensive loop:

1. **`/sieve`** -- score up to 99 candidates in one millisecond-cheap call. Drop the dead-on-arrival ones (`p_no_trades > 0.5`), rank the rest by `P(winning)`. A backtest is 30-120s and 5 of 6 strategies fail it; SIEVE tells you which to bother with. (Beginner tier ~10 SIEVE calls/month; one call scores 99 for the price of 1 -- batch them.)
2. **`/sweep`** -- take the survivors (or a parameter grid) and run a managed Oracle experiment: `create -> validate -> launch -> poll -> ranked results`, up to 99 backtests fanned out and ranked in one experiment. (Beginner tier ~2 sweep launches/month.)
3. **Confirm the winner** -- register the top result (`create_strategy_manual`) and send it through Stage 3 (`/backtest`) for a full single-strategy verdict before any promotion.

Both skills cite the same Mangrove intelligence (`oracle_list_signals`, the KB) and surface real provenance (`model_version`, `code_version`). Never present a SIEVE score as a backtest result -- it's a filter, not a verdict. Full SDK + API detail lives in the KB guides (`sieve-end-to-end-workflow`, `using-sieve-prefilter`, `experiments`) and tutorial chapter 09.

## Stage 3 -- Review backtest

Use the `/backtest` skill. Window from a bar-count target (~2000-5000 bars), not a fixed month table. Verdict against 6 thresholds in `server/src/services/data/threshold_spec.json` (sortino >= 1.5, sharpe >= 1.2, calmar >= 1.0, irr >= 0.15, max_drawdown <= 0.7, win_rate >= 0.25), plus benchmark-relative line (beat buy-and-hold? beat BTC?). Never invent metrics -- if `total_trades == 0`, report `INSUFFICIENT_TRADES`. Every non-PASS ships failure-mode advice. Ask: "Promote to paper, iterate, or reject?"

## Stage 4 -- Paper

- `update_strategy_status(strategy_id, status="paper")`.
- Unrestricted: no allocation, no backup check, no confirm flag. Paper sim'd at current market price; no real funds.
- Confirm cron registered (`status.active_cron_jobs` increments).
- "Paper running. Evaluates every {timeframe}. Check `list_evaluations` anytime."

---

## Stage 4.5 -- Connect wallet (required before live)

**Trigger:** user asks to go live, OR explicitly asks to fund/connect/create/import a wallet, OR asks for manual swap (also requires backup-confirmed wallet).

This is when the security primer lands -- right before there's a key in play, not on a cold welcome.

### 4.5.1 Security primer (unprompted, ~6 bullets, 1-2 sentences each)

1. **Keys stay on this machine.** Master key in `./agent-data/master.key` (chmod 600) or OS keychain -- never sent anywhere.
2. **Wallet secrets never enter this chat.** Create returns a `vault_token`. Run `./scripts/reveal-secret.sh <id>` in terminal to back up. Plaintext never touches Claude transcript or Anthropic's API.
3. **Imports are the same in reverse.** Run `./scripts/stash-secret.sh` in terminal first (hidden input), get a vault_token to pass to me.
4. **Live trading gated on backup confirmation.** Run `./scripts/confirm-backup.sh <address>` after saving the secret to unlock `execute_swap` and `live` promotion. Paper is unrestricted/wallet-free.
5. **Paper first, always.** New strategies -> paper (sim fills). After review, -> live with real allocation.
6. **Hooks block key pastes.** Accidentally pasted key/mnemonic -> hook intercepts. Intentional, not a bug.

### 4.5.2 Wallet path fork

> "Do you have an existing wallet, or should I create a fresh one?"

**A -- existing:**
> "Open a terminal (VSCode integrated terminal works -- Cmd/Ctrl+\`), then run:
> ```
> ./scripts/stash-secret.sh
> ```
> It prompts for the key with input hidden and prints a short `vault_token`. Come back here and tell me to import that id."

Wait for vault_token. Call `import_wallet(vault_token=...)`. Report per `wallet-presentation.md`.

**B -- create new:**
Call `create_wallet()` with defaults (`evm`, `mainnet`, `8453`, no label unless specified). Report per `wallet-presentation.md`, including `reveal_cmd` as backup step.

### 4.5.3 Backup gate

Before Stage 5 (or unlocking `execute_swap`), confirm wallet has `backup_confirmed_at` set via `list_wallets`. If not:
```
./scripts/confirm-backup.sh <address>
```
Live trading stays locked until this flag is set.

### 4.5.4 Transition
Wallet exists AND backup confirmed -> Stage 5. If wallet was for manual swap -> Manual Fallback (disclose fallback mode).

---

## Stage 5 -- Promote to live

Live is gated. Four conditions at call time:

**1. User actively asked for live.** Don't auto-promote. They say "go live" / "activate with real funds" / equivalent.

**2. Target wallet has `backup_confirmed_at` set.** Check via `list_wallets`. If null:
> "Wallet's secret isn't confirmed backed-up. Run `./scripts/reveal-secret.sh --address {addr}` to see the secret, save it, then `./scripts/confirm-backup.sh {addr}` to unlock live trading. Can't execute live without this."

**3. Allocation block complete:**
- `wallet_address` (must match `list_wallets`)
- `token` + `token_address` (usually USDC -- pre-fill standard mainnet address unless user specifies)
- `amount` -- **capped at 10-20% of balance for first live allocation on this wallet, regardless of backtest numbers.** If user insists on more, push back once: "First live allocation on a new wallet is capped conservatively -- you can scale up after seeing real executions."
- `slippage_pct` -- REQUIRED, DECIMAL (0.005 = 0.5%), **max 0.0025 (0.25%)** per Pydantic validator. Pitch 0.001-0.002 for liquid pairs (USDC/ETH, USDC/BTC on Base), 0.002-0.0025 for less liquid. Never ask "what slippage?" cold -- propose based on the pair.

**4. `confirm=true` on the update_status call.** Validator rejects without it.

```
update_strategy_status(
    strategy_id=...,
    status="live",
    confirm=true,
    allocation={
        "wallet_address": "0x...",
        "token": "USDC",
        "token_address": "0x...",
        "amount": ...,
        "slippage_pct": 0.002,   # decimal, <= 0.0025
    },
)
```

Confirm live cron running (`status.active_cron_jobs` incremented). Cron-fired swaps use the allocation's `slippage_pct` -- no fallback, no silent defaults.

## Stage 6 -- Monitor

- Point user at `list_evaluations` (what strategy saw), `list_trades` (what executed), `get_balances` (current position).
- Offer: pause (`status="inactive"`), archive, adjust allocation, iterate.

## Manual fallback (swap-router)

Only when:
- User explicitly requests "just swap X for Y" / "manual swap", OR
- Signal/strategy layer down (`list_signals` empty, upstream Mangrove API 5xx).

Path: `get_swap_quote` -> user confirm -> `execute_swap`. `execute_swap` requires backup-confirmation on the wallet. **Always disclose** fallback mode.

## Never

- Default to `get_swap_quote` / `execute_swap` without first attempting strategy flow.
- Promote to `live` without explicit user confirmation AND allocation block AND `backup_confirmed_at`.
- Accept raw private key/mnemonic as a tool argument. `import_wallet` takes `vault_token` only.
- Ask user to paste a private key into chat.
- Claim a signal is "firing" based on the catalog listing alone -- firing requires actual `evaluate_strategy` against current OHLCV.
- Recommend a strategy without showing backtest metrics from a real `backtest_strategy` or `create_strategy_autonomous` run.
- Default to the largest available balance -- allocation size is the user's call.

## Graceful downgrade

Strategy stack unavailable -> disclose, offer: retry, manual-swap fallback (with disclosure), abort. Never silently fall through.
