"""TwinReal: the local supplier twin calibrated on the hosted arena's REAL suppliers.

`buyer.twin.Twin` guessed its suppliers' economics and concedes far too easily. TwinReal keeps its
plumbing (the kit's API, clock, sessions, claim checks, quote PDFs) and replaces the supplier's
decision with a model fitted to the real practice transcripts in `runs/arena-P*.json`:

  value       what a term sheet is worth to the supplier: `cost.supplier_value` at its own cost of
              money (fitted from its two-sheet MESO answers, ~7 %/yr), the real defect rate for
              chargebacks, plus a premium for a lead time shorter than its standard (capacity-tight)
              and a little for more volume or a costly ppm commitment
  floor F     the lowest value it ever signs, backed out of the arena's c_best (hosted cases) or the
              generic floor rule (`generic_floor`, fitted on those cases)
  limit L     where it really stops in a negotiation (its final offer), between F and the opening:
              fitted per hosted supplier by replaying the real buyer e-mails, generic L = v0 - lam*(v0-F)
  dynamics    round 1: concede RHO1 of the way to L; later rounds: RHO of the rest, only when the
              buyer's best sheet moved up; verified data arguments shift ask and limit (never below F);
              final offer when converged, when the buyer is close or far after round 3, when the buyer
              stalls after a hold, or at once for a capacity-tight supplier facing a low anchor;
              walks away after a final offer when the buyer breaks a mandatory term or pushes >3 %
              below it (or keeps pushing); answers with two iso-value sheets (MESO) in early rounds
  mandatory   raw-material distributors: index clause for any contract over 12 months;
              capacity-tight suppliers: lead time no shorter than its standard minus 12 days

Scores use the arena's own c_open / c_best for the hosted cases, so a twin score is comparable to
a real one. No LLM, no network.

    uv run python -m buyer.twin_real replay [-v]       # real buyer e-mails -> twin replies vs real replies
    uv run python -m buyer.twin_real validate          # + hold-out sessions and the generic supplier
    uv run python -m buyer.twin_real calibrate --write # refit from runs/arena-*.json, rewrite the data block
    uv run python -m buyer.twin_real bench [--cases P01,P08]   # our agent (template writer) on the hosted cases
    uv run python -m buyer.twin_real serve --port 8002 # HTTP, like `buyer.twin serve`
"""

import argparse
import copy
import glob
import itertools
import json
import math
import re
import statistics
import time
from pathlib import Path

from arena_kit.cost import (
    RELATIONSHIP_GOALS,
    closing_terms,
    cost_breakdown,
    must_haves_ok,
    score,
    supplier_value,
    total_cost,
)
from arena_kit.sim import ID_RE, Fail, Sim, create_app, load_cases, pdf, structured

from .twin import LEAK, THREAT, LocalClient, Twin, World, _labels, supplier_profile

ROOT = Path(__file__).resolve().parents[1]
PDF = "application/pdf"

# -- global behaviour, fitted on the transcripts (see runs/twin_real_calibration.md) -----------------
RHO1 = 0.75  # round 1: share of the way from its ask to its limit it concedes
RHO = 0.40  # later rounds, when the buyer moved: share of the remaining way
RHO1_CASH = 0.35  # a supplier short of cash "cannot give up margin right now"
MOVE_TOL = 0.001  # the buyer's best sheet must rise by 0.1 % of the opening value to count as a move
EPS = 0.005  # ask within 0.5 % of its limit: converged -> final offer at the limit
CLOSE = 0.015  # from round 3, buyer within 1.5 % of the ask -> one last step to the limit, final
FAR = 0.10  # from round 3, buyer more than 10 % below the ask -> final offer at the limit
CT_FAR = 0.18  # capacity-tight supplier, round 1, buyer more than 18 % below -> final at once
WALK_GAP = 0.03  # after a final offer: a sheet more than 3 % below it ends the thread
PATIENCE = 2  # after a final offer: the second push below it ends the thread
MAX_ROUNDS = 6  # final offer at the latest after this many buyer messages with term sheets
DELTA = 0.005  # a verified argument that "we took into account": ask and limit -0.5 % of the opening
DELTA_FAIR = 0.0075  # "We did not know that - a fair point" (stale costing): -0.75 %
MAX_CLAIMS = 3
RIVAL_FAR = 0.10  # a rival price this much below its own: the two-step (half now, the rest next round)
LAM = 0.45  # generic limit: this share of the way from the opening value to the floor (the data block sets the fit)
LAM_CT = 0.10  # capacity-tight suppliers give much less (P10, P05 70240)
LEAD_PREMIUM = 0.006  # capacity-tight: share of the opening price per day of lead time below standard
LEAD_SLACK = 12  # capacity-tight: shortest lead time it accepts = standard - 12 days
VOLUME_BONUS = 0.05  # +10 % volume -> +0.5 % of price worth to it (fixed-cost absorption); 0 if capacity-tight
PPM_WORK = 0.03  # committing to a ppm below its real rate costs 3 % of price per 100 % of the gap
RATE = 0.07  # its cost of money per year (MESO answers 6.6-8.3 %; the P01 and P03 relationship charges: 7 %)
RATE_CASH = {"cash_short_hard": 0.30, "cash_short_weak": 0.15}
FULL_COST_FACTOR = 0.979  # true full cost = should-cost x 0.979 (relationship charge of P01 and P03 deals)

# -- calibration: fitted by `calibrate` from runs/arena-*.json; per hosted case and supplier ----------
# per case: the arena's c_open / c_best / c_alt; per supplier: opening sheet, standard terms (quote PDF,
# inferred from c_open and the closing terms of deals), rate, price grid, lead premium, floor and
# limit as shares of the opening value. Regenerate with `uv run python -m buyer.twin_real calibrate`.
CALIB = {}  # filled below the class definitions (CALIB.update(...))


# -- small helpers ------------------------------------------------------------------------------------
def _path(p):
    return Path(p) if Path(p).exists() else ROOT / p


def _fields(case):
    """The negotiable fields except the price, in the case's order."""
    return [f for f in case["negotiable"] if f != "unit_price"]


def _sheet(case, terms):
    return {f: terms.get(f) for f in case["negotiable"]}


def _round_to(x, grid):
    return round(round(x / grid) * grid, 2)


def _short(t):
    """A term sheet in a few characters, for tables."""
    out = []
    for k, v in t.items():
        if k == "unit_price":
            continue
        if k == "index_clause":
            v = "none" if not v else f"idx{v['deadband_pct']:g}"
        out.append(f"{k[:4]}={v}")
    return f"{t.get('unit_price')}[{' '.join(out)}]"


# -- the supplier's economics -------------------------------------------------------------------------
def extra(p, case, terms):
    """EUR per unit beyond `cost.supplier_value`'s items: a lead time below its standard costs a
    capacity-tight plant, more volume fills fixed costs, a ppm commitment below its real rate costs work,
    an index clause on a moving series passes the expected change on (linear in the price)."""
    t, ref, x = closing_terms(case, terms, p["standard"]), p["ref"], 0.0
    lead, std_lead = t.get("lead_time_days"), p["standard"].get("lead_time_days")
    if p["lead_premium"] and lead is not None and std_lead is not None and lead < std_lead:
        x -= p["lead_premium"] * (std_lead - lead) * ref
    vol, std_vol = t.get("volume_per_year"), p["standard"].get("volume_per_year")
    if "volume_per_year" in case["negotiable"] and vol and std_vol:
        dv = vol / std_vol - 1
        if "capacity_tight" not in p["traits"]:
            x += VOLUME_BONUS * dv * ref
        elif (t.get("contract_months") or 12) < 24 and dv > 0:
            x -= VOLUME_BONUS * dv * ref
    ppm = t.get("ppm_commitment")
    if ppm is not None and p["real_ppm"] and ppm < p["real_ppm"]:
        x -= PPM_WORK * ref * (p["real_ppm"] - ppm) / p["real_ppm"]
    if clause := t.get("index_clause"):  # the expected pass-through: what the buyer pays in C, it receives
        path = case["buyer"].get("index_paths", {}).get(clause["series"], 0.0)
        moved = max(0.0, abs(path) - clause["deadband_pct"] / 100) * math.copysign(1, path)
        x += (t.get("unit_price") or 0.0) * clause["material_share"] * moved
    return x


def who(p, case, terms):
    """The supplier as `cost.supplier_value` / the relationship charge take it."""
    out = {"standard": p["standard"], "interest_rate": p["rate"], "extra": extra(p, case, terms)}
    if p["real_ppm"]:
        out["ppm"] = p["real_ppm"]
    if p.get("full_cost"):
        out["full_cost"] = p["full_cost"]
    return out


def value(p, case, terms):
    return supplier_value(case, terms, who(p, case, terms))


def price_for(p, case, terms, target):
    """The unit price that makes these terms worth `target` to the supplier (value is linear in it)."""
    v0, v1 = (value(p, case, {**terms, "unit_price": x}) for x in (0.0, 100.0))
    slope = (v1 - v0) / 100
    return None if slope <= 1e-9 else (target - v0) / slope


def needs_index(p, case, terms):
    """Raw-material distributors: no fixed price beyond 12 months without an index clause."""
    if not p["index_mandatory"]:
        return False
    t = closing_terms(case, terms, p["standard"])
    return (t.get("contract_months") or 12) > 12 and not t.get("index_clause")


