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
            # its own scorecard rows (not the one the ppm claim uses: each ID counts once per case)
            prev = world.all(
                "SELECT id, otd_pct FROM scorecards WHERE supplier_id=? AND id!=? ORDER BY quarter DESC LIMIT 2",
                d.sid,
                sc["id"],
            )
            if len(prev) == 2:
                mean = round(sum(r[1] for r in prev) / 2, 1)
                ids = [r[0] for r in prev]
                out.append(
                    Claim(
                        "sc_otd",
                        ids,
                        mean,
                        f"Your on-time delivery averaged {mean:.1f}% against the 95% target ({ids[0]}, {ids[1]}).",
                        0.8 if ctype in ("quality", "capacity") else 0.4,
                        "delivery",
                    )
                )
    # 4. our volume outlook: what volume-hungry and capacity-rich suppliers want to hear
    for n, (fid, ch) in enumerate(sorted(d.forecasts, key=lambda x: -x[1])):
        if ch <= 3:
            continue
        w = 0.7 if (d.has("small_account") or d.has("free_capacity") or d.has("price_frozen")) else 0.4
        if d.has("capacity_tight"):
            w = 0.5  # with a binding commitment, volume buys priority
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
    # 7. order history: what we paid so far (a price letter asks for more than that)
    if d.recent_orders and len(d.recent_orders) >= 2 and ctype in ("price_increase", "renewal", "raw_material"):
        ids = [o[0] for o in d.recent_orders[:3]]
        price = d.recent_orders[0][1]
        out.append(
            Claim(
                "po",
                ids,
                price,
                f"Our recent orders for this part were placed at €{price:.2f} ({', '.join(ids)}).",
                0.3,
                "history",
            )
        )
    # 8. news about the supplier that speaks for us (free capacity, secured energy, new plant)
    for nid, topic, headline in d.news_items[:1]:
        w = 0.45 if topic in ("free_capacity", "new_plant") else 0.35
        out.append(
            Claim("news", [nid], None, f"We read that {headline[0].lower() + headline[1:]} ({nid}).", w, "context")
        )
    if d.has("capacity_tight"):
        out = [c for c in out if c.kind not in ("sc_otd", "cost")]
    dead = getattr(d, "dead_kinds", set())  # claim kinds the supplier marked contradicted or irrelevant
    out = [c for c in out if c.kind not in dead]
    good = [c for c in out if c.weight >= 0.15 and check(world, c, case.get("facts"))]
    return sorted(good, key=lambda c: -c.weight)


def ration(claims, used, round_no):
    """At most three claims per message, each ID once per case; the strongest early, one in reserve."""
    fresh = [c for c in claims if not any(i in used for i in c.ids)]
    limit = 3 if round_no == 0 else 2  # suppliers move on new verified arguments: keep bringing fresh ones
    picked, ids = [], set()
    for c in fresh:
        if len(picked) >= limit:
            break
        if ids & set(c.ids):
            continue
        picked.append(c)
        ids |= set(c.ids)
    return picked
