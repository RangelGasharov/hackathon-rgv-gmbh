# Experiments: what helps, what hurts (25 Oct, night before the Live Final)

## Summary

**Works (kept, default on):**
1. **Clause sheet at its cheapest price for us** (`Planner._clause_pair`, bug fix): P02 0.630 -> 0.724 live, twice
   (final build, 02:07: 0.724 again, it took 359.92 at once).
2. **No 5 % opening cap for quality cases and cash-short suppliers** (`Planner._anchor_gap`): P11 0.794 / 0.620 /
   0.620 (mean 0.68) against 0.626 / 0.635; matches the LLM-led team best (0.794, opened 7.5 % below).
3. **After a final offer only its own terms go back; its alternative is taken by sending its terms back** (e099702):
   own terms agreed 2 of 2, changed terms walked 3 of 5; P18 0.467 instead of 0.443 on the standing sheet.
4. Earlier audit fixes (no 4 % fast-close cap, no inverted round-1 cue, the 10th counter kept): persona twin back to
   `main` (68.0 vs 66.9 before).

**Does not work (rejected live, off behind switches in `buyer/flags.py`):**
1. Lean concessions (small fixed steps that keep a 2.2 % gap): P05 0.381 vs 0.725; elsewhere +-0.03.
2. Deadband-0 index clause trial: P08 0.438 vs 0.462, P18 0.430 vs 0.467 (the other agent: 0.614, 0.569, 0.449).
3. Deep raw-material openings (18 %): P08 0.379 vs 0.462 (its counters shrank to 0.03), P18 0.497 vs 0.467.
4. Fewer arguments per message: twins -0.02 to -0.03.
5. One new argument per later round (two in the first message, then one new one per message; the same idea as
   `BUYER_PACE`), tested at 07:50 on build 9652365, i.e. before `BUYER_STRAT`, from a local switch:
   P01 0.981, 0.978 vs base 0.992 (same hour) and 0.992; P05 0.762, 0.717 vs 0.723 and 0.725. On P01 the argument
   held back raised the first counter by 0.60 (134.40 vs 133.80) and the later ones did not win it back. Twins:
   persona -1.2 to -1.3, calibrated 0.713 vs 0.731. Only P01 and P05 of the hosted cases have three or more
   argument kinds with weight >= 0.5, so it changes little elsewhere.

**No measurable effect (twins):** `probe` never fires on the hosted cases; profiles off +0.1 on the persona twin;
`match` off -1.3 on one seed (kept).

**Operational:** keep the machine that plays idle (three sessions stalled to 0 under local CPU load); at most 10 open
sessions per API key (the team shares them).

**Add or iterate next (evidence, untested here):**
1. (Tested 25 Oct 07:50, rejected: see "Does not work" 5.) New arguments in later rounds help only when they are
   extra kinds, not ones held back from round 1: round-1 arguments lower the first counter.
2. Answer the supplier's own asks with a concrete trade: volume (asked in 83 replies, followed up 5 times) and
   faster payment where it is a term; the P18 volume answer reached 5.40/90 days (0.532).
