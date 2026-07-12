# Hank API Reference

**Generated:** 2026-04-06
**Status:** Draft

This document consolidates all Mangrove API endpoints available to Hank, organized by **Core** (the essentials) and **Extended** (advanced capabilities).

---

# Core Endpoints

These are the primary endpoints for Hank's trading bot functionality: wallet management, DEX trading, market data (OHLCV), strategy creation, backtesting, and execution.

---

## 1. Wallet Management

### 1.1 Get Chain Info

**Source:** MangroveMarkets MCP Server
**Tool:** `wallet_chain_info`
**REST:** `POST /api/v1/tools/wallet_chain_info`
**Access:** Free

Get chain configuration: supported networks, RPC URLs, native token info.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `chain` | string | no | `"xrpl"` (default), `"evm"`, or `"solana"` |

**Response:**
```json
{
  "chain": "evm",
  "chain_family": "evm",
  "native_token": "ETH",
  "wallet_creation": "client_side_only",
  "supported_chain_ids": [1, 8453, 42161, 137, 10, 56, 43114],
  "networks": {
    "8453": {"name": "Base", "explorer": "https://basescan.org"}
  },
  "sdk_method": "client.wallet.create('evm')"
}
```

---

### 1.2 Create Wallet

**Source:** MangroveMarkets MCP Server
**Tool:** `wallet_create`
**REST:** `POST /api/v1/tools/wallet_create`
**Access:** Free

Create a new wallet. XRPL wallets are funded via testnet faucet. EVM wallets generate a random keypair (unfunded). **Solana: NOT_IMPLEMENTED (Phase 3).**

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `chain` | string | no | `"xrpl"` (default), `"evm"`, or `"solana"` (not yet implemented) |
| `network` | string | no | XRPL: `"testnet"` (default) or `"devnet"`. Ignored for EVM. |
| `chain_id` | int | no | EVM chain ID (1=Ethereum, 8453=Base). Ignored for XRPL. |

**XRPL Response:**
```json
{
  "address": "r4Vx2CdzRwHQHqGCUgDjTqa8PoFRdJjPuJ",
  "secret": "sEdV...",
  "seed_phrase": null,
  "network": "testnet",
  "is_funded": true,
  "warnings": ["IMPORTANT: Save your wallet secret now..."]
}
```

**EVM Response:**
```json
{
  "address": "0xbf57B1ACf74885e215617783Fad4aE4DF849A8d0",
  "private_key": "0x4f9010df...",
  "chain_id": 8453,
  "network": "evm",
  "is_funded": false,
  "warnings": ["IMPORTANT: Save your private key now...", "Fund this wallet yourself..."]
}
```

**Security:** No tool accepts private keys. Keys are returned once at creation -- the agent stores them locally.

---

### 1.3 Check Balance

**Source:** MangroveMarkets MCP Server
**Tool:** `wallet_balance`
**REST:** `POST /api/v1/tools/wallet_balance`
**Status:** NOT_IMPLEMENTED (Phase 1)

Check wallet balance for an address.

---

### 1.4 List Transactions

**Source:** MangroveMarkets MCP Server
**Tool:** `wallet_transactions`
**REST:** `POST /api/v1/tools/wallet_transactions`
**Status:** NOT_IMPLEMENTED (Phase 1)

List recent transactions for an address.

---

### 1.5 Check Balances (1inch)

**Source:** MangroveMarkets MCP Server
**Tool:** `oneinch_balances`
**REST:** `POST /api/v1/tools/oneinch_balances`
**Access:** Free

Get all token balances for a wallet on an EVM chain.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `chain_id` | int | yes | EVM chain ID |
| `wallet` | string | yes | Wallet address |

**Response:**
```json
{
  "chain_id": 8453,
  "wallet": "0xbf57...",
  "balances": {
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee": "5470648682909640",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "9000000"
  }
}
```

---

## 2. DEX Activity

### 2.1 List Supported Venues

**Source:** MangroveMarkets MCP Server
**Tool:** `dex_supported_venues`
**REST:** `POST /api/v1/tools/dex_supported_venues`
**Access:** Free

**Parameters:** None

**Response:**
```json
{
  "venues": [
    {"id": "xpmarket", "name": "XPMarket", "chain": "xrpl-testnet", "status": "active", "supported_pairs_count": 2, "fee_percent": 0.001},
    {"id": "jupiter", "name": "Jupiter", "chain": "solana-devnet", "status": "active", "supported_pairs_count": 3, "fee_percent": 0.002},
    {"id": "1inch", "name": "1inch Aggregator", "chain": "multi", "status": "active", "supported_pairs_count": 3, "fee_percent": 0.0025}
  ]
}
```

---

### 2.2 List Supported Pairs

**Source:** MangroveMarkets MCP Server
**Tool:** `dex_supported_pairs`
**REST:** `POST /api/v1/tools/dex_supported_pairs`
**Access:** Free

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `venue_id` | string | yes | Venue ID (e.g. `"1inch"`, `"xpmarket"`, `"jupiter"`) |

**Response:**
```json
{
  "pairs": [
    {"venue_id": "1inch", "base_token": "ETH", "quote_token": "USDC", "is_active": true}
  ]
}
```

