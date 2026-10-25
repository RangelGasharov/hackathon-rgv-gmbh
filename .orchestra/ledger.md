# Verification ledger

### CP-1 — after M1+M2 Evidence and live A/B — 2026-10-25
- Ref: commit after 10eb4ea on improve/supplier-levers (this checkpoint's commit)
- Covered files: starter-kit/buyer/{strategy,agent,dossier,writer,flags}.py, scripts/ab.sh, scripts/ab_report.py, tests/test_profiles.py
- Evidence: pytest 132 passed (2 runs-dependent tests deselected); twin_real 0.731 (= main); persona twin 64.3/65.5/64.1
  (P02 drop is the old twin's no-clause supplier); live: P02 0.724 x2, P11 0.794/0.620/0.620
- Not covered / known gaps: final-offer alternative path covered by unit test + 2 live sessions only; no live run of the
  final build on P05/P07/P10 (unchanged code paths for them)
