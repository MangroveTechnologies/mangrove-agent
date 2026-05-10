# Bots & Bytes — Facilitator Run-of-Show (v5)

**Workshop:** Build Your Own Trading Bot with Claude Code + Mangrove
**Date:** Friday, April 24, 2026
**Length:** 3 hours (180 min)
**Deck:** `bots-and-bytes-v5-reordered.pptx` — 104 slides
**Pre-read:** `workshop-setup-guide.md` (sent to attendees ahead)
**Handout:** `workshop-attendee-survival-kit.md` (printed at the door)

---

## SYMBOL LEGEND

| Symbol | Meaning |
|---|---|
| ▲ | **STOP GATE** — verify before moving on |
| ★ | **KEY MOMENT** — the slide that earns the segment |
| ● | **INSIGHT** — the line worth landing |
| ! | **WATCH-OUT** — common failure mode |
| § | **TA CUE** — what the TAs should be doing |
| ▶ | **LIVE PROMPT** — type this in your terminal |
| ◷ | **TIMING** — pace check |
| ◇ | **OPTIONAL** — cut if behind |

---

## TIMING AT A GLANCE

| Time | Part | Slides | Length |
|---|---|---|---|
| 00:00–00:05 | A — Opening | 1–3 | 5 min |
| 00:05–00:25 | B — Install + Foundations | 4–12 | 20 min |
| 00:25–00:50 | C — Primitives Deep Dive | 13–35 | 25 min |
| 00:50–01:10 | D — Safety Posture | 36–49 | 20 min |
| 01:10–01:35 | E — Architecture + Mangrove Internals | 50–74 | 25 min |
| 01:35–01:45 | **Break** | 75 | 10 min |
| 01:45–02:05 | F — Author a Strategy | 76–84 | 20 min |
| 02:05–02:17 | G — Paper Mode | 85–89 | 12 min |
| 02:17–02:30 | H — Wallet Setup (Ch 6) | 90–93 | 13 min |
| 02:30–02:45 | I — Going Live (Ch 7) | 94–98 | 15 min |
| 02:45–02:50 | J — Monitor & Extend (Ch 8) | 99–100 | 5 min |
| 02:50–03:00 | K — Wrap | 101–104 | 10 min |

---

## TA BRIEFING — READ TO BOTH TAs BEFORE DOORS OPEN

### TA1 — "Setup TA"
- **Primary mandate:** keep installs moving in Parts A–B, support live trading in Part I.
- **Workshop arc:**
  - **A–B (00:00–00:25):** maximum effort. Walk every aisle. Goal: nobody is stuck silently.
  - **C–E (00:25–01:35):** spot-check anyone behind. Light load.
  - **Break:** active — flag who looked stuck so we can ambush them.
  - **F–G (01:45–02:17):** fielding prompt-syntax issues + Mangrove API errors.
  - **H–I (02:17–02:45):** pair with anyone going live. Sit beside them for the first swap.

### TA2 — "Floor TA"
- **Primary mandate:** room awareness + hands-on coding support.
- **Workshop arc:**
  - **A–B:** secondary install support. Watch for the quiet ones — they're the most stuck.
  - **C–D:** track engagement. Signal me with thumbs-up/down if pace is wrong.
  - **E:** Mangrove internals is dense — watch for glazed eyes; signal me to compress.
  - **F–G:** primary floor support during live coding. Roam constantly.
  - **H–I:** pair with anyone NOT going live to keep them engaged on concepts.

### Both TAs — every part
- ▲ If 3+ people raise hands at once, one of you breaks me (Tim) out to address the whole room.
- ▲ If someone is silently stuck for >2 minutes, intervene unprompted.
- ◇ At each stop gate, give me a thumb count of how many are clear.
- » Use the buddy system — pair the stuck with a working neighbor before debugging solo.

### Hand signals during talking
- [+] = "room is with you"
- [-] = "room is lost, slow down or simplify"
- [!] = "stop, real problem in the back, come over"
- ◇ = "cycle back, you skipped something"

---

## PRE-WORKSHOP CHECKLIST

