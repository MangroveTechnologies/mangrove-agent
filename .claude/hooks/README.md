# mangrove-agent — Claude Code guardrail hooks

These hooks are **harness-enforced guardrails**: Claude Code runs them automatically (wired in [`.claude/settings.json`](../settings.json)) and the agent cannot disable them mid-session. They exist so the agent can't — by accident or by a prompt-injection — commit to `main`, leak wallet secrets into the transcript, or fire a doomed/unfunded swap.

**Standing rule:** when a hook blocks, its message now names the **exact hook file (absolute path)** and states that the block is intentional. **Only a human edits or removes a hook** — the agent must never modify/disable/delete one to route around a block. If a rule is wrong, change the file in a reviewed commit.

## The hooks

| Hook | Event(s) | Fires on | Action |
|---|---|---|---|
| [`block-main-commits.sh`](block-main-commits.sh) | `PreToolUse` (Bash) | A `git commit`/`merge`/`rebase`/`reset --hard`/`reset HEAD~` while on `main`/`master`, or a `git push` (incl. `--force`) to `origin/main`/`master`. Detects the target via `git -C <path>` (worktree-safe). | **Blocker — exit 2** with a feature-branch fix. No-op outside a git repo or on non-Bash tools. |
| [`block-wallet-secrets.sh`](block-wallet-secrets.sh) | `UserPromptSubmit` (`--mode user`) + `PostToolUse` (`--mode tool`) | A private-key shape (`0x`+64 hex) or a 12/24-word BIP-39 mnemonic. **user mode:** any match. **tool mode:** only when the key-shaped value sits in a key-named field (`private_key`, `seed_phrase`, …) — so tx hashes don't false-trip. | **Blocker — exit 2** and points to the out-of-band `stash-secret.sh` → vault-token import flow. Keeps key material out of the transcript + API context. |
| [`preflight-swap.sh`](preflight-swap.sh) | `PreToolUse` (`mcp__plugin_mangrove-agent_mangrove-agent__execute_swap`, or `mcp__mangrove-agent__execute_swap` in a git clone) | The swap's wallet holds **0** of the input token on the target chain. | **Blocker — exit 2** with chain/funding guidance. Passes through if balance is non-zero or can't be cleanly read (the SDK then surfaces its own error). |
| [`_hooklib.sh`](_hooklib.sh) | — (sourced) | n/a | Shared `block_footer` helper — appends the absolute hook path + don't-bypass notice to every block message. Not a hook itself; never registered in settings. |

All three live hooks are **blockers** (they `exit 2` to stop the action). There are no reminder-only hooks registered in this repo's `settings.json`; if one is ever added (a hook that only prints to stdout and `exit 0`s), document it here as a **reminder** and do **not** give it a `block_footer`.

## Server-side equivalents (every client)

These hooks run only in Claude Code. Hosts that use the command client (`scripts/local_mcp_bridge.py`: Codex, Grok, Grok Bot, Perplexity) or another MCP client get none of them, so the enforceable parts also run inside the agent. See [guardrails across clients](../../docs/clients.md#guardrails-across-clients).

| Hook | Agent-side equivalent | Still host-only |
|---|---|---|
| `block-wallet-secrets.sh --mode user` | `PricedFastMCP.call_tool` ([`server/src/mcp/server.py`](../../server/src/mcp/server.py)) refuses a secret in any tool's **arguments** (`SECRET_IN_ARGUMENTS`) before the tool or upstream runs, using [`server/src/shared/secret_guard.py`](../../server/src/shared/secret_guard.py). | Blocking the **prompt itself**: text typed into a host chat reaches the host's model and transcript before any tool runs. |
| `block-wallet-secrets.sh --mode tool` | The same boundary withholds a Mangrove tool **result** that has a secret in a key-named field (`SECRET_IN_RESULT`). | Output of non-Mangrove tools (shell, file reads), which never passes through the agent. |
| `preflight-swap.sh` | `dex_service.require_input_balance`, called from the live swap path and from live strategy entries (`dex_confirmed.execute`), refuses a zero input-token balance (`WALLET_INPUT_BALANCE_EMPTY`) before quoting or approval. It also covers the `USDC` symbol, which the hook skips, and REST swaps. Live strategy exits are not checked. | Nothing. The hook is now redundant defense in depth; removing it is a maintainer decision. |
| `block-main-commits.sh` | None. Git runs in the host shell, not through the agent. | All of it. Use GitHub branch protection for an every-client guarantee. |

`secret_guard.py` starts from the hook's shapes and key-named fields and is stricter in two ways: it matches a 64-hex key with or without `0x`, and it compares field names after lowercasing and removing `_` and `-` (so `privateKey` is key-named). Outside key-named fields it refuses a 64-hex value inside free text unless the field or a preceding word marks it as a hash, and outside key-named fields it counts a word run as a recovery phrase when it has a valid BIP-39 checksum, or when it repeats no word and is not in alphabetical order. Its module docstring states the full rules. It reads the BIP-39 list from `eth_account` so it works in the Docker image, which excludes `.claude/`. [`server/tests/unit/test_secret_guard.py`](../../server/tests/unit/test_secret_guard.py) checks that the list matches `bip39-english.txt` and that the strict scan agrees with the hook's user mode on `0x` keys, phrases and seeds. If you change the hook's patterns, review `secret_guard.py` in the same pull request.

## Related: the code-level signing guard (not a hook)

Beyond these Claude-side hooks, there is a **hard signing guard in the agent itself**: [`server/src/services/wallet_manager.py`](../../server/src/services/wallet_manager.py) (`_validate_sign_target`). Invariant: the agent signs **only** a 1inch `AggregationRouter` call or the ERC-20 `approve()` whose spender is a 1inch router — any other transaction shape (arbitrary EOA transfers, non-1inch contracts) is **refused at sign time**, on every chain. `preflight-swap.sh` is the Claude-side companion: it gates *when* a swap is attempted (balance present); the signing guard restricts *what* may ever be signed.

## Adjusting a hook

Edit the file and commit (changes are visible in review). To disable one, remove its entry from [`.claude/settings.json`](../settings.json) and/or delete the script — **a human action, in a reviewed PR**. The agent will not do this for you.