def lead_min(p, case):
    """The shortest lead time a capacity-tight supplier accepts (an allowed option), else None."""
    std = p["standard"].get("lead_time_days")
    if not p["lead_premium"] or std is None or "lead_time_days" not in case["negotiable"]:
        return None
    floor = std - LEAD_SLACK
    opts = sorted(o for o in case["term_options"].get("lead_time_days") or [] if o >= floor)
    return min([o for o in opts] + [std])


def repair(p, case, terms, sheets=(), standing=None):
    """(terms it could sign, whether the buyer's sheet already kept its mandatory terms)."""
    t, ok = dict(terms), True
    if needs_index(p, case, t):
        ok = False
        offered = [s.get("index_clause") for s in [standing or {}, *sheets] if s.get("index_clause")]
        opts = [o for o in case["term_options"].get("index_clause") or [] if o]
        t["index_clause"] = offered[0] if offered else (opts[0] if opts else None)
    lm = lead_min(p, case)
    if lm is not None and t.get("lead_time_days") is not None and t["lead_time_days"] < lm:
        ok, t["lead_time_days"] = False, lm
    return t, ok


def grid(p, case):
    """Every package of the non-price negotiable terms (the options plus its standard) it could sign."""
    fields, choices = _fields(case), []
    for f in fields:
        vals = list(case["term_options"].get(f) or [])
        if p["standard"].get(f) not in vals:
            vals.append(p["standard"].get(f))
        choices.append(vals)
    for combo in itertools.product(*choices):
        pkg = dict(zip(fields, combo, strict=True))
        if repair(p, case, pkg)[1]:
            yield pkg


def _ternary(f, lo, hi, it=60):
    for _ in range(it):
        a, b = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        if f(a) < f(b):
            hi = b
        else:
            lo = a
    return (lo + hi) / 2


def best_cost(p, case, floor_value):
    """(C, sheet): the cheapest package for the buyer this supplier signs at `floor_value` - where
    the relationship charge applies, the price above the floor that minimises C (it is convex)."""
    b, best = case["buyer"], (math.inf, None)
    rel = b.get("category_goal") in RELATIONSHIP_GOALS and b.get("relationship_eur_per_point") and p.get("full_cost")
    for pkg in grid(p, case):
        if not must_haves_ok(case, {**pkg, "unit_price": 1.0}, p["standard"]):
            continue
        lo = price_for(p, case, pkg, floor_value)
        if lo is None:
            continue

        def c(x, pkg=pkg):
            s = {**pkg, "unit_price": x}
            return sum(cost_breakdown(case, s, p["standard"], who(p, case, s) if rel else None).values())

        x = lo
        if rel:
            hi = price_for(p, case, pkg, p["full_cost"] / (1 - b.get("sustainable_margin_pct", 0) / 100))
            if hi and hi > lo:
                x = _ternary(c, lo, hi)
        cx = c(x)
        if cx < best[0]:
            best = (cx, _sheet(case, {**pkg, "unit_price": x}))
    return best


# -- profiles -------------------------------------------------------------------------------------------
def generic_floor(case, p):
    """The generic floor rule: the floor as a share of the opening value, by case type (the mean of
    the hosted cases of that type, fitted by `fit_generic`); the overall mean for other types."""
    return FLOOR_BY_TYPE.get(case["type"], FLOOR_DEFAULT)


FLOOR_BY_TYPE = {}  # filled by the calibration block below
FLOOR_DEFAULT = 0.90


def generic_opening(world, case, sup, base):
    """Opening price for a supplier the transcripts do not cover (share of the current price by type)."""
    cur = case["buyer"].get("current_unit_price") or case["buyer"]["fallback_unit_price"]
    if case["type"] == "price_increase":
        return base["opening_price"]  # the price it asked for in its letter
    if case["type"] == "rfq":
        return round(base["full_cost"] * 1.02, 2)
    if case["type"] == "bundling":
        return round(cur * 0.92, 2)
    return round(cur * OPEN_BY_TYPE.get(case["type"], 1.075), 2)


OPEN_BY_TYPE = {"renewal": 1.095, "raw_material": 1.08, "capacity": 1.077, "quality": 1.041}


def real_profile(world, case, sup, calib=None, seed=0):
    """The hidden model of one supplier in one case: the twin's persona profile (traits, drivers,
    real ppm), with the calibrated opening, standard terms, cost of money, floor and limit."""
    base = supplier_profile(world, case, sup, seed)
    cal = ((calib if calib is not None else CALIB).get(case["case_id"]) or {}).get("suppliers", {}).get(sup)
    traits = list(base["traits"])
    std = dict(base["standard"])
    std.pop("unit_price", None)
    for f in ("ppm_commitment", "chargeback_eur_per_complaint"):
        if f not in std and f in case["negotiable"]:
            std[f] = 0 if f.startswith("charge") else base["standard"].get(f)
    opening = None
    ct = "capacity_tight" in traits and "lead_time_days" in case["negotiable"]
    if ct and not cal:  # real capacity-tight openings: its usual lead time + 10-12 days (P10, P05 70240)
        row = world.one("SELECT standard_lead_time_days FROM suppliers WHERE id=?", sup) or {}
        if row.get("standard_lead_time_days"):
            std["lead_time_days"] = row["standard_lead_time_days"] + LEAD_SLACK
    if cal:
        std.update(cal["standard"])
        opening = dict(cal["open"])
    rate = RATE  # hosted suppliers: the arena's numbers show ~7 % even where the data hint at cash needs
    for t, r in RATE_CASH.items():
        if t in traits and (not cal or t == "cash_short_hard"):
            rate = max(rate, r)
    if cal and cal.get("rate"):
        rate = cal["rate"]
    sc = world.one(
        "SELECT unit_cost, price_unit FROM should_cost WHERE part_id=? AND supplier_id=?", case["parts"][0], sup
    )
    full = FULL_COST_FACTOR * sc["unit_cost"] / (sc.get("price_unit") or 1) if sc else base["full_cost"]
    p = {
        "supplier_id": sup, "name": base["name"], "country": base["country"], "owner": base["owner"],
        "role": base["role"], "traits": traits, "drivers": base["drivers"], "real_ppm": base["real_ppm"],
        "standard": std, "rate": rate, "full_cost": full, "cost_split": base["cost_split"],
        "index_mandatory": case["type"] == "raw_material" and "index_clause" in case["negotiable"],
        "lead_premium": (cal or {}).get("lead_premium", LEAD_PREMIUM) if ct else 0.0,
        "frozen": "price_frozen" in traits, "cash": "cash_short_hard" in traits,
        "round_to": (cal or {}).get("round_to", 0.01), "hosted": bool(cal),
        "hedge_until": base["hedge_until"], "approval": base["approval"],
        "stale": "stale_costing" in traits,
    }  # fmt: skip
    if opening is None:
        price = generic_opening(world, case, sup, base)
        quoted = {f: std.get(f) for f in _fields(case)}
        if "contract_months" in quoted:
            quoted["contract_months"] = 12
        opening = _sheet(case, {**quoted, "unit_price": price})
    p["ref"] = opening["unit_price"]
    p["opening"] = opening
    p["v0"] = value(p, case, opening)
    share = (cal or {}).get("floor") or (cal or {}).get("floor_generic") or generic_floor(case, p)
    p["floor"] = share * p["v0"]
    lim = cal.get("limit") if cal else None
    lam = LAM_CT if p["lead_premium"] else LAM
    p["limit"] = (lim * p["v0"]) if lim else p["v0"] - lam * (p["v0"] - p["floor"])
    p["limit"] = max(p["limit"], p["floor"])
    return p


BEAT_ALT = 0.95  # unhosted cases: the best deal costs at most 95 % of the alternative (hosted: 74-97 %)


def _beat_alternative(case, profs):
    """Generic suppliers only: lower the floors (and limits) of a case until the cheapest deal any
    supplier signs costs at most BEAT_ALT x the buyer's alternative - in every hosted case c_best was
    3-26 % below c_alt; a twin whose best deal loses to the alternative would reward walking away."""
    alt = (case["buyer"].get("alternative") or {}).get("terms") or {}
    if not alt.get("unit_price"):
        return
    target = BEAT_ALT * total_cost(case, alt)

    def c_at(g):
        return min(best_cost(p, case, g * p["floor"])[0] for p in profs)

    if c_at(1.0) <= target:
        return
    lo, hi = 0.5, 1.0
    for _ in range(30):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if c_at(mid) <= target else (lo, mid)  # feasible: try a higher floor
    for p in profs:
        lam = LAM_CT if p["lead_premium"] else LAM
        p["floor"] *= lo
        p["limit"] = max(p["floor"], p["v0"] - lam * (p["v0"] - p["floor"]))


# -- the arena with these suppliers -----------------------------------------------------------------
QUESTION_VOLUME = "A binding higher volume would help us and can improve the price. Which volume can you commit to?"
QUESTION_DATA = "Which figures is that based on? Please send us the data."
FIGURES = re.compile(r"[-+]?\d+(?:[.,]\d+)?\s*%|\bforecast|\bplanning security\b|\bmarket (?:price|level)", re.I)