### 90 minutes before
- [ ] Setup guide PDF emailed to all registrants (`workshop-setup-guide.pdf`)
- [ ] Survival kits printed: 1 per attendee + 5 extra (`workshop-attendee-survival-kit.pdf`)
- [ ] My laptop: charged, plugged in, adapter tested
- [ ] My copy of repo cloned, `setup.sh` run, `claude` greeter fires cleanly
- [ ] Backup terminal session open with repo loaded
- [ ] Pre-funded demo wallet (~$10 USDC on Base) ready for Part I live demo
- [ ] Slide deck `bots-and-bytes-v5-reordered.pptx` open in presenter mode
- [ ] Setup guide PDF open in second tab (for my own reference during install support)

### 30 minutes before
- [ ] Projector tested with actual slide — colors, font sizes
- [ ] Terminal zoom 18pt minimum
- [ ] WiFi password on screen
- [ ] Mangrove signup page (`mangrovedeveloper.ai`) open in a tab
- [ ] GitHub repo open in another tab
- [ ] 3 spare Mangrove API keys on sticky notes for anyone who can't self-serve
- [ ] TAs briefed (above)

### As doors open
- [ ] Both TAs at the door, handing out survival kits + greeting attendees
- [ ] Point arrivals at the install steps from the setup guide
- [ ] Pair anyone less-technical with someone who has Claude Code already

---

## MINUTE-BY-MINUTE

### ▶ PART A — OPENING (00:00–00:05 · Slides 1–3)

#### Slide 1 — Title
- ◷ 30 seconds.
- "Welcome to Bots & Bytes. We have three hours. We're going to build a working trading bot together, and you'll leave with both a running bot AND the mental model to operate one safely."

#### Slide 2 — Tim Darrah
- ◷ 60 seconds max.
- Don't over-credential. They came to build, not hear your CV.
- One line on why agentic finance, one line on why this workshop.

#### Slide 3 — What you'll leave with
- ◷ 30 seconds.
- 4 outcomes: bot · Claude Code fluency · safety mental model · curriculum access.
- "If any of these were why you came, we're aligned."

---

### ■ PART B — INSTALL + FOUNDATIONS (00:05–00:25 · Slides 4–12)

> ! **The most important 20 minutes of the workshop.** Setup runs in parallel with the foundations content. Lose this window and you'll spend the rest of the day catching up.

> § **TA1 + TA2:** maximum effort. Both of you, walking the room, checking screens, asking "where are you?". Don't wait to be flagged.

#### Slide 4 — Install Now ★
- "Look up. Four steps. **Start them now.** I'll talk for the next 20 minutes — install while I do."
- Walk the four steps:
  - VS Code → claude.com/download → `code.visualstudio.com`
  - Claude Desktop (ships with Claude Code CLI)
  - Mangrove API key → `mangrovedeveloper.ai`
  - Clone + `./scripts/setup.sh` (Python 3.11+)
- ! Most common failure: Python 3.11+ missing on Windows. The setup guide flagged this; not everyone read it.
- ! Second most common: Claude Pro/Max subscription not active.
- ▲ **Stop gate at 00:25:** count thumbs. If <70% have `claude --version` working, extend Part B by 5 min and steal it from Part E.

#### Slide 5 — What is Claude
- ◷ 90 seconds.
- ● "Claude is a model family by Anthropic. Peer to GPT, Gemini — designed safety-first."
- Three sizes: Haiku, Sonnet, Opus. Trade speed ⇄ capability.

