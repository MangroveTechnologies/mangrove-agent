# DeFi Pro analytics tools

MangroveAI serves a set of DeFi analytics tools backed by DeFiLlama's paid API tier.
The agent does not implement them. It discovers them from MangroveAI's MCP server and
forwards each call, so the plan checks, quotas and errors below are MangroveAI's, and
the agent returns them unchanged. Their schemas and descriptions come from the live
catalog; use `discover_tools` or your client's tool list rather than this page for the
exact current contract.

| Tool | Returns |
| --- | --- |
| `get_token_unlocks` | Token unlock schedules and supply metrics |
| `get_perp_funding_rates` | Perpetual funding rates across venues |
| `get_treasuries` | Protocol treasury holdings |
| `get_etf_flows` | ETF net flows |
| `get_lending_rates` | Lending and borrow rates across markets |

The related free tools `get_protocol_tvl`, `get_chain_tvl` and `get_stablecoin_metrics`
are served the same way. `get_protocol_tvl` takes a `protocol_name`; the five tools in
the table take no arguments.

## Access

Access follows how the agent is authenticated to MangroveAI:

- **API-key mode.** The key configured as `MANGROVE_API_KEY` identifies your account.
  MangroveAI decides from your plan whether a Pro tool is available and returns an
  error, such as a tier-upgrade or quota error, when it is not.
- **Wallet mode.** Without a key, the agent pays per request over x402 from its local
  wallet, within your spending limits. See [payment operations](x402-payment-uncertainty.md).

The tools take no local credential argument. The agent applies the credential it is
configured with.

## Using the tools

With the agent connected to your MCP client (see the README setup), ask for the data
in plain language, for example "What token unlocks are coming up?", or call the tool
directly from any MCP client.
