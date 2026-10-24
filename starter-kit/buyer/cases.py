"""Local stand-ins for the 19 practice case files (P01-P19), built from the data package.

The arena hands out the real case files only through its API. These follow the same format as the
kit's demo cases (sim_cases/S01-S03) and the brief: need from the latest forecast, current price from
the price conditions, rates by category goal, the September 2026 second-source quote as the
alternative, and a trigger document per case. They are a test bed, not the arena's numbers.

    uv run python -m buyer.cases            # writes cases/practice/P01.json ... P19.json
"""

import json
import sqlite3
import sys
from pathlib import Path

GOAL_RATES = {  # fallback risk %, index risk %, cost per defect €, late cost €/day
    "leverage": (3, 1, 50, 200),
    "routine": (5, 2, 30, 150),
    "stabilise": (10, 4, 80, 400),
    "secure_supply": (12, 4, 80, 600),
    "quality_first": (6, 2, 200, 300),
}
PAY = [14, 30, 45, 60, 90]

# case -> (type, goal, suppliers, parts, extra)
SPECS = {
    "P01": ("price_increase", "stabilise", ["sup:70110"], ["part:310010"], {}),
    "P02": ("price_increase", "stabilise", ["sup:70510"], ["part:350011"], {}),
    "P03": ("renewal", "stabilise", ["sup:70530"], ["part:350020"], {}),
    "P04": ("renewal", "routine", ["sup:70620"], ["part:360020"], {}),
    "P05": ("rfq", "leverage", ["sup:70210", "sup:70240", "sup:70250"], ["part:320070"], {"need": 4200}),
    "P06": ("rfq", "stabilise", ["sup:70120", "sup:70150"], ["part:310070"], {"need": 4200}),
    "P07": (
        "bundling",
        "routine",
        ["sup:70610"],
        ["part:360010", "part:360011", "part:360012", "part:360030"],
        {"kit": [19, 6, 21, 23], "need": 30000},
    ),
    "P08": ("raw_material", "leverage", ["sup:70710"], ["part:370010"], {"series": "AS-14404"}),
    "P09": ("capacity", "secure_supply", ["sup:70220"], ["part:320030"], {}),
    "P10": ("capacity", "secure_supply", ["sup:70410"], ["part:340010"], {}),
    "P11": ("quality", "quality_first", ["sup:70140"], ["part:310020"], {}),
    "P12": ("quality", "quality_first", ["sup:70310"], ["part:330010"], {}),
    "P13": ("price_increase", "leverage", ["sup:70230"], ["part:320060"], {}),
    "P14": ("renewal", "stabilise", ["sup:70560"], ["part:350040"], {"series": "CU-NOTATION"}),
    "P15": (
        "bundling",
        "routine",
        ["sup:70430"],
        ["part:340020", "part:340030", "part:340040"],
        {"kit": [2, 1, 2], "need": 25000},
    ),
    "P16": ("raw_material", "leverage", ["sup:70720"], ["part:370020"], {"series": "PPI-C241"}),
    "P17": ("price_increase", "stabilise", ["sup:70550"], ["part:350030"], {}),
    "P18": ("raw_material", "leverage", ["sup:70740"], ["part:370010"], {"series": "AS-14404"}),
    "P19": ("renewal", "routine", ["sup:70640"], ["part:360020"], {}),
}


class World:
    def __init__(self, package):
        self.root = Path(package)
        self.db = sqlite3.connect(self.root / "world.sqlite")
        self.db.row_factory = sqlite3.Row
        self.facts = json.loads((self.root / "facts.json").read_text())

    def one(self, sql, *a):
        return self.db.execute(sql, a).fetchone()

    def all(self, sql, *a):
        return self.db.execute(sql, a).fetchall()

    def price(self, part, sup=None):
        if sup:
            r = self.one(
                "SELECT price FROM price_conditions WHERE part_id=? AND supplier_id=? ORDER BY valid_from DESC LIMIT 1",
                part,
                sup,
            )
            if r:
                return r[0]
        r = self.one("SELECT price FROM price_conditions WHERE part_id=? ORDER BY valid_from DESC LIMIT 1", part)
        return r[0] if r else None

    def need(self, part):
        fc = sorted(
            (k.split("@")[1], v["volume_per_year"])
            for k, v in self.facts.items()
            if k.startswith("fc:") and v["part"] == part
        )
        return fc[-1][1] if fc else None

    def index_change(self, series, months=12):
        rows = self.all(
            "SELECT month, value FROM index_values WHERE series_id=? ORDER BY month DESC LIMIT ?", series, months + 1
        )
        return rows[0][1] / rows[-1][1] - 1 if len(rows) > months else 0.0

    def should_cost(self, part, sup=None):
        r = self.one(
            "SELECT unit_cost FROM should_cost WHERE part_id=? ORDER BY (supplier_id=?) DESC LIMIT 1", part, sup
        )
        return r[0] if r else None


