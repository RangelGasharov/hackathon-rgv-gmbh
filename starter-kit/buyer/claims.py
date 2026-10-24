"""Data arguments the arena can check: one sentence per claim, the figure next to its IDs, computed
from facts.json and checked again before it goes out. Which claims are worth sending depends on the
case type and on the supplier's persona: a hedged supplier shrugs off index moves, a supplier that
does its costing from an old spreadsheet is disarmed by them."""

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


def candidates(world, dossier, case, rivals=None):
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
        out.append(
            Claim(
                "notice",
                [doc, ctr],
                days,
                f"Your letter ({doc}) gives us only {days} days' notice under our framework agreement ({ctr}).",
                1.0,
                "date",
                f"The agreement fixes prices for each calendar year and requires {cdays} days' notice, "
                f"so the current price applies at least until the end of the year.",
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
        weight = 0.95
        if d.has("stale_costing"):
            weight = min(1.0, weight + 0.2)
        if hedged:
            weight *= 0.2
        if ctype in ("quality", "capacity"):
            weight *= 0.4
        label = series_label(world, series)
        idx_claims.append(
            Claim(
                "idx",
                [i0, i1],
                ch,
                f"{label} changed {ch:+.1f}% from {m0} to {m1} ({i0}, {i1}).",
                round(weight * min(1, 0.6 + w), 2),
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
    if sc and ctype in ("quality", "capacity") or (sc and sc["colour"] == "red"):
        if sc["ppm"] and sc["ppm"] > 1000:
            out.append(
                Claim(
                    "sc",
                    [sc["id"]],
                    sc["ppm"],
                    f"Your defect rate over the last twelve months was {sc['ppm']:.0f} ppm ({sc['id']}).",
                    0.9 if ctype == "quality" else 0.5,
                    "quality",
                )
            )
        if sc["otd_pct"] and sc["otd_pct"] < 85:
            out.append(
                Claim(
                    "sc_otd",
                    [sc["id"]],
                    sc["otd_pct"],
                    f"Your on-time delivery over the last twelve months was {sc['otd_pct']:.1f}% ({sc['id']}).",
                    0.8 if ctype in ("quality", "capacity") else 0.4,
                    "delivery",
                )
            )
    # 4. our volume outlook: what volume-hungry and capacity-rich suppliers want to hear
    if d.forecast and d.forecast[1] > 3:
        fid, ch = d.forecast
        w = 0.7 if (d.has("small_account") or d.has("free_capacity") or d.has("price_frozen")) else 0.35
        if d.has("capacity_tight"):
            w = 0.5  # with a binding commitment, volume buys priority
        out.append(
            Claim("fc", [fid], ch, f"Our demand forecast for this part has risen {ch:+.1f}% ({fid}).", w, "volume")
        )
    # 5. should-cost, only when it speaks for us
    sc_cost, how = d.should_cost
    ref = (d.letter or {}).get("asked_price") or d.current
    if sc_cost and ref and how and how.startswith("own") and ref > sc_cost * 1.12:
        cid = f"cost:{d.part.split(':')[1]}"
        if cid in world.facts:
            out.append(
                Claim(
                    "cost",
                    [cid],
                    world.facts[cid]["unit_cost"],
                    f"Our cost model puts the full cost of this part at €{world.facts[cid]['unit_cost']:.2f} per unit ({cid}).",
                    0.5 if d.has("open_book") else 0.3,
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
                0.7,
                "competition",
            )
        )
    for oid, price in sorted((rivals or {}).items(), key=lambda kv: kv[1])[:1]:
        out.append(
            Claim(
                "offer",
                [oid],
                price,
                f"Another supplier offers €{price:.2f} for the same part ({oid}).",
                0.8,
                "competition",
            )
        )
    good = [c for c in out if c.weight >= 0.15 and check(world, c, case.get("facts"))]
    return sorted(good, key=lambda c: -c.weight)


def ration(claims, used, round_no):
    """At most three claims per message, each ID once per case; the strongest early, one in reserve."""
    fresh = [c for c in claims if not any(i in used for i in c.ids)]
    limit = 3 if round_no == 0 else 2
    picked, ids = [], set()
    for c in fresh:
        if len(picked) >= limit:
            break
        if ids & set(c.ids):
            continue
        picked.append(c)
        ids |= set(c.ids)
    return picked
