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

from arena_kit.cost import RELATIONSHIP_GOALS, closing_terms, must_haves_ok, supplier_value, total_cost

PRICE = "unit_price"


class Estimate:
    """Our model of one supplier: what it costs, what money is worth to it, what it needs."""

    def __init__(self, dossier, standard=None):
        self.d = dossier
        self.full_cost, self.err = dossier.full_cost
        self.rate = dossier.cash_rate
        self.min_margin = dossier.min_margin
        self.standard = dict(dossier.standard_guess)
        self.set_standard(standard)
        self.cap = None  # the lowest supplier value it has shown it will sign (from its own offers)
        self.wants_clause = False  # set when its own sheets carry an index clause
        # relationship charge: trusted only for a supplier the data shows under water
        self.rel_weight = 1.0 if dossier.has("below_full_cost") else 0.1

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

    def extra(self, t):
        """€ per unit of non-cash value a package has for this supplier (its persona), our guess."""
        p, d, x = self.d.reference_price or 1.0, self.d, 0.0
        months = t.get("contract_months") or 0
        if months >= 24 and (d.has("free_capacity") or d.has("small_account") or d.has("price_frozen")):
            x += 0.008 * p
        std_lead = self.standard.get("lead_time_days")
        if std_lead and t.get("lead_time_days") and t["lead_time_days"] < std_lead:
            x -= min(0.12, 0.005 * (std_lead - t["lead_time_days"])) * p  # arena: ~0.5 %/day faster
        if d.has("capacity_tight") and months >= 24:
            x += 0.01 * p
        if t.get("index_clause") and self.wants_clause:
            x += 0.02 * p  # raw-material distributors trade price for it; others never asked (arena P02)
        if months >= 36:
            x += 0.02 * p
        elif months >= 24:
            x += 0.01 * p
        real = (d.scorecard or {}).get("ppm") or 0
        commit = t.get("ppm_commitment")
        if commit is not None and real and commit < real:
            x -= 0.03 * p * (1 - commit / real)  # promising better quality than it achieves is a risk to it
        return x

    def model(self, case, t):
        std = {**self.standard, **case.get("fixed_terms", {})}
        out = {"standard": std, "interest_rate": self.rate, "full_cost": self.full_cost, "extra": self.extra(t)}
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


def grid(case, std):
    """Every combination of the negotiable non-price fields allowed by the case."""
    fields = [f for f in case["negotiable"] if f != PRICE]
    opts = []
    for f in fields:
        o = list(case.get("term_options", {}).get(f) or [])
        if not o:
            o = [std.get(f)] if std.get(f) is not None else [closing_terms(case, {}, std).get(f)]
        elif f in std and std[f] not in o and isinstance(std[f], int | float):
            o.append(std[f])  # a term sheet may carry the supplier's standard value (arena: P10 lead time 54)
        opts.append(o)
    for combo in itertools.product(*opts):
        yield dict(zip(fields, combo, strict=True))


def cost(case, terms, est, rel=True):
    """C for us. `rel=False` leaves out the relationship charge, whose size rests on our estimate of
    the supplier's cost (arena: often 10-30 % off), so it is weighted by `est.rel_weight`."""
    base = total_cost(case, terms, est.standard)
    if not rel or not est.rel_weight:
        return base
    t = closing_terms(case, terms, est.standard)
    full = total_cost(case, terms, est.standard, est.model(case, t))
    return base + est.rel_weight * (full - base)


class Package:
    __slots__ = ("combo", "p_floor", "p_opt", "c_opt")

    def __init__(self, combo, p_floor, p_opt, c_opt):
        self.combo, self.p_floor, self.p_opt, self.c_opt = combo, p_floor, p_opt, c_opt


