# Hollerau buyer agent

An AI buyer for the Tacto Negotiation Arena. Code prices every offer; Claude Opus chooses among the code's offers,
reads the supplier and writes the e-mail.

```
case file + data package (+ Sunday release)
   ▼
dossier.py    the supplier read from the data: traits from notes, letters, news and quote remarks, past deals,
              should-cost and margin, scorecard, payment record, forecast, trust
   ▼
strategy.py   every combination of term options priced at the supplier's floor and at our cheapest (total cost C);
              equal-cost packages; a concession path of visible steps inside our walk-away
   ▼
claims.py     data arguments the arena checks (figure next to ID), chosen per supplier
   ▼
writer.py     Opus strategist picks sheets and arguments from the code's menu and writes the e-mail; a guard checks
              every price and ID (template writer as fallback)
   ▼
agent.py      per thread: read the reply, update the model, accept, counter, hold or walk
runner.py     one worker per supplier thread inside the 60 s rule; the Live Final in waves
```

Every one of the 46 suppliers in the data has a prebuilt profile ([`starter-kit/profiles/INDEX.md`](starter-kit/profiles/INDEX.md)):
the agent loads it instead of reading documents under the clock and picks one approach per supplier (Leverage,
Floor-chase or Quick-close). Trusted suppliers get no risky arguments; after a final offer only its own terms go back.

## Run

```bash
cd starter-kit
uv sync --all-extras
cp .env.example .env    # ARENA_URL, ARENA_API_KEY, ANTHROPIC_API_KEY, BUYER_BUDGET_USD (40 for the final)
uv run arena data       # the data package and the Sunday release into data/
uv run arena me         # the key works
```

Live Final (start before 13:30; it waits for wave 1, plays each wave's cases in parallel and stops after wave 5):

```bash
caffeinate -dimsu uv run python -m buyer.runner run --set final --writer claude -v 2>&1 | tee runs/final.log
```

Practice and offline checks:

```bash
uv run python -m buyer.runner play case:P01 --writer claude     # one practice session
uv run python scripts/gate.py                                    # tests + six offline benchmarks
```

LLM spend is tracked in `runs/spend.json`; calls stop at `BUYER_BUDGET_USD` and the template writer takes over.

## Results (25 Oct, hosted practice board before the Live Final)

Team **RGV GmbH**, best practice session per case at 11:30 on 25 Oct: **mean 0.806, 6th of 13 teams**. Tacto's
reference agent scores 0.697 on the same cases; we are above it on 7 of 10. Our best deals save about **EUR 1.2M**
against the opening offers.

Two agents set these numbers: the production agent (`buyer/`, this repo) and a research agent where Claude leads
every move (`negotiation-lab/llm_v4.py`, **not in this repo**), which set 7 of the 10 team bests. Its mechanics are
partly ported to `buyer/`.

| Case | Situation | Team best | Set by | This branch's best | Tacto reference | Best of any team | Saved vs opening |
|---|---|---|---|---|---|---|---|
| P01 | +9.5 % price letter, volute casings | 0.998 | research agent | 0.992 | 0.966 | 1.000 | EUR 56,857 |
| P02 | copper pass-through, 7.5 kW motor | 0.816 | research agent | 0.724 | 0.740 | 0.875 | EUR 38,643 |
| P03 | frame renewal, frequency converter | 0.936 | research agent | 0.919 | 0.925 | 0.963 | EUR 315,698 |
| P04 | renewal, ball bearing 6306 | 0.860 | both (tie) | 0.860 | 0.860 | 0.874 | EUR 1,994 |
| P05 | RFQ with 3 bidders, pump shaft | 0.852 | `buyer/` | 0.814 | 0.555 | 0.924 | EUR 69,697 |
| P07 | bundling, fastener kit | 0.776 | research agent | 0.739 | 0.782 | 0.805 | EUR 71,199 |
| P08 | alloy surcharge, 1.4404 bar | 0.614 | research agent | 0.462 | 0.752 | 0.904 | EUR 107,776 |
| P10 | capacity cut, seals | 0.694 | `buyer/` | 0.679 | 0.686 | 0.718 | EUR 418,951 |
| P11 | quality, bearing brackets | 0.877 | research agent | 0.794 | 0.314 | 0.877 | EUR 83,396 |
| P18 | alloy surcharge, 1.4404 bar (Werdenfels) | 0.641 | research agent | 0.497 | 0.386 | 0.767 | EUR 46,475 |
| **Mean** | | **0.806** | | **0.748** | **0.697** | | **EUR 1.21M** |

"This branch's best" is the best live session played from `improve/supplier-levers`. With each case's own history
wiped ("fresh mode"), the research agent scored 0.747 against 0.787 with history, over 9 single-supplier cases.

What changed on 25 Oct, each checked in live practice sessions ([`EXPERIMENTS.md`](EXPERIMENTS.md) has the details
and the rejected ideas):
- **Tenders:** a bidder far above the cheapest rival hears that rival's quote in our first e-mail (P05: 36.00 ->
  30.50 -> 28.00, best 0.814); while a bidder checks its price internally we resend our exact offer (0.744 twice);
  a sheet within 1 % of our next offer is taken, not countered.
- **P02** copper clause at its cheapest price for us (0.63 -> 0.724); **P11** no 5 % cap on quality openings (0.794).
- **Final-day runner:** wave deadlines re-read every poll, banned endpoints refused, budget warnings.

Every change passes `scripts/gate.py` (tests plus four persona-twin seeds, the kit simulator and a twin calibrated
on real sessions; no case category may drop). The 24 Oct results and fixes are in [`HISTORY.md`](HISTORY.md).

## Not yet done

- Tenders: no counter step below the supplier's 0.5 % counter threshold once it has given the rest of its step.
- The runner logs no line for a send that arrives too late (HTTP 409).
- Relationship-goal cases with only a generic cost estimate (e.g. P06) are not checked live.
