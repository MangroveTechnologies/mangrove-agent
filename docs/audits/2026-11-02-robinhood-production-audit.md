# Robinhood server-strategy adoption production audit

Date: 2026-11-02
Original audit verdict: blocked on the recovery defect below; release verification also pending.

See the remediation follow-up at the end for subsequent fixes and verification.

## Actionable findings

### P1 - Interrupted stop can permanently lose the exit-only monitor

Location: `server/src/services/local_deployment.py:205-207` in mangrove-agent; recovery at lines 214-224.

Trigger: stop a live adopted deployment with an unresolved order or open exposure, then lose the process (or raise an exception) after the inactive status commits but before `_apply_scheduler_effects` persists `exit_only_mode='live'`.

Impact: the database retains inactive status with no exit-only marker. On restart, `restore_jobs` cancels its job. Broker ticks also refuse an inactive record without that marker. Existing order reconciliation does not restore strategy exit evaluation. A pending buy can therefore fill without the strategy's protective exit monitor resuming. Retrying stop also sees inactive status and cannot infer the previous live state from the row.

Evidence: a disposable regression test adopted and activated a strategy in an isolated SQLite database, created a pending order, injected process loss at `_apply_scheduler_effects`, reopened the database, and called `restore_jobs`. The expected job registration did not occur; recovery canceled the job. This is an introduced lifecycle ordering problem, not a live-account observation.

Minimal remedy: persist inactive status and the required exit-only state atomically, reusing the existing exposure decision and locks. Make startup recovery safe for interrupted transitions, and add the interruption case to the permanent regression suite. Do not add another scheduler or risk engine.

### P3 - Documented deployment URLs omit the agent prefix

Location: `docs/robinhood-execution.md:117-119` in mangrove-agent.

Trigger: follow the guide's `/api/v1/strategies/deployments` endpoint examples.

Impact: those paths do not match the application router and return 404. The full mounted prefix is `/api/v1/agent/strategies/deployments`: `server/src/api/router.py:24` adds `/api/v1`, and the same file at line 27 adds `/agent`.

Evidence: traced the actual nested router mounts. The new route test mounts the strategies router alone, so it does not validate the guide's full application prefix.

Minimal remedy: correct all three documented deployment paths to include `/agent`.

## Known release gates and verification limits

- The installed agent environment has `mangroveai==2.1.0`, whose actual `StrategiesService` has no `local_deployment` method. The agent lockfile pins that version (`server/requirements.lock:248`), and setup installs the lockfile. The new agent adapter correctly rejects this missing capability. Source SDK tests passing do not make the installed package compatible. Release/install a compatible SDK through the existing release process, update the lock, and verify the installed wheel before end-to-end testing.
- Deploy the server local-deployment endpoint before enabling the agent flow. Verify SDK/server/agent compatibility together; this audit did not deploy or publish anything.
- Current server PostgreSQL-backed route, readiness, and evaluator tests could not be executed: no server test environment was present, required packages were absent from the agent environment, and Docker did not respond within a bounded 10-second probe. This is an environment limitation, not evidence of a code failure. Server SQL and route changes were reviewed statically.
- Manual Robinhood authorization, saved-session reuse, real browser cancellation, and real-account end-to-end testing are deferred at the user's request. Synthetic tests cannot establish those external behaviors.
- No load/capacity benchmark, production rollout, package build, or remote CI result is claimed.
- Existing design limitation: failed upstream validation prevents evaluation, including exit evaluation, until corrected. The guide documents this limitation; this audit does not claim uninterrupted protective execution when the authoritative strategy is invalid or unavailable.
- Existing OAuth storage/refresh behavior is reused. Cross-process concurrent refresh remains a limitation identified by the existing documentation; no new cross-process safety guarantee was established.

## Scope and implementation assessment

Reviewed saved server-strategy adoption into the existing Robinhood execution lane, snapshot/version/backtest readiness checks, ownership checks, activate/inspect/stop operations, scheduler recovery, MCP/REST adapters, SDK transport, migration, and optional interactive connection setup. The server remains authoritative for strategy definitions and evaluation; the agent reuses its scheduler, confirmed execution, portfolio controls, and Robinhood connector. No separate trading engine is introduced by this change set.

Setup review includes Connect now as the initial choice, optional skip/retry, the selectable blue Skip for now action while authorization is pending, automatic continuation on connector success, timeout/process cleanup, noninteractive behavior, and the documented Docker limitation. Credential/session verification remains distinct from activating a strategy or placing an order.

