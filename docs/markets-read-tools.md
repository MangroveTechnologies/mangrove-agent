# Markets read tools

Wallet, portfolio, token, price and DEX quote reads are owned by the Markets server,
not by this agent. The agent discovers version 1 `mangrove/markets` read contracts
from the configured Markets MCP catalog (`MANGROVEMARKETS_BASE_URL`) with a bounded
catalog client, and advertises them under their Markets names. A Markets server that
does not supply these tools simply has none advertised; there is no fallback to local
wrappers. Remote mutations without a supported ownership contract are not exposed
through this path.

The server supplies arguments, defaults, descriptions and results. A local `_agent`
object in each call carries controls that never reach Markets: the local `api_key`,
the selected `wallet_address` and a recovery `operation_id`. The agent removes it
before dispatch, and sends the configured upstream credential only to the configured
Markets endpoint. Native MCP content, structured results, errors and metadata are
preserved without SDK model coercion.

Tools with the `marketplace_`, `dex_`, `oneinch_` or `wallet_` prefix belong to
Markets. The local tools `marketplace_prepare` and `marketplace_submit` are the only
exceptions. See [marketplace wallets](marketplace-wallets.md) for ownership actions.

## Retired local tools and their replacements

Earlier agent builds shipped local wrappers for these reads. They are retired: calling
one returns a `RETIRED_TOOL` error that tells you to discover the current Markets tool.

| Retired agent tool | Discover from Markets |
| --- | --- |
| `get_balances` | `oneinch_balances` (takes `wallet`, not `address`) |
| `portfolio_value`, `portfolio_pnl`, `portfolio_tokens`, `portfolio_defi` | The corresponding `oneinch_portfolio_*` names |
| `portfolio_history` | `oneinch_history`; returns the native history envelope, not an SDK transaction list |
| `list_dex_venues` | No supported replacement |
| `get_swap_quote` | `dex_get_quote`; see units below |
| `get_tx_status` | `dex_tx_status` |
| `get_token_info`, `get_token_search` | `oneinch_token_info`, `oneinch_token_search` |
| `get_spot_price`, `get_gas_price`, `get_allowances` | `oneinch_spot_price`, `oneinch_gas_price`, `oneinch_allowances` |
| `get_dex_chart` | No equivalent replacement |

`oneinch_chart` is a distinct wallet portfolio-value chart. Do not send it token pairs
or read it as price candles. The generic Markets `wallet_balance` and
`wallet_transactions` tools are not implemented and are not advertised. XRPL-specific
reads keep their own names. Remote read contracts accept no private keys. Local wallet
creation and import, signing, approval, spend controls, Kraken (BYOK) access and
execution records remain local.

## Units

`dex_get_quote` uses the server's native units: EVM amounts are in smallest-token
units. For example, 0.01 ETH is `10000000000000000` wei. Resolve the token's decimals
before converting, and never forward a human-unit amount unchanged. Results keep the
server's native units.

The local REST quote route (`POST /api/v1/agent/dex/quote`) and the `execute_swap` tool
are different: they take human-unit amounts and convert them to base units themselves.
Rediscover the Markets schemas after the server changes.

## Access and payments

Markets read tools support API-key access and per-read x402 payment, as declared in
each tool's contract:

- **API-key mode.** With `MANGROVE_API_KEY` configured, reads are authenticated calls
  and need no wallet, backup confirmation, encryption key or payment budget.
- **Wallet mode.** Without a key, reads use the agent's payer, spending limits,
  payment journal and recovery controls. Prices are set by Markets and quoted in its
  402 challenge.
- **No fallback.** An invalid or under-scoped API key never falls back to wallet
  payment.

The server validates arguments before it issues a challenge, prepares the provider
response before settlement, and replays the saved response for the same payment. A
provider failure does not trigger settlement.

The x402 fee buys a read. It does not change DEX swap billing, and quotes still use
`mode=standard`. Remote signing and swap execution are not enabled through this path.

Marketplace reads and purchases keep their own quota or payer, receipts, operation
identity and recovery. See [payment operations](x402-payment-uncertainty.md).
`hello_mangrove` stays callable for diagnostics but is not listed in normal MCP tool
discovery.
