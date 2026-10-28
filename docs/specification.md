# Technical Specification

This is the technical specification for mangrove-agent: its interfaces, behavior, data models,
storage and configuration. For how the pieces fit together, read the
[architecture](architecture.md) first. For the full route list, see the
[API reference](api-reference.md).

## Overview

mangrove-agent is a FastAPI and MCP service that runs on the user's own machine. It holds
wallet keys locally, runs scheduled strategy evaluations, controls what can be signed and
spent, and keeps an audit trail in SQLite. Strategy authoring, backtesting, signals, market
data and strategy evaluation are done by MangroveAI, which the agent reaches over MCP and
through the `mangroveai` SDK. DEX routing and marketplace actions are done by MangroveMarkets.

The agent exposes its functions two ways:

- **MCP tools at `/mcp`** (Streamable HTTP). This is the preferred interface for AI clients
  because tools are discoverable and typed.
- **REST routes at `/api/v1/agent/*`**. Any HTTP client can use them without an MCP library.

Both interfaces call one service layer. For example, the `create_wallet` tool and
`POST /api/v1/agent/wallet/create` call the same function. The agent is single-user and
local-only.

## Access tiers

| Tier | What it covers | How to access |
|------|----------------|---------------|
| Free | `GET /health`, `GET /api/v1/agent/status`, `GET /api/v1/agent/tools`, and the `status` and `list_tools` tools | No credentials |
| Auth | Every other agent route and tool | `X-API-Key` header |
| x402 | `GET /api/x402/hello-mangrove` and the `hello_mangrove` tool, a demo of the payment path | An x402 payment, or a valid `X-API-Key` |

## Authentication

Authentication is a single shared secret between the user's AI client and the agent. There are
no accounts, sessions or roles.

1. The `API_KEYS` config value holds one or more comma-separated keys. `scripts/setup.sh`
   generates a unique key on first run.
2. A request sends `X-API-Key: <key>`. Keys are compared in constant time.
3. A missing key returns 401 `AUTH_MISSING_API_KEY`. A wrong key returns 401
   `AUTH_INVALID_API_KEY`.
4. The two placeholder values published in the example config, `dev-key-1` and
   `GENERATED_BY_SETUP`, never authenticate.
5. When `AUTH_ENABLED` is false, all checks pass.

MCP clients send the same header on every request to `/mcp`. The agent copies the header into
the request context, so a tool authenticates whether the client sends the header or passes the
optional `api_key` argument. A missing or invalid key makes the tool return an error with code
`AUTH_INVALID_API_KEY`.

A Claude Code project registers the server like this:

```json
{
  "mcpServers": {
    "mangrove-agent": {
      "type": "http",
      "url": "http://localhost:9080/mcp/",
      "headers": { "X-API-Key": "<your-configured-api-key>" }
    }
  }
}
```

The key is the agent's own local key. It is not a MangroveAI key and is never forwarded
upstream.

## API contracts

The [API reference](api-reference.md) lists every route with its method, path, access tier and
request shapes. This section records the behavior that the rest of the system depends on.

- **Base path.** Agent routes live under `/api/v1/agent`. x402-gated routes live under
  `/api/x402`.
- **Wallet secrets.** No route or tool returns a plaintext secret except the two reveal routes
  called by the local scripts. `create` and `stash-secret` return a `vault_token`, a
  single-read, in-memory handle that expires after `SECRET_VAULT_TTL_SECONDS`.
- **Backup gate.** Live swaps, live strategy activation and x402 payments are refused for a
  wallet until `POST /wallet/{address}/confirm-backup` has set its `backup_confirmed_at`.
- **Swaps.** `POST /dex/swap` requires `confirm: true` and an explicit `slippage_pct` that is
  greater than 0 and at most `0.0025`. Amounts are in human units.
- **Lifecycle.** `PATCH /strategies/{id}/status` is the only way to activate, deactivate or
  archive a local execution record. Transitions into and out of `live` need `confirm: true`.
- **Retired routes.** `POST /strategies/autonomous`, `POST /strategies/manual` and the
  `/reference-strategies` routes return 410. Creating strategies is done with MangroveAI's
  `load_skill` and `save_strategy` tools.
- **Hosted capabilities.** There are no REST routes for market data, on-chain data, signals,
  the knowledge base, backtests or strategy listing. Use the MCP tools that MangroveAI
  advertises.

## MCP tools

The server registers local tools and also serves tools discovered from MangroveMarkets and
MangroveAI. `list_tools` returns the live catalog with each tool's tier, parameters and,
where known, price. [MCP contracts](mcp-contracts.md) explains the ownership split.

Local tool names are plain `verb_resource` names with no project prefix, except tools that
manage local execution state, which use the `agent_` prefix. Every local tool except the free
tools requires the API key.

