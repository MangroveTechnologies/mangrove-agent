# Isolated wallet custody

Wallet creation and signing use a separate OS identity. The agent holds public
wallet metadata and calls a Unix-domain socket. Its `API_KEYS` credentials do not
authorize export, import, or backup confirmation. `MANGROVE_API_KEY` remains a
separate credential for outbound managed-service requests and grants no local
custody authority. Setting `AUTH_ENABLED=false` refuses privileged local access.
Paper trading and managed-service API-key access do not require custody.

Both old reveal endpoints, secret stash, wallet import, and backup confirmation
return HTTP 410. The compatibility `import_wallet` MCP tool refuses the operation.
Creation returns an empty deprecated `vault_token` and TTL zero, rather than an
export capability. The old agent-side stash/reveal/confirm helpers refuse without
collecting a secret or calling an export endpoint.

## Required operating-system boundary

The custody process runs as a dedicated non-root UID, distinct from the agent
and every general coding client. Its state directory is owned by that UID with
mode 0700. The installation, Python runtime, importable dependencies, service
definition and policy must be root owned and unwritable by the agent. Startup
checks those ownership and permission requirements, rejects extended ACL grants
that mode bits alone would miss, uses Python `-I`, rejects
Linux capabilities, and authenticates the peer UID of each socket connection.
The client independently verifies the socket's owner and server peer UID.
Framing rejects duplicate JSON keys, oversized messages and slow clients that
exceed the total receive deadline.

The agent must have no sudo/root privileges, Docker or other host-container
control, debugging access to custody, or ability to modify its launcher,
environment, policy or dependencies. A client running under an administrator's
unrestricted desktop identity does not meet this requirement. A separate process
under that same identity, a bearer key beside its config, and an agent-controlled
container do not provide the boundary. Host root and a compromised host kernel
remain trusted. A host administrator must verify privilege paths; checking group
membership alone cannot establish their absence.

Linux and macOS have supported peer-identity implementations. Other operating
systems fail closed. macOS deployments need a root-managed Python 3.11+ runtime;
a user-owned Homebrew interpreter fails the ownership checks. macOS installer
defaults are `/Library/MangroveCustody/{app,configuration,state,run}`; Linux
defaults use `/opt`, `/etc`, `/var/lib` and `/run`. Use real protected paths
rather than `/var` symlinks on macOS. Native
macOS ownership and service behavior require deployment acceptance on that host;
Linux acceptance does not establish macOS enforcement. No default installation
silently enables local in-process signing.

## Administrator installation

Create a restricted agent OS user and a separate non-login custody OS user using
the host's account-management tools. Run the coding client and agent service as
the restricted agent user. Keep administrator authentication outside that client.
Then run the installer from a reviewed source checkout as host administrator:

```bash
sudo python3 scripts/install-custody.py --custody-user mangrove-custody --agent-user mangrove-agent
```

This installs into `/opt/mangrove-custody`, creates private state at
`/var/lib/mangrove-custody`, a read-only-to-agent socket directory at
`/run/mangrove-custody`, and root-owned configuration at `/etc/mangrove-custody`.
Existing paths are rejected rather than overwritten. It installs the locked
dependencies from `server/requirements-custody.lock` with
`pip --require-hashes --only-binary :all:`: only wheels whose SHA-256 is listed
in the reviewed lock install, and no source distribution is built, so no build
dependency is fetched without a hash. `scripts/hash_custody_lock.py` regenerates
that file from `server/requirements.lock` and the wheel hashes PyPI publishes;
CI regenerates it with `--verify` and fails on any difference. It also generates service definitions, but starts no service and grants
no spending authority. Choose protected platform-specific paths with
`--destination`, `--state-dir`, `--socket-dir` and `--config-home` when needed.
Do not put writable agent state inside the immutable installation.

