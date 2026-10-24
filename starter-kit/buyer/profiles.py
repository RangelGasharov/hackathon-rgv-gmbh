"""Supplier profiles: everything we know about each of the 46 detailed suppliers, built once and kept in files.

The arena's cases only ever negotiate with the suppliers the data package describes in full (46 of 447). So
instead of reading a supplier's documents while the 60-second clock runs, we read them once, deeply, and save
what we found in `profiles/`:

    profiles/README.md         how the files are built and used
    profiles/INDEX.md          one line per supplier: who it is, what moves it, where to look
    profiles/sup_70110.json    the profile the agent loads (facts, persona, playbook, arena evidence)
    profiles/sup_70110.md      the same for people (and for agents working on this code)
    profiles/persona/*.json    the deep read of each supplier's documents (the only hand/model-written part)
    profiles/manifest.json     what each profile was built from, to tell when the data changed

Facts are computed from the data package here, deterministically. The persona and playbook come from a deep
read of the supplier's documents (`profiles/persona/`), checked against the data: every trait it claims must
cite a document the supplier really has. When the data changes (the Sunday release), `build` recomputes the
facts and flags every supplier whose documents changed, so only those need a new read:

    uv run python -m buyer.profiles build --data data/package [--arena runs/]
    uv run python -m buyer.profiles check --data data/package     # exit 1 when a persona is stale
    uv run python -m buyer.profiles facts sup:70110 --data data/package   # what a reader gets
"""

import argparse
import glob
import hashlib
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .dossier import TRAITS, World

SCHEMA = 1
PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"
LEVELS = ("strong", "weak", "none")
WANTS = ("faster_payment", "binding_volume", "long_contract", "index_clause", "longer_lead_time", "price_stability")
OPENINGS = ("deep", "moderate", "close")


def pid(sid):
    """File stem of a supplier: sup:70110 -> sup_70110."""
    return sid.replace(":", "_")


# -- facts -----------------------------------------------------------------------------------------------------
def supplier_ids(world):
    return [r[0] for r in world.all("SELECT id FROM suppliers WHERE detail='full' ORDER BY id")]


def _r(x, n=2):
    return round(x, n) if isinstance(x, float) else x