class TwinReal(Twin):
    """arena-sim with suppliers calibrated on the real arena. Same API, clock, errors and replays."""

    def __init__(self, cases, world, seed=0, calib=None, event_code="sim", clock=time.time, no_clock=False):
        super().__init__(cases, world, seed=seed, event_code=event_code, clock=clock, no_clock=no_clock)
        self.calib = CALIB if calib is None else calib

    def profile(self, case, sup):
        key = (case["case_id"], sup)
        if key not in self._profiles:
            profs = {s: real_profile(self.world, case, s, self.calib, self.seed) for s in case["suppliers"]}
            if not any(p["hosted"] for p in profs.values()):
                _beat_alternative(case, list(profs.values()))
            self._profiles.update({(case["case_id"], s): p for s, p in profs.items()})
        return self._profiles[key]

    # -- sessions ----------------------------------------------------------------------------------------
    def open(self, team, case_id):
        r = Sim.open(self, team, case_id)  # every check, limit and the session itself; then our openings
        s = self.sessions[r["session_id"]]
        case = s["case"]
        s["offers"].clear()
        s["files"].clear()
        for sup, th in s["threads"].items():
            p = self.profile(case, sup)
            th["messages"].clear()
            sheet = {k: v for k, v in p["opening"].items() if v is not None or k == "index_clause"}
            th.update(n=0, standard=dict(p["standard"]), opening=p["ref"], alt=None)
            th["twin"] = {"p": p, "A": p["v0"], "L": p["limit"], "best": None, "hold": False, "final": False,
                          "pushes": 0, "trust": 2, "halve": False, "claims": 0, "k": 0}  # fmt: skip
            self._mail(s, th, "open", sheet, self._letter(p, self._opening_lines(case, p, sheet)), ["quote.pdf"])
            why = [f"traits: {', '.join(p['traits']) or 'none'}; cost of money {p['rate']:.1%}",
                   f"opening value {p['v0']:.2f}, limit {p['limit']:.2f} ({p['limit'] / p['v0']:.3f}), "
                   f"floor {p['floor']:.2f} ({p['floor'] / p['v0']:.3f})"]  # fmt: skip
            s["trace"].append({"round": 0, "actor": sup, "move": structured(th["messages"][0], 0), "reasons": why})
        return {
            "session_id": s["id"],
            "case": case,
            "opening": {k: structured(t["messages"][0], 0) for k, t in s["threads"].items()},
            "threads": {k: list(t["messages"]) for k, t in s["threads"].items()},
        }

    def message(self, team, sid, sup, msg):
        action = msg.get("action") or {}
        if action.get("type") == "accept":  # the alternative sheet (offer:...-a) can be accepted too
            s = self.session(team, sid)
            for th in s["threads"].values():
                if th.get("alt") and action.get("term_sheet_id") == th["alt"][0] and not th["closed"]:
                    th["stated"], th["offer_id"] = dict(th["alt"][1]), th["alt"][0]
        return super().message(team, sid, sup, msg)

    def _mail(self, s, th, status, stated, text, files=(), alt=None):
        msg = super()._mail(s, th, status, stated, text, files)
        th["alt"] = None
        if alt and status in ("open", "final_offer"):
            aid = f"{th['offer_id']}-a"
            th["alt"] = (aid, dict(alt))
            s["offers"][aid] = dict(alt)
            msg["term_sheets"].append({"term_sheet_id": aid, "terms": dict(alt)})
        return msg

    # -- the supplier's decision ---------------------------------------------------------------------------
    def _claims(self, s, th, msg):
        """The kit's checks, plus what the arena does on top (v3 sessions, P01, P07, P11): a supplier counts
        each kind of argument once - a second forecast (another kit part) or a second scorecard row comes back
        `duplicate`, "we have already taken that volume change data into account". Index series count
        separately; rival offers and quotes per ID."""
        results, found = super()._claims(s, th, msg)
        seen = th["twin"].setdefault("kinds", set())
        for i, (res, info) in enumerate(found):
            if res != "verified" or not info or info["type"] in ("rival", "own"):
                continue
            key = (info["type"], info.get("series"))
            if key in seen:
                found[i] = ("duplicate", None)
                results[i] = {**results[i], "result": "duplicate"}
            seen.add(key)
        return results, found

    def _claim_moves(self, p, case, info, standing):
        """(share of the opening value it moves, what it says) for one verified argument."""
        kind, tr = info["type"], set(p["traits"])
        if kind in ("news", "own", "orders"):
            return 0.0, None
        if kind == "index":
            if "hedged_material" in tr:
                return (
                    0.0,
                    f"That is not our reality: our material is covered at fixed prices until {p['hedge_until']}.",
                )
            if info["change"] > 0:
                return 0.0, "Your figures confirm our case: that driver rose."
            if p["stale"]:
                return DELTA_FAIR, "We did not know that - a fair point."
            if p["cash"]:
                return 0.0, "We see your figures, but we cannot give up margin right now."
            return DELTA, None
        if kind == "rival":
            if info["price"] >= standing:
                return 0.0, None
            return (2 * DELTA if case["type"] == "rfq" else DELTA), None
        if kind == "forecast":
            if tr & {"capacity_tight", "small_account"}:
                return 0.0, None
            return (DELTA_FAIR if p["stale"] else DELTA), (
                "We did not know that - a fair point." if p["stale"] else None
            )
        if kind == "scorecard":
            if "capacity_tight" in tr:
                return 0.0, None
            if info.get("metric") == "otd":  # arena, 23 sessions: "the delivery record ... does not change our
                # calculation" 6 of 6 times; a supplier on an old cost sheet: "we did not know that - a fair point"
                return (DELTA_FAIR, "We did not know that - a fair point.") if p["stale"] else (0.0, None)
            return DELTA, None
        if kind in ("cost", "notice"):
            return DELTA, None
        return 0.0, None

    def _answer(self, s, th, msg, sheets):
        case, rt = s["case"], th["twin"]
        p = rt["p"]
        v0, body = p["v0"], msg.get("body") or ""
        plain = ID_RE.sub(" ", body)
        why, say = [], []
        results, found = self._claims(s, th, msg)
        shift = 0.0
        if rt.get("rest") and not rt["final"]:  # "Checked: give the rest of the step" (arena P05 back office)
            shift += rt.pop("rest")
            say.append("We have clarified this internally and can move further on the price.")
        for res, info in found:
            if res == "contradicted":
                rt["trust"] -= 1
                rt["halve"] = True
                say.append("The figures you cited do not match our information; that does not help our trust.")
            elif res == "duplicate":
                say.append("We have already taken that data into account.")
            elif res == "irrelevant":
                say.append("Some of the data you cited does not concern our part or our company.")
            elif (
                res == "verified"
                and info
                and info["type"] == "rival"
                and not rt["final"]
                and ((stated := th["stated"].get("unit_price")) and info["price"] < stated * (1 - RIVAL_FAR))
            ):
                # arena P05 (b1, b2): "The buyer shows a much lower rival quote. I give half the step now and check
                # the rest" - half the gap to the rival's price now, the other half next round, never below its floor
                step = (stated - info["price"]) * v0 / stated
                shift += step / 2
                rt["rest"] = rt.get("rest", 0.0) + step / 2
                say.append("We need to clarify this internally - we will come back to you on price.")
            elif res == "verified" and info and not rt["final"]:
                d, line = self._claim_moves(p, case, info, th["stated"].get("unit_price") or math.inf)
                if d and rt["claims"] < MAX_CLAIMS:
                    shift += d * v0
                    rt["claims"] += 1
                    say.append(f"We checked the {info['type']} data you cited; it holds and we took it into account.")
                elif line is None:
                    say.append(f"We checked the {info['type']} data you cited; it holds, but it does not change "
                               "our calculation.")  # fmt: skip
                if line:
                    say.append(line)
        if FIGURES.search(plain) and not ID_RE.search(body):  # figures without data: they ask for it
            say.append(QUESTION_DATA)
        if shift:
            rt["L"] = max(p["floor"], rt["L"] - shift)
            rt["A"] = max(rt["L"], rt["A"] - shift)
            why.append(f"verified arguments: ask and limit -{shift / v0:.2%} of the opening value")

        if rt["trust"] < 0:
            why.append("trust below zero: walks away")
            bye = "We no longer trust the information we receive from you and end the negotiation here."
            return self._reply(s, th, "walked_away", None, None, results, why, [bye])
        if not sheets:  # a question: the offer stands
            lines = ["Thank you for your message. Our offer stands."] + say
            return self._reply(
                s, th, th["status"], th["stated"], None, results, why or ["a question"], lines, keep=True
            )

        cands = []
        for t in sheets:
            rep, ok = repair(p, case, t, sheets, th["stated"])
            cands.append({"terms": t, "rep": rep, "ok": ok, "v": value(p, case, rep)})
        compliant = [c for c in cands if c["ok"]]
        top = max(compliant or cands, key=lambda c: c["v"])
        bv = max(c["v"] for c in cands)
        mand = [c for c in cands if not c["ok"]]
        if mand and not compliant:
            if p["index_mandatory"]:
                say.append("We cannot fix prices beyond 12 months due to raw material volatility; "
                           f"the {top['rep']['index_clause']['series']} index clause is mandatory.")  # fmt: skip
            else:
                say.append(f"A lead time of {top['terms'].get('lead_time_days')} days is not feasible; "
                           f"the shortest we can do is {top['rep']['lead_time_days']} days.")  # fmt: skip

        # after a final offer: agree, repeat it, or walk
        if rt["final"]:
            if compliant and top["v"] >= rt["A"] - 1e-6:
                return self._agree(s, th, top["terms"], results, why, say)
            if not compliant:
                why.append("after its final offer the buyer broke a mandatory term: withdraws")
                bye = "You have changed the terms of our final offer; we withdraw our offer."
                return self._reply(s, th, "walked_away", None, None, results, why, [bye])
            rt["pushes"] += 1
            if top["v"] < rt["A"] * (1 - WALK_GAP) or rt["pushes"] >= PATIENCE:
                why.append(
                    f"push below its final offer ({top['v'] / rt['A'] - 1:+.1%}, push {rt['pushes']}): withdraws"
                )
                bye = "We have made our final offer; we see no basis to continue and withdraw it."
                return self._reply(s, th, "walked_away", None, None, results, why, [bye])
            why.append(f"push below its final offer ({top['v'] / rt['A'] - 1:+.1%}): repeats it")
            say.insert(0, "We have made our final offer. Please confirm it so we can proceed.")
            return self._reply(s, th, "final_offer", th["stated"], None, results, why, say, keep=True)

        # no walk on a step back: P05 b1 (27.58 -> 27.48 on the same terms) carried on to its final offer; the
        # P08 b1 withdrawal came at its limit after five rounds (the agent never steps back anyway)

        # the concession of this round
        rt["k"] += 1
        k, A, L = rt["k"], rt["A"], rt["L"]
        moved = rt["best"] is None or bv > rt["best"] + MOVE_TOL * v0
        firm = bool(LEAK.search(plain)) or (bool(THREAT.search(plain)) and p["owner"] in ("family", "founder"))
        gap0 = (A - bv) / v0
        ct_anchor = bool(p["lead_premium"]) and k == 1 and gap0 > CT_FAR
        if firm:
            A_new, how = A, "the buyer leaked urgency, a budget or a threat: firm this round"
            say.append("We note your situation. Our offer stands.")
        elif k == 1:
            rho = RHO1_CASH if p["cash"] else RHO1
            rho = rho / 2 if (rt["halve"] or ct_anchor) else rho
            A_new, how = A - rho * (A - L), f"round 1: {rho:.0%} of the way to its limit"
        elif p["frozen"]:
            A_new, how = A, "price frozen: holds"
        elif moved:
            rho = RHO / 2 if rt["halve"] else RHO
            A_new, how = A - rho * (A - L), f"the buyer moved: {rho:.0%} of the rest"
        else:
            A_new, how = A, "no move from the buyer: holds"
        rt["halve"] = False
        A_new = max(A_new, L)
        why.append(how)

        status, final_at = "open", None
        gap = (A_new - bv) / v0
        if compliant and top["v"] >= A_new - 1e-6:
            rt["best"] = max(bv, rt["best"] or bv)
            return self._agree(s, th, top["terms"], results, why, say)
        if ct_anchor:
            final_at, reason = A_new, "capacity-tight and a low anchor: final offer at once"
        elif p["lead_premium"] and k >= 2:
            final_at, reason = L, "capacity-tight: final offer from round 2"
        elif (A - L) <= EPS * v0 and k >= 2:
            final_at, reason = L, "converged on its limit: final offer"
        elif not moved and rt["hold"] and k >= 2:
            final_at, reason = A, "the buyer did not move after a hold: final offer"
        elif k >= 3 and not p["frozen"] and gap <= CLOSE:
            final_at, reason = L, "the buyer is close: one last step to its limit, final offer"
        elif k >= (4 if p["cash"] else 3) and not p["frozen"] and gap >= FAR:
            final_at, reason = L, "the buyer is far below after three rounds: final offer at its limit"
        elif k >= MAX_ROUNDS:
            final_at, reason = L, "enough rounds: final offer"
        if final_at is not None:
            A_new, status = final_at, "final_offer"
            why.append(reason)
            if compliant and top["v"] >= A_new - 1e-6:
                rt["best"] = max(bv, rt["best"] or bv)
                return self._agree(s, th, top["terms"], results, why, say)
        rt["hold"] = A_new >= A - 1e-9
        rt["A"], rt["best"] = A_new, max(bv, rt["best"] or bv)
        rt["final"] = status == "final_offer"

        main = self._priced(p, case, top["rep"], A_new)
        alt = self._alternative(p, case, cands, top, A_new, k, status)
        why.append(f"ask {A_new / v0:.4f} of its opening value (limit {L / v0:.4f}, floor {p['floor'] / v0:.4f}); "
                   f"buyer's best {bv / v0:.4f}")  # fmt: skip
        if p["lead_premium"] and main.get("lead_time_days", 99) < p["standard"].get("lead_time_days", 0):
            std_lead = p["standard"]["lead_time_days"]
            say.append(f"A shorter lead time costs us overtime; our standard lead time is {std_lead} days.")
        return self._reply(s, th, status, main, alt, results, why, say)

    def _priced(self, p, case, terms, target):
        """The sheet at the price worth `target` to it, on its price grid (nearest step)."""
        price = _round_to(max(price_for(p, case, terms, target), 0.01), p["round_to"])
        return _sheet(case, {**terms, "unit_price": price})

    def _alternative(self, p, case, cands, top, target, k, status):
        """A second sheet of equal value (MESO) in rounds 1-2: the buyer's next package, or the term the
        buyer changed at the supplier's preferred value (payment, effective date, lead time)."""
        if k > 2 or status not in ("open", "final_offer"):
            return None
        main = top["rep"]
        others = [c["rep"] for c in sorted(cands, key=lambda c: -c["v"]) if c["rep"] != main]
        if status == "open":
            for dim in ("payment_days", "effective_date", "lead_time_days"):
                std = p["standard"].get(dim)
                if dim in case["negotiable"] and std is not None and main.get(dim) != std:
                    if dim == "effective_date":
                        opts = sorted(case["term_options"].get(dim) or [])
                        std = next((o for o in opts if main[dim] and o < main[dim] and o > std), std)
                    others.append({**main, dim: std})
        first = self._priced(p, case, main, target)
        for o in others:
            if any(o.get(d) != main.get(d) for d in ("payment_days", "effective_date", "lead_time_days")):
                alt = self._priced(p, case, o, target)
                if abs(alt["unit_price"] - first["unit_price"]) > p["round_to"] / 2:  # a real trade-off only
                    return alt
        return None

    def _agree(self, s, th, terms, results, why, say):
        p, rt = th["twin"]["p"], th["twin"]
        rt["final"], rt["A"] = True, value(p, s["case"], terms)  # it stands by what it agreed, no lower
        why.append(f"agreed: the sheet is worth {value(p, s['case'], terms) / p['v0']:.4f} of its opening value")
        return self._reply(s, th, "agreed", _sheet(s["case"], terms), None, results, why, say)

    def _reply(self, s, th, status, stated, alt, results, why, say, keep=False):
        case, p = s["case"], th["twin"]["p"]
        files = []
        if status == "walked_away":
            th["closed"] = "walked_away"
            stated = th["stated"]
        if status in ("final_offer", "agreed") and not keep:
            files.append("quote.pdf")
        body = msg_body(case, p, status, stated, alt, say, th, keep)
        reply = self._mail(s, th, status, stated, self._letter(p, body), files, alt=alt)
        if status == "walked_away":
            reply["term_sheets"] = []
        reply["claim_results"] = results
        move = {**structured(reply, th["round"]), "alternatives": [t["terms"] for t in reply["term_sheets"][1:]]}
        s["trace"].append({"round": th["round"], "actor": f"sup:{th['key']}", "move": move, "reasons": why})
        return reply

    def _opening_lines(self, case, p, sheet):
        w = self._words(sheet)
        lines = [f"Thank you for your enquiry. We are pleased to submit our offer: {w}. Valid for 30 days."]
        for series, (weight, change) in sorted(p["drivers"].items(), key=lambda x: -x[1][0])[:3]:
            verb = "rose" if change > 0 else "moved"
            lines.append(f"{series} {verb} {change * 100:+.1f} % in twelve months and is {weight:.0%} of our cost.")
        return lines + ["Our quote is attached."]

    # -- documents: real suppliers never share a cost breakdown ---------------------------------------
    def attachment(self, team, sid, att):
        """Quote PDFs and order confirmations in the kit's format (`docs.quote_terms` reads them)."""
        s = self.session(team, sid)
        if att not in s["files"] or "cost-breakdown" in att:
            raise Fail(404, "no such attachment in this session")
        kind, stated, standard = s["files"][att]
        key = att.split(":", 1)[1].split("-", 1)[0]
        p = next(t["twin"]["p"] for t in s["threads"].values() if t["key"] == key)
        fixed = s["case"].get("fixed_terms", {})
        if kind.startswith("order"):
            return pdf(
                f"Order confirmation - {p['name']}", [("Agreed terms", _labels({**fixed, **standard, **stated}))]
            ), PDF
        rest = {k: v for k, v in standard.items() if k not in stated or stated[k] is None}
        remark = f"payment within {standard.get('payment_days')} days requested." if p["rate"] > 0.1 else "none."
        offered = {k: v for k, v in stated.items() if v is not None or k == "index_clause"}
        sections = [
            ("Offered terms", _labels(offered)),
            ("Our standard terms (apply to everything not stated above)", _labels(rest)),
            ("Terms fixed by your enquiry", _labels(fixed)),
            (f"Validity: 30 days. Remark: {remark}", {}),
        ]
        return pdf(f"Quote - {p['name']}", sections), PDF

    # -- scoring: the arena's own yardstick for the hosted cases -----------------------------------------
    def yardstick(self, case, ths):
        cal = self.calib.get(case["case_id"])
        if cal and cal.get("c_best"):
            return cal["c_open"], cal["c_best"]
        if case["case_id"] not in self._yard:
            opens, bests = [], []
            for th in ths.values():
                p = th["twin"]["p"]
                sheet = closing_terms(case, p["opening"], p["standard"])
                ok = must_haves_ok(case, sheet)
                opens.append((not ok, total_cost(case, sheet, supplier=who(p, case, sheet))))
                bests.append(best_cost(p, case, p["floor"])[0])
            self._yard[case["case_id"]] = (min(opens)[1], min(bests))
        return self._yard[case["case_id"]]

    def _end(self, s, reason, sup):
        case, ths = s["case"], s["threads"]
        c_open, c_best = self.yardstick(case, ths)
        alt = case["buyer"].get("alternative") or {}
        c_alt = total_cost(case, alt["terms"]) if alt.get("terms", {}).get("unit_price") else None
        c_best = min(c_best, c_alt) if c_alt is not None else c_best
        r = {"ended": True, "deal": False, "supplier_id": None, "offer_id": None, "terms": None, "c_open": c_open,
             "c_deal": None, "c_alt": c_alt, "c_best": c_best, "score": score(c_open, c_alt, c_best) if c_alt else 0.0,
             "must_haves_ok": None, "cost_breakdown": None, "trace": []}  # fmt: skip
        if sup:
            p = ths[sup]["twin"]["p"]
            t = closing_terms(case, ths[sup]["stated"], ths[sup]["standard"])
            sd = who(p, case, t)
            r.update(deal=True, supplier_id=sup, offer_id=ths[sup]["offer_id"], terms=t,
                     c_deal=total_cost(case, t, supplier=sd), must_haves_ok=must_haves_ok(case, t),
                     cost_breakdown=cost_breakdown(case, t, supplier=sd))  # fmt: skip
            r["score"] = score(c_open, r["c_deal"], c_best) if r["must_haves_ok"] else 0.0
        s.update(status="ended", end_reason=reason, result=r)
        for th in ths.values():
            th["closed"] = th["closed"] or reason