Review the generated policy and sibling `config/custody-config.json`. The latter
selects reviewed service origins/network for narrow identity proofs; it contains
no inbound HTTP server or usable export credential. The installer explicitly
pins `X402_MANGROVE_ENVIRONMENT` to the reviewed catalog default for a local
installation (currently `prod`); the private runtime label `custody` is not a
service environment. The protected administrator may choose another reviewed
environment and origin for their deployment. This lookup does not make a network
request. Do not copy the ordinary
agent's credentials into it. Its database/master-key paths must point into the
private custody state directory.

On Linux, install the generated service definition and start it:

```bash
sudo install -o root -g root -m 0644 /etc/mangrove-custody/mangrove-custody.service /etc/systemd/system/mangrove-custody.service
sudo systemctl daemon-reload
sudo systemctl enable --now mangrove-custody
```

On macOS, install the generated `io.mangrove.custody.plist` into
`/Library/LaunchDaemons` as root:wheel, mode 0644, then use
`sudo launchctl bootstrap system /Library/LaunchDaemons/io.mangrove.custody.plist`.
Service managers supply the protected launch environment. Do not let an agent
launch custody with caller-controlled loader variables such as `LD_PRELOAD` or
`DYLD_INSERT_LIBRARIES`. Python isolation alone does not neutralize the dynamic
loader before interpreter startup.

Set the ordinary agent's JSON configuration to the generated public socket path
and numeric custody UID:

```json
{"CUSTODY_SOCKET":"/run/mangrove-custody/agent.sock","CUSTODY_UID":10002}
```

Use the actual generated UID. Restart the ordinary agent and list wallets. Its
SQLite journal receives metadata-only `external-custody` rows to retain allocation
foreign keys. It stores no wallet ciphertext. Wallet listings report
`custody_status`: `isolated` when custody can sign, `migration_required` for a
legacy wallet whose key is still agent ciphertext (not yet migrated, or migrated
but not yet retired), and `custody_unconfigured` when the agent has no custody
socket. The free `status` tool and `GET /api/v1/agent/status` carry a `custody`
block with `configured`, `migration_required_wallets`,
`unsignable_live_allocations` and `action_required`.

### Upgrading an existing installation

Legacy agent-held wallets cannot sign after this upgrade, including protective
exits on open live positions. Until custody is installed and each wallet is
migrated, retired and its backup confirmed again, the agent refuses live
promotion, live entries and exits, and x402 payments from those wallets with a
`migration_required` or "custody is not configured" error that names this
guide. Before upgrading, flatten live DEX positions or plan to complete the
migration below promptly, and check the `custody` block of `status` afterwards.

## Human backup and import

The human administrator runs these commands in a private terminal outside any
agent-controlled PTY or terminal recorder. The OS privilege transition is the
authorization boundary. A terminal check or typed confirmation by itself cannot
identify a human or prevent recording.

```bash
sudo -u mangrove-custody /opt/mangrove-custody/.venv/bin/python -I /opt/mangrove-custody/scripts/custody-service.py admin --config /etc/mangrove-custody/policy.json backup WALLET_ADDRESS
sudo -u mangrove-custody /opt/mangrove-custody/.venv/bin/python -I /opt/mangrove-custody/scripts/custody-service.py admin --config /etc/mangrove-custody/policy.json confirm-backup WALLET_ADDRESS
sudo -u mangrove-custody /opt/mangrove-custody/.venv/bin/python -I /opt/mangrove-custody/scripts/custody-service.py admin --config /etc/mangrove-custody/policy.json import --chain evm --network testnet --chain-id 84532
```

Backup deliberately displays the selected secret after confirmation. Save it in
an independent secure backup, clear terminal scrollback, then confirm. Import
reads hidden input directly in the custody process and returns only the address;
XRPL family seeds use `--chain xrpl`. No secret, import handoff or export token is
passed through the ordinary agent, HTTP, MCP, process arguments or environment.
Software custody requires the human to protect these backups; these keys are
exportable by that separately authorized human, unlike non-exportable hardware
keys. Python strings cannot be reliably erased from a running process.

## Independent signing policy

The root-owned `policy.json` denies signing by default. A human administrator
must authorize each wallet and destination independently of agent request fields.
Changing ordinary app config, `confirm=true`, an intent's claimed amount, or the
agent's x402 budget cannot widen custody policy. Restart custody after reviewing
and changing this file; an exact retry is bound to the policy version under which
it was signed and cannot release a cached signature after policy changes.

