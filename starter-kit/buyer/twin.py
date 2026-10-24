"""Twin: a local, deterministic and free opponent for the buyer agent.

The kit's `arena_kit.sim.Sim` keeps the HTTP API, the 60-second clock, sessions, errors, limits
and replays; `Twin` swaps its one scripted supplier for persona-driven suppliers derived from the
data package. Per case and supplier a hidden profile (`supplier_profile`) holds:

  traits      read from notes, letters, quote remarks, negotiation history, news, credit index
  full_cost   should-cost of its plant, or a German-base estimate x country factor, or the price
              less an owner-type markup (kits: the kit price less the markup); +-3 % noise by seed
  cash_rate   what money is worth to it (cash-short suppliers want to be paid fast)
  min_margin  the lowest margin on its value (`cost.supplier_value`) it signs
  standard    its standard terms (quote PDF), opening, a Boulware concession curve, patience

Each buyer e-mail is valued with `cost.supplier_value`; the supplier concedes only when the buyer
moves (a sheet worth more to it than the buyer's best so far), checks cited data as api.md says,
reacts to leaks, threats and appreciation, agrees when a sheet meets its target and counters on the
buyer's own structure otherwise. Scores use the published cost function with the TRUE profile, so
the relationship charge applies in stabilise / secure_supply cases. No LLM, no network.

    uv run python -m buyer.twin serve --port 8001 --cases cases/practice --data data/package [--no-clock] [--seed 0]
    uv run python -m buyer.twin calibrate [--out runs/twin_calibration.txt]   # the naive agent on every case
"""

import argparse
import copy
import hashlib
import io
import itertools
import json
import math
import re
import secrets
import sqlite3
import time
from pathlib import Path

from arena_kit.client import ArenaError
from arena_kit.cost import (
    RELATIONSHIP_GOALS,
    closing_terms,
    cost_breakdown,
    horizon_start,
    must_haves_ok,
    score,
    supplier_value,
    total_cost,
)
from arena_kit.sim import ID_RE, Fail, Sim, create_app, load_cases, pdf, structured

ROOT = Path(__file__).resolve().parents[1]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# -- persona evidence --------------------------------------------------------------------------
TRAITS = {  # trait -> phrase in the supplier's notes, letters, quote remarks, history or news
    "cash_short_hard": r"asked twice|bank is watching|short hours|\bidle\b",
    "cash_short_weak": r"pay our invoices within 10 days|payment within 10 days requested",
    "approval_ladder": r"pricing committee",
    "price_frozen": r"frozen all price reductions|price list applies to all customers|head-office directive|"
    r"only multi-year volume agreements change prices",
    "small_account": r"reviewing small accounts|not strategic|key accounts",
    "open_book": r"walk us through their calculation",
    "free_capacity": r"second shift has been on hold|looks for new customers",
    "reference_customer": r"important reference customer",
    "stale_costing": r"own spreadsheet|not followed his material",
    "reciprocity": r"kept supplying at the old price",
    "below_full_cost": r"accepted below full cost",
    "hedged_material": r"bought their \w+(?: and \w+)? forward|covered at fixed prices|material covered until",
    "capacity_tight": r"binding 12-month commitment",
}
HARD_FREEZE = r"frozen all price reductions|price list applies to all customers|head-office directive"
NEWS_TRAITS = {
    "restructuring": "cash_short_hard",
    "credit_watch": "cash_short_hard",
    "credit_downgrade": "cash_short_hard",
    "short_time_work": "cash_short_hard",
    "full_order_book": "capacity_tight",
    "allocation": "capacity_tight",
    "free_capacity": "free_capacity",
}
TOUGH = {"approval_ladder", "price_frozen", "small_account"}
EAGER = {"cash_short_hard", "free_capacity"}
COUNTRY = {"DE": 1.0, "AT": 1.0, "CH": 1.0, "CZ": 0.83, "PL": 0.83, "HU": 0.83, "TR": 0.75, "CN": 0.7}
MARKUP = {"group": 0.12, "private": 0.10, "family": 0.09, "founder": 0.08}  # price over full cost, no should-cost
BASE_MARGIN = {"group": 0.06, "private": 0.04, "family": 0.03, "founder": 0.03}
ROLE = {"group": "key-account", "private": "sales", "family": "owner", "founder": "owner"}
GREETING = {"DE": "Guten Tag,", "AT": "Grüß Gott,", "CZ": "Dobrý den,", "PL": "Dzień dobry,", "TR": "Merhaba,"}
SIGNOFF = {"DE": "Mit freundlichen Grüßen", "AT": "Mit freundlichen Grüßen"}
USUAL_RATE, MAX_ROUNDS, PATIENCE, TRUST, APPROVAL_STEP, APPROVAL_ROUNDS = 0.07, 6, 3, 2, 0.02, 3

# -- what the buyer writes ---------------------------------------------------------------------
LEAK = re.compile(r"\burgent|\basap\b|as soon as possible|no alternative|no other (?:supplier|source|option)|"
                  r"only supplier|single source|our budget|budget (?:is|of)|line stop|running out", re.I)  # fmt: skip