3. Late steps of at least 0.5 % of its opening while its price is >= 2 % above ours (P11's 0.620 held after 0.13 steps).
4. P02: the clause-free path at 90 days reached 0.743 (other agent) against the clause path's ~0.735 ceiling.
5. Offer main-source status (moving another supplier's volume of the same part): P08's biggest step (0.12).
6. The other agent's LLM-led negotiator beats this rule agent on P02, P03, P08, P11, P18; worth comparing on the
   final's case types before the 13:00 freeze.

7. The other agent's mechanics with evidence: `/Users/victor/tacto-hackathon/negotiation-lab/PORTING.md`. Among them:
   verified arguments in round 1 lower the first counter (P07 5.58 -> 5.54, P02 372.65 -> 371.84, P18 5.80 -> 5.60);
   the supplier's money rate shows in two of its sheets that differ only in payment days (P02 6.6 %, P08 7.5 %,
   P03 ~4 %); bottleneck suppliers answer a ~15 % opening with an immediate final offer (P10, twice); do not accept
   open offers while it is still conceding.

Sessions used for this log: 34 (28 + 6 for the spread test).

Branch `improve/supplier-levers`. Every claim below names its evidence: real arena transcripts (84 sessions, 100
supplier threads, saved by several agent versions), the two local twins, or live A/B sessions run for this log.
Practice budget for this log: 40 sessions.

## 1. Evidence from the real transcripts (T-001)

| Finding | Numbers | Strength |
|---|---|---|
| Arguments are front-loaded | 164 of 182 arguments go out in round 1; 91 % of later messages carry none | strong |
| A new verified argument after round 1 moves the supplier | its next step 1.89 pp of its opening with one, 0.52 pp without (n = 12 vs 144, mostly P01 delivery record) | moderate |
| Round-1 arguments do not change the first counter within a case | +-0.5 pp | moderate |
| Our concessions are not reciprocated | when we raise our offer by 1.86 pp it returns 0.49 pp (ratio 0.26, 42 % nothing back); corr -0.17 (n = 156); after round 1 we close 73 % of the remaining gap ourselves | strong |
| Final offers come early | in 40 of 100 threads, median round 3; deals closed on a final-offer sheet 32, on an agreed sheet of ours 27, on an open offer 20 | strong |
| Non-deals | 5 of 84 (4 walks, 1 timeout), 0.033 of mean score: 3 after we altered a final offer (fixed in e099702), 1 haggling over 0.02, 1 P05 timeout | strong |
| Overpaying in round 1/2 (`agreed` at once) | 17 of 100 threads (P02-70510 x7, P05-70250 x6, P05-70210 x3, P18 x1); a clear overpay only at P05-70210 round 1 (0.34/0.45 vs 0.85) | moderate |
| Timeouts | all 16 in P05 (three bidders) | strong |
| Supplier asks go unanswered | it asked for volume in 83 replies and faster payment in 39; we followed up 5/57 and 5/26 | strong |

## 1b. The supplier's own decisions (back office, T-004)

The arena's back office logs every supplier decision ("My call: counter | hold | final_offer | accept"): 294 decisions
in 83 sessions (counter 217, hold 43, accept 23, final 7).

| Finding | Numbers |
|---|---|
| Our step decides counter vs hold, not its size | rounds >= 2: P(counter) 0.87 when our step is >= 0.5 % of its opening (n = 98), 0.54 below (n = 54) |
| The gap decides hold | P(hold) 0.06 when its standing price is >= 2 % of its opening above our best sheet (n = 88), 0.52 below 2 % (n = 64); after a hold, tiny steps hold again 13/16 |
| Its counter is a fixed increment | does not scale with our step (Spearman -0.34, n = 38): P18 exactly 0.10 (14/14), P11 exactly 0.50 (11/11), others 0.6-3 % of opening (P10 and P05 vary) |
| Final offers come by round count | P10 at our message 2 (8/8), P04 at 3 (3/3), P18 and P07 at 4 (6/7, 4/4), P11 at 4-5, P03 at 5-6; P01 and P02 never; 35 of 40 finals had no hold before |
| First counter | verified arguments: no effect outside P05 (n = 70); on P18 the 5.60 vs 5.70 split follows how deep our first offer was |
| Per case | P03/P04 counter once, then freeze about 1.5 % under the opening; P01, P02, P10 never hold; P11 holds 13/25; P05 gives half a step, then the rest, when we cite a rival |

Rule: **it counters (one fixed increment) when our step is >= 0.5 % of its opening and its price stays >= 2 % above
our best sheet; it makes its final offer after a set number of rounds.** So a bigger step from us buys nothing, and
closing the gap below 2 % buys holds. Our concession path did both: it heads for just under its estimated limit in
steps up to 1.5 %.

## 2. Twin ablations (T-003): components switched off, mean x 100

| Variant | Persona twin seeds 0/1/2 | Calibrated twin |
|---|---|---|
| current branch | 68.0 / 69.5 / 67.7 | 0.731 |
| no `probe` | 68.0 / 69.5 / 67.7 | 0.731 (never fires on these cases) |
| no `match` | 68.0 / 68.2 / 67.7 | 0.731 |
| no profiles | 68.1 / 69.6 / 67.8 | 0.731 |
| at most 2 arguments per message | 67.7 / 69.2 / 67.4 | 0.713 |
| at most 1 argument per message | 65.1 / 67.5 / 63.3 | 0.704 |

The twins reward front-loaded arguments by construction (a verified argument shifts the supplier's ask at once), so
they cannot judge spreading; the transcripts above say later arguments move suppliers. The twins have no LLM, so the
strategist's menu, the plan text and the playbook can only be judged live.

| lean concessions (`BUYER_LEAN=1`: 0.7 % steps, never within 2.2 % of its price) | 71.9 / 74.8 / 72.6 | 0.731 |
| deadband-0 clause trial (`BUYER_CLAUSE0=1`) | about equal (P08 0.41/0.43/0.40) | 0.722 (P08 0.405 vs 0.473, P18 0.416 vs 0.441) |

## 3. Live A/B (practice sessions, Opus writer, one session per cell unless noted)

`starter-kit/scripts/ab.sh` runs the sessions, `scripts/ab_report.py` prints scores, prices per round and the
supplier's own calls (c counter, h hold, F final, a accept).

| Case | base (current branch) | lean | notes |
|---|---|---|---|
| P01 | 0.992 (earlier tonight) | 0.959 | base: it agreed to our 131.84; lean: its final 133.30 |
| P02 | 0.630 -> **0.724 with the clause fix** | 0.630 (before the fix) | see 4.1 |
| P03 | - | 0.919 | counter, counter, hold, hold |
| P07 | 0.739 | 0.706 | four counters each, final in round 4 |
| P08 | 0.462 | 0.455 | |
| P10 | 0.679 | 0.679 | final at our 2nd message either way |
| P11 | 0.626 | 0.635 | |
| P18 | 0.467, 0.467 | 0.473 | lean reached 5.40 (lowest ever) but at 14 days |
| P04 | - | 0.860 (round 3; round 2: 0, stalled) | team best 0.860 |
| P05 | 0.725 (round 3; round 2: 0, stalled) | 0.381 (round 3; round 2: 0, stalled) | RFQ: lean's small steps lose the rivals' pull |

Lean vs base on the seven cases with both: P05 -0.344, P01 -0.033, P07 -0.033, P08 -0.007, P10 0, P11 +0.009,
P18 +0.006. **Rejected**: no live gain anywhere despite the persona twin's +5 points, and a large loss in the RFQ.
The back-office rule (fixed increments) is real, but our bigger steps were not what limited the deals: the
supplier's final offer comes after a set number of rounds and lands where it lands.

| Case | base | deadband-0 clause trial (`BUYER_CLAUSE0=1`) | notes |
|---|---|---|---|
| P08 | 0.462 | 0.438 | it took deadband 0 at 5.38/30 days; expected index cost EUR 27,541 ate the lower price |
| P18 | 0.467, 0.467 | 0.430 | it took deadband 0 at 5.40/60 days |

**Rejected** live and on the calibrated twin. The other agent's single 0.614 on P08 (5.24, deadband 0, 45 days) did
not reproduce with our concession path.

### 3.1 Deeper openings (`BUYER_DEEP=1`)

The other agent's LLM-led negotiator (negotiation-lab/llm_v4.py) set the team bests by opening deep and then moving in
small steps: P11 0.794 (opened 43.50/42.96 at 300/1500 ppm, about 7.5 % below 47; steps 0.5, 0.3, 0.3, 0.3, 0.05; the
supplier countered in 0.50 steps to 45.00 by reply 6), P18 0.618 (opened 4.72-4.90, about 20 % below 6.10, with two
verified arguments; counter 5.50 instead of the usual 5.60-5.70; steps of 0.07-0.09 with a new argument each round;
took the 5.20/30-day alternative of its final). Our agent capped quality cases and cash-short suppliers at a 5 %
opening, raw material at 10 % and committee suppliers at 10 %.

| Case | base | deep | notes |
|---|---|---|---|
| P11 | 0.626 (lean 0.635) | **0.794**, 0.620 | 0.794: six counters to 45.00 at 300 ppm. 0.620: it held at 45.50 after our steps fell to 0.13 (< 0.5 % of its opening) and closed on its 1500-ppm sheet |

Twins: persona +3.4 / +1.3 / +1.6 (P12 0.25 -> 0.55 on every seed), calibrated twin unchanged.

### 3.2 Quality cases (WINNING_PLAN W3b): the ppm trade read as a price step

Root cause confirmed on P11: its first reply kept 47.00 but tightened 6400 -> 300 ppm (chargeback 0). The agent's
supplier model charges a ppm commitment below its real 2376 ppm as an extra cost (1.18/unit), so the reply read as a
2.5 % price step; the floor and limit read from it sat at 43.47 / 43.62, the planner thought it was past the limit and
crept in 0.13 steps (0.28 % of the opening, under the 0.5 % that keeps an arena supplier countering).

| Change (switch, default off) | Offline evidence | Why off |
|---|---|---|
| `BUYER_LIKE=1`: first move on like terms (`Planner.first_move`) | replaying the 0.794 session's supplier replies: the higher floor estimate (44.89) accepts its 45.50 in round 4; the old reading went on to 45.00 | the misread helps by accident: the real P11 supplier goes further (44.50-45.00) than the floor model predicts |
| `BUYER_QSTEP=1`: quality steps >= 0.55 % of its opening while >= 2 % apart | replays: same accept points, late steps 0.26 instead of 0.13 where the 0.620 session held; gate: persona twin P11 -0.05/-0.06, quality 0.80 -> 0.77 (seed 3), calibrated twin unchanged | gate fails; only the live counter/hold rule (back office) speaks for it |

Next: a live A/B of `BUYER_QSTEP=1` on P11 (two sessions) decides it; the twins cannot (their suppliers end at a
fitted limit, so smaller steps always look better). The best live P11 runs (0.794, 0.877) opened deep and kept
the gap above 2 % with steps of about 0.28.

## 4. Bugs found and fixed

### 4.1 Clause sheet priced below the supplier's healthy margin (P02, `Planner._clause_pair`)
In a stabilise case the relationship charge rises as the price falls, so no clause price costs us what the clause-free
sheet does. The fallback then took the clause-free price less the clause's cost share (357.12) and allowed it at up to
1.2 % more cost by our own model (it was 0.88 %, about EUR 20k). The supplier wants the clause and took that sheet at
once: relationship charge EUR 66,941, score 0.63. Fix: the fallback prices the clause sheet where it costs us least
(the package's relationship-optimal price, 359.36-360.36); the 1.2 % limit stays. Live: **0.724** (the supplier took
359.92 at once). Regression test `test_the_clause_sheet_sits_at_its_cheapest_price_for_us`. The trial itself stays:
the real P02 supplier sells clause-free only above ~372. (The old persona twin, whose suppliers value no clause,
drops P02 from 1.00 to 0.00 with this fix; the real supplier does the opposite.)

### 4.2 Stalled sessions under local CPU load
Three sessions (P04 lean, P05 base and lean) composed their round-2 message but it never reached the arena; every
thread timed out and the suppliers walked (score 0). All three ran while six local twin benchmarks and the test suite
loaded the machine; no session stalled without that load. Rule: nothing heavy on the machine that plays.
