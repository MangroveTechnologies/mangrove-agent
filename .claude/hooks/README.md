# mangrove-agent — Claude Code guardrail hooks

These hooks are **harness-enforced guardrails**: Claude Code runs them automatically (wired in [`.claude/settings.json`](../settings.json)) and the agent cannot disable them mid-session. They exist so the agent can't — by accident or by a prompt-injection — commit to `main`, leak wallet secrets into the transcript, or fire a doomed/unfunded swap.

**Standing rule:** when a hook blocks, its message now names the **exact hook file (absolute path)** and states that the block is intentional. **Only a human edits or removes a hook** — the agent must never modify/disable/delete one to route around a block. If a rule is wrong, change the file in a reviewed commit.

## The hooks

| Hook | Event(s) | Fires on | Action |
|---|---|---|---|
| [`block-main-commits.sh`](block-main-commits.sh) | `PreToolUse` (Bash) | A `git commit`/`merge`/`rebase`/`reset --hard`/`reset HEAD~` while on `main`/`master`, or a `git push` (incl. `--force`) to `origin/main`/`master`. Detects the target via `git -C <path>` (worktree-safe). | **exit 2** (blocks) with a feature-branch fix. No-op outside a git repo or on non-Bash tools. |
| [`block-wallet-secrets.sh`](block-wallet-secrets.sh) | `UserPromptSubmit` (`--mode user`) + `PostToolUse` (`--mode tool`) | A private-key shape (`0x`+64 hex) or a 12/24-word BIP-39 mnemonic. **user mode:** any match. **tool mode:** only when the key-shaped value sits in a key-named field (`private_key`, `seed_phrase`, …) — so tx hashes don't false-trip. | **exit 2** (blocks) and points to the out-of-band `stash-secret.sh` → vault-token import flow. Keeps key material out of the transcript + API context. |
| [`preflight-swap.sh`](preflight-swap.sh) | `PreToolUse` (`mcp__mangrove-agent__execute_swap`) | The swap's wallet holds **0** of the input token on the target chain. | **exit 2** (blocks) with chain/funding guidance. Passes through if balance is non-zero or can't be cleanly read (the SDK then surfaces its own error). |
| [`_hooklib.sh`](_hooklib.sh) | — (sourced) | n/a | Shared `block_footer` helper — appends the absolute hook path + don't-bypass notice to every block message. |

## Related: the code-level signing guard (not a hook)

Beyond these Claude-side hooks, there is a **hard signing guard in the agent itself**: [`server/src/services/wallet_manager.py::_validate_sign_target`](../../server/src/services/wallet_manager.py). Invariant: the agent signs **only** a 1inch `AggregationRouter` call or the ERC-20 `approve()` whose spender is a 1inch router — any other transaction shape (arbitrary EOA transfers, non-1inch contracts) is **refused at sign time**, on every chain. `preflight-swap.sh` is the Claude-side companion: it gates *when* a swap is attempted (balance present); `_validate_sign_target` restricts *what* may ever be signed.

## Adjusting a hook

Edit the file and commit (changes are visible in review). To disable one, remove its entry from [`.claude/settings.json`](../settings.json) and/or delete the script — **a human action, in a reviewed PR**. The agent will not do this for you.
