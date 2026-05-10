# Issues & PRs Triage — 2026-05-08

Inventory and proposed disposition for open issues and dependabot PRs at the start of the mangrove-agent rebrand. Final dispositions require user approval at the Task 7 decision gate.

## Open issues (6)

All opened 2026-03-12, no comments, labeled `enhancement`.

| # | Title | Proposed disposition | Rationale |
|---|-------|---------------------|-----------|
| 1 | Multi-chain support (Solana, Polygon) | **v2** | v1 is Base-only; multi-chain is a substantial feature. |
| 2 | WebSocket / streaming endpoint (x402-gated) | **v2** | x402-gated streaming is a follow-on capability; not blocking trading agent v1. |
| 3 | Rate limiting | **v1 if pre-launch capacity, else v2** | Real concern for a public-facing trading agent. Decision pending the security audit's findings. |
| 4 | Observability (logging, tracing, metrics) | **v1 if pre-launch capacity, else v2** | Trading agents benefit hugely from structured logs + metrics. Decision pending audit. |
| 5 | AWS variant (ECS/Fargate + Secrets Manager) | **wontfix** | `CLAUDE.md` explicitly states "Not on the v1 roadmap: cloud deployment. Local-first by intent." Close with a comment explaining. |
| 6 | Frontend template (Next.js companion) | **v2** | Belongs in a separate repo if pursued; not part of mangrove-agent. |

**Action when approved:**
```bash
gh issue edit <N> --repo <repo> --add-label v2 --remove-label enhancement
gh issue close 5 --repo <repo> --reason "not planned" \
  --comment "Local-first by design (CLAUDE.md). Cloud variants out of scope for v1. Reopen if scope changes."
```

## Open dependabot PRs (5) — all green CI

| # | Bump | CI | Risk | Proposed disposition |
|---|------|----|------|---------------------|
| 56 | pydantic >=2.0.0 → >=2.13.3 | lint-and-test SUCCESS | Low — pydantic v2 series is stable; minor bumps shouldn't break schema | **merge after rebrand PR lands** |
| 57 | pytest >=8.0.0 → >=9.0.3 | lint-and-test SUCCESS | Low — test-only dep | **merge after rebrand PR lands** |
| 58 | keyring >=24 → >=25.7.0 | lint-and-test SUCCESS | Medium — wallet master-key surface uses `keyring`; major bump needs eyeballs | **inspect after security audit; merge if no audit finding flags it** |
| 61 | ruff >=0.8.0 → >=0.15.12 | lint-and-test SUCCESS | Low — tool-only | **merge after rebrand PR lands** |
| 62 | cdp-sdk >=1.0.0 → >=1.44.0 | lint-and-test SUCCESS | High — cdp-sdk is the EVM signing library; the 2026-04-24 EIP-7702 drain came through this surface | **defer until security audit + manual review** |

PR #63 (CLOSED, not merged): "Commit workshop binaries" — duplicated work that landed via PR #64. No action.

**Merge order after rebrand:** #61 → #57 → #56 → #58 (after audit) → #62 (after audit + review).

## Roll-up actions for Task 7 decision gate

User to approve / modify:
1. Issue #5 → close as `wontfix`
2. Issues #1, #2, #6 → relabel `v2`
3. Issues #3, #4 → decide v1 vs v2 based on security audit findings
4. PRs #56, #57, #61 → ack to merge after rebrand
5. PRs #58, #62 → block until audit-reviewed
