"""Which arguments count with which supplier: replay results per argument kind and supplier persona.

The arena tells us after every message which of our arguments it checked (`claim_results`) and, in the
supplier's words, whether each one counted: "it holds and we took it into account", "we did not know that - a
fair point", or "it holds, but it does not change our calculation" / "that is not our reality". This module
keeps those results as a table per (argument kind, persona) and turns them into the weight `claims.candidates`
gives an argument:

    weight = (K x prior + sum of what each logged argument did) / (K + number logged)

`PRIOR` is the hand-written rule the code used before (worth K arguments); a logged argument adds its full
value when it counted (a little more for a fair point) and 0 when it did not. A cell without logs keeps the
prior, so the table only moves weights where the replays say something.

    uv run python -m buyer.argtable build     # tally runs/arena-*.json (needs the data package), write argtable.json
    uv run python -m buyer.argtable show      # the table with priors, counts and weights

The committed `argtable.json` holds the counts stated in the README until `build` is run on the saved sessions;
`manual` entries (sessions not in runs/, e.g. a team-mate's, read with GET /v1/sessions) survive a rebuild.
"""

import argparse
import json
import re
import time
from pathlib import Path

TABLE = Path(__file__).with_name("argtable.json")
K = 2.0  # pseudo-arguments the prior is worth
FAIR_BONUS = 0.1  # "we did not know that - a fair point": it counted more (twin: 0.75 % of the opening, not 0.5 %)
OUTCOMES = ("counted", "fair", "no_effect", "duplicate", "rejected")

# (kind, persona) -> the weight the code gave before any table: the rules this table replaces. For kinds that do not
# depend on the supplier (quotes, order history) the second key is the situation the argument is made in.
PRIOR = {
    ("notice", "any"): 1.0,
    ("idx", "any"): 0.9,
    ("idx", "stale_costing"): 1.0,  # "we did not know that - a fair point"
    ("idx", "hedge_covered"): 0.05,  # P08: "that is not our reality: our material is covered at fixed prices"
    ("idx", "hedged_other"): 0.54,  # a series its hedge does not name (P08: labour, bar drawing)
    ("idx", "cash_short"): 0.27,  # "we see your figures, but we cannot give up margin right now"
    ("sc", "any"): 0.7,
    ("sc", "quality_case"): 0.95,
    ("sc_otd", "any"): 0.12,
    ("sc_otd", "stale_costing"): 0.8,
    ("fc", "any"): 0.8,
    ("fc", "stale_costing"): 0.85,
    ("fc", "capacity_tight"): 0.2,
    ("fc", "not_strategic"): 0.2,
    ("cost", "any"): 0.75,
    ("quote", "alternative"): 0.85,  # the case's own alternative
    ("quote", "rival_far"): 0.97,  # a recent rival quote more than 10 % below its price: the two-step (P05)
    ("quote", "rival"): 0.9,
    ("quote", "rival_old_far"): 0.7,
    ("quote", "rival_old"): 0.5,
    ("offer", "rfq"): 0.95,
    ("offer", "any"): 0.85,
    ("po", "below_standing"): 0.6,  # raw material: what we pay today is below its standing price
    ("po", "any"): 0.1,
    ("news", "any"): 0.1,
}
# what one argument of a kind that counted is worth, where the persona's prior is lower (a counted delivery record is
# worth what it is to a supplier on an old cost sheet, not the 0.12 of one that ignores it)
VALUE = {"notice": 1.0, "idx": 0.9, "sc": 0.8, "sc_otd": 0.8, "fc": 0.8, "cost": 0.75, "quote": 0.9, "offer": 0.95,
         "po": 0.6, "news": 0.5}  # fmt: skip


def persona(kind, d, case, series=None, situation=None):
    """The table row for an argument of `kind` to the supplier of dossier `d`: the trait that decides whether
    such an argument counts with it (first match), else "any". `situation` for quotes and order history."""
    if kind == "idx":
        if d.has("hedged_material"):
            return "hedge_covered" if d.hedge_covers(series) else "hedged_other"
        if d.has("cash_short", strong=True):
            return "cash_short"
        return "stale_costing" if d.has("stale_costing") else "any"
    if kind == "sc":
        return "quality_case" if case.get("type") == "quality" else "any"
    if kind == "sc_otd":
        return "stale_costing" if d.has("stale_costing") else "any"
    if kind == "fc":
        if d.has("capacity_tight"):
            return "capacity_tight"
        if d.not_strategic:  # told us we no longer matter (P04); not a mere "reviewing small accounts" (P18)
            return "not_strategic"
        return "stale_costing" if d.has("stale_costing") else "any"
    if kind == "offer":
        return "rfq" if case.get("type") == "rfq" else "any"
    if kind in ("quote", "po"):
        return situation or "any"
    return "any"


