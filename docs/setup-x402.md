# Setup with an API key or x402

`./scripts/setup.sh` is the setup entry point for both ways of paying for MangroveAI. Claude Code plugin installation is a separate, API-key-only path. For a client-neutral walkthrough, start with the [user guide](user-guide.md).

## Choosing an access mode

MangroveAI access works in one of two modes:

- **API key.** You hold a MangroveAI API key and calls count against its allowance.
- **x402.** The agent pays per call in USDC from a local wallet you control. There is no signup and no upstream key.

On an interactive run, setup asks which mode you want. The menu highlights the API-key option, so press Enter to take it or move the selection to choose a wallet. API-key input is hidden, and when a key is already saved, setup asks whether to keep it or enter a new one. To skip the menu, pass `--auth api-key` or `--auth x402`. The choice is saved as `MANGROVE_ACCESS_MODE` (`api-key` or `x402`) in `server/src/config/local-config.json`.

- `--yes` skips every prompt and keeps the saved mode. On a fresh install it selects x402 and defers wallet setup. If an older configuration has no saved mode but contains a key, it selects API-key mode.
- API-key mode never falls back to paying from a wallet. A rejected key is an error, not a trigger for payment.
- Switching to x402 keeps a previously saved upstream key in the config but does not send it to MangroveAI or MangroveMarkets for paid calls. Switching back can reuse it.
- Changing the mode does not reset local wallets, approvals, payment history or the AI client's local credential.
- The old `--api-key KEY` argument is rejected because it exposed keys in process arguments and shell history. Automation can pipe a key from a private secret source into `--api-key-stdin --yes`; never type a literal key into a shell command.

Setup always creates or preserves a separate local credential (`API_KEYS`) that your AI client uses to reach the agent. Omitting an upstream key never disables local authentication.

## How Claude connects

Claude registration uses a dynamic `headersHelper` that reads the local credential at connection time, so no key is passed to the Claude CLI or stored in its server registration. Use a current Claude Code version, restart it in this checkout, and approve the helper or workspace if prompted. The helper requires the matching MCP server name and URL, so if you change the local port, rerun setup to register it. It prints the credential only to a subprocess pipe or socket and rejects terminals and regular files. That checks the output channel and the configured target, not the identity of the receiving process.

After setup or a helper update, reconnect from `/mcp` and ask for your x402 spend status. That local read works without shell commands or an explicit key argument. A connected status alone does not prove authenticated tool execution. See Claude's [dynamic-header documentation](https://code.claude.com/docs/en/mcp#use-dynamic-headers-for-custom-authentication).

## Wallet instructions for x402

After the server is authenticated and Claude registration is saved (if Claude is installed), an interactive x402 run offers four choices: create a new wallet, import an existing wallet, use a wallet already saved in the agent, or finish later. Choosing one only prints instructions. Run the commands yourself in a private terminal in this checkout. Never give any AI client a private key or recovery phrase.

The `--wallet` commands below are separate from a normal setup run. They call the agent's local wallet services, do not install packages or restart the agent, and read the configured local URL, including a custom port. Each must be run on its own (not combined with other setup options), in an interactive terminal, and only in x402 mode; they refuse to run while an upstream API key is selected.

### Create a wallet

```bash
./scripts/setup.sh --wallet create
```

Review the configured network and type `CREATE`. The agent creates an encrypted wallet and prints its public address. It does not select the wallet as payer or confirm a backup. Replace `WALLET_ADDRESS` below with that address:

```bash
./scripts/reveal-secret.sh --address WALLET_ADDRESS
```

Run this only in your own terminal; it refuses to write to a file or pipe. Save the secret in secure storage outside the agent and clear your terminal scrollback. Only after saving it, confirm the backup:

```bash
./scripts/confirm-backup.sh WALLET_ADDRESS
```

If creation times out, list saved wallets before retrying, because the server may have completed the request. Reveal-by-address also works after a creation vault token has expired. Do not delete wallet state or regenerate an encryption key to recover.

