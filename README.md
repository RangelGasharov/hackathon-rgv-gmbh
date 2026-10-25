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
uv run python -m buyer.bench --sim real --agents buyer --seeds 0,1,2  # calibrated twin, 19 cases, hosted and other apart
uv run python -m buyer.bench --sim real --agents buyer --seeds 0,1,2 --against runs/bench-real-<before>.json  # keep or reject
uv run python -m buyer.argtable build   # argument results per kind and persona from runs/arena-*.json
uv run python -m buyer.argtable show
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

## v4 fixes (local only, no arena runs)

The twin was refit on all 23 real sessions and now treats a second argument of the same kind as
`duplicate`, as the arena does. On the refit twin, v3 scored 0.698 on the 10 hosted cases and v4 scores
**0.715**. The 9 other cases, with generic suppliers, went from 0.562 to 0.583.

- One argument per kind per supplier (`claims.kind_key`); further slots go to a different kind.
- RFQ opening 18 % below the ask (was 12 %), at most 10 % for a capacity-tight bidder. P05: 0.451 → 0.580.
- Price letters keep a later effective date once the supplier has offered it in every sheet (P01 keeps 1 April 2027).
- "To your question on our figures" only when the supplier asks for data, and never in the first message.
- When a supplier's sheets reveal it wants an index clause, its earlier asks and our last offer are valued
  again, so the limit estimate does not mix two valuations.
- Our guessed value of a long contract and of an index clause is halved, closer to the twin's fitted model (P08 0.40 → 0.45).

### Review round (safety for the final, data still dropped)

Safety:
- **Error fallback:** after an internal error the agent no longer signs the supplier's standing sheet.
  While time and rounds remain it sends one short holding note per supplier reply. Near the cap it takes
  the best live sheet across all bidders, if that beats the alternative.
- **No walking away without an alternative:** with no priced sheet and no alternative, the agent keeps the
  threads open, since a walk-away would score 0.
- **Waiting threads:** an RFQ thread parked behind a final offer gets a holding note whenever its last reply
  is 40 s old (rounds permitting), instead of one note and then silence.

Arguments, from the 23 real sessions:
- **Delivery record:** "does not change our calculation" 6 of 6 times; only a supplier on an old cost sheet
  said "a fair point". The on-time-delivery argument now goes only to those suppliers, and the twin was
  corrected and refit (replay loss 0.0100 → 0.0096).
- **Should-cost:** counted with the capacity-tight P10 supplier in both sessions, so it stays.
- **Duplicates:** a `duplicate` claim result retires that kind of argument for the supplier.

Openings and terms:
- Openings go 5 points deeper for easy-to-move suppliers (spare capacity, old cost sheet, reference
  customer, weak cash squeeze). Price letters with a stabilise goal open up to 5 % below (was 2.5 %).
- A shorter lead time the supplier granted in every sheet stays ours, like the effective date.

Inputs:
- The supplier's 8D report sets a floor on the ppm commitment we ask for ("Commitment: 1500 ppm").
- A delivery schedule fills in a missing standard lead time.
- Each attachment is downloaded once per session. Saved records now keep attachment texts; before, they
  held only empty `content_b64`.
