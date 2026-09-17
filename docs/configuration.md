# Configuration Guide

This guide explains how the configuration system works, how to manage secrets across environments, and how to set up your service for production.

## How Configuration Works

The app loads configuration from a single JSON file selected by the `ENVIRONMENT` env var:

```
ENVIRONMENT=local  -> src/config/local-config.json
ENVIRONMENT=test   -> src/config/test-config.json
ENVIRONMENT=dev    -> src/config/dev-config.json
ENVIRONMENT=prod   -> src/config/prod-config.json
```

`local-config.json` is gitignored. You create it by copying an example:

```bash
cp src/config/local-example-config.json src/config/local-config.json
```

### Plugin installs: `MANGROVE_AGENT_HOME`

When the agent runs as the Claude Code plugin, its code lives in a versioned cache directory that Claude Code replaces on every update. Config and state therefore live under `MANGROVE_AGENT_HOME` (default `~/.mangrove-agent`, set by `scripts/plugin-start-agent.sh`):

```
$MANGROVE_AGENT_HOME/config/local-config.json   # read instead of src/config/<env>-config.json
$MANGROVE_AGENT_HOME/agent-data/agent.db         # relative DB_PATH resolves here
$MANGROVE_AGENT_HOME/agent-data/master.key       # relative MASTER_KEY_PATH resolves here
```

When `MANGROVE_AGENT_HOME` is set, `src/config.py` loads the config from `$MANGROVE_AGENT_HOME/config/` and resolves relative `DB_PATH` / `MASTER_KEY_PATH` against it rather than the process cwd. `configuration-keys.json` always comes from the package. Unset (a git clone), nothing changes. The helper scripts (`reveal-secret.sh`, `stash-secret.sh`, `confirm-backup.sh`, `stash-kraken-secret.sh`) find the right config through `scripts/_agent_home.sh`.

## Value Resolution

Config values are resolved from the JSON config file. If a value starts with `secret:`, it's resolved from GCP Secret Manager:

```
"DB_PASSWORD": "postgres"                          -> plain value
"DB_PASSWORD": "secret:app-config-dev:db_password"  -> fetched from Secret Manager
```

The config file is the single source of truth. To change a value, edit your `local-config.json` and restart the app.

## Key Categories

Config keys are defined in `src/config/configuration-keys.json`:

### Required Keys

Always validated at startup. The app fails if any are missing from the config file.

| Key | Description |
|:----|:-----------|
| `AUTH_ENABLED` | Enable/disable API key authentication (`true`/`false`) |
| `API_KEYS` | Comma-separated list of valid API keys |
| `X402_FACILITATOR_URL` | x402 facilitator endpoint |
| `X402_NETWORK` | Blockchain network in CAIP-2 format |
| `X402_PAY_TO` | Address that receives x402 payments |
| `X402_USDC_CONTRACT` | USDC token contract address |
| `X402_HELLO_MANGROVE_PRICE` | hello_mangrove price in USDC base units (6 decimals) |
| `X402_CDP_API_KEY_ID` | CDP API key ID (empty string if not using CDP) |
| `X402_CDP_API_KEY_SECRET` | CDP API secret (empty string if not using CDP) |
| `X402_SPEND_CAP_USD` | Cumulative budget for OUTBOUND x402 payments, in dollars (default `25`). Bounds volume, not price — the per-payment ceiling is separate and pinned in source. Once spent, payments stop until a human authorizes a new budget; raising this key does not refill an already-spent one. |

### Full App Keys

Validated only if present in your config file. If a key is present but has an empty value, startup fails -- this catches misconfiguration. If the key is absent entirely, the app runs without that feature.

| Key | Description |
|:----|:-----------|
| `DB_HOST` | PostgreSQL host (or Cloud SQL Unix socket path) |
| `DB_NAME` | Database name |
| `DB_USER` | Database user |
| `DB_PASSWORD` | Database password |
| `DB_PORT` | Database port |
| `DB_SSLMODE` | SSL mode (`disable`, `require`) |
| `CLOUD_SQL_CONNECTION_NAME` | Cloud SQL instance connection name |
| `REDIS_URL` | Redis connection URL |

