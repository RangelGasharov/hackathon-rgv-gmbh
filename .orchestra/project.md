# Project map

## Stack
Python 3.12, uv, pytest, ruff. Package `buyer` (our agent) on top of the official `arena_kit` starter kit.
LLM: Claude Opus 5.5 via `buyer/llm.py` (spend capped by BUYER_BUDGET_USD in `.env`, tracked in `runs/spend.json`).

## Commands (run from `starter-kit/`)
| Purpose | Command | Notes |
|---|---|---|
| Install | `uv sync --all-extras` | |
| Test (all) | `uv run pytest -q --deselect tests/test_twin_real.py::test_replay_matches_the_real_suppliers --deselect tests/test_relationship.py::test_charge_matches_the_real_deals` | ~30 s; both deselected tests depend on which transcripts sit in `runs/` |
| A/B switches | `BUYER_OFF=probe,match` / `BUYER_CLAIMS=1` (see `buyer/flags.py`) | default: all on, 3 claims |
| Test (one file) | `uv run pytest -q tests/test_profiles.py` | |
| Lint / format | `uv run ruff check <files>` / `uv run ruff format <files>` | pre-existing E501s in agent/dossier/strategy |
| Twin, calibrated | `uv run python -m buyer.twin_real bench` | 10 hosted cases, template writer, no LLM, ~20 s |
| Twin, persona | `uv run python -m buyer.bench --sim twin --agents buyer --cases-dir cases/real --seed N` | 19 cases, seeds 0-2 |
| Live, one case | `uv run python -m buyer.runner play case:P01 --writer claude` | costs 1 practice session + LLM |
| Live, all practice | `uv run python -m buyer.runner run --set practice --writer claude -p 8` | 10 sessions |
| Quota | `ArenaClient(...).me()` (see arena_kit/client.py) | read-only |

## Layout
- `starter-kit/buyer/` — agent: dossier (data -> traits/economics), strategy (Planner: packages, prices, accept), claims
  (data arguments), writer (template + Opus strategist + guard), agent (BuyerAgent: decide/compose/accept), runner
  (threads, timing, play/run), persona (live read), profiles (46 prebuilt supplier profiles), twin/twin_real (local sims)
- `starter-kit/profiles/` — built supplier profiles (json/md), `persona/` deep reads
- `starter-kit/cases/real/` — the 19 practice case files; `starter-kit/runs/` — local session records (gitignored)
- `AGENT_STRATEGY.md` — handoff + results; `README.md` — architecture and results history

## Key entry points
- `buyer/agent.py` BuyerAgent.start / decide(threads, sid) / compose / accept (closure in decide) / write
- `buyer/strategy.py` profile_strategy(dossier), Planner(case, est) with _anchor_gap, target_value, sheets, menu, probe
- `buyer/claims.py` candidates(world, dossier, case, rivals, own_price), ration(claims, used, round_no, kinds)
- `buyer/writer.py` StrategistWriter (prompt, menu choice), guard(body, plan)
- `buyer/runner.py` play(table, case_id, factory) -> result dict with agent_log
