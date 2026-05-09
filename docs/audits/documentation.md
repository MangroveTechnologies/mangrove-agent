# Documentation Audit — 2026-05-08

**Scope:** README, CLAUDE.md, CONTRIBUTING, docs/, tutorials/, plugin/README, .claude/skills/*/SKILL.md.
**Method:** read each surface against actual code; cross-check tutorial chapter references against routes/scripts/skills/MCP tools.

## Executive summary

Documentation is mostly accurate but carries the dual identity (trading bot + scaffold-fork-template) that the rebrand is removing. The README headline disagrees with the MCP server name disagrees with the plugin name (three-way clash). One tutorial chapter explicitly references `.claude/skills/onboard/SKILL.md` as a "rebrand path for forkers" — that file goes away in Task 8.

## Findings

### HIGH-1 — README headline name disagrees with MCP namespace and plugin name
- **Files:** `README.md:6`, `server/src/mcp/server.py:34`, `plugin/.claude-plugin/plugin.json:2`
- **Detail:**
  - README headline: `<h1>defi-agent</h1>`
  - FastMCP server name: `"app-in-a-box"` — determines the `mcp__app-in-a-box__*` matcher namespace
  - Plugin manifest `name`: `"app-in-a-box"`
  - In-repo hook matcher (`.claude/settings.json`): `mcp__defi-agent__execute_swap` — **does not match** the actual namespace; the hook does not fire today
- **Proposed fix:** unify everything to `mangrove-agent` per the rebrand. Add a regression test that asserts `server.name == "mangrove-agent"` in `tests/test_mcp.py`.

### HIGH-2 — README "Path B — Fork as a scaffold" describes a flow being deleted
- **File:** `README.md` (around the "Getting the Code" section)
- **Detail:** "Path B" instructs users to fork-as-template and run `/onboard` to rebrand. With the scaffold-lifecycle deletion (Task 8) and `init.sh`/`branding.json` removal, the flow doesn't exist anymore.
- **Proposed fix:** delete the entire Path B subsection. Promote "Path A — Use as-is" content to be the single Quickstart.

### MEDIUM-1 — CLAUDE.md "Not here for a trading bot?" line points at a removed flow
- **File:** `CLAUDE.md:11` (the blockquote)
- **Detail:** `> Not here for a trading bot? This same codebase can be rebranded into any FastAPI + Claude Code app. Run /onboard inside Claude Code to start the rebrand flow.`
- **Proposed fix:** delete this line. Also delete the "## Branding" section near the bottom which references `branding.json` and `./init.sh`.

### MEDIUM-2 — Tutorial chapter 08 cites a removed skill as "rebrand path for forkers"
- **File:** `tutorials/trading-app/08-monitor-troubleshoot-extend.md:247`
- **Detail:** `├── skills/onboard/SKILL.md          ← rebrand path for forkers`
- **Proposed fix:** remove the line from the tree diagram.

### MEDIUM-3 — Tutorial chapters reference both old names
- **Files (counts of `app-in-a-box` + `defi-agent`):**
  - `tutorials/trading-app/00-index.md` (1+1)
  - `tutorials/trading-app/02-overview.md` (0+4)
  - `tutorials/trading-app/03-setup.md` (1+10)
  - `tutorials/trading-app/05-paper-mode.md` (0+1)
  - `tutorials/trading-app/06-wallet-setup.md` (0+2)
  - `tutorials/trading-app/07-going-live.md` (0+1)
  - `tutorials/trading-app/08-monitor-troubleshoot-extend.md` (1+1)
- **Proposed fix:** rename pass in Task 15 step 7. All references move to `mangrove-agent`.

### MEDIUM-4 — `docs/workshop/setup-guide.md` has 21 `app-in-a-box` + 10 `defi-agent` refs (highest of any doc)
- **File:** `docs/workshop/setup-guide.md`
- **Proposed fix:** rename pass; the corresponding PDF (`setup-guide.pdf`) cannot be `sed`-ed and should be regenerated post-merge — out of scope for the rebrand PR.

### MEDIUM-5 — `docs/user-stories.md` exists; one defi-agent ref; not in rebrand-plan inventory
- **File:** `docs/user-stories.md`
- **Detail:** Not previously listed in the rebrand-plan file map. One defi-agent reference. Treat as scope cleanup — content review needed: are these v1 user stories or scaffold-template stories?
- **Proposed fix:** read the file in Task 15 step 6, decide if it's relevant (rename) or scaffold-leftover (delete).

### LOW-1 — `tutorials/scaffold-lifecycle/00-overview.md` and `01-onboarding.md` reference app-in-a-box
- **Files:** `tutorials/scaffold-lifecycle/00-overview.md` (3 refs), `01-onboarding.md` (1)
- **Detail:** Whole `tutorials/scaffold-lifecycle/` directory is being deleted in Task 8 — these refs become irrelevant.
- **Proposed fix:** no action; deletion handles it.

### LOW-2 — Plugin README mentions `app-in-a-box` 3 times
- **File:** `plugin/README.md`
- **Proposed fix:** Task 15 step 3 covers it.

### LOW-3 — Six SKILL.md files in `.claude/skills/{architecture,audit-security,check-alignment,onboard,specification,tool-spec,tutorial}/` reference app-in-a-box
- **Files:** as above (~21 refs total).
- **Detail:** The `onboard`, `architecture`, `specification`, `tutorial` skills are deleted in Task 8. The remaining `audit-security`, `check-alignment`, `tool-spec` need rename in Task 15 step 4.

### INFO — confirmed accurate

- `verify_quickstart.sh` exists at `scripts/verify_quickstart.sh` (PR #51 era).
- Every `/api/v1/agent/*` endpoint cited in tutorial chapters resolves to a real `@router.<verb>` decorator in `server/src/api/routes/`. (Spot-checked `/strategies`, `/wallet/<addr>/balances`, `/dex/tx-status`, `/status`.)
- Every `./scripts/<name>.sh` cited in tutorials exists in `scripts/`.
- Every `/skill` cited in tutorials exists in `.claude/skills/` (the ones being deleted are listed above).
- `docs/architecture.md` describes the dual-protocol + service-layer architecture accurately.
- `docs/configuration.md` matches `server/src/config/local-example-config.json` and the loader.
- `docs/incidents/2026-04-24-eip7702-drain.md` is a thorough post-mortem; KEEP intact (historical record — `app-in-a-box` references are accurate at the time of incident).

## Pre-rename references inventory (consolidated)

| File group | `app-in-a-box` refs | `defi-agent` refs | Disposition |
|---|---:|---:|---|
| **README, CLAUDE.md, CONTRIBUTING.md** | 12 | 10 | Edit (Task 15 step 5) |
| **server/src/** (all .py + config JSON) | 4 | 8 | Edit (Task 15 steps 1-2) |
| **server/tests/** | 2 | 1 | Edit + update assertions |
| **plugin/** | 6 | 0 | Edit |
| **.claude/** (skills + rules + hooks + agent + settings) | 21 | 28 | Edit; some files deleted in Task 8 |
| **docs/** (excl. incidents/audits/plans) | 5 | 16 | Edit |
| **docs/workshop/** | 24 | 20 | Edit .md; PDFs regenerate post-merge |
| **docs/incidents/2026-04-24-eip7702-drain.md** | 5 | 0 | **KEEP — historical** |
| **docs/audits/** (this audit) | (this file) | (this file) | KEEP — audit context |
| **docs/superpowers/plans/2026-05-08-mangrove-agent-rebrand.md** | 60 | 29 | KEEP — describes the rename itself |
| **scripts/** | 0 | 13 | Edit |
| **tutorials/trading-app/** | 3 | 19 | Edit |
| **tutorials/scaffold-lifecycle/** | 4 | 0 | **DELETE** (Task 8) |
| **init.sh, init-interactive.sh, branding.json** | 6 | 0 | **DELETE** (Task 8) |
| **docker-compose.yml** | 0 | 1 | Edit |
| **.mcp.json.example** | 0 | 0 | Inspect — likely needs key rename |
