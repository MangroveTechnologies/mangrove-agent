# Strategy Lifecycle — Create, Paper, Live

End-to-end walkthrough of what actually happens when a strategy is
created, promoted to paper, and promoted to live. Not speculation —
every step references the file and function that does the work.

## 1. The scheduler is in-process, not a Unix cron

Everything runs inside one `uvicorn src.app:app` process. No separate
daemon, no system cron, no Cloud Scheduler.

```
uvicorn process (one process, always running)
│
├─ FastAPI (HTTP + MCP endpoints — thread 1)
│
└─ APScheduler BackgroundScheduler      [server/src/services/scheduler_service.py:65-81]
   │  • Jobstore: SQLAlchemyJobStore on agent.db
   │    └─ job persistence — survives restarts
   │  • Executor: ThreadPoolExecutor(max_workers=10)
   │    └─ each tick runs in a thread, non-blocking
   │  • Cron triggers per timeframe:
   │       5m  → minute: */5
   │       15m → minute: */15
   │       1h  → minute: 0
   │       4h  → minute: 0, hour: */4
   │       1d  → minute: 0, hour: 0
   │
   └─ When a cron fires:
      └─ calls `src.services.strategy_service:tick(strategy_id)`
         in a worker thread
```

Key properties:
- **No extra infra.** One process. `setup.sh` + `docker compose up` is
  all that's required.
- **Jobstore lives in `agent.db`** alongside trades/strategies/wallets.
  Single file, single source of truth.
- **Restart-safe.** If uvicorn crashes mid-tick, that tick is lost but
  the job registration survives — next start picks the schedule back
  up.
- **Invariant: as long as uvicorn is running, strategies tick.**
  Close the laptop or kill the process → nothing fires.

### What the agent persists locally, in all cases

