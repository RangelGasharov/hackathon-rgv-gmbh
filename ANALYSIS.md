# Pipeline analysis: inputs → black box → outputs (25 Oct, 10:00 CET)

Build analysed: `ddbb10f` (branch `improve/supplier-levers`), Sunday release loaded. Evidence: our last full live run
(build 7e45c3d, mean 0.683, team best per case 0.792), our 18 own records, and 319 team sessions (244 of them our agent,
1,034 of our e-mails, 2,022 supplier e-mails). Detail and scripts: `starter-kit/runs/analysis/{inputs,blackbox,outputs}.md`.

**Points** below = 100 × EUR / (C_open − C_best) on that case, i.e. score points ×100; "vs team" = against the team's
best session on the case. The 10 hosted cases lose **119 points vs the team best** (11.9 per case).

---

## 1. What is healthy (do not touch)

| Area | Evidence |
|---|---|
| Our cost model | Our C equals the arena's C **to the euro** on all 10 deals, every part except the relationship charge |
| Facts we cite | 619 of 628 verified (98.6 %), **0 contradicted** in 244 sessions; claims mails move the supplier 82 % vs 48 % without |
| Approach choice | Matched the supplier's real behaviour in 10 of 12 threads |
| Mechanics | 0 template fallbacks (strategist max 10 s vs 20 s cap), 0 must-have breaks, 0 pressure/budget leaks, mails < 1,500 chars |

The points are lost in **a few decision rules, some message habits and several input bugs** — all fixable with
general rules.

---

## 2. Ranked findings

Status: **fixed** = already in code at HEAD, needs live confirmation · **open** = to build.
Effort: S ≈ minutes, M ≈ an hour. Risk = chance of making something else worse.

### A. Black box (decisions) — the big points