| Tool | Tier | Purpose | REST equivalent |
|------|------|---------|-----------------|
| `status` | Free | Agent name, version, counts, kill-switch and budget state | `GET /status` |
| `list_tools` | Free | Tool catalog | `GET /tools` |
| `set_agent_name` | Auth | Save the user-chosen agent name | `PUT /identity` |
| `create_wallet` | Auth | Create and encrypt a wallet; returns a `vault_token` | `POST /wallet/create` |
| `import_wallet` | Auth | Import a wallet from a stashed `vault_token` | `POST /wallet/import` |
| `list_wallets` | Auth | List wallets, without secrets | `GET /wallet/list` |
| `execute_swap` | Auth | Execute a DEX swap | `POST /dex/swap` |
| `cex_status` | Auth | Whether Kraken is connected | `GET /cex/status` |
| `cex_connect_kraken` | Auth | Connect Kraken from a `vault_token` | `POST /cex/connect` |
| `cex_balances` | Auth | Kraken balances | `GET /cex/balances` |
| `cex_validate_order` | Auth | Dry-run a Kraken order | `POST /cex/validate-order` |
| `cex_sync_fills` | Auth | Pull Kraken fills into telemetry | `POST /cex/sync-fills` |
| `marketplace_prepare` | Auth | Prepare a Markets action for local approval | `POST /marketplace/prepare` |
| `marketplace_submit` | Auth | Sign and submit a prepared action once | `POST /marketplace/submit` |
| `x402_spend_status` | Auth | Budget state and recent ledger | `GET /x402/spend`, `GET /x402/spend/payments` |
| `x402_spend_reset` | Auth | Start a fresh budget after the user agrees | `POST /x402/spend/reset` |
| `agent_robinhood_status` | Auth | Robinhood readiness and masked account | `GET /brokers/robinhood/status` |
| `agent_configure_execution_venue` | Auth | Bind an inactive strategy to Robinhood | `PUT /brokers/strategies/{id}/venue` |
| `agent_configure_strategy_market` | Auth | Set market data for an inactive stateless strategy | `PATCH /strategies/{id}/market` |
| `agent_list_venue_orders` | Auth | Local broker order journal | `GET /brokers/strategies/{id}/orders` |
| `agent_reconcile_venue_orders` | Auth | Read broker outcomes and save fills | `POST /brokers/strategies/{id}/reconcile` |
| `agent_cancel_venue_order` | Auth | Cancel a pending venue order | `POST /brokers/orders/{id}/cancel` |
| `agent_start_paper_schedule` | Auth | Start a bounded paper schedule | `POST /strategies/paper-schedules` |
| `agent_list_paper_schedules` | Auth | List schedules | `GET /strategies/paper-schedules` |
| `agent_get_paper_schedule` | Auth | Read a schedule | `GET /strategies/paper-schedules/{id}` |
| `agent_pause_paper_schedule` | Auth | Pause a schedule | `POST /strategies/paper-schedules/{id}/pause` |
| `agent_reconcile_paper_schedule` | Auth | Recover an unresolved evaluation | `POST /strategies/paper-schedules/{id}/reconcile` |
| `hello_mangrove` | x402 | Payment demo; hidden from `list_tools` | `GET /api/x402/hello-mangrove` |

Some REST routes have no local tool: the wallet stash, reveal and confirm-backup routes (which
handle secrets and are called by scripts), the `/cex/oauth/*` routes, the DEX read routes
(`venues`, `pairs`, `tx-status`, `token-info`, `spot-price`, `gas-price`) and `quote`, the
wallet `balances`, `portfolio` and `history` routes, `POST /portfolio/risk/reset`,
`PATCH /strategies/{id}/status`, `POST /strategies/{id}/evaluate` and the execution-log
routes. Markets serves the equivalent DEX and wallet reads as discovered tools. In particular,
no tool resets the portfolio kill switch or confirms a wallet backup.

Tool names that clients should treat as MangroveAI's, not the agent's, include
`load_skill`, `save_strategy`, `run_backtest`, `get_backtest`, `list_strategies`,
`get_strategy`, `list_signals`, `get_ohlcv`, `get_market_data`, `kb_search`, `list_trades`,
`list_positions`, `list_evaluations` and `discover_tools`. They are served as MangroveAI
advertises them, so run `list_tools` for the current set.

## Execution behavior

### Scheduler

The scheduler is APScheduler's `BackgroundScheduler` with a SQLAlchemy job store on the agent's
SQLite file. It starts in the FastAPI lifespan and stops without waiting for in-flight ticks.

