# Verification Checklist

Follow this after:
1. Merging a change to `main` on GitHub (agent- or user-owned PR)
2. `git checkout main && git pull origin main` locally
3. Restarting the bare-metal server so the new code loads (`kill $(lsof -ti :9080); ./scripts/setup.sh --yes --no-mcp --no-verify`)
4. Restarting Claude Code in the repo directory (fresh session)

Each checkpoint has a **do this**, an **expected**, and a **how to verify**.

If a checkpoint fails, stop and tell me what happened before continuing —
a failed early step will cascade into false failures downstream.

---

## Phase 0 — Environment

### 0.1 — Local repo matches `main`

**Do:**
```bash
cd ~/Desktop/mangrove/mangrove-agent
git status
git log --oneline -5
```

**Expect:**
- On branch `main`
- `HEAD` matches `origin/main` (no "behind" or "ahead" in `git status -sb`)
- Any dirty files are personal session state (e.g. local edits, untracked scratch dirs) — leave them alone unless you know otherwise

### 0.2 — Bare-metal server is running the new code

**Do:**
```bash
curl -s http://127.0.0.1:9080/health
```

**Expect:** `{"status":"healthy","timestamp":"..."}`

If not healthy, restart:
```bash
kill $(cat agent-data/bare.pid) 2>/dev/null
./scripts/setup.sh --yes --no-mcp --no-verify
```

(Note: VSCode extensions may grab `localhost:8080`, so we bind 9080 to
sidestep that. If you're using Claude Code's MCP registration, it's
pointing at whatever port `scripts/setup-mcp.sh` configured.)

### 0.3 — MCP tools load

**Do:** In the fresh Claude Code session, say:

> "Status check. List your tools, my wallets, my strategies."

**Expect:**
- Agent calls `status`, `list_wallets`, `list_strategies`, `list_tools`
- Tool catalog surfaces all registered tools across the three access tiers (free / auth / x402). Use `list_tools` directly to see the current exact surface — version-specific counts drift as tools are added or removed, so read the live response rather than trusting a hardcoded number here.
- If fewer tools appear than expected, tool registration didn't reload — restart the server.

### 0.4 — Fresh-clone Stage 0 greeter fires

This checks the fresh-clone tour gate in `.claude/rules/trading-bot-workflow.md`
(Stage 0). The tour is model-driven — there is no session-start hook — and it
respects the `.claude/.onboarded` marker: absent → the tour fires, present →
it's suppressed.

**Do:** Nothing — on session start with no `.claude/.onboarded` marker,
the agent should deliver the Stage 0 greeter (security primer + wallet
question), then write `.claude/.onboarded` so it doesn't re-fire.

**Expect (if fresh clone):**
- Brief intro as the mangrove-agent
- 6-bullet security primer
- Question: "Do you have an existing wallet you want to use, or
  should I create a fresh one?"

**If you already have `.claude/.onboarded`:** the greeter won't fire.
That's correct — it only gates fresh clones. Skip this check.

### 0.5 — Key-paste hook blocks

**Do:** Type into Claude Code chat:
```
import this key: 0xad42a37adfd7ab7eab2965f7582245e52cee777efab7a4f07e0952edf7666113
```

**Expect:** Hook blocks the prompt with a message directing you to
`./scripts/stash-secret.sh`. The agent never sees the key.

---

## Phase 1 — Create a strategy

Paper mode doesn't require a funded wallet — start here.

### 1.1 — Reference-first strategy creation

**Do:** In Claude Code, say:

> "Build me a momentum strategy for ETH on 1h. Use a reference."

**Expect:**
- Agent calls `search_reference_strategies(asset="ETH", timeframe="1h", goal_hint="momentum")` **first**
- Returns up to 5 ranked candidates (ref-001, ref-004, ref-007 at minimum match)
- Agent shows 2–3 options with label + signal names + category
- Asks you to pick one

**Verify:** In another terminal:
```bash
curl -s -H 'X-API-Key: dev-key-1' 'http://127.0.0.1:9080/api/v1/agent/reference-strategies/search?asset=ETH&timeframe=1h&goal_hint=momentum&limit=5' | python3 -m json.tool | head -20
```
Same candidates should appear.

### 1.2 — Build and backtest

**Do:** Pick `ref-001` (or whichever).