## Environments

### Outbound MangroveAI authentication (B5)

The agent selects upstream authentication once per process:

- A non-empty `MANGROVE_API_KEY` uses the existing SDK API-key path. A rejected
  key never falls back to spending wallet funds.
- An omitted, null or blank key selects x402. The literal strings `none` and
  `null` also mean unset, consistent with configuration normalization.
- `API_KEYS` and `AUTH_ENABLED` still protect the local agent. Keep local
  authentication enabled: an upstream wallet payment does not authorize access
  to local wallets, secrets, trading or tools.

Users do not need to enter service URLs. Reviewed endpoints ship in
`src/config/mangrove-endpoints.json` and are selected automatically:

| Agent `ENVIRONMENT` | Default upstream services |
|:--------------------|:--------------------------|
| `local` | Hosted production API and Knowledge Base (normal desktop/plugin use) |
| `prod` | Hosted production API and Knowledge Base |
| `dev` | Hosted development API and shared hosted Knowledge Base |
| `test` | Local API and Knowledge Base for tests |

The agent running locally does not mean the user runs a local backend. The
production endpoints are `https://api.mangrovedeveloper.ai/api/v1` and
`https://kb.mangrovedeveloper.ai/api`. Development uses
`https://devapi.mangrove.trade/api/v1` with the same hosted KB, matching
MangroveAI's deployment configuration. Existing installations pick up the bundled
endpoints without rewriting their configuration files.

Service selection **never changes `X402_NETWORK`, the wallet or the spend cap**.
In particular, desktop installs retain their Base Sepolia default: contacting a
hosted service cannot enable mainnet spending. A server quote for another network
is refused before signing. Testnet payment testing requires a receiver configured
for testnet; selecting hosted production does not make it a testnet receiver.

The three settings below are advanced developer overrides, genuinely optional.
Omit them or leave them null/blank to use the bundled defaults. Templates omit
these settings so ordinary users are not prompted to configure infrastructure.

| Optional override | Effect |
|:------------------|:-------|
| `X402_MANGROVE_ENVIRONMENT` | Select a different upstream environment: `local`, `dev` or `prod`; both URLs follow that environment automatically. |
| `X402_MANGROVE_BASE_URL` | Override only the core REST API URL, including `/api/v1`. |
| `X402_MANGROVE_KB_BASE_URL` | Override only the Knowledge Base API URL, including its API path. |

For developers running the backend locally, only one override is needed:

```json
{
  "X402_MANGROVE_ENVIRONMENT": "local"
}
```

That selects `http://localhost:5001/api/v1` and `http://localhost:8080/api`.
A URL override is needed only for nonstandard ports or custom servers. Remote
destinations require HTTPS; HTTP is accepted only for loopback. Malformed
non-empty overrides and unknown environments fail validation instead of silently
selecting a different destination. Neither SDK environment variables nor `.env`
files choose the x402 destinations.

`MANGROVE_API_KEY` remains optional and supports Secret Manager references.
`X402_PAYER_WALLET` still identifies the locally stored, backup-confirmed payment
wallet: that is an explicit spending choice, not an infrastructure setting the
agent can infer. No wallet is selected automatically.

Restart the agent after changing configuration. Do not set `MANGROVE_API_KEY` in
the agent process environment or use a `.env` file for keyless operation. The
pinned SDK can inherit an environment key even with `api_key=None`; the payment
transport refuses that request before sending it and returns instructions to
unset the environment variable. It does not mutate the process environment or
strip credentials silently. SDK `.env` loading is disabled for both AI modes.

Ordinary MangroveAI tools then use the selected mode automatically. Payments
retain backup verification, USDC/network restrictions, the cumulative spend cap,
and at most one signed retry per upstream request. A tool may make multiple
requests (pagination or polling), each potentially billable. `list_signals` stops
after the requested count (1-1000), instead of fetching the entire catalog.
Free responses need no payment; MangroveMarkets and local-only tools do not use
the x402 transport. Existing signed or uncertain authorizations remain counted
even when an HTTP call fails; do not reset the budget to hide uncertain outcomes.

