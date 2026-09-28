# Signal listing through the agent

The local MCP `list_signals` tool collects up to its requested `limit` (1–1000,
50 by default). The local REST `/api/v1/agent/signals` endpoint returns one page,
with its existing requested limit range of 1–100. Both delegate to the shared
signals service, which uses the public MangroveAI SDK.

Browse routing is agent → SDK `signals.list()` → `GET /api/v1/signals/` → the
backend's retained billable operation. Calling the agent through MCP does not
turn this into a call to MangroveAI's hosted MCP endpoint. Hosted MCP transport
acceptance is separate.

## Results and continuation

The server controls page size. The agent follows `next_offset`, keeps filters on
every request and requests only the remaining records on the final page. It does
not assume a server ceiling of 30, 50 or 100. A request for 100 records with a
30-row server ceiling makes requests returning 30, 30, 30 and 10 records.

MCP preserves `items,total`, where `total` means the number returned. REST
preserves the catalogue total and reports the server's effective `limit`,
`offset`, `has_more`, `next_offset` and optional filter metadata. REST consumers
must use `next_offset` rather than adding their requested limit.

`category`, `regime_direction` and `role` pass to the browse endpoint. The agent
does not maintain a second list of accepted backend filter values or filter the
browse response again. The historical normalization of category is preserved.

A nonempty `search` selects SDK `signals.search()` and
`POST /api/v1/signals/search`. It remains a single request, including the
historical category refinement of search results. Regime/role with search is
rejected before a request rather than silently ignored. Search and browse have
separate price bindings; discovery labels them as alternatives, not additive fees.

## Cost and failure bounds

Every requested page can be billable, including a valid empty page. Before the
first result the workflow permits at most one page per requested record. The
first response tightens this to the number needed at the observed effective page
size. Later smaller pages cannot expand that bound. Single-page REST and search
are bounded to one logical SDK request.

The first actual wallet authorization establishes the maximum per-payment price
for that invocation; subsequent increases are refused before signing. The global
spending cap still governs the first and every later payment. This is not an
upfront fixed-price quote or an aggregate reservation: each actual authorization
is reserved atomically by the existing durable ledger. Unused page capacity has
no reservation to refund. Signed-but-unresolved payments stay reserved according
to the existing payer policy. A failed collection returns an error, not a
successful truncated list; already settled pages are not automatically refunded.

Discovery prices retain freshness, unknown-price and network indicators. They
are estimates, not authorization amounts. Actual signed amounts are checked at
the ledger boundary; unknown discovery prices never become an implicit free price.

A stale/non-advancing/empty-more response aborts collection. Authentication
failures do not change the configured billing mode. No local key or private
wallet material is passed as a remote business argument. Synchronous SDK calls
run in a worker thread so they do not block the application's event loop.

## Candidate SDK testing and release gate

This change requires the corrected SDK pagination contract (`SignalListPage`).
An older SDK is rejected before any listing request. The local candidate still
has wheel metadata 1.16.0; it is **not** the published 1.16.0 package and must not
be published under that version. The capability check exists because version
metadata alone cannot distinguish the candidate during development.

Production dependency files deliberately remain unchanged until the corrected
SDK is published. Before releasing this agent, update both `requirements.txt`
and `requirements.lock` to the actual released version (1.17.0 proposed), run
clean-install integration tests, and record the package provenance. Installing
the current lock alone is not sufficient to run this new listing implementation.
Do not deploy the 30-row backend cutover before client adoption is verified.

For local verification, create a disposable virtual environment, install the
agent dependencies, then install the candidate wheel over the locked SDK:

```bash
python3.12 -m venv /tmp/agent-signals-test
/tmp/agent-signals-test/bin/python -m pip install -r server/requirements.lock
/tmp/agent-signals-test/bin/python -m pip install --no-deps --force-reinstall /absolute/path/to/candidate.whl
```

From `server/`, run the signals-service, payment-budget, pricing, client, spend
and x402 transport tests using that interpreter. From the repository root, run
`python -B scripts/mcp_contract.py` with the same interpreter. The full normal
CI suite remains required. Offline transport fixtures exercise the real SDK,
local signing and ledger; they do not prove deployed backend billing or hosted
MCP interoperability.
