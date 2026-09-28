# Remote MCP payments

Normal agent signal tools continue to use SDK -> REST. The remote MCP payer is
an additional client entry point using the same vault, signer, budget and ledger.
It does not create another payment implementation or move secrets to the server.

## Client entry points

`pay_mcp(session, name=..., arguments=..., resource=..., wallet_address=...)`
uses a caller-owned MCP session. Its owner must provide a trusted endpoint,
deadline, authentication mode and cleanup. `resource` must identify that session's
endpoint; a session supplied by arbitrary untrusted code is not a security boundary.
Official MCP SDK wire dumps and exception bodies are filtered for both client
entry points, including transport tasks started before `pay_mcp` is called.
Ordinary SDK diagnostics remain available; custom sessions own their logging.

`pay_remote_mcp(endpoint, name=..., arguments=..., wallet_address=...,
operation_id=..., timeout=120)` owns the official MCP HTTP session and cleanup.
It requires an explicitly selected HTTPS endpoint (HTTP is allowed for loopback),
rejects URL credentials/query strings/fragments, disables redirects and ambient
proxies, and bounds the whole exchange. It is wallet mode only: it supplies no
API key and never switches from failed API-key authentication to payment.
SDK diagnostics containing signed metadata are suppressed during this owned call;
transport failure logs retain only the exception type.

For example, inside an authorized application workflow:

```python
result = await pay_remote_mcp(
    approved_endpoint,
    name="list_signals",
    arguments={"limit": 10, "category": "trend"},
    wallet_address=selected_wallet,
)
page = result.mcp_result["structuredContent"]
```

This can authorize a real payment within the existing budget. The caller must
select an approved endpoint and existing backed-up wallet. Tool arguments never
carry API keys or wallet secrets. MCP endpoints from tool output must not be
followed automatically.

`PaymentResult.body` retains its existing content-block representation.
`PaymentResult.mcp_result` additionally preserves the entire MCP result, including
`structuredContent`, all content blocks, `isError` and `_meta`. Existing `paid`,
`transaction`, `payer` and `network` fields retain validated receipt meanings.
A receipt proves server-reported settlement, not independent chain confirmation.
Cached results restore the same typed content blocks in `body` as fresh results,
including older stored responses that predate `mcp_result`.

## Recovery

The tool name and canonical JSON arguments are part of operation identity.
Different filters/pages are different work. Reusing an explicit operation UUID
with different arguments is rejected before contacting the endpoint.

For receivers advertising `mangrove/payment.idempotency=v1`, the existing
operation ledger encrypts the exact signed MCP metadata and a random private
recovery token before sending them. A lost response can be retried with the
reported operation ID and identical endpoint/tool/arguments: this resends the
original authorization, not a new payment. Completed responses are cached.
If an identical pending request is selected automatically, a recovery timeout
reports that original operation ID and its reservations, even when the caller
omitted the ID or supplied a new one.
Without advertised recovery support, uncertain signed operations remain pending;
the client does not manufacture a replacement signature.

Payment confirmation and fulfillment are separate. A receipt with pending status
records the charge but does not cache an unfinished response as complete.
Cancellation, missing/invalid receipts and connection failures retain signed
reservations. Consult existing payment status/reconciliation rather than resetting
the budget. Paid failures retain the existing refund observer's recovery headers;
refund eligibility and configuration are unchanged.

## Versions and verification

Verified client versions: MCP 1.30.0, x402 2.22.0, mangroveai 1.17.0. These match
`requirements.lock`; dependency floors now require the tested MCP/payment APIs.
MCP negotiates the receiver's supported revision, including MangroveAI's
2025-06-18. The x402 v2 MCP specification is pinned to upstream commit
`b74dd052155464ef8e1182646071bce47ba465ed` in `x402-foundation/x402`.

Run the payment unit tests and `tests/integration/test_remote_mcp.py` under
`ENVIRONMENT=test`. The latter exercises the official MCP HTTP client, real
local signing and the ledger against synthetic responses. It sends no funds.
Controlled testnet acceptance and deployed verification remain release gates.
