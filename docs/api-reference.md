# REST API Reference

This is the reference for the REST routes served by the local agent. The agent runs one
FastAPI process that serves REST, MCP and OpenAPI on a single port (9080 on the host when
started with Docker Compose). For the MCP surface see [MCP contracts](mcp-contracts.md) and
run the `list_tools` tool for the live catalog.

The REST API covers only what the agent owns: wallets, local execution, scheduling, venue
connections, the marketplace approval flow and the spend budget. Strategies, backtests,
signals, market data, on-chain data and the knowledge base belong to MangroveAI and are
reached through the agent's MCP endpoint, which forwards MangroveAI's tools. There is no REST
copy of those tools.

A running agent serves the machine-readable contract at `/openapi.json` and an interactive
view at `/docs`. Routes marked "hidden" below are excluded from the OpenAPI schema.

## Conventions

### Base paths

| Prefix | Contents |
|--------|----------|
| `/health` | Liveness probe |
| `/api/v1/agent/*` | Every agent route below |
| `/api/x402/*` | x402 payment-gated routes (the `hello-mangrove` demo only) |
| `/mcp` | MCP server (Streamable HTTP transport) |

All paths in the tables below are relative to `/api/v1/agent` unless they start with `/api/`
or are `/health`.

### Access tiers

| Tier | How to call |
|------|-------------|
| Free | No credentials |
| Auth | `X-API-Key` header holding one of the keys in the `API_KEYS` config value (comma-separated) |
| x402 | An x402 payment, or a valid `X-API-Key` for free access |

The key is the agent's own local key, not a MangroveAI key. When `AUTH_ENABLED` is false,
every route accepts requests without a key. The two placeholder values shipped in the example
config (`dev-key-1` and `GENERATED_BY_SETUP`) never authenticate; `scripts/setup.sh` replaces
them with a generated key.

### Errors

Domain errors return the HTTP status of the error and this body:

```json
{
  "error": true,
  "code": "WALLET_NOT_FOUND",
  "message": "Human-readable description",
  "suggestion": "What to do about it",
  "correlation_id": "uuid"
}
```