Job defaults are `coalesce=True`, `max_instances=1` and `misfire_grace_time=60`, with a
ten-thread executor. Each local `paper` or `live` record has a job `eval-<strategy_id>` that
calls `strategy_service.tick`:

| Timeframe | Cron |
|-----------|------|
| `1m` | `*/1 * * * *` |
| `5m` | `*/5 * * * *` |
| `15m` | `*/15 * * * *` |
| `1h` | `0 * * * *` |
| `4h` | `0 */4 * * *` |
| `1d` | `0 0 * * *` |

An unknown timeframe is refused with `SCHEDULER_ERROR`. Four fixed-interval jobs also run:
`paper-dispatch` (every 5 seconds), `broker-reconciliation` (every 15 seconds),
`x402-reconciliation` (every 15 seconds) and `x402-refunds` (every 30 seconds). The last two
exist only when `X402_RECONCILIATION_RPC_URLS` is configured.

A tick never raises. A failed evaluation is logged as an evaluation with `status: error` and
the strategy stays active.

### Evaluation lanes

Each local record has an `evaluation_lane`. When it is empty the record uses `server`.

| Lane | Call | Source of truth for state |
|------|------|---------------------------|
| `server` | Evaluate the saved strategy by ID, `persist=True` | MangroveAI's database |
| `stateless` | Evaluate the full strategy object with the agent's `execution_state` and open positions, `persist=False` | The agent's `strategies.execution_state_json` and `open_positions_json` |

A stateless strategy can carry explicit market data (`base_token`, `quote_token`,
`market_data_venue`), which `PATCH /strategies/{id}/market` sets while the strategy is
inactive. Binding a strategy to a Robinhood venue forces the stateless lane.

### Order intents

MangroveAI's evaluation returns `new_orders`. The agent converts each order with
`status: filled` and side `enter_long` or `exit_long` into an `OrderIntent`: `action`
(`enter` or `exit`), `side` (`buy` or `sell`), `symbol`, `amount` in asset units, the
evaluation-time `ref_price`, and the engine's position and order IDs and bracket levels.
Resting orders such as pending stop-loss and take-profit brackets are logged and skipped until
the engine reports them filled. An order that fails to convert is skipped with a logged
warning.

### Paper schedules

Server-owned paper execution evaluates a MangroveAI strategy on MangroveAI's side, on a clock
the agent supplies. See [Paper scheduling](paper-scheduling.md).

- `interval_seconds` must be one of 60, 300, 900, 3600, 14400 or 86400. `max_evaluations` is 1
  to 10000. `confirm=true` is required because evaluations can incur API or x402 cost.
- A schedule is unique per upstream identity and strategy. The identity is the configured API
  key and its destination, or the payer wallet.
- The dispatcher runs at most four evaluations at once. A slot is `unix_time // interval`. A
  slot is recorded as pending before MangroveAI is called and is never evaluated twice.
- States: `starting`, `active`, `pausing`, `paused`, `exhausted` (all evaluations used),
  `attention` (an evaluation outcome is unknown), `start_rejected` and `start_uncertain`.
  `attention` stops further calls until `agent_reconcile_paper_schedule` retries the same slot
  with its original payment identity.
- Pausing stops new local requests first, then asks MangroveAI to pause. It does not close
  positions.
- Start errors map to `PAPER_START_REJECTED` (409, MangroveAI refused) and
  `PAPER_START_UNCONFIRMED` (502, outcome unknown).

### Portfolio kill switch

MangroveAI enforces risk per strategy. Only the agent sees the whole live book, so the agent
enforces aggregate drawdown.

- **Book value** is the sum of active live allocations, the capital of live Robinhood
  mandates, and the realized profit and loss of trades of strategies in `live` status.
- **High-water mark** is the highest book value seen. It advances on each live tick and is
  re-baselined whenever a strategy enters or leaves `live`, so changing the live set is not
  read as drawdown.
- **Trip.** When drawdown from the high-water mark reaches `PORTFOLIO_MAX_DRAWDOWN_PCT`
  (default 0.30), the switch trips: every live strategy is moved to `inactive` (which cancels
  its job and releases its allocation) and the switch latches.
- **Latched.** While tripped, no live tick places an order. The switch never resets itself. A
  human clears it with `POST /api/v1/agent/portfolio/risk/reset`, which re-baselines to the
  current book. There is no MCP tool for the reset.
- The check runs at the start of each live tick, after the evaluation is logged, so a trip
  caused by losses on other strategies still stops this tick's orders.

The state appears in `GET /status` under `portfolio_risk` and at `GET /portfolio/risk`.

### x402 spend budget

The budget governs money going out: what the agent pays for priced upstream calls in wallet
mode. It is separate from trading capital. Calls authenticated with a Mangrove API key are not
payments and are not counted.

