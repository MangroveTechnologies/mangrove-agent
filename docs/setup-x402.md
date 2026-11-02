# Setup with an API key or x402

Run `./scripts/setup.sh` from your clone. This is the setup entry point for both
access methods. Plugin installation is a separate API-key path. For a client-neutral
walkthrough, start with the [user guide](user-guide.md).

On interactive setup runs, choose API-key, x402, or browser sign-in access.
API-key input is hidden. Fresh x402 installations use Base Mainnet and USDC on Base;
an existing different network is changed only after an explicit menu choice.
The script always creates or preserves a separate local credential for the AI client's
access to the agent. Omitting an upstream key never disables local authentication.
Select either mode from this menu, or use `--auth api-key` / `--auth x402` to
choose explicitly without the menu. Invalid API keys never fall back to payment.
The choice is saved as `MANGROVE_ACCESS_MODE` (`api-key` or `x402`). Switching to
x402 preserves a previously saved upstream key but does not send it to MangroveAI
or Markets for these calls. Switching back can reuse it. Existing configurations
without this setting continue to select API-key mode when a key is present.
Local wallets, approvals, payment history and the AI client's local credential are not
reset by changing access mode.

`--yes` preserves existing settings; on a fresh install it selects x402 and defers
wallet onboarding. It never creates/imports a wallet, confirms a backup or makes
a payment. Use the hidden interactive prompt for API keys. The old `--api-key KEY`
argument is rejected because it exposed keys in process arguments and shell history.
Automation can pipe a key from a private secret source into `--api-key-stdin --yes`;
never type a literal key into a shell command.

Claude registration uses a dynamic `headersHelper` to read the local credential
at connection time. No key is passed to the Claude CLI or stored in its server
registration. Use a current Claude Code version, restart it in this checkout, and
approve the helper/workspace if prompted. The helper requires the matching MCP
server name and URL; if you change the local port, rerun setup to register it.
Its credential output is restricted to subprocess pipes or sockets (Claude's
runtime may use either); terminals and regular files are rejected. This checks
the output channel and configured target, not the identity of the receiving process.
After setup or a helper update, reconnect from `/mcp` and ask for your x402 spend
status. That local read should work without shell commands or an explicit key
argument. A connected status alone does not prove authenticated tool execution.
See [Claude's dynamic-header documentation](https://code.claude.com/docs/en/mcp#use-dynamic-headers-for-custom-authentication).

The explicit reveal command only writes secrets to a terminal; redirecting its
output is refused. Use a private terminal and clear scrollback after backing up.
Verification checks local authentication, required tools and scheduler readiness.
It can read upstream discovery and, in API-key mode, verify the configured key.
It does not sign payments or establish wallet/payment readiness.

## Guided x402 wallet setup

Interactive setup keeps one session open through wallet creation/import, backup,
payer review, and applying settings. Fresh installations use **Base Mainnet**;
x402 payments spend real USDC. Creating/importing a wallet makes no payment.

- **Create:** select Create, review the network, and type `CREATE`. Setup creates
  the wallet directly and displays its public address. Keep setup open while you
  run the displayed reveal command in a separate private terminal, save the
  secret, and run the displayed backup-confirmation command. The short `reveal-secret.sh --setup` and `confirm-backup.sh --setup` commands
  read the wallet address and local URL from this checkout’s setup progress,
  ignoring environment overrides from other agents. The confirmation command
  shows the address and asks you to type `BACKED UP`. Return to the wizard and
  choose **Check backup and continue** using the arrow keys and Enter. Setup
  verifies the recorded confirmation; Enter alone never confirms backup.
- **Import:** select Import, type `IMPORT`, acknowledge `BACKED UP` only if the
  secret is saved outside the agent, and enter it at the hidden terminal prompt.
  Confirm that the derived public address matches your intended wallet. An
  already-confirmed backup skips the reveal step. Existing imports that still
  need backup confirmation follow the same private-terminal backup flow.
- **Use saved:** choose from all saved EVM wallets. Each entry shows its address,
  backup status, and whether it is the current payer. The current payer is
  highlighted by default. Keeping it preserves spending settings without another
  budget prompt; choosing another wallet completes any missing backup and opens
  payment-settings review.
- **Finish later:** complete local installation without enabling a new payer.

The new/imported address is carried into payment-settings review automatically.
Review the effective spending cap and type `SELECT` to save. Setup applies the
configuration, restarts its agent if necessary, and verifies local authenticated
access without asking you to rerun commands. Existing spending records and
budget authorizations are preserved. Funding and paid access are not verified;
setup never transfers funds or makes a paid test call.

Interrupted flows use `agent-data/wallet-setup.json`, which contains only
public wallet/workflow metadata, never secrets or vault tokens. When a saved
wallet still needs setup, choose **Resume this wallet**, **Choose a different
setup option**, or **Finish later**. Choosing a different option returns to the
Create/Import/Use saved menu; it dismisses only the setup checkpoint and keeps
the wallet, backup status, payer settings, and spending history intact. If a create/import
response is uncertain, setup does not repeat the mutation: it asks you to refresh
saved wallets and identify the intended public address. If none appears, finish
later and investigate the operation before retrying. Changing access modes does
not discard unfinished wallet progress. `--yes` never creates/imports a wallet.

### Standalone wallet commands

The following commands remain available for manual administration outside the
wizard. They call existing local wallet services and do not install packages or
restart the agent. They read the configured local URL, including a custom port.
Do not run a second `setup.sh` while the wizard owns the setup lock. Backup reveal
and confirmation commands can run while the wizard waits.

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
