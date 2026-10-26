# Markets read migration

Deploy the compatible Markets server before this agent. The agent discovers
version 1 `mangrove/markets` read contracts through the existing bounded Markets
catalog client. An older server does not supply these tools; there is no fallback
to the retired wrappers. Remote mutations without a supported ownership contract
are not exposed through this path.

The server supplies arguments, defaults, descriptions and results. `_agent.api_key`
is a local credential and is removed before dispatch; the configured upstream
credential is sent only to the configured Markets endpoint. Native MCP content,
structured results, errors and metadata are preserved without SDK model coercion.

## Names and compatibility

| Retired agent tool | Discover from Markets |
| --- | --- |
| `get_balances` | `oneinch_balances` (`wallet`, not `address`) |
| `portfolio_value`, `portfolio_pnl`, `portfolio_tokens`, `portfolio_defi` | Corresponding `oneinch_portfolio_*` names |
| `portfolio_history` | `oneinch_history`; native history envelope, not an SDK transaction list |
| `list_dex_venues` | No supported replacement; placeholder venue/pair discovery is hidden |
| `get_swap_quote` | `dex_get_quote`; see units below |
| `get_tx_status` | `dex_tx_status` |
| `get_token_info`, `get_token_search` | `oneinch_token_info`, `oneinch_token_search` |
| `get_spot_price`, `get_gas_price`, `get_allowances` | `oneinch_spot_price`, `oneinch_gas_price`, `oneinch_allowances` |
| `get_dex_chart` | Retired broken token-pair candle wrapper; no equivalent replacement |

`oneinch_chart` is a distinct wallet portfolio-value chart. Do not send token pairs
or interpret it as price candles. Generic server `wallet_balance` and
`wallet_transactions` remain unimplemented and hidden; XRPL-specific reads retain
their own names. `wallet_create` remains retired. No private keys are accepted by
the remote read contracts. Local wallet creation/import, signing, approval,
spend controls, BYOK CEX access and execution records remain local.

`dex_get_quote` uses the server's native units: EVM amounts are smallest-token
units, unlike the retired human-unit wrapper. For example, 0.01 ETH is
10000000000000000 wei. Resolve token decimals before converting; never forward a
cached human-unit request unchanged. Results retain native server units. The
local REST quote adapter and `execute_swap` still use the existing human-unit
conversion service; neither has changed units. Rediscover schemas after upgrading.

## Access and payments

The seventeen declared reads support API-key access and x402 at $0.01 USDC per
successful read. In wallet mode the existing payer, spend limits, payment journal
and recovery controls are reused. The server validates arguments before issuing a
challenge, prepares the provider response before settlement, and replays the saved
response for the same payment. A provider failure does not trigger settlement.
Invalid or under-scoped API keys never fall back to wallet payment. These reads
retain their existing API-key scopes without adding marketplace quota debits.

The x402 fee buys a read; it does not change DEX swap billing mode. Quotes still
use `mode=standard`. Remote signing and swap execution are not enabled.

Existing marketplace reads and purchases retain their existing quota/payer,
receipts, operation identity and recovery. No new payment or recovery system is
introduced. `hello_mangrove` remains directly callable for diagnostics but is
excluded from normal MCP and REST business discovery.

## Verification status

The user reported 1,786 passing server tests with 3 skips and an initial agent
run of 1,684 passed, 8 failed and 2 skipped. Corrections for the eight agent
failures were followed by a user-reported focused run of 98 passed. The new
x402 and SDK changes have not yet been tested. The implementation agent has not
run tests. Provider availability, installed package compatibility,
deployed revisions and live settlement/recovery still require evidence. Do not
equate advertised implementation with healthy provider credentials or release
acceptance. No live spending is part of these source changes.