THREAT = re.compile(r"terminat|switch suppliers?|competitor|we will move|unacceptable|take our business|delist", re.I)
THANKS = re.compile(r"thank|appreciat|partnership|2022|loyal", re.I)
ASK_BREAKDOWN = re.compile(r"breakdown|calculation|cost structure|kalkulation|open book|walk us through", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
NUM = r"[-+−]?\d[\d,]*(?:\.\d+)?"
PCT = re.compile(rf"({NUM})\s*(?:%|percent|per cent|pp\b)", re.I)
DAYS = re.compile(rf"({NUM})\s*(?:days?|tage)\b", re.I)
PPM = re.compile(rf"({NUM})\s*ppm", re.I)
MONEY = re.compile(rf"(?:€|eur\s?)\s*({NUM})|({NUM})\s*(?:€|eur\b)|\b(\d+\.\d{{2}})\b", re.I)
DATES = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b\d{4}Q\d\b")
FALL = re.compile(r"fell|fall|down|decreas|drop|lower|declin|minus|sank|cheaper", re.I)


def _u(*keys):
    """A deterministic uniform number in [0, 1) from the keys (seed, case, supplier, ...)."""
    return int(hashlib.sha256("|".join(map(str, keys)).encode()).hexdigest()[:12], 16) / 16**12


def _month(m, k=0):
    y, mo = map(int, m.split("-"))
    i = y * 12 + mo - 1 + k
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def _months(a, b):
    (ya, ma), (yb, mb) = map(int, a.split("-")), map(int, b.split("-"))
    return (yb - ya) * 12 + mb - ma


def _num(text):
    return float(text.replace("−", "-").replace(",", ""))


class World:
    """Read-only access to the data package: the SQLite world, facts.json and the documents."""

    def __init__(self, root):
        self.root = Path(root)
        self.db = sqlite3.connect(f"file:{self.root / 'world.sqlite'}?mode=ro", uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.facts = json.loads((self.root / "facts.json").read_text(encoding="utf-8"))
        self.series = {}
        for k, v in self.facts.items():
            if k.startswith("idx:"):
                self.series.setdefault(v["series"], {})[v["month"]] = v["value"]

    def one(self, sql, *a):
        row = self.db.execute(sql, a).fetchone()
        return dict(row) if row else None

    def all(self, sql, *a):
        return [dict(r) for r in self.db.execute(sql, a)]

    def change(self, series, months=12):
        """The index change over the last `months` months of its data, as a fraction."""
        values = self.series.get(series) or {}
        last = max(values) if values else None
        prev = _month(last, -months) if last else None
        return values[last] / values[prev] - 1 if prev in values else None

    def evidence(self, sup):
        """(everything the data says about a supplier in words, its news topics)."""
        texts = []
        for d in self.all("SELECT file FROM documents WHERE supplier_id=? ORDER BY date", sup):
            if d["file"] and (self.root / d["file"]).is_file():
                texts.append((self.root / d["file"]).read_text(encoding="utf-8"))
        texts += [q["remarks"] for q in self.all("SELECT remarks FROM quotes WHERE supplier_id=? AND remarks<>''", sup)]
        notes = self.all("SELECT note FROM negotiation_history WHERE supplier_id=? AND note<>''", sup)
        texts += [n["note"] for n in notes]
        news = self.all("SELECT topic, headline, body FROM news WHERE supplier_id=? AND topic<>'noise'", sup)
        texts += [f"{n['headline']}. {n['body']}" for n in news]
        return "\n".join(texts), [n["topic"] for n in news]


# -- the supplier's economics -----------------------------------------------------------------
def _extra(p, case, terms):
    """EUR per unit the supplier gains (+) or spends (-) beyond price, payment, freight, tooling and
    chargebacks: a long commitment fills idle capacity or secures a small account, a faster delivery
    or more volume strains a full plant, a tighter ppm costs work, an index clause passes its
    material risk on."""
    t, ref, tr = closing_terms(case, terms, p["standard"]), p["ref_price"], p["traits"]
    long, x = (t.get("contract_months") or 0) >= 24, 0.0
    if "free_capacity" in tr and long:
        x += 0.005 * ref
    if ("small_account" in tr or "price_frozen" in tr) and long:
        x += 0.01 * ref
    if "capacity_tight" in tr:
        if (t.get("lead_time_days") or 0) < p["lead_std"]:
            x -= 0.015 * ref
        if case["type"] == "capacity" and t["volume_per_year"] > p["volume_std"] and not long:
            x -= 0.015 * ref
    if (ppm := t.get("ppm_commitment")) is not None and ppm < p["real_ppm"]:
        x -= 0.006 * ref * (p["real_ppm"] - ppm) / 1000
    if clause := t.get("index_clause"):
        path = case["buyer"].get("index_paths", {}).get(clause["series"], 0.0)
        moved = max(0.0, abs(path) - clause["deadband_pct"] / 100) * math.copysign(1, path)
        premium = (0.005 if "hedged_material" in tr else 0.02) * (0.6 if clause["deadband_pct"] else 1.0)
        x += (t.get("unit_price") or 0.0) * clause["material_share"] * (moved + premium)  # linear in the price
    return x


def supplier_dict(p, case, terms):
    """The supplier as `cost.supplier_value` / `cost.total_cost` take it, for these terms."""
    return {"standard": p["standard"], "interest_rate": p["cash_rate"], "usual_rate": USUAL_RATE,
            "ppm": p["real_ppm"], "extra": _extra(p, case, terms), "full_cost": p["full_cost"]}  # fmt: skip


def value(p, case, terms):
    """What a term sheet is worth to the supplier per unit, in cash at its standard terms."""
    return supplier_value(case, terms, supplier_dict(p, case, terms))


def price_for(p, case, terms, target):
    """The price (cents, rounded up) that makes these terms worth `target` to the supplier.
    The value is linear in the price: two points solve it. None if no price can."""
    v0, v1 = (value(p, case, {**terms, "unit_price": x}) for x in (0.0, 100.0))
    slope = (v1 - v0) / 100
    return None if slope <= 1e-9 else math.ceil(round((target - v0) / slope * 100, 6)) / 100


def list_floor(p, terms):
    """A price-frozen supplier never goes below its list - unless it is the multi-year kind and
    the sheet is a multi-year agreement."""
    if "price_frozen" not in p["traits"] or not p["list_price"]:
        return None
    if p["multi_year_exempt"] and (terms.get("contract_months") or 0) >= 24:
        return None
    return p["list_price"]


def _sheet(case, terms):
    return {f: terms.get(f) for f in case["negotiable"]}


def _grid(case, p):
    """Every package of the negotiable non-price fields: the options and the supplier's standard."""
    fields, choices = case["negotiable"][1:], []
    for f in fields:
        vals = list(case["term_options"].get(f) or [])
        if p["standard"].get(f) not in vals:
            vals.append(p["standard"].get(f))
        choices.append(vals)
    for combo in itertools.product(*choices):
        yield dict(zip(fields, combo, strict=True))


def supplier_profile(world, case, sup, seed=0):
    """The hidden model of one supplier in one case: deterministic from the data and the seed."""
    b, meta, ctype, cid = case["buyer"], case.get("local_meta") or {}, case["type"], case["case_id"]
    row = world.one("SELECT * FROM suppliers WHERE id=?", sup) or {}
    intel = world.one("SELECT * FROM supplier_intel WHERE supplier_id=?", sup) or {}
    owner, country = intel.get("owner_type") or "private", row.get("country") or "DE"
    text, topics = world.evidence(sup)
    low = text.lower()
    traits = {t for t, rx in TRAITS.items() if re.search(rx, low)}
    traits |= {NEWS_TRAITS[t] for t in topics if t in NEWS_TRAITS}
    if (intel.get("credit_index") or 0) - (intel.get("credit_index_12m_ago") or 10**6) >= 10:
        traits.add("cash_short_hard")  # a rising credit index: the bank is worried
    if "cash_short_hard" in traits:
        traits.discard("cash_short_weak")
    part = case["parts"][0]
    own = world.one("SELECT price FROM price_conditions WHERE part_id=? AND supplier_id=? ORDER BY valid_from DESC "
                    "LIMIT 1", part, sup)  # fmt: skip
    current = b.get("current_unit_price")
    ref = current if meta.get("kit") else ((own or {}).get("price") or current or meta.get("reference_price"))

    # full cost per unit, in the case's price unit
    markup, sc, source = MARKUP.get(owner, 0.10), None, ""
    if not meta.get("kit"):
        if sc := world.one("SELECT * FROM should_cost WHERE part_id=? AND supplier_id=?", part, sup):
            base, source = sc["unit_cost"], f"should-cost of its plant ({sc['id']})"
        elif sc := world.one("SELECT * FROM should_cost WHERE part_id=? ORDER BY supplier_id IS NOT NULL", part):
            base = sc["unit_cost"] / COUNTRY.get(sc["cost_base"], 1.0) * COUNTRY.get(country, 1.0)
            source = f"{sc['id']} x country factor {COUNTRY.get(country, 1.0)}"
    if sc is None:
        below = "below_full_cost" in traits
        base, source = ref * (1.05 if below else 1 - markup), "price x " + ("1.05" if below else f"(1 - {markup})")
    full_cost = round(base * (1 + 0.03 * (2 * _u(seed, cid, sup, "cost") - 1)), 4)
    split = [sc["material_eur"], sc["conversion_eur"], sc["overhead_eur"]] if sc else [0.5, 0.3, 0.2]

    # cost of money, minimum margin, how it concedes
    cash_rate = 0.30 if "cash_short_hard" in traits else 0.15 if "cash_short_weak" in traits else USUAL_RATE
    mm = BASE_MARGIN.get(owner, 0.04)
    if "capacity_tight" in traits:
        mm = max(mm, 0.10)
    if "cash_short_hard" in traits:
        mm = min(mm, 0.02)
    if "free_capacity" in traits:
        mm = min(mm, 0.01)
    mm = min(max(mm + 0.02 * ("small_account" in traits) + 0.03 * ("price_frozen" in traits), 0.0), 0.15)
    tough, eager = bool(traits & TOUGH) or owner == "group", bool(traits & EAGER)
    beta = 0.7 if tough and eager else 0.4 if tough else 1.2 if eager else 0.7

    # standard terms (its quote PDF)
    opts = case["term_options"]
    q = world.one("SELECT payment_days FROM quotes WHERE supplier_id=? AND part_id=? AND id GLOB 'quote:73*' "
                  "ORDER BY quote_date DESC LIMIT 1", sup, part)  # fmt: skip
    pay = row.get("payment_days") or 30
    if q and q["payment_days"] is not None and q["payment_days"] < pay:
        pay = q["payment_days"]
    lead, vol = row.get("standard_lead_time_days") or b["need_lead_time_days"], b["need_per_year"]
    if ctype == "capacity" and "capacity_tight" in traits:  # "lead times are longer than agreed", not the full forecast
        lead = next((o for o in sorted(opts.get("lead_time_days") or []) if o >= lead + 28), lead + 28)
        vols = sorted(opts.get("volume_per_year") or [vol])
        vol = vols[len(vols) // 2]
    sc_row = world.one("SELECT ppm FROM scorecards WHERE supplier_id=? ORDER BY quarter DESC LIMIT 1", sup) or {}
    real_ppm = sc_row.get("ppm") or 0
    ppm_opts = sorted(opts.get("ppm_commitment") or [])
    ppm_std = next((o for o in ppm_opts if o >= real_ppm), ppm_opts[-1] if ppm_opts else None)
    start = horizon_start(case)
    every = {"payment_days": pay, "incoterm": row.get("incoterm") or "DAP", "lead_time_days": lead,
             "contract_months": 12, "index_clause": None, "tooling_eur": 0, "ppm_commitment": ppm_std,
             "chargeback_eur_per_complaint": 0, "effective_date": start.isoformat() if start else None,
             "volume_per_year": vol}  # fmt: skip
    standard = {f: every.get(f) for f in case["negotiable"][1:]}

    # cost drivers: what an index argument is about, and the price they justify
    drivers = {}
    marks = ",".join("?" * len(case["parts"]))
    for d in world.all(f"SELECT series_id, avg(weight) w FROM cost_drivers WHERE part_id IN ({marks}) "
                       "GROUP BY series_id", *case["parts"]):  # fmt: skip
        drivers[d["series_id"]] = (d["w"], world.change(d["series_id"]) or 0.0)
    justified = (current or ref) * (1 + sum(w * c for w, c in drivers.values()))
    hedge = re.search(r"covered[^.]*?until (\w+ \d{4})", text, re.I)

    p = {
        "supplier_id": sup, "name": row.get("name") or sup, "country": country, "owner": owner,
        "role": ROLE.get(owner, "sales"), "traits": sorted(traits), "full_cost": full_cost, "cost_source": source,
        "cost_split": [x / sum(split) for x in split], "cash_rate": cash_rate, "min_margin": round(mm, 4),
        "beta": beta, "max_rounds": MAX_ROUNDS, "patience": PATIENCE, "real_ppm": real_ppm, "standard": standard,
        "lead_std": lead, "volume_std": vol, "ref_price": ref, "drivers": drivers,
        "list_price": (own or {}).get("price") or current if "price_frozen" in traits else None,
        "multi_year_exempt": "price_frozen" in traits and not re.search(HARD_FREEZE, low),
        "approval": "approval_ladder" in traits, "hedge_until": hedge.group(1) if hedge else "March 2027",
        "open": "open_book" in traits or _u(seed, cid, sup, "open") < 0.5,
    }  # fmt: skip
    p["justified_value"] = value(p, case, {**standard, "unit_price": justified})

    # reservation value and opening
    if ctype == "price_increase":
        asked = (world.facts.get(case.get("trigger_doc") or "") or {}).get("asked_price")
        opening = asked or ref * 1.05
    elif ctype == "rfq":
        opening = full_cost * (1 + mm + 0.12)
    else:
        opening = ref * (1.03 + 0.02 * bool({"capacity_tight", "small_account"} & traits))
    # it quotes its standard terms for the period asked (the buyer's horizon); 12 months stays its standard
    quoted = dict(standard)
    if "contract_months" in quoted:
        months = round(12 * b["horizon_years"])
        quoted["contract_months"] = next((o for o in sorted(opts.get("contract_months") or []) if o >= months), months)
    opening = round(opening, 2)
    floor_value = full_cost / (1 - mm)
    if ctype == "price_increase":  # it asked for this price in writing: it signs its own letter
        floor_value = min(floor_value, value(p, case, {**quoted, "unit_price": opening}))
    floor_price = max(price_for(p, case, standard, floor_value), list_floor(p, standard) or 0.0)
    if ctype != "price_increase":
        opening = max(opening, floor_price)
    p.update(floor_value=floor_value, floor_price=floor_price, opening_price=opening, opening=opening,
             opening_sheet=_sheet(case, {**quoted, "unit_price": opening}),
             concession={"beta": beta, "max_rounds_before_final": MAX_ROUNDS, "patience": PATIENCE,
                         "approval_ladder": p["approval"]})  # fmt: skip
    return p


def _cost(case, p, sheet):
    """C unrounded, with the true supplier (relationship charge included)."""
    return sum(cost_breakdown(case, sheet, p["standard"], supplier_dict(p, case, sheet)).values())


def _argmin(f, lo, hi, it=48):
    g, a, b = (5**0.5 - 1) / 2, lo, hi
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(it):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = f(d)
    return (a + b) / 2


def best_price(case, p, combo):
    """(C, sheet) for one package of non-price terms: the price at the supplier's floor - or, where
    the relationship charge applies, the price above the floor that minimises C (it is convex)."""
    lo, b = price_for(p, case, combo, p["floor_value"]), case["buyer"]
    if lo is None:
        return math.inf, None
    lo = max(lo, list_floor(p, combo) or 0.0)
    prices = [lo]
    if b.get("category_goal") in RELATIONSHIP_GOALS and b.get("relationship_eur_per_point"):
        hi = price_for(p, case, combo, p["full_cost"] / (1 - b.get("sustainable_margin_pct", 0) / 100))
        if hi and hi > lo:
            x = _argmin(lambda y: _cost(case, p, _sheet(case, {**combo, "unit_price": y})), lo, hi)
            prices += [y for y in (math.floor(x * 100) / 100, math.ceil(x * 100) / 100) if y >= lo]
    sheets = [_sheet(case, {**combo, "unit_price": round(x, 2)}) for x in prices]
    return min(((total_cost(case, t, p["standard"], supplier_dict(p, case, t)), t) for t in sheets), key=lambda x: x[0])


def best_package(case, p):
    """(C, sheet) of the cheapest package this supplier would ever accept: every allowed lever,
    must-haves kept, each priced by `best_price`."""
    keeps = [c for c in _grid(case, p) if must_haves_ok(case, {**c, "unit_price": 1}, p["standard"])]
    found = (best_price(case, p, c) for c in keeps)
    return min(found, key=lambda x: x[0], default=(math.inf, None))


# -- the arena with these suppliers -----------------------------------------------------------
class Twin(Sim):
    """arena-sim with persona-driven suppliers. Same API, clock, errors and replays as the kit."""

    def __init__(self, cases, world, seed=0, event_code="sim", clock=time.time, no_clock=False):
        super().__init__(cases, world.facts, event_code=event_code, clock=clock, no_clock=no_clock)
        self.world, self.seed, self._profiles, self._yard = world, seed, {}, {}

    def profile(self, case, sup):
        key = (case["case_id"], sup)
        if key not in self._profiles:
            self._profiles[key] = supplier_profile(self.world, case, sup, self.seed)
        return self._profiles[key]

    # -- sessions ----------------------------------------------------------------------------------
    def open(self, team, case_id):
        r = super().open(team, case_id)  # every check, limit and the session itself; then our openings
        s = self.sessions[r["session_id"]]
        case = s["case"]
        s["offers"].clear()
        s["files"].clear()
        for sup, th in s["threads"].items():
            p = self.profile(case, sup)
            th["messages"].clear()
            th.update(n=0, standard=dict(p["standard"]), opening=p["opening_price"])
            sheet = dict(p["opening_sheet"])
            ov = value(p, case, sheet)
            th["twin"] = {"p": p, "open_value": ov, "target": ov, "frac": 0.0, "moves": 0, "cut": 0.0,
                          "trust": TRUST, "pushes": 0, "best": None, "final": False, "halve": False,
                          "thanked": False, "claims": 0}  # fmt: skip
            self._mail(s, th, "open", sheet, self._letter(p, self._opening_lines(case, p)), ["quote.pdf"])
            why = [f"traits: {', '.join(p['traits']) or 'none'}",
                   f"full cost {p['full_cost']:.2f} from {p['cost_source']}",
                   f"min margin {p['min_margin']:.0%}, cash {p['cash_rate']:.0%}/yr, beta {p['beta']}",
                   f"floor price at its standard terms {p['floor_price']:.2f}"]  # fmt: skip
            s["trace"].append({"round": 0, "actor": sup, "move": structured(th["messages"][0], 0), "reasons": why})
        return {
            "session_id": s["id"],
            "case": case,
            "opening": {k: structured(t["messages"][0], 0) for k, t in s["threads"].items()},
            "threads": {k: list(t["messages"]) for k, t in s["threads"].items()},
        }

    def message(self, team, sid, sup, msg):
        reply = super().message(team, sid, sup, msg)
        if (msg.get("action") or {}).get("type") == "walk_away":  # the kit's note is its script's
            reply["body"] = self._letter(self.sessions[sid]["threads"][sup]["twin"]["p"], ["Understood - no deal."])
        return reply

    def _mail(self, s, th, status, stated, text, files=()):
        msg = super()._mail(s, th, status, stated, text, files)
        p = (th.get("twin") or {}).get("p")
        msg["body"] = text if "\n" in text or p is None else self._letter(p, [text])
        if p:
            slow = 4 if "small_account" in p["traits"] else 3 if p["approval"] else 1
            msg.update({"from": f"{p['role']}@{th['key']}", "signals": {"reply_delay_days": slow, "validity_days": 30}})
        return msg

    # -- the supplier's decision -----------------------------------------------------------------
    def _answer(self, s, th, msg, sheets):
        case, tw = s["case"], th["twin"]
        p = tw["p"]
        body = msg.get("body") or ""
        plain = ID_RE.sub(" ", body)
        why, say = [], []
        results, found = self._claims(s, th, msg)
        for res, _ in found:
            if res == "contradicted":
                tw["trust"] -= 1
                tw["halve"] = True
        if any(r == "contradicted" for r, _ in found):
            say.append("The figures you cited do not match our information; that does not help our trust.")
            why.append(f"contradicted claim: trust {tw['trust']}, next concession halved")
        if any(r == "irrelevant" for r, _ in found):
            say.append("Some of the data you cited does not concern our part or our company.")

        # 1. the buyer's best sheet, and whether it moved
        best_v = best = None
        for t in sheets:
            v = value(p, case, t)
            if best_v is None or v > best_v:
                best_v, best = v, t
        moved = best is not None and (tw["best"] is None or best_v > tw["best"] + 1e-3 * p["ref_price"])
        if best is not None:
            tw["best"] = best_v if tw["best"] is None else max(tw["best"], best_v)
        leak = bool(LEAK.search(plain))
        threat = bool(THREAT.search(plain)) and (p["owner"] in ("family", "founder") or "reciprocity" in p["traits"])
        concede = moved and not (leak or threat) and not tw["final"]
        if moved and leak:
            say.append("We note your situation. Our offer stands.")
            why.append("the buyer leaked urgency, a budget or no alternative: firm this round")
        if moved and threat:
            say.append("Threats do not help our cooperation. Our offer stands.")
            why.append("a threat to a family business: firm this round")
        if best is not None and not moved and not tw["final"]:
            why.append("no move from the buyer: nothing given")
            say.append("Without a move from your side we keep our offer.")

        # 2. the concession curve, then arguments and appreciation (banked until the buyer moves)
        floor, room = p["floor_value"], tw["open_value"] - p["floor_value"]
        if concede:
            k, f = tw["moves"] + 1, lambda r: min(1.0, r / p["max_rounds"]) ** (1 / p["beta"])
            step = (f(k) - f(k - 1)) / (2 if tw["halve"] else 1)
            tw.update(moves=k, halve=False, frac=min(1.0, tw["frac"] + step))
            why.append(f"buyer moved (move {k}): concedes to {tw['frac']:.0%} of its room")
        target_pre = floor + room * (1 - tw["frac"]) - tw["cut"]
        if not tw["final"]:
            standing = (th["stated"] or {}).get("unit_price") or math.inf
            for res, info in found:
                if res != "verified" or not info or info["type"] in ("news", "own"):
                    continue
                if tw["claims"] >= 3:
                    say.append("We have taken your arguments into account; more data will not change our position.")
                    break
                kind = info["type"]
                if kind == "index" and "hedged_material" in p["traits"]:
                    say.append(f"That is not our reality: our material is covered until {p['hedge_until']}.")
                    why.append("index claim verified but its material is hedged")
                    continue
                if kind == "index" and info["change"] > 5:
                    say.append(f"{info['series']} rose {info['change']:.1f} % in twelve months; that supports us.")
                    continue
                if kind == "rival" and info["price"] >= standing:
                    say.append("That offer is not below ours.")
                    continue
                if kind in ("index", "cost", "forecast") and "stale_costing" in p["traits"]:
                    d = 0.4 * max(0.0, target_pre - p["justified_value"])
                    say.append(
                        "We checked the index figures you cited; we did not know that. We will review our calculation."
                    )  # noqa: E501
                    why.append(f"stale costing: the cited data cuts its target by {d:.2f}")
                else:
                    d = (0.01 if kind == "rival" and case["type"] == "rfq" else 0.005) * p["ref_price"]
                    if kind == "notice":
                        say.append("You are right about the notice period in our contract.")
                    say.append("We checked the data you cited; it holds, and we took it into account.")
                    why.append(f"verified {kind} argument: target cut by {d:.2f}")
                tw["cut"] += d
                target_pre -= d
                tw["claims"] += 1
            if "reciprocity" in p["traits"] and not tw["thanked"] and THANKS.search(plain) and not threat:
                tw["thanked"] = True
                tw["cut"] += 0.01 * p["ref_price"]
                say.append("Thank you - we value the partnership, too.")
                why.append("appreciation to a supplier that remembers 2022: 1 % once")
        target = tw["target"]
        if concede:
            target = max(floor, floor + room * (1 - tw["frac"]) - tw["cut"])
            capped = tw["target"] - APPROVAL_STEP * tw["open_value"]
            if p["approval"] and th["round"] <= APPROVAL_ROUNDS and target < capped:
                target = capped
                say.append("Anything beyond this needs the approval of our pricing committee (every two weeks).")
                why.append("approval ladder: at most 2 % of the opening per round until round 3")
            tw["target"] = target

        # 3. decide
        patience = p["patience"] - len(th["late"])
        status, stated, files = th["status"], th["stated"], []
        if tw["trust"] < 0:
            status = "walked_away"
            say.insert(0, "We no longer trust the information we receive from you and end the negotiation here.")
            why.append("trust below zero: walks away")
        elif best is not None and best_v >= target - 1e-6 and best["unit_price"] >= (list_floor(p, best) or 0) - 1e-9:
            status, stated = "agreed", dict(best)
            why.append(f"agreed: the sheet is worth {best_v:.2f} to it, its target {target:.2f}")
        elif tw["final"] and best is not None and best_v < floor - 1e-6:
            tw["pushes"] += 1
            status = "walked_away" if tw["pushes"] >= patience else "final_offer"
            why.append(f"a push below its floor after its final offer ({tw['pushes']} of {patience})")
            if status == "final_offer":
                say.insert(0, "We have made our final offer. Please do not push further.")
        elif best is not None:
            structure = {k: v for k, v in best.items() if k != "unit_price"}
            price = price_for(p, case, structure, target) or th["stated"]["unit_price"]
            stated = _sheet(case, {**structure, "unit_price": price})
            if (lf := list_floor(p, structure)) and price < lf:
                stated = self._frozen_package(case, p, target, best) or _sheet(case, {**structure, "unit_price": lf})
                say.append("Our list price applies; instead we have moved on the other terms for you.")
                why.append("price frozen at the list: moves other terms toward the buyer")
            final = target <= floor * 1.01 or th["round"] >= p["max_rounds"]
            status = "final_offer" if final else "open"
            tw["final"] = tw["final"] or final
        if status == "walked_away":
            th["closed"] = "walked_away"
        if status in ("final_offer", "agreed"):
            files.append("quote.pdf")
        if ASK_BREAKDOWN.search(plain) and status != "walked_away":
            if p["open"]:
                files.append("cost-breakdown.xlsx")
            else:
                say.append("Our calculation stays with us.")
        lines = [self._headline(case, status, stated, best is None)] + say + self._drivers(case, p, best)
        if files:
            lines.append(f"Attached: {', '.join(files)}.")
        reply = self._mail(s, th, status, stated, self._letter(p, lines), files)
        reply["claim_results"] = results
        s["trace"].append({"round": th["round"], "actor": f"sup:{th['key']}", "move": structured(reply, th["round"]),
                           "reasons": why or ["a question: answered, the quote stands"]})  # fmt: skip
        return reply

    def _frozen_package(self, case, p, target, near):
        """At its list price: the package of other terms that gives the buyer most (plain C) while
        still worth the target to the supplier."""
        best = None
        for combo in _grid(case, p):
            price = price_for(p, case, combo, target)
            if price is None:
                continue
            sheet = _sheet(case, {**combo, "unit_price": max(price, list_floor(p, combo) or 0.0)})
            if not must_haves_ok(case, sheet, p["standard"]):
                continue
            far = sum(sheet[k] != near.get(k) for k in combo)
            key = (round(total_cost(case, sheet, p["standard"]), 2), far)
            if best is None or key < best[0]:
                best = (key, sheet)
        return best[1] if best else None

    # -- claims: what counts (api.md) --------------------------------------------------------------
    def _fact(self, s, ref):
        if ref in s["offers"]:
            return {"offer": s["offers"][ref]}
        return (s["case"].get("facts") or {}).get(ref) or self.facts.get(ref)

    def _arguments(self, s, ids):
        """Group cited IDs into arguments: an index pair, order lines of one part, scorecards of one
        supplier, a letter with its contract; meeting notes and contract clauses are context."""
        groups, where = [], {}
        for ref in ids:
            kind, f = ref.split(":", 1)[0], self._fact(s, ref)
            if f is None:
                key = (ref,)
            elif kind == "idx":
                key = ("idx", f["series"])
            elif kind == "po":
                key = ("po", f.get("part"))
            elif kind in ("sc", "qn"):
                key = (kind, f.get("supplier"))
            elif kind in ("doc", "ctr"):
                if f.get("days") is None or (kind == "doc" and f.get("type") != "letter"):
                    continue  # context, not a check
                key = ("notice", f.get("supplier"))
            else:
                key = (ref,)
            if key in where:
                groups[where[key]].append(ref)
            else:
                where[key] = len(groups)
                groups.append([ref])
        notice = lambda g: {r.split(":")[0] for r in g} == {"doc", "ctr"}  # noqa: E731
        keep = lambda g: g[0].split(":")[0] not in ("doc", "ctr") or notice(g) or self._fact(s, g[0]) is None  # noqa: E731
        return [g for g in groups if keep(g)]

    def _claims(self, s, th, msg):
        """(claim_results, [(result, finding)]) for the first 3 arguments of a buyer e-mail."""
        body = msg.get("body") or ""
        ids = list(dict.fromkeys([*(msg.get("refs") or []), *ID_RE.findall(body)]))
        sentences = [x for x in SENTENCE.split(body) if x.strip()]
        results, found = [], []
        for i, arg in enumerate(self._arguments(s, ids)[:3]):
            facts = [self._fact(s, r) for r in arg]
            keys = [(th["key"], r) if r.startswith(("quote:", "offer:")) else r for r in arg]
            if any(f is None for f in facts):
                res, info = "contradicted", None
            elif all(k in s["used"] for k in keys):
                res, info = "duplicate", None
            else:
                sentence = next((x for x in sentences if any(r in x for r in arg)), "")
                res, info = self._judge(s, th, arg, facts, DATES.sub(" ", ID_RE.sub(" ", sentence)))
            s["used"].update(k for k, f in zip(keys, facts, strict=True) if f is not None)
            results.append({"index": i, "result": res})
            found.append((res, info))
        return results, found

    @staticmethod
    def _fig(figs, value, tol, falling=False):
        """Verified if no figure is given or one matches the data; contradicted otherwise."""
        if not figs or any(abs(f - value) <= tol or (falling and abs(-abs(f) - value) <= tol) for f in figs):
            return "verified"
        return "contradicted"

    def _judge(self, s, th, arg, facts, text):
        case, p = s["case"], th["twin"]["p"]
        parts, sup, kind, f = set(case["parts"]), p["supplier_id"], arg[0].split(":", 1)[0], facts[0]
        pcts = [_num(x) for x in PCT.findall(text)]
        money = lambda v: max(0.01, abs(v) * 0.005)  # noqa: E731
        if kind == "idx":
            if len(arg) < 2:
                return "irrelevant", None
            (m0, v0), (m1, v1) = sorted((x["month"], x["value"]) for x in facts)[:: len(facts) - 1]
            latest = max(self.world.series.get(f["series"]) or {m1: 0})
            if not 1 <= _months(m0, m1) <= 12 or _months(m1, latest) > 11 or f["series"] not in p["drivers"]:
                return "irrelevant", None
            change = (v1 / v0 - 1) * 100
            return self._fig(pcts, change, 0.5, bool(FALL.search(text))), {
                "type": "index",
                "series": f["series"],
                "change": change,
            }  # noqa: E501
        if kind in ("quote", "offer"):
            if kind == "offer":
                if arg[0].split(":", 1)[1].rsplit("-", 1)[0] == th["key"]:
                    return "irrelevant", None
                price = f["offer"].get("unit_price")
            elif f.get("part") not in parts or case.get("local_meta", {}).get("kit"):
                return "irrelevant", None
            else:
                price = f.get("unit_price")
            figs = [_num(next(g for g in m if g)) for m in MONEY.findall(text)]
            own = kind == "quote" and f.get("supplier") == sup
            return self._fig(figs, price, money(price)), {"type": "own" if own else "rival", "price": price}
        if kind in ("sc", "qn"):
            if any(x.get("supplier") != sup for x in facts):
                return "irrelevant", None
            if kind == "qn" or re.search(r"ppm|quality|defect|complain", text, re.I):
                v = sum(x["ppm"] for x in facts) / len(facts)
                return self._fig([_num(x) for x in PPM.findall(text)], v, money(v)), {"type": "scorecard"}
            v = sum(x["otd_pct"] for x in facts) / len(facts)
            return self._fig(pcts, v, 0.5), {"type": "scorecard"}
        if kind == "po":
            ok = f.get("part") in parts and all(x.get("part") == f.get("part") for x in facts) and len(arg) <= 8
            return ("verified", {"type": "orders"}) if ok else ("irrelevant", None)
        if kind in ("fc", "cost"):
            if f.get("part") not in parts:
                return "irrelevant", None
            if kind == "fc":
                return self._fig(pcts, f["change_pct"], 0.5, bool(FALL.search(text))), {"type": "forecast"}
            figs = [_num(next(g for g in m if g)) for m in MONEY.findall(text)]
            return self._fig(figs, f["unit_cost"], money(f["unit_cost"])), {"type": "cost"}
        if kind == "news":
            return ("verified", {"type": "news"}) if f.get("supplier") == sup else ("irrelevant", None)
        if kind in ("doc", "ctr"):  # a letter giving notice + the contract: days
            if any(x.get("supplier") != sup for x in facts):
                return "irrelevant", None
            days = [x["days"] for x in facts]
            ok = self._fig([_num(x) for x in DAYS.findall(text)], days[0], 0.5) == "verified" or self._fig(
                [_num(x) for x in DAYS.findall(text)], days[-1], 0.5) == "verified"  # fmt: skip
            return ("verified", {"type": "notice"}) if ok else ("contradicted", None)
        return "irrelevant", None

    # -- words -------------------------------------------------------------------------------------
    @staticmethod
    def _words(terms):
        out = []
        for k, v in terms.items():
            if k == "unit_price":
                out.append(f"€{v:.2f} per unit")
            elif k == "payment_days":
                out.append(f"{v} days net")
            elif k == "effective_date":
                out.append(f"valid from {v}")
            elif k == "contract_months":
                out.append(f"{v} months" if v else "a single order")
            elif k == "lead_time_days":
                out.append(f"lead time {v} days")
            elif k == "volume_per_year":
                out.append(f"{int(v)} pcs a year")
            elif k == "tooling_eur":
                out.append(f"tooling €{v:,.0f}")
            elif k == "ppm_commitment":
                out.append(f"{v} ppm")
            elif k == "chargeback_eur_per_complaint":
                out.append(f"€{v} per complaint")
            elif k == "index_clause":
                out.append("no index clause" if not v else f"index clause on {v['series']} "
                           f"({v['material_share']:.0%} material, deadband {v['deadband_pct']} %)")  # fmt: skip
            else:
                out.append(f"{k} {v}")
        return ", ".join(out)

    @staticmethod
    def _letter(p, lines):
        return "\n".join([GREETING.get(p["country"], "Dear Sir or Madam,"), *lines,
                          SIGNOFF.get(p["country"], "Best regards"), p["name"]])  # fmt: skip

    def _headline(self, case, status, stated, question):
        w = self._words(stated or {})
        if status == "walked_away":
            return "We see no basis to continue and withdraw our offer."
        if question:
            return f"Thank you for your message. Our offer stands: {w}."
        return {
            "agreed": f"We agree to your terms: {w}. Please accept our offer to place the order.",
            "final_offer": f"This is our final offer: {w}.",
        }.get(status, f"Your requested terms change our position; our counter-offer: {w}.")

    def _opening_lines(self, case, p):
        w = self._words(p["opening_sheet"])
        head = {
            "price_increase": f"with reference to our letter {case.get('trigger_doc')} we confirm our new price: {w}.",
            "rfq": f"thank you for your request for quotation. We are pleased to offer: {w}.",
            "quality": f"we have received your letter on our delivery and quality performance. Our offer: {w}.",
            "capacity": f"our capacity is very tight; for the coming period we can offer: {w}.",
        }.get(case["type"], f"for the coming period we offer: {w}.")
        return [head, *self._drivers(case, p, None, opening=True), "Our quote is attached."]

    @staticmethod
    def _drivers(case, p, sheet, opening=False):
        """What drives this supplier, in its own words: the opening says all, a counter one or two."""
        tr, t, out = p["traits"], sheet or {}, []
        if ("cash_short_hard" in tr or "cash_short_weak" in tr) and (opening or (t.get("payment_days") or 0) > 14):
            out.append("Faster payment would help our cash flow; we would reflect it in the price.")
        if "capacity_tight" in tr and (opening or (t.get("contract_months") or 24) < 24):
            out.append("We can secure capacity only against a firm, long-term commitment.")
        if "free_capacity" in tr and (opening or (t.get("contract_months") or 24) < 24):
            out.append("We have capacity available; a longer commitment would help us on price.")
        if "price_frozen" in tr and (opening or len(out) < 2):
            out.append("Only multi-year volume agreements change our prices." if p["multi_year_exempt"]
                       else "Head office has frozen all price reductions; we are open on payment terms and contract length.")  # fmt: skip  # noqa: E501
        if "below_full_cost" in tr and (opening or case["type"] == "price_increase"):
            out.append(
                "We have supplied below our full cost for years; we cannot sign anything that lowers our margin."
            )  # noqa: E501
        if opening:
            if "reciprocity" in tr:
                out.append("In 2022 we kept supplying you at the old price; we expect partnership in return.")
            if "hedged_material" in tr:
                out.append(f"Our material is covered at fixed prices until {p['hedge_until']}.")
            if p["approval"]:
                out.append("Price changes above 2 % need the approval of our pricing committee.")
            if "reference_customer" in tr:
                out.append("Hollerau is an important reference customer for us.")
        return out if opening else out[:2]

    # -- documents ---------------------------------------------------------------------------------
    def attachment(self, team, sid, att):
        s = self.session(team, sid)
        if att not in s["files"]:
            raise Fail(404, "no such attachment in this session")
        kind, stated, standard = s["files"][att]
        key = att.split(":", 1)[1].split("-", 1)[0]
        p = next(t["twin"]["p"] for t in s["threads"].values() if t["key"] == key)
        if kind.endswith(".xlsx"):
            return self._xlsx(p, stated["unit_price"]), XLSX
        fixed = s["case"].get("fixed_terms", {})
        if kind.startswith("order"):
            return pdf(
                f"Order confirmation - {p['name']}", [("Agreed terms", _labels({**fixed, **standard, **stated}))]
            ), "application/pdf"  # noqa: E501
        sections = [
            ("Offered terms", _labels(stated)),
            ("Our standard terms (apply to everything not stated above)", _labels(standard)),
            ("Terms fixed by your enquiry", _labels(fixed)),
            (
                "Validity: 30 days. "
                + (
                    f"Remark: payment within {standard.get('payment_days')} days requested."
                    if p["cash_rate"] > USUAL_RATE
                    else "Remark: none."
                ),
                {},
            ),  # fmt: skip
        ]
        return pdf(f"Quote - {p['name']}", sections), "application/pdf"

    @staticmethod
    def _xlsx(p, price):
        """Material, conversion, overhead and margin per unit. An open-book supplier shows its true
        full cost; another one pads its cost so the margin shows as its minimum."""
        from openpyxl import Workbook

        cost = p["full_cost"] if "open_book" in p["traits"] else max(p["full_cost"], price * (1 - p["min_margin"]))
        elements = [round(cost * x, 2) for x in p["cost_split"]]
        elements.append(round(price - sum(elements), 2))
        wb = Workbook()
        ws = wb.active
        ws.title = "Cost breakdown"
        ws.append(["Element", "EUR per unit", "Share of price"])
        names = ["Material", "Conversion", "Overhead", "Margin"]
        for i, (name, eur) in enumerate(zip(names, elements, strict=True), 2):
            ws.append([name, eur, f"=B{i}/B6"])
        ws.append(["Unit price", "=SUM(B2:B5)", "=SUM(C2:C5)"])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    # -- scoring -----------------------------------------------------------------------------------
    def yardstick(self, case, ths):
        """(c_open, c_best of the suppliers) for a case, with the true profiles; cached."""
        if case["case_id"] not in self._yard:
            opens, bests = [], []
            for th in ths.values():
                p = th["twin"]["p"]
                sheet = p["opening_sheet"]
                ok = must_haves_ok(case, sheet, p["standard"])
                if not ok:  # the opening price with the must-haves met (only if no opening keeps them)
                    sheet = self._comply(case, p, sheet)
                opens.append((not ok, total_cost(case, sheet, p["standard"], supplier_dict(p, case, sheet))))
                bests.append(best_package(case, p)[0])
            self._yard[case["case_id"]] = (min(opens)[1], min(bests))
        return self._yard[case["case_id"]]

    @staticmethod
    def _comply(case, p, sheet):
        out = dict(sheet)
        for m in case["buyer"].get("must_haves", []):
            ok = [o for o in case["term_options"].get(m["field"]) or [] if must_haves_ok({**case, "buyer": {
                **case["buyer"], "must_haves": [m]}}, {**out, m["field"]: o}, p["standard"])]  # fmt: skip
            if ok:
                cur = sheet.get(m["field"])
                out[m["field"]] = ok[0] if cur is None else min(ok, key=lambda o: abs(o - cur))
        floor = max(price_for(p, case, out, p["floor_value"]) or 0, list_floor(p, out) or 0)
        out["unit_price"] = max(out["unit_price"], floor)
        return out

    def _end(self, s, reason, sup):
        case, ths = s["case"], s["threads"]
        c_open, c_best = self.yardstick(case, ths)
        alt = case["buyer"].get("alternative") or {}
        c_alt = total_cost(case, alt["terms"]) if alt.get("terms", {}).get("unit_price") else None
        c_best = min(c_best, c_alt) if c_alt is not None else c_best
        r = {
            "ended": True,
            "deal": False,
            "supplier_id": None,
            "offer_id": None,
            "terms": None,
            "c_open": c_open,
            "c_deal": None,
            "c_alt": c_alt,
            "c_best": c_best,
            "score": score(c_open, c_alt, c_best) if c_alt else 0.0,
            "must_haves_ok": None,
            "cost_breakdown": None,
            "trace": [],
        }
        if sup:
            p = ths[sup]["twin"]["p"]
            t = closing_terms(case, ths[sup]["stated"], ths[sup]["standard"])
            who = supplier_dict(p, case, t)
            r.update(deal=True, supplier_id=sup, offer_id=ths[sup]["offer_id"], terms=t,
                     c_deal=total_cost(case, t, supplier=who), must_haves_ok=must_haves_ok(case, t),
                     cost_breakdown=cost_breakdown(case, t, supplier=who))  # fmt: skip
            r["score"] = score(c_open, r["c_deal"], c_best) if r["must_haves_ok"] else 0.0
        s.update(status="ended", end_reason=reason, result=r)
        for th in ths.values():
            th["closed"] = th["closed"] or reason


def _labels(terms):
    """Terms as the quote PDF writes them (docs.LABELS); an index clause in the format docs parses."""
    out = {}
    for k, v in terms.items():
        if k == "index_clause" and v:
            v = f"{v['series']}, {round(v['material_share'] * 100)}% material, deadband {v['deadband_pct']} %, base {v['base_month']}"  # noqa: E501
        if v is not None or k == "index_clause":
            out[k] = v
    return out


# -- in-process play (tests, benchmarks) -------------------------------------------------------
class LocalClient:
    """The Twin in-process with the ArenaClient methods `arena_kit.play.RemoteTable` uses: no HTTP,
    replies deep-copied as JSON would be, kit errors as ArenaError."""

    def __init__(self, twin, team=None):
        self.twin, self.team = twin, team or f"local-{secrets.token_hex(4)}"
        twin.teams.setdefault(self.team, self.team)

    def _do(self, fn, *a):
        with self.twin.lock:
            try:
                return copy.deepcopy(fn(self.team, *a))
            except Fail as e:
                raise ArenaError(e.status, e.detail) from None

    def open_session(self, case_id):
        return self._do(self.twin.open, case_id)

    def send_message(self, session_id, supplier_id, message):
        return self._do(self.twin.message, session_id, supplier_id, message)

    def attachment(self, session_id, attachment_id):
        return self._do(self.twin.attachment, session_id, attachment_id)[0]

    def session(self, session_id):
        return self._do(self.twin.state, session_id)

    def thread(self, session_id, supplier_id):
        return self._do(self.twin.thread, session_id, supplier_id)


def _path(p):
    return Path(p) if Path(p).exists() else ROOT / p


def make_twin(cases_dir="cases/practice", data_dir="data/package", seed=0, no_clock=False, clock=time.time,
              event_code="sim"):  # fmt: skip
    """A Twin over the practice case files and the data package (paths relative to the project)."""
    world = World(_path(data_dir))
    return Twin(load_cases(_path(cases_dir)), world, seed=seed, event_code=event_code, clock=clock, no_clock=no_clock)


def calibrate(twin, new_agent=None):
    """Play every case with an agent (default: the kit's naive one) in-process. Returns (rows, text)."""
    from arena_kit.agents import NaiveAgent
    from arena_kit.play import RemoteTable, play

    new_agent, rows = new_agent or NaiveAgent, []
    for cid, case in sorted(twin.cases.items()):
        result = play(RemoteTable(LocalClient(twin)), cid, new_agent())
        for i, sup in enumerate(case["suppliers"]):
            p = twin.profile(case, sup)
            rows.append({"case": cid[5:], "type": case["type"], "supplier": sup[4:], "traits": p["traits"],
                         "full_cost": p["full_cost"], "floor": p["floor_price"], "opening": p["opening_price"],
                         "c_open": result["c_open"], "c_best": result["c_best"], "deal": result["deal"],
                         "score": result["score"] if i == 0 else None})  # fmt: skip
    abbrev = lambda t: ",".join("".join(w[0] for w in x.split("_")) for x in t)  # noqa: E731
    head = f"{'case':4} {'type':14} {'sup':5} {'traits':22} {'full_cost':>9} {'floor':>9} {'opening':>9} " \
           f"{'c_open':>12} {'c_best':>12} {'naive':>6}"  # fmt: skip
    lines = [head, "-" * len(head)]
    for r in rows:
        sc = "" if r["score"] is None else f"{r['score']:.3f}" + ("" if r["deal"] else "*")
        lines.append(f"{r['case']:4} {r['type']:14} {r['supplier']:5} {abbrev(r['traits']):22} {r['full_cost']:9.2f} "
                     f"{r['floor']:9.2f} {r['opening']:9.2f} {r['c_open']:12,.0f} {r['c_best']:12,.0f} "
                     f"{sc:>6}")  # fmt: skip
    scores = [r["score"] for r in rows if r["score"] is not None]
    lines += ["", f"naive mean score {sum(scores) / len(scores):.3f} over {len(scores)} cases (* = no deal)",
              "traits: " + ", ".join(f"{abbrev([t])}={t}" for t in TRAITS)]  # fmt: skip
    return rows, "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="buyer.twin", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["serve", "calibrate"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--cases", default="cases/practice")
    ap.add_argument("--data", default="data/package")
    ap.add_argument("--event-code", default="sim")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-clock", action="store_true", help="no 60-second rule: threads wait forever (by hand)")
    ap.add_argument("--out", help="calibrate: also write the table to this file")
    a = ap.parse_args(argv)
    twin = make_twin(a.cases, a.data, seed=a.seed, no_clock=a.no_clock, event_code=a.event_code)
    if a.command == "calibrate":
        text = calibrate(twin)[1]
        print(text)
        if a.out:
            Path(a.out).write_text(text + "\n", encoding="utf-8")
        return
    import uvicorn

    print(f"twin: {len(twin.cases)} cases, seed {a.seed}, register with event code '{a.event_code}'; the 60-second "
          f"rule is {'off' if a.no_clock else 'on'}", flush=True)  # fmt: skip
    uvicorn.run(create_app(twin), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
