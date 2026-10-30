# OAuth, external MCP and x402 production audit - 2026-10-28

Original audit verdict (before the follow-up fixes below): release blocked on the credential-exposure incident and specific lifecycle/observability corrections; production rollout verification remains pending. No new authorization bypass or unsafe payment retry was found in the reviewed paths. This is evidence from the bounded review, not a guarantee of security or capacity.

## Follow-up fixes - 2026-10-28

The user authorized fixes for all code/documentation findings and retained ownership of P1 credential rotation. The original findings below remain as an audit record; their code locations and hashes describe the pre-fix review set.

- OAuth cross-issuer replacement is now rejected before registration or token exchange while cleanup is outstanding. Existing mixed-issuer receipts are cleaned independently through the explicitly configured matching issuer; unrevoked credentials remain encrypted. Callback cleanup compares the current saved record before writing so concurrent refresh or disconnect cannot be overwritten. Setup and documentation explain the required recovery sequence.
- The watcher recognizes allowlisted agent payment failures in real console and JSON logging formats. It saves sanitized HTTP source/method/route/status details, distinguishes agent failures from server settlement reports, and reports source coverage. Malformed/oversized records cannot silently hang the reader; queued work and line size are bounded, shutdown issues are reported, and summary replacement is atomic.
- Consent now describes MangroveAI definitions, validation and schemas. A regression checks the actual consent response against the external tool catalog.
- Release documentation points to developer-portal consent and distinguishes the required gateway authority service from the optional platform browser bridge. Migration and legacy grant revocation remain explicit.

Follow-up verification: 20 consent/catalog tests and 12 watcher tests passed. The final OAuth/setup run passed 186 tests with one foreground-process timing failure; that unchanged test passed its isolated rerun in 0.92 seconds and had passed the earlier full run. Independent review separately passed 185 OAuth/setup tests before the final two added regressions, plus the watcher shutdown/atomic-output checks. Shell syntax, generated instructions and whitespace checks passed. No tests were weakened.

Independent review found no new blocking code defect. The final reconnect-warning wording discrepancy it noted was corrected with a regression. No production deployment, paid call, credential rotation, or live grant/database modification was performed in this follow-up. P1 and the original production release-verification gates remain open.

## Scope and comparison

Current uncommitted changes (staged plus unstaged versus each repository HEAD), including delivery-intended untracked files, and necessary existing callers/dependencies. This is not a full review of committed PR history.

| Repository | Branch | HEAD |
|---|---|---|
| MangroveAI | feat/mcp-oauth-2-1 | 71d66d1f6cb8846584272ab38f027f9b7e494f63 |
| mangrove-agent | feat/mangrove-oauth-login | 279a7de02d52cfba718af3dbb1ce9c0e23f6a7c5 |
| mangrove-platform-backend (required authority/read dependency) | dev | fe6e42d317b044e901766a62a3fde3c5bbe88eb8 |

Acceptance: browser OAuth through the developer portal or existing API-key/x402 modes; no silent credential-mode fallback; granted scopes and current tenant authority enforced; platform tools hidden externally but retained for Michael; canonical strategy implementations preserved; managed server-side settlement with existing recovery and budget controls; quiet logs with a usable end summary. Read-only OAuth is the current approved behavior, not a strategy-write grant.

## Findings

### P1 - Operational credential exposure requires rotation

During the earlier implementation, a configuration-search command printed a real CDP API secret from `MangroveAI/src/MangroveAI/config/local-config.before-mainnet-test.json:101` into tool output. That credential subsequently authenticated successfully in the managed-facilitator preflight and was used for local configuration. This audit did not re-read or reproduce the credential.

Impact: anyone with access to that output may be able to authenticate to the associated CDP account within the key's permissions. This does not imply access to the agent wallet's private key. Treat the credential as exposed. Rotate/revoke it through the provider, update affected local configuration through the existing secret mechanism, and restrict/remove retained output where possible. Do not assume the production Secret Manager key is identical; its identity was not inspected. No rotation or external account changes were performed by this audit.

