# Plan

## Done
- M0 Audit of improve/supplier-levers — fixes, trusted-supplier rules, plan from history/news/letters · live P01 0.992, P18 0.467 · commit f8169b1

## Current: M1 Evidence — which parts hinder results (no live sessions)
Goal: a ranked list of components to remove or change, backed by the ~85 real transcripts and twin ablations.
Done when: EXPERIMENTS.md lists each candidate with real-session evidence and twin A/B numbers.

| ID | Task | Tier | Status | Tries | Files | Tokens | Notes |
|----|------|------|--------|-------|-------|--------|-------|
| T-001 | Mine real transcripts: what moved suppliers, what cost us | sonnet | done | 1 | scratch only | 214k / 10 min | report refused by Write policy for subagents; numbers in NOTES -> EXPERIMENTS.md §1 |
| T-002 | Ablation switches (env flags) for candidate components | manager | done | 1 | buyer/flags.py, agent.py, dossier.py, writer.py | | default unchanged: twin_real 0.731 |
| T-003 | Twin ablations, both twins, 3 seeds | manager | done | 1 | scratch only | | probe 0, match -1.3 (s1), claims1 -2.5..-4.4 / real -0.027, claims2 -0.3 / -0.018, profiles off +0.1 persona |
| T-004 | Back-office decision rule (counter/hold/final vs our step, claims) | sonnet | done | 1 | scratch only | 198k / 10 min | rule: counter if our step >= 0.5 % and gap >= 2 %; fixed increment; final by round count |
| T-005 | `lean` concession variant (flag), unit test, twin check | manager | done | 1 | buyer/strategy.py, flags.py, tests | | persona twin +3.9/+5.3/+4.9, twin_real = |
| T-006 | Live round 1: lean x10 cases + base x4 (P02 P07 P08 P11) | manager | doing | 1 | runs/ab | 14 sessions | budget 14/40 |

## Next
- M2 Live A/B — baseline vs leading candidate on the same cases, within the session budget
- M3 Iterate + document — keep what wins, revert what loses; EXPERIMENTS.md + AGENT_STRATEGY.md; push
