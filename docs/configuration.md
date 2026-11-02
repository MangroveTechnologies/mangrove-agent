# Configuration guide

This guide explains how the agent loads its configuration, which keys exist, how the outbound MangroveAI connection is configured, and how to keep secrets out of config files.

## How configuration works

The agent loads one JSON file, chosen by the `ENVIRONMENT` environment variable (`APP_ENV` is accepted as a fallback):

```
ENVIRONMENT=local  -> server/src/config/local-config.json
ENVIRONMENT=test   -> server/src/config/test-config.json
ENVIRONMENT=dev    -> server/src/config/dev-config.json
ENVIRONMENT=prod   -> server/src/config/prod-config.json
```

If neither variable is set, or the file is missing, the agent exits at startup. `setup.sh` runs the agent with `ENVIRONMENT=local`.

`local-config.json` is gitignored and holds your credentials. `./scripts/setup.sh` creates it from `server/src/config/local-example-config.json` on the first run, generates a unique local credential for `API_KEYS`, and rewrites it atomically with owner-only permissions (mode 0600) on later runs. You rarely need to create it by hand. If you do, copy the example from the repository root:

```bash
cp server/src/config/local-example-config.json server/src/config/local-config.json
```

The config file is the single source of truth. To change a value, edit `local-config.json` and restart the agent; rerunning `./scripts/setup.sh --yes` restarts a managed bare-metal agent when its configuration changed. Do not set `MANGROVE_API_KEY` in the agent's process environment or use a `.env` file for keyless (x402) operation; see [MangroveAI authentication](#mangroveai-authentication).

### Plugin installs: `MANGROVE_AGENT_HOME`

When the agent runs as the Claude Code plugin, its code lives in a versioned cache directory that Claude Code replaces on every update. Config and state therefore live under `MANGROVE_AGENT_HOME` (default `~/.mangrove-agent`, set by `scripts/plugin-start-agent.sh`):

```
$MANGROVE_AGENT_HOME/config/local-config.json   # read instead of server/src/config/local-config.json
$MANGROVE_AGENT_HOME/agent-data/agent.db         # a relative DB_PATH resolves here
$MANGROVE_AGENT_HOME/agent-data/master.key       # a relative MASTER_KEY_PATH resolves here
```

With `MANGROVE_AGENT_HOME` set, the loader reads `<environment>-config.json` from `$MANGROVE_AGENT_HOME/config/` and resolves relative `DB_PATH` and `MASTER_KEY_PATH` against it instead of the process working directory. `configuration-keys.json` and `mangrove-endpoints.json` always come from the package. With it unset (a git clone), nothing changes. The plugin seeds its config with your MangroveAI API key as both the upstream key and the local `API_KEYS` credential. The helper scripts (`reveal-secret.sh`, `stash-secret.sh`, `confirm-backup.sh`, `stash-kraken-secret.sh`) find the right config through `scripts/_agent_home.sh`.

## Value resolution and secret references

A config value is either a plain value or a reference to GCP Secret Manager. A string that starts with `secret:` is resolved at startup:

```
"DB_PATH": "./agent-data/agent.db"                          -> plain value
"API_KEYS": "secret:app-config-dev:api_keys"                -> fetched from Secret Manager
```

The format is `secret:<secret-name>:<property>`. The loader fetches the latest version of the named secret in the project given by the `GCP_PROJECT_ID` environment variable, parses it as a JSON object and returns the value of `<property>`. Store one secret per environment holding all sensitive values as one JSON object, and let the config file reference its keys:

```json
{
  "API_KEYS": "secret:app-config-dev:api_keys",
  "X402_CDP_API_KEY_ID": "secret:app-config-dev:cdp_api_key_id",
  "X402_CDP_API_KEY_SECRET": "secret:app-config-dev:cdp_api_key_secret"
}
```

To create and update such a secret with `gcloud`:

```bash
gcloud secrets create app-config-dev --project=YOUR_GCP_PROJECT
echo '{"api_keys": "key1,key2", "cdp_api_key_id": "...", "cdp_api_key_secret": "..."}' | \
  gcloud secrets versions add app-config-dev --data-file=- --project=YOUR_GCP_PROJECT
```

The account the agent runs as needs the `roles/secretmanager.secretAccessor` role. A malformed reference, a missing secret or a missing property stops startup with a message naming the key. The `dev` and `prod` config files in the repository use this mechanism; the `local` and `test` files use plain values. `test-config.json` is committed and contains no secrets; tests run with `ENVIRONMENT=test`.

