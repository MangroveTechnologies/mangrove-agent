---
name: check-alignment
description: Check if a proposed change aligns with mangrove-agent's stated principles, architecture, and trading-bot safety invariants. Use when the user describes a change and asks "does this fit", "check alignment", "review this approach", or "/check-alignment <description>". Read-only analysis — does NOT modify anything. Adapted from the Mangrove workspace `check-alignment` skill with trading-bot-specific documents.
user_invocable: true
argument-hint: "<description of proposed change>"
---

# Check Alignment

Parse `$ARGUMENTS` as a natural-language description of a proposed change — could be a new feature, a refactor, a dependency change, a workflow tweak.

Review the proposal against each of these documents. For every one that exists, state whether the proposal **aligns** or **conflicts**. If it conflicts, cite the specific principle or decision it violates and suggest a concrete adjustment.

## Documents to check

### 1. `CLAUDE.md` — project identity + persona
- "What This Is" section: does the proposal still describe a local trading bot? Any scope drift toward cloud / hosted / multi-tenant breaks the v1 framing.
- "Architecture" section: dual-protocol (REST + MCP), three-tier access (free / auth / x402), service-layer pattern. Does the proposal respect these layers, or does it duplicate logic?
- "Project Context" + "Agent Identity": does the proposal change Tim's persona or the Sage agent? Flag if yes — persona is set in `CLAUDE.md` and changes there are explicit, deliberate edits, not side effects of feature work.

### 2. `.claude/rules/trading-bot-workflow.md` — the 9 operating principles
Check each principle if relevant:
1. Strategy-first, always (no manual-swap-by-default)
2. Bulk candidate evaluation (no label-pick)
3. Every recommendation cites Mangrove intelligence
4. Paper before live
5. Explicit confirmation at status transitions
6. Small first allocation
7. Wallet secrets NEVER in chat
8. **Signing is 1inch-only** (hard guard at `wallet_manager._validate_sign_target`)
9. **Testnet-first for learning** (Base Sepolia 84532 by default)

Plus the stage machine (Stage 0 tour → 1 Orient → 2 Author → 3 Review → 4 Paper → 4.5 Wallet → 5 Live → 6 Monitor). Does the change preserve the stage boundaries?

### 3. `.claude/rules/wallet-presentation.md` — wallet handling
- Never echo plaintext keys / vault_tokens in chat prose.
- Default to testnet for new wallets.
- All signing routes through `wallet_manager.sign()` which is hard-guarded to 1inch-only payloads.
- The signing guard allowlist (`_ONEINCH_ROUTERS`) must only be expanded with explicit review.

### 4. `.claude/rules/git-workflow.md` — branching + PR flow
- Feature branch + PR for every change (no direct commits to main).
- Worktree workflows use `git -C <path>` form so the hook recognizes the target.
- CI must pass before merge.

### 5. `docs/contributing.md` — extension workflow
- Routes + services + MCP tools + tests mirror the repo structure.
- Never duplicate business logic across REST and MCP — service layer owns it.

## For each document

If the document exists, give a per-doc paragraph:

```
### <doc name>
**Aligns** / **Conflicts** with <specific principle>
Reasoning: <one or two sentences>
Suggested adjustment (if conflict): <concrete change>
```

If the document doesn't exist, note it and skip:

```
### <doc name>
Not present in this repo — skipping.
```

## Summary

End with a one-sentence verdict: **Aligns**, **Conflicts**, or **Partially aligns** (with which specific concerns).

Do NOT modify any file. Do NOT implement the proposed change. This is analysis only — the user decides whether to proceed after reading the alignment check.