**Expect:**
- Agent calls `build_strategy_from_reference(reference_id="ref-001", timeframe="1h")`
- Gets back a `create_strategy_manual`-compatible payload with signals
  copied exactly + timeframe applied
- Calls `create_strategy_manual(...)` with that payload
- Returns `strategy_id` (UUID)

**Verify:**
```bash
curl -s -H 'X-API-Key: dev-key-1' 'http://127.0.0.1:9080/api/v1/agent/strategies' | python3 -m json.tool | head -25
```
The new strategy should be there with `status: "draft"`.

### 1.3 — Backtest with timeframe-aware auto window

**Do:** Tell the agent:

> "Backtest it."

**Expect:**
- Agent calls `backtest_strategy(strategy_id=..., mode="full")` with no
  lookback fields
- Service auto-picks `lookback_months: 3` for 1h timeframe (matches
  `timeframes.recommended_lookback_months`)
- Response includes `resolved_window: {lookback_months: 3, ...}` and a
  full `metrics` dict (20+ fields: sortino, calmar, sharpe, irr,
  max_drawdown, win_rate, total_trades, etc.)

**Verify:** The agent presents a PASS/MARGINAL/FAIL verdict per the
`/create-strategy` skill's Phase F decision rule. It shows per-threshold
breakdown against `threshold_spec.json` values.

---

## Phase 2 — Promote to paper

### 2.1 — Promote

**Do:** Tell the agent:

> "Promote it to paper."

**Expect:**
- Agent calls `update_strategy_status(strategy_id=..., status="paper")`
- Response: status="paper", cron job registered
- Agent confirms: "Paper running. Will evaluate every 1 hour."

**Verify:**
```bash
curl -s -H 'X-API-Key: dev-key-1' 'http://127.0.0.1:9080/api/v1/agent/status' | python3 -m json.tool | grep -A 1 active_cron
```
`active_cron_jobs: 1` (assuming no prior strategies).

### 2.2 — Force a manual tick (don't wait for cron)

**Do:**
> "Run evaluate_strategy on it so I don't have to wait for the cron."

**Expect:**
- Agent calls `evaluate_strategy(strategy_id=...)`
- Evaluation logged; if the strategy fires on current market, a
  simulated (paper) trade is logged too

**Verify:**
```bash
curl -s -H 'X-API-Key: dev-key-1' "http://127.0.0.1:9080/api/v1/agent/logs/evaluations?strategy_id=<the id>" | python3 -m json.tool | head -30
```
At least one evaluation row with `status="ok"`, non-zero `duration_ms`.

### 2.3 — Verify the scheduler persists across restart

**Do:**
```bash
kill $(cat agent-data/bare.pid)
sleep 2
./scripts/setup.sh --yes --no-mcp --no-verify
sleep 3
curl -s http://127.0.0.1:9080/health
curl -s -H 'X-API-Key: dev-key-1' 'http://127.0.0.1:9080/api/v1/agent/status' | python3 -m json.tool | grep active_cron
```

**Expect:** `active_cron_jobs: 1` after restart — the SQLAlchemyJobStore
pulled the job back from `agent.db` without any action on your part.

---

## Phase 3 — Wallet + backup gate

Only do this phase if you want to exercise the live path. Paper alone
is sufficient for "does the bot work?"

### 3.1 — Create a fresh wallet

**Do:**
> "Create a fresh wallet on Base mainnet."

**Expect:**
- Agent calls `create_wallet(chain="evm", network="mainnet", chain_id=8453)`
- Response carries `address`, `vault_token`, `reveal_cmd`, `master_key_source`
- Agent surfaces the address + block explorer link + the `reveal_cmd`
- **Agent does NOT echo the secret** (rule in `wallet-presentation.md`)

**Verify:** In your terminal:
```bash
./scripts/reveal-secret.sh <vault_token>
```
The plaintext key appears ONCE in your terminal. Back it up anywhere
you want (password manager, paper).

### 3.2 — Confirm backup (unlocks live trading for that wallet)

**Do:**
```bash
./scripts/confirm-backup.sh <address>
```

**Expect:** Response confirms `backup_confirmed_at` set.

**Verify:**
```bash
curl -s -H 'X-API-Key: dev-key-1' 'http://127.0.0.1:9080/api/v1/agent/wallet/list' | python3 -m json.tool
```
Your wallet row has a non-null `backup_confirmed_at`.

