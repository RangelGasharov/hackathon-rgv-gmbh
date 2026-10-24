"""The numbers: which package to offer, at what price, and when to accept or walk.

The lever space of a case is a small grid (every combination of `term_options`). For each
combination we estimate the lowest price the supplier would sign (its full cost and minimum margin,
valued at its own cost of money with `cost.supplier_value`) and the price that minimises C for us
(in strategic and bottleneck cases the relationship charge makes a squeeze expensive, so that can
sit above the supplier's floor). We offer the cheapest packages first, as equal-cost alternatives
(MESO), and move up a concession path in supplier-value terms so that every message is a move the
supplier can see. Acceptance follows AC_next: take what is at least as good as our own next offer.
"""

import itertools
import math
from functools import cached_property
import statistics

from arena_kit.cost import RELATIONSHIP_GOALS, _soft, _years, closing_terms, must_haves_ok, supplier_value, total_cost

from . import flags
from .dossier import NEWS_SITUATION

PRICE = "unit_price"
# the arena supplier's own rule (back office, 294 decisions in 83 sessions): it counters by one fixed increment when our
# step is >= 0.5 % of its opening (P(counter) 0.87 vs 0.54) and its price stays >= 2 % above our best sheet (P(hold)
# 0.06 vs 0.52); a bigger step buys no bigger counter, and its final offer comes after a set number of rounds
LEAN_STEP, LEAN_GAP = 0.007, 0.022
CLAUSE_VALUE = 0.015  # share of the price an index clause is worth to a supplier that wants one (arena P02: >= 1.6 %)
# Our belief about a supplier's full cost, for the relationship charge: one sigma = SIGMA_K x the error band of the
# estimate (generic should-cost 15 % -> 7.5 %), and SIGMA_OWN for the should-cost of its own plant (the charges of 17
# real deals in P01, P02 and P03 put the full cost at 0.977-0.979 x it, to 0.1 %). Five equal-weight quintile nodes.
SIGMA_K = 0.5
SIGMA_OWN = 0.02
Z5 = (-1.2816, -0.5244, 0.0, 0.5244, 1.2816)  # medians of the five quintiles of a standard normal


class Estimate:
    """Our model of one supplier: what it costs, what money is worth to it, what it needs."""

    def __init__(self, dossier, standard=None):
        self.d = dossier
        self.full_cost, self.err = dossier.full_cost
        self.rate = dossier.cash_rate
        self.min_margin = dossier.min_margin
        self.standard = dict(dossier.standard_guess)
        self.set_standard(standard)
        self.quote_price = None  # the unit price of its opening quote: the base of its lead-time premium
        # a lead time shorter than its standard costs a capacity-tight plant overtime: ~0.5 % of its quote per day
        # (arena P10 0.49 %, P05 70240 0.56-0.67 %). Suppliers with idle capacity were never seen to charge for it:
        # theirs starts at 0 and is learnt from the price gap between two sheets of one reply that differ in lead time
        self.lead_prem = 0.005 if dossier.has("capacity_tight") or dossier.case.get("type") == "capacity" else 0.0
        self.lead_prem_learnt = False
        self.cap = None  # the lowest supplier value it has shown it will sign (from its own offers)
        # it wants an index clause when its own sheets carry one, its letter proposes one (arena P08, P16, P18:
        # "a price adjustment clause based on AS-14404"), it names the clause's series as its cost driver, or the
        # persona read says so (arena P02: no clause in its sheets, yet it agreed at once to a deadband clause on
        # the copper it blamed, 2.5 % below its clause-free counter - scores 0.67-0.72 vs 0.26-0.46)
        self.wants_clause = bool(dossier.has("wants_clause")) or "index_clause" in dossier.wants
        self.open_value = None  # supplier value of its opening quote (set by the planner)
        # the relationship charge always counts, as an expectation over our belief about its full cost (`f_nodes`):
        # a flat 10 % weight for an uncertain estimate let P06 take a first offer 5 % under the supplier's full cost
        # (twin P06: charge 101k of 1.29M, score 0), and a weight of 1 on a point estimate pays dearly for a small error
        self.rel_weight = 1.0

    def f_nodes(self):
        """[(weight, full cost)]: what we believe the supplier's full cost is. Around the estimate with the sigma
        above; and never above what its opening quote allows: in every hosted relationship case it quoted at or
        above the sustainable margin (opening x (1 - sustainable margin) was 0.6-3.5 % over its full cost in P01,
        P02, P03; 22 % in P10), so a belief above that is not credible. Only a cap: how far above the margin it
        quotes varies, so the quote does not say where the cost is."""
        key = (self.full_cost, self.err, self.open_value)
        if getattr(self, "_nodes_key", None) != key:
            sigma = SIGMA_OWN if self.err <= 0.05 else SIGMA_K * self.err
            nodes = [(1 / len(Z5), self.full_cost * max(0.3, 1 + sigma * z)) for z in Z5]
            sust = self.d.case["buyer"].get("sustainable_margin_pct")
            if self.open_value and sust:
                cap = self.open_value * (1 - sust / 100)
                nodes = [(w, min(f, cap)) for w, f in nodes]
            self._nodes_key, self._nodes = key, nodes
        return self._nodes

    @cached_property
    def clause_driven(self):
        """A clause is on offer and the part's cost is driven by its series: a supplier exposed to that series may
        value the cover (arena P02, copper, a 12-month term nobody asked a clause for: it took the deadband-5 sheet
        at once, 1.6-3 % under the clause-free price it held to; the clause-free deals sat at 0.26-0.46, the
        clause deals at 0.67-0.72). Whether this one does, its pick between a sheet with and without shows."""
        case = self.d.case
        if "index_clause" not in case["negotiable"]:
            return False
        series = {o["series"] for o in case.get("term_options", {}).get("index_clause") or [] if o}
        return any(sr in series and w >= 0.1 for sr, w, *_ in self.d.drivers)

    def _margin_tight(self):
        b = self.d.case["buyer"]
        ref, sust = self.d.reference_price, b.get("sustainable_margin_pct")
        if self.err > 0.05 or not ref or sust is None or not b.get("relationship_eur_per_point"):
            return False
        return (ref - self.full_cost) / ref * 100 - sust < 8

    def learn_lead_premium(self, sheets):
        """Two sheets of one reply that differ only in lead time and payment are worth the same to the supplier
        (its MESO answer): the price gap is the premium it puts on the shorter lead time (arena P10: 33.6 @ 42 days
        and 31.7 @ 54 days). True if the estimate changed."""
        std_lead, std_pay = self.standard.get("lead_time_days"), self.standard.get("payment_days")
        base = self.quote_price
        if not (std_lead and std_pay and base):
            return False
        value = lambda t: t[PRICE] * (1 - self.rate * ((t.get("payment_days") or std_pay) - std_pay) / 360)  # noqa: E731
        found = []
        for a, b in itertools.combinations([t for t in sheets if t.get(PRICE)], 2):
            if any(a.get(f) != b.get(f) for f in a if f not in (PRICE, "payment_days", "lead_time_days")):
                continue
            la, lb = a.get("lead_time_days"), b.get("lead_time_days")
            if la is None or lb is None or la == lb:
                continue
            if la > lb:
                a, b, la, lb = b, a, lb, la  # a is the shorter lead time
            days = max(0, std_lead - la) - max(0, std_lead - lb)  # days of lead time a buys below the standard
            if days > 0:
                found.append((value(a) - value(b)) / days / base)
        if not found:
            return False
        prem = round(min(0.02, max(0.0, statistics.median(found))), 5)
        changed = abs(prem - self.lead_prem) > 0.0005
        self.lead_prem, self.lead_prem_learnt = prem, True
        return changed

    def tight_capacity_seen(self):
        """Its own e-mail reveals a plant that allocates capacity (the dossier flipped to capacity_tight): the prior
        premium applies, unless its price gaps already told us better."""
        if not self.lead_prem_learnt and not self.lead_prem:
            self.lead_prem = 0.005
            return True
        return False

    def set_standard(self, std):
        if std:
            self.standard.update({k: v for k, v in std.items() if v is not None})

    def learn_cost(self, unit_cost):
        """A supplier's own cost breakdown is evidence, not truth: blend it with our estimate and keep
        it within the error band of our should-cost."""
        if not unit_cost or unit_cost <= 0:
            return
        lo, hi = self.full_cost * (1 - 2 * self.err), self.full_cost * (1 + 2 * self.err)
        bounded = min(max(unit_cost, lo), hi)
        w = 0.5 if self.err <= 0.05 else 0.7  # trust it more when our own estimate is rough
        self.full_cost = round(w * bounded + (1 - w) * self.full_cost, 2)
        self.err = max(0.03, self.err * 0.6)

    @property
    def floor_value(self):
        v = self.full_cost / (1 - self.min_margin)
        return min(v, self.cap) if self.cap else v

    def extra(self, t, cash=False):
        """€ per unit of non-cash value a package has for this supplier (its persona), our guess. With `cash`, only
        what the arena counts in its margin: a faster delivery and a ppm promise cost it real money, but a long
        contract and an index clause are security, not cash (cost.py) - they win its signature and add nothing to
        its margin. Crediting them hid the relationship charge: the agent priced it at 14.5k and 7.9k for the P02
        deals with a clause where the arena charged 27.7k and 18.1k, and at 0 for the P03 deals of 24 and 36 months
        where it charged 0.3k and 1.0k; without the credit it is 31.1k, 20.9k, 0.4k and 1.0k."""
        p, d, x = self.d.reference_price or 1.0, self.d, 0.0
        months = t.get("contract_months") or 0
        if not cash and months >= 24 and (d.has("free_capacity") or d.has("small_account") or d.has("price_frozen")):
            x += 0.008 * p
        std_lead = self.standard.get("lead_time_days")
        if std_lead and t.get("lead_time_days") and t["lead_time_days"] < std_lead:
            # arena: 0.49 % (P10) to 0.56-0.67 % (P05) of the supplier's own quote price per day faster
            x -= min(0.12, self.lead_prem * (std_lead - t["lead_time_days"])) * (self.quote_price or p)
        if not cash and d.has("capacity_tight") and months >= 24:
            x += 0.01 * p
        if not cash and t.get("index_clause") and self.wants_clause:
            x += CLAUSE_VALUE * p  # raw-material distributors and copper-driven suppliers trade price for it
        # halved after the twin refit on 23 sessions: its fitted values give a long term and a clause little,
        # and our larger guesses made its counters look richer, so we offered more (P08)
        if not cash:
            if months >= 36:
                x += 0.01 * p
            elif months >= 24:
                x += 0.005 * p
        real = (d.scorecard or {}).get("ppm") or 0
        commit = t.get("ppm_commitment")
        if commit is not None and real and commit < real:
            x -= 0.03 * p * (1 - commit / real)  # promising better quality than it achieves is a risk to it
        return x

    def model(self, case, t, cash=False):
        std = {**self.standard, **case.get("fixed_terms", {})}
        out = {"standard": std, "interest_rate": self.rate, "full_cost": self.full_cost, "extra": self.extra(t, cash)}
        if ppm := (self.d.scorecard or {}).get("ppm"):
            out["ppm"] = ppm  # it pays chargebacks at the defect rate it really has
        return out

    def value(self, case, terms):
        t = closing_terms(case, terms, self.standard)
        return supplier_value(case, t, self.model(case, t))

    def price_for_value(self, case, terms, target):
        """The unit price at which `terms` are worth `target` to the supplier (value is linear in price)."""
        v1 = self.value(case, {**terms, PRICE: 100.0})
        v2 = self.value(case, {**terms, PRICE: 200.0})
        slope = (v2 - v1) / 100.0
        return max(0.01, 100.0 + (target - v1) / slope) if slope > 0 else None


