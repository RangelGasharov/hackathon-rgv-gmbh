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

## Not yet done

- The case files in `cases/practice` are built from the data, not the arena's real files: pull the real ones with `arena cases` once the team key exists.
- Supplier twin is our own model of a supplier; hosted suppliers will differ. Calibrate on practice replays.
- No replay-mining loop yet; no Sunday-release handling beyond what `DataPackage` merges automatically.