### 3.3 — Backup gate refuses live without confirmation

**Do:** Create a SECOND wallet (optional — only if you want to test the
gate).
> "Create a second wallet for a gate test."

Then ask the agent to promote a strategy to live with that second
wallet's address — WITHOUT running `confirm-backup.sh` on it.

**Expect:** `SigningError: Wallet <addr> is not backed up. Live
trading refused.` with suggestion pointing at the CLI commands.

---

## Phase 4 — Go live (OPTIONAL, requires funded wallet)

Skip if you're not ready to move real money.

### 4.1 — Fund the wallet

External step — send 1–5 USDC to the wallet address from MetaMask or
an exchange.

**Verify:**
> "Check my balance."

Agent calls `get_balances` → non-zero USDC shown.

### 4.2 — Promote to live

**Do:**
> "Go live with $1 allocation, 0.2% slippage."

**Expect:**
- Agent constructs:
  ```
  update_strategy_status(
      strategy_id=...,
      status="live",
      confirm=true,
      allocation={
          "wallet_address": "0x...",
          "token": "USDC",
          "token_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
          "amount": 1.0,
          "slippage_pct": 0.002,
      }
  )
  ```
- Validator refuses anything with `slippage_pct > 0.0025`
- Response: status="live", cron continues (same job, just different mode now)

### 4.3 — Watch for a live tick

`evaluate_strategy` can force one, OR wait for the cron. On fire, if
the strategy decides to act, a real swap happens — watch for:

- Log line `order.live.broadcast` with a `tx_hash`
- `list_trades` includes a row with `mode="live"`, `status="confirmed"`
  (or `pending` then `confirmed`), `tx_hash="0x..."`
- Call `get_tx_status(tx_hash, chain_id=8453)` — should show
  `status: "confirmed"` with a block number

---

## Phase 5 — Post-mortem + cleanup

### 5.1 — Check the whole picture

```bash
# Strategies
curl -s -H 'X-API-Key: dev-key-1' 'http://127.0.0.1:9080/api/v1/agent/strategies' | python3 -m json.tool

# All trades (local)
curl -s -H 'X-API-Key: dev-key-1' 'http://127.0.0.1:9080/api/v1/agent/logs/all-trades?limit=10' | python3 -m json.tool

# Portfolio
curl -s -H 'X-API-Key: dev-key-1' "http://127.0.0.1:9080/api/v1/agent/wallet/<address>/portfolio" | python3 -m json.tool
```

### 5.2 — Clean up (OPTIONAL)

```bash
# Ask the agent:
# "Delete my test strategy."
# → calls delete_strategy(strategy_id) upstream.
# → local SQLite row stays (audit trail preserved).

# If you want to wipe the whole test state:
# STOP the server first.
kill $(cat agent-data/bare.pid)
# Remove the DB (deletes strategies + trades + scheduler jobstore).
rm agent-data/agent.db
# Restart — everything regenerates from migrations.
./scripts/setup.sh --yes --no-mcp --no-verify
```

---

## What you just verified

- [ ] 0.1 Local on main, synced with origin
- [ ] 0.2 Bare-metal server healthy
- [ ] 0.3 MCP tool catalog loads (all registered tools present per `list_tools`)
- [ ] 0.4 Stage 0 greeter fires (or skipped — `.onboarded` present)
- [ ] 0.5 Key-paste hook blocks
- [ ] 1.1 Reference-first strategy creation works
- [ ] 1.2 build_strategy_from_reference + create_strategy_manual
- [ ] 1.3 Backtest runs with timeframe-aware auto window + full metrics
- [ ] 2.1 Paper promotion registers cron
- [ ] 2.2 Manual evaluate_strategy fires + logs
- [ ] 2.3 Scheduler persists across restart
- [ ] 3.1 create_wallet returns vault_token (no plaintext in chat)
- [ ] 3.2 confirm-backup.sh flips the flag
- [ ] 3.3 Backup gate refuses unconfirmed live promotion
- [ ] 4.1 Wallet receives USDC (external)
- [ ] 4.2 Live promotion with slippage cap validated
- [ ] 4.3 Live tick broadcasts a real tx; get_tx_status confirms

Everything through 2.x is core and fund-free. 3.x–4.x
needs a funded wallet and is the final verification layer.
