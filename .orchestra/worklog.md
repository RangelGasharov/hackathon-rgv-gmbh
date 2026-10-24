# Worklog
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
