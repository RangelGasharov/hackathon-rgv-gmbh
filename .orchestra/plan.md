# Plan

## Done
- M0 Audit of improve/supplier-levers — fixes, trusted-supplier rules, plan from history/news/letters · commit f8169b1
- M1+M2 Evidence and live A/B — 28 sessions; kept clause-pair fix (P02 0.724 x2) and no 5 % quality/cash-short cap (P11
  mean 0.68); rejected lean, deadband-0, deep raw material · checkpoint CP-1 · retro: retros/M1-evidence-ab.md

- M3 Tuning search (read-only, 25 Oct 02:45-03:20) — 210 team sessions mirrored, levers/challenge/gaps mapped, ranked
  plan in TUNING.md; 4 workers (1 haiku, 3 sonnet), all first try, ~550k worker tokens · retro: retros/M3-tuning-search.md

## M4 Build TUNING.md top 3 behind switches (started 03:30; workers on the API key via .claude/api_worker.sh)
| Task | Tier | Status | Tries | Files | Notes |
|---|---|---|---|---|---|
| T-020 BUYER_PACE | sonnet | done | 1 | flags, claims, agent ~595-635, tests/test_pace.py | |
| T-021 BUYER_FLOOR + BUYER_FINALC | sonnet | done | 1 | flags, agent ~440-595, strategy, tests/test_floor_finalc.py | |
| T-023 pace: P18 pool had only 2 claims; weak true claims allowed under pace | sonnet | done | 1 | claims, agent, tests/test_pace.py | $0.29; later claims are weak ("does not change" 6/6) |
| T-022 proof: 133 tests pass (test_twin_real collection error, encoding) + 1 live P18 (user) | manager | running | 1 | | API cost T-020 $0.15, T-021 $0.47 |

## M5 Strategy selection + cleanup audits (read-only, started ~04:00, user)
| Task | Tier | Status | Tries | Files | Notes |
|---|---|---|---|---|---|
| T-030 strategy selection per supplier | sonnet | done | 1 | runs/analysis/strategy.md | $0.36; label text-only; 4-6 tactics/session |
| T-031 overhead/redundancy audit | sonnet | done | 1 | runs/analysis/cleanup.md | $0.21; ~73 dead lines, runner utf-8 |
| T-032 per-supplier strategy for all 46 (profile + all its sessions) + trait fallback; no case-specific numbers; leave-one-case-out check | sonnet | done | 1 | runs/analysis/strategy_rules.md, supplier_strategy.json | $0.45; only 12/46 suppliers have evidence; LOCO 6-7/12 (chance 4) |
| T-033 prevent timeouts (diagnose all 6 + thread timeouts, guards always on) | sonnet | done | 1 | agent, runner, writer/llm timing, tests/test_timeouts.py | user 04:20; $0.41; 141 tests pass; persona twin seed 0: HEAD 45.7 -> 46.7 (P05 0.44 -> 0.62) |

- M6 Build (05:15-06:10): P08 agreed-loop fix, BUYER_STRAT (buyer/approach.py), cleanup (LEAN/DEEP/dead fns), runner utf-8 +
  no backoffice in final; 153 tests pass; twin seed0 45.7 -> 51.1 with STRAT; live STRAT: P18 0.552 (base 0.467), P10 0.679 (=);
  API cost $3.93. Open: P05 twin 0.62 -> 0.44 after T-040; P12 under Floor-chase jump; nothing committed.

- M7 (06:30) T-044: BUYER_STRAT default on; P12 jump capped at 1.5 %; tenders keep base opening; approach-aware tender hold.
  156 tests; twin 53.2/48.9/50.4/55.3, kit 62.1 (was off 45.7/45.7/47.7/46.6, 53.7); P05 0.62-0.72; P12 seed 1 0.90 vs 0.95. $0.91.

## M8 Block A: offline hardening (user 06:50; no live sessions)
| Task | Tier | Status | Tries | Files | Notes |
|---|---|---|---|---|---|
| T-050 calibrated twin loads on Windows | haiku | done | 1 | twin_real.py read helpers | $0.01; `python -m buyer.twin_real bench` = 69.5 |
| T-051 P17/P01/P02 ~0 on twin: agent or twin? | sonnet | done | 1 | runs/analysis/lowcases.md | $0.65; P17 agent (relationship), P01 twin, P02 both; P07/P13 open |
| T-052 no-deal guard, clause direction, payment days | sonnet | done | 1 | tests/test_no_deal.py | $0.22; all 4 already held |
| T-053 relationship cases: charge from own estimate, bid above ask | sonnet | done (accepted by manager) | 1 | strategy, tests/test_relationship.py | $0.37; twin 62.3/59.0/64.4/68.9, real-twin 69.5 =, kit 62.1->58.9 (kit sim has no relationship charge) |
| T-054 P07/P13 + every no-deal across benchmarks | sonnet | done | 1 | runs/analysis/lowcases2.md | $0.75; P07 agent (f_hat), P13 mostly twin, kit P09/P18 wrong no-deals |
| T-057 floor guess + always-procure fixes | sonnet | done | 1 | strategy, agent | $0.43; twin 64.9/61.4/66.7/71.0, kit 61.7, real 69.5; P07 0.58-0.65 |
| T-055 total-cost levers: is every C term negotiated (Incoterm/freight, lead time, tooling, chargeback, effective date), tenders by C | sonnet | done | 1 | tests/test_tco.py | $0.38; all levers held, tenders by C |
| T-056 regression gate scripts/gate.py + baseline | haiku | done | 1 | scripts/gate.py, gate_baseline.json | $0.02; PASS, ~5 min, per-category check |

## Next (not started; see TUNING.md top 3)
- One new verified argument per later round; answer volume asks concretely; minimum 0.5 % late steps
- Compare with the other agent's LLM-led negotiator before the 13:00 freeze (team decision)
