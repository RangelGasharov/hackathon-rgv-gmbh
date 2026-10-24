# Supplier strategy and coordination handoff

Branch: `improve/supplier-levers`

## Goal

Use a supplier's cached profile and past negotiation behavior to choose a focused opening approach, then adapt to the supplier's actual replies. Keep the offer costed by the existing planner, preserve the 60-second move window and 10-message thread cap, and coordinate parallel supplier threads as one procurement.

## Strategy model

- `BuyerAgent.start` loads usable profiles before creating each `ThreadState`; a missing or stale profile uses the existing bounded live-read fallback. The Live Final's new suppliers have no profile, so there the plan is always `reciprocal` with no profile evidence.
- `Planner` chooses one primary policy and one fallback in `profile_strategy`: `fast_close` when the persona read flags high walk risk or a close opening, or the arena record shows 20 %+ walks over 5+ sessions; otherwise `reciprocal`. How often a supplier agreed to our round-1 offer is not a cue: that means our opening was above its first counter (we overpaid).
- The plan can switch once from observed behavior: a one-reply concession of 4 %+ of its opening moves `fast_close` to `reciprocal`; two replies in a row with negligible movement (a raised ask does not count) move `reciprocal` to `fast_close`. Never on a final offer, and not while it said it will come back on price.
- Pricing effect: only one. A `reciprocal` supplier whose arena record shows a first counter at 92 % of its opening or lower, with 15 % walks or fewer, opens up to 2 points deeper (the case's protective caps still apply). `fast_close` changes no price: neither the opening nor the step size. Code prices every sheet either way, so the plan tells the strategist how to write (tone, which package to lead with), not what to offer.
- The strategist receives the active plan in the dossier brief, with what each approach means. Code still prices every sheet and enforces the procurement reservation; the writer may choose only from that menu.

## Shared procurement behavior

- Supplier threads run as workers under one `BuyerAgent`; decisions share the live best offer, reservation ceiling, and rival-price evidence.
- An accept or walk closes the case inside the shared lock and sets the runner's completion event. Other workers stop, and a sibling's counter draft that was already being written is dropped instead of sent.
- The 10-message cap includes holding messages. Our 10th message may still be a counter: the supplier answers it, and after the round limit we may still accept (api.md, 409).
- The first offer uses cached local profile data; only suppliers without a usable profile incur the existing timed live read.

## Files touched for this work

- `starter-kit/buyer/profiles.py` and `starter-kit/profiles/` hold the 46 supplier profiles and their builder/checker.
- `starter-kit/buyer/strategy.py` selects and adapts the two-policy plan, uses it in offer pricing, and adds `probe` (two offers just under the walk-away when every package looks dearer than it, first two offers only).
- `starter-kit/buyer/agent.py` loads profiles, records the selected plan, logs a fallback switch, caps rounds at 10, and calls `probe`.
- `starter-kit/buyer/dossier.py` includes the profile brief and the active plan in the strategist's context.
- `starter-kit/buyer/writer.py` tells the strategist to follow the assigned plan and priced menu.
- `starter-kit/buyer/runner.py` drops sibling drafts after a case result.
- `buyer_agent_v5_flow_updated.svg` and `.png` show the updated process; the root `README.md` summarizes the behavior.

## Audit (25 Oct, before the P18 rerun)

The first version of this branch was reviewed line by line. Fixed:

- **Round-1 agreements chose `fast_close`** (P02's 70510). Those agreements show our opening was too generous, so the cue pointed the wrong way. Removed.
- **`fast_close` capped the opening gap at 4 %** (P10 opened at 30.08 instead of 29.03, P05's 70240 likewise). The arena's first counter does not depend on our opening, an opening above it is agreed at once, and none of the real walks followed an opening except P10's -19 % to a capacity-tight supplier (already capped at 10 %). Removed: `fast_close` keeps the case-calibrated opening.
- **`fast_close` lifted the step cap from 1 % to 1.5 %** for RFQ bidders and committee suppliers, undoing the calibrated committee guard (P02 b1). On the persona twin it was part of the P10 loss (0.35 -> 0.20; removing only the anchor cap recovered 0.26). Removed, together with the same lift for every thread with two messages left.
- **"Reciprocate faster near the horizon" was a no-op.** `nxt = ask - 0.6/0.4 * (ask - lim)` never changed `aim = min(lim - 0.003 v0, nxt - 0.002 v0)`, which always resolves to the first term (also checked on 1,500 random states). Restored the original line.
- **The deeper-opening bonus was added after the protective caps** (committee, capacity-tight, cash-short, quality) and could undo them. It is now applied before them.
- **A raised ask counted as "negligible movement"** and could switch to `fast_close` while the supplier moved away. Now only small moves in either direction count.
- **One message before the cap was forced to settle**, on the false premise that a 10th counter could not be answered. The arena answers it and still allows an accept after the limit. Reverted to deciding at the cap; the test now checks that.
- **`probe` skipped the guards of `sheets`**: it could offer more than the supplier asks for a package it values more, or a lower price than our last offer on the same terms. It now applies both.
- **The 70740 persona recommended citing 70710's €5.45 for the same part.** The P18 probe that did exactly that came back "irrelevant" and closed at €5.70 (0.336). Moved to "avoid"; the volume answer that reached €5.40 at 90 days (0.532) is now its tactic.
- The plan text tells the strategist what each approach means, so "fast close" is not read as licence for pressure or deadlines.
- The strategy layer had no tests; `tests/test_profiles.py` now covers cue selection, the unchanged fast-close opening, the deeper opening with room, at most one switch, and no switch on a final offer.
- Three files the branch added were left unformatted; `ruff format` is clean again for the touched files.

Checked and kept: the runner's completion-event checks (accept/walk still close inside the lock; only counter drafts are dropped, which also happens after a non-409/422 send error, as before), `round_cap` (no effect while every case has `round_limit` 10), the profile loader and its live-read fallback.

Open doubts, not changed: `ts.sent` does not count arena timeouts, which use a round (predates this branch); timeout events that repeat the supplier's offer feed `observe_reply` and can look like a stalled reply.

## Graphify context

The generated Graphify report and graph are local only (`graphify-out/` is in `.git/info/exclude`, not in the repository). The graph on this machine was built before the profile and strategy changes.

## Validation status

- `uv run pytest`: 125 passed, 3 skipped (the branch had 120; the new tests cover the strategy layer).
- Persona twin, 19 cases (`uv run python -m buyer.bench --sim twin --agents buyer --cases-dir cases/real --seed N`), mean x 100 for seeds 0 / 1 / 2: `main` 68.1 / 69.6 / 67.8; this branch before the audit 66.9 / 67.1 / 66.1 (P10 and P05's 70240, both `fast_close`); after the audit 68.0 / 69.5 / 67.7. The remaining 0.01-0.02 on P05 comes from the profile trait reads, not the plan (same with the deeper-opening bonus off).
- Calibrated twin (`uv run python -m buyer.twin_real bench`, 10 hosted cases): mean 0.731, identical per case to `main`. Its fitted limits make it insensitive to the opening, so it could not show the anchor regression.
- `uv run python -m buyer.profiles check --data data/package`: 46 suppliers, 46 clean.

## P18 live run: diagnosis for the next agent

One live P18 practice session was run on 24 Oct 2026. Its saved local record is
`starter-kit/runs/play-1792876339.json`; the full arena transcript is in
`starter-kit/runs/arena-P18-1792876339.json` (session `f5fe76620163ed5a`, replay path `/replay/f5fe76620163ed5a`).
Do not replay the case just to recreate this transcript; the session already consumed quota.

- Result: accepted `sup:70740`'s final €5.50/unit sheet, with 60-day payment, 36 months, and its AS-14404 clause.
- Score: 0.443; `c_open` €295,173.23, `c_deal` €263,046.30, `c_best` €222,651.27. It saved €32,126.93 from opening but remained €40,395.03 above the benchmark best.
- History in the profile: 11 sessions, one walk, one round-one agreement, first counter median 93.4% of opening, final offer median round 4; expected deal range €5.40–€5.70.
- The policy selected reciprocal bargaining. The buyer opened with €5.27/14d/36mo and €5.32/60d/24mo. Supplier replies held the 36-month price at €5.50 while improving payment from 14 to 60 days; buyer later held at €5.41. The supplier's final €5.50/60d/36mo offer arrived in round 5 and was accepted unchanged.
- Interpretation: acceptance respected the final-offer rule, and the result fell inside the profile's historic price range. The score still shows a meaningful optimization gap; diagnose value tradeoffs against `c_best` and the transcript before changing the anchor or trying to squeeze a supplier after its final offer.
- Code correction after this run: do not mark a policy fallback switch on a `final_offer`. There is no next bargaining move where the new policy can affect the outcome, and the final decision belongs to the reservation/outside-option logic. The strategy summary now names only actionable switch triggers.

## Takeover plan

1. **Preserve the game boundary.** Read the case, supplier profile, Arena thread, and offer history before the first move. The first priced offer must be ready within 60 seconds. Cap each supplier thread at 10 buyer messages, including holding messages.
2. **Choose one primary and one fallback.** Record the profile evidence that selected them. Stay with the primary until an observable price/value concession or repeated resistance reaches the explicit trigger; switch at most once. Never create a third strategy mid-thread.
3. **Price every action in code.** Build a small menu of equal-cost packages using the known cost model. Trade only supplier-valued terms (volume, term, payment, clause) for buyer value; keep the reservation/outside option as a hard ceiling. Do not let the language model invent prices, terms, or strategy switches.
4. **Learn from the whole offer.** Compare supplier sheets using supplier-value/cash-flow effects across price and terms, not price alone. Count concessions in either direction, avoid giving multiple buyer concessions while the supplier repeats the same total value, and never send the same menu twice.
5. **Handle the endgame as a finite game.** Increase reciprocal pace only while another response can arrive within the remaining horizon. On a final offer, do not edit its terms or claim a newly activated tactic; accept only if it beats the procurement reservation and live alternative, otherwise walk or continue a still-live rival thread.
6. **Coordinate parallel threads as one procurement.** Keep the best live offer and reservation shared. If one thread closes a deal, publish the shared completion event; sibling threads discard drafts and send nothing else. Wait only when a still-live alternative could beat the standing deal.
7. **Review the evidence before tuning.** Start with the saved P18 transcript and `agent_log`; compare the exact deal against the score formula, profile range, and same-part alternatives. Use replay evidence to isolate the missed value (price, payment, term, clause, or rival quote) before adjusting policy thresholds.
8. **Close the branch cleanly.** Regenerate Graphify after code changes if its CLI is available; otherwise query the checked-in graph and note that it predates these changes. Review the diff and whitespace, keep keys out of files and commits, commit on `improve/supplier-levers`, then push that branch. Do not spend another live Arena session without explicit user authorization.
