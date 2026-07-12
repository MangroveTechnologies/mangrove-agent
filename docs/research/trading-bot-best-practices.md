# Building personal trading agents: best practices & security (research brief)

**Date:** 2026-07-12. Web survey (Jan–Jul 2026 window preferred, canonical evergreen
sources included) grounding this repo's docs and defaults in current external practice.
Each claim carries its source; items we could not corroborate are excluded or flagged.

## Where this repo already matches external consensus

Point-for-point alignment found between mangrove-agent's shipped invariants and the
2025–2026 consensus for (LLM-driven) trading bots:

| Invariant here | External consensus |
|---|---|
| Local keygen + local signing, keys never leave the machine | Hummingbot's canonical positioning: local client, encrypted keys, "never exposes them to any third parties" — https://github.com/hummingbot/hummingbot |
| Signing allowlist (`_validate_sign_target`: 1inch routers + approve only), EIP-7702 refusal | "Policy engine" wallets — allowlists, caps, confirmation outside the LLM — are the emerging standard for AI agents: https://crypto-economy.com/on-chain-wallets-become-policy-engines-how-ai-agents-rewrite-crypto-authorization/ ; EIP-7702 drainer industrialization post-Pectra: https://www.coindesk.com/tech/2025/06/02/post-pectra-upgrade-malicious-ethereum-contracts-are-trying-to-drain-wallets-but-to-no-avail-wintermute , https://www.zealynx.io/research/smart-contracts/eip-7702-wallet-security |
| Paper before live, wallet-free paper mode | freqtrade: "Always start by running a trading bot in Dry-run… do not engage money before you understand how it works" — https://www.freqtrade.io/en/stable/ ; Hummingbot's keyless paper mode — https://hummingbot.org/client/global-configs/paper-trade/ |
| Small first allocation (10–20% cap), explicit confirm at live | 1–2% risk/trade ceiling (https://www.luxalgo.com/blog/risk-management-strategies-for-algo-trading/); staged live ramp 25%→50%→100% of target only as live tracks backtest (https://www.quantvps.com/blog/how-to-create-a-trading-algorithm) |
| Secrets never in chat; vault-token flow; paste-blocking hooks | The "lethal trifecta" rule — an agent with private data + untrusted content + the ability to act externally is the exploitable shape: https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/ |
| Kraken BYOK least-privilege key guidance | Kraken official: keys "equivalent to your Kraken username and password"; minimal permissions, rotation, withdrawal off — https://support.kraken.com/articles/api-key-security |

## Why the paranoia is warranted (recent incidents)

- **Grok/Bankr drain, May 2026 (~$175–200K):** Morse-code-encoded instructions in an X
  reply prompt-injected an agent into transferring ~3B tokens. Lesson: prompts are not a
  security boundary; enforce policy at the signing layer.
  https://www.giskard.ai/knowledge/how-grok-got-prompt-injected-an-x-user-drained-150-000-from-an-ai-wallet
- **ElizaOS memory injection (Princeton/Sentient):** agents "gaslit" via planted false
  memories into bad trades. https://decrypt.co/318200/elizaos-vulnerability-ai-gaslit-losing-millions
- **AiXBT (~$100K ETH, Mar 2025):** social-input conditioning triggered transfers.
  https://decrypt.co/310510/aixbt-ai-influencer-hacked-100k-ethereum
- **MCP tool poisoning:** formally catalogued (OWASP MCP Top 10, MCP03:2025 —
  https://owasp.org/www-project-mcp-top-10/2025/MCP03-2025%E2%80%93Tool-Poisoning);
  first malicious MCP package in the wild Sep 2025.
- **Supply chain:** Sep 2025 npm attack (chalk/debug, 2B+ weekly downloads) hooked
  `window.ethereum` to redirect wallet txs (https://www.sygnia.co/threat-reports-and-advisories/npm-supply-chain-attack-september-2025/);
  Jul 2026 `@injectivelabs/sdk-ts` backdoored to steal keys/mnemonics
  (https://www.bleepingcomputer.com/news/security/injective-sdk-on-npm-infected-with-cryptocurrency-wallet-stealer/).
  Lesson: pin dependencies; a signing allowlist bounds the blast radius even when an SDK
  is compromised.
- **Expectations:** a 925K-wallet study found AI trading agents lost users ~$192M net by
  Feb 2026 (https://www.thestreet.com/crypto/trading/study-finds-most-ai-crypto-trading-agents-arent-really-trading);
  CFTC advisory: "AI technology can't predict the future or sudden market changes"
  (https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/AITradingBots.html).

## Practices reflected in the tutorials (with backing)

1. Paper first; check paper-vs-backtest **parity** before live (freqtrade bot-basics/backtesting docs).
2. Small first allocation; scale only as live tracks backtest (QuantVPS 2026; LuxAlgo 1–2%/trade).
3. CEX keys: withdrawal-disabled, minimally scoped, IP-allowlisted, rotated (~90 days)
   (Kraken official; https://coinledger.io/blog/the-ultimate-guide-to-api-access-for-your-crypto-exchange-accounts ;
   https://docs.cdp.coinbase.com/get-started/authentication/security-best-practices).
4. Untrusted content must never be able to move funds (lethal trifecta; every 2025–2026 drain above).
5. Trust out-of-sample, not the optimizer: walk-forward validation, ≥30% held-out data,
   parameter plateaus over cliffs (https://blog.quantinsti.com/walk-forward-optimization-introduction/ ;
   https://blog.pickmytrade.trade/trading-strategy-validation-backtest-overfitting/).
6. No profit promises; regulator-grade expectation setting (CFTC advisory).

## Candidate product follow-ups surfaced by the survey (not doc gaps)

- An explicit **kill-switch / circuit-breaker** story: portfolio max-drawdown halt,
  per-strategy consecutive-loss breaker with cooldown (patterns: LuxAlgo;
  https://github.com/richkuo/go-trader).
- **Walk-forward / out-of-sample** as a first-class backtest mode (currently the sweep
  path can approximate it manually).
- Kraken **WebSocket v2 `executions`** streaming for fill sync instead of polling
  (https://docs.kraken.com/api/docs/websocket-v2/executions/), and subaccount-per-strategy
  isolation (https://blog.kraken.com/product/api/unlocked-6-multi-strategy-operations-subaccounts-api-keys).

## Flagged / not used

arXiv IDs surfaced but not independently corroborated (2512.12924, 2512.12174,
2601.09625, 2604.11430) — excluded from load-bearing claims per fabrication-risk policy.
Vendor-blog-only figures ("$65M API-key losses", KuCoin "$45M breach") excluded.