- **Cap.** The budget for the current period is, in order: the amount a human authorized at
  the last top-up, then `X402_SPEND_CAP_USD`, then 25 dollars. A cap of 0 blocks all payments.
- **Reservation before signing.** Each payment is recorded in the ledger inside an atomic
  transaction before the signature exists. Amounts are integer micro-USD, the same unit as a
  USDC EIP-3009 `value`.
- **Counting.** A payment counts against the budget from the moment it is authorized, and
  stays counted until it is settled or provably released. Confirmed refunds are credited back
  to the period of the original charge. Over-counting is preferred to under-counting.
- **Exhaustion.** When the budget is consumed it latches as `exhausted` and later payments are
  refused with `X402_SPEND_CAP_EXCEEDED` (403) before anything is signed. A single payment
  that does not fit in what is left is refused without latching.
- **Top-up.** Only a human renews the budget: `POST /x402/spend/reset` or the
  `x402_spend_reset` tool with `confirm=true`. It starts a new period with the spent total at
  zero, optionally with a new `cap_usd`. History is kept under the old period.
- **Uncertainty.** A payment whose outcome is unknown keeps its amount reserved and is never
  retried automatically; independent requests can proceed. `unreconciled_count` reports old
  authorizations still unresolved. See [x402 payment uncertainty](x402-payment-uncertainty.md).

The state appears in `GET /status` under `x402_spend` and at `GET /x402/spend`.

## Data models

These Pydantic models are defined in `server/src/models/domain.py` and
`server/src/services/strategy_service.py`.

```python
class OrderIntent(BaseModel):
    action: Literal["enter", "exit"]
    side: Literal["buy", "sell"]
    symbol: str
    amount: float                      # asset units for strategy intents; input-token amount for swaps
    reason: str = ""
    ref_price: float | None = None     # evaluation-time price
    engine_position_id: str | None = None
    engine_order_id: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    input_token_address: str | None = None    # both or neither
    output_token_address: str | None = None

class Evaluation(BaseModel):           # one scheduler tick
    id: str
    strategy_id: str
    timestamp: datetime
    market_snapshot: dict = {}         # price, timestamp, asset the evaluation saw
    sdk_response: dict = {}            # the evaluation response, verbatim
    order_intents: list[OrderIntent] = []
    duration_ms: int
    status: Literal["ok", "error", "skipped"]
    error_msg: str | None = None

class Trade(BaseModel):                # a fill: live, paper or validate
    id: str
    strategy_id: str | None = None
    evaluation_id: str | None = None   # null for user-initiated swaps
    order_intent: OrderIntent | None = None
    mode: Literal["live", "paper", "validate"]
    tx_hash: str | None = None
    input_token: str | None = None
    input_amount: float | None = None
    output_token: str | None = None
    output_amount: float | None = None
    fill_price: float
    fees: dict = {}
    status: Literal["pending", "confirmed", "failed", "simulated"]
    executed_at: datetime
    confirmed_at: datetime | None = None
    p_and_l: float | None = None       # set on the exit trade
    venue: str | None = None           # kraken, 1inch, robinhood, ...
    venue_order_ref: str | None = None
    venue_trade_ref: str | None = None
    base: str | None = None            # CEX amounts
    quote: str | None = None
    side: Literal["buy", "sell"] | None = None
    qty: float | None = None

class Position(BaseModel):
    id: str                            # the engine's position ID when known
    strategy_id: str
    asset: str
    entry_trade_id: str
    exit_trade_id: str | None = None
    entry_price: float
    entry_amount: float
    entry_time: datetime
    exit_price: float | None = None
    exit_amount: float | None = None
    exit_time: datetime | None = None
    status: Literal["open", "closed"]
    stop_loss: float | None = None
    take_profit: float | None = None

class Allocation(BaseModel):           # live strategies only
    id: str
    strategy_id: str
    wallet_address: str
    token_address: str
    token_symbol: str
    amount: float
    active: bool
    created_at: datetime
    released_at: datetime | None = None
    slippage_pct: float | None = None  # decimal, at most 0.0025

class StrategyStatusUpdate(BaseModel):
    status: Literal["draft", "inactive", "paper", "live", "archived"]
    confirm: bool = False
    allocation: StrategyAllocationInput | None = None

class StrategyDetailResponse(BaseModel):
    id: str                            # the agent's local UUID
    mangrove_id: str                   # the MangroveAI strategy ID
    name: str
    asset: str
    timeframe: str
    status: str
    entry: list[dict]
    exit: list[dict]
    execution_config: dict
    market_context: StrategyMarketContext | None = None
    execution_venue: ExecutionVenueResponse | None = None
    generation_report: dict | None = None
    created_at: datetime
    updated_at: datetime
```