Each `swap_rules` entry specifies `wallet_address`, `chain_id`, `input_token`,
`output_token`, `router`, `allowed_executors`, `max_amount`, `lifetime_amount`,
`min_rate_numerator`, `min_rate_denominator`, `max_routing_fee_bps`, `max_gas`,
`max_fee_per_gas`, `max_total_fee_wei`, `lifetime_total_fee_wei`, and `max_native_value`. Token amounts and
rate ratios use atomic on-chain units. The floor means
`minimum_output * denominator >= input_amount * numerator`; select it from an
independently reviewed minimum acceptable exchange rate. An arbitrary quote
returned to the agent cannot lower this floor. A missing executor whitelist or
routing fee authorization refuses that swap. The routing-fee field bounds the
reviewed quote allowance; it is not a measurement of fees hidden in opaque
executor data. The independently approved output floor remains mandatory.

Approval signatures target the expected token, exact finite amount, approved
router and zero native value. Swap signatures decode canonical 1inch V5/V6
generic `swap` calldata and bind chain, wallet, assets, input, minimum output and
recipient to the immutable intent. Unknown or optimized selectors are refused;
pool-path decoding needs separately verified support. The supported chain set
does not include Base Sepolia router transactions because no canonical deployment
was established there. Testnet wallet creation, identity and USDC x402 signing
remain supported. Fee/gas bounds are checked against both the validator's static
ceilings and the independent policy. Approval and swap lifetime input budgets
are separate counters; maximum gas costs also consume a shared per-wallet/chain lifetime fee budget before signing. A finite allowance remains usable on chain until consumed
or revoked. Policy changes cannot revoke signatures or token approvals already
released.

The `x402` rule specifies `wallet_address`, `chain_id`, `payees`,
`max_payment_micro_usd`, `lifetime_micro_usd`, and `max_validity_seconds`. It
accepts only the guarded deployed USDC EIP-3009 structure, current short validity
windows and explicitly approved recipients. The custody cap is separate from
the agent's reporting-period budget. Resetting that ordinary budget does not
reset protected custody usage. Custody has no remote budget-reset operation.

SIWX requires `siwx_origins`, `identity_chains`, and `identity_wallets`, and still
passes the fixed EIP-4361 proof validator. Marketplace proofs require a recent
exact human approval of the complete challenge, wallet, identity, audience,
operation and arguments. Save the public proof request as JSON, have the human
review it, and run `admin ... approve-marketplace REQUEST.json` in the independent
terminal. Approval lasts ten minutes and authorizes that exact proof only.

Every signing request claims protected usage/nonce state before cryptographic
signing and delivery. Deterministic missing-wallet, backup and argument failures
occur before reservation. A signing exception, or a crash between reservation
and storing the signature, means no signature left custody: the reservation is
refunded and its nonce is free for the next request. Exact successful retries
return the stored signature without spending twice.

A transaction that was signed but never mined (a broadcast failure, an RPC
rejection or a mempool drop) does not strand its nonce. A different transaction
for the same wallet, chain and nonce, such as a re-quoted swap, a gas-bumped
replacement or a protective exit, is signed when it passes the swap policy on
its own. Only one transaction per sender nonce can be included on chain, so the
nonce is charged per budget scope at the largest amount signed there: a
replacement pays only its excess over that amount, and every charge is checked
against the lifetime caps. The custody service does not query the chain; a
replacement for an already mined nonce cannot be included and costs nothing
beyond its charge. For x402, SIWX and marketplace proofs, a different request at
a nonce that already released a signature is refused. The protected
SQLite journal records operation, request digest, policy digest and outcome,
without plaintext secrets or export tokens. Cached signatures are spendable
material and remain inside private custody state. This journal is protected
against the agent UID, not against custody/host-administrator compromise.

## Existing wallet recovery and migration