All three checkouts are on `feat/robinhood-server-strategy-adoption`. Each HEAD equals its merge base with the locally available origin/main, with no committed branch delta. The review includes combined staged and unstaged changes against HEAD and intended untracked delivery files. No remote freshness is implied. All changed hunks (including removals) and every line of new files were read, with enclosing callers and contracts; no binary/unread review items remain. Generated MCP contracts were checked with their generator and policy. No unrelated implementation expansion was identified.

- mangrove-agent: HEAD `25b8a72c1d749d896d8c179b83ee292f37c76eb6`; dirty working tree.
- MangroveAI: HEAD `bf9fd9a19a415f9b83d4a110d703e634cf6f8963`; dirty working tree.
- MangroveAI-SDK: HEAD `41c943d4e9e212ea471c98f11ccd3d478b4b45ca`; dirty working tree.

## Checks run during this audit

| Check | Result |
| --- | --- |
| Agent regression selection covering adoption, broker/confirmed execution, portfolio finalization, lifecycle, strategy routes/services, paper execution, scheduler, risk, MCP/pricing, venue, setup and connector | 490 passed; 2 deprecation warnings |
| SDK strategies tests with source checkout selected via PYTHONPATH=src | 14 passed |
| Disposable negative lifecycle tests | Stop interruption safety assertion failed as described above; timeframe-rebind test passed (concern ruled out) |
| Offline MCP contract checker | Passed, 32 tools; does not verify deployed receiver availability |
| Ruff on changed agent server/test Python and changed SDK Python | Passed |
| Ruff on changed agent scripts | Six diagnostics; all reproduced in HEAD baselines, none newly introduced |
| Python AST parsing | All 24 reviewed Python files parsed |
| bash -n scripts/setup.sh | Passed |
| git diff HEAD --check in all three repos | Passed |
| Reviewed file hash recheck | All 30 unchanged after verification |
| Server database-backed tests | Not run: environment unavailable as described above |

Agent test command, from mangrove-agent/server:

```sh
../.venv/bin/python -m pytest tests/integration/test_local_deployment.py tests/integration/test_robinhood_execution.py tests/integration/test_confirmed_execution.py tests/integration/test_portfolio_tick_finalization.py tests/integration/test_strategy_lifecycle_fixes.py tests/integration/test_strategy_service.py tests/integration/test_strategy_routes.py tests/integration/test_paper_confirmed.py tests/integration/test_scheduler_nonblocking.py tests/unit/test_scheduler_service.py tests/unit/test_portfolio_risk_service.py tests/unit/test_mcp_contract.py tests/unit/test_tool_pricing.py tests/unit/test_robinhood_venue.py tests/unit/test_setup_flow.py tests/unit/test_robinhood_connect.py -q --tb=short
```

SDK command, from MangroveAI-SDK:

```sh
PYTHONPATH=src ../mangrove-agent/.venv/bin/python -m pytest tests/test_strategies.py -q --tb=short
```

The disposable negative tests were stored outside the repositories at `/tmp/robinhood-production-audit/test_audit_deployment.py` and run from mangrove-agent using `ENVIRONMENT=test PYTHONPATH=server .venv/bin/python -m pytest /tmp/robinhood-production-audit/test_audit_deployment.py -k 'test_audit_rebind or test_audit_stop' -q --tb=short` (one failed, one passed, 21 deselected). They did not contact Robinhood or place orders. The failed safety assertion is an additional failure-boundary check; it does not contradict the existing 490-test result.

Pre-existing script lint identities: robinhood_connect.py ASYNC230 and BLE001; setup_support.py TRY004, FURB166, FURB157, SIM905. They are not presented as introduced production defects.

## Secrets, privacy, and authorization

No gitleaks, detect-secrets, or trufflehog executable was available. A bounded full-file scan for private-key blocks, credential-shaped assignments, token prefixes, and workstation paths was combined with manual tracing of authentication, owner checks, SDK business arguments, errors, OAuth callbacks, token storage, fixtures, and generated contracts. The one credential-shaped test sentinel was synthetic. No real credential or unintended PII exposure was found in the reviewed change set; this is not a guarantee that none exists.

Local API credentials remain part of transport/authentication rather than remote strategy business arguments. Existing encrypted OAuth storage and restricted file creation are reused. The audit did not inspect ignored credential stores, live databases, deployed/private logs, or Git history for secrets, and did not install scanners or query vulnerability services.

## Per-file coverage

Paths below are relative to the named repository. "Diff + context" means every changed hunk and relevant enclosing code/callers; "Full new file" means every line. SHA-256 identifies the reviewed working-file content, including pre-existing portions.