### Import an existing wallet

```bash
./scripts/setup.sh --wallet import
```

Review the network, type `IMPORT`, and type `BACKED UP` only if you have already saved the secret outside the agent. Enter the private key or mnemonic at the hidden prompt. The command passes it to the agent's local secret vault and imports the single-use vault token in memory; neither the secret nor the token is printed or placed in a command argument. You do not enter an address: the agent derives it and displays it, so check that it matches your wallet. Import marks the backup as confirmed, which is why the explicit acknowledgement comes first.

If the address is already saved, import returns the existing wallet without replacing its key, original network metadata or backup status. Complete any outstanding backup confirmation before selecting it.

### Use a wallet already saved in this agent

```bash
./scripts/setup.sh --wallet list
```

This lists saved EVM public addresses and their backup status. A saved EVM wallet can be used on either Base network without importing its secret again; the configured payment network decides which one, and balances are separate on each network. If necessary, complete the reveal and backup commands above before selecting a payer. This is local custody, not a MetaMask or WalletConnect browser connection.

### Select the payer and review spending

```bash
./scripts/setup.sh --wallet select
```

Choose a saved, backup-confirmed EVM wallet and review the spending cap, then type `SELECT` to save the public payer address (`X402_PAYER_WALLET`) and the cap (`X402_SPEND_CAP_USD`). Selection reads and displays the effective budget. It does not reset spending history, release uncertain payments or clear an exhausted budget. A cap that a human previously authorized for the current period overrides the configured value, so selection refuses a different cap rather than saving one that has no effect. Check `x402_spend_status` with your AI client before paid use. Budget reset is a separate, explicit human authorization (`x402_spend_reset`) and is not part of setup.

Apply the saved configuration:

```bash
./scripts/setup.sh --yes
```

For Docker installations, add `--docker` to setup and rerun commands; Docker uses port 9080. Bare-metal installs with a custom port set `BARE_PORT=9082` on the first run, and later runs retain it. All setup traffic stays on loopback.

## The spend budget

The agent limits what it pays out. `X402_SPEND_CAP_USD` (default `25`) is a cumulative budget for outbound x402 payments, in dollars. It bounds total volume, not the price of one call; a separate per-payment ceiling of $1, fixed in the source, limits any single automatic payment. Once the budget is spent, payments stop until a human authorizes a new budget; raising the config value does not refill a budget that is already spent. `x402_spend_status` shows what was spent, what is reserved, the cap and where the cap came from, and `x402_spend_reset` is the explicit human top-up.

Payments also require a backup-confirmed payer wallet and USDC on the configured network, and the agent allows at most one signed retry per upstream request. A tool can make several requests (pagination or polling), each potentially billable. Free responses need no payment. MangroveMarkets reads and local-only tools follow their own access rules (see [Markets read tools](markets-read-tools.md)).

## Network and funding

Fresh installs default to Base mainnet (`eip155:8453`) and the hosted production API. Fund the selected public address with real USDC on Base mainnet only after you check the network that setup displays. Setup prints the selected network but never funds a wallet or makes a paid test call, and funding alone does not select the payer or apply configuration. Existing configurations keep their selected network, backend URL, payer and spending history on reruns.

For local testing against your own backend, edit `server/src/config/local-config.json`. Set `X402_MANGROVE_BASE_URL` to your backend's `/api/v1` URL (see [configuration](configuration.md#advanced-service-overrides)) and use the same payment network on both sides:

| Agent setting | Base mainnet | Base Sepolia |
| --- | --- | --- |
| `X402_NETWORK` | `eip155:8453` | `eip155:84532` |
| `X402_USDC_CONTRACT` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | `0x036CbD53842c5426634e7929541eC2318f3dCF7e` |
| Funding | Real USDC on Base | Test USDC on Base Sepolia |