## SQLite schema

All state is in one SQLite file at `DB_PATH`, in WAL mode with foreign keys enforced. Migrations
in `server/src/shared/db/migrations/` apply in filename order at startup and are recorded in a
`_migrations` table. The scheduler adds its own job table (`apscheduler_jobs`).

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `wallets` | Encrypted wallets | `address` (unique), `chain` (`evm` or `xrpl`), `network`, `chain_id`, `encrypted_secret` (Fernet), `encryption_method`, `label`, `backup_confirmed_at`, `created_at` |
| `strategies` | Local execution records | `id`, `mangrove_id` (unique), `name`, `asset`, `timeframe`, `status`, `entry_json`, `exit_json`, `execution_config_json`, `generation_report_json`, `execution_state_json`, `evaluation_lane`, `open_positions_json`, `market_context_json` |
| `allocations` | Per-strategy fund commitments | `strategy_id`, `wallet_address`, `token_address`, `token_symbol`, `amount`, `slippage_pct`, `active`, `released_at` |
| `evaluations` | One row per tick | `strategy_id`, `timestamp`, `market_snapshot_json`, `sdk_response_json`, `order_intents_json`, `duration_ms`, `status`, `error_msg` |
| `trades` | One row per fill | `strategy_id`, `evaluation_id`, `order_intent_json`, `mode`, `tx_hash`, token and amount columns, `fill_price`, `fees_json`, `status`, `executed_at`, `confirmed_at`, `p_and_l`, `venue`, `venue_order_ref`, `venue_trade_ref` |
| `positions` | Open and closed positions | `strategy_id`, `asset`, `entry_trade_id`, `exit_trade_id`, prices, amounts, times, `status`, `stop_loss`, `take_profit` |
| `portfolio_risk` | Single row: kill-switch state | `high_water_mark`, `tripped`, `tripped_at`, `tripped_reason` |
| `x402_spend_state` | Single row: current budget period | `period_id`, `period_started_at`, `period_cap_micro_usd`, `exhausted`, `exhausted_at`, `exhausted_reason` |
| `x402_payments` | One row per payment authorization | `period_id`, `state` (`authorized`, `settled`, `released`), `amount_micro_usd`, `wallet_address`, `payee`, `network`, `resource`, `transaction_hash`, `valid_before`, `valid_after`, `authorization_nonce`, `asset`, `release_reason`, `operation_id` |
| `x402_operations` | Durable identity of a paid operation | `fingerprint`, `state` (`pending`, `complete`, `unsigned_failed`), encrypted `payment_headers` and `response` |
| `x402_recovery_state` | Recovery bookkeeping per operation | `operation_id`, `action_id`, `token`, `replacement_allowed`, `next_id` |
| `x402_reconciliation_evidence` | Evidence behind a reconciliation outcome | `reservation_id`, `outcome`, `evidence_json` |
| `x402_reconciliation_jobs` | Background reconciliation schedule | `reservation_id`, `attempts`, `next_check`, `outcome` |
| `x402_refunds` | Verified credits against original charges | `reservation_id`, `state` (`pending`, `review`, `confirmed`), `refund_tx`, `network`, `amount_micro_usd` |
| `marketplace_approvals` | Prepared marketplace actions | `payload`, `expires_at`, `state` (`prepared`, `submitted`, `completed`), `result` |
| `broker_routes` | Strategy-to-Robinhood bindings | `strategy_id`, `venue`, account numbers, `asset_class`, `capital`, `max_order_usd`, `slippage_pct` |
| `broker_orders` | Durable broker order intents | `strategy_id`, `evaluation_id`, `intent_key`, `status`, `broker_order_id`, `filled_quantity`, `filled_notional`, `fees` |
| `broker_events` | Order status history | `order_id`, `status`, `filled_quantity` |
| `broker_holdings` | Exact broker quantity and cost per position | `position_id`, `quantity`, `unit_cost` |
| `paper_schedules` | Server-owned paper schedules | `scope`, `strategy_id`, `controller_id`, `run_id`, `interval_seconds`, `max_evaluations`, `evaluations`, `state`, `pending_slot`, `last_slot`, `last_result`, `error` |
| `agent_identity` | Single row: the agent name | `name` (1 to 64 characters) |

Migration `002` seeds a placeholder `strategies` row with ID `user-initiated`, in `archived`
status, so trades from direct swaps satisfy the foreign key. Status counts exclude it.

