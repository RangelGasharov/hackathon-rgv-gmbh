# Supplier strategy and coordination handoff

Branch: `improve/supplier-levers`

## Goal

Use a supplier's cached profile and past negotiation behavior to choose a focused opening approach, then adapt to the supplier's actual replies. Keep the offer costed by the existing planner, preserve the 60-second move window and 10-message thread cap, and coordinate parallel supplier threads as one procurement.

## Strategy model

- `BuyerAgent.start` loads usable profiles before creating each `ThreadState`; a missing or stale profile uses the existing bounded live-read fallback.
- `Planner` chooses one primary policy and one fallback in `profile_strategy`: `fast_close` for high walk risk, a close opening, or relevant arena evidence; otherwise `reciprocal`.
- The profile prior can be revised once from observed behavior. A meaningful supplier concession can move `fast_close` to `reciprocal`; two replies with negligible movement or a take-it-or-leave-it offer can move `reciprocal` to `fast_close`.
- The profile affects the first anchor and subsequent concession pace. In the last two available messages, pricing accelerates toward convergence without introducing a third policy.
- The strategist receives the active plan in the dossier brief and is instructed to follow it. Code still prices every sheet and enforces the procurement reservation; the writer may choose only from that menu.
- The bargaining model is finite-horizon and reciprocal: treat profile and arena history as the prior, update from observed concessions or resistance, preserve the outside option as the hard ceiling, and use the final available message for a decision.

## Shared procurement behavior

- Supplier threads run as workers under one `BuyerAgent`; decisions share the live best offer, reservation ceiling, and rival-price evidence.
- When an accept or walk action closes the case, the runner sets its shared completion event. Other workers stop, and a slow sibling draft checks that event before sending.
- The 10-message cap includes holding messages. The first offer uses cached local profile data; only suppliers without a usable profile incur the existing timed live read.

## Files touched for this work

- `starter-kit/buyer/strategy.py` selects and adapts the two-policy plan and uses it in offer pricing.
- `starter-kit/buyer/agent.py` records the selected plan and logs a fallback switch.
- `starter-kit/buyer/dossier.py` includes the active plan in the strategist's context.
- `starter-kit/buyer/writer.py` tells the strategist to follow the assigned plan and priced menu.
- `starter-kit/buyer/runner.py` stops sibling drafts after a case result.
- `buyer_agent_v5_flow_updated.svg` and `.png` show the updated process; the root `README.md` summarizes the behavior.

## Graphify context

The generated Graphify report and graph in `starter-kit/graphify-out/` were used to trace `BuyerAgent` → `ThreadState` → `Planner`, plus `play()` → `worker()` → `finish()`. The Graphify CLI is not available on this machine, so the checked-in graph snapshot was queried directly; its commit predates the profile additions and should be regenerated with Graphify when its runner is available.

## Validation status

The updated SVG was parsed and rendered to a 2720×1920 PNG. Project tests were not run for this change.

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