def grid(case, std, strict=False):
    """Every combination of the negotiable non-price fields allowed by the case. A supplier's own standard value is
    allowed too (api.md: "a value from term_options, or the supplier's standard value") unless `strict`: the
    arena refused one."""
    fields = [f for f in case["negotiable"] if f != PRICE]
    opts = []
    for f in fields:
        o = list(case.get("term_options", {}).get(f) or [])
        if not o:
            o = [std.get(f)] if std.get(f) is not None else [closing_terms(case, {}, std).get(f)]
        elif not strict and f in std and std[f] not in o and isinstance(std[f], int | float):
            o.append(std[f])  # a term sheet may carry the supplier's standard value (arena: P10 lead time 54)
        opts.append(o)
    for combo in itertools.product(*opts):
        yield dict(zip(fields, combo, strict=True))


def rel_charge(case, terms, est):
    """The relationship charge in €: what the arena charges for every margin point the deal leaves the supplier
    below the sustainable margin (`cost.relationship_cost`), as an expectation over our belief about its full cost
    (`Estimate.f_nodes`) and on its cash margin only (`Estimate.extra`)."""
    b = case["buyer"]
    per_point, sustainable = b.get("relationship_eur_per_point"), b.get("sustainable_margin_pct")
    if b.get("category_goal") not in RELATIONSHIP_GOALS or not per_point or not sustainable:
        return 0.0
    t = closing_terms(case, terms, est.standard)
    worth = supplier_value(case, t, est.model(case, t, cash=True))
    soft = sum(w * _soft(sustainable - (worth - f) / worth * 100) for w, f in est.f_nodes())
    return per_point * soft * _years(case, t)


def cost(case, terms, est, rel=True):
    """C for us. `rel=False` leaves out the relationship charge, whose size rests on our estimate of
    the supplier's cost (arena: often 10-30 % off); with it, the charge is an expectation over that error."""
    base = total_cost(case, terms, est.standard)
    if not rel or not est.rel_weight:
        return base
    return base + est.rel_weight * rel_charge(case, terms, est)


class Package:
    __slots__ = ("combo", "p_floor", "p_opt", "c_opt")

    def __init__(self, combo, p_floor, p_opt, c_opt):
        self.combo, self.p_floor, self.p_opt, self.c_opt = combo, p_floor, p_opt, c_opt


FAST_CLOSE = "fast_close"
RECIPROCAL = "reciprocal"


