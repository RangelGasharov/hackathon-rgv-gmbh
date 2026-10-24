# Experiments: what helps, what hurts (25 Oct, night before the Live Final)

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
