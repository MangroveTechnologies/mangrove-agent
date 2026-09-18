# Custodied x402 payment scripts

These scripts exercise an **already running local agent**. All four entry points
are retained, with their original roles:

| Script under `server/scripts/` | Purpose |
| --- | --- |
| `pay_hello_mangrove.py` | Interactive walkthrough: show the unsigned challenge, then confirm payment |
| `test_x402_mainnet.py` | Noninteractive REST smoke test: require a challenge, delivered resource and validated receipt |
| `agent_pay_hello_mangrove.py` | Noninteractive REST payment example |
| `agent_pay_hello_mangrove_mcp.py` | Noninteractive MCP payment example using the SDK's payment metadata |

The historical `test_x402_mainnet.py` filename is preserved. It does **not**
select mainnet. Every script reads `X402_NETWORK` from application configuration;
Base Sepolia (`eip155:84532`) is the normal local configuration. If configuration
selects Base mainnet (`eip155:8453`), an explicit `--allow-mainnet` is also required.
Do not add that flag unless you intend to spend real USDC.

## Prerequisites

Use the agent's Python environment with `server/requirements.lock` installed.
Start the agent normally first; these scripts never create a server, migrate a
database, create/import a wallet, confirm a backup, or reset a spending limit.
Select a funded, existing wallet with a confirmed backup using the config key
`X402_PAYER_WALLET` or the **public address** option `--wallet`.

Use the same `ENVIRONMENT` and configuration as the running agent. For a plugin
installation, also set `MANGROVE_AGENT_HOME` to its existing home so that config,
wallets, encryption key and spending ledger are shared. The scripts do not
auto-discover a different installation or guess which wallet should pay.

For a clone started by `scripts/setup.sh`, relative state paths are anchored to
the repository root, regardless of the terminal's working directory. If you
started the agent manually from another directory, pass `--state-dir` with that
directory. Absolute configured paths, including paths anchored by
`MANGROVE_AGENT_HOME`, remain absolute. For Docker, use the same mounted database
and master-key files, with paths accessible to the script's process. An absent
or outdated database is an error; the scripts do not create a separate budget.
The existing encryption key must also be available from its keyfile or the OS
keychain. If it is missing, restore the original key or unlock the keychain;
payment preflight and decryption never generate a replacement key.
Once the process has loaded its master key, wallet creation and decryption
share that same cached key, including if the OS keychain subsequently locks.
Concurrent initial requests are serialized so they cannot initialize different
keys within the same process.

## Usage

From the repository root, with the matching Python environment activated:

```bash
ENVIRONMENT=local python server/scripts/pay_hello_mangrove.py
ENVIRONMENT=local python server/scripts/test_x402_mainnet.py
ENVIRONMENT=local python server/scripts/agent_pay_hello_mangrove.py
ENVIRONMENT=local python server/scripts/agent_pay_hello_mangrove_mcp.py
```

Each invocation may spend USDC. The three noninteractive scripts pay without a
prompt. The walkthrough pauses before requesting the challenge and before paying.
It shows the server's actual quoted base-unit amount, asset, recipient and network;
the payer obtains and validates a fresh challenge when the paid exchange starts.
The spending cap and per-payment ceiling still apply if the quote changes.

The destination defaults to `LOCAL_AGENT_URL` in configuration. Override a local
port explicitly, for example:

```bash
ENVIRONMENT=local python server/scripts/agent_pay_hello_mangrove_mcp.py --server-url http://127.0.0.1:9082
```

Only HTTP(S) loopback origins are accepted. No redirects, ambient proxy routing,
API-key headers or previously signed payment payloads are used. Legacy
`SERVER_URL`, `X402_NETWORK` and `WALLET_SECRET` environment overrides are no
longer used by these scripts. Do not export private keys. `--help` works without
loading application configuration or touching wallet state.

## Safety and results

Both transports use `x402_payer`, the custodied signer, backup confirmation and
the same persistent spend ledger as the agent. The existing signing guard
validates the chain, USDC contract and authorization before decrypting the key.
Every signature reserves budget first; concurrent processes share the cap.
The agent never settles outbound payments itself; the receiving server does.

The scripts initialize the same redacted logging configuration as the agent.
Diagnostic wallet addresses are shortened and sensitive fields are redacted.
The final receipt still intentionally displays the full payer and transaction
hash so you can verify them; that output contains public, linkable identifiers.

MCP makes one unsigned call and at most one paid retry. Sending a signature is
not evidence of settlement. A receipt must report success and contain a valid
transaction hash, matching payer and configured network before the final
reservation can be recorded as settled. That is server-reported evidence, not
an independent on-chain verification. A valid receipt is recorded even if the
resource execution fails after payment.

Exit code `0` requires **both** a successful resource response and a validated
settlement receipt. An error, redirect, free response or missing/invalid receipt
does not pass this payment check. Exit code `1` means the check failed; it does
not prove no money moved. Cancellation returns `130`. Requests are bounded,
including an overall 120-second payment deadline. Timeouts, cancellation and
missing receipts leave signed reservations counted. Inspect `x402_spend_status`
and reconcile uncertain payments before rerunning; the scripts never retry an
ambiguous failure, release signed reservations or reset the budget.
