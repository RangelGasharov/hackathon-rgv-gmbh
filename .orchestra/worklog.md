# Worklog

## 2026-10-25 08:05 — M8 block A (API-key workers)
- T-050 twin_real reads cp1252 records too; calibrated twin `python -m buyer.twin_real bench` = 69.5.
- T-052/T-055 tests only (tests/test_no_deal.py, tests/test_tco.py): no-deal guard, clause by index direction, payment
  days, every C lever in the grid, tenders awarded on C — all already held.
- T-053 relationship cases: charge from our own cost estimate; may pay above the ask when C is lower (P17 0 -> 0.8-0.9).
- T-057 no floor from one tiny move (f_hat capped, >= 2 moves, settlement history); always make a first offer.
- T-056 scripts/gate.py + gate_baseline.json: tests + 6 benchmarks + per-category means; run before every push.
- Live P02 defaults 0.735 (was 0.724).

## 2026-10-25 06:35 — M7 T-044 (API-key worker)
- BUYER_STRAT on by default (BUYER_STRAT=0 turns it off); single-switch tests pin it off.
- P12: Floor-chase jump capped at 1.5 % of the supplier's opening per round (it jumped 91.34 -> 98.11).
- P05: tenders keep the base opening under Quick-close; approach-aware holding notes; Leverage in a tender holds the
  agreed sheet once and cites the rival (`tender_hold`); no near-floor accept while a bidder still steps.
- 156 tests; twin seeds 0-3 53.2/48.9/50.4/55.3, kit 62.1 (old agent 45.7/45.7/47.7/46.6, 53.7).

## 2026-10-25 06:10 — M6 (API-key workers)
- T-043 runner.py: records as UTF-8; backoffice skipped when state set == final.
- T-040 agent.py: after `agreed`, no dearer sheet (min-step rule pushed the price up); accept or settle. tests/test_agreed.py.
- T-042: removed dossier.hedge_doc, Estimate._margin_tight, Planner.best_estimate, BUYER_LEAN, BUYER_DEEP (twin identical).
- T-041: BUYER_STRAT — buyer/approach.py picks Leverage / Floor-chase / Quick-close from the supplier profile (history,
  news, payments, quality, our weight), pricing room, rivals and our cost drivers; sets opening, tactics (<= 3), switches;
  one correction after 2 replies; asks -> priced trades. tests/test_approach.py.
- Live with BUYER_STRAT=1: P18 0.552 (was 0.467), P10 0.679 (same).

## 2026-10-25 04:40 — M4/M5 (Windows checkout, API-key workers)
- T-020/T-021/T-023: switches BUYER_PACE (2 claims in msg 1, then 1 fresh per msg; weak true claims allowed), BUYER_FLOOR
  (keep countering a stepping supplier; floor only from its own signals; approval replies not flat), BUYER_FINALC
  (cheapest final-offer sheet by C). Live P18 with all three: 0.443 (baseline 0.467), before the T-023 pool fix.
- T-033: timeout guards always on (20 s write cap -> template, holding notes after 30 s, close_now, forced settle).
- T-030/T-031/T-032: runs/analysis/{strategy,cleanup,strategy_rules}.md + supplier_strategy.json (46 suppliers).
- Proof: 141 tests pass; persona twin seed 0 HEAD 45.7 -> 46.7.

## 2026-10-25 03:20 — M3 tuning search (Windows checkout)
- T-010: scripts/pull_team.py mirrors every team practice session to runs/team/ (210, read-only GETs, no quota).
- T-012: runs/analysis/levers.md maps 8 lever groups with file:line; possible bugs agent.py:607 (claims on hold notes), agent.py:471 (no slack after final offer).
- T-014: runs/analysis/challenge.md — final format, cost model, rules; contract months beyond horizon are free; open threads at wave cap score the alternative.
- T-011: runs/analysis/gaps.md — our family A trails teammate family C on P18/P08/P11 by 0.09/0.12/0.12; biggest gap: fresh facts after round 1.
- T-013: TUNING.md ranks the changes (pace facts, P11 floor, final-offer sheet by C). Nothing implemented.

## 2026-10-25 — M2 Live A/B (28 sessions)
- strategy.py: `_clause_pair` prices the fallback clause sheet at the package's relationship-optimal price (P02 0.724 x2).
- strategy.py: no 5 % opening cap for quality / cash-short suppliers (P11 0.794, 0.620, 0.620).
- flags.py: BUYER_LEAN (single-supplier only), BUYER_CLAUSE0, BUYER_DEEP (raw material) - all off, all lost live.
- tests/test_profiles.py: clause-pair price, lean target; EXPERIMENTS.md with every result.
<!-- What was implemented, newest first. For the user to read; no session needs to load this to resume work.
     One entry per batch of finished work, a few lines per task, taken from worker reports. -->

<!-- ## YYYY-MM-DD — M<n> <name>
- T-NNN <title> (<tier>): <what changed, by file> · <decision made, if any>
-->

## 2026-10-25 — M1 Evidence
- T-002 (manager): `buyer/flags.py` switches (BUYER_OFF=..., BUYER_CLAIMS=N) wired into agent/dossier/writer; default unchanged (twin_real 0.731).
- T-003 (manager): twin ablations -> EXPERIMENTS.md §2. No code-priced component worth removing on the twins.
- T-001 (sonnet): transcript mining -> EXPERIMENTS.md §1 (front-loaded arguments, unreciprocated concessions, unanswered asks).
- `starter-kit/scripts/ab.sh`: live A/B harness logging to runs/ab/results.tsv.