def msg_body(case, p, status, stated, alt, say, th, keep):
    """The supplier's e-mail in its own words: headline, reasons, the offer, questions."""
    words = Twin._words
    lines = []
    if status == "walked_away":
        return say or ["We see no basis to continue and withdraw our offer."]
    if status == "agreed":
        lines.append(f"We agree to your terms: {words(stated)}. Please accept our offer to place the order.")
    elif keep:
        lines.append(f"Our offer stands: {words(stated)}.")
    else:
        lines.append("We have reviewed your offer and can adjust our offer as follows.")
    lines += say
    if status in ("open", "final_offer") and not keep:
        head = "This is our final offer" if status == "final_offer" else "Our current offer is"
        lines.append(f"{head}: {words(stated)}.")
        if alt:
            lines.append(f"Alternatively: {words(alt)}.")
        lines.append("This offer stays valid for 30 days.")
    if status == "open" and ("volume_per_year" in case["negotiable"] or th["round"] >= 2):
        lines.append(QUESTION_VOLUME)
    shares = [w for w, _ in p["drivers"].values()]
    if shares and status != "agreed":
        lines.append(f"Material accounts for about {max(shares):.0%} of our cost; more detail we do not share.")
    return lines


# -- the real transcripts ---------------------------------------------------------------------------------
def read_text_any(path):
    """Read a text file as UTF-8, falling back to cp1252 for files written by Windows tools before the UTF-8 fix."""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def load_runs(runs_dir="runs"):
    """Every saved real arena session, oldest first: [(file, record)]."""
    out = []
    for f in sorted(glob.glob(str(_path(runs_dir) / "arena-P*.json"))):
        rec = json.loads(read_text_any(f))
        if rec.get("state", {}).get("case_id") and rec.get("result", {}).get("c_best") is not None:
            out.append((Path(f).name, rec))
    return out