Every tick leaves a complete local record in `agent.db`, regardless of
evaluation lane (#151):

| Table | Written by | What |
|---|---|---|
| `evaluations` | `strategy_service.tick` | Verbatim engine response per tick, incl. `execution_state` |
| `strategies.execution_state_json` | `strategy_service.tick` (migration 005) | Latest engine account/risk state per strategy — the value the stateless lane round-trips |
| `trades` | `order_executor` | Every fill, paper or live; exit trades carry `p_and_l` |
| `positions` | `order_executor._maintain_position` | Opened on entry fills (keyed to the engine's position id), closed with P&L on exit fills |

The division of labor: the **engine decides what to trade** (signals,
sizing, bracket exits — only `status: "filled"` engine orders execute
here); the **agent decides when to ask** (its scheduler), executes, and
keeps the books. `evaluation_lane` (`server` default | `stateless` once
MangroveAI#840 ships) chooses where the engine's position state lives —
the local record above exists either way.

## 2. Deploy to paper

```
User                        Tool (MCP)                     What happens
────────────────────────────────────────────────────────────────────────
"Promote to paper"  →   update_strategy_status(            strategy_service.update_status():
                          strategy_id=...,                  1. Validate transition (draft→paper OK;
                          status="paper"                       no confirm required, no allocation)
                        )                                   2. mangroveai.strategies.update_status(
                                                                mangrove_id, "paper")   [upstream sync]
                                                            3. scheduler_service.register_job(
                                                                 strategy_id,
                                                                 timeframe=row.timeframe,
                                                                 callable_path=
                                                                   "src.services.strategy_service:tick"
                                                               )
                                                               └─ APScheduler.add_job(
                                                                    func=callable_path,
                                                                    trigger=CronTrigger(**cron_for_tf),
                                                                    args=[strategy_id],
                                                                    id=f"eval-{strategy_id}",
                                                                    replace_existing=True,
                                                                    coalesce=True, max_instances=1,
                                                                  )
                                                            4. Local status: draft → paper
```

### What runs on each tick (paper)

[`server/src/services/strategy_service.py:455-569`]

```python
def tick(strategy_id):
    # 1. Load strategy, skip if not paper/live
    row = _get_row(strategy_id)
    mode = row["status"]        # "paper"

    # 2. Ask upstream what to do
    sdk_resp = mangroveai.execution.evaluate(
        row["mangrove_id"],
        persist=False            # paper doesn't persist to MangroveAI side
    )

    # 3. Extract order intents (could be [])
    order_intents = [...]

    # 4. Log the evaluation (always, even on empty)
    trade_log.log_evaluation(Evaluation(...))

    # 5. If there are intents, execute them
    if order_intents:
        order_executor.execute_many(
            order_intents,
            mode="paper",           # → _paper_fill: simulated, no wallet touched
            strategy_id=strategy_id,
            evaluation_id=evaluation_id,
            # wallet_address, chain_id, slippage_pct not needed in paper
        )
```

**Paper fills are simulated.** `_paper_fill` writes a row to the local
`trades` table using the current market price as `fill_price`,
`mode="paper"`, `status="confirmed"`. No real swap, no real funds, no
wallet required. Paper trades surface in `list_trades` /
`list_all_trades` exactly like live trades, so the strategy's decision
pattern can be verified before funding.

## 3. Deploy to live

```
User                             Tool (MCP)                     What happens
──────────────────────────────────────────────────────────────────────────────
"Back up my wallet"    →  ./scripts/reveal-secret.sh --address  Reveals the secret ONCE in your
                            <addr>                               terminal (off-transcript, off-MCP)

"Confirmed backup"     →  ./scripts/confirm-backup.sh <addr>     Flips wallets.backup_confirmed_at
                                                                  in local DB. Live trading gate
                                                                  now open for that wallet.

"Go live with $5"      →  update_strategy_status(                strategy_service.update_status():
   (after paper run)       strategy_id=...,                       1. Validate: status="live" requires
                           status="live",                             confirm=true + allocation
                           confirm=true,                          2. require_backup_confirmed(
                           allocation={                                allocation.wallet_address)
                             "wallet_address": "0x5ff2...",          → SigningError if not set
                             "token": "USDC",                    3. allocation_service.record_allocation(
                             "token_address": "0x833...",            ..., slippage_pct=0.002)
                             "amount": 5.0,                          → inserts row in allocations table
                             "slippage_pct": 0.002                4. mangroveai.strategies.update_status(
                           }                                         mangrove_id, "live")
                         )                                       5. scheduler_service.register_job(...)
                                                                     └─ same cron, same job_id,
                                                                        replace_existing=True.
                                                                        The tick just runs in live
                                                                        mode now.
                                                                 6. Local status: paper → live
```

### What runs on each tick (live)

Same `tick()` function; different branch in order execution:

```python
if mode == "live":
    active_alloc = allocation_service.get_active_allocation(strategy_id)
    wallet_address = active_alloc.wallet_address
    slippage_pct  = active_alloc.slippage_pct     # from allocation row
    chain_id      = <looked up from wallets table>

order_executor.execute_many(
    order_intents,
    mode="live",
    wallet_address=wallet_address,
    chain_id=chain_id,
    slippage_pct=slippage_pct,
)
# → _live_swap:
#    1. dex.get_quote
#    2. dex.approve_token (if needed) → sign locally → broadcast → poll
#    3. dex.prepare_swap(quote_id, wallet_address, slippage=slippage_pct*100)
#    4. sign locally via wallet_manager.sign
#       (decrypt Fernet ciphertext → eth_account → zero out plaintext)
#    5. dex.broadcast → tx_hash → poll
#    6. Write to local trades table with status="confirmed" + tx_hash
```

Live adds three guarantees over paper:

1. **Client-side signing.** SDK never sees the private key. We decrypt
   in-process with the Fernet master key, sign with `eth_account`, and
   drop the plaintext immediately.
2. **Backup gate.** `execute_one` calls
   `require_backup_confirmed(wallet_address)` — refuses to run if
   `wallets.backup_confirmed_at` is null.
3. **Slippage cap.** `_live_swap` defense-in-depth: `slippage_pct > 0.0025`
   → `SigningError` (Pydantic rejects first at the
   `StrategyAllocationInput`/`SwapRequest` boundary).

## 4. Minimum tool set for the paper → live loop

| Step | Tool |
|---|---|
| Create | `create_strategy_autonomous` OR `search_reference_strategies` + `build_strategy_from_reference` + `create_strategy_manual` |
| Backtest | `backtest_strategy` |
| Review | agent uses `threshold_spec` values from `server/src/services/data/threshold_spec.json` |
| Promote paper | `update_strategy_status(status="paper")` |
| Watch paper | `list_evaluations`, `list_trades` |
| Create wallet | `create_wallet` (secret returned via `vault_token` → `reveal-secret.sh`) |
| Import wallet | `./scripts/stash-secret.sh` → `import_wallet(vault_token)` |
| Back up wallet | `./scripts/reveal-secret.sh --address <addr>` + `./scripts/confirm-backup.sh <addr>` — BOTH OUTSIDE MCP ON PURPOSE, keys never enter the transcript |
| Fund wallet | user sends USDC to the address — external |
| Go live | `update_strategy_status(status="live", confirm=true, allocation={...})` |
| Monitor | `list_trades`, `list_evaluations`, `get_balances`, `portfolio_value`, `portfolio_pnl`, `get_tx_status` |

## 5. Design choices considered and rejected

| Alternative | Why not |
|---|---|
| Unix `crontab` + `curl` per user | Each user has to configure system cron; unusable from a Claude Code flow |
| Separate Python daemon + IPC | More moving parts, more things to restart, more failure modes |
| Cloud Scheduler / EventBridge | Out of scope for local-first v1; requires cloud deployment |
| Celery / RQ worker | Heavier than needed for 1-10 strategies per user |

The in-process APScheduler gives:
- Zero infra — one process
- Jobstore persistence — restart-safe
- Thread isolation — ticks don't block HTTP requests
- Standard cron syntax — easy to reason about

## 6. What can go wrong

| Symptom | Cause | Fix |
|---|---|---|
| Strategy promoted to paper but no evaluations after N minutes | uvicorn isn't running | check `ps`/`/health`; restart via `./scripts/setup.sh` |
| Tick fires but `list_evaluations` shows `status="error"` | SDK call failed (MangroveAI down, stale API key, etc.) | check `agent-data/bare.log` for the exception; `evaluation.error_msg` field has the SDK message |
| Live tick rejected with "not backed up" | Wallet's `backup_confirmed_at` is null | run `./scripts/reveal-secret.sh --address <addr>` + `./scripts/confirm-backup.sh <addr>` |
| Live tick rejected with slippage out of range | Allocation had `slippage_pct > 0.0025` | re-promote with `slippage_pct ≤ 0.0025` |
| Scheduler job count is 0 after restart | Jobstore wasn't persisted (DB path mismatch or `:memory:` in tests) | `status` tool reports `active_cron_jobs`; check `DB_PATH` config |
