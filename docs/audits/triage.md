# Cross-Audit Triage — 2026-05-08

Synthesis of `security.md`, `code-quality.md`, `architecture.md`, `documentation.md`, and `issues-prs-triage.md` into a single prioritized action list. This is the document the user reviews at the **Task 7 decision gate** before any destructive scope cuts or rename steps.

## Must-fix before launch (BLOCKERS)

| ID | Source | File:line | Title | Effort | Plan task |
|----|--------|-----------|-------|--------|-----------|
| MF-1 | sec HIGH-1, arch MED-2 | `server/src/app.py:111-117` | x402 middleware bypasses payment when ANY `X-API-Key` header is present, without validating the key. Demo route `hello_mangrove` returns 200 without payment. | S (~1h) | new Task 11 step (security fix) |
| MF-2 | code HIGH-1 | `server/pyproject.toml:3` | `build-backend = "setuptools.backends._legacy:_Backend"` is invalid → `pip install -e .` fails for fresh installers. | XS (~10min) | new Task 11 step (build fix) |
| MF-3 | code HIGH-2 / doc HIGH-1 | three-way name clash | README headline `defi-agent` ≠ MCP server `app-in-a-box` ≠ plugin `app-in-a-box`. The `.claude/settings.json` hook matcher `mcp__defi-agent__execute_swap` doesn't match the actual namespace — preflight-swap.sh **does not fire today**. | M | covered by rename Tasks 15-17 |
| MF-4 | doc HIGH-2, scope | `README.md` Path B | "Path B — Fork as a scaffold" describes a flow being deleted in Task 8. | S | covered by Task 10 |

## Should-fix before launch

| ID | Source | File:line | Title | Effort | Plan task |
|----|--------|-----------|-------|--------|-----------|
| SF-1 | sec MED-1 | `server/src/shared/auth/middleware.py:31-33,44` | API key compared with `in valid_keys` (non-constant time). Use `secrets.compare_digest`. | XS | Task 11 step |
| SF-2 | sec MED-2 | `server/src/config.py:99-103` | Secret resolver crashes on malformed `secret:` strings (no bounds check on `parts[2]`). | XS | Task 11 step |
| SF-3 | code MED-1 | coverage gaps | `gcp_secret_utils.py` 31% / `x402/server.py` 49% / `crypto/fernet.py` 64% — all auth/payment/crypto-critical. Target ≥80%. | M | Task 12 |
| SF-4 | code MED-2 | 98 pytest warnings | pydantic deprecations from upstream SDK schemas (`daily_momentum_limit`, `weekly_momentum_limit`), `datetime.utcnow()` in `tests/e2e/test_paper_lifecycle.py:180`, structlog `format_exc_info` in `shared/logging.py`. | M | Task 12 |
| SF-5 | doc HIGH-2 | `CLAUDE.md` | "Not here for a trading bot?" line + "Branding" section reference removed flow. | XS | Task 10 |
| SF-6 | doc MED-2 | `tutorials/trading-app/08-monitor-troubleshoot-extend.md:247` | References removed `skills/onboard/SKILL.md`. | XS | Task 10 |
| SF-7 | sec rename-blocker | `.claude/hooks/preflight-swap.sh` | Hook keys off `mcp__defi-agent__execute_swap`; latent defect today, must update during rename. | XS | Task 15 step 4 |
| SF-8 | doc MED-5 | `docs/user-stories.md` | Not previously inventoried; one defi-agent ref. Read content, decide rename vs delete. | XS | Task 15 step 6 (preceded by content review) |

## Nice-to-have / defer to v2

| ID | Source | Title | Disposition |
|----|--------|-------|-------------|
| NF-1 | sec LOW-1 | `SecretUtils.get_secret` calls `sys.exit(1)` directly | v2 — refactor to typed exception |
| NF-2 | sec LOW-2 | Bare `except: pass` in scheduler/sqlite shutdown | v2 — narrow + log |
| NF-3 | sec LOW-3 | `try/except/continue` swallows order-intent validation | v2 — log offending payload |
| NF-4 | sec LOW-4 | 1inch router allowlist hardcoded V5/V6 | accepted as-is; add CI watcher in v2 |
| NF-5 | code LOW-1 | 8 functions at radon C complexity | v2 — refactor candidates |
| NF-6 | code LOW-2 | `wallet_manager.py` 715 lines mixed concerns | v2 — split into `wallet/{sign,vault,import}.py` |
| NF-7 | arch MED-3 | `mcp/registry.py` overloaded (FastMCP + discovery) | v2 — split metadata from transport adapter |
| NF-8 | arch LOW-2 | External SDK coupling direct, not behind a port | v2 — introduce `clients/{mangrove_markets,mangrove_ai}.py` adapters |
| NF-9 | issue #3 | Rate limiting | v2 (unless user upgrades to v1) |
| NF-10 | issue #4 | Observability (logging/tracing/metrics) | v2 (unless user upgrades to v1) |
| NF-11 | issue #1 | Multi-chain (Solana/Polygon) | v2 |
| NF-12 | issue #2 | WebSocket / streaming x402 | v2 |
| NF-13 | issue #6 | Frontend template | v2 (separate repo) |
| NF-14 | issue #5 | AWS variant | **wontfix** — `CLAUDE.md` says local-first |

## Open PR dispositions (recap from `issues-prs-triage.md`)

| PR | Bump | Recommendation |
|----|------|---------------|
| #56 pydantic | >=2.13.3 | merge after rebrand |
| #57 pytest  | >=9.0.3  | merge after rebrand |
| #58 keyring | >=25.7.0 | inspect (wallet master-key surface); merge if no audit flag |
| #61 ruff    | >=0.15.12 | merge after rebrand |
| #62 cdp-sdk | >=1.44.0 | **defer** — EIP-7702 surface, requires post-rebrand manual review |

## Scope-cut inventory (proposed for Task 8/9 deletion)

- `tutorials/scaffold-lifecycle/` (9 files)
- `.claude/skills/{onboard,plan,requirements,specification,architecture,tutorial}/` (6 skills)
- `.claude/hooks/check-onboard.sh` + matching `SessionStart` hook block in `.claude/settings.json`
- `init.sh`, `init-interactive.sh`, `branding.json`
- "Path B" subsection in `README.md`
- Branding/`/onboard` references in `CLAUDE.md`
- Top-level workshop binary duplicates (already in `docs/workshop/`): `bots-and-bytes-v5.{pdf,pptx}`, `workshop-run-of-show.{md,pdf}`, `workshop-setup-guide.{md,pdf}`, `.Rhistory`

## Rename inventory (proposed for Task 15)

- 22 files containing `app-in-a-box` (148 refs in tracked text — does not count this audit dir or the plan file or `docs/incidents/2026-04-24-eip7702-drain.md`)
- 28 files containing `defi-agent` (~107 refs)
- Three names → one: `mangrove-agent`. MCP namespace becomes `mcp__mangrove-agent__*`.

## Decisions required from user (Task 7 gate)

1. **Approve scope cuts** above as-is, or pull anything back into v1?
2. **Approve rename to `mangrove-agent`**, or pick a different name?
3. **Issue triage:** are #3 (rate limiting) and #4 (observability) v1 or v2?
4. **PR triage:** ack the merge order (#61 → #57 → #56 → #58 → #62) — or override?
5. **`docs/user-stories.md`:** one ref to defi-agent; not in original plan inventory. Treat as scope cleanup or rename target?

When user answers all five, execution proceeds to Task 8 (scope cleanup) and onward.