def facts(world, sid):
    """What the data package says about one supplier, as plain data (no judgement)."""
    s = world.one("SELECT * FROM suppliers WHERE id=?", sid)
    i = world.one("SELECT * FROM supplier_intel WHERE supplier_id=?", sid)
    out = {
        "id": sid,
        "name": s["name"],
        "country": s["country"],
        "category": s["category"],
        "abc_class": s["abc_class"],
        "standard_terms": {
            "payment_days": s["payment_days"],
            "cash_discount": {"pct": s["skonto_pct"], "days": s["skonto_days"]} if s["skonto_pct"] else None,
            "incoterm": s["incoterm"],
            "lead_time_days": s["standard_lead_time_days"],
        },
    }
    if i:
        out["company"] = {
            "owner_type": i["owner_type"],
            "group_parent": i["group_parent"],
            "legal_form": i["legal_form"],
            "founded": i["founded"],
            "staff": i["staff"],
            "turnover_eur": i["turnover_eur"],
            "turnover_band": i["turnover_band"],
            "equity_ratio_pct": i["equity_ratio_pct"],
            "credit_index": i["credit_index"],
            "credit_index_12m_ago": i["credit_index_12m_ago"],
            "vda63_grade": i["vda63_grade"],
            "certifications": i["certifications"],
        }
    spend = {r["fiscal_year"]: r["spend_eur"] for r in world.all("SELECT * FROM spend_cube WHERE supplier_id=?", sid)}
    last = max(spend) if spend else None
    turnover = (i["turnover_eur"] if i else None) or None
    band = re.match(r"([\d.]+)\s*-\s*([\d.]+)\s*M", (i["turnover_band"] or "") if i else "")
    rng = None  # small firms file only a turnover band: our share is then a range, not a figure
    if last and not turnover and band:
        lo, hi = float(band.group(1)) * 1e6, float(band.group(2)) * 1e6
        rng = [round(100 * spend[last] / hi, 1), round(100 * spend[last] / lo, 1) if lo else None]
    out["our_weight"] = {
        "spend_eur_by_year": {k: round(v) for k, v in sorted(spend.items())},
        # what we are worth to it: a few % of its turnover is a customer it notices, a fraction of 1 % is not
        "share_of_its_turnover_pct": round(100 * spend[last] / turnover, 2) if last and turnover else None,
        "share_of_its_turnover_range_pct": rng,
        "spend_trend_pct": round(100 * (spend[last] / spend[min(spend)] - 1), 1) if len(spend) > 1 else None,
    }
    out["parts"] = _parts(world, sid)
    out["contract"] = _contract(world, sid)
    out["negotiations"] = _negotiations(world, sid)
    out["payments"] = _payments(world, sid)
    out["quality"] = _quality(world, sid)
    out["news"] = [
        {"id": r.get("id"), "date": r.get("date"), "topic": r.get("topic"), "headline": r.get("headline")}
        for r in world.rows("news", sid)
    ]
    out["documents"] = [
        {
            "id": r.get("id"),
            "date": r.get("date"),
            "kind": r.get("kind"),
            "subject": r.get("subject"),
            "author": r.get("author"),
            "text": _doc_body(world.doc_text(r.get("id"))),
        }
        for r in world.rows("documents", sid)
    ]
    out["quote_remarks"] = [
        {"id": r["id"], "date": r["quote_date"], "part": r["part_id"], "remarks": r["remarks"]}
        for r in world.all("SELECT * FROM quotes WHERE supplier_id=? AND remarks!='' ORDER BY quote_date", sid)
    ]
    return out


