# Facilitator runbook

Reference for running the *Bots & Bytes* mangrove-agent workshop.
Scannable while you're on the floor — not a script to read line by
line.

Assumes attendees have gone through
[`workshop-prereqs.md`](workshop-prereqs.md) before arriving. If they
haven't, plan to lose 20–30 minutes getting them to parity before the
session can start.

## Session shape

**Target duration: 2.5 hours.** Core workshop: 2 hours; buffer: 30
min for Q&A, troubleshooting, edge cases.

| Phase | Chapter(s) | Time | Funds? |
|---|---|---|---|
| Intro + safety framing | 01 | 15 min | no |
| Architecture tour | 02 | 10 min | no |
| Local setup | 03 | 20 min (inc. troubleshooting) | no |
| Author + backtest a strategy | 04 | 30 min | no |
| Paper mode | 05 | 15 min | no |
| **Mid-session checkpoint** | — | 5 min | — |
| Wallet setup (optional) | 06 | 20 min | yes |
| Going live (optional) | 07 | 20 min | yes |
| Monitor + wrap | 08 + Q&A | 15 min | no |

Live-trading chapters (06–08) are **optional per attendee**. Some
will stop at 05 with a paper strategy running. That's success.

## 12 hours before the workshop

- [ ] Confirm Mangrove API keys are available (hosted at
      https://mangrovedeveloper.ai). If you're handing them out,
      verify they work: `curl -H "X-API-Key: <KEY>"
      <MANGROVEMARKETS_BASE_URL>/health`.
- [ ] Test a fresh clone yourself from scratch on a machine you
      haven't used for dev: `git clone` → `./scripts/setup.sh` →
      Stage 0 tour → paper strategy → verify.
- [ ] Confirm the reference-strategies library is loading
      (`search_reference_strategies` returns 15+ entries).
- [ ] Confirm `mangroveai`, `mangrovemarkets` SDK versions in
      `server/requirements.txt` resolve: `pip install --dry-run -r
      server/requirements.txt`.
- [ ] Pre-warm a Base-network USDC source so you can front funds to
      an attendee if needed (rare but happens).
- [ ] Have `docs/workshop-prereqs.md` URL ready to share if anyone
      arrives without pre-reqs done.

## 30 minutes before the workshop

- [ ] Project your laptop running the bot — attendees see the
      reference setup working before they attempt their own.
- [ ] Know where attendees will physically plug in / connect to
      Wi-Fi. Bandwidth matters for the first `pip install`.
- [ ] Share the repo URL + pre-reqs URL on screen /
      whiteboard / Slack.
- [ ] Check Mangrove's API status. If hosted is degraded, the
      backtest chapter falls apart — have a plan.

## Session flow

### Opening (0:00–0:15, Chapter 01)

Your job here is **tone-setting**, not teaching. Introduce:

- What Claude Code is (chat-driven CLI, runs locally, connects to
  Anthropic's API for the LLM).
- What the bot is (Mangrove-powered, local, self-custody).
- The three safety nets (key-paste block, backup gate, slippage
  cap). Point at why each exists. Don't gloss.
- Paper before live, always.

Leave time for one or two "wait, so you're telling me..." questions.
If nobody asks anything, toss a rhetorical: "Who here has ever
accidentally left a private key in a terminal history?"

### Architecture tour (0:15–0:25, Chapter 02)

Drop into the ASCII diagram. Key point: **your laptop is the trust
boundary.** Most AI trading bots on the market are the opposite
(hosted, custodial, share-your-keys). This one isn't. That framing
lands on the security-minded attendees.

Ten minutes is enough. If you're over, skip.

### Setup (0:25–0:45, Chapter 03)

Most failure-prone phase. Walk through step-by-step. Common issues:

- **Python version wrong.** `python3 --version` returns 3.9 or older.
  Fix: install 3.11 via `brew` / python.org, confirm PATH. Budget
  5 min.
- **Port 9080 conflict.** Usually another dev tool or a previous
  mangrove-agent instance. `lsof -iTCP:9080 -sTCP:LISTEN` to identify;
  kill or use `BARE_PORT=9082`.
- **MCP not connecting.** `claude mcp list` shows "Failed to
  connect." Confirm server is on the port you think it is; confirm
  the registration URL matches. Re-register if needed.
- **Stage 0 greeter doesn't fire.** Either tools didn't load (MCP
  issue above) or `.claude/.onboarded` already exists. Remove the
  onboarded file and restart Claude Code.

If an attendee is stuck at setup for more than 10 minutes, pair
them with you or another facilitator — don't let them block the
rest of the room.

### First strategy (0:45–1:15, Chapter 04)

The most rewarding chapter. Attendees go from zero to "here's a real
strategy with a real backtest" in 30 minutes.

**Thing facilitators often need to clarify:**

- **PASS vs MARGINAL vs FAIL.** 6 thresholds. A MARGINAL strategy
  is fine to paper; a FAIL should not be papered. Show the
  threshold table if attendees are confused.
- **"0 trades in the backtest."** Not a failure. Suggest extending
  the lookback or dropping to a shorter timeframe. This case comes
  up ~1 in 5 attempts in quiet markets.
- **Reference strategy selection.** Attendees sometimes want "the
  fancy one." Push them toward category: trend-following or
  momentum for the first pass — those reliably produce trades in
  most market regimes.
- **Autonomous mode.** Mention it exists but don't default to it —
  ref-first is faster and more interpretable.

If Mangrove's API is slow or timing out, reframe: "This is a good
moment to talk about what this looks like when infra is degraded —
the bot surfaces the error rather than silently failing."

### Paper mode (1:15–1:30, Chapter 05)

Fast chapter. Three things:

1. Promote → scheduler registers → verify `active_cron_jobs: 1`.
2. Force a tick via `evaluate_strategy` so they see something
   immediately (don't wait for the cron).
3. Read `list_evaluations`, `list_trades` — recognize the rows.

**If a live tick produces 0 orders, that's normal.** Don't let
attendees declare the strategy "broken." A strategy is a watcher,
not a trader — it trades when its conditions are met.

Optionally: demo the scheduler-persists-across-restart property
(kill server → restart → cron still registered). Impressive for
people who've wrangled cron before.

### Mid-session checkpoint (1:30–1:35)

**Pause.** Read the room.

- Who got through Chapter 05 cleanly? Great, they can continue to
  06 if they want.
- Who's stuck at 03 or 04? Pair them with a facilitator, get them
  un-stuck, or let them stop at their current point.
- Who wants to go live, who wants to stay paper? Announce both
  paths are valid.

Offer: "Chapters 06–08 are optional. You can stop here and come
back to live trading any time. If you're not sure, stop here."

This is the moment to signal that stopping is not failure. Some
attendees need permission.

### Wallet + live (1:35–2:15, Chapters 06–07)

**Highest-risk phase.** Real money, real mistakes possible.

**Things that routinely go wrong:**

- **Wrong network.** Attendee sends USDC on Ethereum mainnet to a
  Base address. Funds are stuck. Resolution: bridge at
  base.org/bridge. Prevent by calling this out 3x during chapter
  06.
- **Forgot to save secret before closing the reveal terminal.**
  Recovery: `./scripts/reveal-secret.sh --address <addr>` regenerates
  a fresh reveal session (master key still intact).
- **Skipped `confirm-backup.sh`.** Bot refuses live promotion.
  Attendee frustrated. Resolution: confirm the backup. Don't let
  them work around the gate.
- **Slippage validator rejects the call.** Attendee put `0.01`
  thinking it's fine. Explain: 0.01 = 1%, cap is 0.0025 = 0.25%.
  Use 0.002 for USDC/ETH.

**Do not let attendees send more than 5 USDC on their first live
trade.** If they insist, push back once, then let them — but
document it in your mental model so you can debrief later.

**If an attendee's tx fails on-chain** (e.g., reverted, gas too
low), it's generally a one-off. Rerun the tick. If it fails twice
in a row, punt to Chapter 08 troubleshooting.

### Monitor + wrap (2:15–2:30, Chapter 08 + Q&A)

- Point attendees at `list_trades`, `list_evaluations`,
  `portfolio_value`, `portfolio_pnl`.
- Remind them: the bot keeps running as long as uvicorn is up.
  Closing the laptop stops ticks; restarting resumes them.
- Remind them: their funds are self-custody. They can withdraw to
  MetaMask using the key they saved, any time.
- Point at `tutorials/trading-app/08-*.md` as the reference they
  come back to post-workshop.

Open Q&A.

## Common attendee questions + crisp answers

**"Can I run this on a cloud server?"**
Not v1. Local-first is a design choice (see `docs/architecture.md`).
If they're persistent, point them at
`server/src/config/prod-config.json` as a starting hint but don't
take responsibility for supporting it.

**"Why can't I set slippage higher?"**
Because 0.25% is the threshold where sandwich attacks on Base's
deepest pools start to be profitable for them. Above that, you're
giving MEV bots free money. We cap rather than warn.

**"Can the bot trade on Binance?"**
No. This is a DEX-only bot — 1inch on Base. CEX integration is not
planned.

**"Is this financial advice?"**
No. Mangrove provides informational signal analysis; the bot
executes what you tell it. Nothing in either constitutes a
recommendation. If the attendee is about to deploy significant
capital, say this out loud.

**"My backtest shows 120% IRR, should I put my whole net worth in?"**
No. Backtests are hypotheses, not predictions. Live behavior often
diverges significantly. Rule of thumb: first live allocation caps
at $1. Scale after seeing real behavior.

**"What if Mangrove goes down?"**
The bot can't author or evaluate strategies — those depend on the
hosted API. Existing paper / live strategies in the local DB stay,
but their next tick fails gracefully (`status: "error"` in
`list_evaluations`). Funds are untouched because they're
self-custody. Attendee can withdraw to MetaMask anytime.

## Red flags — when to punt

- **Attendee wants to deploy with significant capital immediately.**
  Slow them down. Recommend a week of paper first.
- **Attendee asks you to hold their keys "for safekeeping."** Don't.
  Self-custody is the point. Help them set up a password manager.
- **Attendee is clearly drunk / high / distressed.** Politely
  redirect to paper only. No live chapter for them.
- **Attendee has a laptop that can't run Python 3.11+ (old
  Chromebook, work-locked machine).** Suggest they observe the
  workshop and attempt solo later on a personal machine.

## Post-workshop

- [ ] Collect feedback on what worked / what didn't.
- [ ] Note which chapter(s) had the most stumbles — candidate for
      rewriting before the next session.
- [ ] Follow up with attendees who stopped mid-workshop to see if
      they completed at home.
- [ ] File GitHub issues for anything repeatedly broken during the
      session (setup.sh edge cases, MCP registration quirks, etc.).
- [ ] Thank Mangrove if anyone there fielded live API support.

## Useful emergency commands

All run in the attendee's terminal (not Claude Code):

```bash
# "My server isn't responding."
tail -50 agent-data/bare.log
ps aux | grep uvicorn
kill $(cat agent-data/bare.pid) 2>/dev/null ; ./scripts/setup.sh --yes --no-mcp --no-verify

# "Claude Code can't see my tools."
claude mcp list
# If not Connected:
claude mcp remove mangrove-agent
claude mcp add -s local -t http mangrove-agent http://localhost:9080/mcp/ --header "X-API-Key: dev-key-1"
# Then restart Claude Code in the repo dir.

# "I want to nuke everything and start over."
kill $(cat agent-data/bare.pid) 2>/dev/null
rm -rf agent-data/
claude mcp remove mangrove-agent 2>/dev/null
./scripts/setup.sh

# "I want to pause my strategy immediately."
# In Claude Code:
#   "Pause my live strategy."
# Or from the terminal:
curl -s -H 'X-API-Key: dev-key-1' -X PATCH \
  -H 'Content-Type: application/json' \
  -d '{"status": "inactive"}' \
  http://localhost:9080/api/v1/agent/strategies/<id>/status
```

## End state

By the end of the workshop, every attendee should have:

- A local mangrove-agent running on their laptop.
- At least one strategy in `paper` status.
- Optionally: a wallet, a funded wallet, and a live trade on-chain.
- A clear sense of how to stop, restart, and resume the bot.
- A pointer at Chapter 08 for post-workshop operation.

If any attendee leaves with a live strategy running but unclear on
how to pause or stop it, you failed. Don't let that happen.

Good luck.