---

### 2.3 Get Quote

**Source:** MangroveMarkets MCP Server
**Tool:** `dex_get_quote`
**REST:** `POST /api/v1/tools/dex_get_quote`
**Access:** Free

Get the best swap quote across all venues or from a specific venue.

> **Units note.** This is the **raw backend tool**, whose `amount` is in the
> token's **smallest units** (wei / base units), as shown below. The agent's
> own `get_swap_quote` tool and `POST /api/v1/agent/dex/quote` route instead
> take `amount` in **human units** (e.g. `0.001` = 0.001 ETH) and convert to
> base units for you — passing a human float (e.g. `0.001`) straight to the
> raw tool reads as sub-wei dust and returns `INSUFFICIENT_LIQUIDITY`.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `input_token` | string | yes | Source token address or symbol |
| `output_token` | string | yes | Destination token address or symbol |
| `amount` | float | yes | Amount in smallest units (e.g. wei for ETH, 1e6 for USDC) |
| `venue_id` | string | no | Specific venue (omit for best across all) |
| `chain_id` | int | no | EVM chain ID (required for 1inch) |
| `mode` | string | no | `"standard"` (default, fee in swap) or `"x402"` (separate payment) |

**Example -- 1 USDC to ETH on Base:**
```json
{
  "input_token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  "output_token": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
  "amount": 1000000,
  "venue_id": "1inch",
  "chain_id": 8453
}
```

**Response:**
```json
{
  "quote_id": "1inch-a1b2c3d4...",
  "venue_id": "1inch",
  "input_token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  "output_token": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
  "input_amount": 1000000,
  "output_amount": 459244868977722,
  "exchange_rate": 4.59e-7,
  "venue_fee": 0.0,
  "mangrove_fee": 2500.0,
  "total_cost": 1002500.0,
  "chain_id": 8453,
  "billing_mode": "standard"
}
```

---

### 2.4 Approve Token

**Source:** MangroveMarkets MCP Server
**Tool:** `dex_approve_token`
**REST:** `POST /api/v1/tools/dex_approve_token`
**Access:** Free

Get unsigned ERC-20 approval calldata. Required before swapping ERC-20 tokens on EVM chains. Not needed for native tokens (ETH) or non-EVM chains.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `token_address` | string | yes | ERC-20 token contract address |
| `chain_id` | int | yes | EVM chain ID |
| `wallet_address` | string | yes | Agent's public wallet address |
| `amount` | float | no | Approval amount in smallest units (default: unlimited) |

**Response:** `UnsignedTransaction` with approval calldata, or `null` for non-EVM chains.

---

### 2.5 Prepare Swap

**Source:** MangroveMarkets MCP Server
**Tool:** `dex_prepare_swap`
**REST:** `POST /api/v1/tools/dex_prepare_swap`
**Access:** Free

Get unsigned swap calldata for a previously obtained quote. The agent signs this locally and broadcasts via `dex_broadcast`.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `quote_id` | string | yes | The `quote_id` from `dex_get_quote` |
| `wallet_address` | string | yes | Agent's public wallet address |
| `slippage` | float | no | Slippage tolerance % (default 1.0) |

**Response:**
```json
{
  "chain_family": "evm",
  "chain_id": 8453,
  "venue_id": "1inch",
  "description": "Swap USDC for ETH via 1inch on chain 8453",
  "payload": {
    "to": "0x111111125421cA6dc452d289314280a0f8842A65",
    "data": "0x12aa3caf...",
    "value": "0",
    "gas": 234948,
    "gasPrice": "7500000"
  },
  "estimated_gas": "234948"
}
```

---

### 2.6 Broadcast Transaction

**Source:** MangroveMarkets MCP Server
**Tool:** `dex_broadcast`
**REST:** `POST /api/v1/tools/dex_broadcast`
**Access:** Free

Broadcast a locally-signed transaction to the network.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `signed_tx` | string | yes | Raw signed transaction as hex string |
| `chain_id` | int | yes | Chain ID where to broadcast |
| `venue_id` | string | no | Venue hint for routing |
| `mev_protection` | bool | no | Use Flashbots private mempool (default: false) |

**Response:**
```json
{
  "tx_hash": "0xc29ac8f7663151c9...",
  "chain_family": "evm",
  "chain_id": 8453,
  "venue_id": "1inch",
  "broadcast_method": "public"
}
```

---

### 2.7 Check Transaction Status

**Source:** MangroveMarkets MCP Server
**Tool:** `dex_tx_status`
**REST:** `POST /api/v1/tools/dex_tx_status`
**Access:** Free

Check the on-chain status of a broadcast transaction.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `tx_hash` | string | yes | Transaction hash from `dex_broadcast` |
| `chain_id` | int | yes | Chain ID where the tx was broadcast |
| `venue_id` | string | no | Venue hint |

**Response:**
```json
{
  "tx_hash": "0xc29ac8f7...",
  "chain_family": "evm",
  "chain_id": 8453,
  "status": "confirmed",
  "block_number": 12345678,
  "gas_used": "150000"
}
```

---

### Complete Swap Flow

