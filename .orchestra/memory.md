# Project memory

## Goal
Raise the buyer agent's expected score on the Tacto Negotiation Arena (team RGV GmbH) before the Live Final
(Sun 25 Oct 13:30; code freeze 13:00; new data release 08:00). Remove parts that hinder results, prove changes with
live practice sessions, document what works and what doesn't. Score per case = (C_open - C_deal) / (C_open - C_best).

## User preferences & standing answers
- Work only in the worktree `tacto-supplier-levers` on branch `improve/supplier-levers`; never edit the main checkout
  `tacto-buyer` (another agent works there, uncommitted WIP in agent/claims/dossier/strategy/writer/twin_real).
- Live arena sessions: authorized for this project, budget **40 sessions** total for M2/M3 (user, 00:25). Count every
  `play`/`run` session against it; check `/v1/me` before each batch.
- Final code: changes stay on `improve/supplier-levers`; the team merges into main (user). Never merge into main.
- Other agent (tacto-negotiation-are-2, local_a5963e3f...) was told my budget (user OK'd the note).
- Use lean-orchestrator; debugging via the `investigate` skill (no /debug installed); grilling format for decisions.
- Commits: commit and push `improve/supplier-levers` at milestone close.

## Decisions
- 2026-10-25 Plan text shapes writing only; code prices every sheet (twin: plan-driven pricing lost P10/P05).
- 2026-10-25 After a final offer only the supplier's own terms go back (changed: 3 walks/5; own: 2/2 agreed).

## Constraints & gotchas
- Practice quota is team-wide: 400/day, resets 2026-10-25 16:16 CEST; 248 left at 00:20. Check `/v1/me` before a batch.
- Supplier replies are noisy (LLM suppliers): one session per case is weak evidence; compare on the same cases.
- Twins: `twin_real` (10 hosted cases, fitted) is insensitive to openings; persona twin (19 cases, seeds) is not the arena.
- `runs/` is gitignored; session records stay local. Main checkout's `runs/` holds ~80 real transcripts (read-only use).
- `.env` and `data/` in the worktree are symlinks to the main checkout (read-only use).
- Arena: only the standing sheet (first of a reply) can be accepted; `-a` alternatives are taken by sending their terms back.
- Each argument kind counts once per supplier; contradicted data costs trust; final offers are final.

- Arena: at most 10 OPEN sessions per API key (429 beyond), shared with the other agent: run one `scripts/ab.sh` at a
  time, AB_MAX=6 (default). A 429 opens no session.

## Management rules (learned from retros)
- Subagents cannot write report files (Write refuses 'report' files for subagents): cards ask for findings in the
  NOTES reply, or have the worker's script print them, instead of a report.md.
