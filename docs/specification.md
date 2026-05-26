# Technical Specification: mangrove-agent

**Generated:** 2026-04-17
**Status:** Draft
**Based on:** `docs/user-stories.md` (approved requirements)

## Overview

mangrove-agent is a FastAPI + MCP service that wraps the `mangroveai` and `mangrovemarkets` Python SDKs with local state, autonomous strategy generation, cron-based execution, and a full audit trail.

It exposes the same functionality two ways:
- **MCP tools at `/mcp`** (Streamable HTTP transport) — preferred for AI agents because of structured tool discovery and typed invocation.
- **REST endpoints at `/api/v1/agent/*`** — universal; any HTTP client (Python scripts, cron jobs, curl, notebooks, tests) can use them without an MCP library.

Both protocols share a single service layer — `create_wallet` (MCP tool) and `POST /api/v1/agent/wallet/create` (REST endpoint) call the same Python function. No duplicated business logic.

The agent runs on the user's own machine via Docker Compose (or `uvicorn` directly) — single-user, local-only for v1. It holds wallet keys locally (encrypted), registers APScheduler cron jobs at strategy activation, and logs every evaluation and trade to local SQLite.

Cloud deployment (Cloud Run, persistent cloud storage) is **out of scope for v1** and will be addressed in a subsequent release.

## Access Tiers