```
1. dex_get_quote(...)        -> Returns quote with quote_id
2. dex_approve_token(...)    -> Returns unsigned approval calldata
   -> Agent signs locally    -> dex_broadcast(signed_approval)
   -> dex_tx_status(...)     -> Wait for confirmation
3. dex_prepare_swap(...)     -> Returns unsigned swap calldata
   -> Agent signs locally    -> dex_broadcast(signed_swap)
   -> dex_tx_status(...)     -> Wait for confirmation
4. Done. Token swap confirmed on-chain.
```

---

## 3. Market Data (OHLCV)

### 3.1 Get OHLCV Data

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/ohlcv/{symbol}`
**Auth:** Bearer token

Get historical candlestick data for technical analysis and backtesting.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | string (path) | yes | Asset symbol (e.g. `"ETH"`, `"BTC"`) |
| `days` | int (query) | no | Number of days (default: 30) |
| `provider` | string (query) | no | Data provider (default: `"coinapi"`) |

**Response:**
```json
{
  "success": true,
  "symbol": "ETH",
  "data_points": 30,
  "provider": "coinapi",
  "data": [
    {
      "timestamp": "2026-01-04T00:00:00Z",
      "open": 3200.00,
      "high": 3250.00,
      "low": 3150.00,
      "close": 3240.50,
      "volume": 850000
    }
  ]
}
```

---

### 3.2 Get Real-Time Market Data

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/market-data/{symbol}`
**Auth:** Bearer token

Get current price, market cap, and 24h volume.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | string (path) | yes | Asset symbol |
| `provider` | string (query) | no | Data provider (default: `"coingecko"`) |

**Response:**
```json
{
  "success": true,
  "symbol": "BTC",
  "data": {
    "current_price": 91282.00,
    "market_cap": 1823027068010,
    "volume_24h": 30539603461,
    "price_change_24h_pct": 2.3,
    "price_change_7d_pct": -1.5,
    "ath": 108000,
    "ath_date": "2024-12-17"
  }
}
```

---

## 4. Strategy Management

### 4.1 List Strategies

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/strategies`
**Auth:** Bearer token

List all strategies for the authenticated user. Returns summary objects (no rules/config).

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `skip` | int (query) | no | Pagination offset (default: 0) |
| `limit` | int (query) | no | Max results (default: 100) |

**Response:**
```json
{
  "success": true,
  "strategies": [
    {
      "id": "5089b9a8-f375-4813-8ed0-d7175368a11e",
      "name": "eth_strategy",
      "asset": "ETH",
      "status": "inactive",
      "created_at": "2026-01-24T22:01:51Z",
      "strategy_type": "momentum",
      "description": "Buys ETH when RSI crosses above 50 while price is above the 50-period SMA"
    }
  ],
  "total": 1,
  "skip": 0,
  "limit": 10
}
```

---

### 4.2 Create Strategy

**Source:** MangroveAI
**Endpoint:** `POST /api/v1/strategies`
**Auth:** Bearer token

Create a new trading strategy with entry/exit signal rules.

**Required Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Strategy name |
| `asset` | string | Asset symbol (e.g. `"BTC"`, `"ETH"`) |
| `entry` | array | Entry rules (exactly 1 TRIGGER, 0+ FILTERs) |

**Optional Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `exit` | array | Exit rules (default: `[]`) |
| `reward_factor` | number | Risk/reward ratio (default: 2.0) |
| `strategy_type` | string | One of: `trend_following`, `momentum`, `volatility`, `breakout`, `mean_reversion` |
| `description` | string | Human-readable description |
| `execution_config` | object | Execution parameters (defaults from `trading_defaults.json`) |

**Strategy Rules Format:**

Each rule object:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Signal function name (e.g. `"rsi_oversold"`, `"ema_cross_up"`) |
| `signal_type` | string | yes | `"TRIGGER"` (event-based) or `"FILTER"` (state-based) |
| `timeframe` | string | yes | Candle interval: `5m`, `15m`, `30m`, `1h`, `4h`, `1D` |
| `params` | object | yes | Signal-specific parameters |

**Signal Composition Constraints:**
- **Entry:** Exactly 1 TRIGGER + 0 or more FILTERs. All must be true for entry.
- **Exit:** 0 or 1 TRIGGER + 0 or more FILTERs.
- **Stop loss / take profit** are NOT signal rules -- managed by execution engine via ATR-based risk management in `execution_config`.

**Request Example:**
```json
{
  "name": "BTC RSI Strategy",
  "asset": "BTC",
  "strategy_type": "momentum",
  "description": "Buys BTC when RSI crosses below 30 while price holds above the 50-period SMA",
  "entry": [
    {"name": "rsi_oversold", "signal_type": "TRIGGER", "timeframe": "1h", "params": {"window": 14, "threshold": 30}},
    {"name": "is_above_sma", "signal_type": "FILTER", "timeframe": "1h", "params": {"window": 50}}
  ],
  "exit": [],
  "reward_factor": 2.0
}
```

**Response (201):** Full strategy object including auto-populated `execution_config` and `execution_state`.

---

### 4.3 Get Strategy

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/strategies/{strategy_id}`
**Auth:** Bearer token

Retrieve full strategy details including rules, execution_config, and execution_state.

---

### 4.4 Update Strategy Status

