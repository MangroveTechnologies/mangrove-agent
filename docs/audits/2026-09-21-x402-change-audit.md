# x402 payment recovery and refund change audit

Date: 2026-09-21

## Scope and verdict

Reviewed the current working changes against HEAD, including untracked source,
migrations, tests, and documentation, in mangrove-agent and MangroveAI on
`fix/x402-payment-uncertainty`. This is a focused change audit, not a certification
of every existing component or deployment.

No unintended file deletions or unrelated feature additions were identified.
Removed payment handling was replaced with durable operation recovery and
conservative accounting. Strategy creation retains its existing validation and
persistence flow; the new preflight exception distinguishes failures before
mutation for refund eligibility. Deferred skill consolidation remains separate.

No real credentials or personal data were identified in the reviewed changes.
The changed-file pattern scan covered private-key blocks, common provider token
formats, literal secret assignments, personal home paths, and email-like text.
Remaining matches were synthetic URL credential fixtures on test/invalid domains.
Synthetic signing keys remain test-only. This scan does not establish that the
entire repository history, ignored files, runtime logs, or deployment are clean.

## Findings fixed

- Async HTTP payment entry points now require HTTPS except for explicit loopback
  development hosts. Validation runs before wallet access or operation recovery.
- Async requests require finite positive timeouts. Caller-supplied recovery tokens
  are stripped; only stored operation credentials can supply recovery tokens.
- The default synchronous HTTP transport ignores environment configuration,
  consistent with the asynchronous payment path.
- Both payment diagnostic sanitizers redact persisted payment/recovery header
  fields and signed refund transactions, including nested structures.
- Removed a pre-existing personal filesystem path from the changed documentation
  and corrected obsolete descriptions of retry authorization behavior.

Added regression coverage for insecure URLs, unbounded timeouts, accepted HTTPS
and loopback requests, caller recovery-token removal, and nested sensitive fields.
Existing mocked remote payer endpoints now use HTTPS.

## Payment and refund safety reviewed

- Pending amounts continue consuming spending limits; unrelated operations can
  proceed within the remaining budget. Uncertainty cannot create a fresh payment
  authorization for the same pending operation.
- Recovery uses durable identity and private recovery credentials. Agent recovery
  credentials and cached results are encrypted at rest.
- Refund processing is opt-in. The worker validates original payment evidence and
  binds the refund to the receiving wallet, original payer, network, token, and
  amount. Signed transfer bytes are persisted before broadcast and reused after
  ambiguous failures; a timeout does not authorize a replacement transfer.
- Agent refund credits require independent finalized transfer evidence and apply
  once to the original budget period. Original charge records remain intact.
- No live payments, refunds, historical wallet RPC queries, application database
  modifications, or service restarts were performed during this audit.

## Validation

- Full agent suite: **1,272 passed, 2 skipped**, with 133 warnings.
- Receiver x402, MCP, and v402 suites: **444 passed**, including refund tests,
  using disposable PostgreSQL. The test database was removed afterward.
- Focused operation tests: **13 passed**.
- Changed-file whitespace checks passed in both repositories. Focused Ruff checks
  passed for the audit-modified operation, transport, redaction, and operation-test
  files; this is not a claim of repository-wide lint cleanliness.

## Remaining release limits

- Nine strategy ensure-endpoint tests previously failed on both the working
  version and unchanged HEAD because the fixtures reference `ema_crossover` that
  the current strategy library does not recognize. The payment suites above are
  green; the broader strategy suite is not claimed to be green.
- Refunds remain disabled for normal local acceptance. The actual receiving-wallet
  provider integration and live refund acceptance are not validated; the bundled
  signer supports a protected local EOA key file.
- Approved reconciliation RPC configuration, historical uncertain-payment
  resolution, remote CI, and deployment acceptance remain separate release work.
- No fresh dependency vulnerability database scan or full Git-history secret scan
  was performed. Local tests and source review cannot replace those release checks.

The reviewed changes are locally regression-tested after hardening. Blanket
production readiness is not asserted while these release limits remain.