| Repository / file | Reason in scope | Reviewed scope | SHA-256 |
| --- | --- | --- | --- |
| mangrove-agent/README.md | Setup and feature entry-point documentation | Diff + context | `d315e550bce6c8a43a11abab52d5d4e73d2387cc0c8d96f5a626395c0bf106c6` |
| mangrove-agent/docs/robinhood-execution.md | Operation, safety limits and rollout guide | Diff + context | `8643cc154fed0f04bfeb898aed89e6e372ca88f7c7132a96b849183096f195af` |
| mangrove-agent/scripts/contracts/local.json | Generated public MCP schema | Diff + context + generator | `25b0f3c477f29d1cf89234809e0e8f23db73fc698dbb124c5611ac091f9c71bd` |
| mangrove-agent/scripts/contracts/policy.json | MCP compatibility policy | Diff + context | `de750bfa7fe9d8f0d9e0be67a2a002e4158a26d1dd8a16009407dd69a1260f67` |
| mangrove-agent/scripts/robinhood_connect.py | Existing connector cancellation and authorization flow | Diff + context | `8e762574bbea5ad6124a8152ad0299a06de9540df38e67c1441308f5ab7627c8` |
| mangrove-agent/scripts/setup.sh | Interactive setup integration | Diff + context | `f391d2f156ec911ab95e23a796326f71a0b9f4771b64427d0a1bb1b23e960e73` |
| mangrove-agent/scripts/setup_support.py | Optional connection UX and process lifecycle | Diff + context | `19cd54deb23e52978f0f934b3d60c995cbb0b810c21ce96291eaa4eecd3eff0c` |
| mangrove-agent/server/src/api/routes/strategies.py | Local deployment REST adapters | Diff + context | `c9dd32c81108c41bda2c94bcd725a8f16e68d29f2d87e3301aa3b99503ec212e` |
| mangrove-agent/server/src/mcp/broker_tools.py | Local deployment MCP adapters | Diff + context | `53a382c970139f36541588bd4330023f97ee590e3edcda397f47d26572076276` |
| mangrove-agent/server/src/mcp/pricing_bindings.py | Existing upstream pricing binding | Diff + context | `777c49efc3666ed137ad64a848e1179d6144783f7c41b9a9a5aa6acd37b5691a` |
| mangrove-agent/server/src/mcp/server.py | Tool registration | Diff + context | `3f49bad804d4ed69d32f8cf1cf78a4a292c2e5b7a7e59d07d40fc30f676cf042` |
| mangrove-agent/server/src/services/broker_execution.py | Server snapshot validation and execution integration | Diff + context | `faeb148e65398a485ef9253604159725caca82efb789e1808f0d92786f9be109` |
| mangrove-agent/server/src/services/local_deployment.py | Adoption and lifecycle orchestration | Full new file + callers | `ce68142a86af485372811555fb274775ec4ec953dca8a2b632f2e123f3edde24` |
| mangrove-agent/server/src/services/scheduler_service.py | Startup restoration | Diff + context | `cab35c333098d7c6923208efc7923fc8193c7a444b70d51f1ccd4963d35d3255` |
| mangrove-agent/server/src/services/strategy_service.py | Existing lifecycle and tick integration | Diff + context | `d5e81f5034841db842fa7aed108ede86dc3f609775621a938658c31dd9eeb074` |
| mangrove-agent/server/src/shared/db/migrations/021_local_deployments.sql | Binding persistence schema | Full new file + callers | `3adea4278d6b83af48cead7a2a7e415d29f0c0732c3edbd11bddaeecaebfe50c` |
| mangrove-agent/server/tests/integration/test_local_deployment.py | Regression coverage for changed behavior | Full new file + callers | `15aae72f1303ff7827540f8aac04e5c6a81155fc07ab7dbb19a85a4543e0df76` |
| mangrove-agent/server/tests/unit/test_robinhood_connect.py | Regression coverage for changed behavior | Full new file + callers | `c81108b5926f81fe2c593f42198239e2682af9619421423f4c2b74604f62b732` |
| mangrove-agent/server/tests/unit/test_scheduler_service.py | Regression coverage for changed behavior | Diff + context | `b8dc7e94ce50a6e63021a9f5580016c77892c0bfc42939210cf985ce9d0971d7` |
| mangrove-agent/server/tests/unit/test_setup_flow.py | Regression coverage for changed behavior | Diff + context | `8ef7db77f90cee4469d8c09a2e25165e11acaf323e0cc2d5b1f5b149cc32fd05` |
| MangroveAI/src/MangroveAI/domains/managers/paper_execution.py | Exact stored-strategy backtest matching | Diff + context | `1c4ff3890b7fc9fd1b74c473a605f4ddcd3dec08b88144b3866f32f8de772799` |
| MangroveAI/src/MangroveAI/domains/managers/services.py | Authoritative evaluator parameter consistency | Diff + context | `dfa464f6c47f9f66f324eab709f4d2f6d72a9df6e0e06f45194870fce365ffa9` |
| MangroveAI/src/MangroveAI/domains/strategies/local_deployment.py | Server deployment snapshot and readiness | Full new file + callers | `9da4cca65c3067b76f12650ba8a87f7aa732e4afad0614ba316dc18be33e4808` |
| MangroveAI/src/MangroveAI/domains/strategies/routes.py | Authenticated server endpoint | Diff + context | `072f547864c5bedf124e66e040186e322ba77cd793b761df00cb0b32d4fd9ef7` |
| MangroveAI/src/MangroveAI/domains/strategies/strategy.py | Stored-strategy execution parameter definition | Diff + context | `3392400b5298413e1ddfedce74d373b3521e9e599dd37ecae5d51951372e7f99` |
| MangroveAI/src/MangroveAI/tests/domains/managers/test_stored_strategy_version.py | Regression coverage for changed behavior | Diff + context | `b77e506796a621380e2aca92ff2f7ef6398c6075a4999b279abb20e1a8d5173f` |
| MangroveAI/src/MangroveAI/tests/domains/strategies/test_local_deployment.py | Regression coverage for changed behavior | Full new file + callers | `0272afe3d57369ed2269968b4d607612ec69d1617d8cef197611897f04284a1e` |
| MangroveAI/src/MangroveAI/tests/domains/strategies/test_strategy_verify_route.py | Regression coverage for changed behavior | Diff + context | `bed144d6d5b1b14b417adad7c8095811599ff5518dc07f3753edd7a7b09987d2` |
| MangroveAI-SDK/src/mangrove_ai/_services/strategies.py | SDK transport adapter | Diff + context | `44d51a0fbb0651fcd8545749015a09d5c746cac3fb9ed2359b28727138902f6d` |
| MangroveAI-SDK/tests/test_strategies.py | Regression coverage for changed behavior | Diff + context | `4899a5c2b23d59b8a1cd514b412173b2343380bc201b95b4226a010d9dea4d89` |