- The Sunday release is read in every supplier lookup (`World.rows`): hedge note, news, own letter, news traits.
- In single-supplier cases the case briefing feeds the supplier profile (no trait changes on today's 19 cases).

Twin A/B on the refit twin, before → after:
- The 10 hosted cases: 0.679 → 0.679.
- The 9 other cases: 0.553 → 0.573.

Tried and dropped:
- **Forced €150 chargeback** (the company standard): it raised C, because the supplier pays it at its real
  defect rate while it is worth less to us at the committed rate (P12 0.351 → 0.325).
- **Raw-material opening at 14 %:** P08 fell from 0.451 to 0.386.

Not taken:
- A sheet within 2 % of the ask for committee suppliers: our pacing to the supplier's limit already lands
  below that.
- Never offering above today's price in quality cases: price is the only move a quality supplier values,
  so holding it would stall the talks.
- A profile cache: building a supplier profile takes 0.07 s.
- An LLM e-mail cache: prompts carry the supplier's e-mail, so they rarely repeat.
- `validity_days` and `reply_delay_days`: they count simulated days, and no offer expired within a session.

## Evening push (24 Oct): what the suppliers really do, and v5

Evidence: 47 real sessions, including each supplier's back-office notes and the arena trace with its
reasons. Two Opus analyses of them are in the session scratchpad: a mechanism report and an ideas report.

Practice sessions this evening:

| Batch | Cases | Mean | Notes |
|---|---|---|---|
| b1 | all 10 | 0.624 | P05 **0.814** (rival two-step), P01 0.903; P02 0.462 and P08 0.000 from bugs |
| b2 | all 10 | 0.593 | P01 **0.928**, P07 **0.721**; P05 0.000 (accept loop plus timeouts) |
| b3 | P02, P05, P08, P18 | 0.541 | quotes outside the case are "irrelevant"; P02 copper clause agreed at once |
| b4 | P02, P05 | 0.740 | P05 0.814 (waited for the second step) |

The best score per case averages **0.711**: P01 .928, P02 .723, P03 .923, P04 .860, P05 .814, P07 .721,
P08 .473, P10 .694, P11 .536, P18 .441.

How the hosted suppliers behave:
- **Round-1 counter:** a fixed step whatever our anchor. An offer above it is agreed at once, so we
  overpaid.
- **After round 1:** about one price-grid step (about 1 %) per round on a visible move. The final offer
  comes at round 3-4, at most 2.6 % of the opening below the round-1 counter. A final offer is final:
  any change to it, or a step back in price, ends in a walk.
- **Rival quotes:** a rival price far below its own triggers the only escalation we have seen ("I give
  half the step now and check the rest", then "give the rest of the step"). P05 went 36 → 30.5 → 28.0.
  Quotes outside the case's refs do not count ("does not concern this part or our company").
- **Arguments:** each kind counts once per supplier. "Does not change our calculation" means no effect;
  the delivery record counts only with suppliers on old cost sheets.
- **Committees:** asking for approval does nothing (about 40 tries).
- **Timeouts:** the arena logs supplier answers to late moves in `timeouts`, outside `messages`. Missing
  them cost the b2 P05 deal.

v5 changes:
- **Opus strategist** (`StrategistWriter`): code builds a menu of offers that cost us the same, checked
  arguments and points. Opus reads the supplier's documents and the thread, picks from the menu, notes
  contradictions, and writes the e-mail. `guard()` still checks every price and ID.
- **Runner:**
  - supplier answers from `timeouts` are merged into the thread;
  - a holding note goes out after 45 s without a reply;
  - a refused accept is retried at most twice, then we offer its own terms back.
  - Stress test on the twin (refused accepts, late errors, 10 cases in parallel): 10 of 10 deals.
- **Pacing:**
  - the limit estimate is min(75 % rule, round-1 counter − 2 % of the opening);
  - jumps are capped at 1.5 % a round (1 % in RFQs and with committee suppliers);
  - never a lower price for the same terms;
  - our last offer is re-valued when the model changes;
  - after "come back to you on price", only a minimal move.
- **RFQ:** cite the cheapest rival offer to every bidder. While a bidder's second step is pending, do
  not sign another bidder.
- **Relationship charge** counted in full where our calibrated cost estimate puts the supplier near its
  sustainable margin. The no-step-back and repricing logic now handles C falling and then rising in price.
- **Twin:** refit on all sessions. It now models duplicates per kind, the delivery-record rule, the rival
  two-step, out-of-case quotes being irrelevant, and the step-back walk.

Ceiling: both analyses put the realistic mean on these 10 cases at about 0.73-0.75 under the behaviour
seen so far. More needs a trigger the suppliers have not shown yet.

## P18 fix (Blankstahl, raw material)

In 7 real sessions Blankstahl answered on a €0.10 grid (5.8 / 5.7 / 5.6 / 5.5) and never went below €5.50 at
90 days, 36 months, AS-14404 clause with 5 % deadband. The only session that reached €5.50 (0.467) had a
+1.9 % forecast argument next to the index one ("we took it into account"). The agent dropped forecast rises
of 3 % or less, so v5 sent only the two index arguments.

- Any forecast rise is now an argument. +1.9 % counted 4 of 4 times (P08 ×3, P18). It stays a weak argument
  for a supplier that calls us "not strategic" (P04: "does not change our calculation", 4 of 4). It counts
  for one "reviewing small accounts, focus on customers with volume" (P18).
- Twin: Blankstahl's grid is €0.10, read from its own counters only; agreements that echo our price no longer
  count. The P18 replay loss went from 0.0126 to 0.0095, and the twin now reproduces the €5.50 session
  round by round.
- On the twin, same suppliers, old vs new arguments: P18 0.441 → 0.467 (€5.54 → €5.50), P08 0.473 → 0.505.
  The other 8 hosted cases send the same arguments as before and score the same.

Hosted, after the fix (24 Oct, evening):

| session | writer | arguments sent | deal | score |
|---|---|---|---|---|
| 4d027315f6d5656f | template (no API key) | AS-14404, PPI-C2431, forecast | €5.45, 90 days, 36 months | **0.500** |
| 647f738579e7e8f9 | Opus strategist | AS-14404, PPI-C2431 (forecast dropped) | €5.50 final offer | 0.467 |

- The strategist may choose the arguments, but the code's own picks now fill any slots it leaves free
  (one per kind, at most 3), so a counted argument can no longer be dropped. Picks the code rates below 0.3
  stay the strategist's call (P04's forecast, 0.2).
- Run records are saved as UTF-8. On Windows they were written as cp1252, which the replay loader could
  not read.

With the forecast sent, one real session reached €5.45. More needs a mechanism this supplier has not shown.

### More arguments for P08 and P18

Across the 9 real P18 sessions, each counted argument took about €0.03 (0.5 % of the opening) off where
Blankstahl stopped. With none it ended at €5.50 at 60 days. With two it refused €5.45 and €5.48. With three it
agreed €5.45. Arguments also count after round 1: P01 counted a fourth in round 2. Each index series counts
once. Both suppliers name the same three drivers in their opening: AS-14404 45 %, PPI-C2431 38 %, LCI-C 5 %.

- Raw-material cases (P08, P16, P18) cite a fall in a window that ends within the last 12 months, when nothing
  fell in one ending now. The arena accepts such windows (P07: one ending 5 months back came back `verified`).
  For P18 this adds LCI-C −0.3 % (2025-10 to 2026-03), sent in round 2 after the three current arguments.
  The other cases are unchanged.
- A hedge mutes only the series its note names. Donautal (P08) covered "nickel and alloys", so AS-14404 is
  "not our reality" for it, but PPI-C2431 and LCI-C go out with the forecast. Untested on the arena; the one
  answer it gave so far ("not our reality") cost nothing.
- An index or forecast argument the supplier takes into account after its first counter lowers the agent's
  limit estimate by 0.5 % of the opening. "Not our reality" and "does not change our calculation" do not.
- Twin: at most 3 counted arguments per message, not per thread (the P01 replay loss fell from 0.0053 to
  0.0045). A hedge covers only the series its note names. The overall replay loss is unchanged (0.0166).

On the twin, previous vs new agent against the same suppliers: P18 0.467 → 0.509 (€5.50 → €5.40 at 60 days),
P08 0.505 → 0.570 (€5.51 → €5.45). The other 8 hosted cases are identical. The P08 gain depends on the
untested hedge assumption.

Hosted, Opus strategist (24 Oct, 22:34):

| case | session | arguments sent | supplier's answer | deal | score |
|---|---|---|---|---|---|
| P08 | 9b1e05d79107a3c5 | PPI-C2431, LCI-C, forecast (round 1) | all verified, "took it into account"; only AS-14404 is hedged | €5.48, 90 days, 36 months | **0.538** (best was 0.473) |
| P18 | 6155cdbec958ab7c | AS-14404, PPI-C2431, LCI-C (round 1); no forecast | all verified, "adjusted our calculation" | €5.40, 45 days, 36 months | 0.497 (best 0.500) |

- P08 confirms the hedge reading: Donautal "acknowledged the slight decreases in PPI-C2431 and LCI-C" and
  said only its AS-14404 material is covered.
- P18 never sent the forecast. Opus's persona read called Blankstahl's "reviewing small accounts" a strong
  small account, so the forecast fell to 0.2, too weak to send. Only "not strategic" (P04) now makes a forecast
  weak, whatever the read says, so P18 sends AS-14404, PPI-C2431 and the forecast in round 1 and LCI-C in round 2.

### Raw material: one argument a round, a steady gap

The practice board showed P18 at 0.688 (best on the board) and P08 at 0.769 for other teams, and our team's own
sessions (not in `runs/`, read with `GET /v1/sessions` and `/v1/sessions/<id>`) at 0.532 and 0.581. What
those team sessions did differently, and what the supplier answered:

- **First counter is a fixed step.** P18 gave €5.60 at 90 days with one argument or with three. Arguments sent in
  round 1 beyond the first are spent for little.
- **Each later argument took one more step.** Team P18: €5.60, then €5.50 after the forecast in round 2, €5.40 after
  PPI-C2431 in round 3, final €5.40.
- **Low opening, small steps.** Openings at €5.05–5.20, then about 1 % a round, kept both suppliers conceding. Our
  agent opened at €5.36, closed in by round 3 and got its final offer or signed at its own price there.
- **Price history counts as an argument.** "We pay you EUR 5.45 per unit today (po:…)" came back "we took it into
  account" (team P08).

Changes, raw-material cases only (P08, P16, P18):

- One new argument a message, strongest first, by code and strategist alike; later rounds take arguments down to
  weight 0.3.
- Opening 14 % below its ask in our value model (was 10 %, and the committee cap no longer applies here). That
  lands at €5.11–5.13.
- Limit estimate: its first counter minus 3.5 % of the opening (was 2 %), minus 0.5 % for each argument counted
  later.
- Price history (weight 0.6) while what we pay today is below its standing price: P08 at €5.45. For P18 (€5.84) it
  never goes out.

The twin cannot judge this pacing: its limits were fitted on our old, faster sessions, and it does not model a
later argument buying a step. It now rates P18 at 0.473, against 0.539 and 0.598 on the arena. The other 8
hosted cases are unchanged on it.

Hosted, Opus strategist (24 Oct, 23:05):

| case | session | arguments, one a round | supplier's asks | deal | score |
|---|---|---|---|---|---|
| P18 | 19babd2964dbae78 | AS-14404, PPI-C2431, forecast, LCI-C | 5.60, 5.50, 5.40, final 5.30 | €5.30, 90 days | **0.598** (team best was 0.532) |
| P18 | c61076dd334baf17 | the same | 5.70, 5.60, 5.50, final 5.40 (or 5.30 at 14 days) | €5.30, 14 days | 0.539 |
| P08 | 544cfcd8ec19d82e | forecast, price history, PPI-C2431, LCI-C | 5.62, 5.59, 5.54, 5.50, final 5.45 | €5.45, 90 days | 0.570 (team best 0.581) |

Price history came back "does not change our calculation" in this P08 session; PPI-C2431 and LCI-C counted.

### Raw material: payment terms from the supplier's own cost of money

P08 `60a9bbb3a082a042` scored 0.523 at €5.44, a cent below the 0.570 session's €5.45. Its final came on our
60-day sheet, the agent signed its 45-day alternative, and 45 days earlier than 90 cost us about €10,000. Donautal's
money costs about 7 %, ours 8 %, so at the same value to it the longest term is always our cheapest package.

- Each two-sheet answer that differs only in price and payment days gives its cost of money: P08 6.8–7.7 %, P18
  8.6–10.7 % (the 0.10 grid inflates it). Same-price pairs on the grid are no reading.
- Below our rate, once its first counter is in: 90-day sheets only. Above: shorter sheets stay, as in P18's best
  deals.
- The rate decides only that. Priced at P18's grid-inflated 10.7 %, the agent dropped every 90-day sheet on the
  twin, and P18's best deal (0.598) was at 90 days.

Hosted, Opus strategist (24 Oct, 23:15 and 23:28):

| case | session | deal | score |
|---|---|---|---|
| P08 | 60a9bbb3a082a042 (before) | €5.44, 45 days | 0.523 |
| P08 | 42cbc95a790fb3f1 (after) | €5.48, 90 days (all sheets 90 days from round 2) | 0.538 |
| P18 | 25e718bc6491c6d0 | €5.30, 14 days | 0.539 |
| P18 | d7307833d4495c89 | €5.40, 90 days (LCI-C bought no step this time) | 0.532 |

Session to session, the supplier's first counter moves by a grid step (P08 €5.62–5.69, P18 €5.60–5.70), and the
score moves with it: P08 0.523–0.570 and P18 0.532–0.598 over the sessions with the new pacing.

## 25 Oct review: robustness, early arguments, money, clauses, leapfrog

Eight changes from a review of the replays. Each behaviour change has a switch (`True` = new), so it can be
A/B-tested on the twin. They were built in a cloud session that could reach neither the arena nor the data package:
**none of them has been played on the twin or the arena yet.** The 27 new tests (`tests/test_review.py`) use a
stand-in dossier and need neither. The behaviour tests fail when their change is switched off or reverted. The 39
tests that ran without the data package before still pass.

1. **Error paths: no signing, walking or going silent on an error** (`runner.py`, `agent.py`). An error-free batch
   on all 10 hosted cases is still to do before the final. Found and fixed:
   - The case ended when one message hit a 5xx or 429 that outlasted the client's 3 retries. All threads went
     silent, so no deal. Now: re-read the thread; if the message arrived, carry on; otherwise send it again
     (`RESENDS = 3`). Only 401, 403 and 404 end the case.
   - A dropped connection after the client's retries, an unreadable attachment, or an error while writing,
     composing a holding note or re-offering killed that worker. Its supplier then went silent with `in_flight`
     stuck, which also held up the other bidders. Now each of these has a fallback, and a worker never stops while
     its case is open.
   - After an internal error the agent sent one holding note, then went silent if the error repeated, because it
     counted replies with the state that had failed. Now it counts replies from the thread itself. A repeated error
     sends a small safe move instead: our last sheet 1 % higher, never above the supplier's ask. A second hold
     would only draw a final offer.
   - Near the cap, the error fallback compared sheets with the published formula only, not with model state that
     might be broken.
   - A replay of the old runner against the same flaky table: one 502 → no deal; a dropped connection → crash.
     The new runner closes the deal both times.
2. **Front-loaded arguments and a table of results** (`argtable.py`, `argtable.json`, `claims.py`). Message 1 carries
   the three strongest arguments, at most one of each kind. The strategist can no longer drop a new rival offer.
   Raw material keeps one argument a round (`agent.RAW_ONE_A_ROUND`): on these suppliers, three arguments in
   round 1 scored 0.497 / 0.538, against 0.598 / 0.570 for one a round. The hand-written "counted / did not count"
   weights are now a table per argument kind and supplier persona (hedge-covered, hedged-other, cash-short,
   stale costing, capacity-tight, not strategic, ...). Each weight is
   `(2 × old rule + what each logged argument did) / (2 + number logged)`. The table is seeded with the counts this
   README states (delivery record 0 of 6, forecast 5 of 5, not-strategic forecast 0 of 4, P08's unhedged indices
   4 of 4, ...). **Run `python -m buyer.argtable build` where `runs/` and the data package are** to tally every saved
   session. What the seed changes: P04's forecast is no longer a candidate, and P08 sends PPI-C2431 and LCI-C before
   its price history.
3. **Cost-of-money gap** (`strategy.MONEY_SHEET`). The equal-cost sheets always include 14 days for a supplier whose
   money costs more than our 8 %, and 90 days for one whose money costs less. Raw material uses the rate read off
   the supplier's own two-sheet answers. On the stand-in, the sheets already held 14 days for cash-short suppliers.
   The change mattered at 7 %, where 90 days was missing.
4. **Clause as currency** (`strategy.CLAUSE_CHEAP`). Raw-material first messages already carried the cheapest clause
   with 36 months. 36 months costs us the same as 24 over the 2-year horizon, and the supplier values it more. Now
   the e-mail also says it: "We accept a price adjustment clause on AS-14404 together with a 36-month agreement:
   it covers your material risk, so we expect a lower base price in return." An optional clause is offered
   unprompted only if it costs us less than the ~1.5 % suppliers value it at. P02 (0.8 %) keeps its trial. In P14
   the cheapest copper clause costs 2.2 % (55 % share × 4 % index risk), so it goes on our sheets only once the
   supplier's sheets carry it or it calls it mandatory. By the published formula, P08's deadband-3 clause costs 1.8 %
   (path 6 %, deadband 3 %); the agent's deadband-5 choice costs 0.9 %.
5. **`_pref_clause`** ranks by the full clause cost, including `material_share × index_risk_pct`. With the path
   inside the deadband, a 45 % share now beats an 80 % share whichever order the case lists them in. None of the 19
   cases changes its pick.
6. **RFQ leapfrog** (`agent.LEAPFROG`). Before signing a bidder, whether on an agreement, a final offer or AC_next,
   every other bidder still `open` with two rounds left must have seen that offer or a lower one. Each new, lower
   offer is a new ID. Bounded by rounds and by the wave's cap. Agreed and final-offer threads are never reopened.
7. **Sustainable-margin stop** (`strategy.SUSTAINABLE_STOP`). In relationship cases with a rough cost estimate (no
   should-cost of the supplier's own plant and no breakdown from it, as in P06), no offer goes below the price where
   our median belief of the supplier's full cost meets the sustainable margin, and none above its ask. Own-plant
   estimates (P01-P03) are unchanged. This closes the P06 item that was here under "Not yet done".
8. **Overfitting gate** (`bench.py`). `--sim real --seeds 0,1,2` plays all 19 real case files on the calibrated twin
   and reports the hosted 10 and the other 9 apart, per seed. `--against <earlier bench file>` rejects a change that
   helps the hosted 10 but costs the other 9, on average or on most seeds.

Before the final:

```bash
cd starter-kit
uv run python -m buyer.bench --sim real --agents buyer --seeds 0,1,2                  # with all switches True
# set the switches to False (strategy.MONEY_SHEET, SUSTAINABLE_STOP, CLAUSE_CHEAP = 1.0; agent.LEAPFROG), run again,
# then judge the new code against that run:
uv run python -m buyer.bench --sim real --agents buyer --seeds 0,1,2 --against runs/bench-real-<switches-off>.json
uv run python -m buyer.argtable build && uv run python -m buyer.argtable show
uv run python -m buyer.runner run --set practice --writer claude -p 10                 # one error-free hosted batch
```

## Not yet done

- None of the 25 Oct changes has been played on the twin or the arena (see above).
- No Sunday-release handling beyond what `DataPackage` merges automatically.
