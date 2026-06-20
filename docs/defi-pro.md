# DeFi Pro tools — starter

Five DeFiLlama **Pro** analytics tools are exposed over MCP. They surface the
paid DeFiLlama API tier through MangroveAI, so the same access rules apply.

| Tool | Returns |
|---|---|
| `get_token_unlocks` | token unlock schedules + supply metrics (supply-shock signal) |
| `get_perp_funding` | aggregated perpetual funding rates across venues |
| `get_treasuries` | protocol treasury holdings (crowd-positioning signal) |
| `get_etf_flows` | ETF net flows |
| `get_lending_borrow_rates` | lending / borrow rates across markets |

The free DeFi tools (`get_protocol_tvl`, `get_chain_tvl`, `get_stablecoin_metrics`)
work on any plan and need no special entitlement.

## Access rules

- **Plan**: the five Pro tools require a **Pro, Startup, or Enterprise** plan.
  On an unentitled plan the tool returns an error carrying
  `code: TIER_UPGRADE_REQUIRED` (HTTP 403 upstream) — upgrade to use them.
- **x402**: an x402-paid call pays per request and is not subscription-checked.
- **Monthly cap**: 1,000 Pro calls per account per month. Past the cap the tool
  returns `error: quota_exceeded` (HTTP 429 upstream). The cap is uniform across
  paid tiers and protects the shared DeFiLlama budget.

## 1. Configure the MCP server (Claude Code)

Copy the drop-in config and add your MangroveAI key:

```bash
cp .mcp.json.example .mcp.json
# edit .mcp.json: set MANGROVE_API_KEY to a key on a Pro/Startup/Enterprise plan
```

Restart Claude Code so it picks up the MCP server, then verify the tools are
registered:

```bash
./scripts/verify_quickstart.sh --bare   # tool catalog should include the DeFi Pro tools
```

## 2. Call a Pro tool

Ask the agent (it routes to the MCP tool):

> "What token unlocks are coming up? Use get_token_unlocks."

Or invoke the tool directly from any MCP client. Each tool takes a single
optional `api_key` argument (falls back to the server-configured key) and
returns the JSON envelope:

```json
{ "success": true, "count": 338, "data": [ { "name": "Chainlink", "circSupply": 748009578.99, "events": [ ... ] }, ... ] }
```

On an unentitled plan you get `{"error": "...", "code": "TIER_UPGRADE_REQUIRED"}`;
past the monthly cap, `{"error": "quota_exceeded", "resource_type": "defillama_pro_calls"}`.

## Programmatic use (SDK)

For scripts/services, the MangroveAI SDK is the quickest path —
`pip install mangroveai`, then see `examples/defi_pro.py` in the SDK repo for one
runnable call per Pro method with 403/429 handling. The SDK methods are
`client.defi.get_token_unlocks()` / `get_perp_funding()` / `get_treasuries()` /
`get_etf_flows()` / `get_lending_borrow_rates()`.