## Changes made by the audit

Only this audit report was added to the established docs/audits directory. Disposable tests and the scope manifest were created under /tmp. Implementation files and existing staged changes were preserved. No fixes, dependency changes, commits, pushes, releases, deployments, paid requests, or broker orders were performed.


## Remediation follow-up (2026-11-02)

The user subsequently authorized fixes. The P1 interrupted-stop defect and P3
incorrect REST paths are now fixed in the working tree; original findings and
hashes above are retained as the pre-fix audit record.

- Extracted the existing exposure/exit-monitor decision into
  `strategy_service._exit_monitor_mode`, used by both the existing scheduler
  effects and local-deployment stop. No new risk rules or scheduler were added.
- Stop now commits inactive status and the required exit-only mode in one SQLite
  update before scheduler side effects. Startup can restore the committed monitor
  after process loss, and retries remain inactive without fetching upstream data.
- Added permanent interruption regression cases with and without a pending order.
  They reopen the isolated database, assert the durable lifecycle state, verify
  scheduler restoration/cancellation, preserve the pending order, and retry stop.
- Corrected the documented REST prefix. The route authentication test now mounts
  the actual application API router and exercises the full documented paths.
- Expanded rollout instructions to identify the incompatible current SDK pin and
  require a compatible published version, regenerated lockfile, and an installed
  SDK capability check. No unpublished version was invented and no installed SDK
  was replaced with the older-version source checkout.

Post-fix verification: 492 agent tests passed (the original audit selection plus
two interruption cases), 14 SDK source tests passed, changed Python files passed
Ruff, and git diff HEAD --check passed. The first broad attempt was blocked by
sandbox localhost socket permissions; the permitted rerun passed with two
existing deprecation warnings. Focused lifecycle checks also passed (51 tests).
Only four implementation/test/documentation files from the original manifest
changed, all in mangrove-agent, plus this report. MangroveAI and SDK sources were
not modified during remediation. Existing unrelated working changes were preserved.

Remaining gates: publish/install the compatible SDK and update its dependency
pin, deploy the server contract, run server database-backed verification, and
perform the deferred manual Robinhood tests. This follow-up does not certify a
production rollout or repair any live database. No commits, pushes, releases,
trades, or deployments were performed.