| Tier | Endpoints | How to access |
|------|-----------|---------------|
| Free | `/health`, `/api/v1/agent/tools`, `/api/v1/agent/status` | No credentials |
| Auth | Everything else (default tier for v1 agent endpoints) | `X-API-Key: <configured-key>` header |
| x402 | Reserved — currently the `hello_mangrove.py` demo route only (renamed from the template's `easter_egg.py`); no v1 agent endpoints land here yet | x402 payment OR `X-API-Key` bypass |

Single-user model: the API key (config key `API_KEY`) is a shared secret between the user's Claude Code config and the agent. No RBAC, no user accounts.

x402 stays wired up — middleware, routes, config keys, the `hello_mangrove` demo endpoint — so future agent endpoints can be moved to the payment tier without scaffolding work. The choice of which agent endpoints to monetize is deferred.

---

## API Contracts

All endpoints are JSON over HTTPS. Base path: `/api/v1/agent`. Every endpoint has a mirrored MCP tool with identical semantics (see [MCP Tools](#mcp-tools)).

### Discovery (free)

#### `GET /health`
Returns `{ status, service, version, timestamp }`.

#### `GET /api/v1/agent/tools`
Returns the full MCP tool catalog (tool names, descriptions, parameters, access tier).

#### `GET /api/v1/agent/status`
Returns service state:
```json
{
  "version": "0.1.0",
  "wallets_count": 2,
  "strategies": {"draft": 3, "inactive": 1, "paper": 2, "live": 1, "archived": 5},
  "active_cron_jobs": 3,
  "db_path": "./agent.db",
  "uptime_seconds": 12345
}
```

---

### Wallet (auth)

#### `POST /api/v1/agent/wallet/create`
Create + encrypt + store a wallet locally.

**v1 scope:** EVM chains only for live execution. XRPL accepted but returns a clear "not supported in v1" error; Solana not supported at all.

**Request:**
```json
{
  "chain": "string — evm | xrpl (stubbed, returns 501)",
  "network": "string — mainnet | testnet",
  "chain_id": "int | null — required for evm",
  "label": "string | null — human-friendly name"
}
```

**Response (201):**
```json
{
  "address": "string — public address",
  "chain": "string",
  "network": "string",
  "chain_id": "int | null",
  "label": "string | null",
  "created_at": "string — ISO 8601",
  "warning": "string — reminder that keys are stored locally encrypted; user should back up their seed phrase shown in the chat"
}
```

**Security warning returned in `warning` field and also logged to stdout:**

> The seed phrase is shown **once** in the response, then encrypted to disk. Never retrievable via API after creation.
>
> ⚠️ **Important:** the seed phrase will appear in your chat transcript, which Claude Code writes to disk under `~/.claude/projects/.../*.jsonl`. If you do not want the seed phrase persisted there, (1) copy it to a secure location, (2) delete the corresponding session transcript file, and (3) back it up offline (paper, hardware wallet, password manager). Never screenshot without securing the image.

**Errors:** 400 `VALIDATION_ERROR`, 409 `WALLET_ALREADY_EXISTS`, 501 `CHAIN_NOT_SUPPORTED_IN_V1` (for XRPL), 502 `SDK_ERROR`.

#### `GET /api/v1/agent/wallet/list`
List all stored wallets (addresses + metadata only, no keys).

#### `GET /api/v1/agent/wallet/{address}/balances?chain_id=<int>`
Returns token balances via `mangrovemarkets.dex.balances()`.

#### `GET /api/v1/agent/wallet/{address}/portfolio?chain_id=<int>`
Returns aggregate portfolio: value, P&L, tokens, DeFi positions (via `mangrovemarkets.portfolio.*`).

#### `GET /api/v1/agent/wallet/{address}/history?limit=<int>`
Returns transaction history via `mangrovemarkets.portfolio.history()`.

---

### DEX (auth)

#### `GET /api/v1/agent/dex/venues`
List supported DEX venues via `mangrovemarkets.dex.supported_venues()`.

#### `GET /api/v1/agent/dex/pairs?venue_id=<str>`
List trading pairs for a venue.

#### `POST /api/v1/agent/dex/quote`
**Request:**
```json
{
  "input_token": "string — token address or symbol",
  "output_token": "string",
  "amount": "number — in base units of input_token",
  "chain_id": "int",
  "venue_id": "string | null — null = best across venues"
}
```
**Response (200):** Mirrors `mangrovemarkets.dex.Quote` model.

#### `POST /api/v1/agent/dex/swap`
Execute the full 6-step swap flow. **Requires `confirm: true`** in the request body — protects against agent-initiated swaps without user approval.

**Request:**
```json
{
  "input_token": "string",
  "output_token": "string",
  "amount": "number",
  "chain_id": "int",
  "wallet_address": "string — must be in the agent's wallet store",
  "slippage": "number — default 1.0 (percent)",
  "mev_protection": "boolean — default false",
  "confirm": "boolean — must be true"
}
```

**Response (200):**
```json
{
  "tx_hash": "string",
  "status": "string — confirmed | pending",
  "input_amount": "number",
  "output_amount": "number",
  "fill_price": "number",
  "fees": "object",
  "approval_tx_hash": "string | null",
  "trade_log_id": "string — UUID in the agent's local trades table"
}
```

Internal flow: quote → `approve_token` (returns None if already approved) → sign approval if returned → broadcast → wait → `prepare_swap` → sign locally → `broadcast` → poll `tx_status` until confirmed → log to SQLite.

**Errors:** 400 `VALIDATION_ERROR`, 400 `CONFIRMATION_REQUIRED`, 404 `WALLET_NOT_FOUND`, 502 `SDK_ERROR`, 500 `SIGNING_ERROR`.

---

### Market Data (auth)

#### `GET /api/v1/agent/market/ohlcv?symbol=<str>&timeframe=<str>&lookback_days=<int>`
Returns OHLCV via `mangroveai.crypto_assets.get_ohlcv()`.

#### `GET /api/v1/agent/market/data?symbol=<str>`
Current market data (price, market cap, volume, 24h/7d change).

#### `GET /api/v1/agent/market/trending`
Trending assets.

#### `GET /api/v1/agent/market/global`
Global market cap, BTC dominance, 24h change.

#### `GET /api/v1/agent/on-chain/smart-money?symbol=<str>&chain=<str>`
Smart money sentiment via `mangroveai.on_chain.get_smart_money_sentiment()`.

#### `GET /api/v1/agent/on-chain/whale-activity?symbol=<str>&hours_back=<int>`
Whale activity summary.

#### `GET /api/v1/agent/on-chain/token-holders/{symbol}`
Holder distribution and concentration.

---

### Signals (auth)

#### `GET /api/v1/agent/signals?category=<str>&search=<str>&limit=<int>`
List/search signals via `mangroveai.signals.list()` with optional filtering.

#### `GET /api/v1/agent/signals/{name}`
Signal detail with parameter spec.

---

### Strategies (auth)

#### `POST /api/v1/agent/strategies/autonomous`
Autonomous strategy creation: skill picks candidates → quick backtest → filter → rank by IRR → full backtest → persist.

**Request:**
```json
{
  "goal": "string — natural-language goal, e.g. 'Trade ETH on momentum breakouts with tight stops'",
  "asset": "string",
  "timeframe": "string — 1m | 5m | 15m | 1h | 4h | 1d",
  "candidate_count": "int — default 7, range [5, 10]",
  "backtest_lookback_months": "int — default 3"
}
```

**Response (201):**
```json
{
  "strategy": { ... full StrategyDetail ... },
  "generation_report": {
    "candidates_tried": 7,
    "candidates_passed_filter": 3,
    "winner_rank": 1,
    "full_backtest_metrics": {
      "irr_annualized": 0.42,
      "sharpe_ratio": 1.8,
      "max_drawdown": 0.12,
      "win_rate": 0.58,
      "total_trades": 47
    },
    "rejected_reasons": [
      {"candidate": "...", "reason": "win_rate 0.48 < 0.51"},
      ...
    ]
  }
}
```

**Errors:** 400 `VALIDATION_ERROR`, 422 `STRATEGY_NO_VIABLE_CANDIDATES`, 502 `SDK_ERROR`.

#### `POST /api/v1/agent/strategies/manual`
Manual strategy creation with explicit entry/exit rules.

**Request:**
```json
{
  "name": "string",
  "asset": "string",
  "timeframe": "string",
  "entry": [
    {
      "name": "string — signal name",
      "signal_type": "string — TRIGGER | FILTER",
      "params": "object"
    }
  ],
  "exit": [ ... ],
  "execution_config": "object | null — null = Mangrove defaults"
}
```

**Response (201):** Full `StrategyDetail`.

Validation: entry must be exactly 1 TRIGGER + 0+ FILTERs; exit must be 0–1 TRIGGERs + 0+ FILTERs.

#### `GET /api/v1/agent/strategies?status=<str>&limit=<int>&offset=<int>`
List strategies with optional status filter.

#### `GET /api/v1/agent/strategies/{id}`
Full strategy details.

#### `PATCH /api/v1/agent/strategies/{id}/status`
**Single source of truth for strategy lifecycle.** This is the only way to activate, deactivate, or archive a strategy. Side effects (register/cancel cron jobs, allocate/release funds) are driven by the status transition.

**Request:**
```json
{
  "status": "string — draft | inactive | paper | live | archived",
  "confirm": "boolean — required when activating to live or deactivating a live strategy",
  "allocation": {
    "wallet_address": "string — required when transitioning to live",
    "token": "string — token address or symbol",
    "amount": "number"
  }
}
```

Valid transitions: `draft → inactive`, `inactive → paper`, `inactive → live`, `paper → live`, `paper → inactive`, `live → inactive`, `* → archived`.

Side effects by target status:
- `paper`: register APScheduler cron job keyed to strategy timeframe. Allocation field ignored.
- `live`: require `confirm: true` AND `allocation` block. Record allocation in local DB. Register cron job.
- `inactive` (from live): require `confirm: true`. Cancel cron job. Release allocation (mark `active=false`, set `released_at`).
- `inactive` (from paper): cancel cron job. No allocation change.
- `archived`: cancel any running cron job. Release allocation if active.

**Errors:** 400 `STRATEGY_INVALID_STATUS_TRANSITION`, 400 `CONFIRMATION_REQUIRED`, 400 `ALLOCATION_INSUFFICIENT`, 404 `WALLET_NOT_FOUND`.

#### `POST /api/v1/agent/strategies/{id}/backtest`
**Request:**
```json
{
  "mode": "string — quick | full",
  "lookback_months": "int — default 3",
  "start_date": "string | null — ISO 8601",
  "end_date": "string | null"
}
```
**Response:** Full backtest metrics + trade history.

#### `POST /api/v1/agent/strategies/{id}/evaluate`
Manually trigger a single evaluation tick (for debugging/power users). Same code path the cron job runs.

---

### Execution Logs (auth)

#### `GET /api/v1/agent/strategies/{id}/evaluations?limit=<int>&offset=<int>`
Returns evaluation log for a strategy, newest first.

#### `GET /api/v1/agent/strategies/{id}/trades?limit=<int>&offset=<int>`
Returns trades for a strategy, newest first.

#### `GET /api/v1/agent/trades?limit=<int>&strategy_id=<str>&mode=<str>`
All trades across strategies, with optional filters.

---

### Knowledge Base (auth)

#### `GET /api/v1/agent/kb/search?q=<str>&limit=<int>`
Full-text search via `mangroveai.kb.search.*`.

#### `GET /api/v1/agent/kb/glossary/{term}`
Glossary term lookup with backlinks.

---

## MCP Tools

Every REST endpoint has a mirrored MCP tool with identical semantics. Tool names use plain `verb_resource` form (e.g. `create_strategy_autonomous`, `list_trades`, `get_market_data`) — no project prefix; the MCP server namespace is enough.

Tool descriptions include parameters, return shapes, and access tier. All tools enforce the same auth as their REST counterparts.

### v1 scope — core vs nice-to-have

**Core (must work for a demoable trading bot, ~22 tools):**

| Category | Tool | REST endpoint |
|----------|------|---------------|
| Discovery | `status` | `GET /status` |
| Discovery | `list_tools` | `GET /tools` |
| Wallet | `create_wallet` | `POST /wallet/create` |
| Wallet | `list_wallets` | `GET /wallet/list` |
| Wallet | `get_balances` | `GET /wallet/{a}/balances` |
| DEX | `list_dex_venues` | `GET /dex/venues` |
| DEX | `get_swap_quote` | `POST /dex/quote` |
| DEX | `execute_swap` | `POST /dex/swap` |
| Market | `get_ohlcv` | `GET /market/ohlcv` |
| Market | `get_market_data` | `GET /market/data` |
| Signals | `list_signals` | `GET /signals` |
| Strategy | `create_strategy_autonomous` | `POST /strategies/autonomous` |
| Strategy | `create_strategy_manual` | `POST /strategies/manual` |
| Strategy | `list_strategies` | `GET /strategies` |
| Strategy | `get_strategy` | `GET /strategies/{id}` |
| Strategy | `update_strategy_status` | `PATCH /strategies/{id}/status` |
| Strategy | `backtest_strategy` | `POST /strategies/{id}/backtest` |
| Strategy | `evaluate_strategy` | `POST /strategies/{id}/evaluate` |
| Logs | `list_evaluations` | `GET /strategies/{id}/evaluations` |
| Logs | `list_trades` | `GET /strategies/{id}/trades` |
| Logs | `list_all_trades` | `GET /trades` |
| KB | `kb_search` | `GET /kb/search` |

**Nice-to-have (extend after core ships):**

| Category | Tool | REST endpoint |
|----------|------|---------------|
| Wallet | `get_portfolio` | `GET /wallet/{a}/portfolio` |
| Wallet | `get_history` | `GET /wallet/{a}/history` |
| DEX | `list_dex_pairs` | `GET /dex/pairs` |
| Market | `get_trending` | `GET /market/trending` |
| Market | `get_global_market` | `GET /market/global` |
| On-chain | `get_smart_money` | `GET /on-chain/smart-money` |
| On-chain | `get_whale_activity` | `GET /on-chain/whale-activity` |
| On-chain | `get_token_holders` | `GET /on-chain/token-holders/{s}` |
| Signals | `get_signal` | `GET /signals/{name}` |
| KB | `kb_glossary` | `GET /kb/glossary/{term}` |

Cut list rationale: the core 22 are enough to demo "autonomously create, backtest, deploy, and execute a strategy" end-to-end. The 10 nice-to-haves are research/analytics surface that the user can reach via other Mangrove tooling if needed.

---

## Data Models

### Pydantic request/response models

```python
# Wallets
class WalletCreateRequest(BaseModel):
    chain: Literal["evm", "xrpl"]
    network: Literal["mainnet", "testnet"]
    chain_id: int | None = None
    label: str | None = None

class WalletCreateResponse(BaseModel):
    address: str
    chain: str
    network: str
    chain_id: int | None
    label: str | None
    created_at: datetime
    seed_phrase: str  # ONLY returned on create, never again
    warning: str

class WalletListItem(BaseModel):
    address: str
    chain: str
    network: str
    chain_id: int | None
    label: str | None
    created_at: datetime


# Strategies
class StrategyRule(BaseModel):
    name: str                                 # signal name
    signal_type: Literal["TRIGGER", "FILTER"]
    timeframe: str | None = None
    params: dict[str, Any]

class ExecutionConfig(BaseModel):
    initial_balance: float = 10000
    max_open_positions: int = 3
    max_trades_per_day: int = 10
    max_risk_per_trade: float = 0.02
    max_units_per_trade: float | None = None
    max_trade_amount: float | None = None
    min_trade_amount: float = 25
    volatility_window: int = 24
    target_volatility: float = 0.1

class StrategyCreateAutonomousRequest(BaseModel):
    goal: str                                  # natural-language
    asset: str
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"]
    candidate_count: int = Field(7, ge=5, le=10)
    backtest_lookback_months: int = 3

class StrategyCreateManualRequest(BaseModel):
    name: str
    asset: str
    timeframe: str
    entry: list[StrategyRule]
    exit: list[StrategyRule] = []
    execution_config: ExecutionConfig | None = None

class StrategyDetail(BaseModel):
    id: str                                    # agent's local UUID
    mangrove_id: str                           # Mangrove's strategy ID
    name: str
    asset: str
    timeframe: str
    status: Literal["draft", "inactive", "paper", "live", "archived"]
    entry: list[StrategyRule]
    exit: list[StrategyRule]
    execution_config: ExecutionConfig
    generation_report: dict | None = None       # for autonomous strategies
    created_at: datetime
    updated_at: datetime


# Allocations (live strategies only)
class Allocation(BaseModel):
    strategy_id: str
    wallet_address: str
    token_address: str
    token_symbol: str
    amount: float
    active: bool
    created_at: datetime
    released_at: datetime | None


# Execution
class OrderIntent(BaseModel):
    """Pure output of strategy_evaluator. No side effects."""
    action: Literal["enter", "exit"]
    side: Literal["buy", "sell"]
    symbol: str
    amount: float
    reason: str                                 # which signal fired
    stop_loss: float | None = None
    take_profit: float | None = None

class Evaluation(BaseModel):
    """A record of one cron-tick evaluation. OrderIntents come from the SDK,
    not from local logic — the agent does not evaluate strategies itself."""
    id: str
    strategy_id: str
    timestamp: datetime
    market_snapshot: dict                        # last bar of data passed to SDK
    sdk_response: dict                           # verbatim response from mangrove_ai.execution.evaluate()
    order_intents: list[OrderIntent]             # extracted from sdk_response for easy querying
    duration_ms: int
    status: Literal["ok", "error", "skipped"]
    error_msg: str | None

class Trade(BaseModel):
    id: str
    strategy_id: str
    evaluation_id: str
    order_intent: OrderIntent
    mode: Literal["live", "paper"]
    tx_hash: str | None                          # null for paper
    input_token: str
    input_amount: float
    output_token: str
    output_amount: float
    fill_price: float
    fees: dict                                   # gas, protocol, slippage
    status: Literal["pending", "confirmed", "failed", "simulated"]
    executed_at: datetime
    confirmed_at: datetime | None
    p_and_l: float | None                        # filled when the position closes

class Position(BaseModel):
    id: str
    strategy_id: str
    asset: str
    entry_trade_id: str
    exit_trade_id: str | None
    entry_price: float
    entry_amount: float
    entry_time: datetime
    exit_price: float | None
    exit_amount: float | None
    exit_time: datetime | None
    status: Literal["open", "closed"]
    stop_loss: float | None
    take_profit: float | None


# Backtest
class BacktestRequest(BaseModel):
    mode: Literal["quick", "full"]
    lookback_months: int = 3
    start_date: datetime | None = None
    end_date: datetime | None = None

class BacktestResult(BaseModel):
    strategy_id: str
    mode: str
    metrics: dict                                 # irr, sharpe, sortino, max_dd, win_rate, total_trades, net_pnl
    trades: list[dict] | None                     # only populated for full mode
    duration_ms: int
```

---

### SQLite Schema

```sql
-- Wallets: encrypted local key storage
CREATE TABLE wallets (
    id TEXT PRIMARY KEY,                          -- UUID
    address TEXT UNIQUE NOT NULL,
    chain TEXT NOT NULL,                          -- evm | xrpl
    network TEXT NOT NULL,                        -- mainnet | testnet
    chain_id INTEGER,
    encrypted_secret BLOB NOT NULL,               -- Fernet-encrypted seed phrase
    encryption_method TEXT NOT NULL,              -- 'fernet-v1'
    label TEXT,
    created_at TEXT NOT NULL,                     -- ISO 8601
    metadata_json TEXT
);
CREATE INDEX idx_wallets_chain ON wallets(chain, chain_id);

-- Strategies: local cache of Mangrove strategies (Mangrove is source of truth)
CREATE TABLE strategies (
    id TEXT PRIMARY KEY,                          -- agent's UUID
    mangrove_id TEXT UNIQUE NOT NULL,             -- Mangrove's strategy ID
    name TEXT NOT NULL,
    asset TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,                         -- draft | inactive | paper | live | archived
    entry_json TEXT NOT NULL,                     -- list[StrategyRule]
    exit_json TEXT NOT NULL,
    execution_config_json TEXT NOT NULL,
    generation_report_json TEXT,                  -- null for manual strategies
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_strategies_status ON strategies(status);

-- Allocations: per-strategy fund commitments (live only)
CREATE TABLE allocations (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    wallet_address TEXT NOT NULL REFERENCES wallets(address),
    token_address TEXT NOT NULL,
    token_symbol TEXT NOT NULL,
    amount REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,            -- boolean
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX idx_allocations_strategy ON allocations(strategy_id, active);

-- Evaluations: every cron tick
CREATE TABLE evaluations (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    timestamp TEXT NOT NULL,
    market_snapshot_json TEXT NOT NULL,           -- data sent to the SDK
    sdk_response_json TEXT NOT NULL,              -- verbatim response from mangrove_ai.execution.evaluate()
    order_intents_json TEXT NOT NULL,             -- extracted from sdk_response for querying
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,                         -- ok | error | skipped
    error_msg TEXT
);
CREATE INDEX idx_evaluations_strategy_ts ON evaluations(strategy_id, timestamp DESC);

-- Trades: every order intent → execution
CREATE TABLE trades (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    evaluation_id TEXT REFERENCES evaluations(id),
    order_intent_json TEXT NOT NULL,
    mode TEXT NOT NULL,                           -- live | paper
    tx_hash TEXT,                                 -- null for paper
    input_token TEXT NOT NULL,
    input_amount REAL NOT NULL,
    output_token TEXT NOT NULL,
    output_amount REAL NOT NULL,
    fill_price REAL NOT NULL,
    fees_json TEXT NOT NULL,
    status TEXT NOT NULL,                         -- pending | confirmed | failed | simulated
    executed_at TEXT NOT NULL,
    confirmed_at TEXT,
    p_and_l REAL
);
CREATE INDEX idx_trades_strategy ON trades(strategy_id, executed_at DESC);
CREATE INDEX idx_trades_status ON trades(status);

-- Positions: derived from trades, cached for fast evaluator access
CREATE TABLE positions (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    asset TEXT NOT NULL,
    entry_trade_id TEXT NOT NULL REFERENCES trades(id),
    exit_trade_id TEXT REFERENCES trades(id),
    entry_price REAL NOT NULL,
    entry_amount REAL NOT NULL,
    entry_time TEXT NOT NULL,
    exit_price REAL,
    exit_amount REAL,
    exit_time TEXT,
    status TEXT NOT NULL,                         -- open | closed
    stop_loss REAL,
    take_profit REAL
);
CREATE INDEX idx_positions_strategy_status ON positions(strategy_id, status);

-- APScheduler job store (built-in table schema, managed by apscheduler[sqlalchemy])
-- CREATE TABLE apscheduler_jobs ... (managed by the library)
```

---

## Error Handling

Standard error response:
```json
{
  "error": true,
  "code": "ERROR_CODE",
  "message": "Human-readable description",
  "suggestion": "What to do about it",
  "correlation_id": "uuid"
}
```

### Error codes

| Code | HTTP | When |
|------|------|------|
| `AUTH_MISSING_API_KEY` | 401 | `X-API-Key` header not provided |
| `AUTH_INVALID_API_KEY` | 401 | Key doesn't match the configured `API_KEY` |
| `VALIDATION_ERROR` | 400 | Pydantic validation failed; details in `message` |
| `CONFIRMATION_REQUIRED` | 400 | Live deploy/stop/withdraw without `confirm: true` |
| `WALLET_NOT_FOUND` | 404 | Address not in the agent's wallet store |
| `WALLET_ALREADY_EXISTS` | 409 | Wallet with that address already stored |
| `STRATEGY_NOT_FOUND` | 404 | Strategy ID not found |
| `STRATEGY_INVALID_STATUS_TRANSITION` | 400 | Illegal transition (e.g., draft → live) |
| `STRATEGY_INVALID_COMPOSITION` | 400 | Manual mode: entry/exit rule constraint violated |
| `STRATEGY_NO_VIABLE_CANDIDATES` | 422 | Autonomous mode: no candidates passed filters |
| `ALLOCATION_INSUFFICIENT` | 400 | Wallet balance < requested allocation |
| `SDK_ERROR` | 502 | Upstream Mangrove SDK error; original error in `message` |
| `SIGNING_ERROR` | 500 | Local signing failed (bad key, wallet corruption) |
| `EVALUATION_ERROR` | 500 | Strategy evaluator raised; details logged |
| `SCHEDULER_ERROR` | 500 | APScheduler job registration/cancellation failed |
| `CHAIN_NOT_SUPPORTED_IN_V1` | 501 | User requested XRPL/Solana wallet creation or live execution |
| `INTERNAL_ERROR` | 500 | Catch-all; details in server logs |

All errors carry a `correlation_id` for cross-referencing against the agent's logs.

---

## Authentication & Authorization

**Model:** single-user API key authentication.

**Flow:**
1. The `API_KEY` config value is loaded at startup via the template's config loader (see [Configuration](#configuration)).
2. Requests include `X-API-Key: <key>`.
3. Middleware validates against the configured `API_KEY`; rejects with 401 if missing or invalid.
4. Free tier endpoints bypass the middleware.

**MCP auth:** MCP is served over Streamable HTTP transport. The MCP client (Claude Code, Claude Desktop, custom agent) sends the API key as a standard HTTP header on every request to `/mcp`.

Client-side configuration example (`.mcp.json` in a Claude Code project):
```json
{
  "mcpServers": {
    "mangrove-agent": {
      "transport": "http",
      "url": "http://localhost:9080/mcp",
      "headers": {
        "X-API-Key": "<your-configured-api-key>"
      }
    }
  }
}
```

Server-side, the agent's FastAPI middleware inspects `X-API-Key` identically for REST and MCP requests — the MCP mount at `/mcp` is just another FastAPI route group. If the key is missing or invalid, the MCP tool call returns an MCP-level error with `code: AUTH_INVALID_API_KEY` (mapped to 401 for REST). Discovery tools (`status`, `list_tools`) bypass auth.

**Storage:** No user accounts, no sessions, no tokens. Single shared secret per deployment.

---

## External Integrations

### 1. `mangroveai` SDK

**Purpose:** strategies, backtesting, signals, market data, on-chain, KB.

**Config:**
- `MANGROVE_API_KEY` — prod_* or dev_* (SDK auto-detects env)
- `MANGROVEAI_BASE_URL` — optional override

**Usage:**
```python
from mangrove_ai import MangroveAI
client = MangroveAI()  # reads env
```

**Failure handling:** SDK raises `APIError`, `NotFoundError`, `RateLimitError`. The agent's service layer catches these and re-raises as `SDKError` (502) with the original correlation_id preserved.

**Retry:** SDK's built-in retry handles 429/5xx. The agent does not add additional retry.

---

### 2. `mangrovemarkets` SDK

**Purpose:** DEX swaps, wallet creation, portfolio analytics.

**Config:**
- `MANGROVEMARKETS_BASE_URL` — defaults to `http://localhost:9081` (self-hosted placeholder; port chosen to dodge the VSCode Helper squat on :8080); set to deployed URL in prod
- `MANGROVE_API_KEY` — same key as `mangroveai`

**Usage:**
```python
from mangrove_markets import MangroveMarkets
client = MangroveMarkets(base_url=os.environ["MANGROVEMARKETS_BASE_URL"])
```

**Signing:** The SDK never touches private keys. `prepare_swap()` and `approve_token()` return unsigned transaction payloads; the agent's `wallet_manager` decrypts the key in memory, signs the payload, then calls `broadcast()` with the signed tx. Key is zeroed from memory immediately after.

**Failure handling:** Same pattern as `mangroveai`.

---

### 3. Local Key Encryption

**Library:** `cryptography` (Fernet symmetric encryption).

**Master key source (priority order):**
1. OS Keychain via `keyring` library (macOS Keychain, GNOME Keyring, Windows Credential Manager) — default
2. Config value `MASTER_KEY_ENV_FALLBACK` (resolved via the template's config loader, can reference a secret) — fallback for CI or other environments without a keychain

**Scheme:**
- Master key generated once on first wallet creation, stored in keychain
- Each wallet's seed phrase encrypted with `Fernet(master_key)` before DB insert
- Decryption only in `wallet_manager.sign()` scope; decrypted bytes never logged, never returned from an endpoint

---

### 4. APScheduler

**Library:** `apscheduler` with `BackgroundScheduler` and SQLAlchemy job store.

**Job store:** the same SQLite DB as the agent's data (config value `DB_PATH`) — survives process restarts.

**Timeframe mapping:**
| Strategy timeframe | Cron expression |
|--------------------|-----------------|
| 1m | `*/1 * * * *` |
| 5m | `*/5 * * * *` |
| 15m | `*/15 * * * *` |
| 1h | `0 * * * *` |
| 4h | `0 */4 * * *` |
| 1d | `0 0 * * *` |

**Lifecycle:**
- Scheduler starts in FastAPI lifespan (`on_startup`)
- Strategy activation (`paper` or `live`) → register job `eval-<strategy_id>`
- Strategy deactivation → remove job
- Job fires → call `strategy_evaluator.evaluate(strategy_id)`

**Failure handling:** failed evaluations are logged to the `evaluations` table with `status='error'` and `error_msg` populated. The strategy remains active — transient failures (API outages, etc.) should not stop the schedule.

---

## Configuration

The agent uses the existing mangrove-agent config system — no invented `.env` files, no parallel layer. Values live in `server/src/config/{environment}-config.json`, with required keys declared in `server/src/config/configuration-keys.json`. The `ENVIRONMENT` env var selects which file to load. Secret values can be referenced using the existing `secret:NAME:PROPERTY` syntax; literal values are fine for local dev.

### `configuration-keys.json` (agent + x402, both required)

x402 keys from the template stay required — payment middleware needs them at startup even if no agent endpoints are payment-gated yet.

```json
{
  "required": [
    "AUTH_ENABLED",
    "API_KEY",
    "MANGROVE_API_KEY",
    "MANGROVEMARKETS_BASE_URL",
    "DB_PATH",
    "KEYRING_SERVICE_NAME",
    "MASTER_KEY_ENV_FALLBACK",
    "BACKTEST_CANDIDATE_COUNT",
    "BACKTEST_MIN_WIN_RATE",
    "BACKTEST_MIN_TRADES",
    "BACKTEST_DEFAULT_LOOKBACK_MONTHS",
    "LOG_RETENTION_DAYS",
    "X402_FACILITATOR_URL",
    "X402_NETWORK",
    "X402_PAY_TO",
    "X402_USDC_CONTRACT",
    "X402_HELLO_MANGROVE_PRICE",
    "X402_CDP_API_KEY_ID",
    "X402_CDP_API_KEY_SECRET"
  ],
  "full_app_keys": []
}
```

### Example `local-config.json`

```json
{
  "AUTH_ENABLED": true,
  "API_KEY": "local-dev-key",
  "MANGROVE_API_KEY": "dev_...",
  "MANGROVEMARKETS_BASE_URL": "http://localhost:9081",
  "DB_PATH": "./agent.db",
  "KEYRING_SERVICE_NAME": "mangrove-agent",
  "MASTER_KEY_ENV_FALLBACK": "",
  "BACKTEST_CANDIDATE_COUNT": 7,
  "BACKTEST_MIN_WIN_RATE": 0.51,
  "BACKTEST_MIN_TRADES": 10,
  "BACKTEST_DEFAULT_LOOKBACK_MONTHS": 3,
  "LOG_RETENTION_DAYS": 90,
  "X402_FACILITATOR_URL": "https://x402.org/facilitator",
  "X402_NETWORK": "eip155:84532",
  "X402_PAY_TO": "0xde991861bB3e7078015826Fad749de398F6ec1f6",
  "X402_USDC_CONTRACT": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
  "X402_HELLO_MANGROVE_PRICE": "50000",
  "X402_CDP_API_KEY_ID": "",
  "X402_CDP_API_KEY_SECRET": ""
}
```

### Notes

- `API_KEY`, `MANGROVE_API_KEY`, and `MASTER_KEY_ENV_FALLBACK` are the secret-sensitive values. In environments with Secret Manager configured, they can be written as `"secret:mangrove-api-key:value"`; local deployments use literal strings.
- `MASTER_KEY_ENV_FALLBACK` is intentionally non-required at runtime — if empty, the agent uses the OS keychain. Set it only for environments without a keychain (e.g., CI).
- `full_app_keys` is empty for v1 (no Postgres or Redis).
- The `MANGROVEAI_BASE_URL` is auto-detected by the SDK from the `MANGROVE_API_KEY` prefix (`prod_*` vs `dev_*`); no separate config key needed.

---

## Service Layer Modules

All routes and MCP tools delegate to these services. Never duplicate business logic between the two interfaces.

**Service principle:** if a service would just forward arguments to an SDK call and return the result, it doesn't exist. Routes call the SDK clients directly (via `shared/clients/mangrove.py` singletons). Services exist only when they add orchestration the SDK doesn't provide.

The 8 services that stay:

| Module | Responsibility |
|--------|---------------|
| `services/wallet_manager.py` | Key gen, Fernet encryption, local signing of unsigned txs from the SDK |
| `services/strategy_service.py` | Cron-tick orchestration: call `mangroveai.execution.evaluate(strategy_id)` (SDK handles market data fetch + signal eval + risk gates internally), dispatch returned orders to `order_executor`. Strategy CRUD against `mangroveai.strategies` + local cache writes. No local signal evaluation or risk logic. |
| `services/candidate_generator.py` | Autonomous: goal → 5–10 signal combos (deterministic heuristics over the `mangroveai.signals` catalog) |
| `services/backtest_service.py` | Quick + full backtest orchestration; filter + rank by IRR |
| `services/order_executor.py` | The single execution path for all DEX swaps. Takes an `OrderIntent` (from `strategy_service` for cron-driven trades, or built from a user request for the `POST /dex/swap` route). Orchestrates the full 6-step flow against `mangrovemarkets.dex` (quote → conditional approve → sign → broadcast → poll → prepare → sign → broadcast → poll). Branches paper vs live. |
| `services/scheduler_service.py` | APScheduler wrapper: register, cancel, list active jobs |
| `services/trade_log.py` | SQLite writes: evaluations, trades, positions |
| `services/allocation_service.py` | Local allocation accounting for live strategies |

**Routes that call SDKs directly (no service layer):** signal listing, market data (OHLCV, current data, trending, global), on-chain analytics, KB search/glossary, portfolio (value, P&L, tokens, DeFi, history), DEX venue/pair listing, DEX quote. Each route handler imports the appropriate SDK client from `shared/clients/mangrove.py`, calls the method, returns the response. Adding a wrapper service for these would only duplicate the SDK's interface.

**Architectural note:** there is no `strategy_evaluator.py` module. Strategy evaluation (signal firing, position sizing, risk gates, cooldowns, volatility adjustments) is entirely the responsibility of `mangroveai.execution.evaluate()`. The agent calls the SDK and executes whatever `OrderIntent[]` comes back. Reimplementing any of that logic locally would duplicate upstream work and inevitably drift out of sync.

---

## Traceability: User Story → Endpoint

Every user story maps to at least one endpoint.

| Story | Endpoints |
|-------|-----------|
| US-1 create wallet | `POST /wallet/create` |
| US-2 balances | `GET /wallet/{a}/balances` |
| US-3 DEX venues/pairs | `GET /dex/venues`, `GET /dex/pairs` |
| US-4 swap quote | `POST /dex/quote` |
| US-5 execute swap | `POST /dex/swap` |
| US-6 OHLCV | `GET /market/ohlcv` |
| US-7 market data | `GET /market/data` |
| US-8 trending / global | `GET /market/trending`, `GET /market/global` |
| US-9 on-chain | `GET /on-chain/smart-money`, `/whale-activity`, `/token-holders` |
| US-10 portfolio | `GET /wallet/{a}/portfolio`, `/history` |
| US-11 signals | `GET /signals`, `GET /signals/{name}` |
| US-12 create strategy | `POST /strategies/autonomous`, `POST /strategies/manual` |
| US-13 list/view strategies | `GET /strategies`, `GET /strategies/{id}` |
| US-14 update status | `PATCH /strategies/{id}/status` |
| US-15 backtest | `POST /strategies/{id}/backtest` |
| US-16 automated eval loop | `PATCH /strategies/{id}/status` (activates cron) + `POST /strategies/{id}/evaluate` (manual tick) |
| US-17 deposit/withdraw | `PATCH /strategies/{id}/status` with `allocation` block (deposit on → live) or confirm (withdraw on → inactive) |
| US-18 KB | `GET /kb/search`, `GET /kb/glossary/{term}` |
