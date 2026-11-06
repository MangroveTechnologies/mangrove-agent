# Chapter 06 - Wallet setup

Goal: create or import a dedicated wallet under isolated custody, save an
independent backup, and verify its public address and authorized network before
any funding. Wallet setup does not make a payment.

## Before you start

A human administrator must first complete [isolated custody deployment](../../docs/custody.md).
The custody process needs a separate non-root OS identity, private state, and
root-owned code, dependencies and policy. The coding client must not have sudo,
root, Docker/container control, debugging access or write access over custody.
A second process under the same unrestricted desktop identity is insufficient.

Paper trading and managed-service API-key access work without wallet custody.
If custody is not installed, keep using those workflows; wallet signing fails
closed. Do not delete existing wallet data or generate a replacement master key.
Use the documented human migration and guarded retirement procedure for an older
installation, and preserve recovery material until it is verified.

Use a dedicated trading wallet rather than a personal savings wallet. New-wallet
learning should use Base Sepolia (`network="testnet"`, `chain_id=84532`). Mainnet
requires an explicit request. Testnet wallet creation, USDC x402 and identity
proofs work, but 1inch transaction signing on Base Sepolia is refused because a
canonical router deployment has not been established. Do not fund a testnet
wallet expecting this tutorial to demonstrate a supported testnet router swap.

The swap wallet and the optional x402 payment wallet may be different. Both need
independent custody policy. Selecting a payer in the ordinary agent does not
authorize spending from it inside custody.

## Create a wallet

In the AI client, explicitly choose the network, for example:

> "Create a dedicated wallet on Base Sepolia testnet."

`create_wallet` asks custody to generate and encrypt the key. It returns the
public address, network, secret type, backup status and human backup instructions.
The compatibility `vault_token` field is empty and its TTL is zero; it cannot be
redeemed for a secret. Neither this response nor `list_wallets` returns plaintext.

Check the displayed address and network. An address is public; a private key or
recovery phrase must never be pasted into chat or included in a model tool call.

## Human backup and confirmation

The human administrator uses an independent private terminal outside the AI
client and any captured PTY. A terminal check alone cannot identify a human or
prevent recording; the separate OS authority is required.

Use the installed custody interpreter, launcher and root-owned policy paths.
For the documented Linux defaults:

```bash
sudo -u mangrove-custody /opt/mangrove-custody/.venv/bin/python -I /opt/mangrove-custody/scripts/custody-service.py admin --config /etc/mangrove-custody/policy.json backup WALLET_ADDRESS
```

The human reviews the wallet and types `DISPLAY SECRET`. Save the secret in an
independent secure backup, then clear terminal scrollback. Never capture this
command with the coding client, share a screenshot, or redirect its output into
an agent-readable file. After saving the backup, the human runs:

```bash
sudo -u mangrove-custody /opt/mangrove-custody/.venv/bin/python -I /opt/mangrove-custody/scripts/custody-service.py admin --config /etc/mangrove-custody/policy.json confirm-backup WALLET_ADDRESS
```

Type `BACKED UP` only after saving it. For macOS use the installed paths under
`/Library/MangroveCustody`; see the deployment guide. The agent must not run these
privileged commands on the human's behalf.

Ask the client to list wallets again. The selected address should have a
non-null `backup_confirmed_at`. The former reveal/stash/confirm scripts and HTTP
routes are retired; neither the local `API_KEYS` credential nor the upstream
`MANGROVE_API_KEY` authorizes backup/export or confirmation.

## Import an existing wallet

Import only a dedicated wallet whose secret you already control and have backed
up. The human administrator runs this directly in the independent custody
terminal, using the intended chain/network:

```bash
sudo -u mangrove-custody /opt/mangrove-custody/.venv/bin/python -I /opt/mangrove-custody/scripts/custody-service.py admin --config /etc/mangrove-custody/policy.json import --chain evm --network testnet --chain-id 84532
```

It asks for `BACKED UP`, then reads hidden secret input. It displays only the
public address. For XRPL use `--chain xrpl` with the intended XRPL network; family
seeds for Ed25519 and secp256k1 are supported. Check the derived address against
the wallet you intended to import. Reimporting an existing wallet preserves its
key and existing backup state.

There is no agent handoff file or import capability to provide. The compatibility
`import_wallet` MCP tool refuses the operation. Return only the public address
to the client, refresh its wallet list, and select it where required.

## Authorize limits before funding or live use

The human administrator reviews the root-owned custody policy for this exact
wallet. Swap rules bind the chain, input/output tokens, router, executor, finite
input, output-price floor, gas limits and lifetime input/fee budgets. x402 rules
bind the wallet, network, payees, payment amount, validity and lifetime budget.
SIWX identities and marketplace actions have their own narrow approval rules.
An agent-supplied `confirm=true` or claimed transaction intent cannot raise these
independent limits. Backup confirmation alone does not grant unlimited spending.

For explicitly requested mainnet use, verify the exact network, address and
supported operation before transferring a small amount. Sending funds and paying
network fees are separate human decisions outside this tutorial's setup actions.
Check the address on the correct explorer: Base mainnet uses
`https://basescan.org/address/ADDRESS`; Base Sepolia uses
`https://sepolia.basescan.org/address/ADDRESS`. Never infer current delegation or
key safety merely from installing a signing guard.

Use the discovered Markets balance tool to verify the expected asset on the
selected network. This checks a public balance; it does not prove custody policy,
wallet recovery, or the absence of previously copied keys.

## Recovery and troubleshooting

- If creation has an uncertain outcome, list wallets before retrying. Do not
  create a replacement automatically.
- If a wallet needs backup, the human uses direct custody backup and confirmation
  for that exact address. No expired export token needs to be recovered.
- If the custody key is missing, preserve state and restore the original key or
  use the independent wallet backup. Never generate a replacement to hide a
  decryption failure.
- If an old wallet shows `custody_status="migration_required"`, use the human
  migration and retirement procedure in the custody guide. Active wallet
  ciphertext must be removed from agent-readable state after recovery is verified.
- If a transaction is refused, retain its operation/nonce identity and inspect
  the protected policy and audit record. Do not widen limits or retry with changed
  details after an uncertain signature or submission.

Migration cannot prove historical database copies, master-key backups, snapshots
or transcripts erased. A copied private key remains compromised after software
changes; do not fund that identity merely because custody was upgraded.

Next: [Chapter 07 - live execution](07-going-live.md).
