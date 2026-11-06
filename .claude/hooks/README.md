# mangrove-agent — Claude Code guardrail hooks

These hooks are **harness-enforced guardrails**: Claude Code runs them automatically (wired in [`.claude/settings.json`](../settings.json)) and the agent cannot disable them mid-session. They exist so the agent can't — by accident or by a prompt-injection — commit to `main`, leak wallet secrets into the transcript, or fire a doomed/unfunded swap.

**Standing rule:** when a hook blocks, its message now names the **exact hook file (absolute path)** and states that the block is intentional. **Only a human edits or removes a hook** — the agent must never modify/disable/delete one to route around a block. If a rule is wrong, change the file in a reviewed commit.

## The hooks

| Hook | Event(s) | Fires on | Action |
|---|---|---|---|
| [`block-main-commits.sh`](block-main-commits.sh) | `PreToolUse` (Bash) | A `git commit`/`merge`/`rebase`/`reset --hard`/`reset HEAD~` while on `main`/`master`, or a `git push` (incl. `--force`) to `origin/main`/`master`. Detects the target via `git -C <path>` (worktree-safe). | **Blocker — exit 2** with a feature-branch fix. No-op outside a git repo or on non-Bash tools. |
| [`block-wallet-secrets.sh`](block-wallet-secrets.sh) | `UserPromptSubmit` (`--mode user`), `PreToolUse` (`--mode pre`), `PostToolUse` (`--mode tool`), `PostToolUseFailure` (`--mode failure`) | An adapter ([`scripts/wallet_secret_guard.py`](../../scripts/wallet_secret_guard.py)) over [`server/src/shared/secret_guard.py`](../../server/src/shared/secret_guard.py), the detector the MCP boundary uses. **user:** any 64-hex value, 12-word BIP-39 run or XRPL seed in the prompt. **pre:** the argument rules, plus known export commands, reveal routes and legacy secret files. **tool / failure:** a secret in a key-named field of the output or error, so tx hashes don't false-trip. | **Blocker.** A prompt is blocked (exit 2), a tool call is denied, a tool result is replaced with the same shape and every string withheld, and a failure stops continuation. Wallet import runs only in the custody administrator terminal ([`docs/custody.md`](../../docs/custody.md)). |
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

`secret_guard.py` is the only wallet-secret detector; the hook loads it by file path, so the hook and the MCP boundary cannot disagree. It reads the BIP-39 list from `eth_account` (present in the Docker image, which excludes `.claude/`) and falls back to the identical `bip39-english.txt` when the hook runs under a `python3` without `eth_account`. Its module docstring states the full rules. [`server/tests/unit/test_secret_guard.py`](../../server/tests/unit/test_secret_guard.py) checks that the two lists match and that the hook's prompt mode agrees with `find_in_text`; [`server/tests/unit/test_wallet_secret_guard.py`](../../server/tests/unit/test_wallet_secret_guard.py) covers the adapter's events.