### P2 - OAuth issuer changes can strand grant cleanup

`mangrove-agent/server/src/services/mangrove_oauth.py:198` stores the previous issuer's grant as retired after a successful replacement. `revoke_record` at line263 refuses records from another issuer, while `start` at line151 refuses all new login while a retired grant remains.

Reproduction used a disposable encrypted SQLite fixture and mocked HTTP: connect issuer A, change configuration to issuer B, connect B, disconnect. The old A grant stays pending, disconnect reports revoked=false, and subsequent sign-in is blocked. Credentials are not sent to the wrong issuer; the defect is lifecycle recovery. Prevent replacement across issuers until the original connection has been safely disconnected, and provide an explicit cleanup path for already mixed records. Revoking remotely alone does not clear the local record.

### P2 - Watcher misses agent-side payment failures

`mangrove-agent/scripts/watch_calls.py:43` requires server-style message/extra JSON; line80 otherwise recognizes only HTTP access lines. The agent emits local ConsoleRenderer output (`server/src/shared/logging.py:107`) or flat event-based JSON. Consequently agent events such as `x402.spend.settlement_unconfirmed` and `x402.payment.reconciliation_failed` disappear. HTTP 200 from MCP can still contain a tool/payment failure, so server-only success does not establish the agent accepted its receipt.

Reproduced offline: both synthetic flat structured agent error events returned None. Add explicit sanitized support for the agent's actual logging formats, preserve event-source distinctions and bounded failure categories, and test receipt/reconciliation failures without raw messages or private fields.

### P2 - Saved summary discards HTTP error identity

`mangrove-agent/scripts/watch_calls.py:117` increments only a count; `render` at line126 never retains route, source, method or status. Reproduced with a synthetic AGENT POST /mcp/ 503: summary says one HTTP error but contains neither /mcp/ nor 503. This defeats the requirement to investigate from the summary alone. Aggregate bounded, already-sanitized source/method/route/status tuples into the saved summary.

### P2 - Consent describes platform capabilities hidden from external clients

`MangroveAI/src/MangroveAI/domains/oauth/scopes.py:89` promises platform strategy, deployment, balance and marketplace reads. The final external policy deliberately removes those capabilities. `describe_request` passes this text to consent. This is misleading approval text, not a demonstrated authorization bypass. Replace it with the actual MangroveAI-only scope description and add a consent/catalog consistency regression.

### P3 - Release documentation still describes the abandoned external platform path

`mangrove-agent/docs/oauth-login.md:20-28` directs operators to the platform consent frontend and platform-origin consent URL. `MangroveAI/docs/mcp-oauth-design.md:1671` says ListPlatformStrategies is exposed to the connector. Update both to the developer-portal consent and external MangroveAI-only decisions, preserving Michael's separate platform access.

### Optional watcher hardening

Malformed structured input with a non-string message raises TypeError at `watch_calls.py:59`; the reader thread then exits without posting its completion sentinel (lines177-183), potentially leaving the watcher waiting. Reproduced with synthetic JSON. Validate message type and report reader completion in finally. The event queue is unbounded (line166); no sustained-volume/backpressure capacity test was performed. These are local diagnostic robustness concerns, not payment authorization defects.

## Checks performed in this audit