def profile_strategy(dossier):
    """Choose one primary bargaining approach and one fallback before the first message, from what we know of the
    supplier: its profile and arena record, its past negotiations with us, and its current news and letters. All
    but the profile come from the data package, so a new supplier (the Live Final) gets a plan too.

    Returns (primary, fallback, reason, evidence). `evidence["lead"]` is what the strategist should lead with,
    `evidence["trusted"]` (with `why_trusted`) switches risky trades off. The plan shapes how we write; code
    prices every sheet either way. Its only pricing effect: a slightly deeper opening for a reciprocal, untrusted
    supplier whose arena record shows room below its first counter (`room`)."""
    profile = getattr(dossier, "profile", None) or {}
    playbook = (profile.get("persona") or {}).get("playbook") or {}
    arena = profile.get("arena") or {}
    try:
        sessions, walked = max(0, int(arena.get("sessions") or 0)), max(0, int(arena.get("walked") or 0))
    except (TypeError, ValueError):
        sessions, walked = 0, 0
    first = arena.get("first_counter_vs_opening")
    try:
        first = float(first) if first is not None and math.isfinite(float(first)) else None
    except (TypeError, ValueError):
        first = None
    walk_rate = walked / sessions if sessions else None
    # a supplier whose first counter gives 8 %+ of its opening and rarely walks has room below its counter
    room = sessions >= 5 and first is not None and first <= 0.92 and walk_rate is not None and walk_rate <= 0.15
    trusted, why_trusted = getattr(dossier, "relationship", (False, []))

    cues, reasons, lead = [], [], []  # cues pick fast close; reasons only explain the reciprocal default
    # its situation now, from the news (newest item per kind)
    news = {}
    for nid, day, topic, headline in getattr(dossier, "current_news", None) or []:
        news.setdefault(NEWS_SITUATION[topic], f"{headline} ({nid}, {day})")
    if "capacity" in news:
        cues.append(f"capacity is its constraint: {news['capacity']}")
        lead.append("the binding-volume or lead-time package rather than a price squeeze")
    if "refocus" in news or dossier.has("small_account", strong=True):
        cues.append("it is refocusing on bigger customers" + (f": {news['refocus']}" if "refocus" in news else ""))
        lead.append("our binding volume and a long contract")
    if "distress" in news:
        if dossier.goal in RELATIONSHIP_GOALS:  # the arena charges for a strategic supplier left below its margin
            cues.append(f"a strategic supplier under financial pressure: {news['distress']}")
        else:
            reasons.append(f"it is under financial pressure: {news['distress']}")
        lead.append("faster payment, which helps it more than a price step")
    if "slack" in news:
        reasons.append(f"it has idle capacity: {news['slack']}")
        lead.append("volume for price")
    # its letter: what it asks against what its own cost drivers justify, and the notice it gave
    letter = getattr(dossier, "letter", None) or {}
    if letter.get("supplier") == dossier.sid and letter.get("asked_pct") is not None:
        just = dossier.justified_pct
        if letter["asked_pct"] - just >= 2:
            reasons.append(
                f"its letter asks +{letter['asked_pct']} %, its cost drivers moved {just:+.1f} % in 12 months"
            )
            lead.append("the cost drivers that fell, with their index ids")
    if notice := getattr(dossier, "notice", None):
        reasons.append(f"its letter gives {notice[1]} days' notice, our contract {notice[3]}")
        lead.append("the notice period of our contract")
    # how our past negotiations with it went
    if (deals := getattr(dossier, "deals", None)) and deals[0] >= 4:
        n, asked, settled = deals
        if asked - settled >= 2:
            reasons.append(
                f"in {n} past negotiations it asked {asked:+.1f} % and settled at {settled:+.1f} %: it gives way"
            )
        elif asked - settled <= 0.5:
            cues.append(
                f"in {n} past negotiations it settled close to its ask ({asked:+.1f} %, settled {settled:+.1f} %)"
            )
    # its profile and our arena sessions with it. Not a cue: how often it agreed to our round-1 offer. That says
    # our opening was above its first counter (we overpaid: P02, P05-70210, P18), never that we should close fast.
    if playbook.get("walk_risk") == "high":
        cues.append("the profile flags high walk risk")
    if playbook.get("opening") == "close":
        cues.append("the profile describes a close opening")
    if sessions >= 5 and walk_rate is not None and walk_rate >= 0.20:
        cues.append(f"the arena record shows {walked}/{sessions} walks")
    if room:
        reasons.append(
            f"its first counter is at {first:.0%} of its opening (median) and it rarely walks ({walked}/{sessions})"
        )
    evidence = {
        "first_counter_vs_opening": first,
        "walk_rate": walk_rate,
        "sessions": sessions,
        "room": room,
        "trusted": trusted,
        "why_trusted": why_trusted,
        "lead": list(dict.fromkeys(lead)),
        "reasons": reasons,
    }
    if cues:
        return FAST_CLOSE, RECIPROCAL, "; ".join(cues), evidence
    reason = (
        "; ".join(reasons) or "no warning signs in its record, news or letters; test reciprocity on its first counter"
    )
    return RECIPROCAL, FAST_CLOSE, reason, evidence