def make_twin_real(cases_dir="cases/real", data_dir="data/package", calib=None, seed=0, no_clock=True,
                   clock=time.time, event_code="sim"):  # fmt: skip
    """A TwinReal over the real case files and the data package (paths relative to the project)."""
    world = World(_path(data_dir))
    return TwinReal(load_cases(_path(cases_dir)), world, seed=seed, calib=calib, event_code=event_code,
                    clock=clock, no_clock=no_clock)  # fmt: skip


def _supplier_replies(rec, sup):
    """The real supplier's term-sheet replies in one thread: [(round, status, [terms, ...])]."""
    out = []
    for e in rec["state"]["result"]["trace"]:
        if e["actor"] == sup:
            mv = e["move"]
            sheets = ([mv["terms"]] if mv.get("terms") else []) + list(mv.get("alternatives") or [])
            sheets = [t.get("terms", t) for t in sheets]
            sheets = [t for t in sheets if t.get("unit_price") is not None]
            out.append((e["round"], mv["status"], sheets))
    return out


def _rate_from_pairs(pairs, std_pay):
    """Its cost of money from two iso-value sheets that differ only in payment days."""
    rates = []
    for a, b in pairs:
        diff = {k for k in a if a.get(k) != b.get(k)} - {"unit_price"}
        if diff != {"payment_days"} or std_pay is None:
            continue
        (p1, d1), (p2, d2) = (a["unit_price"], a["payment_days"]), (b["unit_price"], b["payment_days"])
        den = (p1 * (d1 - std_pay) - p2 * (d2 - std_pay)) / 360
        if abs(den) > 1e-9 and 0 < (r := (p1 - p2) / den) < 0.5:
            rates.append(r)
    return round(statistics.mean(rates), 4) if rates else None


def _grid_of(prices):
    """The coarsest price grid (0.5, 0.1, 0.05 or 0.01) every price sits on."""
    for g in (0.5, 0.1, 0.05):
        if len(prices) >= 2 and all(abs(x / g - round(x / g)) < 1e-6 for x in prices):
            return g
    return 0.01


def observe(world, cases, runs):
    """What the real transcripts say per hosted case and supplier, before any fitting: the arena's
    yardstick, the opening sheet, its standard terms (opening + quote-PDF fields inferred from c_open
    + closing terms of deals), its cost of money and lead-time premium (MESO pairs), its price grid."""
    out = {}
    for _, rec in runs:
        cid = rec["state"]["case_id"]
        case, res = cases[cid], rec["result"]
        yard = {k: res[k] for k in ("c_open", "c_best", "c_alt")}
        o = out.setdefault(cid, {**yard, "suppliers": {}, "_deals": []})
        if res.get("terms"):
            o["_deals"].append(res["terms"])
        for sup, op in rec["state"]["opening"].items():
            so = o["suppliers"].setdefault(sup, {"open": _sheet(case, op["terms"]), "missing": op["missing_fields"],
                                                 "_pairs": [], "_prices": []})  # fmt: skip
            for _, _status, sheets in _supplier_replies(rec, sup)[1:]:
                so["_prices"] += [t["unit_price"] for t in sheets]
                if len(sheets) == 2:
                    so["_pairs"].append((sheets[0], sheets[1]))
    for cid, o in out.items():
        case = cases[cid]
        sups = o["suppliers"]
        for sup, so in sups.items():
            base = supplier_profile(world, case, sup)
            std = {k: v for k, v in so["open"].items() if k != "unit_price" and k not in so["missing"]}
            for f in so["missing"]:
                std[f] = base["standard"].get(f)
            so["standard"] = std
        # quote-PDF fields of the supplier that sets c_open: the missing fields and ppm that reproduce it
        best = None
        for sup, so in sups.items():
            choices = []
            for f in so["missing"]:
                opts = sorted({*(case["term_options"].get(f) or []), so["standard"].get(f)} - {None}, key=str)
                choices.append([(f, v) for v in opts] or [(f, so["standard"].get(f))])
            for combo in itertools.product(*choices) if choices else [()]:
                std = {**so["standard"], **dict(combo)}
                for d in o["_deals"]:
                    if d.get("ppm_commitment") is not None and "ppm_commitment" not in case["negotiable"]:
                        std.setdefault("ppm_commitment", d["ppm_commitment"])
                t = closing_terms(case, so["open"], std)
                if not must_haves_ok(case, t):
                    continue
                c = total_cost(case, so["open"], std)
                if best is None or abs(c - o["c_open"]) < best[0]:
                    best = (abs(c - o["c_open"]), sup, std)
        if best:
            sups[best[1]]["standard"] = best[2]
        for d in o.pop("_deals"):  # closing terms of a deal show the standard terms it closed on
            for so in sups.values():
                for f in ("ppm_commitment", "chargeback_eur_per_complaint"):
                    if f not in case["negotiable"] and d.get(f) is not None and len(sups) == 1:
                        so["standard"][f] = d[f]
        for so in sups.values():
            pairs, prices = so.pop("_pairs"), so.pop("_prices")
            so["rate"] = _rate_from_pairs(pairs, so["standard"].get("payment_days"))
            so["round_to"] = _grid_of(prices)
            so["lead_premium"] = None
            for a, b in pairs:  # iso-value pair across lead times: the premium per day
                if a.get("lead_time_days") and b.get("lead_time_days") and a["lead_time_days"] != b["lead_time_days"]:
                    r, sp = so["rate"] or RATE, so["standard"].get("payment_days") or 30
                    va = a["unit_price"] * (1 - r * ((a.get("payment_days") or sp) - sp) / 360)
                    vb = b["unit_price"] * (1 - r * ((b.get("payment_days") or sp) - sp) / 360)
                    days = b["lead_time_days"] - a["lead_time_days"]
                    so["lead_premium"] = round((va - vb) / days / so["open"]["unit_price"], 5)
            so.pop("missing")
    return out