| Area | Result |
|---|---|
| Agent payer, uncertainty, sync transport | 219 passed |
| Watcher filtering and summary tests | 8 passed; additional negative probes reproduced the gaps above |
| Backend payment diagnostics, verifier, hook | 90 passed |
| External tool boundary, skill projection, canonical routing | 150 passed |
| Agent OAuth/setup/proxy/discovery | 260 passed after permitting disposable loopback fixtures |
| Backend authority/scopes unit tests | 46 passed |
| Backend native consent/authority real SQL tests | 27 passed in random disposable schemas |
| Backend approved catalog / read-scope boundaries | 146 passed; scope-only fixtures complemented by actual exposure tests |
| Gateway focused tests | 49 pass events including subtests; 2 database tests skipped because dedicated test DB unavailable |
| Frontend login redirect smoke | Both assertion groups passed; sandbox WebSocket-bind diagnostic did not prevent assertions |
| Frontend TypeScript noEmit/nonincremental | Passed |
| Generated agent instructions, shell syntax, diff whitespace | Passed |

Counts are grouped and should not be added blindly where suites overlap. Dependency deprecation/future-support warnings remain. Failed initial loopback-sandbox runs passed after granting fixture socket access; they were not treated as code regressions. No paid requests, production changes, commits, pushes or credential rotations were made during this audit. Earlier live testnet success is historical evidence, not a payment test performed by this audit.

## Security review and limits

No dedicated secret scanner was installed; none was installed for this audit. A redacted credential-pattern sweep over all changed full files in the three bounded sets found no candidate credential literals. Manual review covered sensitive flows, encryption, PKCE/state, redirects, live authority, tenant binding, token rotation, fail-closed behavior, payment reservations and diagnostic sanitization. Synthetic keys and public contract addresses in fixtures are not real credentials. This cannot prove absence of secrets/PII. Ignored credentials, live user databases, private log history and Git history were not broadly scanned. The known transcript exposure above is a separately evidenced operational incident.

Backend installed x402 is 2.19.0 and httpx 0.28.1; no standalone mcp distribution metadata was present in that container. Agent installed x402 is 2.22.0, MCP 1.30.0 and httpx 0.28.1. Its MangroveAI SDK imports the adjacent source checkout, not a verified published distribution. Frontend packages inspected: Firebase 12.17.1, Axios 1.20.0, Vite 8.1.5, TypeScript 5.9.3.

## Release gates

- Production OAuth remains disabled in configuration. Developer-portal deployment and production discovery/consent/browser checks are required; prior public endpoints returned404. No new production probe was performed in this audit.
- Deploy the gateway authority/org-echo implementation before enabling the matching AI client. Deploy migration117 through the normal migration lifecycle; it revokes legacy grants, requiring reconnection. Never roll back by reactivating invalidated grants.
- Rebuild the compiled skill graph with the authored external sections; a restart with an old graph is insufficient.
- Validate clean installs with published SDK artifacts, full CI, browser sign-in and callback behavior. Mainnet payments and load/concurrency capacity were not tested.
- The managed CDP switch was local ignored configuration. Production already specifies CDP in repository config, but its live runtime configuration was not audited.
- The changed TypeScript build-info file is generated workstation cache churn; consider excluding it from delivery. Its parsed root/version changes were inspected, not ignored as generated output.

## Per-file coverage

Every listed modified file's changed hunks, including deletions, were reviewed with relevant enclosing flow. Every listed new file was read completely. Generated build info was parsed and compared. Full-file redacted pattern review covered the same inventory. The audit report itself is excluded from its own review inventory.

