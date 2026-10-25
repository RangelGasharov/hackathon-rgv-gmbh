# Plan

## HANDOFF (25 Oct, 08:10 CET) — read this first
The next agent continues from **WINNING_PLAN.md** (repo root): workstreams W1-W7, CET timeline to 15:10, live budget,
checklists, runbook. State at handoff:
- Pushed build = this commit: BUYER_STRAT default on, T-060 wave ordering, T-061 tender/raw/renewal fixes, gate PASS
  (twin 65.4/66.5/71.7/70.8, kit 62.4, calibrated twin 71.8); baseline in starter-kit/scripts/gate_baseline.json.
- Live (build 7e45c3d, before T-061): all 10 hosted mean 0.683. T-061 changes are NOT live-tested yet.
- Next steps in order: W1 release (`uv run arena data` after 08:00 CET -> profiles build + check -> test release facts
  reach the agent), W2 final ops (BUYER_BUDGET_USD=40 + fresh runs/spend.json; timed 19-case wave sim; pre-flight),
  W3 categories raw material / quality (P11 open) / tenders, W4 live L1 (10) + L2 (5), W6 pitch + README.
- MUST CHECK (teammate's live test 07:50, EXPERIMENTS.md "Does not work" 5): holding arguments back from round 1
  raised P01's first counter by 0.60 and lost; extra kinds in later rounds are fine. BUYER_STRAT applies pacing to
  Leverage and Floor-chase (approach.py switches): make pacing send all strong (weight >= 0.5) arguments in round 1
  and use later rounds only for extra kinds, then gate + live P01/P18.
- Every code change must pass `python scripts/gate.py` (tests + 6 benchmarks + per-category means) before push.
- Practice quota ~94 left team-wide at 08:00 CET (resets 15:16 CET). Ask the user before any live session.


## M10 Pipeline analysis: inputs -> black box -> outputs (user 09:35 CET; read-only)
Pulled ddbb10f (other agent: release customer_loss/chip_shortage news, W3b quality like-terms + step floor behind
switches, framework-agreement status-quo price, RFQ two-step hold). Release downloaded on this checkout (data/release-sunday).
Team mirror refreshed: runs/team 319 sessions.
| Task | Tier | Status | Tries | Files | Notes |
|---|---|---|---|---|---|
| T-070 inputs audit | sonnet | done | 1 | runs/analysis/inputs.md | $1.10 |
| T-071 black-box audit (estimates vs truth, money left) | opus | done | 1 | runs/analysis/blackbox.md | $4.33; C exact except relationship; 119 pts vs team |
| T-072 outputs audit (mails, claim verdicts, asks, leaks) | sonnet | done | 1 | runs/analysis/outputs.md | $0.44; 0 contradicted claims |
| T-073 synthesis: ranked fixes | manager | done | 1 | ANALYSIS.md (repo root) | waves 1-4 proposed |

## M11 Build ANALYSIS.md waves 1-3 (user 10:10 CET; gate each; ask before live)
| Task | Tier | Status | Tries | Files | Notes |
|---|---|---|---|---|---|
| T-080 wave 1a: B1 promise, B2 holds, B3 boilerplate, A3 earlier sheet, C7 ask regex, C8 floor words | sonnet | done | 1 | agent.py, tests/test_wave1_agent.py | |
| T-081 wave 1b: C1 units, C2 hedge, C3 alias, C4 horizon, C5 news claims, C11, A5 rel, A6 clause tol, A1 tender f_hat | sonnet | done | 1 | dossier, claims, profiles, approach, strategy, tests/test_wave1_inputs.py | A5 reverted (P01 twin); C4 -> free give = longest contract; gate PASS |
| wave 3: profiles build (70210 trend -10.4) + T-083 deep reads of 6 release suppliers | opus | done, gate PASS | 1 | profiles/ | $0.63; 46/46 clean; 70340 hedged to 2027-12; 70170/70190/70270 cash_short strong |
| T-082 wave 2: A2 payment-day rules + B4 answer every ask | opus | done | 1 | strategy, writer, agent | $2.90; benchmarks PASS; replay test small-record rule widened |
| T-084 levers: volume shift from a rival source + committee business case (records P04/P10) | sonnet | ON HOLD (user 11:40: config approaches, no code) | 0 | agent, writer, claims, approach, tests/test_levers.py | user 11:00 |
| live record attempts A (after merge gate): P01 P02 P03 P05 P07 P08 P18 defaults + P11 BUYER_QSTEP=1, up to 3 tries each (.claude/runs/records.py) | manager | pending | 0 | | user: records P11 0.794, P07 0.769, P03 0.936, P05 0.852 |
| T-085 Live Final wave rules: every rule checked, gaps fixed, 19-case wave simulated | sonnet | done (gate + push pending) | 1 | runner, llm, client, tests/test_waves.py | $0.43; 19/19 closed by 284 s of 480; banned-endpoint guard; budget warning |
| T-086 BUYER_NIBBLE (one small counter below a close open offer, then close) | sonnet | done | 1 | flags, agent, tests/test_nibble.py | $0.23; default off; live via sets C and D |
| T-087 every news item: market news -> suppliers, owner/succession/rename kinds, bundles, memo | sonnet | done | 1 | dossier, approach, writer, tests/test_news.py | $0.40; sets C/D play on it (fresh processes) |
| live record attempts B: out-of-the-box config per case (.claude/runs/outofbox.json), all 10 hosted, 2 tries; was: P04 x2, P10 x2 | manager | pending | 0 | | user: records P04 0.860, P10 0.694 |

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

## M9 Toward first place (user 08:15: steps 1, 3, 4; release block B on hold)
| Task | Tier | Status | Tries | Files | Notes |
|---|---|---|---|---|---|
| live run all 10 hosted cases, defaults | manager | done | 1 | runs/live/all10-0815.log | mean 0.683; gaps P05 -0.42, P11 -0.17, P08 -0.14 |
| T-060 waves finish in time (offline) | sonnet | done | 1 | runner, agent timing, tests/test_waves.py | |
| T-061 general fixes for the weakest live cases (P05, P11, P08, P03/P04) | opus | done | 1 | strategy, approach, agent | $7.44; P11 still open; gate PASS: twin 65.4/66.5/71.7/70.8, kit 62.4, real 71.8 |

## Next (not started; see TUNING.md top 3)
- One new verified argument per later round; answer volume asks concretely; minimum 0.5 % late steps
- Compare with the other agent's LLM-led negotiator before the 13:00 freeze (team decision)
