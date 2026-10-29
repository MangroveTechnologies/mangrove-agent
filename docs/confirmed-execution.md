# Confirmed strategy execution

`strategy_service.tick` evaluates externally executed LIVE strategies by object,
with `persist=False`, regardless of their paper evaluation lane. The local
journal supplies actual cash, remaining physical positions, immutable opening
capital and one net close event per fully closed engine position. Default
engine-owned paper evaluation continues to use `persist=True`.

Explicit stateless PAPER evaluation also uses caller-owned evaluation. Its
separate paper journal records the paper executor's simulated fill price and
amounts atomically with the position, prospective bracket template, order dedup
key and book revision. The next tick supplies that actual simulated book and
one net close per fully closed paper position. An engine exit estimate cannot
advance paper risk until the simulated fill is recorded and delivered. Paper
fills have no venue fees; their net PnL uses actual simulated entry and exit
prices. Historical paper fills without complete journal evidence hold entries
while recorded protective templates remain available.

Paper opening capital is immutable after adoption. Paper risk, receipts and
cash remain separate from LIVE history. A closed LIVE position does not poison
the paper journal; open LIVE exposure cannot become a paper position. Stateless
paper currently supports v1/v2 long positions and explicitly refuses v3 core
allocations. A paper book must settle flat before LIVE promotion. Human paper
halts retain protective exit monitors until flat, without releasing LIVE
allocations. Status changes and ticks share the same per-strategy lock, so a
paper fill cannot race a promotion or monitor retirement.

`proposed_positions` are prospective bracket templates. An entry creates no
local position until actual fills exist. Engine account balances and modeled
closes never replace local settlement facts. Only returned risk and evaluation
clock fields are committed, with execution/book revision checks and a per-strategy
tick lock. Close delivery uses booked timestamps and receipt identities; unseen
cursor ties or late history trigger a complete replay for engine deduplication.

The Robinhood journal applies cumulative fill deltas once. Partial exits retain
the remainder and a final close aggregates every actual exit's net PnL, including
entry and exit fees. Configured capital cannot be changed after execution history.
Pending orders fence their position; unidentified legacy orders fence the whole
strategy. Only one entry may be pending per strategy. Distinct position exits
submit serially, and exits defer entries until the next confirmed evaluation.
An exit targeting a pending entry cancels that entry, reconciles and waits for the
next tick's fresh quantity.

The DEX journal reserves intent identities before execution. Configured receipt
RPC, chain identity, finalized reads, token metadata, quote allocation, wallet
backup and zero native transaction value are checked before signing. Locally
computed signed transaction hashes are persisted before each broadcast; a lost
response retains a recoverable hash and is never automatically rebroadcast.
Known failures before any broadcast are terminal. Approval receipts and reverted
swaps retain their own actual native fee evidence.

DEX holdings come only from canonical finalized receipts proving net ERC-20
wallet transfers for the signed transaction, wallet, chain and router. Quotes,
status labels, pending transactions and unsigned templates cannot create
positions or realized PnL. Native or internal asset flows require a trace adapter
and are refused by this ERC-20 path.

Native fees remain in their native asset. No native-to-quote valuation adapter
exists here: any nonzero or incomplete native fee, including approval fees,
makes financial history incomplete. The physical book and protective exits
remain available, but new entries and close-history acknowledgements are held.
There is no guessed USD fee. New explicit allocations establish an opening quote
capital event; historical LIVE books without that evidence remain on entry hold.
Paper audit trades do not establish or contaminate the live financial book.

The portfolio latch remains human-reset only. Pausing or archiving a strategy
with confirmed exposure or unresolved orders retains its allocation and exit
monitor. Protective exits continue while new entries are refused. The allocation
and monitor retire only after confirmed flatness; paper execution requires the
live exit monitor to finish first.