This configuration enables B5's runtime path. The plugin installation manifest
still requires a key; changing the installation flow belongs to B9.

### Tool price discovery (B6)

The free `/api/v1/agent/tools` endpoint, the `list_tools` tool and MCP
`tools/list` display wallet-payment prices fetched from MangroveAI. Prices are
not duplicated in the agent. Existing local access tiers remain unchanged;
upstream API-key calls continue to use quota billing.

Discovery uses the same destination as the wallet-payment client, including
the optional environment/URL overrides above. These are prospective **x402
prices**, even when an API key is configured. The displayed network comes from
the receiver; a mismatch with `X402_NETWORK` is flagged, never corrected by
changing payment configuration.

Prices refresh on the next discovery request after five minutes. Concurrent
requests share one refresh; other readers receive the previous snapshot or
`unavailable` on a cold cache. Failed refreshes retry no more than once per
30 seconds. Last-known prices may be shown as `stale` for up to one hour, with
their fetch timestamp; after that the price is unavailable. A missing price
never means free. MCP clients may cache their tool descriptions, so call the
`list_tools` tool again for the current snapshot.

Prices are **per upstream request**, not guaranteed totals. Pagination,
backtest polling, optional benchmarks and multi-step workflows can make several
requests. Their catalog entries mark `variable_total` and expose component
prices rather than adding them into a misleading total. KB calls target a
separate service, and some other endpoints lack published billing definitions;
these explicitly report `unavailable`. Execution still validates the actual
402 quote against payment rules and the spend cap.

Discovery uses a separate anonymous HTTP client: no API key, wallet lookup,
signing, payment retry, redirects or ambient proxies. Response size, page count,
entry count and time are bounded. It does no network work during startup.
The free status endpoint also skips SDK catalog counts in wallet-payment mode
so checking status cannot spend money.

**Receiver rollout:** deploy the companion MangroveAI pricing-metadata change
before expecting prices to appear. The agent requests anonymous `tools/list`
with `params._meta["mangrove/include_pricing"] = true`, and reads the versioned
`result._meta["mangrove/pricing"]` extension. Older receivers continue to work;
the agent reports unavailable prices without disabling tools. The receiver
reads existing REST, skill and Oracle billing definitions, including prices
for REST-only endpoints, without exposing additional callable MCP tools.
The agent's bindings contain billing identifiers only, never dollar amounts.

### Local Development

Copy the example and edit as needed:

```bash
cp src/config/local-example-config.json src/config/local-config.json
```

The example defaults to Base Sepolia testnet with the x402.org facilitator. No API keys needed for testnet.

To include PostgreSQL and Redis, use the full example instead:

```bash
cp src/config/local-full-example-config.json src/config/local-config.json
```

### Test

`test-config.json` is committed to the repo. It uses plain values (no secrets, no Secret Manager). Tests run with `ENVIRONMENT=test`.

### Dev and Prod (GCP)

Dev and prod configs use GCP Secret Manager for sensitive values.

#### Secret Reference Format