def fit_floor(world, case, cal, lo=0.5, hi=1.1):
    """The floor share f (floor value / opening value, the same for every supplier of the case) at
    which the cheapest package any supplier signs costs the arena's c_best. Returns (f, C at f)."""
    profs = [real_profile(world, case, sup, {case["case_id"]: cal}) for sup in cal["suppliers"]]

    def c_at(f):
        return min(best_cost(p, case, f * p["v0"])[0] for p in profs)

    target = cal["c_best"]
    for _ in range(40):
        mid = (lo + hi) / 2
        if c_at(mid) > target:
            hi = mid
        else:
            lo = mid
    return round(lo, 5), c_at(lo)


# -- replay: the real buyer's e-mails against the twin ---------------------------------------------------
def replay(twin, rec):
    """Feed the real buyer's messages of one session to the twin. Returns rows per supplier reply:
    {sup, round, real_status, twin_status, real (main terms), twin (main terms), err (value-equivalent
    error of the twin's main sheet vs the real one, as a share), deal comparison at the end}."""
    from arena_kit.client import ArenaError

    case_id = rec["state"]["case_id"]
    client = LocalClient(twin)
    opened = client.open_session(case_id)
    sid, case, rows, out = opened["session_id"], opened["case"], [], {"rows": None, "real": rec["result"]}
    trace, ended = rec["state"]["result"]["trace"], {}
    twin_last = {s: m[0] for s, m in opened["threads"].items()}
    for i, e in enumerate(trace):
        if e["actor"] != "buyer":
            continue
        mv, sup = e["move"], e["move"].get("supplier_id")
        if mv.get("type") == "timeout" or sup in ended:
            continue
        real = next((x["move"] for x in trace[i + 1 :] if x["actor"] == sup), None)
        p = twin.profile(case, sup)
        if mv.get("type") == "accept":
            last = twin_last[sup]
            sheets = last.get("term_sheets") or []
            alt = mv.get("offer_id", "").endswith("-a") and len(sheets) > 1
            pick = sheets[1 if alt else 0] if sheets else None
            if pick is None or last.get("status") == "walked_away":
                out["twin"] = {"deal": False, "note": "twin thread ended before the real accept"}
                break
            try:
                r = client.send_message(
                    sid, sup, {"action": {"type": "accept", "term_sheet_id": pick["term_sheet_id"]}}
                )
                out["twin"] = r.get("result")
            except ArenaError as err:
                out["twin"] = {"deal": False, "note": str(err.detail)}
            break
        sheets = [{"terms": ts["terms"]} for ts in mv.get("term_sheets") or []]
        msg = {"subject": mv.get("subject") or case["title"], "body": mv.get("body") or "",
               "refs": mv.get("refs") or [], "term_sheets": sheets}  # fmt: skip
        try:
            reply = client.send_message(sid, sup, msg)
        except ArenaError as err:
            rows.append({"sup": sup, "round": e["round"], "real_status": real and real["status"],
                         "twin_status": f"error {err.status}", "err": None, "real": real and real.get("terms"),
                         "twin": None})  # fmt: skip
            ended[sup] = True
            continue
        twin_last[sup] = reply
        tsheets = reply.get("term_sheets") or []
        tmain = tsheets[0]["terms"] if tsheets and reply["status"] != "walked_away" else None
        rmain = (real or {}).get("terms") or None
        err = None
        if tmain and rmain:
            err = value(p, case, tmain) / value(p, case, {**tmain, **rmain}) - 1
        rows.append({"sup": sup, "round": e["round"], "real_status": real and real["status"],
                     "twin_status": reply["status"], "real": rmain, "twin": tmain, "err": err,
                     "real_alt": bool(real and real.get("alternatives")), "twin_alt": len(tsheets) > 1,
                     "same_pkg": bool(tmain and rmain and all(tmain.get(k) == v for k, v in rmain.items() if k != "unit_price"))})  # fmt: skip  # noqa: E501
        if reply["status"] == "walked_away":
            ended[sup] = True
        if reply["status"] == "agreed" and (real or {}).get("status") != "agreed":
            # the twin agreed earlier than the real supplier: the buyer would have accepted here
            r = client.send_message(
                sid, sup, {"action": {"type": "accept", "term_sheet_id": tsheets[0]["term_sheet_id"]}}
            )
            out["twin"] = r.get("result")
            out["early"] = e["round"]
            break
        if "result" in reply:
            out["twin"] = reply["result"]
            break
    if "twin" not in out:  # the real buyer never accepted (no deal): did the twin end the same way?
        st = twin.sessions[sid]
        if st["status"] == "open":
            twin._end(st, "no_deal", None)
        out["twin"] = st["result"]
    out["rows"] = rows
    return out


def replay_summary(results):
    """Per session: status match, mean |value-equivalent error|, deal match, prices."""
    lines = []
    for name, rep in results:
        rows = [r for r in rep["rows"] if r["real_status"]]
        st = sum(r["real_status"] == r["twin_status"] for r in rows)
        errs = [abs(r["err"]) for r in rows if r["err"] is not None]
        real, tw = rep["real"], rep["twin"] or {}
        rp = (real.get("terms") or {}).get("unit_price")
        tp = (tw.get("terms") or {}).get("unit_price")
        lines.append({"session": name, "case": name.split("-")[1], "n": len(rows), "status_ok": st,
                      "mean_err": statistics.mean(errs) if errs else None, "max_err": max(errs) if errs else None,
                      "real_deal": real.get("deal"), "twin_deal": tw.get("deal"), "real_price": rp, "twin_price": tp,
                      "real_score": real.get("score"), "twin_score": tw.get("score"),
                      "seq_real": "".join(_code(r["real_status"]) for r in rows),
                      "seq_twin": "".join(_code(r["twin_status"]) for r in rows)})  # fmt: skip
    return lines


def _code(status):
    return {"open": "o", "final_offer": "F", "agreed": "A", "walked_away": "W"}.get(status, "?")


def replay_all(calib=None, runs_dir="runs", cases_dir="cases/real", data_dir="data/package", only=None):
    twin = make_twin_real(cases_dir, data_dir, calib=calib)
    out = []
    for name, rec in load_runs(runs_dir):
        if only and rec["state"]["case_id"] not in only:
            continue
        out.append((name, replay(twin, rec)))
    return out


def _loss(results):
    """Replay loss: mean |value error| of the main sheets + 2 % per status mismatch + 5 % per deal mismatch."""
    tot, n = 0.0, 0
    for _, rep in results:
        for r in rep["rows"]:
            if not r["real_status"]:
                continue
            n += 1
            tot += abs(r["err"]) if r["err"] is not None else 0.03
            tot += 0.02 * (r["real_status"] != r["twin_status"])
        real, tw = rep["real"], rep["twin"] or {}
        if bool(real.get("deal")) != bool(tw.get("deal")):
            tot += 0.05
        elif real.get("deal") and tw.get("terms"):
            tot += abs(tw["terms"]["unit_price"] / real["terms"]["unit_price"] - 1)
    return tot / max(n, 1)


def fit_limits(calib, runs, world, cases, step=0.0025):
    """Per hosted supplier: the limit share (of its opening value) whose replay fits best."""
    by_case = {}
    for name, rec in runs:
        by_case.setdefault(rec["state"]["case_id"], []).append((name, rec))
    for cid, recs in by_case.items():
        for sup in calib[cid]["suppliers"]:
            so = calib[cid]["suppliers"][sup]
            lo = so["floor"] if so.get("floor") else 0.7
            best = None
            f = 1.0
            while f >= lo - 1e-9:
                so["limit"] = round(f, 4)
                twin = TwinReal(cases, world, calib=calib, no_clock=True)
                loss = _loss([(n, replay(twin, r)) for n, r in recs])
                if best is None or loss < best[0] - 1e-9:
                    best = (loss, so["limit"])
                f -= step
            so["limit"], so["fit_loss"] = best[1], round(best[0], 5)
    return calib


def calibrate(runs_dir="runs", cases_dir="cases/real", data_dir="data/package", limits=True):
    """Fit everything from the transcripts. Returns the calibration dict (the CALIB layout)."""
    world, cases, runs = World(_path(data_dir)), load_cases(_path(cases_dir)), load_runs(runs_dir)
    calib = observe(world, cases, runs)
    for cid, cal in calib.items():
        f, c = fit_floor(world, cases[cid], cal)
        for so in cal["suppliers"].values():
            so["floor"] = f
        cal["floor_fit_err"] = round(c / cal["c_best"] - 1, 6)
    if limits:
        fit_limits(calib, runs, world, cases)
    return calib


def fit_generic(calib, cases):
    """The generic rules for suppliers the transcripts do not cover, fitted on the hosted ones:
    floor share by case type (mean), limit lam = (v0 - L) / (v0 - F) (mean; capacity-tight apart).
    Returns (rules, rows) with leave-one-case-out errors of the floor share."""
    rows, by_type = [], {}
    for cid, cal in calib.items():
        f = next(iter(cal["suppliers"].values()))["floor"]
        by_type.setdefault(cases[cid]["type"], []).append((cid, f))
    allf = [f for v in by_type.values() for _, f in v]
    for cid, cal in sorted(calib.items()):
        t = cases[cid]["type"]
        f = next(iter(cal["suppliers"].values()))["floor"]
        same = [x for c, x in by_type[t] if c != cid]
        loo = statistics.mean(same) if same else statistics.mean([x for x in allf if x != f])
        rows.append({"case": cid, "type": t, "floor": f, "loo": round(loo, 4), "err": round(loo - f, 4)})
    lams, lams_ct = [], []
    for cal in calib.values():
        for so in cal["suppliers"].values():
            if so.get("limit") is None or so["floor"] >= 0.999:
                continue
            lam = (1 - so["limit"]) / (1 - so["floor"])
            (lams_ct if so.get("lead_premium") else lams).append(lam)
    rules = {"floor_by_type": {t: round(statistics.mean(x for _, x in v), 4) for t, v in by_type.items()},
             "floor_default": round(statistics.mean(allf), 4), "lam": round(statistics.mean(lams), 3),
             "lam_ct": round(statistics.mean(lams_ct), 3) if lams_ct else None}  # fmt: skip
    return rules, rows


