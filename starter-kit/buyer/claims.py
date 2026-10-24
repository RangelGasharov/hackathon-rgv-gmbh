"""Data arguments the arena can check: one sentence per claim, the figure next to its IDs, computed
from facts.json and checked again before it goes out. Which claims are worth sending depends on the
case type and on the supplier's persona: a hedged supplier shrugs off index moves, a supplier that
does its costing from an old spreadsheet is disarmed by them."""

import re
from dataclasses import dataclass, field

TOL_PP = 0.5


@dataclass
class Claim:
    kind: str
    ids: list
    figure: float | None
    sentence: str
    weight: float  # how much we expect it to help with this supplier, 0-1
    topic: str = ""
    follow: str = ""  # a sentence without IDs that explains the point
    meta: dict = field(default_factory=dict)


LABELS = {
    "LCI-C": "Labour costs in German manufacturing",
    "PPI-D351": "Electricity producer prices in Germany",
    "PPI-D35": "Energy producer prices in Germany",
    "CU-NOTATION": "The copper notation",
    "AS-14404": "The alloy surcharge for 1.4404",
    "AS-14301": "The alloy surcharge for 1.4301",
    "SMARD-DA": "Day-ahead power prices",
    "WB-NGEU": "European gas prices",
}


def series_label(world, series):
    if series in LABELS:
        return LABELS[series]
    r = world.one("SELECT name FROM index_series WHERE id=?", series)
    name = r[0] if r else series
    if name.startswith("Producer prices DE: "):
        return "German producer prices for " + name.removeprefix("Producer prices DE: ").split(",")[0]
    return name.split(" (")[0].split(",")[0]


def check(world, claim, case_facts=None):
    """Re-compute a claim's figure from the facts, as the arena would. True if it holds."""
    facts = {**world.facts, **(case_facts or {})}
    if any(i not in facts and not i.startswith("offer:") for i in claim.ids):
        return False
    if claim.kind == "idx":
        a, b = (facts[i] for i in claim.ids)
        if a["series"] != b["series"]:
            return False
        ch = (b["value"] / a["value"] - 1) * 100
        return abs(ch - claim.figure) <= TOL_PP
    return True