Do not delete an existing master key, database or backup to install custody. A
human administrator first stops the old agent and preserves a coherent SQLite
snapshot, including checkpointed WAL contents. Transfer a snapshot and its
original Fernet master key into custody-owned mode-0600 files inside protected
custody state. For OS-keychain installs the human must obtain the original key
through their existing recovery process; the migration does not guess a key or
read the user's keychain. Keep the snapshot outside the agent's readable paths.

Run the human command against those protected copies:

```bash
sudo -u mangrove-custody /opt/mangrove-custody/.venv/bin/python -I /opt/mangrove-custody/scripts/custody-service.py admin --config /etc/mangrove-custody/policy.json migrate --legacy-db /var/lib/mangrove-custody/legacy.db --legacy-master-key /var/lib/mangrove-custody/legacy.key
```

Migration validates every decrypted wallet identity, reencrypts with the custody
master key, preserves metadata, commits atomically, and writes
a private receipt containing addresses and hashes of original ciphertext. A
same-snapshot retry verifies already migrated identities and resumes receipt
creation without overwriting them. Backup confirmations are not carried over,
because ordinary agent authority could set the legacy flag: verify each wallet's
backup with `admin ... backup ADDRESS`, then run `admin ... confirm-backup ADDRESS`.
Until then the wallet cannot sign. Verify address listing and human backup from
custody before retiring the original active database. Funds and approved network
limits are never inferred or automatically authorized from legacy balances.

With the old agent still stopped, the independent root administrator moves its
coherent active database into a private protected root/custody-owned staging
directory. Its former agent-readable path must no longer contain the wallet
ciphertext. Preserve any needed journal/WAL data while staging; do not finalize
an incoherent copy. The finalizer refuses a database under agent-writable
ancestors or a symlink. Finalize this protected staged database:

```bash
sudo python3 /opt/mangrove-custody/scripts/custody-retire-legacy.py --legacy-db /var/lib/mangrove-custody/retiring-agent.db --receipt /var/lib/mangrove-custody/migration-receipt.json --custody-uid 10002
```

The finalizer reads the private receipt through a verified file descriptor under
protected ancestors, compares every active original wallet ciphertext hash,
replaces only wallet ciphertext with metadata markers, and performs secure-delete,
checkpoint and vacuum operations. It preserves the shared original master key
because OAuth/venue credentials may still depend on it. Restore the resulting metadata-only database to the original agent state path
with its restricted agent ownership, using host-administrator file tools that
do not follow an agent-controlled destination with elevated write authority.
Configure the socket, restart the agent and verify metadata and constrained
signing before considering that active installation migrated.

Review and relocate old database copies, old master-key backups, OS/filesystem
snapshots and prior transcripts separately. Rewriting the active database cannot
prove historical copies erased. Migration does not recover trust in a key that
another party already copied, revoke existing authorizations or remove installed
EIP-7702 delegation. Treat a known compromised identity separately and do not
fund it merely because this software was installed.

## Verification

The acceptance harness builds an immutable installation, starts real custody and
HTTP/MCP processes under different Linux users, and generates disposable unfunded
wallets. It verifies signatures by recovering their public addresses. It exercises
export denials, protected files, captured terminals, malformed requests, policy
limits, restarts, expiry, and migration of a synthetic legacy database. The runtime
container has no network access or host mounts:

```bash
docker build -f scripts/custody-security.Dockerfile -t mangrove-custody-security .
docker run --rm --network none mangrove-custody-security
```

CI runs this harness separately from the Python test suite. On macOS, run
`python -m pytest server/tests/unit/test_custody_acl.py` to exercise real extended
ACL grants on disposable files. These checks do not establish launchd deployment,
the privileges of an existing host account, or successful funded chain execution.
Validate those properties on the intended installation before funding it.

Client-hook acceptance is separate from custody acceptance. Verify the actual
client has loaded and trusted the hooks, then test with invalid all-zero key-shaped
sentinels. Inspect the client output and retained telemetry without supplying a real
wallet secret. A blocked prompt or post-tool decision cannot erase content a host
already recorded. See [client hook support](clients.md#wallet-boundary-and-hook-support).