**Source:** MangroveAI
**Endpoint:** `PATCH /api/v1/strategies/{strategy_id}/status`
**Auth:** Bearer token

Update a strategy's deployment status.

**Body:**
```json
{"status": "paper"}
```

**Valid statuses:** `draft`, `inactive`, `paper`, `live`, `archived`

---

### 4.5 Delete Strategy

**Source:** MangroveAI
**Endpoint:** `DELETE /api/v1/strategies/{strategy_id}`
**Auth:** Bearer token

Delete a strategy. Only the owner can delete.

---

### Execution Config Reference

Every strategy carries an `execution_config` with these fields (defaults from `trading_defaults.json`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_risk_per_trade` | number | 0.01 | Max risk per trade (1%) |
| `reward_factor` | number | 2 | Risk/reward ratio target |
| `atr_period` | int | 14 | ATR lookback period in bars |
| `atr_volatility_factor` | number | 2.0 | ATR multiplier for stop loss |
| `min_balance_threshold` | number | 0.1 | Minimum account balance |
| `min_trade_amount` | number | 25 | Minimum trade size ($) |
| `max_open_positions` | int | 10 | Max concurrent positions |
| `max_trades_per_day` | int | 50 | Daily trade limit |
| `volatility_window` | int | 24 | Volatility calc window (bars) |
| `target_volatility` | number | 0.02 | Target volatility level |
| `volatility_mode` | string | `"stddev"` | `"stddev"` or `"atr"` |
| `enable_volatility_adjustment` | bool | false | Volatility-based position sizing |
| `cooldown_bars` | int | 24 | Bars between trades |
| `max_hold_bars` | int | 50 | Max bars to hold a position |
| `exit_on_loss_after_bars` | int | 50 | Exit losers after N bars |
| `exit_on_profit_after_bars` | int | 60 | Exit winners after N bars |
| `profit_threshold_pct` | number | 0.05 | Threshold to consider "winning" |

---

## 5. Backtesting

### 5.1 Run Backtest

**Source:** MangroveAI
**Endpoint:** `POST /api/v1/backtesting/backtest`
**Auth:** Bearer token

Run a synchronous backtest against historical market data.

**Required Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `asset` | string | Asset symbol (e.g. `"BTC"`, `"ETH"`) |
| `interval` | string | Time interval (`"1h"`, `"4h"`, `"1d"`) |
| `initial_balance` | number | Starting account balance |
| `strategy_json` | string | **Stringified** JSON strategy config |
| `min_balance_threshold` | number | Minimum balance threshold |
| `min_trade_amount` | number | Minimum trade size |
| `max_open_positions` | int | Max concurrent positions |
| `max_trades_per_day` | int | Daily trade limit |
| `max_risk_per_trade` | number | Max risk per trade (e.g. 0.01 = 1%) |
| `max_units_per_trade` | number | Max position size in units |
| `max_trade_amount` | number | Max trade amount ($) |
| `volatility_window` | int | Volatility window (bars) |
| `target_volatility` | number | Target volatility |
| `volatility_mode` | string | `"stddev"` or `"atr"` |
| `enable_volatility_adjustment` | bool | Volatility-based sizing |
| `cooldown_bars` | int | Bars between trades |
| `daily_momentum_limit` | number | Daily momentum threshold |
| `weekly_momentum_limit` | number | Weekly momentum threshold |

**Date Range Options (at least one required):**

| Mode | Parameters | Behavior |
|------|-----------|----------|
| Explicit Range | `start_date` + `end_date` | Exact date range |
| From Date to Now | `start_date` only | Start to current time |
| Lookback from End | `end_date` + `lookback_months` | N months back from end |
| Recent History | `lookback_months` only | N months from now |

**Optional Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `slippage_pct` | number | Max slippage per leg (default: 0.004 = 0.4%) |
| `fee_pct` | number | Max fee rate (default: 0.0085 = 0.85%) |
| `execution_config` | object | ATR params (`atr_period`, `atr_volatility_factor`). Defaults from `trading_defaults.json`. |

**Important:** `atr_period` and `atr_volatility_factor` belong in `execution_config`, NOT in `strategy_json`.

**Response:**
```json
{
  "success": true,
  "metrics": {
    "sharpe_ratio": 1.23,
    "sortino_ratio": 1.10,
    "calmar_ratio": 0.80,
    "irr_annualized": 0.25,
    "max_drawdown": 0.15,
    "max_drawdown_duration": 42,
    "win_rate": 0.55
  },
  "trade_history": [
    {
      "entry_price": 95000.0,
      "exit_price": 97500.0,
      "position_size": 0.01,
      "profit_loss": 18.75,
      "entry_timestamp": "2025-04-10T08:00:00+00:00",
      "exit_timestamp": "2025-04-12T14:00:00+00:00",
      "exit_reason": "TP",
      "strategy": "BTC RSI Strategy"
    }
  ],
  "execution_time_seconds": 3.21,
  "trade_count": 12
}
```

---

### 5.2 Retrieve Backtest Result

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/backtesting/backtest/{backtest_id}`
**Auth:** Bearer token

Retrieve a previously executed backtest by ID (persisted via AI Copilot workflow).

---

### 5.3 Retrieve Backtest Trades

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/backtesting/backtest/{backtest_id}/trades`
**Auth:** Bearer token

Get trade history for a backtest run.

**Response:**
```json
{
  "success": true,
  "backtest_id": "550e8400...",
  "trade_count": 35,
  "trades": [
    {
      "trade_id": "b976121d...",
      "outcome": "win",
      "profit_loss": 86.37,
      "asset": "PAXG",
      "side": "exit_long",
      "entry_price": 4338.24,
      "exit_price": 4435.75,
      "position_size": 2.22929936,
      "entry_timestamp": "2025-12-16T14:00:00+00:00",
      "exit_timestamp": "2025-12-22T06:00:00+00:00",
      "exit_reason": "TP",
      "stop_loss_price": 4293.38,
      "take_profit_price": 4427.95
    }
  ]
}
```

---

### 5.4 Bulk Backtest

**Source:** MangroveAI
**Endpoint:** `POST /api/v1/backtesting/backtest/bulk`
**Auth:** Bearer token

Run multiple strategies over a shared date range. Market data is fetched once per unique `(asset, timeframe)` pair and reused across strategies.

**Strategy Sources (provide exactly one):**

| Field | Type | Description |
|-------|------|-------------|
| `strategy_ids` | array | UUIDs of saved strategies |
| `strategy_configs` | array | Inline strategy configs (parsed JSON, not stringified) |

Same required parameters as single backtest, plus `start_date` and `end_date` (both required for bulk).

**Response includes per-strategy results:**
```json
{
  "success": true,
  "results": [
    {
      "success": true,
      "strategy_name": "BTC MACD Strategy",
      "metrics": {"sharpe_ratio": 1.42, "win_rate": 0.57, "total_trades": 38},
      "trade_count": 38,
      "execution_time_seconds": 2.1
    }
  ],
  "data_fetches": 4,
  "total_execution_time_seconds": 8.7
}
```

---

## 6. Strategy Execution

### 6.1 Evaluate Strategy (by ID)

**Source:** MangroveAI
**Endpoint:** `POST /api/v1/execution/evaluate/{strategy_id}`
**Auth:** Bearer token

Evaluate a strategy against current market data. Loads open positions, checks exit conditions (SL, TP, signals, time-based), evaluates entry signals, returns/persists orders.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `persist` | bool | no | Persist orders/positions/trades (default: true) |

**Response:**
```json
{
  "success": true,
  "strategy_id": "48982c90...",
  "strategy_name": "BTC Momentum Strategy",
  "asset": "BTC-USD",
  "current_price": 77287.01,
  "timestamp": "2026-01-31T21:23:40",
  "execution_state": {
    "cash_balance": 101562.65,
    "account_value": 101562.65,
    "total_trades": 2,
    "num_open_positions": 0
  },
  "new_orders": [
    {
      "order_id": "d6e605da...",
      "asset": "BTC-USD",
      "side": "exit_long",
      "order_type": "take_profit",
      "status": "filled",
      "price": 77287.01,
      "position_size": 0.5,
      "position_id": "e99b2211...",
      "history": {
        "created": "2026-01-31T21:23:29",
        "activated": "2026-01-31T21:23:29",
        "filled": "2026-01-31T21:23:40",
        "cancelled": null
      }
    }
  ],
  "execution_time_seconds": 2.81
}
```

---

### 6.2 Evaluate Strategy (by Object)

**Source:** MangroveAI
**Endpoint:** `POST /api/v1/execution/evaluate`
**Auth:** Bearer token

Evaluate using an inline strategy object instead of a saved ID. Useful for testing draft strategies.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `strategy` | object | yes | Full strategy object with `asset`, `rules`, `execution_config`, `execution_state` |
| `persist` | bool | no | Persist results (default: false for object-based) |

---

### 6.3 Bulk Evaluate

**Source:** MangroveAI
**Endpoint:** `POST /api/v1/execution/evaluate/bulk`
**Auth:** Bearer token

Evaluate multiple strategies at the current bar. Market data fetched once per unique `(asset, timeframe)` pair.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `strategy_ids` | array | conditional | DB strategy UUIDs |
| `strategy_configs` | array | conditional | Inline strategy objects |
| `persist` | bool | no | Persist results (default: false) |

Both `strategy_ids` and `strategy_configs` may be provided in the same request.

---

## 7. Signals

### 7.1 List Signals

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/signals`
**Auth:** Bearer token

List all available trading signals. 96 active signals (34 TRIGGER, 62 FILTER) across momentum, trend, volume, volatility categories.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `limit` | int (query) | no | Max results (default: 50, max: 100) |
| `offset` | int (query) | no | Pagination offset (default: 0) |

---

### 7.2 Get Signal Details

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/signals/{signal_name}`
**Auth:** Bearer token

Get full metadata for a signal including parameter specs (type, min, max, default).

**Response:**
```json
{
  "name": "rsi_oversold",
  "category": "momentum",
  "metadata": {
    "rule_name": "rsi_oversold",
    "description": "RSI is below threshold (oversold condition)",
    "requires": ["Close"],
    "params": {
      "window": {"type": "int", "min": 2, "max": 100, "default": 14},
      "threshold": {"type": "float", "min": 0.0, "max": 50.0, "default": 30.0}
    }
  }
}
```

---

### 7.3 Search Signals

**Source:** MangroveAI
**Endpoint:** `POST /api/v1/signals/search`
**Auth:** Bearer token

Search signals by name, parameters, or keywords.

**Body:**
```json
{
  "query": "sma",
  "search_type": "name",
  "limit": 50,
  "offset": 0
}
```

**Search types:** `"name"`, `"params"`, `"keywords"`

---

# Extended Endpoints

These endpoints provide advanced capabilities beyond the core trading loop.

---

## 8. Crypto Asset Intelligence

### 8.1 Query Assets

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/query`
**Auth:** Bearer token

Query approved assets with risk scores. Optimized for frontend dropdowns and asset selection.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `approved_only` | bool | no | Filter to approved only (default: true) |
| `min_score` | int | no | Minimum risk score (0-100) |
| `limit` | int | no | Max results (default: 250) |

---

### 8.2 List All Assets

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/all`
**Auth:** Bearer token

All crypto assets with risk scores, approval status, and metadata.

**Parameters:** `approved` (bool), `min_score` (float), `limit` (int, max 500)

---

### 8.3 Get Asset Details

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/symbols/{symbol}`
**Auth:** Bearer token

Comprehensive details: risk scores (6-factor), approval status, supply data.

---

### 8.4 List Approved Assets

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/symbols`
**Auth:** Bearer token

Only assets that pass all quality gates. These are cleared for trading.

---

### 8.5 List Exchanges

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/exchanges`
**Auth:** Bearer token

All exchanges with tier classification (1, 2, 3), type, volume, regulation status.

---

### 8.6 Get Asset Exchanges

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/symbols/{symbol}/exchanges`
**Auth:** Bearer token

Which exchanges list a specific asset and with what trading pairs.

---

### 8.7 Trending Assets

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/trending`
**Auth:** Bearer token

Top trending assets by 24h search volume (CoinGecko).

---

### 8.8 Global Market Data

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/global-market`
**Auth:** Bearer token

Total market cap, BTC dominance, ETH dominance, 24h change. Useful for market regime detection.

---

## 9. On-Chain Analytics (Nansen)

### 9.1 Token Holder Distribution

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/token-holders/{symbol}`
**Auth:** Bearer token

Smart money count, whale count, top-10 concentration %. Use sparingly -- expensive API calls.

---

### 9.2 Flow Intelligence (Per-Token)

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/flow-intelligence/{symbol}`
**Auth:** Bearer token

Real-time capital flows by wallet type (smart traders, whales, exchanges, fresh wallets).

**Parameters:** `chain` (default: `"ethereum"`), `timeframe` (`"5m"`, `"1h"`, `"6h"`, `"12h"`, `"1d"`, `"7d"`)

**Interpretation:**
- Positive = accumulation (buying/withdrawing from CEX)
- Negative = distribution (selling/depositing to CEX)

---

### 9.3 Smart Money Netflows (Market-Wide)

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/smart-money/netflows`
**Auth:** Bearer token

Aggregated netflows across ALL tokens. Find what smart money is buying/selling.

**Parameters:** `chains`, `labels`, `timeframe` (`"1h"`, `"24h"`, `"7d"`, `"30d"`), `limit`

---

### 9.4 Smart Money Holdings (Market-Wide)

**Source:** MangroveAI
**Endpoint:** `GET /api/v1/crypto-assets/smart-money/holdings`
**Auth:** Bearer token

What smart money is holding RIGHT NOW. Position sizes, 24h changes, holder counts.

**Parameters:** `chains`, `labels`, `min_value` (default: $1M), `limit`

---

## 10. 1inch Portfolio & Analytics

### 10.1 Portfolio Value

**Source:** MangroveMarkets MCP Server
**Tool:** `oneinch_portfolio_value`

Total portfolio value across chains. **Params:** `addresses` (comma-separated wallets), `chain_id` (optional filter).

### 10.2 Portfolio P&L

**Tool:** `oneinch_portfolio_pnl`

Profit and loss for a portfolio. Same params as portfolio value.

### 10.3 Portfolio Tokens

**Tool:** `oneinch_portfolio_tokens`

ERC-20 token holdings detail. Same params.

### 10.4 Portfolio DeFi

**Tool:** `oneinch_portfolio_defi`

DeFi protocol positions. Same params.

### 10.5 Chart Data

**Tool:** `oneinch_chart`

OHLCV candle data for a token pair on-chain. **Params:** `chain_id`, `token0`, `token1`, `period` (`"5m"`, `"15m"`, `"1h"`, `"4h"`, `"1d"`, `"1w"`).

### 10.6 Transaction History

**Tool:** `oneinch_history`

Wallet transaction history across all supported chains. **Params:** `address`, `limit` (default: 50).

---

## 11. 1inch Token & Gas

### 11.1 Token Search

**Tool:** `oneinch_token_search`

Search tokens by name/symbol. **Params:** `chain_id`, `query`.

### 11.2 Token Info

**Tool:** `oneinch_token_info`

Detailed token info by contract address. **Params:** `chain_id`, `address`.

### 11.3 Spot Price

**Tool:** `oneinch_spot_price`

USD spot prices. **Params:** `chain_id`, `tokens` (comma-separated addresses).

### 11.4 Gas Price

**Tool:** `oneinch_gas_price`

Current gas prices (low/medium/high). **Params:** `chain_id`.

### 11.5 Token Allowances

**Tool:** `oneinch_allowances`

Check token allowances for a spender. **Params:** `chain_id`, `wallet`, `spender`.

---

## 12. Marketplace

### 12.1 Create Listing

**Source:** MangroveMarkets MCP Server
**Tool:** `marketplace_create_listing`
**Access:** Free

Create a peer-to-peer listing. Categories: `data`, `compute`, `intelligence`, `models`, `apis`, `storage`, `identity`, `media`, `code`, `other`.

### 12.2 Search Marketplace

**Tool:** `marketplace_search`
**Access:** x402 ($0.01)

Full-text search with filters (category, price range, listing type).

### 12.3 Get Listing

**Tool:** `marketplace_get_listing`
**Access:** x402 ($0.01)

Full details of a specific listing.

### 12.4 Settlement Flow

| Tool | Description | Status |
|------|-------------|--------|
| `marketplace_make_offer` | Make offer on listing | NOT_IMPLEMENTED (Phase 2) |
| `marketplace_accept_offer` | Accept offer (creates XRPL escrow) | NOT_IMPLEMENTED (Phase 2) |
| `marketplace_confirm_delivery` | Confirm delivery (releases escrow) | NOT_IMPLEMENTED (Phase 2) |
| `marketplace_rate` | Rate completed transaction | NOT_IMPLEMENTED (Phase 2) |

---

## 13. Integrations

| Tool | Description | Status |
|------|-------------|--------|
| `integration_akash_deploy` | Deploy compute on Akash Network | NOT_IMPLEMENTED (Phase 7) |
| `integration_bittensor_query` | Query Bittensor subnets | NOT_IMPLEMENTED (Phase 7) |
| `integration_fetch_discover` | Discover Fetch.ai agents/services | NOT_IMPLEMENTED (Phase 7) |
| `integration_nodes_status` | Check Nodes.ai distributed infra | NOT_IMPLEMENTED (Phase 7) |

---

## 14. Metrics

| Tool | Description | Status |
|------|-------------|--------|
| `metrics_market_overview` | Overall marketplace statistics | NOT_IMPLEMENTED (Phase 6) |
| `metrics_category_trends` | Demand/supply trends by category | NOT_IMPLEMENTED (Phase 6) |
| `metrics_price_history` | Historical price data by category | NOT_IMPLEMENTED (Phase 6) |

---

## 15. Knowledge Base

### 15.1 Search

**Source:** MangroveKnowledgeBase
**Tool:** `kb_search`

Full-text search with Porter stemming, synonym expansion, tag filtering across 11 trading docs.

### 15.2 List Documents

**Tool:** `kb_list_documents`

All documents with summaries.

### 15.3 Get Document

**Tool:** `kb_get_document`

Full document content by slug.

### 15.4 Get Document Sections

**Tool:** `kb_get_document_sections`

Hierarchical section structure of a document.

### 15.5 Get Backlinks

**Tool:** `kb_get_backlinks`

Find all documents referencing a specific anchor.

### 15.6 List Glossary

**Tool:** `kb_list_glossary`

All glossary entries with definitions.

### 15.7 Glossary Lookup

**Tool:** `kb_glossary_lookup`

Look up a specific term with definition and backlinks.

### 15.8 List Tags

**Tool:** `kb_list_tags`

All tags with document counts.

### 15.9 Get Documents by Tag

**Tool:** `kb_get_documents_by_tag`

Documents associated with a specific tag.

### 15.10 List Signals (KB)

**Tool:** `kb_list_signals`

Available signals with category/type filtering. Mirrors MangroveAI signals from the knowledge base perspective.

### 15.11 Get Signal (KB)

**Tool:** `kb_get_signal`

Full signal metadata from the knowledge base.

### 15.12 List Indicators

**Tool:** `kb_list_indicators`

70 technical indicators across momentum, trend, volume, volatility, patterns, returns.

### 15.13 Get Indicator

**Tool:** `kb_get_indicator`

Full indicator spec: data requirements, parameters, outputs.

### 15.14 Evaluate Signal

**Tool:** `evaluate_signal`
**Access:** x402 (payment-gated)

Evaluate a trading signal against OHLCV data.

### 15.15 Compute Indicator

**Tool:** `compute_indicator`
**Access:** x402 (payment-gated)

Compute a technical indicator from market data.

---

## 16. AI Copilot

### 16.1 Start Conversation

**Source:** MangroveAI
**Endpoint:** `POST /api/v1/ai-copilot/start_new_conversation`
**Auth:** Bearer token

Initialize a new chat session for AI-assisted strategy building.

### 16.2 Chat

**Endpoint:** `POST /api/v1/ai-copilot/chat/{session_id}`

Send message to AI agent. Handles signal matching, parameter selection, backtesting automatically.

### 16.3 List Conversations

**Endpoint:** `GET /api/v1/ai-copilot/list_conversations`

### 16.4 Get Conversation

**Endpoint:** `GET /api/v1/ai-copilot/conversations/{session_id}`

### 16.5 Get Latest Conversation

**Endpoint:** `GET /api/v1/ai-copilot/get_latest_conversation`

### 16.6 Update Conversation

**Endpoint:** `PUT /api/v1/ai-copilot/conversations/{session_id}`

Update conversation title.

### 16.7 Delete Conversation

**Endpoint:** `DELETE /api/v1/ai-copilot/conversations/{session_id}`

### 16.8 Save Strategy

**Endpoint:** `POST /api/v1/ai-copilot/save_strategy`

Persist a generated strategy to the database.

### 16.9 Configuration

**Endpoint:** `GET /api/v1/ai-copilot/configuration`

List available context, agentic, and prompt files.

---

## 17. Authentication

### 17.1 Firebase Login

**Source:** MangroveAI
**Endpoint:** `POST /api/v1/auth/login`

Exchange Firebase ID token for internal JWT tokens.

### 17.2 Google OAuth

**Endpoint:** `POST /api/v1/auth/login/google`

Initiate Google OAuth login flow.

### 17.3 Refresh Token

**Endpoint:** `POST /api/v1/auth/refresh`

Refresh access token using refresh token.

### 17.4 Get Profile

**Endpoint:** `GET /api/v1/auth/profile`

### 17.5 Update Profile

**Endpoint:** `PUT /api/v1/auth/profile`

### 15.6 API Keys

- `GET /api/v1/auth/api-keys` -- List all API keys (masked)
- `POST /api/v1/auth/api-keys` -- Generate new key with optional scopes/expiration
- `DELETE /api/v1/auth/api-keys/{key_id}` -- Revoke key

---

## x402 Payment Protocol

Tools that require payment use the x402 protocol:

1. Agent calls a payable tool
2. Server returns `payment_required` with `accepts` array
3. Agent picks chain/asset, signs payment authorization locally
4. Agent retries with `payment_proof` parameter
5. Server verifies, executes, settles payment
6. Agent receives result + `settlement_receipt`

**Supported Facilitators:**

| Network | Facilitator | URL |
|---------|-------------|-----|
| Base (EVM) | Coinbase | https://x402.org/facilitator |
| XRPL | t54.ai | https://t54.ai/facilitator |
| Solana | Coinbase | https://x402.org/facilitator |

---

## Supported EVM Chains (1inch)

| Chain ID | Name |
|----------|------|
| 1 | Ethereum |
| 8453 | Base |
| 42161 | Arbitrum |
| 137 | Polygon |
| 10 | Optimism |
| 56 | BNB Chain |
| 43114 | Avalanche |
| 324 | zkSync Era |
| 100 | Gnosis |
| 59144 | Linea |

---

## Error Format

All MCP Server errors follow:
```json
{
  "error": true,
  "code": "QUOTE_NOT_FOUND",
  "message": "Quote abc123 not found or expired",
  "suggestion": "Call dex_get_quote first to obtain a fresh quote"
}
```

All MangroveAI errors follow:
```json
{
  "error": "VALIDATION_ERROR",
  "message": "Missing required fields: name, asset",
  "code": "MISSING_FIELD"
}
```

---

## Endpoint Summary

### Core (39 endpoints)

| # | Area | Count | Endpoints | Status |
|---|------|-------|-----------|--------|
| 1 | Wallet | 5 | chain_info, create, balance, transactions, balances (1inch) | 2 NOT_IMPLEMENTED |
| 2 | DEX | 7 | venues, pairs, quote, approve, prepare_swap, broadcast, tx_status | All live |
| 3 | Market Data | 2 | OHLCV, real-time market data | All live |
| 4 | Strategies | 5 | list, create, get, update status, delete | All live |
| 5 | Backtesting | 4 | run, get result, get trades, bulk | All live |
| 6 | Execution | 3 | evaluate by ID, evaluate by object, bulk evaluate | All live |
| 7 | Signals | 3 | list, get details, search | All live |

### Extended (80+ endpoints)

| # | Area | Count | Endpoints | Status |
|---|------|-------|-----------|--------|
| 8 | Crypto Assets | 8 | query, list all, details, approved, exchanges, asset exchanges, trending, global market | All live |
| 9 | On-Chain Analytics | 4 | holders, flow intelligence, netflows, holdings | All live |
| 10 | 1inch Portfolio | 6 | value, P&L, tokens, DeFi, chart, history | All live |
| 11 | 1inch Token/Gas | 5 | search, info, spot price, gas price, allowances | All live |
| 12 | Marketplace | 7 | create, search, get, make offer, accept, confirm, rate | 4 NOT_IMPLEMENTED (Phase 2) |
| 13 | Integrations | 4 | akash, bittensor, fetch, nodes | All NOT_IMPLEMENTED (Phase 7) |
| 14 | Metrics | 3 | market overview, category trends, price history | All NOT_IMPLEMENTED (Phase 6) |
| 15 | Knowledge Base | 15 | search, docs, glossary, tags, signals, indicators, compute | All live |
| 16 | AI Copilot | 9 | start, chat, list, get, latest, update, delete, save, config | All live |
| 17 | Auth | 7 | login, OAuth, refresh, profile, API keys | All live |