def candidates(world, dossier, case, rivals=None, own_price=None):
    """Every claim we could make to this supplier, best first. `rivals`: {offer_id: price} of other
    suppliers' standing offers in this session (RFQs)."""
    d, out = dossier, []
    ctype = case["type"]
    asked = None
    if d.letter and d.letter.get("asked_price") and d.current:
        asked = (d.letter["asked_price"] / d.current - 1) * 100
    # 1. a price letter that gives less notice than the contract requires
    if d.notice:
        doc, days, ctr, cdays = d.notice
        # say what the agreement says: every contract in the package fixes prices per calendar year (§ 3.1),
        # a hidden case's contract may not
        yearly = "calendar year" in d.clause("price")
        out.append(
            Claim(
                "notice",
                [doc, ctr],
                days,
                f"Your letter ({doc}) gives us only {days} days' notice under our framework agreement ({ctr}).",
                1.0,
                "date",
                f"The agreement fixes prices for each calendar year and requires {cdays} days' notice, "
                f"so the current price applies at least until the end of the year."
                if yearly
                else f"The agreement requires {cdays} days' notice for a price change.",
            )
        )
    # 2. cost drivers that fell, or rose far less than the ask: never cite one that supports a rise
    hedged = d.has("hedged_material")
    idx_claims = []
    for series, w, ch, i0, i1, m0, m1 in d.drivers:
        if ch >= 0:
            # arena replay P01: citing a driver that rose (even far less than the ask) got
            # "your figures confirm our case": only falling drivers are arguments for us
            continue
        weight = 0.9
        if d.has("stale_costing"):
            weight = 1.0  # arena: "we did not know that - a fair point"
        if hedged and d.hedge_covers(series):
            weight = 0.05  # arena P08: "that is not our reality: our material is covered at fixed prices"
        elif hedged:
            weight *= 0.6  # a series its hedge does not name (P08: labour, bar drawing): untested, no harm seen
        elif d.has("cash_short", strong=True):
            weight *= 0.3  # "we see your figures, but we cannot give up margin right now"
        if m1 < world.series_last.get(series, m1):
            weight *= 0.85  # an older window: after the current ones, so P18's forecast still goes in round 1
        label = series_label(world, series)
        idx_claims.append(
            Claim(
                "idx",
                [i0, i1],
                ch,
                f"{label} changed {ch:+.1f}% from {m0} to {m1} ({i0}, {i1}).",
                round(weight * (0.9 + 0.1 * min(1.0, w / 0.3)), 2),  # heavier drivers first
                "cost",
                meta={"series": series, "weight": w},
            )
        )
    if idx_claims and asked is not None and d.justified_pct < asked - 1:
        idx_claims.sort(key=lambda c: -c.weight)
        idx_claims[0].follow = (
            f"Weighted by the cost structure of this part, the index data supports at most "
            f"{max(d.justified_pct, 0):+.1f}%, not {asked:+.1f}%."
        )
    out += idx_claims
    # 3. quality and delivery record
    sc = d.scorecard
    if sc and not d.has("capacity_tight"):  # a supplier that allocates capacity shrugs off its record
        wants_ppm = bool(sc["ppm"] and sc["ppm"] > 500)
        if wants_ppm:
            out.append(
                Claim(
                    "sc",
                    [sc["id"]],
                    sc["ppm"],
                    f"Your defect rate over the last twelve months was {sc['ppm']:.0f} ppm ({sc['id']}).",
                    0.95 if ctype == "quality" else 0.7,
                    "quality",
                )
            )
        # on-time delivery from rows the ppm claim does not use: each ID counts once per case
        rows = (
            world.all(
                "SELECT id, otd_pct FROM scorecards WHERE supplier_id=? AND id!=? ORDER BY quarter DESC LIMIT 2",
                d.sid,
                sc["id"],
            )
            if wants_ppm
            else [(sc["id"], sc["otd_pct"])]
        )
        rows = [r for r in rows if r[1] is not None]
        if rows:
            mean = round(sum(r[1] for r in rows) / len(rows), 1)
            ids = [r[0] for r in rows]
            if mean < 93:
                out.append(
                    Claim(
                        "sc_otd",
                        ids,
                        mean,
                        f"Your on-time delivery was {mean:.1f}% against our 95% target ({', '.join(ids)}).",
                        # arena (23 sessions): "the delivery record ... does not change our calculation" 6 of 6
                        # times; only a supplier on an old cost sheet said "we did not know that - a fair point"
                        0.8 if d.has("stale_costing") else 0.12,
                        "delivery",
                    )
                )
    # 4. our volume outlook: what volume-hungry and capacity-rich suppliers want to hear. Any rise counts: +1.9 %
    # came back "we took it into account" 4 of 4 times (P08, P18). A small account told it is "not strategic" shrugs
    # it off (P04, 4 of 4); one "reviewing small accounts, focus on customers with volume" counts it (P18)
    for n, (fid, ch) in enumerate(sorted(d.forecasts, key=lambda x: -x[1])):
        if ch <= 0:
            continue
        cold = d.has("capacity_tight") or d.not_strategic
        w = 0.2 if cold else 0.85 if d.has("stale_costing") else 0.8
        what = "this part" if len(d.forecasts) == 1 else f"part {world.facts[fid]['part'].split(':')[1]} of the kit"
        out.append(
            Claim(
                "fc",
                [fid],
                ch,
                f"Our demand forecast for {what} has risen {ch:+.1f}% ({fid}).",
                round(w - 0.05 * n, 2),
                "volume",
            )
        )
    # 5. should-cost, only when it speaks for us
    sc_cost, how = d.should_cost
    ref = (d.letter or {}).get("asked_price") or d.current
    cid = f"cost:{d.part.split(':')[1]}"
    unit_cost = (world.facts.get(cid) or {}).get("unit_cost")
    if unit_cost and ref and ref > unit_cost * 1.02 and not d.has("below_full_cost"):
        out.append(
            Claim(
                "cost",
                [cid],
                unit_cost,
                f"Our cost model puts the full cost of this part at €{unit_cost:.2f} per unit ({cid}).",
                0.75,
                "cost",
            )
        )
    # 6. a cheaper alternative, cited only when it is cheaper than what this supplier asks
    alt = case["buyer"].get("alternative") or {}
    ask = ref or d.reference_price
    if alt.get("ref") and alt.get("terms", {}).get("unit_price") and ask and alt["terms"]["unit_price"] < ask * 0.99:
        p = alt["terms"]["unit_price"]
        out.append(
            Claim(
                "quote",
                [alt["ref"]],
                p,
                f"We hold a quote of €{p:.2f} for the same part ({alt['ref']}).",
                0.85,
                "competition",
            )
        )
    # a recent quote from another supplier for the same part, far below this one's ask: the only argument
    # that ever moved a supplier past its limit ("the buyer shows a much lower rival quote. I give half the
    # step now and check the rest" - arena P05). Not in the case refs, the arena may call it irrelevant
    # (no trust cost); the case's own alternative is cited above.
    stated = own_price or ask  # the supplier's own standing price (its quote), else today's price
    if stated and not rivals:
        seen = {alt.get("ref")}
        for q in world.all(
            "SELECT id, supplier_id, unit_price, quote_date FROM quotes WHERE part_id=? AND supplier_id!=? "
            "AND quote_date>='2025-01-01' ORDER BY quote_date>='2026-07-01' DESC, unit_price LIMIT 3",
            d.part,
            d.sid,
        ):
            if q[0] in seen or q[2] >= stated * 0.97 or len(case.get("parts") or []) > 1:
                continue
            if q[0] not in (case.get("refs") or []):  # b3 P18: "the rival quote data you cite does not concern
                continue  # this part or our company" - only quotes of this case count (api.md)
            recent = q[3] >= "2026-07-01"  # an older quote may still count, lower in the order
            far = q[2] < stated * 0.9  # "a much lower rival quote": the two-step (arena P05)
            when = "" if recent else f" in {q[3][:4]}"
            out.append(
                Claim(
                    "quote",
                    [q[0]],
                    q[2],
                    f"Another supplier quoted €{q[2]:.2f} for the same part{when} ({q[0]}).",
                    (0.97 if far else 0.9) if recent else (0.7 if far else 0.5),
                    "competition",
                    meta={"rival": q[1]},
                )
            )
            break
    for oid, price in sorted((rivals or {}).items(), key=lambda kv: kv[1])[:1]:
        out.append(
            Claim(
                "offer",
                [oid],
                price,
                f"Another supplier offers €{price:.2f} for the same part ({oid}).",
                0.95 if ctype == "rfq" else 0.85,
                "competition",
            )
        )
    # 7. order history: what we paid so far (a price letter asks for more than that)
    if d.recent_orders and len(d.recent_orders) >= 2 and ctype in ("price_increase", "renewal", "raw_material"):
        ids = [o[0] for o in d.recent_orders[:3]]
        price = d.recent_orders[0][1]
        # raw material, while what we pay today is below its standing price: team P08 "We pay you EUR 5.45 per unit
        # today (po:..., po:...)" came back "we checked the price history you cited; it holds and we took it into
        # account"
        below = ctype == "raw_material" and stated and price < stated * 0.99
        if below or ctype != "raw_material":
            out.append(
                Claim(
                    "po",
                    ids[:2] if below else ids,
                    price,
                    f"We pay you €{price:.2f} per unit today ({', '.join(ids[:2] if below else ids)}).",
                    0.6 if below else 0.1,
                    "history",
                )
            )
    # 8. news about the supplier that speaks for us (free capacity, secured energy, new plant)
    for nid, _topic, headline in d.news_items[:1]:
        w = 0.1
        out.append(
            Claim("news", [nid], None, f"We read that {headline[0].lower() + headline[1:]} ({nid}).", w, "context")
        )
    dead = getattr(d, "dead_kinds", set())  # what the supplier called contradicted, irrelevant, duplicate, no change
    out = [c for c in out if dead_key(c) not in dead]
    good = [c for c in out if c.weight >= 0.15 and check(world, c, case.get("facts"))]
    return sorted(good, key=lambda c: -c.weight)