Any config value can reference a secret stored in [GCP Secret Manager](https://cloud.google.com/secret-manager/docs/overview) using this format:

```
secret:<secret-name>:<property>
```

| Part | What it is | Example |
|:-----|:-----------|:--------|
| `secret` | Prefix that tells the config loader to fetch from Secret Manager | `secret` |
| `<secret-name>` | The name of the secret in GCP Secret Manager | `app-config-dev` |
| `<property>` | The JSON key inside the secret to extract | `db_password` |

For example, this config file:

```json
{
  "DB_PASSWORD": "secret:app-config-dev:db_password",
  "API_KEYS": "secret:app-config-dev:api_keys",
  "X402_CDP_API_KEY_ID": "secret:app-config-dev:cdp_api_key_id"
}
```

At startup, the config loader:
1. Sees the `secret:` prefix on `DB_PASSWORD`
2. Calls GCP Secret Manager API to fetch the secret named `app-config-dev`
3. Parses the secret value as JSON: `{"db_password": "...", "api_keys": "...", ...}`
4. Extracts the `db_password` property and sets it as the config value

This means you store **one secret per environment** in GCP, and it contains all your sensitive values as a JSON blob. The config file just references which key to pull from that blob.

#### Setting Up GCP Secret Manager

**Prerequisites:**
- A GCP project with the [Secret Manager API enabled](https://console.cloud.google.com/apis/library/secretmanager.googleapis.com)
- The [gcloud CLI](https://cloud.google.com/sdk/docs/install) installed and authenticated
- A service account with the `roles/secretmanager.secretAccessor` role (Terraform provisions this automatically)

**1. Create the secret:**

```bash
gcloud secrets create app-config-dev --project=YOUR_GCP_PROJECT
```

**2. Add your sensitive values as a JSON blob:**

```bash
echo '{
  "db_password": "your-database-password",
  "api_keys": "key1,key2,key3",
  "redis_url": "redis://your-redis:6379/0",
  "cdp_api_key_id": "your-cdp-key-id",
  "cdp_api_key_secret": "your-cdp-key-secret"
}' | gcloud secrets versions add app-config-dev --data-file=- --project=YOUR_GCP_PROJECT
```

**3. Update a secret** (creates a new version):

```bash
echo '{ ... updated values ... }' | \
  gcloud secrets versions add app-config-dev --data-file=- --project=YOUR_GCP_PROJECT
```

The config loader always fetches the `latest` version.

**Learn more:**
- [Secret Manager overview](https://cloud.google.com/secret-manager/docs/overview)
- [Creating and accessing secrets](https://cloud.google.com/secret-manager/docs/creating-and-accessing-secrets)
- [Managing secret versions](https://cloud.google.com/secret-manager/docs/add-secret-version)
- [IAM roles for Secret Manager](https://cloud.google.com/secret-manager/docs/access-control)

#### Runtime Environment Variables

The app itself only needs two env vars (set by Cloud Run, docker-compose, etc.):

| Env var | Purpose | Example |
|:--------|:--------|:--------|
| `ENVIRONMENT` | Selects which config file to load | `dev`, `prod` |
| `GCP_PROJECT_ID` | Tells the config loader which GCP project to fetch secrets from | `my-gcp-project` |

These are set in the Cloud Run deploy command (see `.github/workflows/deploy-cloudrun.yaml`) and in `docker-compose.yml` for local development. All other configuration comes from the JSON config file.

## AWS Support

AWS Secrets Manager support is planned. The same `secret:name:property` syntax will work -- the config loader will detect the cloud provider based on available credentials.

Until then, for AWS deployments, use a secrets management tool that populates a config JSON file before the app starts (e.g., inject values into the config file at container startup).

## x402 Configuration

### Testnet (default)

The example config defaults to Base Sepolia testnet:

```json
{
  "X402_FACILITATOR_URL": "https://x402.org/facilitator",
  "X402_NETWORK": "eip155:84532",
  "X402_USDC_CONTRACT": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
}
```

No CDP API keys needed. The x402.org facilitator is free and doesn't require authentication.

### Mainnet

To accept real payments on Base mainnet, update your `local-config.json`:

```json
{
  "X402_FACILITATOR_URL": "https://api.cdp.coinbase.com/platform/v2/x402",
  "X402_NETWORK": "eip155:8453",
  "X402_USDC_CONTRACT": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  "X402_CDP_API_KEY_ID": "your-cdp-key-id",
  "X402_CDP_API_KEY_SECRET": "your-cdp-key-secret"
}
```

CDP API keys are available from the [Coinbase Developer Platform](https://docs.cdp.coinbase.com/). The free tier includes 1,000 transactions per month.

### Supported Facilitators

| Facilitator | Networks | Auth | Cost |
|:------------|:---------|:-----|:-----|
| [x402.org](https://x402.org) | Base Sepolia | None | Free |
| [CDP](https://docs.cdp.coinbase.com/x402/welcome) | Base, Solana, Polygon | CDP API keys | 1,000 free tx/month |
