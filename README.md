# Hollerau buyer agent

An AI buyer for the Tacto Negotiation Arena. Code decides every number; Claude only words the e-mail.

```
case file + data package
   │
   ▼
dossier.py   persona traits from notes, letters, quote remarks, history, news (strong vs weak),
             should-cost and margin, notice check, hedges, scorecard, payment record, forecast
   │
   ▼
strategy.py  every combination of term_options priced: lowest price the supplier signs (its cost of
             money, minimum margin) and the price that is cheapest for us (relationship charge);
             equal-cost packages (MESO); a concession path that is always a visible move;
             never above the supplier's own ask, never past our walk-away cost
   │
   ▼
claims.py    data arguments the arena checks, figure next to ID, verified against facts.json,
             chosen per persona (no index claims to a hedged supplier, never one that supports a rise)
   │
   ▼
writer.py    template (free, deterministic) or Claude Opus 5.5 at low effort, with a guard:
             claim sentences kept verbatim, unplanned IDs removed, no leaks, prices must match
   │
   ▼
agent.py     per thread: read reply (quote PDF standard terms, cost breakdown), update the model,
             accept (AC_next, never the first offer), counter, match its level on our terms,
             safe match after a final offer, or walk only if the alternative is clearly cheaper
runner.py    one worker per supplier thread (no thread idles past 60 s), Live Final waves
```

## Run

```bash
cd starter-kit
uv sync --all-extras
cp .env.example .env          # ARENA_URL, ARENA_API_KEY, ANTHROPIC_API_KEY, BUYER_BUDGET_USD
uv run python -m buyer.cases  # local stand-ins for the 19 practice case files (cases/practice)
```

Local benchmark (no arena calls):

```bash
uv run python -m buyer.bench --sim kit  --agents naive,buyer          # the kit's scripted supplier
uv run python -m buyer.bench --sim twin --agents naive,buyer          # persona-driven supplier twin
uv run python -m buyer.bench --sim twin --agents buyer-claude --cases P01,P05   # with the Opus writer (costs money)
```

Against the arena (practice quota counts):

```bash
uv run python -m buyer.runner play case:P01 --writer claude
uv run python -m buyer.runner run --set practice --writer claude -p 8
uv run python -m buyer.runner run --set final --writer claude          # Live Final: start before 13:30
```

LLM spend is tracked in `runs/spend.json`; calls stop at `BUYER_BUDGET_USD` and the template writer takes over.

## Local results (24 Oct, v1)

Mean score × 100 over the 19 local practice cases. Nothing was sent to the hosted arena.

| Opponent | naive (kit) | kit LLM agent (Opus 5.5) | this agent |
|---|---|---|---|
| supplier twin, seeds 0 / 1 / 2 / 3 | 15.7 / 14.5 / 16.6 / 14.5 | - | 81.7 / 80.6 / 77.9 / 74.4 |
| twin, 7 cases, 60-second clock, Opus e-mails | - | 21.9 | 69.8 |
| kit's scripted supplier (`arena-sim`) | 65.1 | - | 65.1 |
| Live Final rehearsal, 2 waves × 6 cases, 240 s cap | - | - | 67.6, every case closed in time |

The kit's scripted supplier ignores the relationship charge and concedes 2 % a message to a fixed floor,
so the agent's margin-protecting moves earn nothing there. The twin models the published cost function
with persona-driven suppliers; it is our own model, so treat its numbers as relative, not absolute.
LLM spend for this build: about $1.13 (63 calls).

## First hosted-arena session (24 Oct, team RGV GmbH)

One practice session, `case:P01` (Brandlhuber +9.5 %), Opus writer: **score 0.78**, deal at €133.80 from
1 January 2027 with 14-day payment (asked: €138.55 from 1 November 2026). C_open €1,153,780,
C_deal €1,109,365, C_best €1,096,816. 3 messages, 94 s, $0.04. Replay: https://arena.tacto.ai/replay/2f7132cb8b201e08

What the replay taught, now in the code:
- The notice-period claim and the forecast claim moved the supplier ("we did not know that - a fair point").
- Citing a driver that rose, even far less than the ask, backfired ("your figures confirm our case"):
  only falling drivers are cited now.
- The supplier answered with two alternative term sheets; the agent now reads all of them and takes the
  cheapest for us.

## Hosted practice, v3 (24 Oct, 17:38, one session per case)

Agent v3 (commit 0e295d2), Opus 5.5 writer, all 10 hosted cases in parallel: **10 deals, mean 0.655**.
The first sessions (v1) averaged 0.428, and the best per case before this run averaged 0.547.
`buyer/twin_real.py` had predicted 0.669. LLM spend for the run: $0.45 (32 calls).

| case | v3 score | twin prediction | best before | deal |
|---|---|---|---|---|
| P01 price increase | **0.875** | 0.855 | 0.78 | €132.70, 14 days |
| P02 price increase | **0.723** | 0.555 | 0.26 | €362.81, agreed in round 1 |
| P03 renewal | **0.923** | 0.993 | 0.90 | €432.15, 36 months |
| P04 renewal | 0.860 | 1.000 | 0.86 | €4.94, final offer |
| P05 RFQ (3 bidders) | **0.451** | 0.441 | 0.34 | €31.51 from 70210, our first offer accepted |
| P07 bundling (kit) | 0.624 | 0.728 | 0.70 | €5.60, final offer |
| P08 raw material | **0.473** | 0.430 | 0.26 | €5.54 |
| P10 capacity | **0.694** | 0.671 | 0.59 | €31.30, final offer |
| P11 quality | **0.536** | 0.536 | 0.44 | €46.00 |
| P18 raw material | **0.395** | 0.480 | 0.34 | €5.61 |

Best score per case on the practice board is now 0.663 on average.

What these sessions taught:
- **Suppliers count each kind of argument once.** A second forecast (another kit part) or a second
  scorecard row (ppm after on-time delivery) came back `duplicate` with "we have already taken that
  volume change data into account". This happened even with new IDs and even within one message. One
  argument of each kind per supplier is enough. Further slots should go to a different kind.
- **An offer accepted at once was too generous.** P05-70210 accepted our first RFQ offer (12 % below its
  ask), and P02 and P18 agreed in one step too. In RFQs the anchor can go deeper.
- **Effective date given up.** P01 opened with "valid from 1 April 2027" (the notice argument), and the
  supplier accepted it with 1 January 2027 as an alternative. In round 2 we fell back to 1 November 2026.
- **The writer answers questions nobody asked.** "To your question on our figures" appeared when the
  supplier only wrote "these figures" in its opening. Only a real request for data should trigger it.

## Not yet done

- Supplier twin: refit with these 10 sessions (`uv run python -m buyer.twin_real calibrate --write`).
- Dedupe arguments by kind per supplier; deeper RFQ anchor; keep a won effective date; fix the "figures" trigger.
- Relationship-goal cases with only a generic cost estimate (e.g. P06): the agent can squeeze below the
  supplier's full cost, which the relationship charge punishes. Not hosted in practice, so not checked live.
- No Sunday-release handling beyond what `DataPackage` merges automatically.
