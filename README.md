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

**Supplier profiles:** every one of the 46 suppliers the cases can involve has a profile, built once from the
data and a deep read of its documents: [`starter-kit/profiles/INDEX.md`](starter-kit/profiles/INDEX.md) (start
there) and [`starter-kit/profiles/README.md`](starter-kit/profiles/README.md) (how they are built and used). The
agent loads them at case start instead of reading documents under the clock. The profile and arena record select
one primary bargaining approach and one fallback; live concessions or resistance can trigger the single switch.
The plan reads the supplier's past negotiations, current news and letters (so new suppliers get one too) and shapes
how the strategist writes; its only pricing effect is a slightly deeper opening where the arena showed room below the
supplier's first counter. Trusted suppliers (strategic goal, partnership history, or our source for 8+ years) get
no risky trades, and after any final offer only the supplier's own terms go back. Code still prices every offer inside the shared procurement ceiling. First offers remain within the 60-second move window, each
supplier thread is capped at 10 messages (after the 10th we may still accept). In a multi-supplier case, closing the
procurement stops work on sibling threads.

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

## Not yet done

- Relationship-goal cases with only a generic cost estimate (e.g. P06): the agent can squeeze below the
  supplier's full cost, which the relationship charge punishes. Not hosted in practice, so not checked live.
- No Sunday-release handling beyond what `DataPackage` merges automatically.