def kind_key(claim):
    """What a supplier counts once (arena v3 sessions): a second forecast, even for another kit part, or a
    second scorecard row, even for another metric, comes back `duplicate` ("we have already taken that
    volume change data into account"). Index series count separately, rival offers and quotes per ID."""
    if claim.kind == "idx":
        return ("idx", claim.meta.get("series"))
    if claim.kind in ("sc", "sc_otd"):
        return ("sc",)
    if claim.kind in ("offer", "quote"):
        return (claim.kind, *claim.ids)
    return (claim.kind,)


def dead_key(claim):
    """What a rejection retires: one index series, one rival price (a later, lower offer is a new ID and may
    still count), else the whole kind."""
    return kind_key(claim) if claim.kind in ("idx", "offer", "quote") else claim.kind


# how the arena's replies name the data it checked ("We checked the volume change data you cited; it holds, but
# it does not change our calculation") and the twin's wording, mapped to the claim kinds they retire
NO_CHANGE = re.compile(
    r"checked the (.+?) data you cited; (?:it holds|both hold|these hold), but[^.]*?does not change", re.I
)
NAMES = {
    "volume change": ("fc",), "forecast": ("fc",),
    "delivery record": ("sc_otd",), "delivery performance": ("sc_otd",), "scorecard": ("sc_otd",),
    "should cost": ("cost",), "cost": ("cost",),
}  # fmt: skip


def no_change_kinds(body):
    """Claim kinds the supplier says it checked but that do not change its calculation. Index data and rival
    prices are never retired this way: a later, lower rival offer or another series may still count."""
    out = set()
    for m in NO_CHANGE.finditer(body or ""):
        for name, kinds in NAMES.items():
            if name in m.group(1).lower():
                out.update(kinds)
    return out


def ration(claims, used, round_no, kinds=(), per_message=3, later_min=0.5):
    """The strongest fresh claims, `per_message` of them (the arena checks three arguments per message, each ID
    once per case), at most one of each kind per supplier: `kinds` are the kind keys this supplier has already
    heard. After the first message only claims of weight `later_min` or more."""
    fresh = [c for c in claims if not any(i in used for i in c.ids)]
    picked, ids, seen = [], set(), set(kinds)
    for c in fresh:
        if len(picked) >= per_message:
            break
        if ids & set(c.ids) or kind_key(c) in seen or (round_no and c.weight < later_min):
            continue
        picked.append(c)
        ids |= set(c.ids)
        seen.add(kind_key(c))
    return picked