_cache = {"mtime": None, "counts": {}}


def counts():
    """{(kind, persona): {outcome: n}} from `argtable.json` (runs and manual entries), re-read when it changes."""
    try:
        mtime = TABLE.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != mtime:
        try:
            data = json.loads(TABLE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        out = {}
        for key, row in (data.get("counts") or {}).items():
            out[tuple(key.split("|", 1))] = {o: int(row.get(o, 0)) for o in OUTCOMES}
        for e in data.get("manual") or []:
            row = out.setdefault((e["kind"], e["persona"]), dict.fromkeys(OUTCOMES, 0))
            row[e["outcome"]] = row.get(e["outcome"], 0) + int(e.get("n", 1))
        _cache.update(mtime=mtime, counts=out)
    return _cache["counts"]


def prior(kind, who):
    return PRIOR.get((kind, who), PRIOR.get((kind, "any"), 0.5))


def weight(kind, who, table=None):
    """How much an argument of `kind` is expected to help with a supplier in row `who`, 0-1."""
    p = prior(kind, who)
    row = (counts() if table is None else table).get((kind, who))
    if not row:
        return p
    hit = max(p, VALUE.get(kind, p))
    n = row["counted"] + row["fair"] + row["no_effect"]  # duplicates and rejections say nothing about the persona
    if not n:
        return p
    total = row["counted"] * hit + row["fair"] * min(1.0, hit + FAIR_BONUS)
    return round(min(1.0, (K * p + total) / (K + n)), 3)


# -- the replay logs ----------------------------------------------------------------------------------------------
PREFIX_KIND = {"idx": "idx", "fc": "fc", "cost": "cost", "quote": "quote", "offer": "offer", "po": "po",
               "news": "news", "doc": "notice", "ctr": "notice", "qn": "qn"}  # fmt: skip
SENTENCE = re.compile(r"(?<=[.!?])\s+")
EURO = re.compile(r"€\s?(\d+(?:[.,]\d+)?)")
FAIR = re.compile(r"did not know that|fair point", re.I)
SHRUG_IDX = re.compile(r"not our reality|covered at fixed prices|confirm our case|cannot give up margin", re.I)


class Logged:
    """One argument of a saved buyer e-mail: its kind, IDs, sentence and figure."""

    def __init__(self, kind, ids, sentence):
        self.kind, self.ids, self.sentence = kind, ids, sentence
        m = EURO.search(sentence)
        self.figure = float(m.group(1).replace(",", "")) if m else None
        self.series = ids[0].split(":", 1)[1].split("@")[0] if kind == "idx" else None


def parse_claims(body, refs):
    """The arguments of one buyer e-mail in the order the arena numbers them: the IDs of one sentence form one
    argument (an index change cites two months, a delivery record two quarters)."""
    body = body or ""
    sentences = [s for s in SENTENCE.split(body) if s.strip()]
    out, done = [], set()
    for ref in refs or []:
        if ref in done:
            continue
        sentence = next((s for s in sentences if ref in s), "")
        ids = [r for r in refs if r not in done and r in sentence] if sentence else [ref]
        if ref not in ids:
            ids = [ref]
        done.update(ids)
        prefix = ref.split(":", 1)[0]
        kind = PREFIX_KIND.get(prefix, prefix)
        if prefix == "sc":
            kind = "sc_otd" if "on-time" in sentence.lower() else "sc"
        out.append(Logged(kind, ids, sentence))
    return out


def outcome(result, body, claim, who):
    """What one checked argument did: counted, fair, no_effect, duplicate or rejected."""
    if result in ("contradicted", "irrelevant"):
        return "rejected"
    if result == "duplicate":
        return "duplicate"
    if result != "verified":
        return None
    from .claims import no_change_kinds

    if claim.kind in no_change_kinds(body):
        return "no_effect"
    if claim.kind == "idx" and SHRUG_IDX.search(body or ""):
        # "not our reality" is about the series its hedge covers; the others in the same reply may still count
        if who == "hedge_covered" or not re.search(r"not our reality|covered at fixed prices", body, re.I):
            return "no_effect"
    if FAIR.search(body or "") and claim.kind in ("idx", "fc", "sc_otd", "cost", "notice"):
        return "fair"
    return "counted"


def situation(case, claim, stated):
    """The row of a quote or order-history argument, from its sentence and the supplier's price at the time."""
    s = claim.sentence
    if claim.kind == "quote":
        if s.startswith("We hold a quote"):
            return "alternative"
        far = bool(claim.figure and stated and claim.figure < stated * 0.9)
        old = bool(re.search(r"for the same part in \d{4}", s))
        return ("rival_old" if old else "rival") + ("_far" if far else "")
    if claim.kind == "po":
        below = case.get("type") == "raw_material" and claim.figure and stated and claim.figure < stated * 0.99
        return "below_standing" if below else "any"
    return None


def tally(records, cases, persona_of):
    """{(kind, persona): {outcome: n}} over saved sessions. `records`: [(name, record)] as `twin_real.load_runs`
    gives them; `persona_of(name, record, sid, claim)` -> the row of an argument of that kind to that supplier."""
    out = {}
    for name, rec in records:
        case = cases.get(rec.get("state", {}).get("case_id"))
        if not case:
            continue
        for sid, th in (rec.get("threads") or {}).items():
            msgs = th.get("messages") or []
            stated = None
            for i, m in enumerate(msgs):
                if m.get("from") != "buyer":
                    prices = [(s.get("terms") or {}).get("unit_price") for s in m.get("term_sheets") or []]
                    stated = min([p for p in prices if p], default=stated)
                    continue
                if (m.get("action") or {}).get("type") or not m.get("refs"):
                    continue
                reply = next((x for x in msgs[i + 1 :] if x.get("from") != "buyer"), None)
                if not reply:
                    continue
                claims = parse_claims(m.get("body"), m.get("refs"))
                for r in reply.get("claim_results") or []:
                    j = r.get("index")
                    if not isinstance(j, int) or not 0 <= j < len(claims):
                        continue
                    c = claims[j]
                    if c.kind in ("quote", "po"):
                        who = situation(case, c, stated) or "any"
                    else:
                        who = persona_of(name, rec, sid, c)
                    res = outcome(r.get("result"), reply.get("body") or "", c, who)
                    if res:
                        row = out.setdefault((c.kind, who), dict.fromkeys(OUTCOMES, 0))
                        row[res] += 1
    return out


def dossier_personas(world, cases):
    """`persona_of` for `tally` from the data package: the supplier's dossier with the persona read the agent logged
    for that session and everything the supplier wrote in it."""
    from .dossier import Dossier

    made = {}

    def persona_of(name, rec, sid, claim):
        if (name, sid) not in made:
            case = cases[rec["state"]["case_id"]]
            d = Dossier(world, case, sid)
            for e in (rec.get("result") or {}).get("agent_log") or []:
                if isinstance(e, list | tuple) and len(e) == 3 and e[0] == sid and e[1] == "persona":
                    d.apply_read(e[2])
            for m in (rec.get("threads") or {}).get(sid, {}).get("messages") or []:
                if m.get("from") != "buyer":
                    d.add_text(m.get("body") or "")
            made[(name, sid)] = d
        d = made[(name, sid)]
        return persona(claim.kind, d, d.case, series=claim.series)

    return persona_of


def write(table, runs, path=TABLE):
    old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data = {
        "note": f"tallied from {runs} saved sessions by `python -m buyer.argtable build`; manual entries kept",
        "built": time.strftime("%Y-%m-%d %H:%M"),
        "counts": {f"{k}|{p}": row for (k, p), row in sorted(table.items())},
        "manual": old.get("manual") or [],
    }
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def show(table=None):
    table = counts() if table is None else table
    rows = sorted(set(PRIOR) | set(table))
    print(f"{'kind':8} {'persona':15} {'prior':>6} {'n':>4} {'counted':>8} {'fair':>5} {'none':>5} {'weight':>7}")
    for kind, who in rows:
        r = table.get((kind, who)) or dict.fromkeys(OUTCOMES, 0)
        n = r["counted"] + r["fair"] + r["no_effect"]
        print(f"{kind:8} {who:15} {prior(kind, who):6.2f} {n:4d} {r['counted']:8d} {r['fair']:5d} "
              f"{r['no_effect']:5d} {weight(kind, who, table):7.2f}")  # fmt: skip


def main(argv=None):
    ap = argparse.ArgumentParser(prog="argtable")
    ap.add_argument("cmd", choices=["build", "show"])
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--data", default="data/package")
    ap.add_argument("--cases-dir", default="cases/real")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        return show()
    from arena_kit.sim import load_cases

    from .dossier import World
    from .twin_real import load_runs

    cases = load_cases(a.cases_dir)
    runs = load_runs(a.runs)
    table = tally(runs, cases, dossier_personas(World(a.data), cases))
    write(table, len(runs))
    _cache["mtime"] = None
    show()


if __name__ == "__main__":
    main()
