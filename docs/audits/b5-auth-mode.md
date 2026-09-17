# B5 audit — 2026-09-17

Scope: automatic MangroveAI authentication selection, bundled configuration,
normal signal REST/MCP entry points, and the inherited x402 payment safeguards.
MangroveAI receiver code was inspected for its signal filter contract; no receiver
changes were made. This is not an application-wide security certification.

## Findings fixed

- Signal REST routes and MCP list/detail/search/match tools forwarded raw SDK
  errors or allowed them to reach framework error handling. An upstream error can
  echo tokens, user input or other private data. These paths now return fixed
  failure messages; synthetic token/email regression cases exercise real SDK
  error responses. No actual credential disclosure was observed.
- Detail/search/match tools flattened payment domain errors into generic signal
  failures. They now preserve structured errors such as `X402_SPEND_CAP_EXCEEDED`.
  Tests assert no signed retry or ledger reservation occurs when the cap refuses.
- Catalog category filtering happened after the requested limit. It now reaches
  the upstream catalog query before pagination, preventing an unrelated first
  page from hiding matching results. Text-search category filtering remains local
  because the SDK search request has no category field.
- REST pagination now rejects limits outside 1–100 and negative offsets before
  making a potentially billable request. The MCP catalog tool retains its
  bounded multi-page limit of 1–1000.

## Requirements checked

- Configured key selects the original SDK key path; authentication failures do
  not invoke the payment factory. Absent/blank/null selects payment mode.
- SDK dotenv loading is disabled. Payment destinations come from bundled data
  or explicit config overrides. Ambient SDK credentials fail closed at transport.
- Local authentication remains required and its key is not forwarded upstream.
- Network, USDC asset, backup confirmation and cumulative spend-cap checks remain
  in the payment path. Concurrent reservations cannot exceed the cap; uncertain
  authorizations remain counted. No budget reset or additional live payment was
  performed during this audit.
- Redirect/origin guards, unsigned initial requests, finite payment timeouts,
  fresh authorization nonces and one paid retry remain covered by existing tests.
- All production `MangroveAI(...)` construction remains in the shared factory.
  Cached client construction is serialized and reset closes the cached pools.
- Existing configuration loads without URL migration; Secret Manager resolution
  is retained. Service selection does not change wallet or payment network.

## Secret and privacy review

Changed files were checked for known configured credential values and common
private-key/token signatures without printing credentials. No matches remained
after excluding the non-secret master-key *path* and keyring service *name*.
No local config, database, master-key file or log is tracked. This limited scan
does not establish that all historical commits or arbitrary secret formats are
clean. Payment diagnostics redact addresses and remove URL queries/userinfo;
the authenticated local payment ledger intentionally retains payment metadata.

## Open findings and acceptance limits

1. **P2 — Existing non-signal error forwarding still exposes raw upstream text.**
   For example, `api/routes/on_chain.py::whale_activity` wraps `str(exception)`
   into a public SDK error; multiple MCP tools do the same. A synthetic exception
   reproduced this without network access. This predates B5 and was not expanded
   into a cross-application rewrite here. It needs a consistent safe error
   contract before claiming application-wide protection against error leakage.
2. **Live valid-key/quota acceptance remains open.** The available configured key
   returned 401. Invalid-key failure without payment passed, but a currently
   valid key and quota evidence are needed to close the success regression gate.
3. Live evidence covers two signal reads on Base Sepolia and a free local spend
   status call. It does not establish correctness of all 106 tools, mainnet
   deployment or high-concurrency shared hosting. Existing synchronous SDK calls
   inside async handlers also mean throughput should not be inferred from these
   functional tests.

B5 remains a draft PR pending its live acceptance gate and human review. The
no-signup installer remains B9; B6–B9 were not silently included or marked done.

## Verification

Final Python 3.12 full-suite run: **924 passed, 2 skipped**, 133 existing
deprecation warnings, 47.71 seconds. Ruff on source and changed tests, generated
skills manifest verification, and whitespace checks passed. The 13 new cases
cover private error text, preserved spend-cap errors, category pagination and
rejection of invalid REST pagination before upstream access.
