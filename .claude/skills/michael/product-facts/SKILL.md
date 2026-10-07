---
name: product-facts
description: Answer questions about Mangrove custody, loss alerts, creator payments and product guarantees from maintained facts and the person's current notification preferences.
uses-tools: []
---

<!-- Synced from MangroveTechnologies/MangroveAI src/MangroveAI/domains/agent/michael/skills/product-facts/SKILL.md by scripts/sync-michael-skills.py. Edit the upstream skill and regenerate. -->

These instructions describe MangroveAI server tools and server-owned records. Discover current tools and input schemas through MCP before calling them; report unavailable capabilities without substituting a local implementation. Local execution workflows use agent_ tools and local strategy IDs. Do not pass IDs between those stores.

# Ground product answers

Use maintained facts and the person's applicable settings to explain custody, alerts, creator fees and risk. Keep claims conditional and make uncertainty or source conflicts explicit.

This is ONLY Mangrove's own published policy -- custody arrangement, alert delivery, creator fee
schedule, risk DISCLOSURE (the legal statement that trading can lose money), platform limits --
never how markets, instruments or trades behave. A question about margin, liquidation, stops,
slippage or funding is trading mechanics, not Mangrove policy, however much it sounds like "risk":
use `query_knowledge op=ask` instead.

## Resolve the applicable facts

Call `query_product_facts` for the relevant topic before making product claims. State the applicable conditions. Cite `public_sources` only for the claims they support; audit `provenance` is internal evidence and must not be exposed as product UI filenames or private repository links. Describe source conflicts honestly and check the applicable arrangement or active plan rather than silently replacing published terms with implementation behavior. Custody depends on the selected wallet and execution arrangement; clarify that arrangement when it is unknown. Do not promise exchange-only custody, capital protection, automatic loss alerts, universal creator fee percentages, or fixed payout dates.

## Check this person's alert preferences

When the person asks about their alerts, call `get_notification_settings` and distinguish their actual preferences from the supported event types and delivery conditions. An enabled strategy preference is not evidence that every losing position triggers a warning. If the read is unavailable, relay its concrete reason and still answer the general product question from the maintained facts. Do not claim there is no settings tool.

## Explain payment conditions

For creator payments, explain the plan and eligible-fee conditions, configured recipient wallet, collection and settlement. Do not infer the person's fees or payout status from general policy.
