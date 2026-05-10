# Mangrove-Agent Rebrand & Finalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take app-in-a-box from a dual-purpose ("trading bot OR generic FastAPI scaffold") repo to a single-purpose, production-ready trading agent published as `mangrove-agent`. Audit it for security, code quality, and architecture; cut everything not related to the trading agent; rename the repo, plugin, and MCP server consistently; verify end-to-end.

**Architecture:** The repo is a Python FastAPI server (`server/`) exposing both REST (`/api/v1/*`, `/api/x402/*`) and MCP (`/mcp`) over a shared service layer. A Claude Code plugin (`plugin/`) ships skills/hooks/commands for end-users. Local `.claude/` configures the in-repo dev experience. Cleanup removes the "scaffold" identity (init/onboard/rebrand pathway, scaffold-lifecycle tutorials, generic dev-cycle skills); rename touches every literal `app-in-a-box` and `defi-agent` reference.

**Tech Stack:** Python 3.11, FastAPI, FastMCP, APScheduler, SQLite, MangroveAI SDK, MangroveMarkets SDK, pytest, ruff, GitHub Actions, GCP Secret Manager.

---

## Pre-flight context

**Repo state at plan-write time (2026-05-08):**
- Branch: `main`, clean vs `origin/main` (`a=0 b=0`), 8 dirty files (popped from a sync-stash) — described in Task 1.
- Last merge: PR #66 `chore(deps): pin mangrovemarkets >=0.2.0,<0.3.0` (2026-05-05).
- Open issues (6): #1 Multi-chain, #2 WebSocket/x402-streaming, #3 Rate limiting, #4 Observability, #5 AWS variant, #6 Frontend template — all `enhancement`, none blockers for v1.
- Open PRs (5): #56 pydantic, #57 pytest, #58 keyring, #61 ruff, #62 cdp-sdk — all dependabot. Plus #63 (CLOSED-not-merged): "Commit workshop binaries" — duplicates work that landed via #64.
- Headline name in `README.md` is `defi-agent`. MCP server name in `server/src/mcp/server.py:34` is `"app-in-a-box"`. Plugin name in `plugin/.claude-plugin/plugin.json` is `app-in-a-box`. **Three different names in the same repo today.**

**What's in scope for "trading agent" (KEEP):**
- `server/src/api/routes/{strategies,signals,dex,market,on_chain,kb,reference_strategies,logs,discovery,hello_mangrove}.py`
- `server/src/services/*` (all 11 services)
- `server/src/mcp/{server,tools,registry}.py`
- `.claude/skills/{create-strategy,backtest,custom-signal,audit-security,tool-spec,check-alignment}/`
- `.claude/rules/{git-workflow,trading-bot-workflow,wallet-presentation}.md`
- `.claude/hooks/{preflight-swap,block-wallet-secrets,block-main-commits}.sh`
- `tutorials/trading-app/` (00–08, the workshop)
- `docs/{specification,architecture,configuration,strategy-lifecycle,verification-checklist,contributing}.md`
- `docs/workshop/` (run-of-show, setup-guide, slides, prereqs, facilitator-runbook)
- `docs/incidents/2026-04-24-eip7702-drain.md`
- `plugin/` (Claude Code plugin to be renamed)
- `scripts/{setup.sh,run-bare.sh,init-master-key.sh,confirm-backup.sh,reveal-secret.sh,stash-secret.sh,setup-mcp.sh,verify_quickstart.sh}`

**What's NOT in scope (CUT):**
- `tutorials/scaffold-lifecycle/` (00–08, generic dev-lifecycle scaffolding)
- `.claude/skills/{onboard,plan,requirements,specification,architecture,tutorial}/` (six generic scaffold skills)
- `.claude/hooks/check-onboard.sh` (only fires for the rebrand/onboard flow)
- `init.sh`, `init-interactive.sh` (rebrand scripts)
- `branding.json` (consumed only by `init.sh`)
- "Path B — Fork as a scaffold" section in `README.md`
- "Not here for a trading bot? This same codebase can be rebranded…" line in `CLAUDE.md`
- Top-level workshop binaries (`bots-and-bytes-v5.{pdf,pptx}`, `workshop-run-of-show.{md,pdf}`, `workshop-setup-guide.{md,pdf}`) — duplicates of `docs/workshop/*` after PR #64; reintroduced as untracked WIP, must be deleted.
- `.Rhistory` (artifact)

**Decision gates** (Tasks 7, 17, 19): explicit user approval required before bulk deletes, GitHub repo rename, and local directory rename. The plan stops at each gate.

---

## File-Structure Changes Map

| Path | Change | Owner Task |
|---|---|---|
| `tutorials/scaffold-lifecycle/` (entire dir, 9 files) | DELETE | 8 |
| `.claude/skills/{onboard,plan,requirements,specification,architecture,tutorial}/` | DELETE | 8 |
| `.claude/hooks/check-onboard.sh` | DELETE | 8 |
| `init.sh`, `init-interactive.sh`, `branding.json` | DELETE | 8 |
| `bots-and-bytes-v5.pdf`, `bots-and-bytes-v5.pptx`, `workshop-run-of-show.{md,pdf}`, `workshop-setup-guide.{md,pdf}`, `.Rhistory` | DELETE | 9 |
| `README.md` | MODIFY: drop Path B; rename headline | 10, 16 |
| `CLAUDE.md` | MODIFY: drop rebrand line + product-owner persona update; rename | 10, 16 |
| `server/src/mcp/server.py:34` | MODIFY: `"app-in-a-box"` → `"mangrove-agent"` | 16 |
| `server/src/mcp/tools.py:1` (docstring) | MODIFY: `defi-agent` → `mangrove-agent` | 16 |
| `plugin/.claude-plugin/plugin.json` | MODIFY: name + description | 16 |
| `plugin/.mcp.json` | MODIFY: server key | 16 |
| `plugin/README.md` | MODIFY: rename | 16 |
| `.mcp.json.example` | MODIFY: rename | 16 |
| `.claude/settings.json` (`mcp__defi-agent__execute_swap` matcher) | MODIFY: → `mcp__mangrove-agent__execute_swap` | 16 |
| `.claude/hooks/preflight-swap.sh` (defi-agent refs) | MODIFY | 16 |
| `.claude/rules/trading-bot-workflow.md` | MODIFY | 16 |
| `.claude/skills/{audit-security,tool-spec,check-alignment}/SKILL.md` | MODIFY: app-in-a-box refs | 16 |
| `.claude/agents/product-owner.md` | MODIFY: rename | 16 |
| `server/src/config/{local-example,test,dev,prod}-config.json` | MODIFY: defi-agent refs (logger names, etc.) | 16 |
| `server/src/api/router.py`, `server/src/shared/errors.py`, `server/src/services/backtest_service.py` | MODIFY: defi-agent refs | 16 |
| `server/tests/test_mcp.py`, `server/tests/test_mcp_auth.py`, `server/tests/unit/test_wallet_manager.py` | MODIFY: rename + assertion updates | 16 |
| `server/pyproject.toml` | MODIFY: project name | 16 |
| `docker-compose.yml` | MODIFY: service name | 16 |
| `Dockerfile` (server/) | inspect for refs | 16 |
| `docs/{specification,architecture,configuration,verification-checklist}.md` | MODIFY | 16 |
| `docs/workshop/{run-of-show,setup-guide,prereqs,facilitator-runbook}.md` | MODIFY: rename refs (PDFs are bundled WIP — regen out of scope) | 16 |
| `tutorials/trading-app/01–08` | MODIFY: rename refs | 16 |
| `.github/workflows/ci.yml` | INSPECT, MODIFY if any name refs | 16 |
| `CONTRIBUTING.md` | MODIFY: rename | 16 |
| `docs/plans/2026-05-08-mangrove-agent-rebrand.md` | THIS PLAN — keep | — |
| `docs/audits/{security,code-quality,architecture,documentation}.md` | CREATE | 3, 4, 5, 6 |
| `docs/audits/triage.md` | CREATE | 7 |
| GitHub repo: `MangroveTechnologies/app-in-a-box` | RENAME → `mangrove-agent` | 17 |
| Local clone: `/Users/darrahts/mangrove-workspace/app-in-a-box` | RENAME → `mangrove-agent` | 19 |
| Workspace `CLAUDE.md` (top-level) | MODIFY: short-name + table row | 19 |

