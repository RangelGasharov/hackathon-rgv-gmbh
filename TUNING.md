# Tuning plan for the buyer agent (25 Oct, before the 13:00 freeze)

Ranked changes for `starter-kit/buyer/` on branch `improve/supplier-levers`, built from all 210 team practice sessions,
a lever-by-lever read of the code and the challenge rules. Nothing here is implemented yet.

**Where we stand (live, this branch):** P07 0.728, P10 0.679, P02 0.724, P11 ~0.68, P18 0.467, P08 ~0.46.
**Who beats us:** the teammate agent with subject lines like `<item> part:<id> - price basis …` (call it C). On the same
cases it averages P18 0.54 / P08 0.53 / P11 0.73; ours ("Re: <case title>") averages 0.45 / 0.41 / 0.61.

## Top 3 (do these first)

| # | Change | Cases | Expected gain | Code | Effort |
|---|---|---|---|---|---|
| 1 | Hold back fresh verified facts: one new, not-yet-cited fact in each of messages 2-4 | P18, P08 (+P02, P11) | +0.07-0.10 on P18 and P08 | `claims.py:322-336` (`ration`), `agent.py:604-607` | M |
| 2 | P11-type quality cases: keep countering to the supplier's floor and never close on the 1500 ppm sheet | P11 | +0.08-0.17 | `strategy.py:905-922` (near-floor/stall stop), `agent.py:493-509` (accept rules), ppm term in sheets | M |
| 3 | Take the supplier's final-offer sheet and pick between its sheets by total cost C | P18, P08 | +0.02 each | `agent.py:462-492` | S |

### 1. Pace the arguments
- **Evidence:** our agent puts 4-6 facts into message 1 and then about 0.4 per later message; agent C adds about 1.5 per
  later message. The supplier's next step is 1.37 % of its opening after a message with a new verified fact, 0.53 %
  without (P18, n = 30 vs 59). Sessions where none of messages 2-4 carried a fact: P18 0.434, P08 0.423. With two or more:
  0.55 and 0.53.
- **Code today:** `ration` is greedy by weight. After message 1 it only passes claims with weight ≥ 0.5, and a kind used
  once never comes back. There is no per-round budget.
- **Change:** cap message 1 at the 2 strongest claims. Queue the rest one per message for messages 2-4, strongest first.
  Candidates: another index window or series (LCI-C, PPI), forecast, scorecard, our other POs (P08: `po:4500014455-10`,
  `po:4500014391-10`, EUR 5.45 elsewhere). Put it behind a switch in `flags.py` (e.g. `BUYER_PACE=1`) so it can be A/B'd.
- **Also fix:** `agent.py:607` computes claims even for hold notes, keep-alives and RFQ holds with no sheet, which uses up
  claim kinds and IDs for nothing.

### 2. P11: reach the floor
- **Evidence:** the supplier steps EUR 0.50 a round: 47.0 → 46.5 → 46.0 → 45.5 → 45.0, and 45.00 at 300 ppm is its floor.
  The floor comes only after 5 counters. 18 of 20 deals closed above it. Score by deal price: 45.0 → 0.794, 45.5 → 0.710,
  46.0 → 0.626. Our sessions stop after 4-5 rounds at 46.0. Deals at 1500 ppm add about EUR 8.6k of quality cost
  (−0.09 score): mean 0.56 against 0.68 at 300 ppm.
- **Change:** while the supplier still moves by a fixed step, don't accept its standing offer and don't settle on the
  "near floor" rule; accept at `final_offer` or after two flat rounds. Keep 300 ppm in every sheet and drop the 1500 ppm
  fallback. Opening 7-9 % below (we open 3.9-5.7 %) helped in the top sessions.
- **Watch:** 10 messages per thread and the 60 s move window. 7 rounds fit.

### 3. The final-offer sheet
- **Evidence:** P08 top sessions closed on the supplier's final offer 90 % of the time, the rest 45 %. Sessions that
  closed on an open or buyer-chosen sheet averaged 0.45. P18 best (0.618) took the supplier's 30-day 5.20 sheet over
  its 90-day 5.30: the 30-day sheet wins whenever its discount exceeds 60 days × 0.0222 % × price (about EUR 0.07/kg).
- **Change:** after `final_offer`, rank every sheet the supplier offered by C (the scoring formula) and take the cheapest.
  Today `agent.py:471` accepts at C ≤ reservation with no slack, while the agreed path (`agent.py:454`) uses
  reservation × slack. Check that this doesn't make us skip a good final sheet.

## Next tier (test only if time allows)

| # | Change | Evidence | Gain | Risk |
|---|---|---|---|---|
| 4 | Open 16-19 % below on raw-material and bundling cases (we open ~13 %), never past ~19 % | P18 corr 0.70; buckets 0.475 → 0.543; P08 0.469 → 0.552 | +0.05-0.08 | **Confounded:** C opened deep *and* paced facts. `BUYER_DEEP=1` (18 %) alone lost live on P08 (0.379 vs 0.462). Only try together with #1. |
| 5 | Answer every volume ask in the next mail; commit the binding volume in message 1 | corr 0.46-0.48; P08 top commits in mail 1 60 % vs 36 % | +0.01-0.03 | low |
| 6 | Treat contract length as free up to the horizon: offer the longest allowed contract as a give and trade it for price | Rules: months beyond the horizon cost nothing. The model values 36 m at +1 % to the supplier (`strategy.py:164-181`) | small | low |
| 7 | Guard: never close under 24 months | 12-month deals score 0.00-0.13 (uncovered need 145k-1M) | avoids disasters | none |
| 8 | P11: offer EUR 150 chargeback for 300 ppm when the supplier balks on price | one session 0.744 in 3 rounds | +0.01 | n = 1 |
| 9 | P08 only: deadband-0 clause package | db 0 mean 0.525 vs 0.476 (n = 6) | ? | `BUYER_CLAUSE0` lost live twice: keep off |

## Live Final facts to respect (from the brief and API docs)
- 5 waves, 8 min hard cap each (the host may extend; read `seconds_left`). A thread still open at the cap scores the
  **alternative**, not 0, so a good early deal beats running into the cap.
- More than 10 cases in a wave → the 10-open-session limit queues the rest. Start the cases with the most to gain first.
- Hidden cases cite the 08:00 release: run `arena data` after 08:00 and make sure release facts feed the dossier.
- Tie-break: more deals, then fewer rounds.
- `runner.py:286` calls the back office after every case; it returns 403 during the final. That's harmless (caught), but
  skip it when `set=final`.

## How to test without burning the quota
1. Build each change behind a `flags.py` switch, default off.
2. Unit test plus the twins (`buyer.bench --sim twin`) for crashes only. The twins reward front-loading by construction,
   so they can't judge #1.
3. Sandbox cases D01/D02 cost no quota: use them for a smoke run.
4. Live A/B: 2 sessions per case on P18, P08, P11 with the switch on (6-8 sessions), compared with the baseline above.
   Keep the playing machine idle (local CPU load stalled 3 sessions to 0 earlier).

## Sources (local, gitignored)
- `starter-kit/runs/analysis/gaps.md`: top vs rest per case, all numbers above.
- `starter-kit/runs/analysis/levers.md`: every lever with `file:line`.
- `starter-kit/runs/analysis/challenge.md`: final format, cost model, rules.
- `starter-kit/runs/team/`: all 210 sessions (`scripts/pull_team.py` adds new ones).
