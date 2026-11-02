# Remote MCP payments

The agent can pay for a tool call on a remote MCP server over x402. This page is for
developers extending the agent. It explains the payer entry points, how a payment is
identified and recovered, and where the agent uses them. The payer lives in
`server/src/services/x402_payer.py` and reuses the same wallet vault, signer, spending
budget and payment ledger as the REST payer. It adds no second payment implementation
and moves no secrets to the server.

## Where the agent uses it

The agent forwards MangroveAI tools over MCP (`server/src/mcp/mangrove_proxy.py`):

- With an API key configured, or for a public free read, the call is authenticated or
  free and no payment is made.
- Otherwise the call goes through `pay_on_open_mcp_session` in wallet mode, over a
  session the proxy keeps open across calls.

Authentication failures never switch an API-key call to payment.

Markets reads use the same payer: see [Markets read tools](markets-read-tools.md).

## Entry points

`pay_mcp(session, name=..., arguments=..., resource=..., wallet_address=...)` pays a
tool call on a caller-owned MCP session. The caller provides a trusted endpoint, the
deadline, the authentication mode and cleanup. `resource` must identify that session's
endpoint, and it must be HTTPS (HTTP is allowed for loopback) without credentials,
query strings or fragments. A session supplied by untrusted code is not a security
boundary.

`pay_on_open_mcp_session(session, endpoint, name=..., arguments=...,
wallet_address=..., operation_id=..., timeout=...)` wraps `pay_mcp` for a session the
caller has already initialized and keeps open. It applies the deadline and the same
failure reporting as `pay_remote_mcp`. The caller owns the session's lifetime.

`pay_remote_mcp(endpoint, name=..., arguments=..., wallet_address=...,
operation_id=..., timeout=...)` owns the whole exchange: it opens the official MCP
HTTP session, pays, and cleans up. It requires an explicitly selected HTTPS endpoint
(HTTP is allowed for loopback), rejects URL credentials, query strings and fragments,
disables redirects and ambient proxies, and bounds the whole exchange with a finite,
positive `timeout`. It runs in wallet mode only: it supplies no API key and never
switches from a failed API-key authentication to payment.

`pay_remote_mcp` and `pay_on_open_mcp_session` suppress SDK diagnostics that contain
signed metadata while the call runs. With `pay_mcp`, a custom session owns its own
logging.

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

This can authorize a real payment within the existing budget. The caller must select
an approved endpoint and an existing backed-up wallet. Tool arguments never carry API
keys or wallet secrets. Never follow an MCP endpoint that appears in tool output.

## Results

`PaymentResult.body` holds the MCP content blocks. `PaymentResult.mcp_result` keeps the
entire MCP result, including `structuredContent`, all content blocks, `isError` and
`_meta`. The `paid`, `transaction`, `payer` and `network` fields carry validated receipt
data. A receipt proves server-reported settlement, not independent chain confirmation.
Cached results restore the same typed content blocks as fresh results.

## Identity and recovery

The tool name and the canonical JSON of its arguments are part of the operation
identity, along with the payer, network and endpoint. Different filters or pages are
different work. Reusing an explicit operation UUID with different arguments is rejected
before the endpoint is contacted.

For a receiver that advertises `mangrove/payment` idempotency `v1`, the operation
ledger encrypts the exact signed MCP metadata and a random private recovery token
before sending them. If the response is lost, retry with the reported operation ID and
the identical endpoint, tool and arguments. That resends the original authorization
rather than creating a new payment, and a completed response is cached. If an
identical pending request is selected automatically, a recovery timeout reports the
original operation ID and its reservations, even when the caller omitted the ID.

Without advertised recovery support, an uncertain signed operation stays pending. The
client never creates a replacement signature.

Payment confirmation and fulfillment are separate. A receipt with a pending status
records the charge but does not cache an unfinished response as complete.
Cancellation, a missing or invalid receipt and connection failures keep signed
reservations counted. Use the payment status and reconciliation tools described in
[payment operations](x402-payment-uncertainty.md) rather than resetting the budget.

If payment succeeds but local result storage fails, the payer raises
`X402PaymentUncertain` with the original operation ID. The pending operation keeps its
encrypted recovery proof. Retry that same operation after storage is restored; do not
authorize another payment.

## Dependencies and tests

The tested MCP and x402 client versions are pinned in `server/requirements.lock`. The
MCP client negotiates the revision the receiver supports.

Run the payment unit tests and `server/tests/integration/test_remote_mcp.py` with
`ENVIRONMENT=test`. The integration test exercises the official MCP HTTP client, real
local signing and the ledger against synthetic responses, and sends no funds.
