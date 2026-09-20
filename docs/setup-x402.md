# Setup with an API key or x402

Run `./scripts/setup.sh` from your clone. This is the setup entry point for both
access methods. Plugin installation is a separate, unchanged API-key path.

On every interactive setup run, choose an API key or x402. Enter keeps the current
mode (x402 is the default for a fresh install). API-key input is hidden.
The script always creates or preserves a separate local credential for Claude's
access to the agent. Omitting an upstream key never disables local authentication.
Select either mode from this menu, or use `--auth api-key` / `--auth x402` to
choose explicitly without the menu. Invalid API keys never fall back to payment.

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
Verification checks local authentication, required tools and scheduler readiness;
it does not call upstream services or verify payment readiness.

## The x402 wallet menu prints instructions

After the server is authenticated and MCP registration is saved (if Claude is
installed), select **create**, **import**, **use saved wallet**, or **finish later**.
Selecting a menu item only prints instructions. Execute the commands yourself in
a private terminal in this checkout. Never give Claude a private key or mnemonic.

The following explicit commands are separate from the normal setup run. They
call existing local wallet services and do not install packages or restart the
agent. They read the configured local URL, including a custom port.

### Create a wallet

```bash
./scripts/setup.sh --wallet create
```

Review the configured network and type `CREATE`. The agent creates an encrypted
wallet and prints its public address. It does not select it as payer or confirm
backup. Replace `WALLET_ADDRESS` below with that address:

```bash
./scripts/reveal-secret.sh --address WALLET_ADDRESS
```

Run this only in your own terminal. Save the secret in secure storage outside the
agent. Only after saving it, confirm the backup:

```bash
./scripts/confirm-backup.sh WALLET_ADDRESS
```

If creation times out, list saved wallets before retrying: the server may have
completed the request. Reveal-by-address also works if a creation vault token
has expired. Do not delete wallet state or regenerate an encryption key to recover.

### Import an existing wallet

```bash
./scripts/setup.sh --wallet import
```

Review the network, type `IMPORT`, and confirm `BACKED UP` only if you have already
saved its secret outside the agent. Enter the private key/mnemonic at the hidden
prompt. The CLI passes it to the existing local secret vault and immediately
imports the single-use vault token in memory. Neither secret nor token is printed
or put in command arguments. The existing import service confirms backup on
import; this is why the explicit backup acknowledgement comes first.

### Use a wallet already saved in this agent

```bash
./scripts/setup.sh --wallet list
```

This lists public addresses and backup status for the configured payment network.
If necessary, complete the reveal/backup commands above before selecting a payer.
It is local custody, not a MetaMask/WalletConnect browser connection.

### Select the payer and review spending

```bash
./scripts/setup.sh --wallet select
```

Choose a saved, backup-confirmed wallet on the configured network, review the cap,
and type `SELECT` to save the public payer address and configured cap. This does
not reset spending history, release uncertain payments or clear an exhausted
budget. Selection reads and displays the effective budget. A previously human-authorized
period cap overrides configuration, so selection refuses a different cap rather
than saving an ineffective limit. Inspect `x402_spend_status` in Claude before paid use. Budget reset is a separate,
explicit human authorization, not part of setup.

Apply the saved configuration:

```bash
./scripts/setup.sh --yes
```

For Docker installations, add `--docker` to setup/rerun commands. Existing Docker
port mapping uses 9080. Bare-metal custom ports use `BARE_PORT=9082` on the first
run and are retained on later runs. All setup traffic stays on loopback.

Fund the selected public address with USDC on the configured network **after
checking that the receiver accepts that network**. The shipped local default is
Base Sepolia (`eip155:84532`), which uses test USDC. Test USDC cannot pay a Base
mainnet service. Setup does not change network implicitly, verify funding or
certify receiver/mainnet compatibility. Do not send real USDC to follow a testnet
example. Funding alone does not select the payer or apply its configuration.

Once configured, ordinary paid tools may pay automatically within the existing
signing and spend controls. Paper trading simulates trades, but data, signals and
backtests may still incur API costs. MangroveMarkets and other providers have their
own access requirements; keyless MangroveAI access does not prove all trading
capabilities work without other credentials.

## Readiness and recovery

- Local readiness proves a protected wallet-list read works with the configured
  credential and is refused without it. Verification also checks required tools.
  Neither check creates a wallet, signs a payment or validates an upstream key.
- MCP registration is saved configuration; open/reconnect Claude to confirm its
  actual connection. If the CLI is absent, setup explicitly reports registration
  as skipped. Install it and rerun setup.
- Wallet instructions and funding status are separate from installation success.
  `--yes` defers the instructions; rerun interactively to see them again.
- Reruns preserve wallet state, credentials and ledger. Managed bare-metal agents
  restart when configuration/source/dependency fingerprints change. Older agents
  whose process ownership cannot be established must be stopped manually first.
- An occupied port, malformed config, failed verification or conflicting setup
  process is an error, not successful installation. No unknown process is killed.
- Config writes are atomic with mode 0600. Setup serializes runs using
  `agent-data/.setup.lock`. After a hard interruption, check its recorded PID and
  remove that lock directory only after confirming setup is no longer running.
- `--foreground` completes registration and checks before waiting in the terminal;
  Ctrl+C stops that agent. Background mode is not a reboot/crash supervisor.