| Migration | Adds |
|-----------|------|
| `001_initial` | `wallets`, `strategies`, `allocations`, `evaluations`, `trades`, `positions` |
| `002_user_initiated_placeholder` | The `user-initiated` placeholder strategy |
| `003_wallet_backup_confirmed` | `wallets.backup_confirmed_at` |
| `004_allocation_slippage` | `allocations.slippage_pct` |
| `005_strategy_execution_state` | `strategies.execution_state_json` |
| `006_evaluation_lane` | `strategies.evaluation_lane`, `open_positions_json` |
| `007_portfolio_risk` | `portfolio_risk` |
| `008_x402_spend` | `x402_spend_state`, `x402_payments` |
| `009_x402_uncertain_authorizations` | Restores authorizations that had been released without proof |
| `010_x402_authorization_metadata` | Public authorization fields on `x402_payments` |
| `011_x402_payment_pause` | Pending-payment index, `x402_reconciliation_evidence` |
| `012_x402_operations` | `x402_operations`, `x402_reconciliation_jobs`, `x402_payments.operation_id` |
| `013_x402_refunds` | `x402_refunds` |
| `014_marketplace_approvals` | `marketplace_approvals` |
| `015_independent_payment_operations` | Replaces the pending-operation index so unrelated operations do not block each other |
| `016_broker_execution` | `broker_routes`, `broker_orders`, `broker_events`, `broker_holdings`, venue columns on `trades` |
| `017_strategy_market_context` | `strategies.market_context_json` |
| `018_agent_identity` | `agent_identity` |
| `018_paper_schedules` | `paper_schedules` |
| `019_paper_payment_attempt` | `paper_schedules.payment_request_id` |
| `020_x402_shared_recovery` | `x402_recovery_state` |

Kraken credentials and the Robinhood OAuth grant are not in the database. They are
Fernet-encrypted files in the data directory next to it (`cex-kraken.enc` and
`robinhood-oauth.enc`).

## Error handling

Every domain error has this body, over REST with the error's HTTP status, and as JSON text from
an MCP tool:

```json
{
  "error": true,
  "code": "ERROR_CODE",
  "message": "Human-readable description",
  "suggestion": "What to do about it",
  "correlation_id": "uuid"
}
```

| Code | HTTP | When |
|------|------|------|
| `AUTH_MISSING_API_KEY` | 401 | No `X-API-Key` header |
| `AUTH_INVALID_API_KEY` | 401 | The key is not in `API_KEYS` |
| `VALIDATION_ERROR` | 400 | Input failed validation |
| `CONFIRMATION_REQUIRED` | 400 | A swap, live transition or top-up without `confirm: true` |
| `WALLET_NOT_FOUND` | 404 | Address not in the local wallet store |
| `WALLET_ALREADY_EXISTS` | 409 | A wallet with that address is already stored |
| `STRATEGY_NOT_FOUND` | 404 | No local record with that ID |
| `STRATEGY_INVALID_STATUS_TRANSITION` | 400 | Illegal lifecycle move, or an unsupported combination such as a Robinhood-bound strategy going to `paper` |
| `ALLOCATION_INSUFFICIENT` | 400 | Allocation amount is not greater than 0 |
| `CEX_CREDENTIALS_MISSING` | 409 | A Kraken tool was used before Kraken was connected |
| `SDK_ERROR` | 502 | An upstream call failed |
| `UPSTREAM_AUTHENTICATION_FAILED` | 401 | An upstream service rejected the configured credential |
| `UPSTREAM_ACCESS_DENIED` | 403 | An upstream service denied the operation |
| `UPSTREAM_RATE_LIMITED` | 429 | MangroveAI's rate limit was reached; no retry or payment fallback is attempted |
| `SIGNING_ERROR` | 500 | Local signing failed or the signing guard refused the payload |
| `X402_PAYMENT_ERROR` | 502 | An outbound payment could not be completed |
| `X402_PAYMENT_UNCERTAIN` | 409 | A payment's outcome is unresolved; its amount stays reserved and it is not retried automatically |
| `X402_SPEND_CAP_EXCEEDED` | 403 | A payment was refused for budget reasons; nothing was signed |
| `PAPER_START_REJECTED` | 409 | MangroveAI refused to start paper execution |
| `PAPER_START_UNCONFIRMED` | 502 | The paper start outcome is unknown |
| `MARKETPLACE_UNAVAILABLE` | 400 | Markets discovery or ownership configuration is unavailable or incompatible |
| `PURCHASE_BALANCE_UNAVAILABLE` | 503 | The funding check for an approved purchase could not be read |
| `PURCHASE_INSUFFICIENT_FUNDS` | 409 | The wallet cannot cover an approved purchase |
| `SCHEDULER_ERROR` | 500 | Unknown timeframe when registering a job |
| `CHAIN_NOT_SUPPORTED_IN_V1` | 501 | A wallet chain other than `evm` or `xrpl` |
| `INTERNAL_ERROR` | 500 | Unexpected failure; see the logs |