def build(world, cid):
    ctype, goal, sups, parts, extra = SPECS[cid]
    sup = sups[0]
    trigger = world.one("SELECT id, subject FROM documents WHERE id GLOB ?", f"*-{cid}")
    s = world.one("SELECT * FROM suppliers WHERE id=?", sup)
    fr, ir, defect, late = GOAL_RATES[goal]
    part = parts[0]
    if "kit" in extra:
        current = round(
            sum(
                q
                * (world.price(p, sup) or world.price(p))
                / (world.one("SELECT price_unit FROM parts WHERE id=?", p)[0])
                for p, q in zip(parts, extra["kit"])
            ),
            2,
        )
        title_part = "Kit"
    else:
        current = world.price(part, sup)
        title_part = world.one("SELECT description FROM parts WHERE id=?", part)[0]
    unit = 1 if "kit" in extra else world.one("SELECT price_unit FROM parts WHERE id=?", part)[0]
    need = extra.get("need") or round(world.need(part) / unit)  # in the part's price unit, as the arena negotiates
    sc = world.should_cost(part, sup)
    if current is None:  # a new part: priced off should-cost
        current = None
        fallback = round(sc * 1.35, 2)
        ref_price = round(sc * 1.18, 2)
    else:
        fallback = round(current * 1.15, 2)
        ref_price = current
    lead = s["standard_lead_time_days"]
    freight = {"EXW": round(ref_price * 0.03, 2), "FCA": round(ref_price * 0.02, 2), "DAP": 0.0}
    std_inc = s["incoterm"]
    negotiable, fixed = ["unit_price", "payment_days"], {}
    options: dict[str, list] = {"payment_days": PAY}
    must = []
    index_paths = {}
    eff_opts = None
    if ctype == "price_increase":
        letter = world.one("SELECT effective_date FROM documents WHERE id=?", trigger["id"])
        eff = letter["effective_date"]
        eff_opts = sorted({eff, "2027-01-01", "2027-04-01"})
        negotiable += ["effective_date", "contract_months"]
        options.update(effective_date=eff_opts, contract_months=[12, 24, 36])
        fixed.update(lead_time_days=lead, volume_per_year=need, incoterm=std_inc, index_clause=None, tooling_eur=0)
    elif ctype == "renewal":
        negotiable += ["contract_months", "incoterm"]
        options.update(contract_months=[12, 24, 36], incoterm=["EXW", "FCA", "DAP"])
        fixed.update(lead_time_days=lead, volume_per_year=need, tooling_eur=0, effective_date="2027-01-01")
        if extra.get("series"):
            negotiable.append("index_clause")
            ser = extra["series"]
            options["index_clause"] = [
                None,
                {"series": ser, "material_share": 0.55, "deadband_pct": 0, "base_month": "2026-09"},
                {"series": ser, "material_share": 0.55, "deadband_pct": 5, "base_month": "2026-09"},
            ]
            index_paths[ser] = round(world.index_change(ser) * 0.5, 3)
        else:
            fixed["index_clause"] = None
    elif ctype == "rfq":
        negotiable += ["lead_time_days", "incoterm", "tooling_eur"]
        options.update(
            lead_time_days=[28, 35, 42, 56, 70], incoterm=["EXW", "FCA", "DAP"], tooling_eur=[0, 6000, 12000]
        )
        fixed.update(volume_per_year=need, contract_months=24, index_clause=None, effective_date="2027-03-01")
        lead = 42
        must.append({"field": "lead_time_days", "op": "<=", "value": 56})
    elif ctype == "bundling":
        negotiable += ["contract_months", "incoterm"]
        options.update(contract_months=[12, 24, 36], incoterm=["FCA", "DAP"])
        fixed.update(
            lead_time_days=lead, volume_per_year=need, index_clause=None, tooling_eur=0, effective_date="2027-01-01"
        )
    elif ctype == "raw_material":
        ser = extra["series"]
        negotiable += ["index_clause", "contract_months"]
        options.update(
            contract_months=[12, 24],
            index_clause=[
                None,
                {"series": ser, "material_share": 0.45, "deadband_pct": 0, "base_month": "2026-09"},
                {"series": ser, "material_share": 0.45, "deadband_pct": 3, "base_month": "2026-09"},
                {"series": ser, "material_share": 0.8, "deadband_pct": 3, "base_month": "2026-09"},
            ],
        )
        index_paths[ser] = round(world.index_change(ser) * 0.5, 3)
        fixed.update(
            lead_time_days=lead, volume_per_year=need, incoterm="DAP", tooling_eur=0, effective_date="2027-01-01"
        )
    elif ctype == "capacity":
        negotiable += ["volume_per_year", "contract_months", "lead_time_days"]
        vol = [round(need * f, -1) for f in (0.8, 0.9, 1.0)]
        vol[-1] = need
        options.update(
            volume_per_year=vol, contract_months=[12, 24], lead_time_days=[lead, lead + 14, lead + 28, lead + 42]
        )
        fixed.update(incoterm=std_inc, index_clause=None, tooling_eur=0, effective_date="2027-01-01")
        lead = lead + 14  # the lead time Hollerau can live with
    elif ctype == "quality":
        negotiable += ["ppm_commitment", "chargeback_eur_per_complaint"]
        options.update(ppm_commitment=[500, 1000, 1500, 2500, 4000], chargeback_eur_per_complaint=[0, 50, 150, 300])
        fixed.update(
            lead_time_days=lead,
            volume_per_year=need,
            incoterm=std_inc,
            contract_months=24,
            index_clause=None,
            tooling_eur=0,
            effective_date="2027-01-01",
        )
        must.append({"field": "ppm_commitment", "op": "<=", "value": 1500})
    alt = {"kind": "none", "ref": None, "terms": {}}
    if ctype not in ("rfq",) and "kit" not in extra:
        q = world.one(
            "SELECT * FROM quotes WHERE part_id=? AND supplier_id!=? AND quote_date>='2026-09-01' AND id GLOB 'quote:72*' "
            "ORDER BY unit_price LIMIT 1",
            part,
            sup,
        )
        if q:
            alt = {
                "kind": "quote",
                "ref": q["id"],
                "terms": {
                    "unit_price": q["unit_price"],
                    "payment_days": q["payment_days"],
                    "lead_time_days": q["lead_time_days"],
                    "incoterm": q["incoterm"] or "EXW",
                    "volume_per_year": need,
                    "contract_months": 24,
                    "index_clause": None,
                    "tooling_eur": 0,
                    "effective_date": "2027-01-01",
                },
            }
    elif "kit" in extra:
        alt = {
            "kind": "current_contract",
            "ref": None,
            "terms": {
                "unit_price": round(current * 1.04, 2),
                "payment_days": 30,
                "lead_time_days": lead,
                "incoterm": "DAP",
                "volume_per_year": need,
                "contract_months": 24,
                "index_clause": None,
                "tooling_eur": 0,
                "effective_date": "2027-01-01",
            },
        }
    buyer = {
        "need_per_year": need,
        "horizon_years": 2,
        "current_unit_price": current,
        "fallback_unit_price": fallback,
        "interest_rate": 0.08,
        "reference_payment_days": 60,
        "need_lead_time_days": lead,
        "category_goal": goal,
        "fallback_risk_pct": fr,
        "index_risk_pct": ir,
        "late_cost_per_day": late,
        "cost_per_defect": defect,
        "freight_eur_per_unit": freight,
        "index_paths": index_paths,
        "must_haves": must,
        "alternative": alt,
    }
    if goal in ("stabilise", "secure_supply"):
        spend = need * ref_price
        buyer.update(sustainable_margin_pct=8, relationship_eur_per_point=round(spend * 0.04, -2))
    refs = [trigger["id"], s["contract_id"], f"sc:{sup[4:]}@2026Q3"] + ([f"cost:{part[5:]}"] if sc else [])
    for d in world.all("SELECT series_id FROM cost_drivers WHERE part_id=? ORDER BY weight DESC LIMIT 2", part):
        last = world.one("SELECT max(month) FROM index_values WHERE series_id=?", d[0])[0]
        prev = f"{int(last[:4]) - 1}{last[4:]}"
        refs += [f"idx:{d[0]}@{prev}", f"idx:{d[0]}@{last}"]
    names = ", ".join(world.one("SELECT name FROM suppliers WHERE id=?", x)[0] for x in sups)
    briefing = (
        f"{ctype.replace('_', ' ').title()}: {trigger['subject']}. You buy {', '.join(parts)} for Hollerau Pumpenwerke "
        f"from {names}. Need: {need} a year for 2 years. Category goal: {goal}."
    )
    return {
        "case_id": f"case:{cid}",
        "set": "practice",
        "type": ctype,
        "title": f"{title_part}: {trigger['subject']}",
        "briefing": briefing,
        "trigger_doc": trigger["id"],
        "parts": parts,
        "suppliers": sups,
        "negotiable": negotiable,
        "fixed_terms": fixed,
        "term_options": options,
        "buyer": buyer,
        "round_limit": 10,
        "refs": [r for r in refs if r],
        "facts": {},
        "hosted": True,
        "local_meta": {"kit": extra.get("kit"), "should_cost": sc, "reference_price": ref_price},
    }


def main(package="data/package", out="cases/practice"):
    world = World(package)
    Path(out).mkdir(parents=True, exist_ok=True)
    for cid in SPECS:
        case = build(world, cid)
        (Path(out) / f"{cid}.json").write_text(json.dumps(case, indent=1, ensure_ascii=False))
        b = case["buyer"]
        print(
            cid,
            case["type"],
            b["category_goal"],
            "need",
            b["need_per_year"],
            "current",
            b["current_unit_price"],
            "fallback",
            b["fallback_unit_price"],
            "alt",
            b["alternative"]["ref"],
        )


if __name__ == "__main__":
    main(*sys.argv[1:])