## Key categories

The keys the loader knows about are listed in `server/src/config/configuration-keys.json`.

### Required keys

These must be present in the config file, or the agent fails at startup.

| Key | Description |
|:----|:-----------|
| `AUTH_ENABLED` | Enables authentication of the agent's local surface (`true` or `false`). Keep it `true`: setup refuses to run without it. |
| `API_KEYS` | Comma-separated local credentials for the AI client's `X-API-Key` header. Setup generates a unique value; the published defaults `dev-key-1` and `GENERATED_BY_SETUP` are rejected. |
| `MANGROVEMARKETS_BASE_URL` | The MangroveMarkets server used for DEX routing and read tools. The example ships `http://localhost:9081`; setup replaces that with the hosted default `https://mangrovemarkets-pcqgpciucq-uc.a.run.app`, or with the value of `--markets-url`. |
| `DB_PATH` | The SQLite database file (`./agent-data/agent.db`). |
| `MASTER_KEY_PATH` | The Fernet master keyfile that encrypts wallet secrets (`./agent-data/master.key`). |
| `KEYRING_SERVICE_NAME` | The OS keychain service name, used only when no keyfile exists. |
| `MANGROVE_SDK_TIMEOUT_SECONDS` | Request timeout for the MangroveAI client, in seconds (`180`). Long backtests need a generous value. |
| `LOG_RETENTION_DAYS` | Log retention in days. The loader requires it; the agent does not currently act on the value. |
| `SECRET_VAULT_TTL_SECONDS` | How long a stashed or newly created wallet secret stays in the in-memory vault, in seconds (`300`). |
| `X402_FACILITATOR_URL` | The x402 facilitator for the agent's `hello_mangrove` demo receiver. |
| `X402_NETWORK` | The payment network in CAIP-2 form, for example `eip155:8453` (Base mainnet) or `eip155:84532` (Base Sepolia). |
| `X402_PAY_TO` | The address that receives `hello_mangrove` demo payments. |
| `X402_USDC_CONTRACT` | The USDC contract address on the selected network. |
| `X402_HELLO_MANGROVE_PRICE` | The demo price in USDC base units (6 decimals). The demo currently advertises a fixed $0.05 USDC. |
| `X402_CDP_API_KEY_ID`, `X402_CDP_API_KEY_SECRET` | Coinbase Developer Platform credentials for the demo receiver's facilitator. Use empty strings when you have none. |

### Full-app keys

These are validated only when present. If a key is present but empty, startup fails; if it is absent, the feature uses its default.