Tools that call MangroveAI return errors in MangroveAI's own format, plus the codes above for
failures in the agent's layer (`AUTH_REQUIRED`, `UNKNOWN_TOOL`, `VALIDATION_ERROR` for
arguments that do not match the current input schema, `UPSTREAM_ERROR`). `RETIRED_TOOL` is
returned for a retired agent wrapper whose function Markets serves directly.

The `correlation_id` matches the `X-Correlation-Id` response header and the agent's structured
logs.

## External integrations

### MangroveAI

**Purpose:** strategies, backtests, signals, market and on-chain data, knowledge base,
strategy evaluation.

- **MCP.** Tools are discovered from MangroveAI's `/mcp/` endpoint and forwarded unchanged. The
  destination is the MangroveAI API origin with `/api/v1` replaced by `/mcp/`.
- **SDK.** The `mangroveai` SDK is used for evaluation (`execution.evaluate` and
  `evaluate_by_object`), paper execution control, strategy status sync and mark prices.
- **Authentication modes.** With a Mangrove API key (`MANGROVE_API_KEY`) the key authenticates
  every call. Without one, the agent pays per call with x402 from the payer wallet
  (`X402_PAYER_WALLET`). `MANGROVE_ACCESS_MODE` set to `api-key` or `x402` forces a mode;
  unset, a configured key selects API-key mode. A mode never changes because a call failed.
- **Destination.** `MANGROVEAI_BASE_URL` overrides the API origin for API-key mode and must be
  HTTPS (or loopback HTTP) and end in `/api/v1`. In wallet mode the destination comes from
  the bundled `mangrove-endpoints.json`, selected by `X402_MANGROVE_ENVIRONMENT` or the agent's
  environment, with `X402_MANGROVE_BASE_URL` and `X402_MANGROVE_KB_BASE_URL` as overrides.
- **Timeout.** `MANGROVE_SDK_TIMEOUT_SECONDS` (180 in the example config) bounds SDK calls.

### MangroveMarkets

**Purpose:** DEX quotes and routing through 1inch, token and gas data, portfolio reads and
marketplace actions.

- `MANGROVEMARKETS_BASE_URL` points at the Markets server, used by both the SDK and the MCP
  client. The example config uses `http://localhost:9081`.
- Signing is local. The SDK returns unsigned transactions; the agent signs them with the
  wallet's decrypted key and broadcasts the signed bytes. The key is never passed to the SDK.
- Markets tools that move assets or pay money return an approval preview and need
  `marketplace_submit`.

### Robinhood

The agent connects to Robinhood's MCP endpoint with an OAuth grant it owns, created by
`scripts/connect-robinhood.sh`. The grant is Fernet-encrypted under the master key. The agent
never reads another client's credentials, never opens a login from a background worker and
never retries an order submission automatically. See
[Robinhood execution](robinhood-execution.md).

### Kraken

Two modes: bring-your-own-key, where an encrypted key stays on the machine and the agent calls
Kraken directly; and keyless OAuth, where the Mangrove platform holds the grant. Fills from the
bring-your-own-key mode can be sent to telemetry with `cex_sync_fills`; the key itself is never
sent.

### Local key encryption

Wallet secrets are encrypted with Fernet (`cryptography`) before they are written to SQLite. The
master key is resolved in this order:

1. The keyfile at `MASTER_KEY_PATH` (owner-only permissions, 44 bytes).
2. The OS keychain through `keyring`, under the service name `KEYRING_SERVICE_NAME`.
3. If neither exists, a new key is generated into the keyfile. A missing key that the database
   has ciphertext for is a configuration error to fix, not a reason to generate a new key.

Decryption happens only inside the wallet manager's signing functions, and only after the
payload passes the signing guard.

### Signing guard

The wallet manager signs exactly these things, and refuses everything else before it touches
the key:

- A transaction to a known 1inch aggregation router (EIP-155 or EIP-1559 only).
- An ERC-20 `approve()` whose spender is a 1inch router.
- An x402 payment authorization: EIP-712 `TransferWithAuthorization` for USDC on a pinned
  chain and contract, with the struct fields checked field for field.
- Marketplace ownership proofs and Sign-In-With-X messages, each with its own validation.

EIP-7702 and blob transactions, authorization lists, contract deployments, other targets and
arbitrary message signing are refused.

## Configuration

Configuration is JSON in `server/src/config/{ENVIRONMENT}-config.json`. `configuration-keys.json`
declares which keys are required. A value can be a literal or a `secret:NAME:PROPERTY`
reference resolved from GCP Secret Manager. With `MANGROVE_AGENT_HOME` set (plugin installs),
config, database and master key live under that directory and relative paths resolve against
it. See the [configuration guide](configuration.md).

