# B6 final audit — 2026-09-17

## Scope and result

Reviewed the local B6 changes in mangrove-agent and the companion MangroveAI
changes: discovery bindings and caching, REST/MCP metadata, REST skill payment
challenges, compatibility, and disclosure risks. No unrelated chat/provider
implementation, config credentials, dependencies, migrations or lockfiles changed.
No B6 blocker was found in this scope. Changes remain uncommitted; this is not a
claim that all providers or a hosted production deployment have been validated.

## Audit corrections

MangroveAI's public catalog now validates meter identifiers and decimal price
strings. It never stringifies arbitrary metadata objects, and withholds malformed
prices, including conflicting valid/invalid definitions. A regression test checks
that private metadata cannot be converted to text or published.

Optional pricing enrichment now fails without breaking the normal MCP tool list.
The response and warning do not include exception/configuration details. Existing
result metadata is preserved when adding prices. A regression test covers this.

## Discovery acceptance

The running receiver on port 5002 returned enabled REST payments on Base Sepolia
(`eip155:84532`), with 99 price entries. All nonempty bindings in the agent's 69
pricing bindings resolved. Explicit unknown-price bindings remain unknown.

The user's agent on port 9082 was stopped. The actual agent REST discovery route
and MCP server were therefore exercised in an isolated in-process ASGI harness
against the live receiver, using the local destination configuration. No scheduler
was started and no wallet or paid tool was invoked. Both agent catalogs listed
106 tools. For these four tools, REST and MCP pricing metadata matched exactly:

| Tool | USDC per upstream request | Status |
| --- | ---: | --- |
| list_signals | 0.001 | Fresh |
| get_market_data | 0.005 | Fresh |
| get_global_market | 0.005 | Fresh |
| get_whale_activity | 0.02 | Fresh |

Prices are observations from this audit, not constants in the implementation.
The receiver's default callable tools were identical with and without the opt-in
pricing extension. A read-only payment-ledger snapshot before/after was identical.
This validates discovery without making payments; it does not retest execution.

## Regression evidence

- Full agent suite: 985 passed, 2 skipped (live tests disabled).
- Receiver MCP, payment, v402, auth, metering and affected domain suites:
  601 passed, 5 skipped before the final audit corrections.
- After corrections, all receiver MCP/payment tests: 370 passed, including the
  two new disclosure/availability regression cases.
- Agent source lint, skill-manifest verification, changed receiver module/test
  lint, and both repositories' whitespace checks passed.
- Earlier chat-specific verification on these changes: 1,566 passed, 48 skipped,
  one expected failure. Two backtest tests failed for missing DB_HOST; both were
  reproduced on an isolated committed baseline. Chat code remains unchanged.

## Security and privacy review

Reviewed credential isolation, destination validation, redirect rejection,
bounded network reads, untrusted metadata parsing, cache expiry/failure behavior,
safe error messages, and payment/quota separation. Discovery uses an anonymous
client independent of the payment SDK and never signs or retries payment.

Scanned changed/new source, tests and docs in both repos for configured secret
values, private-key markers, common access-token patterns and personal home paths.
No matches were found. Configured secret values were compared only in memory and
not printed. Actual discovery responses also contained none of the configured
secret values or private filesystem/key markers checked. Public pricing entries
are limited to meter IDs and amounts; user, wallet and credential data are not
part of that schema. This is a scoped code/disclosure review, not a penetration
test or an assertion that every existing repository file is secret-free.

## Remaining boundaries

- Restart MangroveAI workers for the final two catalog safeguards. Refresh the
  agent price cache and reconnect clients to refresh tool descriptions.
- The WhaleAlert provider's 404 and outstanding signed authorizations remain
  separate issues. No retry, reconciliation, ledger release or budget reset was
  performed by this audit.
- Live API-key/quota acceptance and hosted/mainnet deployment acceptance are not
  established by the mocked regression suites.
- The pre-existing anonymous agent status route exposes operational counts,
  budget information and DB path. B6 removes raw catalog exception text, but
  does not redesign this local status contract. Keep the agent bound to loopback;
  exposing it as a multi-user hosted service requires a separate access review.
- No service restart, commit, push, deployment or paid request was performed.