class Planner:
    """Per supplier thread: packages, the concession path, accept and walk."""

    def __init__(self, case, est, horizon_rounds=6):
        self.case, self.est, self.rounds = case, est, horizon_rounds
        alt = case["buyer"].get("alternative") or {}
        self.c_alt = total_cost(case, alt["terms"]) if alt.get("terms", {}).get(PRICE) else math.inf
        self.frozen = est.d.current if est.d.has("price_frozen", strong=True) else None
        self.relationship = case["buyer"].get("category_goal") in RELATIONSHIP_GOALS
        self.last_value = None  # supplier value of our best sheet in the last message
        self.ask_value = None  # supplier value of its own standing sheet: we never offer more than that
        self.cap_c = math.inf  # the most a package may cost us (set by the agent: the reservation)
        self.anchor_value = self._anchor()
        self.anchor_gap = self._anchor_gap()
        self.asks = []  # supplier value of its best standing sheet, one entry per reply
        self.locked = {}  # terms the supplier keeps insisting on (e.g. a mandatory index clause)
        self.seen_terms = {}  # field -> set of values the supplier has offered
        self.replies = 0
        self.refresh()

    def _anchor_gap(self):
        """How far below the supplier's ask we open, in value terms. Fitted to the real arena's revealed
        C_best: leverage-type cases (RFQ, raw material, bundling, capacity) had 12-30 % room, routine and
        strategic renewals and price letters only 0.3-5 %."""
        ctype, goal = self.case.get("type"), self.case["buyer"].get("category_goal")
        # arena (13 sessions): the supplier's first counter is the same whether we open 5 % or 20 % below,
        # and deep openings only bring its final offer sooner (P10: final in round 1 after -19 %)
        gap = {
            "rfq": 0.12,
            "raw_material": 0.10,
            "bundling": 0.10,
            "capacity": 0.10,
            "quality": 0.10,
            "price_increase": 0.06,
            "renewal": 0.06,
        }.get(ctype, 0.08)
        d = self.est.d
        if ctype in ("renewal", "price_increase") and goal in ("stabilise", "routine", "secure_supply"):
            gap = min(gap, 0.025)
        if ctype == "price_increase" and d.notice:
            gap = max(gap, 0.06)
        if ctype == "quality" or d.has("cash_short", strong=True):
            gap = min(gap, 0.05)  # quality cases are about ppm and chargebacks, not a price cut to a cash-short shop
        if ctype == "capacity" and d.has("capacity_tight"):
            gap = min(gap, 0.08)  # no alternative and a +25 % fallback: commitment, not a squeeze
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
        for combo in grid(self.case, self.est.standard):
            if any(combo.get(f) != v for f, v in self.locked.items() if f in combo):
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
            if lim is None:
                target = max(self.last_value + 0.004 * v0, ask - 0.015 * v0)
            else:
                nxt = ask - 0.4 * max(0.0, ask - lim)
                aim = min(lim - 0.003 * v0, nxt - 0.002 * v0)
                step = 0.0025 * v0 if self.last_value + 0.0025 * v0 <= aim else 0.0012 * v0  # small near its limit
                target = max(self.last_value + step, aim)
        target = max(target, 0.75 * self.est.floor_value)
        if self.last_value:
            target = max(target, self.last_value * 1.003)  # never a step back
        return min(target, ask * 0.997)

    def price_at(self, pkg, k, rounds_left=10):
        """Our price for a package in round k, at this round's supplier-value target."""
        target = self.target_value(k, rounds_left)
        if (
            self.relationship and self.est.rel_weight >= 1 and self.last_value
        ):  # from round 2: the cheapest price may sit higher
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
        c0, best = cand[0]
        out, vals = [best], [self.est.value(self.case, best)]
        for c, t in cand[1:]:
            if len(out) >= n or c > c0 * 1.03:
                break
            same = self.reprice_to_cost(t, c0)
            if not same or not (0.85 * best[PRICE] <= same[PRICE] <= 1.15 * best[PRICE]):
                continue
            if self.ask_value and not self.fair_price_case and self.est.value(self.case, same) > self.ask_value:
                continue
            v = self.est.value(self.case, same)
            if all(abs(v - u) / u > 0.002 for u in vals):  # a real alternative for the supplier
                out.append(same)
                vals.append(v)
        return out

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
        return v0 - max(0.0, v0 - a1) / rho

    def near_floor(self):
        """Its ask is within 15 % of the room above our floor estimate: take it."""
        f = self.f_hat
        if f is None or not self.asks or not self.ask_value:
            return False
        v0 = self.asks[0]
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
            self.last_value = max(self.est.value(self.case, t) for t in sheets)

    def reprice_to_cost(self, terms, target_c):
        lo, hi = terms[PRICE] * 0.6, terms[PRICE] * 1.6  # C misbehaves far from real prices: stay near
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
        if any(t.get("index_clause") for t in sheets) and not self.est.wants_clause:
            self.est.wants_clause = True  # it asks for a clause: now it is worth something to it
            self.refresh()
        values = [self.est.value(self.case, t) for t in sheets]
        self.asks.append(min(values))
        for t in sheets:
            self.observe(t, status)
        self.ask_value = self.asks[-1]

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