class Planner:
    """Per supplier thread: packages, the concession path, accept and walk."""

    def __init__(self, case, est, horizon_rounds=6):
        self.case, self.est, self.rounds = case, est, horizon_rounds
        (
            self.primary_strategy,
            self.fallback_strategy,
            self.strategy_reason,
            self.strategy_evidence,
        ) = profile_strategy(est.d)
        self.strategy = self.primary_strategy
        self.strategy_switched = False  # at most one profile-chosen fallback; no strategy roulette
        self.strategy_switch_reason = None
        est.d.strategy_plan = self.strategy_summary()
        alt = case["buyer"].get("alternative") or {}
        self.c_alt = total_cost(case, alt["terms"]) if alt.get("terms", {}).get(PRICE) else math.inf
        self.frozen = est.d.current if est.d.has("price_frozen", strong=True) else None
        self.relationship = case["buyer"].get("category_goal") in RELATIONSHIP_GOALS
        self.strict_options = False  # the arena refused a value outside `term_options`: offer options only
        self.last_value = None  # supplier value of our best sheet in the last message
        self.ask_value = None  # supplier value of its own standing sheet: we never offer more than that
        self.cap_c = math.inf  # the most a package may cost us (set by the agent: the reservation)
        self.anchor_value = self._anchor()
        self.anchor_gap = self._anchor_gap()
        self.asks = []  # supplier value of its best standing sheet, one entry per reply
        self.ask_sheets = []  # the sheets behind `asks`, to value them again when our model of it changes
        self.last_sheets = []  # our last sheets, likewise
        self.locked = {}  # terms the supplier keeps insisting on (e.g. a mandatory index clause)
        self.kept = {}  # terms it called mandatory in words (the strategist's reading)
        self.seen_terms = {}  # field -> set of values the supplier has offered
        self.replies = 0
        # a raw-material distributor fixes no price beyond 12 months (arena P08, P18: "we cannot offer a fixed price
        # over a 36-month period, so we must decline your second option"): every longer sheet carries a clause, so
        # a clause-free long sheet is only declined and, on the twin, made it step back (P16 ended in a walk)
        self.clause_required = "index_clause" in case["negotiable"] and case.get("type") == "raw_material"
        self.pref_clause = self._pref_clause()
        self.clause_seen = False  # its own sheets carried a clause: no trial needed any more
        self.open_date = None  # price letters: the effective date of its first quote
        self.min_date = None  # the earliest effective date it has since offered in every sheet: ours now
        self.open_lead = None  # the lead time of its first quote
        self.second_step = False  # it said it will come back on price (the RFQ two-step): move minimally
        # bounds on non-price terms we have won or must keep: field -> (op, value). Not the company's EUR 150
        # chargeback standard (company.md): it costs the supplier at its real defect rate but is worth less to
        # us at the committed one, so forcing it raised C on the twin (P12 0.351 -> 0.325); MESO still offers it.
        self.bounds = {}
        self.refresh()

    def strategy_summary(self):
        # code prices every sheet either way: the plan is about how we write, never about the numbers
        labels = {
            FAST_CLOSE: "fast-close bargaining (a supplier that may walk: cooperative tone, answer every question, "
            "lead with the package it wants; no pressure or deadlines)",
            RECIPROCAL: "reciprocal bargaining (acknowledge each concession it makes and show ours in return)",
        }
        ev = self.strategy_evidence
        extra = []
        if ev.get("lead"):
            extra.append("Lead with: " + "; ".join(ev["lead"]) + ".")
        if ev.get("trusted"):
            extra.append(
                f"Trusted supplier ({'; '.join(ev['why_trusted'])}): no risky trades. Partnership tone; no pressure, "
                "deadlines or threats; never question its figures without our own data."
            )
        if self.strategy_switched:
            head = (
                f"Negotiation plan: started with {labels[self.primary_strategy]}, then switched once to "
                f"{labels[self.strategy]} because {self.strategy_switch_reason}. Do not add a third approach; "
                "price and terms stay inside the planned menu and our walk-away ceiling."
            )
        else:
            trigger = (
                "a meaningful supplier concession (at least 4% of its opening)"
                if self.strategy == FAST_CLOSE
                else "two consecutive replies with negligible supplier movement"
            )
            head = (
                f"Negotiation plan: use {labels[self.strategy]} first ({self.strategy_reason}). One fallback is "
                f"{labels[self.fallback_strategy]}, used only after {trigger}. Its record, news and letters choose "
                "the approach; the priced menu and our walk-away ceiling choose the offer."
            )
        return " ".join([head, *extra])

    def _switch_to_fallback(self, reason):
        if self.strategy_switched or self.strategy == self.fallback_strategy:
            return None
        previous = self.strategy
        self.strategy = self.fallback_strategy
        self.strategy_switched = True
        self.strategy_switch_reason = reason
        self.est.d.strategy_plan = self.strategy_summary()
        return {"from": previous, "to": self.strategy, "reason": reason}

    def _anchor_gap(self):
        """How far below the supplier's ask we open, in value terms. Fitted to the real arena's revealed
        C_best: leverage-type cases (RFQ, raw material, bundling, capacity) had 12-30 % room, routine and
        strategic renewals and price letters only 0.3-5 %."""
        ctype, goal = self.case.get("type"), self.case["buyer"].get("category_goal")
        # arena (13 sessions): the supplier's first counter is the same whether we open 5 % or 20 % below,
        # and deep openings only bring its final offer sooner (P10: final in round 1 after -19 %)
        gap = {
            "rfq": 0.18,  # arena v3: P05-70210 accepted our first offer 12.5 % below its ask
            "raw_material": 0.10,  # 0.14 lost on the twin (P08 0.451 -> 0.386): its final offer lands at its limit anyway
            "bundling": 0.10,
            "capacity": 0.10,
            "quality": 0.10,
            "price_increase": 0.06,
            "renewal": 0.06,
        }.get(ctype, 0.08)
        d = self.est.d
        if ctype == "price_increase" and goal in ("stabilise", "routine", "secure_supply"):
            gap = min(gap, 0.05)  # arena v3: P02 agreed to our first sheet, 2 % below its ask
        elif ctype == "renewal" and goal in ("stabilise", "routine", "secure_supply"):
            gap = min(gap, 0.025)
        # easy to move: spare capacity, an old cost sheet, a reference customer to win, a weak cash squeeze
        if any(d.has(t) for t in ("free_capacity", "stale_costing", "reference_customer")) or (
            d.has("cash_short") and not d.has("cash_short", strong=True)
        ):
            gap += 0.05
        # where the arena record shows room below its first counter and few walks, open slightly deeper (at most
        # two points; the caps below still apply) - not to a trusted supplier, whose goodwill is worth more. No
        # shallower opening for a fast-close supplier: its first counter does not depend on ours, an opening above
        # it is agreed at once, and the only real walk after an opening was P10's -19 % to a capacity-tight
        # supplier (capped below). Twin: a 4 % cap cost P10 and P05.
        ev = self.strategy_evidence
        if self.strategy == RECIPROCAL and ev["room"] and not ev.get("trusted"):
            gap += min(0.02, (0.92 - ev["first_counter_vs_opening"]) * 0.15)
        if ctype == "price_increase" and d.notice:
            gap = max(gap, 0.06)
        if ctype == "quality" or (d.has("cash_short", strong=True) and ctype != "rfq"):
            gap = min(gap, 0.05)  # quality cases are about ppm and chargebacks, not a price cut to a cash-short shop
            # (in an RFQ the rivals' prices do the cutting: P05's bidders anchored 18 % below still countered)
        if ctype == "capacity" and d.has("capacity_tight"):
            gap = min(gap, 0.08)  # no alternative and a +25 % fallback: commitment, not a squeeze
        elif d.has("capacity_tight"):
            gap = min(gap, 0.10)  # far below a capacity-tight bidder: its final offer at once (P10: -19 %)
        if d.has("approval_ladder") and ctype != "rfq":
            gap = min(gap, 0.10)  # a pricing committee decides above 2 %: stay in a range it can approve
        return gap

    def _anchor(self):
        """Price letters: open at the price the index data justifies (current price × weighted driver
        change), so the offer says what the e-mail argues."""
        d = self.est.d
        letter = d.letter or {}
        if self.case.get("type") != "price_increase" or not d.current or not letter.get("asked_price"):
            return None
        asked = (letter["asked_price"] / d.current - 1) * 100
        if d.justified_pct >= asked - 1:
            return None  # the increase is justified: no data anchor below it
        price = d.current * (1 + max(d.justified_pct, 0) / 100)
        return self.est.value(self.case, {PRICE: price})

    def _complete(self, terms):
        return {f: terms.get(f) for f in self.case["negotiable"]}

    def refresh(self):
        """Re-price every package after the estimate changed."""
        out = []
        for combo in grid(self.case, self.est.standard, self.strict_options):
            if any(combo.get(f) != v for f, v in {**self.kept, **self.locked}.items() if f in combo):
                continue
            if self.min_date and combo.get("effective_date") and combo["effective_date"] < self.min_date:
                continue  # never give back a later start of a price increase it already granted
            if not self._in_bounds(combo):
                continue
            if not self._clause_ok(combo):
                continue
            pf = self.est.price_for_value(self.case, combo, self.est.floor_value)
            if pf is None:
                continue
            if self.frozen:
                pf = max(pf, self.frozen)
            t = self._complete({**combo, PRICE: round(pf, 2)})
            if not must_haves_ok(self.case, t, self.est.standard):
                continue
            p_opt = self._argmin_price(combo, pf) if self.relationship and self.est.rel_weight >= 1 else pf
            c = cost(self.case, {**t, PRICE: round(p_opt, 2)}, self.est)
            out.append(Package(combo, pf, p_opt, c))
        self.packages = sorted(out, key=lambda p: p.c_opt)

    def _months(self, combo):
        m = combo.get("contract_months")
        if m is None:
            m = (self.case.get("fixed_terms") or {}).get("contract_months")
        return m or 12

    def _pref_clause(self):
        """The clause option that costs us least per unit of price: the case's index path beyond the deadband (signed:
        a falling index makes the clause pay us) plus the index risk, which is the same share for every deadband.
        Where the path sits inside several deadbands they cost the same and the lowest protects the supplier most."""
        b, best = self.case["buyer"], None
        opts = [o for o in self.case.get("term_options", {}).get("index_clause") or [] if o]
        weight = {}
        if len({o["series"] for o in opts}) > 1:  # several series: the one its cost follows
            weight = {sr: w for sr, w, *_ in self.est.d.drivers}
        top = max((weight.get(o["series"], 0.0) for o in opts), default=0.0)
        for o in [o for o in opts if weight.get(o["series"], 0.0) == top]:
            path = b.get("index_paths", {}).get(o["series"], 0.0)
            moved = max(0.0, abs(path) - o["deadband_pct"] / 100) * math.copysign(1, path)
            key = (round(o["material_share"] * moved, 6), o["deadband_pct"])
            if best is None or key < best[0]:
                best = (key, o)
        return best[1] if best else None

    def _clause_ok(self, combo):
        """One clause per thread: every supplier of the arena answered with the same deadband-5 clause (12 of 12
        sessions), also when we had offered deadband 3 or none, and on the twin a mix of deadbands made our moves
        look like steps back. Offer the cheapest one for us, unless the supplier has insisted on another. A
        required clause is needed on every sheet beyond 12 months."""
        clause = combo.get("index_clause")
        if clause and self.pref_clause and clause != self.pref_clause and "index_clause" not in self.locked:
            return False
        return not (self.clause_required and not clause and self._months(combo) > 12)

    def require_clause(self):
        """It said in words that it fixes no price beyond 12 months."""
        if not self.clause_required and "index_clause" in self.case["negotiable"]:
            self.clause_required = True
            self.refresh()

    def set_strict(self):
        """Offer only values from `term_options` from now on."""
        if not self.strict_options:
            self.strict_options = True
            self.refresh()

    def _in_bounds(self, combo):
        """A bound applies only where some option of the case meets it (else it would rule out every package)."""
        for f, (op, v) in self.bounds.items():
            x, opts = combo.get(f), self.case.get("term_options", {}).get(f) or []
            if x is None or v is None or not any((o >= v) if op == ">=" else (o <= v) for o in opts):
                continue
            if (op == ">=" and x < v) or (op == "<=" and x > v):
                return False
        return True

    def set_ppm_floor(self, ppm):
        """Its 8D report commits to `ppm`: a tighter commitment would cost it work it has not promised."""
        if self.bounds.get("ppm_commitment") != (">=", ppm):
            self.bounds["ppm_commitment"] = (">=", ppm)
            self.refresh()

    def _argmin_price(self, combo, lo):
        """C is convex in price once the relationship charge is in: golden-section search above the floor."""
        f = lambda p: cost(self.case, self._complete({**combo, PRICE: p}), self.est)  # noqa: E731
        a, b = lo * 0.85, lo * 1.35  # may sit below our floor estimate: the floor is a guess, the charge is known
        g = (math.sqrt(5) - 1) / 2
        c, d = b - g * (b - a), a + g * (b - a)
        for _ in range(40):
            if f(c) < f(d):
                b = d
            else:
                a = c
            c, d = b - g * (b - a), a + g * (b - a)
        return (a + b) / 2

    @property
    def confident(self):
        """Our cost estimate is good enough to act on the relationship charge: a should-cost of the
        supplier's own plant (±5 %) or its own cost breakdown."""
        return self.est.err <= 0.05

    @property
    def fair_price_case(self):
        """Strategic or bottleneck goal, a confident cost estimate, and the supplier's own ask is below
        its full cost: it is selling at a loss, and a fair price above its ask is the cheapest deal for
        us once the relationship charge counts. (Tested on the twin: offering above the ask only pays
        when the supplier is under water, not merely below a sustainable margin.)"""
        if not (self.relationship and self.confident and self.ask_value and self.packages):
            return False
        return self.ask_value < self.est.full_cost

    def best_estimate(self):
        p = self.packages[0] if self.packages else None
        return (p.c_opt, self._complete({**p.combo, PRICE: round(p.p_opt, 2)})) if p else (math.inf, None)

    # -- offers -----------------------------------------------------------------------------------
    def level(self, k):
        """Supplier-value level of the k-th offer relative to its estimated floor: start below, concede slowly."""
        e = self.est.err
        x = min(1.0, k / max(1, self.rounds - 1))
        return (1 - 0.9 * e) + 1.7 * e * x**1.6

    def target_value(self, k, rounds_left=10):
        """Supplier value of our next offer. Real suppliers concede 1-6 % a round: we open a case-type
        distance below their ask and then move reciprocally (0.6x their last step, at least a visible
        0.4 %), never backwards, never above their ask. Our cost estimate is only a loose lower bound."""
        if not self.ask_value:  # no quote seen yet: our estimate is all we have
            return self.est.floor_value * self.level(k)
        if self.last_sheets:  # our model of it may have changed since: value our last offer again
            self.last_value = max(self.est.value(self.case, t) for t in self.last_sheets)
        ask = self.ask_value
        if not self.last_value:
            target = ask * (1 - self.anchor_gap)
            if self.anchor_value:  # a price letter: open at the data-justified price
                target = min(self.anchor_value, ask * 0.995)
            d = self.est.d
            if self.case.get("type") == "renewal" and d.current:  # a renewal opens at today's price at most
                target = min(target, self.est.value(self.case, {PRICE: d.current}))
        else:
            # arena: a supplier concedes only on a visible move, and its final offer lands at its limit. An
            # offer above its next ask is agreed at our own price, so stay just under that limit and keep
            # moving a little every round until the final offer comes.
            v0 = self.asks[0] if self.asks else ask
            lim = self.limit_hat
            if flags.lean():  # small fixed steps that keep it countering, instead of heading for its limit
                target = min(self.last_value + LEAN_STEP * v0, ask - LEAN_GAP * v0)
                if self.second_step:  # "we will come back to you on price": its next step comes anyway
                    target = self.last_value + 0.001 * v0
            elif lim is None:
                target = max(self.last_value + 0.004 * v0, ask - 0.015 * v0)
            else:
                nxt = ask - 0.4 * max(0.0, ask - lim)
                aim = min(lim - 0.003 * v0, nxt - 0.002 * v0)
                step = 0.0025 * v0 if self.last_value + 0.0025 * v0 <= aim else 0.0012 * v0  # small near its limit
                # at most 1.5 % of its opening a round: the limit estimate assumes a 75 % first step, and a supplier
                # whose bigger moves need its committee steps less at first (P02 b1: we jumped to its opening price).
                # RFQ: the rivals' cheaper offers do the work ("the buyer shows a much lower rival quote. I give
                # half the step now and check the rest" - P05: 0.45 -> 0.81), so 1 %; committee suppliers 1 %.
                # (Not lifted for a fast-close plan: 1.5 % to P10's committee supplier lost on the twin.)
                jump = 0.01 if len(self.case.get("suppliers") or []) > 1 or self.est.d.has("approval_ladder") else 0.015
                aim = min(aim, self.last_value + jump * v0)
                if self.second_step:  # "we will come back to you on price": its next step comes anyway
                    aim = self.last_value + 0.001 * v0
                target = max(self.last_value + step, aim)
        target = max(target, 0.75 * self.est.floor_value)
        if self.last_value:
            target = max(target, self.last_value * 1.003)  # never a step back
        return min(target, ask * 0.997)

    def price_at(self, pkg, k, rounds_left=10):
        """Our price for a package in round k, at this round's supplier-value target."""
        target = self.target_value(k, rounds_left)
        if self.relationship and self.est.rel_weight >= 1:  # the cheapest price (charge included) may sit higher
            target = max(
                target,
                min(
                    self.est.value(self.case, self._complete({**pkg.combo, PRICE: pkg.p_opt})),
                    (self.ask_value or math.inf) * 0.997,
                ),
            )
        p = self.est.price_for_value(self.case, pkg.combo, target) or pkg.p_floor
        if self.frozen:
            p = max(p, self.frozen)
        return round(p, 2)

    def sheets(self, k, n=3, rounds_left=10):
        """Up to n packages that cost us the same (MESO): the cheapest at this round's level, plus the
        next structurally different ones re-priced to the same C. The supplier's pick shows what it values."""
        if not self.packages:
            return []
        cand = []
        for pkg in self.packages[:40]:
            t = self._complete({**pkg.combo, PRICE: self.price_at(pkg, k, rounds_left)})
            cand.append((cost(self.case, t, self.est), t))
        cand = [x for x in cand if cost(self.case, x[1], self.est, rel=self.confident) <= self.cap_c]
        if not cand:
            return []
        cand.sort(key=lambda x: x[0])
        if self._clause_trial():  # the trial: clause-free sheets, plus one clause sheet added below
            cand = [x for x in cand if not x[1].get("index_clause")] or cand
        c0, best = cand[0]
        out, vals = [best], [self.est.value(self.case, best)]
        # alternatives that differ from the best in a term not varied yet first (b2 P02: three payment
        # variants, and the index-clause sheet it had taken at once in v3 was never shown)
        varied = set()

        def fresh(t):
            diff = {f for f in t if f != PRICE and t.get(f) != best.get(f)}
            return 0 if diff - varied else 1

        rest = [x for x in cand[1:] if x[0] <= c0 * 1.03]
        if self._clause_trial() and not best.get("index_clause"):
            pair = self._clause_pair(best, c0)
            if pair and not (self.ask_value and self.est.value(self.case, pair) > self.ask_value):
                out.append(pair)
                vals.append(self.est.value(self.case, pair))
            rest = [x for x in rest if not x[1].get("index_clause")]  # one clause sheet is the trial
        while rest and len(out) < n:
            c, t = min(rest, key=lambda x: (fresh(x[1]), x[0]))  # re-ranked after every pick
            rest.remove((c, t))
            same = self.reprice_to_cost(t, c0)
            if not same or not (0.85 * best[PRICE] <= same[PRICE] <= 1.15 * best[PRICE]):
                continue
            if self.ask_value and not self.fair_price_case and self.est.value(self.case, same) > self.ask_value:
                continue
            v = self.est.value(self.case, same)
            if all(abs(v - u) / u > 0.002 for u in vals):  # a real alternative for the supplier
                out.append(same)
                vals.append(v)
                varied |= {f for f in same if f != PRICE and same.get(f) != best.get(f)}
        return [self._no_step_back(t) for t in out]

    def _clause_trial(self):
        """An optional clause on a part driven by its series, and no sign yet that this supplier wants one: offer
        each time one sheet with the clause next to the one without, so its pick shows (arena P02: it took the clause
        sheet over three clause-free ones; on the twin, where it values no clause, an all-clause message came back
        as an all-clause deal, 0.26 against 0.65, the whole index risk for nothing)."""
        # only its own sheets end the trial: a wish read from its words (persona read, the series it blames)
        # gets the clause sheet beside the clause-free ones, and its pick decides (merge of the two rules)
        return (
            not self.clause_required
            and not self.clause_seen
            and self.pref_clause is not None
            and self.est.clause_driven
            and self.locked.get("index_clause") is None
        )

    def _clause_pair(self, best, c0):
        """The best sheet with the clause, at the price where it costs us what the best does; where no price does
        (the best sits low on a relationship charge) the price less the clause's cost share, if that stays close."""
        t = {**best, "index_clause": self.pref_clause}
        same = self.reprice_to_cost(t, c0)
        if not same:
            # no clause price costs what the best does (it sits low on a relationship charge): the clause sheet at the
            # price where it costs us least. Not the best's price less the clause's cost share: live P02 (stabilise)
            # took that at once, 357.12 with the clause, relationship charge EUR 66,941, score 0.63 (FINDINGS: its
            # optimum with the clause is ~361-363; this package's is 360.36)
            rest = {f: v for f, v in t.items() if f != PRICE}
            opt = [
                k.p_opt
                for k in self.packages
                if {f: v for f, v in self._complete(k.combo).items() if f != PRICE} == rest
            ]
            if opt:
                same = {**t, PRICE: round(opt[0], 2)}
            else:
                b = self.case["buyer"]
                path = b.get("index_paths", {}).get(self.pref_clause["series"], 0.0)
                moved = max(0.0, abs(path) - self.pref_clause["deadband_pct"] / 100) * math.copysign(1, path)
                share = self.pref_clause["material_share"] * (moved + b.get("index_risk_pct", 0) / 100)
                same = {**t, PRICE: round(best[PRICE] * (1 - share), 2)}
            if cost(self.case, same, self.est) > c0 * 1.012:
                return None
        return same if 0.85 * best[PRICE] <= same[PRICE] <= 1.15 * best[PRICE] else None

    def menu(self, base, k, rounds_left=10, n=5):
        """The offers the strategist may choose from: our equal-cost sheets (`base`, extra cost 0) plus up to
        `n` structurally different packages (another index clause, payment, term, lead time ...) at the
        price where they cost us least but no less than the same value to the supplier as our best sheet.
        [(terms, extra cost to us as a share)], every one inside our limits and never above its ask."""
        if not base:
            return []
        c0 = min(cost(self.case, t, self.est) for t in base)
        out = [(t, cost(self.case, t, self.est) / c0 - 1) for t in base]
        seen = [{f: v for f, v in t.items() if f != PRICE} for t in base]
        target = self.est.value(self.case, base[0])  # the value of our own best sheet, not the richest one
        pool = []
        for pkg in self.packages[:60]:
            combo = self._complete(pkg.combo)
            rest = {f: v for f, v in combo.items() if f != PRICE}
            if rest in seen:
                continue
            p = self.est.price_for_value(self.case, pkg.combo, target)  # the same value to it as our best
            if p is None:
                continue
            if self.frozen:
                p = max(p, self.frozen)
            t = self._no_step_back({**combo, PRICE: round(p, 2)})
            if cost(self.case, t, self.est) > c0 * 1.002:  # equal cost to us, or it is not on the menu: the
                t = self.reprice_to_cost(t, c0)  # supplier takes the sheet best for it (b3 P02: the copper clause
                if not t:  # at +0.8 % cost to us, 0.67 where the clause-free sheet would have scored ~0.94)
                    continue
                t = self._no_step_back(t)
            c = cost(self.case, t, self.est)
            if c / c0 - 1 > 0.002 or cost(self.case, t, self.est, rel=self.confident) > self.cap_c:
                continue
            if self.ask_value and self.est.value(self.case, t) > self.ask_value:
                continue
            pool.append((t, c / c0 - 1, {f for f in rest if rest[f] != seen[0].get(f)}))
        varied = {f for t in base[1:] for f in t if f != PRICE and t.get(f) != base[0].get(f)}
        while pool and len(out) < len(base) + n:  # a different term first, then the cheapest for us
            t, dc, diff = min(pool, key=lambda x: (0 if x[2] - varied else 1, x[1]))
            pool = [x for x in pool if x[0] is not t]
            out.append((t, dc))
            varied |= diff
        return out

    def _no_step_back(self, t):
        """Never a lower price than our last offer for the same terms (arena b1 P08: 5.55 then 5.54 on the
        same package, and the supplier withdrew)."""
        rest = {f: v for f, v in t.items() if f != PRICE}
        prev = [x[PRICE] for x in self.last_sheets if {f: v for f, v in x.items() if f != PRICE} == rest]
        if prev and t[PRICE] < max(prev) + 0.01:
            return {**t, PRICE: round(max(prev) + 0.01, 2)}
        return t

    @property
    def f_hat(self):
        """Floor estimate from the supplier's own first move (arena fit, leave-one-out error ~2 %):
        F = V0 - (V0 - A1) / k, k ~0.335 for leverage-type cases, ~0.75 for routine and strategic ones;
        price letters: today's price plus the data-justified change."""
        if self.anchor_value and self.case.get("type") == "price_increase":
            return self.anchor_value
        if len(self.asks) < 2:
            return None
        v0, a1 = self.asks[0], self.asks[1]
        wide = self.case.get("type") in ("rfq", "raw_material", "bundling", "capacity", "quality")
        k = 0.335 if wide else 0.75
        return v0 - max(0.0, v0 - a1) / k

    @property
    def limit_hat(self):
        """Where its final offer will land (arena fit): its first counter covers ~75 % of the way from its
        opening to that limit, ~35 % for a supplier short of cash. Verified arguments are already in it."""
        if len(self.asks) < 2:
            return None
        rho = 0.35 if self.est.d.has("cash_short", strong=True) else 0.75
        v0, a1 = self.asks[0], self.asks[1]
        # and never above 2 % under its first counter: in 47 real sessions no supplier gave more than 2.6 % of
        # its opening after round 1, and one that barely moved at first (a committee, b4 P02: 0.4 %) still
        # has that room - the 75 % rule alone put its limit at its ask, we offered that, and it agreed at once
        return min(v0 - max(0.0, v0 - a1) / rho, a1 - 0.02 * v0)

    def near_floor(self):
        """Its ask is within 15 % of the room above our floor estimate: take it."""
        f = self.f_hat
        if f is None or not self.asks or not self.ask_value:
            return False
        v0 = self.asks[0]
        if v0 - f < 0.02 * v0:  # it barely moved in round 1 (a committee, a frozen list): no floor to read
            return False  # (b2 P02: this took its ask at once, above its opening price)
        return self.ask_value <= f + 0.15 * max(0.0, v0 - f)

    def holding(self):
        """It answered twice without moving (arena P03: four holds, then a final offer)."""
        return (
            len(self.asks) >= 3
            and abs(self.asks[-1] - self.asks[-2]) < 0.001 * self.asks[-1]
            and abs(self.asks[-2] - self.asks[-3]) < 0.001 * self.asks[-2]
        )

    def match_sheets(self, n=2):
        """Its own value level on our terms: worth as much to the supplier as its standing offer, but
        with the package that is cheapest for us (its standing sheet may leave fields to its standard terms)."""
        if not self.ask_value:
            return []
        out = []
        for pkg in self.packages[:40]:
            p = self.est.price_for_value(self.case, pkg.combo, self.ask_value * 1.0005)
            if p is None or (self.frozen and p < self.frozen):
                continue
            t = self._complete({**pkg.combo, PRICE: math.ceil(p * 100) / 100})  # never a cent below its level
            if cost(self.case, t, self.est, rel=self.confident) <= self.cap_c:
                out.append((cost(self.case, t, self.est), t))
        out.sort(key=lambda x: x[0])
        picked = []
        for _c, t in out:
            if len(picked) >= n:
                break
            if all(sum(t[f] != o[f] for f in t if f != PRICE) >= 1 for o in picked):
                picked.append(t)
        return picked

    def safe_match(self, standing_terms):
        """After a final offer: its exact price, with terms changed only where that is cheaper for us
        and, in our model, no worse for the supplier. Anything less would be a push it may walk on."""
        if not standing_terms or standing_terms.get(PRICE) is None:
            return None
        full = closing_terms(self.case, standing_terms, self.est.standard)
        base = self._complete({**full, PRICE: standing_terms[PRICE]})
        v0, c0 = self.est.value(self.case, base), cost(self.case, base, self.est)
        best, c_best = dict(base), c0
        for f in [f for f in self.case["negotiable"] if f != PRICE]:
            for opt in self.case.get("term_options", {}).get(f) or []:
                t = {**best, f: opt}
                if not must_haves_ok(self.case, t, self.est.standard):
                    continue
                if self.est.value(self.case, t) >= v0 * 0.9995 and (c := cost(self.case, t, self.est)) < c_best:
                    best, c_best = t, c
        return best if c_best < c0 * 0.999 else None

    def commit(self, sheets):
        """Remember the supplier value of what we sent, so the next offer is a visible move."""
        if sheets:
            self.last_sheets = [dict(t) for t in sheets]
            self.last_value = max(self.est.value(self.case, t) for t in sheets)

    def probe(self, target_c, n=2):
        """Sheets that cost us `target_c` (just under our walk-away), for when every package our cost estimate
        allows looks dearer than the alternative. The estimate of its floor is a guess (10-15 % off on generic
        should-cost): walking without one real offer below the alternative throws the chance away (twin P12:
        walked after the opening at 0.377 while a deal at C_best 853 k, under the alternative's 907 k, existed)."""
        out = []
        for pkg in self.packages[:12]:
            t = self._complete({**pkg.combo, PRICE: round(pkg.p_floor, 2)})
            same = self.reprice_to_cost(t, target_c)
            if not same or same in out or not must_haves_ok(self.case, same, self.est.standard):
                continue
            if self.ask_value and not self.fair_price_case and self.est.value(self.case, same) > self.ask_value:
                continue  # as in `sheets`: never more than it asks
            out.append(same)
            if len(out) >= n:
                break
        return [self._no_step_back(t) for t in out]

    def reprice_to_cost(self, terms, target_c):
        lo, hi = terms[PRICE] * 0.6, terms[PRICE] * 1.6  # C misbehaves far from real prices: stay near
        if cost(self.case, {**terms, PRICE: lo}, self.est) > target_c:
            # with the relationship charge C falls and then rises in price: search the rising branch only
            # (b1/b2 P02: every alternative sheet, the index-clause one too, was dropped here)
            f = lambda x: cost(self.case, {**terms, PRICE: x}, self.est)  # noqa: E731
            a, b, g = lo, hi, (math.sqrt(5) - 1) / 2
            for _ in range(40):
                c, d = b - g * (b - a), a + g * (b - a)
                a, b = (a, d) if f(c) < f(d) else (c, b)
            lo = (a + b) / 2
        if (
            cost(self.case, {**terms, PRICE: lo}, self.est) > target_c
            or cost(self.case, {**terms, PRICE: hi}, self.est) < target_c
        ):
            return None
        for _ in range(40):
            mid = (lo + hi) / 2
            if cost(self.case, {**terms, PRICE: mid}, self.est) > target_c:
                hi = mid
            else:
                lo = mid
        if self.frozen and lo < self.frozen:
            return None
        return {**terms, PRICE: round(lo, 2)}

    # -- learning ---------------------------------------------------------------------------------
    def observe_reply(self, sheets, status):
        """One supplier reply (all its alternative sheets): its best value for us is its current ask;
        a term it repeats in every sheet of two replies while we proposed otherwise becomes a given."""
        sheets = [t for t in sheets if t and t.get(PRICE) is not None]
        if not sheets:
            return
        self.replies += 1
        for f in self.case["negotiable"]:
            if f == PRICE:
                continue
            vals = {repr(t.get(f)) for t in sheets if f in t}
            if len(vals) == 1:
                self.seen_terms.setdefault(f, []).append(next(iter(vals)))
            else:
                self.seen_terms[f] = []
        if self.clause_required and any(self._months(t) > 12 and not t.get("index_clause") for t in sheets):
            self.clause_required = False  # it quoted a long term without a clause: no mandatory clause here
            self.refresh()
        locked = {}
        for f, seq in self.seen_terms.items():
            if len(seq) >= 2 and len(set(seq[-2:])) == 1:
                v = next(t.get(f) for t in sheets if f in t)
                if f == "index_clause" and v is None:
                    continue  # never lock ourselves out of a clause we did not want anyway
                locked[f] = v
        if locked != self.locked:
            self.locked = locked
            self.refresh()
        dates = [t.get("effective_date") for t in sheets]
        if self.case.get("type") == "price_increase" and all(dates):
            # arena v3 P01: it granted 1 January / 1 April instead of 1 November, and we offered 1 November again
            self.open_date = self.open_date or min(dates)
            if min(dates) > self.open_date and min(dates) > (self.min_date or ""):
                self.min_date = min(dates)
                self.refresh()
        if "lead_time_days" in self.case["negotiable"] and self.est.learn_lead_premium(sheets):
            self._revalue()  # what a lead time is worth to it changed: old asks and our last offer on the new model
            self.refresh()
        leads = [t.get("lead_time_days") for t in sheets]
        if "lead_time_days" in self.case["negotiable"] and all(isinstance(x, int | float) for x in leads):
            self.open_lead = self.open_lead or max(leads)
            if max(leads) < self.open_lead and self.bounds.get("lead_time_days", ("<=", 1e9))[1] > max(leads):
                self.bounds["lead_time_days"] = ("<=", max(leads))  # a shorter lead time it granted stays ours
                self.refresh()
        if any(t.get("index_clause") for t in sheets):
            self.clause_seen = True
            self.want_clause()  # it asks for a clause: now it is worth something to it
        values = [self.est.value(self.case, t) for t in sheets]
        self.asks.append(min(values))
        self.ask_sheets.append([dict(t) for t in sheets])
        if len(self.asks) == 1:
            self.est.open_value = self.asks[0]
            self.refresh()  # the opening quote caps what we believe its full cost to be
        for t in sheets:
            self.observe(t, status)
        self.ask_value = self.asks[-1]
        return self._adapt_strategy(status)

    def _adapt_strategy(self, status):
        """Use the one preselected fallback only when live bargaining evidence changes the prior. Not on a final
        offer (no further move for a new plan to shape; `BuyerAgent.decide` accepts or walks) and not while it
        said it will come back on price (its next step is coming anyway)."""
        if self.strategy_switched or len(self.asks) < 2 or self.second_step or status in ("final_offer", "agreed"):
            return None
        opening = max(self.asks[0], 1e-9)
        if self.strategy == FAST_CLOSE:
            step = self.asks[-2] - self.asks[-1]
            if step >= 0.04 * opening:
                return self._switch_to_fallback(
                    f"the supplier conceded {step / opening:.1%} of its opening on one reply"
                )
            return None
        if len(self.asks) >= 3:
            threshold = 0.0015 * opening
            recent = (self.asks[-3] - self.asks[-2], self.asks[-2] - self.asks[-1])
            if all(abs(step) <= threshold for step in recent):  # a raised ask is not "negligible"
                return self._switch_to_fallback("two consecutive supplier replies showed negligible movement")
        return None

    def want_clause(self):
        """An index clause is worth something to this supplier from now on."""
        if not self.est.wants_clause:
            self.est.wants_clause = True
            self._revalue()  # arena v3 P08: old asks on the old model inflated its limit estimate
            self.refresh()

    def keep(self, field, value):
        """A term it says is mandatory: every package keeps its value (the strategist read it in its e-mail;
        `locked` only learns it after two replies)."""
        if field in self.case["negotiable"] and field != PRICE and self.kept.get(field, object()) != value:
            self.kept[field] = value
            self.refresh()

    def _revalue(self):
        """Our model of what terms are worth to it changed: value its earlier asks and our last offer again."""
        self.asks = [min(self.est.value(self.case, t) for t in shs) for shs in self.ask_sheets]
        if self.asks:
            self.ask_value = self.asks[-1]
            self.est.open_value = self.asks[0]
        if self.last_sheets:
            self.last_value = max(self.est.value(self.case, t) for t in self.last_sheets)

    def observe(self, standing_terms, status):
        """Its standing sheet is something it will sign: its floor is at most that value."""
        if not standing_terms or standing_terms.get(PRICE) is None:
            return
        v = self.est.value(self.case, standing_terms)
        self.ask_value = v if self.ask_value is None else min(self.ask_value, v)
        changed = False
        if status == "final_offer" or (v < self.est.floor_value):
            if self.est.cap is None or v < self.est.cap:
                self.est.cap, changed = v, True
        if changed:
            self.refresh()

    # -- decisions --------------------------------------------------------------------------------
    def reservation(self, c_open):
        return min(self.c_alt, c_open) if c_open and math.isfinite(c_open) else self.c_alt

    def accept_now(self, c_standing, k_next, c_open, rounds_left):
        if c_standing is None or not math.isfinite(c_standing) or c_standing > self.reservation(c_open):
            return False
        nxt = self.sheets(k_next, n=1, rounds_left=rounds_left)
        c_next = cost(self.case, nxt[0], self.est) if nxt else math.inf
        return c_standing <= c_next * 1.002 or rounds_left <= 1