The same ID is returned in the `X-Correlation-Id` response header (a caller-supplied value is
echoed back) and written to the agent's structured logs. The full code table is in the
[specification](specification.md#error-handling). Request bodies that fail Pydantic
validation return FastAPI's standard 422 response.

### Retired routes

These routes still exist so old callers get a clear answer. They require the API key, are
hidden from OpenAPI, and always return HTTP 410:

| Method | Path | Replacement |
|--------|------|-------------|
| POST | `/strategies/autonomous` | MangroveAI `load_skill` (`strategy-composition`) and `save_strategy` |
| POST | `/strategies/manual` | MangroveAI `save_strategy` |
| GET | `/reference-strategies`, `/reference-strategies/`, `/reference-strategies/search` | MangroveAI `load_skill` (`strategy-composition`) |
| POST | `/reference-strategies/{reference_id}/build` | MangroveAI `load_skill` (`strategy-composition`) |

Routes for market data, on-chain data, signals, the knowledge base, backtests and strategy
listing do not exist and return 404.

## Discovery

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| GET | `/health` | Free | `{status, scheduler_running, timestamp}` |
| GET | `/status` | Free | Agent state (below) |
| GET | `/tools` | Free | Catalog of MCP tools: `{tools: [...], mangroveai_status}` |
| PUT | `/identity` | Auth | Save the agent name |

`GET /status` returns:

- `agent_name` (`null` until chosen), `version` and `server_version`, and installed SDK
  versions under `sdk_versions`.
- `catalog`: MangroveAI signal and knowledge-base counts, cached for 10 minutes. `catalog.error`
  is set when they are unavailable, including in wallet (x402) payment mode, where the agent
  does not spend money on a status read.
- `wallets_count`, `strategies` (counts by status) and `active_cron_jobs`.
- `portfolio_risk` (same payload as `GET /portfolio/risk`) and `x402_spend` (same payload as
  `GET /x402/spend`). Either degrades to `{"error": "unavailable"}` instead of failing the
  request.
- `db_path` and `uptime_seconds`.

`GET /tools` lists the tools served by this agent, by MangroveMarkets and by MangroveAI.
`mangroveai_status` is `unavailable` when MangroveAI's catalog cannot be read; local tools
remain available.

`PUT /identity` takes `{"name": "<1 to 64 printable characters>"}` and returns
`{"agent_name": "<name>"}`.

## Wallets

Wallet secrets never appear in a response. Creating or stashing a secret returns a
`vault_token` that refers to an in-process vault entry. The entry is single-read and expires
after `SECRET_VAULT_TTL_SECONDS` (300 by default). The reveal routes are called by the local
scripts in `scripts/`; no MCP tool calls them.

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| POST | `/wallet/create` | Auth | Create and encrypt a wallet (201) |
| POST | `/wallet/import` | Auth | Import a wallet from a stashed secret (201) |
| POST | `/wallet/stash-secret` | Auth | Stash a plaintext secret; used by `scripts/stash-secret.sh` |
| GET | `/wallet/reveal-secret/{vault_token}` | Auth | Reveal and consume a stashed secret; used by `scripts/reveal-secret.sh` |
| GET | `/wallet/{address}/reveal` | Auth | Decrypt a stored wallet's secret; used by `scripts/reveal-secret.sh --address` |
| POST | `/wallet/{address}/confirm-backup` | Auth | Record that the user backed up the secret; used by `scripts/confirm-backup.sh` |
| GET | `/wallet/list` | Auth | Stored wallets (addresses and metadata only) |
| GET | `/wallet/{address}/balances?chain_id=<int>` | Auth | Token balances (`chain_id` required) |
| GET | `/wallet/{address}/portfolio?chain_id=<int>` | Auth | Combined `value`, `pnl`, `tokens` and `defi` (`chain_id` optional) |
| GET | `/wallet/{address}/history?limit=<int>` | Auth | Transaction history (`limit` defaults to 50) |

`POST /wallet/create` body: `chain` (`evm` default, or `xrpl`), `network` (default `mainnet`;
XRPL also accepts `testnet` and `devnet`), `chain_id` (default 8453, ignored for XRPL), `label`. The response carries
`address`, `chain`, `network`, `chain_id`, `label`, `created_at`, `vault_token`, `secret_type`,
`master_key_source`, `reveal_cmd`, `secret_ttl_seconds`, `backup_required`,
`deposit_instructions` and `safety_note`. Any other chain returns 501
`CHAIN_NOT_SUPPORTED_IN_V1`.

`POST /wallet/import` body: `vault_token` plus the same `chain`, `network`, `chain_id` and
`label` fields. `POST /wallet/stash-secret` body: `secret` and optional `address_hint`; it
returns `{vault_token, secret_ttl_seconds}`.

`POST /wallet/{address}/confirm-backup` sets `backup_confirmed_at`. Until it is set for a
wallet, live swaps and live strategy activation from that wallet are refused, and so are x402
payments signed by it.

## DEX

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| GET | `/dex/venues` | Auth | Supported DEX venues |
| GET | `/dex/pairs?venue_id=<str>` | Auth | Trading pairs for a venue |
| POST | `/dex/quote` | Auth | Swap quote in human units |
| POST | `/dex/swap` | Auth | Execute a swap; requires `confirm: true` |
| GET | `/dex/tx-status?tx_hash=<str>&chain_id=<int>&venue_id=<str>` | Auth | State of a broadcast transaction |
| GET | `/dex/token-info?chain_id=<int>&address=<str>` | Auth | Token metadata |
| GET | `/dex/spot-price?chain_id=<int>&tokens=<csv>` | Auth | Spot price for comma-separated symbols or addresses |
| GET | `/dex/gas-price?chain_id=<int>` | Auth | Gas price estimate |

`venues`, `pairs`, `tx-status`, `token-info`, `spot-price` and `gas-price` pass through to the
`mangrovemarkets` SDK.

`POST /dex/quote` body: `input_token`, `output_token`, `amount` (human units, greater than 0,
for example `0.001` for 0.001 ETH), `chain_id`, optional `venue_id`. The agent converts the
amount to the token's base units for the upstream call and converts the returned
`input_amount` and `output_amount` back to human units. The raw values are kept as
`input_amount_base_units` and `output_amount_base_units`, next to `input_token_decimals` and
`output_token_decimals`.

`POST /dex/swap` body: `input_token`, `output_token`, `amount` (input amount), `chain_id`,
`wallet_address`, `slippage_pct`, `mev_protection` (default false), optional `venue_id`,
`confirm`.

- `slippage_pct` is required, a decimal fraction, greater than 0 and at most `0.0025` (0.25
  percent). There is no default.
- `confirm` must be `true`, otherwise the route returns 400 `CONFIRMATION_REQUIRED`.
- The wallet must have a confirmed backup.
- The response carries `tx_hash`, `status`, `input_token`, `input_amount`, `output_token`,
  `output_amount`, `fill_price`, `fees`, `approval_tx_hash` and `trade_log_id`.

The swap runs quote, conditional token approval, local signing, broadcast and status polling.
Signing is local; the SDK never sees a key.

## Kraken (CEX)

There are two connection modes. Bring-your-own-key mode stores an encrypted Kraken key locally
and talks to Kraken directly. Keyless OAuth mode holds no Kraken credential; the Mangrove
platform holds the grant and executes.

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| POST | `/cex/stash-kraken` | Auth | Stash `{api_key, api_secret}`; returns `{vault_token, secret_ttl_seconds}`. Used by `scripts/stash-kraken-secret.sh` |
| POST | `/cex/connect` | Auth | Connect Kraken from `{vault_token}` |
| GET | `/cex/status` | Auth | Whether a Kraken account is connected |
| GET | `/cex/balances` | Auth | Kraken balances |
| POST | `/cex/validate-order` | Auth | Dry-run an order (`pair`, `side`, `volume`, `ordertype`, `price`); no fill |
| POST | `/cex/sync-fills` | Auth | Pull fills and emit them to telemetry (`mode`, default `live`) |
| POST | `/cex/oauth/connect-start` | Auth | Begin keyless connect (`mode`: `view` or `execute`); returns an authorize URL |
| GET | `/cex/oauth/status` | Auth | Keyless connection status |
| GET | `/cex/oauth/balances` | Auth | Balances through the platform |
| POST | `/cex/oauth/orders` | Auth | Place or validate an order through the platform (`base`, `quote`, `side`, `volume`, `order_type`, `limit_price`, `validate_only`) |

Routes that need a connected account return 409 `CEX_CREDENTIALS_MISSING` when none exists.
The `/cex/oauth/*` routes have no MCP tools.

## Robinhood execution venue

See [Robinhood execution](robinhood-execution.md) for setup and scope.

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| GET | `/brokers/robinhood/status` | Auth | Connection, masked account, buying power and trade-approval setting; places no orders |
| PUT | `/brokers/strategies/{strategy_id}/venue` | Auth | Bind an inactive strategy to Robinhood and a capital mandate |
| GET | `/brokers/strategies/{strategy_id}/orders` | Auth | Local order journal (account IDs masked) |
| POST | `/brokers/strategies/{strategy_id}/reconcile` | Auth | Read broker outcomes and persist confirmed fills; never submits an order |
| POST | `/brokers/orders/{order_id}/cancel` | Auth | Request cancellation of a pending venue order and reconcile |

`PUT .../venue` body (unknown fields rejected): `venue` (`robinhood`), `asset_class` (`equity`
or `crypto`), `capital`, `max_order_usd` (not above `capital`), `slippage_pct` (greater than 0,
at most `0.0025`). The strategy must be `draft` or `inactive` and have no execution history.
The route does not activate trading.

## Marketplace approvals

Marketplace actions that sign or pay use a two-step local approval: the agent shows an exact
preview first and signs only after explicit confirmation. See
[Marketplace wallets](marketplace-wallets.md).

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| GET | `/marketplace/readiness` | Auth | Check ownership discovery without signing or creating an approval |
| POST | `/marketplace/prepare` | Auth | Body `{operation, arguments, wallet_address}`; returns an exact action preview and an `approval_id`; signs nothing |
| POST | `/marketplace/submit` | Auth | Body `{approval_id, confirm}`; signs and submits only the prepared action, once |

Both bodies reject unknown fields. `confirm` must be `true` to submit. A prepared approval
expires at the time in its `expires_at` field.

## Strategy execution records

These routes operate on local execution records, identified by the agent's local strategy UUID.
That ID is not a MangroveAI saved-strategy ID. The agent's API does not create local records.

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| PATCH | `/strategies/{strategy_id}/status` | Auth | Lifecycle transition; the only way to activate, deactivate or archive |
| PATCH | `/strategies/{strategy_id}/market` | Auth | Set explicit market data for an inactive stateless-lane strategy |
| POST | `/strategies/{strategy_id}/evaluate` | Auth | Run one evaluation tick now, through the same code path as the scheduler |

`PATCH .../status` body: `status` (`draft`, `inactive`, `paper`, `live`, `archived`), `confirm`
(default false), and `allocation` (`wallet_address`, `token`, `token_address`, `amount`,
`slippage_pct`).

- Allowed transitions: `draft` to `inactive` or `archived`; `inactive` to `paper`, `live` or
  `archived`; `paper` to `live`, `inactive` or `archived`; `live` to `inactive` or `archived`.
  `archived` is final. A transition to the current status is a no-op.
- Moving to `live`, or leaving `live`, requires `confirm: true`.
- Moving to `live` also requires an `allocation` block and a wallet with a confirmed backup.
  `slippage_pct` is required in the block, greater than 0 and at most `0.0025`.
- A strategy bound to a Robinhood venue takes no `allocation` and cannot move to `paper`.
- `paper` and `live` register a scheduler job on the strategy's timeframe. Leaving them
  cancels it. Leaving `live` releases the allocation.
- The response is the strategy record: `id`, `mangrove_id`, `name`, `asset`, `timeframe`,
  `status`, `entry`, `exit`, `execution_config`, `market_context`, `execution_venue`,
  `generation_report`, `created_at`, `updated_at`.

`PATCH .../market` body (unknown fields rejected): `base_token`, `quote_token`,
`market_data_venue`, each made of uppercase letters, digits, `.`, `_` or `-`. The strategy must
be `draft` or `inactive`, on the stateless evaluation lane, with no trades, broker orders or
positions. The base token must match the strategy asset.

`POST .../evaluate` returns `{strategy_id, evaluation_id, status, order_count, duration_ms,
error_msg}`, or `{strategy_id, status: "no_evaluation_recorded"}`. An unknown ID returns 404
`STRATEGY_NOT_FOUND`.

### Paper schedules

The agent supplies the clock for server-owned paper execution of a MangroveAI strategy. See
[Paper scheduling](paper-scheduling.md). Schedules are scoped to the current upstream
identity. In these routes `strategy_id` is the MangroveAI strategy UUID, not a local record ID.

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| POST | `/strategies/paper-schedules` | Auth | Start a bounded schedule |
| GET | `/strategies/paper-schedules` | Auth | List schedules with cached progress; no upstream call |
| GET | `/strategies/paper-schedules/{schedule_id}?refresh=<bool>` | Auth | Read a schedule; `refresh=true` fetches server state and may incur API or x402 usage |
| POST | `/strategies/paper-schedules/{schedule_id}/pause` | Auth | Stop new local requests and pause on the server; does not close positions |
| POST | `/strategies/paper-schedules/{schedule_id}/reconcile` | Auth | Recover an unresolved evaluation with its original identity |

`POST` body (unknown fields rejected): `strategy_id` (UUID), `interval_seconds` (one of 60, 300,
900, 3600, 14400, 86400), `max_evaluations` (1 to 10000), `confirm` (must be `true` to accept
the recurring API and x402 cost).

## Execution logs

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| GET | `/strategies/{strategy_id}/evaluations?limit=<int>&offset=<int>` | Auth | Evaluation log, newest first (limit defaults to 50) |
| GET | `/strategies/{strategy_id}/trades?limit=<int>&offset=<int>` | Auth | Trades for a strategy, newest first |
| GET | `/trades?limit=<int>&strategy_id=<str>&mode=<live\|paper>` | Auth | Trades across strategies, optionally filtered |

These read the agent's local SQLite records. Server-side trade and evaluation history for
MangroveAI strategies is available through MangroveAI's MCP tools.

## Portfolio kill switch

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| GET | `/portfolio/risk` | Auth | State of the portfolio kill switch |
| POST | `/portfolio/risk/reset` | Auth | Clear a tripped switch; human re-activation |

The state carries `tripped`, `tripped_at`, `tripped_reason`, `high_water_mark`, `book_value`,
`drawdown` and `max_drawdown_limit`. See
[Portfolio kill switch](specification.md#portfolio-kill-switch).

## x402 spend budget

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| GET | `/x402/spend` | Auth | Budget state |
| GET | `/x402/spend/payments?limit=<int>&period_id=<int>` | Auth | Payment ledger, newest first (limit 1 to 500, default 50) |
| POST | `/x402/spend/reset` | Auth | Start a fresh budget period; human top-up |

The state includes `exhausted`, `cap_usd`, `cap_source` (`authorized`, `config` or `default`),
`spent_usd`, `settled_usd`, `reserved_usd`, `remaining_usd`, `payment_count`,
`unreconciled_count`, `refunds`, `pending_operations`, `unresolved_payments`, `period_id` and
`period_started_at`. The payments route returns `{payments, count}`.

`POST /x402/spend/reset` takes an optional body `{"cap_usd": <number >= 0>}`. `cap_usd`
becomes the budget a human authorized for the new period and overrides `X402_SPEND_CAP_USD`.
Omit it to restart at the current size. See
[x402 spend budget](specification.md#x402-spend-budget).

## x402-gated routes

| Method | Path | Tier | Purpose |
|--------|------|------|---------|
| GET | `/api/x402/hello-mangrove` | x402 | Demo route that returns a thank-you message |

The route costs $0.05 USDC on the configured `X402_NETWORK`. Without a valid payment or
`X-API-Key` it returns 402 with the payment requirements. It exists to exercise the payment
path end to end.
