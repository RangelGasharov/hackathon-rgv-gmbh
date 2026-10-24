# Supplier profiles

The arena's cases only negotiate with the **46 suppliers the data package describes in full**. Each one has a
profile here, built once, so the agent never spends the 60-second clock reading documents.

**Start with [INDEX.md](INDEX.md)**: one line per supplier (category, owner, how much of its turnover we are,
key traits, opening, walk risk, arena record), each linking to its full profile.

## Files

| File | For | What |
|---|---|---|
| `INDEX.md` | people, agents | every supplier on one page, links to the profiles |
| `sup_<no>.md` | people, agents | one supplier: summary, playbook, persona with evidence, facts, arena record, documents |
| `sup_<no>.json` | the buyer agent | the same as data (schema below) |
| `persona/sup_<no>.json` | the builder | the deep read of the supplier's documents: the only part not computed from data |
| `manifest.json` | the builder | what each profile was built from, to see what changed |

Supplier `sup:70110` lives in `sup_70110.*`. `python -m buyer.profiles facts sup:70110` prints what a reader gets.

## `sup_<no>.json`

| Key | Content |
|---|---|
| `id`, `name`, `country`, `category`, `abc_class` | who it is |
| `company` | owner type, group, staff, turnover, equity ratio, credit index now and 12 months ago (higher is worse) |
| `our_weight` | our spend by year, our share of its turnover, spend trend |
| `standard_terms` | its payment days, cash discount, incoterm, lead time |
| `parts[]` | per part: current price and since when, should-cost (and whether it models this supplier's plant), other approved sources |
| `contract` | framework agreement: validity, payment, notice days, clauses (index clauses with series and share) |
| `negotiations` | past price events (asked vs settled %), averages, share of its ask it usually keeps |
| `payments`, `quality` | how late we pay it; scorecards, complaints, open 8D |
| `news`, `documents[]`, `quote_remarks` | everything it wrote or we noted, with ids |
| `persona` | the deep read: `summary`, `traits` (strong/weak/none, with `evidence` doc ids and quotes), `wants`, `fears`, `hedge_until`, `playbook` |
| `persona.playbook` | `opening` (deep/moderate/close), `walk_risk`, `levers`, `arguments`, `avoid`, `mandatory_terms`, `tactics`, `expected_concession` |
| `arena` | our practice sessions with it: cases, best score, first counter vs opening, final-offer round, walk-aways, round-1 agreements, "fair point" and "does not change" replies |
| `source_hash`, `persona_stale`, `problems` | what the read was based on, whether the documents changed since, anything the checks found |

## How the agent uses them

- `BuyerAgent.start` loads the profile of every supplier in the case and merges `persona` over the regex traits
  (`Dossier.apply_read`). Only a supplier **without a usable profile** gets the live persona read (15 s at most).
- The traits drive the numbers (cost of money, minimum margin, opening gap, which arguments go out).
  `playbook.opening` = `close` or `walk_risk` = `high` caps the opening gap at 4 %.
- The strategist (Opus) gets the compact playbook (`profiles.brief`) instead of the raw notes.
- `BUYER_PROFILES=0` plays without profiles, for an A/B on the twin.

## Building and updating

```bash
cd starter-kit
uv run python -m buyer.profiles build --data data/package --arena "runs/arena-*.json"   # facts + arena evidence
uv run python -m buyer.profiles check --data data/package                               # exit 1 on any problem
```

`build` recomputes every fact and rewrites all files; it never touches `persona/`. A persona read is checked
against the data: unknown traits or levels, a `strong` trait without evidence, or evidence citing a document the
supplier does not have are reported in `problems`. When a supplier's documents change (the Sunday 08:00 release),
its `source_hash` changes, the profile is marked `persona_stale` and **not used** until its persona is read again;
the agent falls back to the live read for that supplier meanwhile.

**Sunday routine:** `uv run arena data` → `build` → `check` lists the suppliers with new documents → read only
those again (same spec as the first read) → `build` → tests.