#### Slide 6 — Where you use Claude
- ◷ 90 seconds.
- claude.ai (web) · Claude Desktop (app) · **Claude Code (terminal — what we're using)** · API.
- ● "Same model, different interfaces. We're using the one designed for agentic coding."

#### Slide 7 — What Claude Code is
- ◷ 2 min.
- Three properties: **Local** (code stays on your machine), **Visible** (every tool call shown inline), **Customizable** (CLAUDE.md, skills, hooks, MCP).

#### Slide 8 — The agentic loop ★
- ◷ 3 min. Walk the diagram.
- User Prompt → READ → THINK → ACT → OBSERVE → loop.
- ● "Old chatbots: one turn, done. Agents run the loop until a task is complete. The autonomy lives in the loop."

#### Slide 9 — Opus in 2026
- ◷ 2 min.
- 4.5 (Nov 2025) · 4.6 (Feb 2026) · **4.7 (April 16, 2026 — what we're running today)**.
- ● "1M context, best Opus to date for long-horizon agentic work. Trading bots qualify."

#### Slide 10 — CLAUDE.md
- ◷ 90 seconds.
- "How you onboard Claude to your project. Short, specific, load-bearing."

#### Slide 11 — Read. The. Tool. Calls. ★
- ◷ 2 min.
- ● **The single most important habit of the day.** Land it hard.
- "When the agent does something you didn't expect, the tool call log tells you exactly what."
- "If you remember nothing else, remember to read the tool calls."

#### Slide 12 — Four ways to shape behavior
- ◷ 90 seconds. Bridge slide.
- "Skills, hooks, MCP, plugins. We'll spend the next 25 minutes on each. Then we'll use them all."

> ▲ **STOP GATE — end of Part B (00:25)**
> - § TAs report: how many have `claude --version` working?
> - If ≥70%: proceed to Part C on schedule.
> - If <70%: extend by 5 min, take it from Part E (Mangrove internals).
> - If <50%: extend by 10 min, take 5 from Part C and 5 from Part E.

---

### □ PART C — PRIMITIVES DEEP DIVE (00:25–00:50 · Slides 13–35)

> ◷ 25 minutes for 23 slides ≈ 1 min/slide. **Brisk.** This is reference material — you're naming concepts, not teaching from scratch.

> § **TA2:** monitor engagement signals. If 3+ glazed faces, signal me to compress.

#### Slide 13 — Companion deck title
- ◷ 30 sec. "Now: how each primitive actually works."

#### Slide 14 — Four primitives, one agent
- ◷ 60 sec. The 2x2: SKILLS / HOOKS / MCP / PLUGINS.

#### Slides 15–19 — SKILLS (5 slides, ~5 min)
- 15: Section divider · 30 sec
- 16: Skills — what + why · 90 sec
- 17: How skills work (progressive disclosure) · 90 sec
- 18: A real SKILL.md (`create-strategy`) · 90 sec
- 19: Learn more · 30 sec
- ● **Land:** "A skill is onboarding docs the agent can actually use. Write one, Claude does the thing the same way every time."

#### Slides 20–24 — HOOKS (5 slides, ~5 min)
- 20: Section divider · 30 sec
- 21: Hooks — what + why · 90 sec
- 22: Eight lifecycle events · 2 min
- 23: Block secrets example (real `block-wallet-secrets.sh`) · 90 sec
- 24: Learn more · 30 sec
- ● **Land:** "Non-zero exit = refuse. The key-paste hook in our bot stops your prompt before it leaves the terminal."

#### Slides 25–29 — MCP (5 slides, ~5 min)
- 25: Section divider · 30 sec
- 26: MCP servers — what + why · 90 sec
- 27: Client / server / transport · 90 sec
- 28: `claude mcp add` example · 90 sec
- 29: Learn more · 30 sec
- ● **Land:** "Claude Code doesn't know about trading out of the box. MCP is how it gets to. One register command = 41 new tools."

#### Slides 30–34 — PLUGINS (5 slides, ~5 min)
- 30: Section divider · 30 sec
- 31: Plugins — what + why · 90 sec
- 32: Marketplace → installed flow · 90 sec
- 33: `/plugin` example · 90 sec
- 34: Learn more · 30 sec
- ● **Land:** "Plugins bundle skills + hooks + MCP servers. Marketplaces are catalogs. Distribute your team's setup as one install."

#### Slide 35 — How they compose
- ◷ 60 sec. The whole stack diagram.
- ● **Land:** "Start with a skill. Add hooks when safety matters. Add MCP when you need external tools. Bundle as a plugin when it's time to share."

---

### ◆ PART D — SAFETY POSTURE (00:50–01:10 · Slides 36–49)

> ★ **The differentiator of the workshop.** Generic AI safety → war stories → bot-specific nets, all in one arc.

> § **TA1:** during slide 37 (war stories), watch for attendees with their own stories. We may want to invite one to share. **TA2:** mute notifications, watch the clock — slide 37 will run long if not capped.

#### Slide 36 — A word on AI safety
- ◷ 90 sec. Somber framing.
- ● "Agentic AI is leverage. Multiplies right AND wrong. In coding: data loss, false success. In finance: money gone. Not recoverable."

#### Slide 37 — War stories ★
- ◷ **HARD CAP 5 minutes.** Set it out loud.
- Four category cards. Riff on each:
  - **Data loss** — your story
  - **False success** — your story
  - **Prompt injection** — your story or paraphrase a published incident
  - **Runaway autonomy** — your story
- ! This slide can run 15 min if you let it. **Don't.**

#### Slide 38 — What is agentic finance?
- ◷ 90 sec.
- Three tiers: trading bots (today) · DeFi autopilots · autonomous treasuries.
- ● "We're doing Level 01. Everything above it builds on the same primitives."

#### Slide 39 — What changes when finance gets agentic
- ◷ 2 min.
- Upside: democratization, speed, personalization, composability.
- Downside: correlated failure, attribution, new attack surfaces, regulatory arbitrage.

#### Slide 40 — Cybersecurity when data is code ★
- ◷ 2 min.
- Old paradigm (keep them out — SQL injection, firewalls) vs new paradigm (stop authorized agents from being manipulated).
- ● **Land hard:** "Prompt injection is the new SQL injection. Your data is your code. A README can pwn you."

#### Slide 41 — You're about to do this
- ◷ 60 sec. Bridge.
- "Four rules apply doubly to what comes next."

#### Slide 42 — Autonomy cuts both ways (pullquote)
- ◷ 45 sec. Read the quote. Let it breathe.

#### Slide 43 — Two YouTube exhibits
- ◷ 2 min.
- Don't name the YouTubers. Name the *pattern*.
- ◇ If asked "is this common?", verbal cite (DO NOT put on slide):
  - `youtube.com/watch?v=saggDHHnmtQ&t=855s` — 600k subs, bypass permissions
  - `youtube.com/watch?v=3GAxd90fEE4&t=833s` — API keys in chat
  - `youtube.com/watch?v=UAMAAoSPu8o&t=399s` — bypass permissions

#### Slide 44 — Convenience is a security budget (pullquote)
- ◷ 30 sec. Repeat the line. **Load-bearing for the rest of the workshop.**

#### Slide 45 — Three safety nets preview
- ◷ 60 sec.
- "We built these in. Not 'please be careful' — structurally can't."

#### Slide 46 — Net 1 — Key-paste block
- ◷ 90 sec. Hook scans every prompt.

#### Slide 47 — Net 2 — Backup gate
- ◷ 90 sec. State machine: reveal → save → confirm → unlocked.

#### Slide 48 — Net 3 — Keys never in plaintext
- ◷ 90 sec. Fernet + OS keychain + decrypt-only-to-sign.

#### Slide 49 — Paper before live
- ◷ 30 sec. Stat slide. Read it.

---

### ▦ PART E — ARCHITECTURE + MANGROVE INTERNALS (01:10–01:35 · Slides 50–74)

> ◷ **25 slides in 25 min = 1 min/slide.** Many slides are reference; you're naming concepts, not teaching each one in depth.

> ! **Risk:** this section runs long if you stop on every Mangrove internals slide. Don't. Push through.

> § **TA1 + TA2:** glance at the room every minute. If glazed > engaged, signal me with [-] and I'll compress.

> ◇ **Cut targets if behind:** slides 64–67 (SDKs deep dive), 71–73 (building in public). The architecture diagram (51) is non-negotiable; the rest is.

#### Slides 50–55 — B&B Architecture (6 slides, ~7 min)
- 50: Act 02 divider · 30 sec
- 51: Where things run (architecture diagram) · 3 min ★ **The keystone slide.** Walk it carefully.
- 52: Trust boundary · 90 sec
- 53: mangrove-agent process · 90 sec
- 54: Mangrove side · 90 sec
- 55: Nothing touches Base mainnet (pullquote) · 30 sec

#### Slides 56–63 — Mangrove KB (8 slides, ~7 min)
- 56: Inside Mangrove title · 30 sec
- 57: Three layers, one bot · 60 sec
- 58: KB divider · 30 sec
- 59: What the KB is · 60 sec
- 60: 223 signals / 40+ indicators · 60 sec
- 61: Anatomy of a signal · 90 sec
- 62: Signals come with context · 90 sec
- 63: How the agent actually uses it · 2 min ★
- ● **Land:** "kb_search → signals.get → cite by name → RuleRegistry dispatch."

#### Slides 64–67 — SDKs (4 slides, ~4 min) ◇
- 64: SDKs divider · 30 sec
- 65: MangroveAI · 90 sec
- 66: MangroveMarkets · 90 sec
- 67: Architecture (how the pieces fit) · 60 sec

#### Slides 68–70 — Signal library in practice (3 slides, ~3 min)
- 68: Section divider · 30 sec
- 69: Composing a strategy · 90 sec
- 70: The loop (search → compose → backtest → iterate) · 60 sec

#### Slides 71–74 — Building in public (4 slides, ~4 min) ◇
- 71: Section divider · 30 sec
- 72: Open source is good for the community · 90 sec
- 73: Open primitives, hosted intelligence · 90 sec
- 74: Mangrove internals resources · 60 sec

---

### ◐ BREAK (01:35–01:45 · Slide 75)

- ◷ **HARD: 10 minutes. Set a timer.**
- Tell them the exact return time out loud.
- § **TA1:** active during break — find anyone behind on setup, sit with them.
- § **TA2:** restroom queue management, pour coffee, gentle herd back.
- § At 01:43, both TAs make eye contact with stragglers. At 01:45, doors close.

---

### ◇ PART F — AUTHOR A STRATEGY (01:45–02:05 · Slides 76–84)

> ★ **First hands-on segment.** Everyone types alongside you.

> § **TA2:** primary floor support. Walk constantly. **TA1:** field prompt syntax errors and Mangrove API errors.

#### Slide 76 — Act 03 divider
- ◷ 30 sec.

#### Slide 77 — What a strategy is
- ◷ 90 sec. Five parts: asset / timeframe / entry / exit / risk.

#### Slide 78 — Three authoring modes
- ◷ 2 min. Reference-first / manual / autonomous.
- ● "Reference-first is the default. Always."

#### Slide 79 — Ground truth, fast
- ◷ 60 sec.

#### Slide 80 — Your first prompt ★
- ◷ 60 sec. Display the prompt. **Tell the room: type this verbatim.**
- ▶ **Live:** `Build me a momentum strategy for ETH on 1h. Use a reference.`
- ▲ **Stop gate:** wait for the room to type it. Look up. Count keyboards.

#### Slide 81 — Reading the candidates
- ◷ 2 min. Sample output. "Pick ref-001."
- ▶ **Live:** `Let's try ref-001.`
- § **TAs watch screens** as the bot calls `build_strategy_from_reference` and `backtest_strategy`.

#### Slide 82 — Six metrics, six thresholds
- ◷ 2 min.

#### Slide 83 — What to do with each verdict
- ◷ 2 min.
- PASS → promote. MARGINAL → accept or iterate. FAIL → pick another reference.

#### Slide 84 — INSUFFICIENT_TRADES (pullquote)
- ◷ 30 sec. Callout.

> ▲ **STOP GATE — end of Part F (02:05)**
> - § TAs report: how many have a strategy committed?
> - If ≥70%: proceed.
> - If <70%: extend by 3 min, take it from Part J (Ch 8).

---

### ▤ PART G — PAPER MODE (02:05–02:17 · Slides 85–89)

> § **TA2:** stay floor-focused. **TA1:** start prepping for Part H — pre-check anyone who'll go live in Part I has their wallet ready.

#### Slide 85 — Act 04 divider
- ◷ 30 sec.

#### Slide 86 — Promote it to paper
- ◷ 2 min.
- ▶ **Live:** `Promote it to paper.`
- Show the cron registration in the response.

#### Slide 87 — Cron schedule by timeframe
- ◷ 90 sec.

#### Slide 88 — Force a tick ★
- ◷ 3 min. Don't make them wait an hour.
- ▶ **Live:** `Run evaluate_strategy on it so I don't have to wait for the cron.`
- Show the evaluation output.

#### Slide 89 — Evaluations vs trades
- ◷ 4 min.
- ▶ **Live:** `Show me my evaluations. Then show me my trades.`
- Walk both tables.

---

### ◼ PART H — WALLET SETUP / CHAPTER 06 (02:17–02:30 · Slides 90–93)

> ! **Real money territory begins here.** Reaffirm: this part is optional. Anyone not going live just watches.

> § **TA1:** sit with anyone going live. **TA2:** keep non-live attendees engaged on the concepts.

#### Slide 90 — CHAPTER 06 · Create or import
- ◷ 3 min.
- ▶ **Live:** `Create me a new wallet.`
- Show the `secret_id` output.

#### Slide 91 — CHAPTER 06 · Encryption Model
- ◷ 3 min. Fernet + OS keychain + decrypt-only-to-sign.

#### Slide 92 — CHAPTER 06 · Backup Gate (three steps)
- ◷ 4 min. The state machine.
- ▶ **Terminal (NOT in chat):**
  - `./scripts/reveal-secret.sh <secret_id>`
  - Save the key (password manager / paper / hardware wallet)
  - `./scripts/confirm-backup.sh <address>`
- ! Anyone trying to paste their key into chat — the hook will block it. **This is the moment to reinforce why the hook exists.**

#### Slide 93 — CHAPTER 06 · Verify it landed
- ◷ 3 min.
- ▶ **Live:** `List my wallets.` Then: `Check my portfolio.`
- Look for: `backup_confirmed_at: ✓` and `live_enabled: true`.

> ▲ **STOP GATE — end of Part H (02:30)**
> - Funded wallets only proceed to Part I. Unfunded watchers stay engaged via TA2.

---

### ▲ PART I — GOING LIVE / CHAPTER 07 (02:30–02:45 · Slides 94–98)

> ★ **The moment of the workshop.** A live swap on mainnet, in front of the room.

> § **TA1:** sit beside anyone going live. **TA2:** keep non-live attendees focused on the concepts.

#### Slide 94 — CHAPTER 07 · Going live (divider)
- ◷ 30 sec.

#### Slide 95 — CHAPTER 07 · Go/No-Go review
- ◷ 2 min. Four gates: paper-traded cleanly · wallet backup-confirmed · small allocation.

#### Slide 96 — CHAPTER 07 · Promotion
- ◷ 3 min. Paper → Live state transition.

#### Slide 97 — CHAPTER 07 · The on-chain flow
- ◷ 3 min. Quote+approve / prepare+sign / broadcast+log.

#### Slide 98 — CHAPTER 07 · First live swap ★
- ◷ 6 min. **Live demo on YOUR pre-funded wallet.**
- ▶ **Live:** `Promote strategy s-... to live. Allocate 5 USDC from w-... confirm=true`
- ▶ **Live:** `Run evaluate_strategy on s-...`
- Wait for the tx_hash. Project basescan.org/tx/<hash> on screen. **Let the block confirm live.**
- ● **Pause and let the moment land:** "That just happened on Base mainnet. Real swap, real tokens, your wallet."

---

### ◊ PART J — MONITOR & EXTEND / CHAPTER 08 (02:45–02:50 · Slides 99–100)

> ◷ **Compressed deliberately.** Reference material; full content lives in `tutorials/trading-app/08-...`

#### Slide 99 — CHAPTER 08 · Everything the bot did
- ◷ 2 min. Audit trail: list_evaluations, list_trades, raw SQLite access.

#### Slide 100 — CHAPTER 08 · Make it yours
- ◷ 3 min. Custom signals, MCP tools, hooks. Pointer to the tutorial chapter for detail.
- ● "This is the end of the workshop and the start of your project. Tell me what you build."

---

### ▶ PART K — WRAP (02:50–03:00 · Slides 101–104)

#### Slide 101 — Act 05 divider
- ◷ 30 sec. Pivot to generalizable principles.

#### Slide 102 — Four principles for agents that touch real things
- ◷ 4 min. Read each principle aloud. Self-custody · audit trail · guardrails you can't skip · staged promotion.
- ● **Tie back:** "Every guardrail you saw today was a worked example of one of these."

#### Slide 103 — Resources
- ◷ 2 min.
- Setup guide / Survival kit / mangrove-agent repo / mangrovedeveloper.ai / Claude Code docs / Tim's email.
- "Take a picture of this slide."

#### Slide 104 — Thank you / Q&A
- ◷ 3 min.
- Take questions. Prioritize ones that benefit the whole room.
- Individual debugging: "stick around, I'll help after."

---

## ‼ CONTINGENCY PLANS

### If Part B runs long (install crisis at 00:25)
- **Take 5 min from Part E.** Cut slides 64–67 (SDKs detail) and 71–73 (building in public).
- **Take 3 more min from Part J.** Skip slide 99 entirely.

### If WiFi dies mid-workshop
- Most of Parts F–I require Mangrove API. Not fakeable.
- Pivot to discussion mode for the affected segment.
- Show your pre-recorded terminal session if available.
- Use the time for deeper Q&A on the slide content.

### If nobody's bot works at 00:50
- Designate one working laptop as "class bot" — project it.
- Skip live coding in Parts F–G. Demo on your machine.
- Make Parts H–I discussion-only. Concepts still land.

### If Part F runs long (00:25 over)
- **Cut Part E from 25 → 20 min.** Drop slides 64–67 and 71–73.
- **Cut Part J entirely.** Skip to Part K.

### If someone pastes a private key despite the hook
- Their key is now compromised. Have them rotate **immediately**.
- Clear their transcript: `rm -rf ~/.claude/projects/<hash>/`
- ● Use it as a teaching moment: "The hook should have caught this. Why didn't it?" Inspect together.

### If a live trade fails
- Most common: insufficient gas on the wallet.
- Second most common: slippage cap rejected (illiquid pair).
- Use it as a teaching moment. Show `trades.error_msg`. Don't panic.

---

## • POST-WORKSHOP CHECKLIST

- [ ] Collect feedback (QR to form)
- [ ] Email all attendees: thank-you + repo link + chapters 6-8 if they didn't finish
- [ ] Ask the two who got stuck what specifically broke — fix it for next time
- [ ] Note which slides ran long / short for v6 of the deck
- [ ] Note which demo prompts worked / fizzled
- [ ] Debrief with TAs: what should each of you have done differently?

---

## • APPENDIX A — DEMO PROMPTS IN ORDER

```
# Part B, after install (00:25)
[no prompts — slides only until Part F]

# Part F, author a strategy (01:50)
Build me a momentum strategy for ETH on 1h. Use a reference.
Let's try ref-001.

# Part G, paper mode (02:05)
Promote it to paper.
Run evaluate_strategy on it so I don't have to wait for the cron.
Show me my evaluations.
Show me my trades.

# Part H, wallet setup (02:17 — in the bot chat)
Create me a new wallet.

# Part H, in your terminal (NOT in chat)
./scripts/reveal-secret.sh <secret_id>
./scripts/confirm-backup.sh <address>

# Part H, back in the bot
List my wallets.
Check my portfolio.

# Part I, going live (02:34)
Promote strategy s-<id> to live. Allocate 5 USDC from w-<id>. confirm=true
Run evaluate_strategy on s-<id>.
# After: open basescan.org/tx/<hash> in a browser
```

---

## • APPENDIX B — YOUTUBE EXHIBITS (verbal reference only — NOT on slides)

1. `youtube.com/watch?v=saggDHHnmtQ&t=855s` — 600k subs, bypass permissions at 14:15
2. `youtube.com/watch?v=3GAxd90fEE4&t=833s` — API keys in chat / .env at 13:45
3. `youtube.com/watch?v=UAMAAoSPu8o&t=399s` — bypass permissions at 6:30

**The pattern:** influencer content optimizes for "no friction." Permission prompts and secret-management habits feel like friction. The content deletes them. Your job is to name the deletion — not the people.

---

## • APPENDIX C — TIME RESERVES & WHERE TO STEAL

| If behind, take time from | Cost | Damage |
|---|---|---|
| Part E slides 64–67 (SDKs detail) | +4 min | Low — Mangrove internals reference |
| Part E slides 71–73 (building in public) | +4 min | Low — OSS philosophy |
| Part J entirely | +5 min | Medium — skip Ch 8 reference |
| Slide 37 war stories (cap to 3 min) | +2 min | Low — story budget recovery |
| Slide 102 four principles (read faster) | +2 min | Medium — wrap message |

**Total reserves:** ~17 min available. Don't burn them all in Part B.

---

*End of run-of-show. Break a leg.*