| Key | Description |
|:----|:-----------|
| `X402_SPEND_CAP_USD` | Cumulative budget for outbound x402 payments, in dollars (default `25`). It bounds total volume, not the price of one call. Once spent, payments stop until a human authorizes a new budget; raising this value does not refill a budget that is already spent. See [the spend budget](setup-x402.md#the-spend-budget). |
| `PORTFOLIO_MAX_DRAWDOWN_PCT` | Aggregate drawdown at which the portfolio kill switch trips, as a fraction (default `0.3`). |

### Optional keys

These resolve normally when present and are `None` otherwise. The code that uses them validates them when the feature runs.

| Key | Description |
|:----|:-----------|
| `MANGROVE_API_KEY` | Your MangroveAI API key. Supports secret references. |
| `MANGROVE_ACCESS_MODE` | `api-key` or `x402`. See [MangroveAI authentication](#mangroveai-authentication). |
| `MANGROVEAI_BASE_URL` | Core API URL for API-key mode. See [local backend with API-key access](#local-backend-with-api-key-access). |
| `X402_MANGROVE_ENVIRONMENT`, `X402_MANGROVE_BASE_URL`, `X402_MANGROVE_KB_BASE_URL` | Developer overrides for wallet mode. See [advanced service overrides](#advanced-service-overrides). |
| `X402_RECONCILIATION_RPC_URLS` | Read-only RPC endpoints, by network ID, for payment reconciliation. Setup fills in the reviewed endpoint for the selected network. See [payment recovery](setup-x402.md#automatic-payment-recovery). |
| `MARKETPLACE_OWNERSHIP_AUDIENCE`, `MARKETPLACE_CHAIN_ID`, `MARKETPLACE_XRPL_NETWORK` | Pin the expected MangroveMarkets ownership identity and wallet networks. `MARKETPLACE_CHAIN_ID` accepts `8453` or `84532`; `MARKETPLACE_XRPL_NETWORK` accepts `mainnet`, `testnet` or `devnet`. See [marketplace wallet approvals](marketplace-wallets.md). |

Two more keys are read when present but are not in the manifest: `LOCAL_AGENT_URL` (this agent's own loopback origin, default `http://localhost:9080`, kept in sync with the port by setup) and `X402_PAYER_WALLET` (the wallet that signs outbound x402 payments, empty by default; see [selecting the payer](setup-x402.md#select-the-payer-and-review-spending)).

`LOCAL_AGENT_URL` is the agent's own surface, where wallet and secret operations live; it must be a loopback address. It is different from `MANGROVEMARKETS_BASE_URL`, the remote, keyless routing server.

## MangroveAI authentication

The agent selects upstream authentication once per process:

- `MANGROVE_ACCESS_MODE` selects `api-key` or `x402` independently of any saved key. If it is unset, a non-empty `MANGROVE_API_KEY` selects API-key mode, and an omitted, null or blank key (including the literal strings `none` and `null`) selects x402.
- Explicit `api-key` mode requires a usable key. A rejected key never falls back to spending wallet funds.
- `API_KEYS` and `AUTH_ENABLED` still protect the local agent. Keep local authentication enabled: paying MangroveAI from a wallet does not authorize access to local wallets, secrets, trading or tools.

Restart the agent after changing the mode. In x402 mode, do not set `MANGROVE_API_KEY` in the agent's process environment or in a `.env` file. The pinned SDK can inherit an environment key even when none is passed, so the payment transport refuses that request before sending it and tells you to unset the variable. SDK `.env` loading is disabled in both modes.

Ordinary MangroveAI tools use the selected mode automatically. Payments keep backup verification, the USDC and network restrictions, the cumulative spend cap and at most one signed retry per upstream request. See [setup with an API key or x402](setup-x402.md).

### Which MangroveAI services the agent uses

You do not enter service URLs. Reviewed endpoints ship in `server/src/config/mangrove-endpoints.json` and are chosen by the agent's `ENVIRONMENT`:

| Agent `ENVIRONMENT` | Default upstream services |
|:--------------------|:--------------------------|
| `local` | Hosted production API and knowledge base (normal desktop and plugin use) |
| `prod` | Hosted production API and knowledge base |
| `dev` | Hosted development API and the shared hosted knowledge base |
| `test` | Local API and knowledge base, for tests |

Running the agent locally does not mean running a backend locally. The production endpoints are `https://api.mangrovedeveloper.ai/api/v1` and `https://kb.mangrovedeveloper.ai/api`. Development uses `https://devapi.mangrove.trade/api/v1` with the same hosted knowledge base. Existing installations pick up the bundled endpoints without any change to their config.

Service selection never changes `X402_NETWORK`, the payer wallet or the spend cap. A fresh `local` install pays on Base mainnet, because the example config sets `eip155:8453`; contacting a hosted service cannot change that. A server quote for a different network is refused before signing, and testing payments on a testnet needs a receiver configured for that testnet.

### Advanced service overrides

These three settings are optional developer overrides for wallet (x402) mode. Omit them, or leave them null or blank, to use the bundled defaults. The example config leaves them out so ordinary users are not asked to configure infrastructure.

| Override | Effect |
|:---------|:-------|
| `X402_MANGROVE_ENVIRONMENT` | Select a different upstream environment, `local`, `dev` or `prod`; both URLs follow it. |
| `X402_MANGROVE_BASE_URL` | Override only the core REST API URL, including `/api/v1`. |
| `X402_MANGROVE_KB_BASE_URL` | Override only the knowledge base API URL, including its API path. |

A developer running the backend locally needs only one override:

```json
{
  "X402_MANGROVE_ENVIRONMENT": "local"
}
```

That selects `http://localhost:5001/api/v1` and `http://localhost:8080/api`. A URL override is needed only for nonstandard ports or custom servers. Remote destinations must use HTTPS; HTTP is accepted only for loopback. Malformed non-empty overrides and unknown environments fail validation instead of silently selecting another destination. Neither SDK environment variables nor `.env` files choose the x402 destinations.

### Local backend with API-key access

For a backend on a different port than the SDK default, set `"MANGROVEAI_BASE_URL": "http://127.0.0.1:5002/api/v1"` in the agent config and restart the agent. This setting controls the core API destination in API-key mode only; it does not change credentials or enable wallet payments. The URL must end in `/api/v1`, must use HTTPS (loopback HTTP is allowed), and must carry no credentials, query or fragment. Leave it absent to keep the SDK's own URL resolution. `X402_MANGROVE_BASE_URL` applies to wallet mode only, and changing the API key does not copy wallet-mode destination settings.

## Tool price discovery

The free `/api/v1/agent/tools` endpoint, the `list_tools` tool and MCP `tools/list` show wallet-payment prices for the local tools the agent can price. Prices come from MangroveAI and are not stored in the agent. Today these are the paper-schedule tools (`agent_start_paper_schedule`, `agent_get_paper_schedule`, `agent_pause_paper_schedule`, `agent_reconcile_paper_schedule`). Tools served by MangroveAI carry their own metadata from MangroveAI's catalog.

The displayed prices are prospective x402 prices, even when an API key is configured, and upstream API-key calls continue to use quota billing. The agent asks MangroveAI for an anonymous `tools/list` with pricing metadata, without an API key, wallet, signing or payment, and caches the result. Prices refresh on the next discovery request after five minutes. Concurrent requests share one refresh, and other readers get the previous snapshot or `unavailable` on a cold cache. A failed refresh is retried no more than once per 30 seconds. Last-known prices can show as `stale` for up to an hour with their fetch time, after which the price is `unavailable`. A missing price never means free. MCP clients can cache tool descriptions, so call `list_tools` again for the current snapshot. The agent does no network work for pricing during startup.

Prices are per upstream request, not guaranteed totals. Pagination, polling and multi-step workflows make several requests, so such tools are marked `variable_total` and expose component prices instead of a summed quote. The network shown comes from the receiver; a mismatch with `X402_NETWORK` is flagged, never corrected by changing payment configuration. Execution still checks the actual 402 quote against payment rules and the spend cap. Older receivers that do not supply pricing metadata keep working; the agent reports those prices as unavailable.

In wallet mode, `status` omits the SDK catalog counts (`catalog.error` is `catalog_counts_unavailable_in_payment_mode`), so checking status cannot spend money.

## Network and facilitator settings

The example config selects Base mainnet:

```json
{
  "X402_FACILITATOR_URL": "https://api.cdp.coinbase.com/platform/v2/x402",
  "X402_NETWORK": "eip155:8453",
  "X402_USDC_CONTRACT": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  "X402_CDP_API_KEY_ID": "",
  "X402_CDP_API_KEY_SECRET": ""
}
```

These `X402_*` receiver settings configure only the `hello_mangrove` demo receiver. The CDP facilitator needs `X402_CDP_API_KEY_ID` and `X402_CDP_API_KEY_SECRET` from the [Coinbase Developer Platform](https://docs.cdp.coinbase.com/) to authenticate; with them empty, the facilitator is called without credentials. Outbound payments to MangroveAI do not use them. If the facilitator is unreachable at startup, the demo is disabled with a `x402.hello_mangrove.facilitator_unavailable` warning and everything else starts normally.

For Base Sepolia testnet, as in `test-config.json`, use the free [x402.org](https://x402.org) facilitator, which needs no credentials:

```json
{
  "X402_FACILITATOR_URL": "https://x402.org/facilitator",
  "X402_NETWORK": "eip155:84532",
  "X402_USDC_CONTRACT": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
}
```

The agent's payer wallet and the receiving backend must use the same network; see [network and funding](setup-x402.md#network-and-funding).

## Runtime environment variables

The agent reads only a few environment variables. Everything else comes from the JSON config file.

| Env var | Purpose |
|:--------|:--------|
| `ENVIRONMENT` (or `APP_ENV`) | Selects which config file to load. |
| `GCP_PROJECT_ID` | The GCP project to fetch `secret:` references from. Needed only when the config has such references. |
| `MANGROVE_AGENT_HOME` | Where a plugin install keeps its config and state. |
| `BARE_PORT` | Read by `setup.sh` to choose the bare-metal port (default `9080`). |

`docker-compose.yml` sets `ENVIRONMENT=local` and mounts `server/src/config/local-config.json` read-only into the container.