def generic_calib(calib, cases, loo=True):
    """The calibration with floors and limits removed (generic rules apply); with `loo` the floor
    rule for each case is refitted without that case - an honest test of the generic supplier."""
    out = copy.deepcopy(calib)
    for cid, cal in out.items():
        for so in cal["suppliers"].values():
            so.pop("limit", None)
            so.pop("fit_loss", None)
            if loo:
                rest = {c: v for c, v in calib.items() if c != cid}
                rules, _ = fit_generic(rest, cases)
                share = rules["floor_by_type"].get(cases[cid]["type"], rules["floor_default"])
                so["floor_generic"] = share
            so.pop("floor", None)
    return out


def holdout(calib, runs, world, cases):
    """Cases with two real sessions: fit the limit on one session, replay the other."""
    by_case = {}
    for name, rec in runs:
        by_case.setdefault(rec["state"]["case_id"], []).append((name, rec))
    out = []
    for cid, recs in by_case.items():
        if len(recs) < 2:
            continue
        for i, (name, rec) in enumerate(recs):
            train = [r for j, r in enumerate(recs) if j != i]
            cal = copy.deepcopy(calib)
            fit_limits(cal, train, world, cases)
            twin = TwinReal(cases, world, calib=cal, no_clock=True)
            out.append((name, replay(twin, rec), {s: so["limit"] for s, so in cal[cid]["suppliers"].items()}))
    return out


# -- our agent against the twin -------------------------------------------------------------------------
REAL_SCORES = {  # the scores our agent got on the hosted arena (all saved sessions, oldest first)
    "case:P01": [0.78], "case:P02": [0.26], "case:P03": [0.90], "case:P04": [0.86], "case:P05": [0.34],
    "case:P07": [0.70], "case:P08": [0.0, 0.259], "case:P10": [0.0, 0.586], "case:P11": [0.44],
    "case:P18": [0.0, 0.336],
}  # fmt: skip


def bench(case_ids=None, data_dir="data/package", calib=None, old_twin=False):
    """Play our agent (template writer, no LLM) on the hosted cases against TwinReal (and, with
    `old_twin`, against buyer.twin's guessed suppliers on the same case files). Returns rows."""
    from arena_kit.play import RemoteTable

    from .dossier import World as AgentWorld
    from .runner import make_factory, play

    factory = make_factory(AgentWorld(_path(data_dir)))
    ids = case_ids or sorted(REAL_SCORES)
    rows = []
    for cid in ids:
        twin = make_twin_real(data_dir=data_dir, calib=calib)
        client = LocalClient(twin)
        t0 = time.time()
        res = play(RemoteTable(client), cid, factory)
        sess = next(iter(twin.sessions.values()))
        seqs = {s[4:]: "".join(_code(m["status"]) for m in th["messages"][1:] if m.get("from") != "buyer")
                for s, th in sess["threads"].items()}  # fmt: skip
        row = {"case": cid, "score": res.get("score") or 0.0, "deal": res.get("deal"),
               "price": (res.get("terms") or {}).get("unit_price"), "c_deal": res.get("c_deal"),
               "statuses": seqs, "seconds": round(time.time() - t0, 1), "real": REAL_SCORES.get(cid, [])}  # fmt: skip
        if old_twin:
            from .twin import make_twin

            old = make_twin("cases/real", data_dir, no_clock=True)
            r2 = play(RemoteTable(LocalClient(old)), cid, factory)
            row["old_twin"] = r2.get("score") or 0.0
        rows.append(row)
    return rows


# -- command line ---------------------------------------------------------------------------------------
def _fmt_calib(calib):
    """The calibration as a Python literal for CALIB (stable order, rounded)."""

    def r(v):
        return round(v, 5) if isinstance(v, float) else v

    lines = ["{"]
    for cid in sorted(calib):
        c = calib[cid]
        head = ", ".join(f'"{k}": {r(c[k])!r}' for k in ("c_open", "c_best", "c_alt", "floor_fit_err") if k in c)
        lines.append(f'    "{cid}": {{{head}, "suppliers": {{')
        for sup, so in sorted(c["suppliers"].items()):
            keep = {k: r(v) for k, v in so.items() if v is not None}
            lines.append(f'        "{sup}": {json.dumps(keep, ensure_ascii=False)},  # noqa: E501')
        lines.append("    }},")
    lines.append("}")
    return "\n".join(lines).replace("null", "None").replace("true", "True").replace("false", "False")


DATA_HEAD = "# -- calibration data (generated by `python -m buyer.twin_real calibrate`) ---"


def write_calibration(calib, rules, path=None):
    """Rewrite the calibration data block at the end of this module (`calibrate --write`)."""
    path = Path(path or __file__)
    block = (f"{DATA_HEAD}\n# fmt: off\nFLOOR_BY_TYPE.update({json.dumps(rules['floor_by_type'])})  # noqa: E501\n"
             f"FLOOR_DEFAULT = {rules['floor_default']}\nLAM, LAM_CT = {round(rules['lam'], 2)}, "
             f"{round(rules['lam_ct'] or LAM_CT, 2)}\nCALIB.update({_fmt_calib(calib)})\n# fmt: on\n\n\n")  # fmt: skip
    text = path.read_text(encoding="utf-8")
    start = text.rindex("\n" + DATA_HEAD + "\n") + 1  # the block itself, not this function's text
    end = text.index("# fmt: on\n\n\n", start) + len("# fmt: on\n\n\n")
    path.write_text(text[:start] + block + text[end:], encoding="utf-8")


def _replay_table(results):
    out = ["| session | replies | status real | status twin | status ok | mean / max err of main sheet | "
           "deal real / twin | price real / twin | score real / twin |",
           "|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for row in replay_summary(results):
        e = "-" if row["mean_err"] is None else f"{100 * row['mean_err']:.2f} % / {100 * row['max_err']:.2f} %"
        rs, ts = row["real_score"], row["twin_score"]
        out.append(f"| {row['session'][6:-5]} | {row['n']} | {row['seq_real']} | {row['seq_twin']} | "
                   f"{row['status_ok']}/{row['n']} | {e} | {'deal' if row['real_deal'] else 'no'} / "
                   f"{'deal' if row['twin_deal'] else 'no'} | {row['real_price']} / {row['twin_price']} | "
                   f"{0 if rs is None else rs:.3f} / {0 if ts is None else ts:.3f} |")  # fmt: skip
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="buyer.twin_real", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["replay", "validate", "calibrate", "bench", "serve"])
    ap.add_argument("--cases", default="", help="e.g. P01,P08")
    ap.add_argument("--cases-dir", default="cases/real")
    ap.add_argument("--data", default="data/package")
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--old", action="store_true", help="bench: also play buyer.twin's suppliers")
    ap.add_argument("--port", type=int, default=8002)
    ap.add_argument("--clock", action="store_true", help="serve: the 60-second rule on")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--write", action="store_true", help="calibrate: rewrite the data block of this module")
    a = ap.parse_args(argv)
    only = {f"case:{c.strip().removeprefix('case:')}" for c in a.cases.split(",") if c.strip()} or None
    if a.command == "replay":
        res = replay_all(runs_dir=a.runs, cases_dir=a.cases_dir, data_dir=a.data, only=only)
        print(_replay_table(res))
        print(f"\nreplay loss {_loss(res):.5f}")
        if a.verbose:
            for name, rep in res:
                print(f"\n{name}")
                for r in rep["rows"]:
                    err = "" if r["err"] is None else f"{100 * r['err']:+.2f} %"
                    print(f"  R{r['round']} {r['sup']} real {r['real_status']:<11} {r['real'] and _short(r['real'])}"
                          f"  twin {r['twin_status']:<11} {r['twin'] and _short(r['twin'])}  {err}")  # fmt: skip
    elif a.command == "validate":
        world, cases, runs = World(_path(a.data)), load_cases(_path(a.cases_dir)), load_runs(a.runs)
        rules, rows = fit_generic(CALIB, cases)
        print("generic floor rule (share of the opening value by case type):", json.dumps(rules))
        print("| case | type | fitted floor | leave-one-out rule | error |\n|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['case'][5:]} | {r['type']} | {r['floor']:.4f} | {r['loo']:.4f} | {r['err']:+.4f} |")
        res = replay_all(runs_dir=a.runs, cases_dir=a.cases_dir, data_dir=a.data)
        print(f"\n## in-sample replay (calibrated limits), loss {_loss(res):.5f}\n" + _replay_table(res))
        ho = [(n, r) for n, r, _ in holdout(CALIB, runs, world, cases)]
        print(f"\n## hold-out: limit fitted on the other session of the case, loss {_loss(ho):.5f}\n"
              + _replay_table(ho))  # fmt: skip
        gen = replay_all(calib=generic_calib(CALIB, cases, loo=True), runs_dir=a.runs, cases_dir=a.cases_dir,
                         data_dir=a.data)  # fmt: skip
        print(f"\n## generic supplier (leave-one-case-out floor, generic limit), loss {_loss(gen):.5f}\n"
              + _replay_table(gen))  # fmt: skip
    elif a.command == "calibrate":
        calib = calibrate(a.runs, a.cases_dir, a.data)
        cases = load_cases(_path(a.cases_dir))
        rules, rows = fit_generic(calib, cases)
        print("CALIB =", _fmt_calib(calib))
        print("\ngeneric rules:", json.dumps(rules))
        for r in rows:
            print(f"  {r['case']} {r['type']:<15} floor {r['floor']:.4f}  "
                  f"leave-one-out rule {r['loo']:.4f}  err {r['err']:+.4f}")  # fmt: skip
        res = replay_all(calib=calib, runs_dir=a.runs, cases_dir=a.cases_dir, data_dir=a.data)
        print(f"\nreplay loss {_loss(res):.5f}\n" + _replay_table(res))
        if a.write:
            write_calibration(calib, rules)
            print(f"\nwrote the calibration into {__file__}")
    elif a.command == "bench":
        rows = bench(sorted(only) if only else None, a.data, old_twin=a.old)
        print(f"{'case':10} {'twin':>6} {'old twin':>9} {'real (all sessions)':>22}  deal price  statuses")
        for r in rows:
            old = f"{r['old_twin']:.3f}" if "old_twin" in r else "-"
            real = ", ".join(f"{x:.2f}" for x in r["real"])
            print(f"{r['case']:10} {r['score']:6.3f} {old:>9} {real:>22}  {'deal' if r['deal'] else 'no  '} "
                  f"{r['price']}  {r['statuses']}")  # fmt: skip
        mean = statistics.mean(r["score"] for r in rows)
        real_best = statistics.mean(max(r["real"]) if r["real"] else 0 for r in rows)
        real_first = statistics.mean(r["real"][0] if r["real"] else 0 for r in rows)
        print(f"\nmean twin {mean:.3f}   real first session {real_first:.3f}   real best {real_best:.3f}")
    else:
        import uvicorn

        twin = make_twin_real(a.cases_dir, a.data, no_clock=not a.clock)
        print(f"twin_real: {len(twin.cases)} cases on port {a.port}, register with event code 'sim'", flush=True)
        uvicorn.run(create_app(twin), host="127.0.0.1", port=a.port)


