# Plan to win the Live Final (Sun 25 Oct 2026)

Written 08:00 CET, branch `improve/supplier-levers`. Code freeze **13:00**, agent must be polling at **13:30**,
top-5 pitch **15:10**. About 5 hours left. Everything below has an owner, a time, a test and a stop rule.

---

## 0. The short version

We are 5th. To finish first we need (a) the most points over 5 themed waves and (b) a pitch the jury believes.
Ranked by expected points:

| # | Move | Why it pays | When | Live sessions |
|---|---|---|---|---|
| 1 | **Load the Sunday release** and rebuild profiles | Hidden cases cite it; without it the agent argues blind and misses news that weakens a supplier | 08:00–09:00 | 0 |
| 2 | **Final operations are bullet-proof**: LLM budget, timing per wave, start/restart runbook | One budget cut-off or a case still open at the cap costs a whole case; a crash costs a wave | 08:30–10:00 | 1 (pre-flight) |
| 3 | **Raise the 3 weakest categories**: raw material, quality, tenders | Waves are themed: a weak category can cost a whole wave, not one case | 09:00–11:30 | 0 (offline) |
| 4 | **Prove it live once**, all 10 hosted cases, then the changed weak ones | The twin is a guide; only the arena decides | 10:00 and 11:45 | ~16 |
| 5 | **Pitch and replays** that show *why* each offer was made | Places 1–3 are a jury decision from score **and** pitch | 11:30–13:00 | 0 |

Stop rule for every code change: `python scripts/gate.py` must PASS (tests + 6 offline benchmarks + every
category mean), then at least one live session must not be worse. No new features after **12:15**.

---

## 1. How the final is scored, and what follows from it

- **Case score** = clip((C_open − C_deal) / (C_open − C_best), 0, 1). C is *total cost*, not price: spend over the
  horizon, payment days (8 %/yr against 60 days), freight by Incoterm, lateness against our need date, quality
  (ppm vs chargeback), tooling, index clause, uncovered need, relationship charge.
- **No deal** scores the alternative (0 if there is none). **A broken must-have scores 0.**
- **Wave score** = mean of its case scores × 100. **Total** = sum of 5 waves (max 500). Ties: more deals, then fewer rounds.
- **Waves are themed** (theme shown by the host). With 7 categories (renewal, price increase, RFQ, bundling,
  capacity, quality, raw material) a wave is likely one or two categories. **A category at 0.5 instead of 0.7 costs
  20 points in that wave.** That is why §3 works by category, not by case.
- **8-minute hard cap per wave** (the host may extend: read `seconds_left`). A thread still open at the cap scores
  the alternative; an unplayed case scores the alternative. **At most 10 open sessions per key**: a wave with more
  than 10 cases queues.
- **Hidden cases**: new situations, same world and suppliers (the brief allows new suppliers too), some cite the
  Sunday release, some carry their own `facts` (a new letter) in the case file.
- **The practice leaderboard is not the final.** It rewards the best of many tries; the final is one try per case.
  Do not burn quota chasing practice places: spend it on evidence.
- **Top 5 by points, then the jury** picks 1–3 from score and pitch. Replays are open to the jury; they may weigh
  how a score came about (exploiting a bot weakness can count against us). Our rules are real procurement logic —
  say so.

---

## 2. Where we stand (honest numbers)

**Live, all 10 hosted cases, one session each, 08:30, build 7e45c3d: mean 0.683** (team best per case: 0.792).

| Case | Category | Ours | Team best | Gap | Status after T-061 (not yet live) |
|---|---|---|---|---|---|
| P05 | RFQ (3 bidders) | 0.433 | 0.849 | −0.42 | fixed: sign a final only if cheapest standing sheet, cite cheapest rival in round 1 |
| P11 | quality | 0.626 | 0.794 | −0.17 | **open** (ppm change misread as a price step) |
| P08 | raw material | 0.473 | 0.614 | −0.14 | fixed: Floor-chase now cites one fresh fact per mail |
| P04 | renewal | 0.766 | 0.860 | −0.09 | fixed: payment days no longer locked by our own proposal |
| P03 | renewal | 0.845 | 0.935 | −0.09 | fixed: same; supplier cash rate learned from its own sheets |
| P18 | raw material | 0.552 | 0.618 | −0.07 | approach Leverage, paced facts |
| P02 | price increase | 0.735 | 0.800 | −0.07 | relationship charge now priced (live 0.735) |
| P07 | bundling | 0.733 | 0.769 | −0.04 | |
| P10 | capacity | 0.686 | 0.694 | −0.01 | |
| P01 | price increase | 0.982 | 0.992 | −0.01 | |