def _doc_body(text):
    """A document's text without its markdown title and dateline (they repeat the index fields)."""
    lines = [x for x in (text or "").splitlines() if x.strip() and not x.startswith("# ")]
    if lines and re.match(r"^\w[\w ]* - \d{4}-\d{2}-\d{2} - ", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _parts(world, sid):
    out = []
    for r in world.all(
        "SELECT s.*, p.description, p.category AS pcat, p.price_unit FROM sourcing s JOIN parts p ON p.id=s.part_id "
        "WHERE s.supplier_id=? ORDER BY s.part_id",
        sid,
    ):
        part = r["part_id"]
        prices = world.all(
            "SELECT valid_from, price FROM price_conditions WHERE part_id=? AND supplier_id=? ORDER BY valid_from",
            part,
            sid,
        )
        sc = world.one(
            "SELECT unit_cost, supplier_id, confidence_pct, method FROM should_cost WHERE part_id=? "
            "ORDER BY (supplier_id=?) DESC LIMIT 1",
            part,
            sid,
        )
        others = world.all(
            "SELECT supplier_id, status, quota_pct FROM sourcing WHERE part_id=? AND supplier_id!=?", part, sid
        )
        price = prices[-1]["price"] if prices else None
        drivers = []
        for d in world.all("SELECT series_id, weight FROM cost_drivers WHERE part_id=? ORDER BY weight DESC", part):
            ch = world.index_change(d["series_id"])  # (change %, id from, id to, month from, month to) over 12 months
            if ch:
                drivers.append({"series": d["series_id"], "weight": d["weight"], "change_12m_pct": ch[0],
                                "ids": [ch[1], ch[2]], "months": [ch[3], ch[4]]})  # fmt: skip
        out.append(
            {
                "part": part,
                "description": r["description"],
                "status": r["status"],
                "quota_pct": r["quota_pct"],
                "tooling_owner": r["tooling_owner"],
                "price_unit": r["price_unit"],
                "current_price": price,
                "price_since": prices[-1]["valid_from"] if prices else None,
                "price_change_pct_since_first": round(100 * (price / prices[0]["price"] - 1), 1)
                if prices and prices[0]["price"]
                else None,
                "should_cost": {
                    "unit_cost": sc["unit_cost"],
                    "of_this_supplier": sc["supplier_id"] == sid,
                    "confidence_pct": sc["confidence_pct"],
                    "method": sc["method"],
                    "price_over_cost_pct": round(100 * (price / sc["unit_cost"] - 1), 1) if price else None,
                }
                if sc
                else None,
                # its cost drivers as the category team weighs them, with their real 12-month moves (citable ids):
                # a fallen driver is an argument, a risen one backfires ("your figures confirm our case")
                "cost_drivers": drivers,
                "drivers_weighted_change_pct": round(sum(x["weight"] * x["change_12m_pct"] for x in drivers), 1)
                if drivers
                else None,
                # a second approved source is our strongest walk-away: it decides how far we can push
                "other_sources": [
                    {"supplier": o["supplier_id"], "status": o["status"], "quota_pct": o["quota_pct"]} for o in others
                ],
            }
        )
    return out


def _contract(world, sid):
    c = world.one("SELECT * FROM contracts WHERE supplier_id=?", sid)
    if not c:
        return None
    clauses = world.all("SELECT * FROM contract_clauses WHERE contract_id=? ORDER BY clause", c["id"])
    return {
        "id": c["id"],
        "type": c["type"],
        "valid": [c["valid_from"], c["valid_to"]],
        "payment_days": c["payment_days"],
        "incoterm": c["incoterm"],
        "notice_days": c["notice_days"],
        "price_basis": c["price_basis"],
        "clauses": [
            {
                "id": k["id"],
                "type": k["type"],
                "text": k["text"],
                **({"series": k["series"], "material_share": k["material_share"]} if k["series"] else {}),
            }
            for k in clauses
            if k["type"] != "scope"
        ],
    }


def _negotiations(world, sid):
    rows = world.all("SELECT * FROM negotiation_history WHERE supplier_id=? ORDER BY date", sid)
    events = [
        {
            "date": r["date"],
            "part": r["part_id"],
            "type": r["type"],
            "asked_pct": r["asked_pct"],
            "settled_pct": r["settled_pct"],
            "note": r["note"] or None,
        }
        for r in rows
    ]
    pairs = [(e["asked_pct"], e["settled_pct"]) for e in events if e["asked_pct"] and e["settled_pct"] is not None]
    return {
        "events": events,
        "asked_avg_pct": _r(statistics.mean(a for a, _ in pairs), 1) if pairs else None,
        "settled_avg_pct": _r(statistics.mean(b for _, b in pairs), 1) if pairs else None,
        # how much of what it asks it usually keeps: a low share means it opens high and gives way
        "kept_share_of_ask": _r(statistics.mean(b / a for a, b in pairs if a), 2) if pairs else None,
    }


def _payments(world, sid):
    r = world.one(
        "SELECT count(*) n, avg(julianday(paid_date)-julianday(due_date)) late, sum(discount_taken) disc, "
        "sum(CASE WHEN julianday(paid_date)-julianday(due_date) > 5 THEN 1 ELSE 0 END) over5 "
        "FROM invoices WHERE supplier_id=? AND paid_date IS NOT NULL",
        sid,
    )
    return {
        "invoices": r["n"],
        "avg_days_late": _r(r["late"], 1) if r["late"] is not None else None,
        "share_paid_over_5_days_late_pct": round(100 * r["over5"] / r["n"], 1) if r["n"] else None,
        "cash_discounts_taken": r["disc"],
    }


def _quality(world, sid):
    sc = world.all("SELECT * FROM scorecards WHERE supplier_id=? ORDER BY quarter", sid)
    qn = world.one(
        "SELECT count(*) n, sum(cost_recharged_eur) eur, sum(CASE WHEN d8_date IS NULL THEN 1 ELSE 0 END) open8d "
        "FROM quality_notifications WHERE supplier_id=?",
        sid,
    )
    return {
        "scorecards": [
            {"id": r["id"], "quarter": r["quarter"], "otd_pct": r["otd_pct"], "ppm": r["ppm"], "colour": r["colour"]}
            for r in sc[-4:]
        ],
        "complaints_3y": qn["n"],
        "cost_recharged_eur": round(qn["eur"] or 0),
        "open_8d": qn["open8d"],
    }


def source_hash(f):
    """Hash of what a persona read depends on: documents, news, quote remarks, negotiation notes, company data."""
    keep = {
        "documents": [(d["id"], d["text"]) for d in f["documents"]],
        "news": f["news"],
        "quote_remarks": f["quote_remarks"],
        "negotiations": f["negotiations"]["events"],
        "company": f.get("company"),
    }
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()[:16]


# -- arena evidence ----------------------------------------------------------------------------------------------
FAIR = re.compile(r"did not know that|fair point", re.I)
NO_CHANGE = re.compile(r"does not change", re.I)


def arena_evidence(patterns):
    """What our practice sessions showed about each supplier: {sid: summary}. Reads session records (the runner's
    `runs/arena-*.json` or GET /v1/sessions/{sid} dumps): any JSON with a `state.result.trace`."""
    per = {}
    for path in sorted({p for pat in patterns for p in glob.glob(pat)}):
        try:
            rec = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        state = rec.get("state") or rec
        res = state.get("result") or {}
        trace = res.get("trace") or []
        if not trace:
            continue
        case = state.get("case_id")
        threads = {}
        for t in trace:
            actor = t.get("actor")
            mv = t.get("move") or {}
            if actor and actor != "buyer":
                threads.setdefault(actor, []).append((t.get("round"), mv, t.get("reasons") or []))
        for sid, moves in threads.items():
            e = per.setdefault(sid, {"cases": set(), "sessions": 0, "deals": 0, "best_score": None, "first_counter": [],
                                     "final_offer_round": [], "walked": 0, "agreed_round1": 0, "fair_point": 0,
                                     "no_change": 0, "duplicate": 0})  # fmt: skip
            e["cases"].add(case)
            e["sessions"] += 1
            if res.get("deal") and res.get("supplier_id") == sid:
                e["deals"] += 1
                if res.get("score") is not None:
                    e["best_score"] = max(e["best_score"] or 0, res["score"])
            p0 = (moves[0][1].get("terms") or {}).get("unit_price")
            if p0 and len(moves) > 1 and (p1 := (moves[1][1].get("terms") or {}).get("unit_price")):
                e["first_counter"].append(p1 / p0)
            for rnd, mv, reasons in moves:
                st = mv.get("status")
                if st == "final_offer":
                    e["final_offer_round"].append(rnd)
                if st == "walked_away":
                    e["walked"] += 1
                if st == "agreed" and rnd == 1:
                    e["agreed_round1"] += 1
                text = " ".join(reasons)
                e["fair_point"] += bool(FAIR.search(text))
                e["no_change"] += bool(NO_CHANGE.search(text))
                e["duplicate"] += sum(c.get("result") == "duplicate" for c in mv.get("claim_results") or [])
    out = {}
    for sid, e in per.items():
        fc = e.pop("first_counter")
        fr = e.pop("final_offer_round")
        out[sid] = {
            **e,
            "cases": sorted(e["cases"]),
            "best_score": _r(e["best_score"], 3),
            "first_counter_vs_opening": _r(statistics.median(fc), 3) if fc else None,
            "final_offer_round_median": statistics.median(fr) if fr else None,
        }
    return out


# -- persona (the deep read) -------------------------------------------------------------------------------------
def check_persona(p, f):
    """Problems with a persona read against the supplier's facts: [] when it can be used."""
    errs = []
    texts = {d["id"]: d["text"] for d in f["documents"]}
    texts |= {n["id"]: n.get("headline") or "" for n in f["news"]} | {q["id"]: q["remarks"] for q in f["quote_remarks"]}
    doc_ids = set(texts)
    for k, v in (p.get("traits") or {}).items():
        if k not in TRAITS:
            errs.append(f"unknown trait {k}")
        elif v not in LEVELS:
            errs.append(f"bad level {k}={v}")
    errs += [f"unknown want {w}" for w in p.get("wants") or [] if w not in WANTS]
    for ev in p.get("evidence") or []:
        doc = ev.get("doc")
        if doc and doc not in doc_ids and not str(doc).startswith(("data:", "arena:")):
            errs.append(f"evidence cites {doc}, not a document of {f['id']}")
        elif doc in texts and ev.get("quote") and _norm(ev["quote"]) not in _norm(texts[doc]):
            # a quote must be the document's own words: no paraphrase passes as evidence
            errs.append(f'quote not found verbatim in {doc}: "{ev["quote"][:60]}"')
    strong = {k for k, v in (p.get("traits") or {}).items() if v == "strong"}
    cited = {ev.get("trait") for ev in p.get("evidence") or []}
    errs += [f"strong trait {k} without evidence" for k in sorted(strong - cited)]
    if (p.get("playbook") or {}).get("opening") not in (None, *OPENINGS):
        errs.append("playbook.opening must be deep, moderate or close")
    if h := p.get("hedge_until"):
        if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", str(h)):
            errs.append("hedge_until must be YYYY-MM")
    return errs


def _norm(s):
    """Text compared for quotes: markdown, quotes and whitespace removed, case ignored."""
    s = re.sub(r"[*_`\"'“”‘’]", "", str(s)).replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().lower().rstrip(".")


def read_of(profile):
    """The persona read in the shape `Dossier.apply_read` takes (as `persona.parse` returns it)."""
    p = (profile or {}).get("persona") or {}
    if not p or profile.get("persona_stale"):
        return None
    return {
        "traits": {k: v for k, v in (p.get("traits") or {}).items() if k in TRAITS and v in LEVELS},
        "unsure": [k for k in p.get("unsure") or [] if k in TRAITS],
        "wants": [w for w in p.get("wants") or [] if w in WANTS],
        "hedge_until": p.get("hedge_until"),
        "notes": str(p.get("notes") or p.get("summary") or "")[:400],
    }


# -- loading (what the agent uses) -------------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load(sid, root=None):
    """The built profile of a supplier, or None (no file: the agent falls back to its live read)."""
    return _load(str(Path(root or PROFILE_DIR) / f"{pid(sid)}.json"))


def brief(profile, limit=1800):
    """Background on the supplier for the strategist: who it is, what it wants and fears, which arguments work
    and which backfire, what the arena showed. Not which package to pick: the menu's costs decide that (on the
    twin, profile tactics in the prompt made Opus pick sheets that cost us more: P03, P04, P14, P17)."""
    if not profile:
        return ""
    p, pb, a = profile.get("persona") or {}, (profile.get("persona") or {}).get("playbook") or {}, profile.get("arena")
    lines = [
        f"PROFILE {profile['id']} {profile['name']} (background only; pick sheets by the menu's cost to us): "
        f"{p.get('summary', '')}"
    ]
    w = profile.get("our_weight") or {}
    if _share(w) != "?":
        lines.append(f"We are {_share(w)} % of its turnover.")
    if p.get("wants"):
        lines.append("Wants: " + ", ".join(p["wants"]) + ".")
    if p.get("fears"):
        lines.append("Fears: " + "; ".join(p["fears"]) + ".")
    for key, label in (("arguments", "Arguments that work"), ("avoid", "Avoid"),
                       ("mandatory_terms", "Terms it insists on")):  # fmt: skip
        if pb.get(key):
            lines.append(f"{label}: " + "; ".join(pb[key]) + ".")
    if pb.get("walk_risk"):
        lines.append(f"Walk risk: {pb['walk_risk']}.")
    if a:
        lines.append(
            f"Arena ({a['sessions']} sessions): first counter at {a.get('first_counter_vs_opening')} of its opening, "
            f"final offer around round {a.get('final_offer_round_median')}, walked {a['walked']}x, "
            f"agreed our round-1 offer {a['agreed_round1']}x, 'fair point' {a['fair_point']}x."
        )
    return "\n".join(lines)[:limit]


# -- building ----------------------------------------------------------------------------------------------------
def build(world, root=None, arena_patterns=(), now=None):
    """Write every profile, the index, the README and the manifest. Returns {sid: [problems]}."""
    root = Path(root or PROFILE_DIR)
    (root / "persona").mkdir(parents=True, exist_ok=True)
    arena = arena_evidence(arena_patterns) if arena_patterns else {}
    old = _json(root / "manifest.json") or {}
    if not arena:  # keep the evidence of the last build when no session records are at hand
        arena = {sid: (_json(root / f"{pid(sid)}.json") or {}).get("arena") for sid in supplier_ids(world)}
        arena = {k: v for k, v in arena.items() if v}
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    manifest, problems, rows = {"schema": SCHEMA, "built": stamp, "data": str(world.root), "suppliers": {}}, {}, []
    for sid in supplier_ids(world):
        f = facts(world, sid)
        h = source_hash(f)
        persona = _json(root / "persona" / f"{pid(sid)}.json")
        errs = check_persona(persona, f) if persona else ["no persona read yet"]
        stale = bool(persona) and persona.get("source_hash") not in (None, h)
        if stale:
            errs.append("documents changed since the persona read: read it again")
        profile = {
            "schema": SCHEMA,
            "id": sid,
            "name": f["name"],
            "built": stamp,
            "source_hash": h,
            "persona_stale": stale,
            "problems": errs,
            **{k: v for k, v in f.items() if k not in ("id", "name")},
            "persona": persona,
            "arena": arena.get(sid),
        }
        (root / f"{pid(sid)}.json").write_text(json.dumps(profile, indent=1, ensure_ascii=False), encoding="utf-8")
        (root / f"{pid(sid)}.md").write_text(markdown(profile), encoding="utf-8")
        manifest["suppliers"][sid] = {"source_hash": h, "persona": bool(persona), "stale": stale,
                                      "changed": (old.get("suppliers") or {}).get(sid, {}).get("source_hash") not in (None, h)}  # fmt: skip
        problems[sid] = errs
        rows.append(profile)
    (root / "INDEX.md").write_text(index(rows, stamp), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    _load.cache_clear()
    return problems


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _share(w):
    """Our share of its turnover as text: a figure, or a range when it files only a turnover band."""
    if w.get("share_of_its_turnover_pct") is not None:
        return str(w["share_of_its_turnover_pct"])
    r = w.get("share_of_its_turnover_range_pct")
    if not r:
        return "?"
    return f"{r[0]}-{r[1]}" if r[1] is not None else f"at least {r[0]}"


def _terms(t):
    cd = t.get("cash_discount")
    return (f"{t['payment_days']} days net" + (f" ({cd['pct']} % discount within {cd['days']} days)" if cd else "")
            + f", {t['incoterm']}, lead time {t['lead_time_days']} days")  # fmt: skip


def _pct(x):
    return "-" if x is None else f"{x:+.1f} %"


def markdown(pr):
    p, pb = pr.get("persona") or {}, (pr.get("persona") or {}).get("playbook") or {}
    c, w, n, q, pay = pr.get("company") or {}, pr["our_weight"], pr["negotiations"], pr["quality"], pr["payments"]
    out = [f"# {pr['name']} ({pr['id']})", ""]
    if pr["problems"]:
        out += ["> **Check:** " + "; ".join(pr["problems"]), ""]
    out += [
        f"{pr['category']} · {pr['country']} · {c.get('owner_type', '?')}"
        + (f" (group: {c['group_parent']})" if c.get("group_parent") else "")
        + f" · {c.get('staff', '?')} staff · turnover {c.get('turnover_band') or c.get('turnover_eur')}",
        "",
        "## In one paragraph",
        "",
        p.get("summary") or "_No persona read yet._",
        "",
        "## Playbook",
        "",
    ]
    if pb:
        out += [
            f"- **Opening:** {pb.get('opening', '-')} · **walk risk:** {pb.get('walk_risk', '-')}",
            f"- **Levers that move it:** {'; '.join(pb.get('levers') or []) or '-'}",
            f"- **Arguments that work:** {'; '.join(pb.get('arguments') or []) or '-'}",
            f"- **Avoid:** {'; '.join(pb.get('avoid') or []) or '-'}",
            f"- **Terms it insists on:** {'; '.join(pb.get('mandatory_terms') or []) or '-'}",
        ]
        out += [f"- {t}" for t in pb.get("tactics") or []]
    else:
        out.append("_None yet._")
    traits = {k: v for k, v in (p.get("traits") or {}).items() if v != "none"}
    out += ["", "## Persona", "", "| Trait | Level | Evidence |", "|---|---|---|"]
    ev = {}
    for e in p.get("evidence") or []:
        ev.setdefault(e.get("trait"), []).append(f'{e.get("doc")}: "{e.get("quote", "")}"')
    out += [f"| {k} | {v} | {'<br>'.join(ev.get(k, [])) or '-'} |" for k, v in sorted(traits.items())] or [
        "| - | - | - |"
    ]
    out += [
        "",
        f"Wants: {', '.join(p.get('wants') or []) or '-'} · Fears: {'; '.join(p.get('fears') or []) or '-'}"
        + (f" · Hedged until {p['hedge_until']}" if p.get("hedge_until") else ""),
        "",
        "## Facts",
        "",
        "- Our spend: "
        + ", ".join(f"{k} €{v:,}" for k, v in w["spend_eur_by_year"].items())
        + f" · we are {_share(w)} % of its turnover "
        f"· trend {_pct(w['spend_trend_pct'])}",
        f"- Company: equity {c.get('equity_ratio_pct', '?')} %, credit index {c.get('credit_index', '?')} "
        f"(12 months ago {c.get('credit_index_12m_ago', '?')}; higher is worse), VDA 6.3 {c.get('vda63_grade', '?')}",
        "- Standard terms: " + _terms(pr["standard_terms"]),
        f"- Past negotiations: asked {_pct(n['asked_avg_pct'])}, settled {_pct(n['settled_avg_pct'])}, "
        f"kept {n['kept_share_of_ask']} of its ask ({len(n['events'])} events)",
        f"- Payments: {pay['avg_days_late']} days late on average, {pay['share_paid_over_5_days_late_pct']} % over "
        f"5 days late, cash discounts taken {pay['cash_discounts_taken']}",
        f"- Quality: {q['complaints_3y']} complaints, €{q['cost_recharged_eur']:,} recharged, {q['open_8d']} open 8D; "
        + ", ".join(f"{s['quarter']} {s['otd_pct']} % OTD / {s['ppm']} ppm ({s['colour']})" for s in q["scorecards"]),
    ]
    if pr.get("contract"):
        k = pr["contract"]
        out.append(f"- Contract {k['id']}: {k['valid'][0]} to {k['valid'][1]}, {k['payment_days']} days, "
                   f"{k['incoterm']}, notice {k['notice_days']} days, {k['price_basis']}")  # fmt: skip
    out += [
        "",
        "| Part | Price | Should-cost | Since | Cost drivers, 12 months | Other sources |",
        "|---|---|---|---|---|---|",
    ]
    for x in pr["parts"]:
        sc = x["should_cost"] or {}
        drv = ", ".join(
            f"{d['series']} {d['change_12m_pct']:+.1f} % (w {d['weight']})" for d in x.get("cost_drivers") or []
        )
        out.append(
            f"| {x['part']} {x['description']} | {x['current_price']} | {sc.get('unit_cost', '-')}"
            f"{' (its plant)' if sc.get('of_this_supplier') else ''} | {x['price_since']} | {drv or '-'} | "
            f"{', '.join(o['supplier'] for o in x['other_sources']) or 'none'} |"
        )
    if a := pr.get("arena"):
        out += [
            "",
            "## Arena evidence",
            "",
            f"{a['sessions']} sessions in {', '.join(a['cases'])}: best score {a['best_score']}, first counter at "
            f"{a['first_counter_vs_opening']} of its opening, final offer around round {a['final_offer_round_median']}, "
            f'walked {a["walked"]}x, agreed our round-1 offer {a["agreed_round1"]}x, "fair point" {a["fair_point"]}x, '
            f'"does not change" {a["no_change"]}x, duplicates {a["duplicate"]}x.',
        ]
    out += ["", "## Documents", ""]
    out += [f"- `{d['id']}` {d['date']} {d['kind']} — {d['subject']}: {d['text'][:240]}" for d in pr["documents"]]
    out += [f"- `{x['id']}` news {x['date']} ({x['topic']}): {x['headline']}" for x in pr["news"]]
    return "\n".join(out) + "\n"


def index(rows, stamp):
    out = [
        "# Supplier profiles",
        "",
        f"Built {stamp}. One file per supplier: `sup_<no>.json` (agent) and `sup_<no>.md` (people). "
        "See [README.md](README.md) for how they are made and used.",
        "",
        "| Supplier | Category | Country | Owner | We are % of its turnover | Key traits | Opening | Walk risk | Arena |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        p = r.get("persona") or {}
        pb = p.get("playbook") or {}
        strong = [k for k, v in (p.get("traits") or {}).items() if v == "strong"]
        a = r.get("arena")
        flag = " ⚠" if r["problems"] else ""
        out.append(
            f"| [{r['name']}]({pid(r['id'])}.md) `{r['id']}`{flag} | {r['category']} | {r['country']} | "
            f"{(r.get('company') or {}).get('owner_type', '?')} | {_share(r['our_weight'])} | "
            f"{', '.join(strong) or '-'} | {pb.get('opening', '-')} | {pb.get('walk_risk', '-')} | "
            f"{(str(a['sessions']) + ' sessions, ' + ('best ' + str(a['best_score']) if a['best_score'] else 'no deal')) if a else '-'} |"
        )
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="profiles")
    ap.add_argument("cmd", choices=["build", "check", "facts"])
    ap.add_argument("supplier", nargs="?")
    ap.add_argument("--data", default="data/package")
    ap.add_argument("--out", default=str(PROFILE_DIR))
    ap.add_argument("--arena", action="append", default=[], help="glob of session records, e.g. 'runs/arena-*.json'")
    a = ap.parse_args(argv)
    world = World(a.data)
    if a.cmd == "facts":
        f = facts(world, a.supplier)
        print(json.dumps({**f, "source_hash": source_hash(f)}, indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "build":
        problems = build(world, a.out, a.arena)
    else:
        problems = {}
        for sid in supplier_ids(world):
            f, persona = facts(world, sid), _json(Path(a.out) / "persona" / f"{pid(sid)}.json")
            errs = check_persona(persona, f) if persona else ["no persona read yet"]
            if persona and persona.get("source_hash") not in (None, source_hash(f)):
                errs.append("documents changed since the persona read: read it again")
            problems[sid] = errs
    bad = {k: v for k, v in problems.items() if v}
    for sid, errs in bad.items():
        print(f"{sid}: {'; '.join(errs)}")
    print(f"{len(problems)} suppliers, {len(problems) - len(bad)} clean")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