**Required keys:**

| Key | Meaning | Example-config value |
|-----|---------|----------------------|
| `AUTH_ENABLED` | Turn API key checking on or off | `true` |
| `API_KEYS` | Comma-separated valid keys | generated by setup |
| `MANGROVEMARKETS_BASE_URL` | Markets server | `http://localhost:9081` |
| `DB_PATH` | SQLite file | `./agent-data/agent.db` |
| `MASTER_KEY_PATH` | Master keyfile | `./agent-data/master.key` |
| `KEYRING_SERVICE_NAME` | OS keychain service name | `mangrove-agent` |
| `MANGROVE_SDK_TIMEOUT_SECONDS` | SDK and upstream call timeout | `180` |
| `LOG_RETENTION_DAYS` | Log retention period | `90` |
| `SECRET_VAULT_TTL_SECONDS` | Lifetime of a `vault_token` | `300` |
| `X402_FACILITATOR_URL` | Facilitator for the demo route and tool | Coinbase CDP facilitator |
| `X402_NETWORK` | Payment network, CAIP-2 | `eip155:8453` |
| `X402_PAY_TO` | Address that receives demo payments | set |
| `X402_USDC_CONTRACT` | USDC contract for the network | Base mainnet USDC |
| `X402_HELLO_MANGROVE_PRICE` | Demo price in USDC base units | `50000` |
| `X402_CDP_API_KEY_ID`, `X402_CDP_API_KEY_SECRET` | Facilitator credentials (empty if unused) | empty |

**Optional keys** are loaded when present: `MANGROVE_API_KEY`, `MANGROVE_ACCESS_MODE`,
`MANGROVEAI_BASE_URL`, `X402_MANGROVE_ENVIRONMENT`, `X402_MANGROVE_BASE_URL`,
`X402_MANGROVE_KB_BASE_URL`, `X402_RECONCILIATION_RPC_URLS` (a map of network ID to RPC URL;
there is no public fallback, and `scripts/setup.sh` seeds the selected network when wallet
mode is chosen), `MARKETPLACE_OWNERSHIP_AUDIENCE`, `MARKETPLACE_CHAIN_ID` (8453 or
84532) and `MARKETPLACE_XRPL_NETWORK`.

**Full-app keys** are validated only when present, and startup fails if one is present but
empty: `PORTFOLIO_MAX_DRAWDOWN_PCT` (default 0.30) and `X402_SPEND_CAP_USD` (default 25).

**Read with defaults:** `LOCAL_AGENT_URL` (default `http://localhost:9080`) is the agent's own
local address, used by scripts and never a remote host. `X402_PAYER_WALLET` (default empty) is
the wallet outbound payments are signed with; no wallet is chosen on the user's behalf.

## Service layer

Routes and tools delegate to these services in `server/src/services/`. Business logic is never
duplicated between the two interfaces.

| Module | Responsibility |
|--------|----------------|
| `wallet_manager` | Key generation, Fernet encryption, the signing guard, local signing, backup gate |
| `secret_vault` | In-memory, single-read, expiring store behind `vault_token` |
| `strategy_service` | Lifecycle transitions and the scheduler tick for local records |
| `order_executor` | The single execution path for swaps and paper fills; maintains positions |
| `scheduler_service` | APScheduler wrapper and fixed-interval jobs |
| `paper_schedule_service` | Server-owned paper schedules |
| `trade_log` | Writes and reads of evaluations, trades and positions |
| `allocation_service` | Per-strategy fund allocations |
| `portfolio_risk_service` | Aggregate drawdown and the kill switch |
| `spend_service` | The outbound x402 budget and ledger |
| `x402_payer`, `payment_operations`, `payment_reconciliation_worker`, `refund_service` | Outbound payments, operation identity, reconciliation and refunds |
| `dex_service` | Quotes in human units |
| `broker_execution`, `broker_store`, `robinhood_client`, `robinhood_venue` | Robinhood execution venue |
| `cex_service`, `cex_oauth_service`, `cex_credentials` | Kraken in both modes |
| `marketplace`, `marketplace_catalog`, `marketplace_reads`, `marketplace_payments`, `marketplace_authorization` | Local approval flow for Markets actions |
| `agent_identity` | The agent name |
| `tool_pricing` | Price hints for `list_tools` |

Some read-only routes (DEX venues, pairs, gas price and similar, and the wallet balance and
portfolio reads) call the `mangrovemarkets` SDK client directly from the route handler. A
service exists only where it adds orchestration, state or safety checks.

There is no local signal evaluator. Signal firing, position sizing, risk gates, cooldowns and
volatility adjustment are the responsibility of MangroveAI's evaluation, and the agent
executes whatever order intents it returns.