**Offline (gate, after T-061):** persona twin seeds 0–3 65.4 / 66.5 / 71.7 / 70.8, kit 62.4, calibrated twin 71.8.
**Calibrated twin by category** (the most arena-like benchmark): renewal 1.00, price increase 0.86, bundling 0.69,
capacity 0.68, **RFQ 0.59, quality 0.54, raw material 0.48**. These three match the live gaps exactly.

**What the agent already does well** (keep, don't touch): one tailored approach per supplier from its profile,
history, news, rivals and our cost drivers; total-cost decisions on every sheet; always procures; no timeouts;
relationship cases priced; every cost lever negotiated; regression gate.

---

## 3. Work plan

### W1 — Sunday release and hidden-case readiness (must) · 08:00–09:00 · manager + 1 worker
The code already merges the release (`arena_kit/data.py`, `DataPackage.release`) and inline case facts
(`dossier.py:239-257`). It is **not downloaded** on this machine (`data/` has only `package`).
1. 08:00: `uv run arena data` (downloads `release-sunday.zip`, unpacks next to the package). Check
   `data/release-sunday/facts.json` exists and `DataPackage(...).release` is set.
2. Rebuild profiles: `uv run python -m buyer.profiles build --data data/package --arena "runs/arena-*.json"`, then
   `uv run python -m buyer.profiles check --data data/package`. `check` lists suppliers with new documents.
3. For each supplier with new release news or letters: confirm the profile now carries it and that
   `approach.py` reads it (weakening news → deeper opening and a cited fact; strengthening news → shallower).
4. Test with a synthetic hidden case that carries its own `facts` (a new letter): the dossier sees it, a claim can
   cite its ID, nothing crashes. Add it to `tests/`.
5. Gate PASS, commit, push.
**Done when:** a supplier named in the release shows the new news in its profile and in the approach reason.
**Risk:** profile rebuild may change approach choices: run the gate; if a category drops, check that supplier.

### W2 — Final operations (must) · 08:30–10:00 · manager + 1 worker
1. **LLM budget.** `BUYER_BUDGET_USD=14` with $3.06 spent; about $0.037 per call. A full final (5 waves × ~8 cases ×
   ~6 calls) is ~250 calls ≈ $9 plus profile reads. Set **`BUYER_BUDGET_USD=40`** in the final machine's `.env`
   and start the final with a fresh `runs/spend.json` (rename the old one). Otherwise the template writer takes
   over mid-final.
2. **Wave timing.** Run the 19-case timed simulation that T-060 did not run (`buyer.bench --sim twin --clock -p 10`
   with the template writer) and measure wall time per case. Target: every case closed by **6:00** of the 8:00 cap.
   If not: a per-case time budget — after 5:00 in a wave, Quick-close every open thread at its best standing sheet
   that beats the alternative; cut holding/probing rounds when the wave clock is short. `close_margin_s=90` today.
3. **Throughput.** Cases start most valuable first (T-060). Check `CALLS_PER_SECOND=9` is shared across all threads of
   all cases (the key allows 10/s); 10 open sessions max; a closed case frees its slot at once.
4. **Pre-flight on the final machine** (the brief asks for it): one practice case on that machine and network with
   the exact final `.env`; check the record is written, the strategist answers in < 10 s, no 4xx.
5. **Runbook** (§8) printed and on the desk. One person starts it, one watches logs; nobody types into the agent
   after 13:30 (humans in the loop are banned).
6. **Never call** `/leaderboard`, `/v1/events`, `/screen`, `/results` from the agent (banned). The runner's
   back-office call is skipped in the final (done).
**Done when:** timed simulation closes all 19 cases inside the cap; pre-flight passes on the final machine.

### W3 — Raise the weakest categories (score) · 09:00–11:30 · Opus/Sonnet workers on the API key
All fixes general (by category, supplier history and behaviour, pricing room, our cost drivers), never case numbers.
Each passes the gate on its own before the next starts.

**W3a Raw material (0.48 calibrated; P08, P18, P16)** — biggest lever if a wave is "raw materials".
- Opening: live and team-best sessions opened 16–19 % below on raw material; we open ~13 %. The twin preferred 10 %.
  Rule to test: open deeper when the justified index change and should-cost gap are large (pricing room), never
  past ~19 %; keep today's depth when the supplier's history shows it keeps its ask.
- Index clause: on a falling index path, offer the deadband-0 clause sheet alongside (team best P08 0.614 closed
  at 5.24, 45 days, deadband 0); on a rising path refuse or widen.
- Take the supplier's own alternative sheet when its C is lower (P18 best took 30 days at 5.20 over 90 at 5.30).
- Check the P16 warning (0.96 → 0.87 since Floor-chase cites facts): allow pacing only when the fact is strong.

**W3b Quality (0.54 calibrated; P11, P12)** — open since T-061. *Status 08:40: root cause confirmed; like-terms
reading (`BUYER_LIKE`) and quality step floor (`BUYER_QSTEP`) built, both off by default (replay / gate evidence in
EXPERIMENTS.md 3.2). Needs a 2-session live A/B of `BUYER_QSTEP=1` on P11 to decide.*
- Root cause found: a ppm change (6400 → 300) was read as a 3.6 % price step, so our floor guess sat too low and our
  steps shrank to 0.13. Fix: compare the supplier's moves **on like terms** (same ppm) when judging its step and its
  floor; T-061's first try lost 0.05–0.09 on the twin, so it was reverted — retry with the like-terms value only for
  the step/floor estimate, not for pricing.
- Put the tightest ppm sheet first, with chargeback as the trade (€150/complaint with 300 ppm reached 0.744 in 3 rounds).
- Keep countering while the supplier still steps a fixed amount (its floor shows as final offer / "rock bottom").

**W3c Tenders (0.59 calibrated; P05, P06)** — T-061 fixed the big error; confirm live (W4).
- Award on total cost C across bidders (holds); sign a final only if it is the cheapest standing sheet (new).
- Rival quote cited once, early when a bidder is ≥ 8 % above it (new); keep the other bidders moving with holding
  notes; no bidder starved (timeout guards).

**W3d Bundling and capacity (~0.68–0.69)** — only if time: bundling should price the package across parts
(one combined offer, volume on all parts); capacity should trade binding volume and lead time for price.

**W3e Do not touch:** renewal (1.00 calibrated) and price increase (0.86) beyond the P03/P04 payment fix.

### W4 — Live validation (evidence) · budget ≤ 16 sessions of the team's ~94 left
The quota is shared: tell the team before each batch. Keep the machine idle while it plays.
| Batch | When | Cases | Sessions | Keep the change if |
|---|---|---|---|---|
| L1 | ~10:00 (after W1 + T-061) | all 10 hosted | 10 | mean ≥ 0.683 and P05 clearly up |
| L2 | ~11:45 (after W3) | P05, P08, P11, P18 + 1 changed | 5 | each changed case not worse than L1 |
| Pre-flight | ~12:30 | 1 case on the final machine | 1 | no error, record written |
Single sessions are noisy (LLM suppliers): judge by direction across several cases, plus the gate. If L1 is worse
than 0.683, find the switch: `BUYER_STRAT=0`, then each T-061 rule, on the worst case only.

### W5 — E-mail quality (optional) · 10:00–11:00 · only if W1–W3 are on track
Suppliers decide "like a person from the whole e-mail" (tone, appreciation, clear answers to their questions).
The strategist runs Opus at `LLM_EFFORT=low` (~6–7 s). Possible gain: `medium` effort and a sharper brief (answer
every supplier question first, one argument per paragraph, thank concessions). Risk: slower replies against the
60-second rule (write cap 20 s, then template). Only with a live A/B on 2–3 cases; otherwise skip.

### W6 — Pitch, replays, submission · 11:30–13:00 · manager + a teammate
1. **Explain every move in the record**: per thread the approach, its reason (history, news, pricing room, rivals),
   the lead argument and why each sheet was sent. Much of this is in `strategy_summary`; make sure it is saved in
   `runs/arena-*.json` so replays can be narrated.
2. **README**: how to run (env vars incl. `BUYER_BUDGET_USD`, release step), the design in 10 lines, the switches,
   the gate. `AGENT_STRATEGY.md`: the approach table.
3. **5-minute pitch** (outline):
   1. The problem: procurement is total cost, not price; every supplier is different.
   2. Our agent reads each supplier like a buyer does: its record with us, its news, its letters, how it moves now,
      and the room in its price → one fitting approach, few tactics, one correction.
   3. It never squeezes a strategic supplier into a loss (that costs us more), always secures supply, never times out.
   4. Evidence: live 0.683 → (L1 result), gains by category, the regression gate that stops steps back.
   5. A replay: one negotiation, move by move, with the reason for each offer.
   6. What we learned about suppliers (fixed steps, approval ladders, final offers by round count, facts that move them).
4. **Q&A prep**: "would it work on a real supplier?", "why did it write that?", "what if the supplier lies?",
   "how do you avoid overfitting to the practice cases?" (per-supplier rules from data, leave-one-case-out, gate).
5. **Submission by 13:00**: repo URL, README, commit hash, short description. Keys stay out of the repo.

### W7 — Team coordination · all day
- One branch is the truth: `improve/supplier-levers`; the team merges into `main` (not us). Agree by **12:15** which
  commit plays the final and on which machine.
- One person owns the practice quota calendar (§W4) so two agents don't burn it at once.
- If the teammate LLM-led agent is clearly better on a category, a category router is possible in principle, but
  integrating two agents before 13:00 is high risk: only if it is already one command away.

---

## 4. Timeline (CET)

| Time | What | Owner |
|---|---|---|
| 08:00 | Release public: `arena data`, rebuild + check profiles (W1) | manager |
| 08:15 | Push T-060/T-061 + new gate baseline (pending) | manager |
| 08:30 | W2 budget + timed wave simulation (worker); W1 tests (worker) | workers |
| 09:00 | W3a raw material (Opus worker) → gate | worker |
| 09:45 | W3b quality (Sonnet worker) → gate | worker |
| 10:00 | **L1 live batch** (10 sessions) — machine idle | manager |
| 10:30 | W3c tender check, W3d if time; W5 decision | workers |
| 11:30 | W6 decision log + README + pitch draft | manager + teammate |
| 11:45 | **L2 live batch** (5 sessions) | manager |
| 12:15 | **Feature freeze.** Only fixes for crashes from here | all |
| 12:30 | Final gate, push, pre-flight on the final machine (1 session) | manager |
| 12:50 | Submission form: repo, commit hash, description | team |
| 13:00 | Code freeze | — |
| 13:15 | Start `run --set final` on the final machine; watch, don't touch | one person |
| 13:30 | Waves start | — |
| 15:10 | Pitch (top 5) | team |

---

## 5. Live-session budget

Team quota ~94 left (resets 15:16 CET, after the final). This plan uses at most **16**: L1 10, L2 5, pre-flight 1.
Everything else is offline (gate: 6 benchmarks, ~11 min).

## 6. Go / no-go checklists

**Before pushing any change:** gate PASS · no category drop > 3 points · no extra no-deal · no key in the diff.

**At 12:30 (final build):**
- [ ] Release downloaded on the final machine; profiles rebuilt; `profiles check` clean
- [ ] `.env` on the final machine: `ARENA_URL`, `ARENA_API_KEY`, `ANTHROPIC_API_KEY`, `BUYER_WRITER=claude`,
      `BUYER_BUDGET_USD=40`, `LLM_MODEL=claude-opus-5-5`; fresh `runs/spend.json`; no `BUYER_*` A/B overrides
- [ ] Gate PASS on the final commit; commit pushed; hash noted
- [ ] Pre-flight practice case on that machine: deal, record written, no errors
- [ ] Machine: power plugged, sleep off, nothing else running, stable network
- [ ] Runbook printed; restart procedure tested once (kill, rerun → 409 resumes the session)

## 7. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| LLM budget runs out mid-final | high if unchanged | template e-mails, weaker deals | `BUYER_BUDGET_USD=40`, fresh spend file |
| A case still open at the 8-min cap | medium | alternative score | timed simulation; Quick-close after 5:00 |
| Release facts not used | medium | blind arguments on cited cases | W1 test with a release supplier |
| Last-minute change breaks something | medium | a wave | gate on every change; feature freeze 12:15 |
| Machine stalls (CPU, sleep) | low–medium | sessions time out → 0 | idle machine, no tests while playing |
| Network / arena 5xx | low | delays | client retries; restart resumes via 409 |
| Practice quota exhausted by the team | medium | no evidence | quota owner, ≤ 16 sessions here |
| Overfitting to the 10 practice cases | medium | weak on hidden cases | per-supplier/category rules only, calibrated twin, gate per category |
| Jury sees "exploits" | low | pitch | rules are procurement logic; log reasons |

## 8. Final runbook (on the final machine)

```bash
cd starter-kit
git pull && git log -1 --oneline            # the agreed final commit
uv sync --all-extras
uv run arena data                           # package + release-sunday
uv run python -m buyer.profiles check --data data/package
mv runs/spend.json runs/spend-practice.json # fresh LLM budget
uv run python scripts/gate.py               # optional if the commit already passed
uv run python -m buyer.runner run --set final --writer claude    # start 13:15, before 13:30
```
If it crashes: run the last command again; open sessions resume (409 names the running session).

## 9. What not to do
- Don't tune to one practice case's numbers. Don't chase the practice leaderboard.
- Don't run tests, benchmarks or a second agent on the machine while it plays.
- Don't change code after 12:15 except to fix a crash; don't touch anything after 13:00.
- Don't call banned endpoints; don't type into the agent after 13:30.

## 10. Decisions needed from the team
1. Which machine plays the final, and who owns it from 12:30.
2. The final commit (default: the last gate-passing commit on `improve/supplier-levers` at 12:30).
3. The practice-session budget for L1/L2 (16 proposed).
4. W5 (e-mail effort) yes/no.