| # | Finding | Points | Where | Fix (general) | Status / effort / risk |
|---|---|---|---|---|---|
| A1 | **Tender: signed the dearest of 3 standing sheets** and held the rival quote back from a bidder (P05) | ~38 | agent.py decide, rival rule | sign a final only if cheapest standing; cite cheapest rival early when a bidder is ≥ 8 % above | **fixed** (29bf84c), needs live; open part: ignore round-1 drops ≥ 10 % in tenders when guessing the floor (strategy.py ~965) · S · low |
| A2 | **Payment days too short**: strategist dropped our 90-day sheet; a mis-read ("refused 90 d" when it had *offered* 90 d) became short-payment trades; the cash rate learned from one pair overwrote another; two equal replies locked 30 d (P03, P04, P07) | 27 | writer/strategist menu (strategy.py `menu` ~904), trade_sheets ~1281, `observe_reply` ~1183, `Estimate.rate` ~119 | never offer shorter payment than the planner's best sheet unless the supplier insisted twice; a "shorter payment" ask becomes a trade only if the supplier's own sheets show it; no lock from our own dropped alternative; learn the cash rate per day band | **open** · M · low (team: 90-day deals 0.92 vs our 30-day 0.85 on P03) |
| A3 | **Final offer ignores the same supplier's earlier, cheaper sheet** (P03: 432.29@90 d two replies earlier was €25.8k cheaper) | 7.6 | agent.py final-offer path ~504-563 | at a final offer, send back the cheapest of *its* earlier sheets once (like take_alternative) | **open** · S · low |
| A4 | **Price steps too small against a holding supplier** (P11: 46.00 vs team 44.50 at the same 300 ppm) | 25 | strategy.py `qstep` ~756 (BUYER_QSTEP, off) | when it holds and the gap to its ask ≥ 2 % of its opening, step ≥ 0.5 % of opening (all approaches) | **open** behind switch · S · medium → **live A/B needed** |
| A5 | **Relationship charge estimated 30–40 % too high** (P01–P03): averaging a convex penalty over 5 cost guesses inflates it, so the planner aims at the wrong price (P02: arena best 368.85 without clause, 6.5 pts better) | ~8 | strategy.py `rel_charge` ~239 | point estimate (or narrower spread) when the cost estimate is tight (err ≤ 0.05) | **tried and reverted (wave 1)**: point and half spread both dropped P01 on the persona twin (seeds 1–3: 0.14/0.83/0.68 → 0.01/0.58/0.40), calibrated twin unchanged → kept the spread |
| A6 | **Clause tolerance too loose**: `_clause_pair` allows +1.2 % of C (= 57 pts of P02's room) | part of P02 | strategy.py ~900 | tie the tolerance to the negotiation room, e.g. ≤ 2 % of (C_open − our C_best) | **open** · S · low |
| A7 | Deadband-0 clause beside deadband-5 (P08, P18) | ~14–17 | `_clause0_sheet` (BUYER_CLAUSE0, off) | offer both in the first two messages for raw material | **conflicting**: team data favours db0 (P08 0.52 vs 0.50, P18 0.55 vs 0.50) but our live A/B lost twice → skip unless spare live sessions |

### B. Outputs (what we send)

| # | Finding | Frequency | Where | Fix | Effort / risk |
|---|---|---|---|---|---|
| B1 | **Broken promise**: "If you confirm this package, we accept it in our next message" — supplier confirmed 87×, we did **not** accept 37× (43 %) | 305 mails | agent.py ~1120 | say it only when `decide` would accept that reply; otherwise "we will give you a clear answer in our next message" | S · none |
| B2 | **Holds after a final offer** ("we are reviewing all offers") — 32 of 44 holds just drew the same final offer back; costs rounds (P05: 3 rounds) | 44 mails, 25 after final | agent.py ~843, ~552 | after a final offer: accept or answer with a sheet; hold only while another bidder's reply is pending | S · low |
| B3 | **Boilerplate repeated**: 32 % of long sentences copied from the previous mail; "We will pay every invoice on the agreed due date" in 32 % of mails | all mails | agent.py ~1041, 1045, 1081 | each standing commitment once per thread, then only the new fact | S · none |
| B4 | **Unanswered asks**: 57 of 527 (11 %), worst on data (21) and payment (6 of 11) | 57 | writer `asks_for` ~200, agent answers | map each ask to a file or a number before composing; answer it first | M · low |
| B5 | Rival-price claims move suppliers least (50 % vs 91–100 % for cost/index/document facts) | 16 % of claims | claims.py weights ~240 | outside tenders lead with cost/index facts; rival price only when no cost fact is left (keep A1's tender rule) | S · low |

### C. Inputs (what goes in)

| # | Finding | Affects | Where | Fix | Effort / risk |
|---|---|---|---|---|---|
| C1 | **Forecast schedule CSV sent in raw units** while our commitment is per price unit: the supplier sees a **100× gap** | P07, P14, P15, P16 and every hidden case with price_unit ≠ 1 | dossier.py ~461 | divide qty by the part's price_unit; label the column | S · none |
| C2 | **Hedged-supplier wording in the release missed** ("bought forward at fixed prices … from 2028"): index-fall claims keep full weight against a hedged supplier | raw material, price increase, renewal (70340 + hidden) | dossier.py ~40, ~81 | broaden the hedge regex (forward / fixed prices / secured … for <year>), parse any month/year | S · low |
| C3 | **Old record 71999 of supplier 70210 never merged**: spend trend read +8 % instead of −10 % | P05 (tender) + any 70210 case | profiles.py ~90 | alias map (same-name stem + blocked + no contract, or news "renamed") → fold history under the live ID | S–M · low |
| C4 | **`horizon_months` read but the case has `horizon_years`**: horizon always defaults to 36 months | all cases using it | approach.py ~380 | read `horizon_years × 12` | **resolved (wave 1)**: the only reader was the free contract give; months beyond the horizon are free to us, so the give is now the longest allowed contract and the horizon cap is gone |
| C5 | `customer_loss` news reaches traits but never becomes a claim; the newest weak news item is not the one picked | release suppliers (70190, 70170) | claims.py ~11, ~276 | derive WEAK_NEWS from the news situation (slack, distress); pick the newest | S · none |
| C6 | Market news (no supplier, e.g. "water utilities push pump tenders into 2027") and the buyer memo never read; succession / new_owner / approvals / renamed kinds ignored | hidden cases | dossier.py ~46-76 | a case-level market read (soft demand → more room); trait hooks for owner change | M · medium |
| C7 | **Ask regexes mostly false hits** (data 67 %, volume 84 % false: "the data you cited", "volume schedule you provided") → CSVs sent unasked; 11 real "proof" asks missed | all | agent.py ~35-50, ~1130 | match request sentences only (question / please send / proof), exclude acknowledgements; add a proof-of-payment answer | S–M · low |
| C8 | Floor/approval words catch almost nothing (17 / 5 of 2,022 mails); "we cannot give up margin right now" (52), "our flexibility remains limited" (~19) missed | Floor-chase, approval | agent.py FLOOR_WORDS / APPROVAL_WORDS | add these phrases as weak floor / soft reply when the same mail shows no price move | S · low |
| C9 | **6 profiles stale vs the release** (70170, 70190, 70270, 70340, 70560, 70640), but marked fresh | hidden cases with these suppliers | profiles/ | rebuild those six (`python -m buyer.profiles build ...`) | S · low (LLM cost for persona reads) |
| C10 | Bundling judged by the kit's first part only (scorecards, quotes, PO) | P07, P15 | dossier.py ~224 | aggregate across parts | M · medium |
| C11 | Stale-price PO lines cited; turnover band ignored in the approach; delivery reliability cites only `otd_pct` | small | dossier.py ~488, approach.py ~162, claims.py ~147 | filter `price_note=''`; use the band midpoint; keep as is | S · none |

### D. A warning about our offline benchmarks
`cases/practice` differs from what the arena actually plays (e.g. P01 total cost 2.40 M in practice vs 1.10 M in the
arena); `cases/real` matches the arena. The gate measures on `cases/practice`. Live runs are unaffected (they use the
arena's case). → Treat twin/kit numbers as a regression guard, not as a score forecast; consider adding `cases/real` to
the gate.

---

## 3. What to build, in order (fits before 12:15)

| Wave | Contents | Why first | Time | Proof |
|---|---|---|---|---|
| **1. Safe bug fixes** (always on) | C1, C4, C5, C2, C3, C7, C8, C11, B1, B2, B3, A3, A5, A6, A1-open (tender floor guess) | small, general, no behaviour risk; several affect hidden cases directly | ~60 min, 1–2 workers | gate PASS |
| **2. Payment-days rules** | A2 (menu, trades, locks, cash rate per band) + B4 (answer every ask) | 27 pts on 3 of 10 cases, renewals likely a whole wave | ~45 min, 1 worker | gate PASS |
| **3. Profiles** | C9 rebuild the six release suppliers | hidden cases may use them | ~15 min | `profiles check` clean |
| **4. Live check** (needs your go) | all 10 hosted once (10 sessions) on the new build; then P11 with A4 on vs off (2) | confirm A1/A2/B-fixes; decide A4 | ~25 min | mean ≥ 0.683, P05/P03/P04 up |
| Later / only with spare time | B5, C6, C10, A7, D | medium effort or conflicting evidence | — | — |

**Expected effect** if waves 1–2 land and A1 holds live: about **+40 to +70 points** over the 10 hosted cases (from the
measured losses: P05 ~38, payment mix ~27, P03 final 7.6, relationship ~8), i.e. **+0.04 to +0.07 on the mean**, plus
safer behaviour on hidden cases (units, hedging, release news, ask parsing). A4 could add up to ~25 on quality cases if
the live A/B confirms it.

## 4. Stop rules
- Every wave passes `python scripts/gate.py` (tests + 6 benchmarks + per-category means) before it is pushed.
- Feature freeze 12:15 CET; only crash fixes after that; code freeze 13:00.
- Live sessions only with the user's go (team quota ~94 at 08:00, shared).