__all__ = ["TwinReal", "LocalClient", "make_twin_real", "real_profile", "value", "price_for", "best_cost"]


# -- calibration data (generated by `python -m buyer.twin_real calibrate`) ---
# fmt: off
FLOOR_BY_TYPE.update({"price_increase": 0.9588, "renewal": 0.9774, "rfq": 0.791, "bundling": 0.8635, "raw_material": 0.7949, "capacity": 0.8535, "quality": 0.9086})  # noqa: E501
FLOOR_DEFAULT = 0.8879
LAM, LAM_CT = 0.54, 0.11
CALIB.update({
    "case:P01": {"c_open": 1153779.74, "c_best": 1096815.78, "c_alt": 1176178.02, "floor_fit_err": -0.0, "suppliers": {
        "sup:70110": {"open": {"unit_price": 138.55, "effective_date": "2026-11-01", "payment_days": None}, "standard": {"effective_date": "2026-11-01", "payment_days": 30, "ppm_commitment": 900, "chargeback_eur_per_complaint": 0.0}, "round_to": 0.1, "floor": 0.94483, "limit": 0.97, "fit_loss": 0.00531},  # noqa: E501
    }},
    "case:P02": {"c_open": 2282867.49, "c_best": 2235514.05, "c_alt": 2604227.78, "floor_fit_err": -0.0, "suppliers": {
        "sup:70510": {"open": {"unit_price": 370.96, "payment_days": 30, "index_clause": None}, "standard": {"payment_days": 30, "index_clause": None, "ppm_commitment": 250, "chargeback_eur_per_complaint": 0.0}, "rate": 0.0661, "round_to": 0.01, "floor": 0.97273, "limit": 0.9875, "fit_loss": 0.02228},  # noqa: E501
    }},
    "case:P03": {"c_open": 4246314.02, "c_best": 3909043.1, "c_alt": 4332545.92, "floor_fit_err": -0.0, "suppliers": {
        "sup:70530": {"open": {"unit_price": 438.21, "payment_days": 30, "contract_months": 12, "volume_per_year": 4580}, "standard": {"payment_days": 30, "contract_months": 12, "volume_per_year": 4580, "ppm_commitment": 350, "chargeback_eur_per_complaint": 0.0}, "rate": 0.0407, "round_to": 0.01, "floor": 0.97272, "limit": 0.98, "fit_loss": 0.01057},  # noqa: E501
    }},
    "case:P04": {"c_open": 198846.33, "c_best": 196529.32, "c_alt": 203169.0, "floor_fit_err": -0.0, "suppliers": {
        "sup:70620": {"open": {"unit_price": 4.96, "payment_days": 30, "contract_months": 12}, "standard": {"payment_days": 30, "contract_months": 12, "ppm_commitment": 150, "chargeback_eur_per_complaint": 0.0}, "rate": 0.0718, "round_to": 0.01, "floor": 0.98216, "limit": 0.9825, "fit_loss": 0.00809},  # noqa: E501
    }},
    "case:P05": {"c_open": 305544.96, "c_best": 223424.17, "c_alt": None, "floor_fit_err": -0.0, "suppliers": {
        "sup:70210": {"open": {"unit_price": 36.0, "payment_days": None, "lead_time_days": None}, "standard": {"payment_days": 30, "lead_time_days": 28, "ppm_commitment": 1200}, "round_to": 0.01, "floor": 0.79096, "limit": 0.895, "fit_loss": 0.03837},  # noqa: E501
        "sup:70240": {"open": {"unit_price": 31.5, "payment_days": None, "lead_time_days": 52}, "standard": {"lead_time_days": 52, "payment_days": 45}, "rate": 0.1183, "round_to": 0.5, "lead_premium": 0.00481, "floor": 0.79096, "limit": 0.97, "fit_loss": 0.03789},  # noqa: E501
        "sup:70250": {"open": {"unit_price": 38.5, "payment_days": None, "lead_time_days": None}, "standard": {"payment_days": 30, "lead_time_days": 28}, "round_to": 0.01, "floor": 0.79096, "limit": 0.8625, "fit_loss": 0.03633},  # noqa: E501
    }},
    "case:P07": {"c_open": 391027.4, "c_best": 299241.49, "c_alt": 379916.0, "floor_fit_err": -0.0, "suppliers": {
        "sup:70610": {"open": {"unit_price": 5.74, "payment_days": 30, "contract_months": 12}, "standard": {"payment_days": 30, "contract_months": 12, "ppm_commitment": 80, "chargeback_eur_per_complaint": 0.0}, "rate": 0.0765, "round_to": 0.01, "floor": 0.86347, "limit": 0.95, "fit_loss": 0.00543},  # noqa: E501
    }},
    "case:P08": {"c_open": 1135887.28, "c_best": 960309.33, "c_alt": 1157019.84, "floor_fit_err": -0.0, "suppliers": {
        "sup:70710": {"open": {"unit_price": 6.05, "index_clause": None, "payment_days": 30, "contract_months": 12}, "standard": {"index_clause": None, "payment_days": 30, "contract_months": 12, "ppm_commitment": 50, "chargeback_eur_per_complaint": 0.0}, "rate": 0.0707, "round_to": 0.01, "floor": 0.8276, "limit": 0.9075, "fit_loss": 0.0134},  # noqa: E501
    }},
    "case:P10": {"c_open": 1932492.18, "c_best": 1328926.89, "c_alt": None, "floor_fit_err": -0.0, "suppliers": {
        "sup:70410": {"open": {"unit_price": 32.2, "volume_per_year": 19890, "contract_months": 12, "lead_time_days": 54}, "standard": {"volume_per_year": 19890, "contract_months": 12, "lead_time_days": 54, "ppm_commitment": 300, "chargeback_eur_per_complaint": 0.0}, "round_to": 0.1, "lead_premium": 0.00492, "floor": 0.85352, "limit": 0.9875, "fit_loss": 0.00487},  # noqa: E501
    }},
    "case:P11": {"c_open": 817578.0, "c_best": 722500.02, "c_alt": 894383.19, "floor_fit_err": -0.0, "suppliers": {
        "sup:70140": {"open": {"ppm_commitment": 6400, "chargeback_eur_per_complaint": None, "unit_price": 47.0}, "standard": {"ppm_commitment": 6400, "chargeback_eur_per_complaint": 0}, "round_to": 0.5, "floor": 0.90863, "limit": 0.9775, "fit_loss": 0.0077},  # noqa: E501
    }},
    "case:P18": {"c_open": 295173.23, "c_best": 222651.27, "c_alt": 301087.96, "floor_fit_err": -0.0, "suppliers": {
        "sup:70740": {"open": {"unit_price": 6.1, "index_clause": None, "payment_days": 30, "contract_months": 12}, "standard": {"index_clause": None, "payment_days": 30, "contract_months": 12, "ppm_commitment": 70, "chargeback_eur_per_complaint": 0.0}, "rate": 0.0939, "round_to": 0.01, "floor": 0.76225, "limit": 0.9075, "fit_loss": 0.01255},  # noqa: E501
    }},
})
# fmt: on


if __name__ == "__main__":
    main()
