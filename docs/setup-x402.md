# Setup with an API key or x402

Run `./scripts/setup.sh` from your clone. This is the setup entry point for both
access methods. Plugin installation is a separate, unchanged API-key path.

On every interactive setup run, choose an API key or x402. Enter keeps the current
mode (x402 is the default for a fresh install). API-key input is hidden.
The script always creates or preserves a separate local credential for Claude's
access to the agent. Omitting an upstream key never disables local authentication.
Select either mode from this menu, or use `--auth api-key` / `--auth x402` to
choose explicitly without the menu. Invalid API keys never fall back to payment.
The choice is saved as `MANGROVE_ACCESS_MODE` (`api-key` or `x402`). Switching to
x402 preserves a previously saved upstream key but does not send it to MangroveAI
or Markets for these calls. Switching back can reuse it. Existing configurations
without this setting continue to select API-key mode when a key is present.
Local wallets, approvals, payment history and Claude's local credential are not
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

## Automatic payment recovery

Normal `scripts/setup.sh` x402 setup now configures a read-only reconciliation
RPC for the selected Base network, using the reviewed endpoints shipped in
`mangrove-endpoints.json`. Existing custom RPCs are preserved. Operators can use
a dedicated provider through `X402_RECONCILIATION_RPC_URLS`; its endpoint must
support finalized blocks and block-hash-pinned `eth_call`. Setup configures the
endpoint but does not claim it is reachable or make payments. Rerun setup and
restart existing installations to enable this configuration.

The shared agent worker checks pending authorizations in bounded batches every
15 seconds, with per-operation backoff. Retrying the same pending request also
checks that operation when its inspection is due, with an eight-second RPC
inspection deadline and bounded network timeouts. An in-flight network phase
can finish after that deadline. Different requests do not wait for that inspection. This
applies to MangroveAI HTTP calls and Markets MCP calls. Spend status reports
configured reconciliation networks without exposing RPC credentials.

Only finalized evidence of an expired unused or cancelled authorization releases
its reservation. Wall-clock expiry, an HTTP error, an empty result, or a request
for a different tool cannot do so. A normal fresh request after proven nonpayment
can create a new operation within the existing budget. Explicit recovery of a
closed operation never silently creates a replacement payment. Confirmed paid
operations retain their identity for response recovery; repeated recovery uses
the original proof and never signs again. A successful empty response is cached
like any other successful result.

The worker never purchases, signs, resets a spending cap, or deletes payment
evidence. Unavailable RPCs and ambiguous settlement remain pending; unrelated
requests can proceed within the remaining budget. Authorization expiry bounds
an unused signature's lifetime, not retention of financial evidence or paid
results. Refund processing still requires a receiver-supported policy and
confirmed repayment; discarding a record is not a refund.

Rejected requests do not justify erasing a signed payment. The agent distinguishes
unsigned failures, provably unspendable authorizations, and unsettled outcomes.
For supported Base USDC v2 payments, it can close an authorization addressed to
zero or one with an empty validity interval without waiting for finality. It must
first recover the signer from the original saved EIP-712 proof and match its
network, token, payer, recipient, amount, nonce, and validity bounds to the ledger.
An HTTP status, facilitator rejection string, changed recipient configuration,
or an altered ledger field alone is insufficient evidence.

This check runs on an ordinary retry and in background reconciliation, for both
MangroveAI HTTP and Markets MCP payments. Closing the attempt releases its budget
and retains the audit record; the next ordinary request receives a new operation
identity. Explicitly replaying the closed old ID never authorizes a new charge.
Concurrent retries still have one owner. A usable authorization with an unknown
settlement stays reserved for chain-based reconciliation; different requests can
continue within the remaining budget. A paid-but-lost response still recovers the
original operation instead of creating a replacement payment.


### New requests versus payment recovery

An explicit new operation ID represents an independent request even when its
arguments match an older pending request. Markets reads and remote MCP calls
allocate a fresh ID when the caller omits the recovery ID. MangroveAI SDK calls
also allocate one ID per HTTP request, preserved through its payment challenge;
SDK automatic retries remain disabled. Explicit recovery
reuses the original ID, proof and saved result. Automatic recovery never creates
a fresh payment. Legacy callers without an operation identity retain conservative
fingerprint-based recovery; callers must assign one identity per logical request
to opt into independent identical requests.

Uncertain reservations still consume the remaining budget until proven unpaid.
The status response exposes `settled_usd` (net of confirmed refunds) and
`reserved_usd` separately; legacy `spent_usd` includes both for compatibility.
Proven unpaid attempts are marked released, excluded from budget consumption,
and retained as audit/replay evidence. Elapsed local time alone never proves a
signed authorization was not used. Fresh requests can therefore proceed while
older attempts reconcile, provided there is enough unreserved budget.