---

## Task 1: Worktree setup, baseline, plan-into-branch

**Files:**
- Create: `/tmp/mangrove-agent-rebrand/` (worktree)
- Move: `docs/plans/2026-05-08-mangrove-agent-rebrand.md` (this file) into the worktree
- Test: `server/tests/`

- [ ] **Step 1: Confirm baseline at HEAD of origin/main is clean and tests pass**

```bash
cd /Users/darrahts/mangrove-workspace/app-in-a-box
git fetch origin
git rev-list --left-right --count HEAD...origin/main  # expect 0	0
```

Expected: `0	0`.

- [ ] **Step 2: Create the worktree off origin/main**

```bash
cd /Users/darrahts/mangrove-workspace/app-in-a-box
git worktree add -b feature/mangrove-agent-rebrand /tmp/mangrove-agent-rebrand origin/main
cd /tmp/mangrove-agent-rebrand
```

Expected: new worktree created, branch tracking nothing yet.

- [ ] **Step 3: Move this plan file into the worktree (it currently lives in the user's checkout as untracked)**

```bash
mkdir -p /tmp/mangrove-agent-rebrand/docs/plans
cp /Users/darrahts/mangrove-workspace/app-in-a-box/docs/plans/2026-05-08-mangrove-agent-rebrand.md \
   /tmp/mangrove-agent-rebrand/docs/plans/
cd /tmp/mangrove-agent-rebrand
git add docs/plans/2026-05-08-mangrove-agent-rebrand.md
git commit -m "docs(plan): mangrove-agent rebrand & finalization plan"
```

- [ ] **Step 4: Verify clean baseline tests pass before any change**

```bash
cd /tmp/mangrove-agent-rebrand/server
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev] 2>&1 | tail -3
pytest -q 2>&1 | tail -10
ruff check . 2>&1 | tail -5
```

Expected: all tests pass, ruff clean. **If anything fails on a clean baseline, stop and report — that's a pre-existing problem to fix before continuing.**

- [ ] **Step 5: Push the branch (so the plan is visible on GH for review)**

```bash
cd /tmp/mangrove-agent-rebrand
git push -u origin feature/mangrove-agent-rebrand
```

Expected: branch pushed, no PR yet.

---

## Task 2: Triage open issues + open PRs

**Files:**
- Modify: GitHub issues + PRs (no local files)

- [ ] **Step 1: Inventory open issues with current state**

```bash
gh issue list --repo MangroveTechnologies/app-in-a-box --state open \
  --json number,title,labels,createdAt,comments
```

Expected: 6 issues (#1–#6), all `enhancement`.

- [ ] **Step 2: Add `v2` or `wontfix-v1` label to each, with a comment explaining the v1 scope**

Decisions to apply (gated on user approval in Task 7):
- #1 Multi-chain (Solana, Polygon) → `v2` (post-v1)
- #2 WebSocket/streaming x402 → `v2`
- #3 Rate limiting → `v1` if user wants pre-launch, else `v2`
- #4 Observability → `v1` if user wants pre-launch, else `v2`
- #5 AWS variant → `wontfix` (CLAUDE.md explicitly says local-first only)
- #6 Frontend template → `v2` (separate repo)

Apply via:
```bash
gh issue edit <N> --repo MangroveTechnologies/app-in-a-box --add-label v2 --remove-label enhancement
gh issue comment <N> --repo MangroveTechnologies/app-in-a-box --body "Deferring to v2; v1 scope is single-chain Base + REST/MCP only."
```

- [ ] **Step 3: Inventory open PRs**

```bash
gh pr list --repo MangroveTechnologies/app-in-a-box --state open \
  --json number,title,createdAt,isDraft,statusCheckRollup
```

Expected: 5 dependabot PRs.

- [ ] **Step 4: For each dependabot PR, check CI green and decide merge/close**

```bash
for n in 56 57 58 61 62; do
  echo "=== PR #$n ==="
  gh pr view $n --repo MangroveTechnologies/app-in-a-box --json title,statusCheckRollup,mergeable
done
```

Default policy: dependabots with green CI and pinned major/minor compatible bumps → merge in dependency order (lockstep); ones that bump majors → defer until covered by audit. **Do not merge any PR before audits in Task 3 land.** Mark this step as DEFERRED until after Task 7's decision gate.

- [ ] **Step 5: Note PR #63 stays closed (duplicate of merged #64)**

No action — already closed.

- [ ] **Step 6: Commit a triage note**

```bash
cd /tmp/mangrove-agent-rebrand
mkdir -p docs/audits
cat > docs/audits/issues-prs-triage.md <<'EOF'
# Issues & PRs triage — 2026-05-08

## Open issues
| # | Title | Disposition |
|---|-------|-------------|
| 1 | Multi-chain | v2 |
| 2 | WebSocket streaming | v2 |
| 3 | Rate limiting | TBD pending audit |
| 4 | Observability | TBD pending audit |
| 5 | AWS variant | wontfix (local-first) |
| 6 | Frontend template | v2 |

## Open PRs (dependabot)
| # | Bump | Disposition |
|---|------|-------------|
| 56 | pydantic >=2.13.3 | merge after audit if green |
| 57 | pytest >=9.0.3 | merge after audit if green |
| 58 | keyring >=25.7.0 | merge after audit if green |
| 61 | ruff >=0.15.12 | merge after audit if green |
| 62 | cdp-sdk >=1.44.0 | inspect before merge — wallet-adjacent |
EOF
git add docs/audits/issues-prs-triage.md
git commit -m "docs(audits): triage open issues and PRs"
```

---

## Task 3: Security audit (delegated to security-audit-agent)

**Files:**
- Create: `docs/audits/security.md`

- [ ] **Step 1: Dispatch security-audit-agent**

Use the Agent tool with `subagent_type: "security-audit-agent"`. Prompt:

> Perform a security audit of `/tmp/mangrove-agent-rebrand` (a fork of MangroveTechnologies/app-in-a-box, a Python FastAPI + MCP trading bot that signs and submits onchain swaps via the MangroveMarkets SDK). Pay particular attention to: (1) wallet handling — `server/src/services/wallet_manager.py`, `server/src/services/secret_vault.py`, `.claude/hooks/block-wallet-secrets.sh`, `.claude/hooks/preflight-swap.sh`, `scripts/{init-master-key,reveal-secret,stash-secret}.sh`. (2) signing surface — `server/src/services/order_executor.py`. (3) auth — `server/src/shared/auth/middleware.py` and `has_valid_api_key`. (4) x402 path — `server/src/api/x402/*`. (5) Secret resolution — `server/src/shared/gcp_secret_utils.py` and the `secret:name:property` JSON-config syntax. The 2026-04-24 EIP-7702 drain post-mortem at `docs/incidents/2026-04-24-eip7702-drain.md` is essential context — verify the documented controls (1inch-only signing guard, swap preflight) are actually enforced in code paths. Run bandit, pip-audit, safety, semgrep. Output severity-rated findings with file:line citations to `docs/audits/security.md`. Do not modify code.

- [ ] **Step 2: Read the agent's output and verify it landed**

```bash
ls -la /tmp/mangrove-agent-rebrand/docs/audits/security.md
wc -l /tmp/mangrove-agent-rebrand/docs/audits/security.md
```

Expected: file exists, non-trivial length.

- [ ] **Step 3: Commit the audit**

```bash
cd /tmp/mangrove-agent-rebrand
git add docs/audits/security.md
git commit -m "docs(audits): security audit findings"
```

---

## Task 4: Code-quality audit (delegated to code-analyst)

**Files:**
- Create: `docs/audits/code-quality.md`

- [ ] **Step 1: Dispatch code-analyst agent**

Use the Agent tool with `subagent_type: "code-analyst"`. Prompt:

> Analyze code quality across `/tmp/mangrove-agent-rebrand/server/src/` and the in-repo Claude Code surface (`.claude/`, `plugin/`). Run vulture (dead code), grimp (cycle detection), pydeps (import graph), import-linter (architecture rules), radon (complexity), ruff, pyright. Apply reasoning over: naming consistency (the codebase has three names today — `app-in-a-box`, `defi-agent`, plus the package name in `pyproject.toml` — flag every divergence), test coverage gaps, slop/incomplete code, doc/code drift. Output a severity-rated findings list with file:line citations to `docs/audits/code-quality.md`. Do not modify code.

- [ ] **Step 2: Verify and commit**

```bash
cd /tmp/mangrove-agent-rebrand
ls -la docs/audits/code-quality.md
git add docs/audits/code-quality.md
git commit -m "docs(audits): code-quality audit findings"
```

---

## Task 5: Architecture audit (delegated to software-audit-agent)

**Files:**
- Create: `docs/audits/architecture.md`

- [ ] **Step 1: Dispatch software-audit-agent**

Use the Agent tool with `subagent_type: "software-audit-agent"`. Prompt:

> Audit the architecture of `/tmp/mangrove-agent-rebrand`. Evaluate: (1) the documented "service-layer pattern" (`server/src/services/` shared between REST routes and MCP tools) — verify routes and MCP tools genuinely call the same services and don't duplicate business logic. Specifically compare `server/src/api/routes/dex.py` to `_register_dex` in `server/src/mcp/tools.py`, and `server/src/api/routes/strategies.py` to `_register_strategy`. (2) Layering: do `server/src/services/` ever import from `server/src/api/`? That would invert the dependency direction. (3) Coupling between `wallet_manager`, `secret_vault`, `order_executor`. (4) The MCP registry pattern (`server/src/mcp/registry.py`) — is it doing too much, or coherent? (5) Configuration loading (`server/src/config/`) — does the loader work cleanly across the four envs (local/test/dev/prod)? Use code-analyst tools (dep-graph, trace-imports, import-linter) for evidence. Output a severity-rated findings list with file:line citations to `docs/audits/architecture.md`. Do not modify code.

- [ ] **Step 2: Verify and commit**

```bash
cd /tmp/mangrove-agent-rebrand
ls -la docs/audits/architecture.md
git add docs/audits/architecture.md
git commit -m "docs(audits): architecture audit findings"
```

---

## Task 6: Documentation audit

**Files:**
- Create: `docs/audits/documentation.md`

- [ ] **Step 1: Verify quickstart end-to-end against code**

```bash
cd /tmp/mangrove-agent-rebrand
cat README.md  # capture the quickstart commands
cat scripts/setup.sh  # verify it does what README says
cat tutorials/trading-app/03-setup.md  # tutorial setup chapter
```

- [ ] **Step 2: Run the quickstart commands in a clean shell, recording any divergence**

```bash
# Fresh shell
cd /tmp/mangrove-agent-rebrand
./scripts/verify_quickstart.sh 2>&1 | tee /tmp/quickstart-log.txt
```

Expected: `verify_quickstart.sh` was added in PR #51-area to mechanize this. If it fails on a clean baseline, that's a finding.

- [ ] **Step 3: Cross-check tutorial chapters 01–08 against current API surface**

For each chapter, identify:
- API endpoints referenced — confirm route file exists, signature unchanged
- Skill invocations referenced (`/create-strategy`, `/backtest`, etc.) — confirm skill exists in `.claude/skills/`
- File paths referenced — confirm they exist
- Commands referenced — confirm they're in `scripts/`

```bash
for chap in tutorials/trading-app/0[1-8]*.md; do
  echo "=== $chap ==="
  grep -nE "GET |POST |/api/|scripts/|mcp__|\\\\bcurl\\\\b" "$chap" | head -10
done > /tmp/tutorial-refs.txt
```

Compare to actual route/script/tool inventory:
```bash
grep -rn "@router\\." /tmp/mangrove-agent-rebrand/server/src/api/routes/ | head -30
ls /tmp/mangrove-agent-rebrand/scripts/
grep -n "register_tool" /tmp/mangrove-agent-rebrand/server/src/mcp/tools.py | head -20
```

- [ ] **Step 4: Verify CLAUDE.md doesn't reference removed/renamed surface**

```bash
grep -nE "Path B|onboard|init\\.sh|branding\\.json|scaffold-lifecycle|app-in-a-box|defi-agent" \
  /tmp/mangrove-agent-rebrand/CLAUDE.md
```

Expected: enumerate every match — these all need fixing in Task 11/16.

- [ ] **Step 5: Write findings to docs/audits/documentation.md**

Document for each issue: file, line, the divergence, the fix. Include:
- README headline shows `defi-agent`, server names itself `app-in-a-box`, plugin is `app-in-a-box` — three-way name clash.
- README "Path B" describes a flow being removed.
- CLAUDE.md "Not here for a trading bot? …rebrand flow" line references `/onboard` skill being removed.
- Any quickstart command that fails or lies.
- Any tutorial chapter referring to a removed skill or moved file.

```bash
cd /tmp/mangrove-agent-rebrand
git add docs/audits/documentation.md
git commit -m "docs(audits): documentation audit findings"
```

---

## Task 7: Compile triage + DECISION GATE

**Files:**
- Create: `docs/audits/triage.md`

- [ ] **Step 1: Read all four audit outputs**

```bash
cd /tmp/mangrove-agent-rebrand
ls docs/audits/
wc -l docs/audits/*.md
```

- [ ] **Step 2: Synthesize a single triage document**

Write `docs/audits/triage.md` with three sections:
1. **Must-fix before launch** (severity high+ from any audit)
2. **Should-fix** (severity medium)
3. **Defer to v2** (low / nice-to-have)

For each item: source audit, file:line, proposed fix, estimated effort.

- [ ] **Step 3: Commit triage**

```bash
git add docs/audits/triage.md
git commit -m "docs(audits): cross-audit triage"
```

- [ ] **Step 4: STOP — present triage + scope-cut + rename plan to user**

Post to user:
- Summary of triage (counts by severity)
- Confirmed deletions list from "What's NOT in scope" section above
- Rename plan: repo `app-in-a-box` → `mangrove-agent`; plugin `app-in-a-box` → `mangrove-agent`; MCP server `app-in-a-box` → `mangrove-agent`; README headline `defi-agent` → `mangrove-agent`
- Open dependabot PRs disposition

**Do not proceed past this step without explicit user "approved" or equivalent.** The next tasks make destructive changes.

---

## Task 8: Cut scaffold identity (skills + tutorials + onboard hook)

**Files:**
- Delete: `tutorials/scaffold-lifecycle/` (9 files)
- Delete: `.claude/skills/{onboard,plan,requirements,specification,architecture,tutorial}/`
- Delete: `.claude/hooks/check-onboard.sh`
- Delete: `init.sh`, `init-interactive.sh`, `branding.json`
- Modify: `.claude/settings.json` (remove SessionStart `check-onboard.sh` entry)

- [ ] **Step 1: Remove scaffold-lifecycle tutorials**

```bash
cd /tmp/mangrove-agent-rebrand
git rm -r tutorials/scaffold-lifecycle/
```

- [ ] **Step 2: Remove generic scaffold skills**

```bash
cd /tmp/mangrove-agent-rebrand
git rm -r .claude/skills/onboard/ .claude/skills/plan/ .claude/skills/requirements/ \
         .claude/skills/specification/ .claude/skills/architecture/ .claude/skills/tutorial/
```

- [ ] **Step 3: Remove the onboard hook script**

```bash
git rm .claude/hooks/check-onboard.sh
```

- [ ] **Step 4: Remove the SessionStart hook entry from `.claude/settings.json`**

Edit `.claude/settings.json` to remove the `SessionStart` block entirely (it only contained `check-onboard.sh`):

```json
{
  "hooks": {
    "UserPromptSubmit": [...],
    "PostToolUse": [...],
    "PreToolUse": [...]
  }
}
```

(Verbatim: drop the `"SessionStart": [...]` key and value, leave the other three keys.)

- [ ] **Step 5: Remove rebrand scripts and branding manifest**

```bash
git rm init.sh init-interactive.sh branding.json
```

- [ ] **Step 6: Run tests to verify nothing depended on removed code**

```bash
cd /tmp/mangrove-agent-rebrand/server
source .venv/bin/activate
pytest -q 2>&1 | tail -10
```

Expected: same green baseline as Task 1, Step 4. If tests fail, an import or fixture is referencing a removed file — fix the reference (likely in a test that imported something from a deleted skill).

- [ ] **Step 7: Commit**

```bash
cd /tmp/mangrove-agent-rebrand
git add -A
git commit -m "chore(scope): remove scaffold-lifecycle, onboard, and rebrand pathway

Single-purpose trading agent — drop the 'fork as scaffold' identity.
Removed:
- tutorials/scaffold-lifecycle/
- .claude/skills/{onboard,plan,requirements,specification,architecture,tutorial}/
- .claude/hooks/check-onboard.sh + matching SessionStart hook entry
- init.sh, init-interactive.sh, branding.json"
```

---

## Task 9: Remove top-level workshop binary duplicates

**Files:**
- Delete: `bots-and-bytes-v5.pdf`, `bots-and-bytes-v5.pptx`, `workshop-run-of-show.md`, `workshop-run-of-show.pdf`, `workshop-setup-guide.md`, `workshop-setup-guide.pdf`, `.Rhistory`

These are stale duplicates after PR #64 collocated them into `docs/workshop/`. They came back as untracked WIP in the user's checkout but the canonical location is `docs/workshop/`.

- [ ] **Step 1: Confirm `docs/workshop/` has the canonical versions**

```bash
cd /tmp/mangrove-agent-rebrand
ls -la docs/workshop/
```

Expected: `slides.pdf`, `run-of-show.{md,pdf}`, `setup-guide.{md,pdf}`, `prereqs.md`, `facilitator-runbook.md`.

- [ ] **Step 2: Delete the top-level duplicates and the .Rhistory artifact**

```bash
# These files only exist in the user's main checkout (not the worktree HEAD).
# Run this in /tmp/mangrove-agent-rebrand only if any of these files are present:
cd /tmp/mangrove-agent-rebrand
rm -f bots-and-bytes-v5.pdf bots-and-bytes-v5.pptx \
      workshop-run-of-show.md workshop-run-of-show.pdf \
      workshop-setup-guide.md workshop-setup-guide.pdf \
      .Rhistory
git status --short
```

Expected: clean (these files weren't in HEAD).

- [ ] **Step 3: Add a gitignore entry to prevent reintroduction**

Append to `.gitignore`:
```
# Stale workshop binaries — canonical copies live under docs/workshop/
/bots-and-bytes-v5.*
/workshop-run-of-show.*
/workshop-setup-guide.*
.Rhistory
```

- [ ] **Step 4: Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore top-level workshop duplicates (canonical: docs/workshop/)"
```

- [ ] **Step 5: Clean the user's main checkout too** (after this PR merges, or now if they want)

To be done by the user post-merge in their actual `app-in-a-box/` directory:
```bash
cd /Users/darrahts/mangrove-workspace/app-in-a-box
rm -f bots-and-bytes-v5.pdf bots-and-bytes-v5.pptx \
      workshop-run-of-show.md workshop-run-of-show.pdf \
      workshop-setup-guide.md workshop-setup-guide.pdf \
      .Rhistory
```

Document this in the PR description.

---

## Task 10: Update CLAUDE.md and README.md to single-purpose

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

- [ ] **Step 1: Edit `CLAUDE.md` — remove the "Not here for a trading bot?" line**

Open `/tmp/mangrove-agent-rebrand/CLAUDE.md`. Delete this exact block:

```
> Not here for a trading bot? This same codebase can be rebranded into any FastAPI + Claude Code app. Run `/onboard` inside Claude Code to start the rebrand flow.
```

Also delete the entire **"Branding"** section near the bottom (it references `branding.json` and `init.sh`, both removed):

```
## Branding

Edit `branding.json` + `assets/`, then run `./init.sh` to propagate.
```

- [ ] **Step 2: Edit `README.md` — remove "Path B"**

In `/tmp/mangrove-agent-rebrand/README.md`, delete the entire **"Getting the Code"** "Path B" subsection (everything from `**Path B — Fork as a scaffold (you're building something else on top):**` through the end of its numbered list, ending at `Dev-lifecycle scaffolding…lives in tutorials/scaffold-lifecycle/.`).

Also rename the **"Path A — Use as-is"** heading to just **"Quickstart"** (since there's now only one path), and shift the flow.

- [ ] **Step 3: Edit `README.md` — remove the "Workshop attendee?" pointer to scaffold-lifecycle**

Search for any line referencing `scaffold-lifecycle` and delete it.

- [ ] **Step 4: Run tests + ruff (no Python changed, but verify imports still resolve)**

```bash
cd /tmp/mangrove-agent-rebrand/server
source .venv/bin/activate
pytest -q 2>&1 | tail -5
ruff check . 2>&1 | tail -3
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
cd /tmp/mangrove-agent-rebrand
git add CLAUDE.md README.md
git commit -m "docs: drop scaffold/rebrand pathway from CLAUDE.md and README"
```

---

## Task 11: Apply must-fix audit findings (security + code + arch)

**Files:**
- Modify: per `docs/audits/triage.md` "Must-fix before launch" section

This task is shaped by the audits. The plan can't enumerate fixes that don't exist yet. **The expected pattern per finding:**

- [ ] **Step 1: Read `docs/audits/triage.md` "Must-fix" section**

```bash
cat /tmp/mangrove-agent-rebrand/docs/audits/triage.md | sed -n '/## Must-fix/,/## Should-fix/p'
```

- [ ] **Step 2: For each must-fix item, write a failing test that exercises the bug**

(TDD per finding. Test shape depends on the finding — security: a request that should be rejected; arch: an import that shouldn't resolve; code: a function returning wrong output.)

- [ ] **Step 3: Run the test to verify it fails**

```bash
pytest server/tests/path/to/test.py::test_specific_finding -v
```

Expected: FAIL.

- [ ] **Step 4: Implement the fix per the finding's "proposed fix" field**

- [ ] **Step 5: Re-run the test**

```bash
pytest server/tests/path/to/test.py::test_specific_finding -v
```

Expected: PASS.

- [ ] **Step 6: Run full suite + ruff + the related audit's tooling to verify no regression**

```bash
pytest -q 2>&1 | tail -5
ruff check . 2>&1 | tail -3
# If the finding came from bandit:
bandit -r server/src/ 2>&1 | tail -10
```

- [ ] **Step 7: Commit per finding (frequent commits)**

```bash
git add server/...
git commit -m "fix(audit): <finding-id> <short description>"
```

Repeat steps 2–7 for each must-fix item.

---

## Task 12: Apply should-fix audit findings

Same shape as Task 11 but iterating the "Should-fix" section. Cherry-pick by impact; defer the rest to v2 by adding to the triage doc with a `v2-deferred` note.

- [ ] **Step 1: Per item: TDD-fix loop (mirror Task 11 steps 2–7)**

- [ ] **Step 2: For deferred items, append to `docs/audits/triage.md`** under a "Deferred to v2" section, then file a GH issue with the `v2` label.

```bash
gh issue create --repo MangroveTechnologies/<new-name> \
  --label v2 \
  --title "<finding title>" \
  --body "<finding body with file:line and proposed fix>"
```

(Note: `<new-name>` will be `mangrove-agent` after Task 17. If running Task 12 before rename, use `app-in-a-box` and re-tag after rename.)

---

## Task 13: Decide and merge open dependabot PRs

**Files:**
- Modify: `server/requirements.txt`, `server/pyproject.toml` (via PR merges, not direct edit)

- [ ] **Step 1: Re-check each open dependabot PR**

```bash
for n in 56 57 58 61 62; do
  echo "=== #$n ==="
  gh pr view $n --repo MangroveTechnologies/app-in-a-box \
    --json title,statusCheckRollup,mergeable,headRefName
done
```

- [ ] **Step 2: Merge ones with green CI in dependency order**

Order: ruff (#61) first (no runtime impact), then pytest (#57), then keyring (#58 — adjacent to wallet code, double-check audit didn't flag it), then pydantic (#56 — schema impact), then cdp-sdk (#62 — wallet path, defer until x402/wallet audit findings are addressed).

For each: rebase on top of the rebrand branch's main *after* rebrand merges, OR cherry-pick the dependency bump into the rebrand branch. Recommended: **let dependabots auto-rebase against `main` and merge them after Task 17 ships**, so the rebrand PR isn't blocked.

- [ ] **Step 3: Document dispositions back into `docs/audits/issues-prs-triage.md`**

```bash
# Update each row with final disposition
git add docs/audits/issues-prs-triage.md
git commit -m "docs(triage): final dependabot PR dispositions"
```

---

## Task 14: Verify post-cleanup baseline before rename

This is a checkpoint — never start Task 15+ on a non-green baseline.

- [ ] **Step 1: Full test suite**

```bash
cd /tmp/mangrove-agent-rebrand/server
source .venv/bin/activate
pytest -q 2>&1 | tail -10
```

Expected: green.

- [ ] **Step 2: ruff**

```bash
cd /tmp/mangrove-agent-rebrand
ruff check . 2>&1 | tail -5
```

Expected: clean.

- [ ] **Step 3: setup.sh dry-run on a fresh clone**

```bash
cd /tmp
git clone /tmp/mangrove-agent-rebrand /tmp/agent-fresh
cd /tmp/agent-fresh
./scripts/verify_quickstart.sh 2>&1 | tail -20
rm -rf /tmp/agent-fresh
```

Expected: green.

- [ ] **Step 4: server boot smoke**

```bash
cd /tmp/mangrove-agent-rebrand/server
source .venv/bin/activate
python -m uvicorn src.app:app --port 9080 &
SERVER_PID=$!
sleep 4
curl -s http://localhost:9080/health | python -m json.tool
curl -s http://localhost:9080/api/v1/discovery | python -m json.tool | head -20
kill $SERVER_PID
```

Expected: `/health` returns ok, `/discovery` returns a tool list.

---

## Task 15: Rename in code — internal references (single commit per file group)

**Files:**
- Modify: 22+ files containing `app-in-a-box` literal
- Modify: 20+ files containing `defi-agent` literal

Strategy: do this as several commits grouped by surface (server code, tests, .claude, plugin, docs, config) so each commit is reviewable. **No `sed -i` blanket rewrites** — every change must be inspected because some contexts are URLs (e.g., `https://github.com/MangroveTechnologies/app-in-a-box` will become `mangrove-agent` only after Task 17 runs the GH rename).

- [ ] **Step 1: Server code — `server/src/`**

Files to edit:
- `server/src/mcp/server.py:34` — change `"app-in-a-box"` to `"mangrove-agent"` (the FastMCP server name; this is what shows up in `mcp__<name>__<tool>` matchers).
- `server/src/mcp/tools.py:1` (docstring) — `defi-agent` → `mangrove-agent`.
- `server/src/api/router.py` — find `defi-agent` refs (likely OpenAPI title/description), update.
- `server/src/shared/errors.py` — find `defi-agent` refs in error codes/messages, update.
- `server/src/services/backtest_service.py` — find `defi-agent` ref (likely a User-Agent header for outbound MangroveAI calls), update.
- `server/src/config/{local-example,test,dev,prod}-config.json` — `defi-agent` refs (logger names, etc.).
- `server/pyproject.toml` — project `name`. Decide between `mangrove-agent` (matches new repo) and `mangrove_agent` (Python convention requires underscore for module names; `name` field accepts hyphen). Use hyphen `mangrove-agent` for the project name; keep package import path as `src` (no module rename needed since imports use `from src.x import y`).

For each file, inspect the context first (`grep -n "app-in-a-box\|defi-agent" <file>`), then `Edit` the literal in place. Run tests after each:
```bash
pytest -q 2>&1 | tail -5
```
Expected: green.

Commit:
```bash
git add server/
git commit -m "refactor(rename): server code refs app-in-a-box / defi-agent → mangrove-agent"
```

- [ ] **Step 2: Tests — `server/tests/`**

Files: `test_mcp.py`, `test_mcp_auth.py`, `unit/test_wallet_manager.py`. Likely have assertions on the MCP server name (e.g., `assert server.name == "app-in-a-box"`).

```bash
grep -n "app-in-a-box\|defi-agent" server/tests/**/*.py
```

Edit each match; run the relevant test; commit.

```bash
pytest server/tests/ -q 2>&1 | tail -5
git add server/tests/
git commit -m "test(rename): update assertions to mangrove-agent"
```

- [ ] **Step 3: Plugin — `plugin/`**

Files:
- `plugin/.claude-plugin/plugin.json` — change `"name": "app-in-a-box"` → `"name": "mangrove-agent"`. Update `description` to "Claude Code plugin for the Mangrove trading agent".
- `plugin/.mcp.json` — change the `"app-in-a-box"` server key to `"mangrove-agent"`.
- `plugin/README.md` — global rename.

```bash
git add plugin/
git commit -m "refactor(rename): plugin manifest + README → mangrove-agent"
```

- [ ] **Step 4: `.claude/` — settings, hooks, rules, skills, agent**

Files:
- `.claude/settings.json` — change `mcp__defi-agent__execute_swap` matcher to `mcp__mangrove-agent__execute_swap`.
- `.claude/hooks/preflight-swap.sh` — replace `defi-agent` refs.
- `.claude/rules/trading-bot-workflow.md` — replace refs.
- `.claude/skills/{audit-security,tool-spec,check-alignment,create-strategy,backtest,custom-signal}/SKILL.md` — replace refs.
- `.claude/agents/product-owner.md` — rename project from app-in-a-box to mangrove-agent.

```bash
git add .claude/
git commit -m "refactor(rename): .claude config + skills + rules → mangrove-agent"
```

- [ ] **Step 5: Top-level — `CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, `.mcp.json.example`, `docker-compose.yml`**

- `CLAUDE.md`: rename project header from `App-in-a-Box` to `Mangrove Agent`, replace literal refs. Update product-owner persona block if needed.
- `README.md`: change headline `defi-agent` → `mangrove-agent`. Update repo URL refs (these will 404 until Task 17 redirects them — GH preserves redirects on rename, so this is safe; do NOT change the URL host or org).
- `CONTRIBUTING.md`: replace refs.
- `.mcp.json.example`: change server key.
- `docker-compose.yml`: change service name.

```bash
git add CLAUDE.md README.md CONTRIBUTING.md .mcp.json.example docker-compose.yml
git commit -m "refactor(rename): top-level docs and compose → mangrove-agent"
```

- [ ] **Step 6: Docs — `docs/`**

```bash
grep -rln "app-in-a-box\|defi-agent" docs/
```

For each file, inspect and edit. Likely affected: `docs/specification.md`, `docs/architecture.md`, `docs/configuration.md`, `docs/verification-checklist.md`, `docs/contributing.md`, `docs/workshop/{run-of-show,setup-guide,prereqs,facilitator-runbook}.md`.

**Workshop PDFs are bundled binaries and can't be `sed`'d.** Add a note to the PR description: "Workshop PDFs in docs/workshop/*.pdf still say 'app-in-a-box' inside the slides — regenerate from the .md sources in a follow-up PR (out of scope for this rename)."

```bash
git add docs/
git commit -m "refactor(rename): docs → mangrove-agent (PDFs deferred)"
```

- [ ] **Step 7: Tutorials — `tutorials/trading-app/`**

```bash
grep -rln "app-in-a-box\|defi-agent" tutorials/
```

Edit each match. Run the verify script:
```bash
./scripts/verify_quickstart.sh 2>&1 | tail -10
```
Expected: green.

```bash
git add tutorials/
git commit -m "refactor(rename): trading-app tutorials → mangrove-agent"
```

- [ ] **Step 8: Scripts**

```bash
grep -rln "app-in-a-box\|defi-agent" scripts/
```

Edit each. Many will be log/echo strings. Run setup smoke after.

```bash
./scripts/setup.sh --dry-run 2>&1 | tail -10  # if --dry-run is supported; else skip
git add scripts/
git commit -m "refactor(rename): scripts → mangrove-agent"
```

- [ ] **Step 9: CI workflows**

```bash
grep -nE "app-in-a-box|defi-agent" .github/workflows/ci.yml
```

If the workflow runs against a hardcoded repo URL, update once Task 17 has run. If it just uses `${{ github.repository }}`, no edit needed — but verify the workflow file doesn't pin to the old name.

```bash
git add .github/
git commit -m "refactor(rename): CI workflows → mangrove-agent"  # only if any change
```

- [ ] **Step 10: Final sweep — `grep` should return nothing relevant**

```bash
grep -rn "app-in-a-box\|defi-agent" \
  --exclude-dir=.git \
  --exclude="*.pdf" \
  --exclude="*.pptx" \
  --exclude-dir=docs/incidents \
  --exclude="2026-05-08-mangrove-agent-rebrand.md" \
  /tmp/mangrove-agent-rebrand/
```

Expected: ZERO matches outside of:
- `docs/incidents/2026-04-24-eip7702-drain.md` (historical record — keep `app-in-a-box` there)
- This plan file (`docs/plans/2026-05-08-mangrove-agent-rebrand.md`)
- `docs/audits/*.md` if they reference the original name as audit context — keep
- `docs/workshop/*.pdf` (binary)

If any other file matches, fix it and commit.

---

## Task 16: Run full verification on the renamed code

- [ ] **Step 1: Full test suite**

```bash
cd /tmp/mangrove-agent-rebrand/server
source .venv/bin/activate
pytest -q 2>&1 | tail -10
```

Expected: green.

- [ ] **Step 2: ruff + import-linter (if architecture audit configured rules)**

```bash
cd /tmp/mangrove-agent-rebrand
ruff check . 2>&1 | tail -3
# If import-linter contracts.toml exists from Task 5:
[ -f contracts.toml ] && lint-imports 2>&1 | tail -3
```

- [ ] **Step 3: Boot the server and smoke each MCP tool group**

```bash
cd /tmp/mangrove-agent-rebrand/server
source .venv/bin/activate
python -m uvicorn src.app:app --port 9080 &
SERVER_PID=$!
sleep 4

# Discovery (no auth)
curl -s http://localhost:9080/api/v1/discovery | python -m json.tool | head -20

# Status
curl -s http://localhost:9080/api/v1/status | python -m json.tool

# MCP tool list (should now reference "mangrove-agent" namespace)
curl -s -X POST http://localhost:9080/mcp/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python -m json.tool | head -40

kill $SERVER_PID
```

Expected: `tools/list` returns the agent's tools; the JSON-RPC envelope has no name-related errors.

- [ ] **Step 4: Verify each hook still fires**

Check `.claude/settings.json` lists 4 hooks:
- `UserPromptSubmit`: block-wallet-secrets.sh --mode user
- `PostToolUse`: block-wallet-secrets.sh --mode tool
- `PreToolUse` matcher `Bash`: block-main-commits.sh
- `PreToolUse` matcher `mcp__mangrove-agent__execute_swap`: preflight-swap.sh

For each, manually exercise:

```bash
# block-main-commits — try to git commit on main (in a throwaway worktree)
cd /tmp && rm -rf hook-test-1 && \
  git clone /tmp/mangrove-agent-rebrand hook-test-1 && \
  cd hook-test-1 && git checkout main && \
  echo "test" > x.txt && git add x.txt && \
  # The hook fires inside Claude Code, not bare git. Verify by reading the hook script:
  bash /tmp/mangrove-agent-rebrand/.claude/hooks/block-main-commits.sh < /dev/null
```

(Hooks are exercised by the harness, not by direct `git`. Verify each hook script is syntactically valid bash and exits 0/non-0 correctly.)

```bash
for h in /tmp/mangrove-agent-rebrand/.claude/hooks/*.sh; do
  echo "=== $h ==="
  bash -n "$h" && echo "  syntax ok" || echo "  SYNTAX ERROR"
done
```

Expected: all syntax ok.

- [ ] **Step 5: Verify fresh setup-from-scratch one more time**

```bash
cd /tmp && rm -rf agent-fresh
git clone /tmp/mangrove-agent-rebrand agent-fresh
cd agent-fresh
./scripts/verify_quickstart.sh 2>&1 | tail -20
cd /tmp && rm -rf agent-fresh
```

Expected: green.

- [ ] **Step 6: Commit a verification report**

```bash
cd /tmp/mangrove-agent-rebrand
mkdir -p docs/audits
cat > docs/audits/post-rename-verification.md <<'EOF'
# Post-rename verification — 2026-05-08

| Check | Result |
|-------|--------|
| pytest -q | (paste tail) |
| ruff check | (paste) |
| /health | (paste) |
| /discovery | (paste tool count) |
| MCP tools/list | (paste namespace + count) |
| Hooks syntax | all ok |
| verify_quickstart.sh | (paste tail) |
EOF
git add docs/audits/post-rename-verification.md
git commit -m "docs(audits): post-rename verification report"
```

---

## Task 17: GitHub repo rename — DECISION GATE

**Action:** rename `MangroveTechnologies/app-in-a-box` → `MangroveTechnologies/mangrove-agent`.

**Reversibility:** GitHub preserves a redirect from the old name for ~ "indefinitely if not reused", so old URLs don't 404. But every external link / clone URL / CI integration / dependabot config should be updated. **This is the most destructive step in the plan.**

- [ ] **Step 1: STOP — confirm with user one more time**

State out loud: "About to run `gh repo rename mangrove-agent -R MangroveTechnologies/app-in-a-box`. This renames the GitHub repo. Existing clones will need `git remote set-url`. Old issue/PR URLs redirect. Proceed?"

Wait for explicit yes.

- [ ] **Step 2: Run the rename**

```bash
gh repo rename mangrove-agent -R MangroveTechnologies/app-in-a-box
```

Expected: `✓ Renamed repository to MangroveTechnologies/mangrove-agent`.

- [ ] **Step 3: Verify**

```bash
gh repo view MangroveTechnologies/mangrove-agent --json name,url,description
gh repo view MangroveTechnologies/app-in-a-box --json name,url 2>&1 | head -3  # should redirect or 404
```

- [ ] **Step 4: Update the worktree's remote URL**

```bash
cd /tmp/mangrove-agent-rebrand
git remote set-url origin https://github.com/MangroveTechnologies/mangrove-agent.git
git remote -v
git fetch origin
```

- [ ] **Step 5: Push the in-flight branch to the new origin**

```bash
git push origin feature/mangrove-agent-rebrand
```

Expected: pushed without error.

---

## Task 18: Open the PR

- [ ] **Step 1: Check branch ahead/behind state**

```bash
cd /tmp/mangrove-agent-rebrand
git status
git log --oneline origin/main..HEAD | head -30
```

Expected: clean working tree; commits all listed.

- [ ] **Step 2: Open the PR**

```bash
gh pr create \
  --repo MangroveTechnologies/mangrove-agent \
  --base main \
  --head feature/mangrove-agent-rebrand \
  --title "feat: rebrand to mangrove-agent + audits + scope cleanup" \
  --body "$(cat <<'EOF'
## Summary

Single-purpose pivot: app-in-a-box → mangrove-agent (the Mangrove trading agent). Cuts the dual "fork-as-scaffold" identity, lands three audits, and commits the audit triage + must-fix items.

## Scope cleanup
- Removed `tutorials/scaffold-lifecycle/`, six generic scaffold skills, the `/onboard` flow, `init.sh` / `init-interactive.sh` / `branding.json`.
- Dropped "Path B" from README and the rebrand line from CLAUDE.md.

## Audits (under `docs/audits/`)
- `security.md` — security-audit-agent findings
- `code-quality.md` — code-analyst findings
- `architecture.md` — software-audit-agent findings
- `documentation.md` — README + tutorials vs code drift
- `triage.md` — must-fix / should-fix / v2-deferred synthesis
- `post-rename-verification.md` — post-rename smoke checks

## Rename surface
Every literal `app-in-a-box` and `defi-agent` updated except: `docs/incidents/2026-04-24-eip7702-drain.md` (historical record, intentional), `docs/workshop/*.pdf` (binary — regen in follow-up).

## Test plan
- [ ] Full pytest green on CI
- [ ] ruff clean on CI
- [ ] `./scripts/verify_quickstart.sh` succeeds in fresh clone
- [ ] `/health` and `/api/v1/discovery` reachable on local boot
- [ ] All four hooks fire (manually verified pre-merge)
- [ ] MCP `tools/list` returns the renamed namespace `mangrove-agent`
EOF
)"
```

Capture the PR URL.

- [ ] **Step 3: Wait for human approval — DO NOT MERGE**

Per `.claude/rules/git-workflow.md`: AI agents must never merge a PR. Post the URL, wait.

---

## Task 19: Local directory rename + workspace CLAUDE.md update — DECISION GATE

**Pre-condition:** PR from Task 18 is merged by the user.

- [ ] **Step 1: Verify PR merged**

```bash
gh pr view <PR-URL> --json state,mergedAt
```

Expected: `MERGED`.

- [ ] **Step 2: Confirm with user before renaming the local clone**

State: "PR merged. About to rename `/Users/darrahts/mangrove-workspace/app-in-a-box` → `mangrove-agent` and update workspace `CLAUDE.md` references. Proceed?"

Wait for yes.

- [ ] **Step 3: Rename the local directory**

```bash
cd /Users/darrahts/mangrove-workspace
mv app-in-a-box mangrove-agent
cd mangrove-agent
git remote set-url origin https://github.com/MangroveTechnologies/mangrove-agent.git
git checkout main
git pull --ff-only
git remote -v
```

- [ ] **Step 4: Clean up the worktree**

```bash
cd /Users/darrahts/mangrove-workspace/mangrove-agent
git worktree remove /tmp/mangrove-agent-rebrand
git worktree list
git branch -D feature/mangrove-agent-rebrand 2>/dev/null
git push origin --delete feature/mangrove-agent-rebrand 2>/dev/null
```

- [ ] **Step 5: Update workspace `CLAUDE.md`**

In a workspace worktree:

```bash
cd /Users/darrahts/mangrove-workspace
# Use a worktree for this edit (workspace git-workflow rule)
git worktree add -b chore/rename-app-in-a-box-row /tmp/workspace-rename-row origin/main
cd /tmp/workspace-rename-row
```

Edits to `CLAUDE.md`:
1. In the "Core Products" table or wherever app-in-a-box appears: replace `app-in-a-box` with `mangrove-agent`, update the description from generic FastAPI scaffolding to "Mangrove trading agent — local AI trading bot built on the Mangrove API. FastAPI + MCP + APScheduler. Author strategies, backtest, paper-trade, go live."
2. In the Product Owners table: short name `app-in-a-box` → `mangrove-agent`, repo `app-in-a-box` → `mangrove-agent`.
3. Anywhere referencing `mangrove/app-in-a-box/` paths: update to `mangrove/mangrove-agent/`.

```bash
cd /tmp/workspace-rename-row
git add CLAUDE.md
git commit -m "chore: workspace CLAUDE.md — app-in-a-box → mangrove-agent"
git push -u origin chore/rename-app-in-a-box-row
gh pr create \
  --base main \
  --head chore/rename-app-in-a-box-row \
  --title "chore: workspace CLAUDE.md — app-in-a-box → mangrove-agent" \
  --body "Reflects the GH repo rename in the workspace product registry."
```

Wait for human merge.

- [ ] **Step 6: After workspace PR merges, sync workspace and clean up**

```bash
cd /Users/darrahts/mangrove-workspace
git pull --ff-only
git worktree remove /tmp/workspace-rename-row
git push origin --delete chore/rename-app-in-a-box-row
git branch -D chore/rename-app-in-a-box-row 2>/dev/null
```

---

## Task 20: Post-merge CI + deploy verification

- [ ] **Step 1: Watch CI on the merged main**

```bash
gh run list --repo MangroveTechnologies/mangrove-agent --branch main --limit 3
gh run watch --repo MangroveTechnologies/mangrove-agent <run-id>
```

Expected: green.

- [ ] **Step 2: Re-merge dependabot PRs (Task 13)**

Now that main has shifted, dependabot will auto-rebase. Once rebased and green, merge in the order from Task 13.

- [ ] **Step 3: Mark deferred audit findings as v2 GH issues**

Per Task 12 Step 2 — all deferred items get a `v2`-labeled issue on `MangroveTechnologies/mangrove-agent`.

- [ ] **Step 4: Update memory**

If user wants persistent memory of the rename:

```
[saves project memory: app-in-a-box renamed to mangrove-agent (single-purpose Mangrove trading agent) on 2026-05-08. Old URLs redirect; local path /Users/darrahts/mangrove-workspace/mangrove-agent. Why: scope discipline — drop dual "fork-as-scaffold" identity. How to apply: any reference to app-in-a-box in conversation maps to mangrove-agent.]
```

---

## Self-Review Checklist (run after writing this plan, before exec)

**1. Spec coverage:** Every requirement in the user's prompt mapped to a task?
- "security audit" → Task 3 ✓
- "code audit" → Task 4 ✓
- "documentation audit" → Task 6 ✓
- "review the gh issues" → Task 2 ✓
- "review the git log and prs" → Task 2 ✓
- "get this across the finish line" → Tasks 11, 12, 14, 16, 18, 20 ✓
- "make sure readme and quick start guide are correct" → Tasks 6, 10, 14 (verify_quickstart.sh) ✓
- "make sure the mcp server and skills and hooks and tools are correct" → Tasks 8 (cut wrong skills/hooks), 15 (rename), 16 (smoke each) ✓
- "rename this to mangrove-agent" → Tasks 15, 17, 19 ✓
- "clear out all things not related to a trading agent" → Tasks 8, 9 ✓

**2. Placeholder scan:** Searched for "TBD", "TODO", "implement later", "appropriate error handling", "similar to". One legitimate `<finding-id>` placeholder in Task 11/12 — those names depend on what the audits surface, which is by design (the audit IS the spec for those tasks).

**3. Type/name consistency:** New name is `mangrove-agent` (hyphen, lowercase) everywhere. MCP namespace `mcp__mangrove-agent__<tool>`. Plugin name `mangrove-agent`. Pyproject project name `mangrove-agent` (hyphen, since `name` field accepts it). Local dir `mangrove-agent`. Workspace short name `mangrove-agent` (matches repo).