| Repository / file | Reason | Reviewed scope | SHA-256 prefix |
|---|---|---|---|
| `MangroveAI/docs/mcp-oauth-design.md` | OAuth lifecycle / authority / login | All changed hunks + context | `7cbc58ee6f87490a` |
| `MangroveAI/docs/x402-payment-failure-policy.md` | Payment safety / diagnostics | All changed hunks + context | `16b7013e0cf4a886` |
| `MangroveAI/src/MangroveAI/domains/agent/michael/skills/strategy-composition/SKILL.md` | Canonical external tool separation | All changed hunks + context | `8df454ae57aa0905` |
| `MangroveAI/src/MangroveAI/domains/agent/michael/skills/strategy-management/SKILL.md` | Canonical external tool separation | All changed hunks + context | `91602dfc3fb8cf90` |
| `MangroveAI/src/MangroveAI/domains/agent/michael/tools/marketplace.py` | Canonical external tool separation | All changed hunks + context | `4b95c843bc54ad53` |
| `MangroveAI/src/MangroveAI/domains/agent/michael/tools/portfolio.py` | Canonical external tool separation | All changed hunks + context | `a6b700f64c8d64e1` |
| `MangroveAI/src/MangroveAI/domains/agent/michael/tools/product_facts.py` | Canonical external tool separation | All changed hunks + context | `0de9af15fb2a3ecc` |
| `MangroveAI/src/MangroveAI/domains/agent/michael/tools/skills.py` | Canonical external tool separation | All changed hunks + context | `fa7be411d15d6963` |
| `MangroveAI/src/MangroveAI/domains/agent/michael/tools/strategies.py` | Canonical external tool separation | All changed hunks + context | `2aad461bd61b15b4` |
| `MangroveAI/src/MangroveAI/domains/agent/michael/tools/trade_extras.py` | Canonical external tool separation | All changed hunks + context | `378edfa3246d5dd8` |
| `MangroveAI/src/MangroveAI/domains/agent/tooling/context.py` | Canonical external tool separation | All changed hunks + context | `c667f3edb8092d42` |
| `MangroveAI/src/MangroveAI/domains/agent/tooling/workflow_policy.py` | Canonical external tool separation | All changed hunks + context | `fc796237a39ecb8d` |
| `MangroveAI/src/MangroveAI/domains/ai_copilot/agentic/skills/shared_tool_adapter.py` | Required setup / routing / authority integration | All changed hunks + context | `bf36b5107e5901b1` |
| `MangroveAI/src/MangroveAI/domains/managers/agent_evaluations_db.py` | Required setup / routing / authority integration | All changed hunks + context | `ca12838395d3b399` |
| `MangroveAI/src/MangroveAI/domains/oauth/consent_routes.py` | OAuth lifecycle / authority / login | All changed hunks + context | `9a3b78972e4326fd` |
| `MangroveAI/src/MangroveAI/domains/oauth/db.py` | OAuth lifecycle / authority / login | All changed hunks + context | `07e0c89f5b07eb61` |
| `MangroveAI/src/MangroveAI/domains/oauth/routes.py` | OAuth lifecycle / authority / login | All changed hunks + context | `41679665ad98452d` |
| `MangroveAI/src/MangroveAI/domains/oauth/scopes.py` | OAuth lifecycle / authority / login | All changed hunks + context | `366d6568ec97fd22` |
| `MangroveAI/src/MangroveAI/domains/oauth/service.py` | OAuth lifecycle / authority / login | All changed hunks + context | `f271ad480e6cc566` |
| `MangroveAI/src/MangroveAI/domains/strategies/platform_client.py` | Canonical external tool separation | All changed hunks + context | `67e6fbca6cb0d443` |
| `MangroveAI/src/MangroveAI/domains/strategies/workflow_routes.py` | Canonical external tool separation | All changed hunks + context | `77fbb683b47d14b8` |
| `MangroveAI/src/MangroveAI/mcp/billable_scanner.py` | Canonical external tool separation | All changed hunks + context | `85c5642b0570fdc1` |
| `MangroveAI/src/MangroveAI/mcp/billable_taxonomy.py` | Canonical external tool separation | All changed hunks + context | `42d910cc6676563a` |
| `MangroveAI/src/MangroveAI/mcp/connect.py` | Canonical external tool separation | All changed hunks + context | `e07d6bd6566d7be7` |
| `MangroveAI/src/MangroveAI/mcp/michael_bridge.py` | Canonical external tool separation | All changed hunks + context | `d3dcaaf83a40ffd6` |
| `MangroveAI/src/MangroveAI/mcp/server.py` | Canonical external tool separation | All changed hunks + context | `5407e5d0556cd4a5` |
| `MangroveAI/src/MangroveAI/scripts/migrations/117_oauth_live_platform_authority.sql` | OAuth lifecycle / authority / login | Full new file | `694449d02bea6a2f` |
| `MangroveAI/src/MangroveAI/tests/domains/agent/michael/test_external_store_boundary.py` | Regression / negative coverage | Full new file | `0c61a788f6f5f551` |
| `MangroveAI/src/MangroveAI/tests/domains/agent/michael/test_list_platform_strategies.py` | Regression / negative coverage | All changed hunks + context | `9ac45bcbdae70a42` |
| `MangroveAI/src/MangroveAI/tests/domains/agent/michael/test_platform_evaluations.py` | Regression / negative coverage | All changed hunks + context | `d71e66b42cce9fa7` |
| `MangroveAI/src/MangroveAI/tests/domains/agent/michael/test_platform_trades_fallback.py` | Regression / negative coverage | All changed hunks + context | `74acce2bd27b1384` |
| `MangroveAI/src/MangroveAI/tests/domains/agent/michael/test_skills.py` | Regression / negative coverage | All changed hunks + context | `10459841f3af2db0` |
| `MangroveAI/src/MangroveAI/tests/mcp/test_external_platform_boundary.py` | Regression / negative coverage | Full new file | `d2bebf4bf3f6f4e1` |
| `MangroveAI/src/MangroveAI/tests/mcp/test_michael_bridge.py` | Regression / negative coverage | All changed hunks + context | `1e7e2ff35cdd7195` |
| `MangroveAI/src/MangroveAI/tests/mcp/test_michael_parity.py` | Regression / negative coverage | All changed hunks + context | `d3f7e7aa01963cf7` |
| `MangroveAI/src/MangroveAI/tests/mcp/test_strategy_workflow.py` | Regression / negative coverage | All changed hunks + context | `6b60878d5eae2750` |
| `MangroveAI/src/MangroveAI/tests/oauth/test_approved_catalog.py` | Regression / negative coverage | Full new file | `786afad6120511a6` |
| `MangroveAI/src/MangroveAI/tests/oauth/test_authority_boundaries.py` | Regression / negative coverage | Full new file | `47eea34dba864a91` |
| `MangroveAI/src/MangroveAI/tests/oauth/test_authority_database.py` | Regression / negative coverage | Full new file | `1804f3a9c14547c4` |
| `MangroveAI/src/MangroveAI/tests/oauth/test_native_consent_database.py` | Regression / negative coverage | Full new file | `bc62735bbbb9843f` |
| `MangroveAI/src/MangroveAI/tests/oauth/test_platform_authority.py` | Regression / negative coverage | Full new file | `1d40aa42fc819fed` |
| `MangroveAI/src/MangroveAI/tests/oauth/test_platform_read_boundary.py` | Regression / negative coverage | Full new file | `5bd379e079518de1` |
| `MangroveAI/src/MangroveAI/tests/oauth/test_read_scope_boundaries.py` | Regression / negative coverage | Full new file | `e4173857d5d0f135` |
| `MangroveAI/src/MangroveAI/tests/oauth/test_scopes.py` | Regression / negative coverage | All changed hunks + context | `823661cd02a7e818` |
| `MangroveAI/src/MangroveAI/tests/x402_enforcement/test_payment_diagnostics.py` | Regression / negative coverage | All changed hunks + context | `b356813743e0c70f` |
| `MangroveAI/src/MangroveAI/utils/auth/middleware.py` | OAuth lifecycle / authority / login | All changed hunks + context | `bc24d87cf0249dd1` |
| `MangroveAI/src/MangroveAI/utils/auth/oauth_authority.py` | OAuth lifecycle / authority / login | Full new file | `c3b67193b7a8036a` |
| `MangroveAI/src/MangroveAI/utils/auth/oauth_tokens.py` | OAuth lifecycle / authority / login | All changed hunks + context | `b04617f877179572` |
| `MangroveAI/src/MangroveAI/x402_enforcement/diagnostics.py` | Payment safety / diagnostics | All changed hunks + context | `006fb4e3159e9601` |
| `MangroveAI/src/MangroveAdmin/frontend/scripts/login-redirect.mjs` | Required setup / routing / authority integration | Full new file | `1100e2a25cb2fe6b` |
| `MangroveAI/src/MangroveAdmin/frontend/src/components/auth/AnonymousOnlyRoute.tsx` | OAuth lifecycle / authority / login | All changed hunks + context | `b6c3d56eefef1055` |
| `MangroveAI/src/MangroveAdmin/frontend/src/components/auth/ProtectedRoute.tsx` | OAuth lifecycle / authority / login | All changed hunks + context | `57d96664fea3a995` |
| `MangroveAI/src/MangroveAdmin/frontend/src/pages/LoginPage.tsx` | OAuth lifecycle / authority / login | All changed hunks + context | `e649dc80793ec4df` |
| `MangroveAI/src/MangroveAdmin/frontend/src/services/api/client.ts` | Required setup / routing / authority integration | All changed hunks + context | `f441f291bab8105c` |
| `MangroveAI/src/MangroveAdmin/frontend/src/services/firebase/config.ts` | Required setup / routing / authority integration | All changed hunks + context | `4bee99af2a136c5f` |
| `MangroveAI/src/MangroveAdmin/frontend/src/utils/loginRedirect.ts` | Required setup / routing / authority integration | Full new file | `a16372655ca57b4f` |
| `MangroveAI/src/MangroveAdmin/frontend/tsconfig.app.tsbuildinfo` | Generated frontend cache | Parsed generated delta | `6ae9fc4a9345000f` |
| `mangrove-agent/.claude/rules/trading-bot-workflow.md` | Release / client guidance | All changed hunks + context | `291ae3f0de35a7ec` |
| `mangrove-agent/README.md` | Release / client guidance | All changed hunks + context | `3cad0e72710f242e` |
| `mangrove-agent/docs/clients/sage-instructions.md` | Release / client guidance | All changed hunks + context | `802c9aebfc73741e` |
| `mangrove-agent/docs/oauth-login.md` | OAuth lifecycle / authority / login | Full new file | `2da7a71e25d1b2cd` |
| `mangrove-agent/scripts/setup.sh` | Required setup / routing / authority integration | All changed hunks + context | `e3786ef5e47dc376` |
| `mangrove-agent/scripts/setup_support.py` | Required setup / routing / authority integration | All changed hunks + context | `8ca041c34091afbe` |
| `mangrove-agent/scripts/test_watch_calls.py` | Regression / negative coverage | Full new file | `41eb416cf983eee8` |
| `mangrove-agent/scripts/watch_calls.py` | Sanitized diagnostic watcher | Full new file | `360b49ef2e528a3e` |
| `mangrove-agent/server/src/api/router.py` | Required setup / routing / authority integration | All changed hunks + context | `cbe49d4e825e54f8` |
| `mangrove-agent/server/src/api/routes/discovery.py` | Required setup / routing / authority integration | All changed hunks + context | `d462b5114cb48430` |
| `mangrove-agent/server/src/api/routes/oauth.py` | OAuth lifecycle / authority / login | Full new file | `c0e86ef2154c4642` |
| `mangrove-agent/server/src/config/configuration-keys.json` | Required setup / routing / authority integration | All changed hunks + context | `27a95565e8781a43` |
| `mangrove-agent/server/src/mcp/mangrove_proxy.py` | Canonical external tool separation | All changed hunks + context | `6503e899e92f7077` |
| `mangrove-agent/server/src/mcp/server.py` | Canonical external tool separation | All changed hunks + context | `ee06b0bc85e57794` |
| `mangrove-agent/server/src/mcp/tools.py` | Canonical external tool separation | All changed hunks + context | `b7281ed589946396` |
| `mangrove-agent/server/src/services/cex_oauth_service.py` | OAuth lifecycle / authority / login | All changed hunks + context | `23c74324c95f80ec` |
| `mangrove-agent/server/src/services/mangrove_oauth.py` | OAuth lifecycle / authority / login | Full new file | `6871d604a0f8db7b` |
| `mangrove-agent/server/src/services/marketplace.py` | Required setup / routing / authority integration | All changed hunks + context | `61060c481dc0947b` |
| `mangrove-agent/server/src/services/x402_payer.py` | Payment safety / diagnostics | All changed hunks + context | `1213a4c1881ea7c9` |
| `mangrove-agent/server/src/shared/clients/mangrove.py` | Required setup / routing / authority integration | All changed hunks + context | `918ee270f0780638` |
| `mangrove-agent/server/tests/integration/test_discovery_routes.py` | Regression / negative coverage | All changed hunks + context | `970d9321d943773d` |
| `mangrove-agent/server/tests/unit/test_mangrove_oauth.py` | Regression / negative coverage | Full new file | `9d98cec760593fa0` |
| `mangrove-agent/server/tests/unit/test_mangrove_proxy.py` | Regression / negative coverage | All changed hunks + context | `85c288ee90c2ed1a` |
| `mangrove-agent/server/tests/unit/test_oauth_tool_availability.py` | Regression / negative coverage | Full new file | `b89129428b665854` |
| `mangrove-agent/server/tests/unit/test_setup_flow.py` | Regression / negative coverage | All changed hunks + context | `0c3f3f646dc85c19` |
| `mangrove-agent/server/tests/unit/test_x402_payer.py` | Regression / negative coverage | All changed hunks + context | `d437b5f19e29ef60` |
| `mangrove-platform-backend/cmd/gateway/internal/handlers/agent_oauth_scope_boundary_test.go` | Regression / negative coverage | Full new file | `4cdd83c5b0463d6d` |
| `mangrove-platform-backend/cmd/gateway/internal/handlers/agent_oauth_scope_test.go` | Regression / negative coverage | Full new file | `a82edae886bac3d6` |
| `mangrove-platform-backend/cmd/gateway/internal/handlers/oauth_authority_handler.go` | OAuth lifecycle / authority / login | Full new file | `19c34eba6b363bbe` |
| `mangrove-platform-backend/cmd/gateway/internal/handlers/oauth_authority_handler_test.go` | Regression / negative coverage | Full new file | `33f9fe77d9e73a20` |
| `mangrove-platform-backend/cmd/gateway/internal/handlers/oauth_consent_handler.go` | OAuth lifecycle / authority / login | Full new file | `8e2d3ebe14147129` |
| `mangrove-platform-backend/cmd/gateway/internal/handlers/oauth_consent_handler_test.go` | Regression / negative coverage | Full new file | `27e7e093e2e39d1a` |
| `mangrove-platform-backend/cmd/gateway/internal/handlers/v2_handlers.go` | Required setup / routing / authority integration | All changed hunks + context | `8b4e692bf650fd1a` |
| `mangrove-platform-backend/cmd/gateway/internal/orgaccess/oauth_authority.go` | OAuth lifecycle / authority / login | Full new file | `af509df7c647ee26` |
| `mangrove-platform-backend/cmd/gateway/internal/orgaccess/oauth_authority_test.go` | Regression / negative coverage | Full new file | `3fdeda5d9935f971` |
| `mangrove-platform-backend/cmd/gateway/oauth_authority_route_test.go` | Regression / negative coverage | Full new file | `c752a221825facc0` |
| `mangrove-platform-backend/cmd/gateway/router.go` | Required setup / routing / authority integration | All changed hunks + context | `eaf5fffba9ce3550` |
