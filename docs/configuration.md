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

### Optional Keys

Loaded if present in your config file (secret-reference capable), and default to
`None` when absent. They never fail startup -- they back off-by-default feature
knobs. Because the loader always defines the attribute, `app_config.<KEY>` is
safe to read directly (it is `None` when unset).

| Key | Description |
|:----|:-----------|
| `SLACK_WEBHOOK_URL` | Slack [incoming-webhook](https://api.slack.com/messaging/webhooks) URL for scheduler-tick notifications. Empty/absent disables Slack (the default). |
| `SLACK_QUIET_IF_EMPTY` | When `true` (default), only post on a fire (new orders / closed trades) or an evaluation error -- quiet ticks are silent. Set `false` to post a heartbeat on every tick. |

When a paper/live strategy's scheduled evaluation fires (or errors), the agent
posts a consolidated OPEN/CLOSE block to Slack (`notification_service`). A failed
Slack POST is logged, never fatal to the tick.

To enable in dev/prod, add a `slack_webhook_url` field to the env's app-config
secret and point the key at it:

```json
"SLACK_WEBHOOK_URL": "secret:app-config-prod:slack_webhook_url"
```

(Leaving it `""`, as the committed dev/prod configs do, keeps Slack off so startup
never depends on a not-yet-provisioned secret.)

## Environments

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