Rerun `./scripts/setup.sh --yes` after editing to apply the configuration and set up payment reconciliation for the selected network. This does not switch the remote backend, which must be configured for the same network. Test USDC cannot pay a mainnet service. Switching networks does not move funds: the same saved EVM wallet has a separate balance on each network. Confirm the backup and select a payer before paid use.

The agent's `X402_FACILITATOR_URL`, `X402_PAY_TO` and CDP credential settings configure its own receiving endpoint for the `hello_mangrove` demo, not the remote MangroveAI receiver. Users who pay MangroveAI do not need CDP credentials. Ordinary outbound payments follow the payment challenge the MangroveAI backend returns.

Once configured, paid tools can pay automatically within the signing and spend controls. Paper trading simulates trades, but data, signals and backtests can still incur API costs. MangroveMarkets and other providers have their own access requirements, so keyless MangroveAI access does not prove that every trading capability works without other credentials.

## Readiness and recovery

- **Local readiness** proves that a protected wallet-list read works with the configured credential and is refused without it. Verification also checks scheduler readiness and that the required onboarding tools are discoverable, and in API-key mode it presents the configured key to the platform once. None of this creates a wallet, signs a payment or establishes payment readiness.
- **Claude registration** is saved configuration. Open or reconnect Claude to confirm the actual connection. If the CLI is absent, setup reports registration as skipped; install it and rerun setup.
- **Wallet instructions and funding** are separate from installation success. `--yes` defers the instructions; rerun interactively to see them again.
- **Reruns** preserve wallet state, credentials and the spending ledger. A managed bare-metal agent restarts when the configuration, source or dependency fingerprint changes. An agent whose process ownership setup cannot establish must be stopped manually first.
- **Errors:** an occupied port, malformed config, failed verification or a conflicting setup process is an error, not a successful installation. Setup does not kill unknown processes.
- **Locking and writes:** config writes are atomic with mode 0600. Setup serializes runs with `agent-data/.setup.lock`. After a hard interruption, check its recorded PID and remove that directory only after confirming no setup is running.
- **Foreground mode:** `--foreground` completes registration and checks before it waits in the terminal, and Ctrl+C stops that agent. Background mode is not a reboot or crash supervisor.

## Automatic payment recovery

An x402 payment can be left uncertain when a response is lost after a payment was signed. The agent tracks every payment as an operation, keeps the amount reserved against the budget until the outcome is proven, and never signs a replacement for an uncertain one.

Setup configures a read-only reconciliation RPC for the selected Base network, using the reviewed endpoints in `mangrove-endpoints.json`, and preserves any custom endpoints. To use a dedicated provider, set `X402_RECONCILIATION_RPC_URLS` to a JSON object that maps network IDs to URLs. The endpoint must serve finalized-chain queries. Setup configures the endpoint but does not check that it is reachable.

When an endpoint is configured, a background worker checks pending authorizations in batches of up to five every 15 seconds, with per-operation backoff and an eight-second inspection deadline. Retrying the same pending request also checks it when its inspection is due, and different requests do not wait for that inspection. The worker never purchases, signs, resets a spending cap or deletes payment evidence. Only finalized chain evidence of an unused, expired or cancelled authorization releases its reservation; wall-clock expiry, an HTTP error, an empty result or an unrelated request cannot. Unavailable RPCs and ambiguous settlement leave the amount reserved, while unrelated requests continue within the remaining budget.

`x402_spend_status` reports `settled_usd` (net of confirmed refunds) and `reserved_usd` separately, along with the configured reconciliation networks. `spent_usd` is their sum. Uncertain reservations keep consuming budget until proven unpaid. A fresh request after a proven non-payment gets a new operation identity within the existing budget, and explicitly replaying a closed old operation never authorizes a new charge. A paid operation whose response was lost recovers the original result instead of paying again.

For the complete model, including operation identity, evidence rules, refunds and operator inspection, see [payment operations and reconciliation](x402-payment-uncertainty.md), [remote MCP payments](remote-mcp-payments.md) and [the synchronous x402 transport](x402-sync-transport.md).
