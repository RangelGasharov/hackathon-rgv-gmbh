"""The supplier dossier: everything the data says about one supplier in one case, computed before
the 60-second clock runs. Persona traits come from the recurring phrases in notes, letters, quote
remarks, negotiation history and news; economics from should-cost, prices, contracts and invoices.
"""

import json
import re
import sqlite3
import threading
from datetime import date
from functools import cached_property

from arena_kit.data import DataPackage

# trait -> (strong pattern, weak pattern); a strong hit is evidence, a weak one only a hint
TRAITS = {
    "cash_short": (
        r"asked twice whether we could pay|bank is watching|short hours|half the shop floor was idle|"
        r"cannot sign anything that lowers our margin",
        r"pay our invoices within 10 days|Payment within 10 days requested",
    ),
    "approval_ladder": (r"pricing committee", None),
    "price_frozen": (
        r"frozen all price reductions|applies to all customers without exception|head-office directive|"
        r"discounts not possible",
        r"only multi-year volume agreements change prices",
    ),
    "small_account": (
        r"not strategic|individual agreements will not be extended|key accounts",
        r"reviewing small accounts|focus on customers with volume|supply from stock only",
    ),
    "open_book": (r"walk us through their calculation", None),
    "free_capacity": (r"second shift has been on hold|could take additional parts", None),
    "reference_customer": (r"important reference customer", None),
    "stale_costing": (r"own spreadsheet|not followed his material|office manager does the costing", None),
    "reciprocity": (r"kept supplying at the old price|expects partnership", None),
    "below_full_cost": (r"accepted below full cost", None),
    "hedged_material": (r"bought their \w+(?: and \w+)? forward|covered at fixed prices|Material covered until", None),
    "capacity_tight": (
        r"binding 12-month commitment|Order intake is very strong|cannot confirm your full forecast",
        None,
    ),
    "late_payment_grievance": (r"invoices are overdue", None),
}
NEWS_TRAITS = {
    "restructuring": "cash_short",
    "credit_watch": "cash_short",
    "credit_downgrade": "cash_short",
    "short_time_work": "cash_short",
    "free_capacity": "free_capacity",
    "full_order_book": "capacity_tight",
    "allocation": "capacity_tight",
    "big_order": "capacity_tight",
    "refocus": "small_account",
    "energy_contract": "hedged_material",
    "new_plant": "free_capacity",
}
COUNTRY_COST = {"DE": 1.0, "AT": 1.05, "CH": 1.1, "CZ": 0.83, "PL": 0.83, "HU": 0.83, "TR": 0.75, "CN": 0.7}
OWNER_MARGIN = {"group": 0.12, "private": 0.10, "family": 0.09, "founder": 0.08}
HEDGE_UNTIL = re.compile(r"until (January|March|June|December) (\d{4})")
MONTHS = {"January": 1, "March": 3, "June": 6, "December": 12}


class World:
    """Read-only access to the data package (and the Sunday release, merged by DataPackage)."""

    def __init__(self, root):
        self.pkg = DataPackage(root)
        self.root = self.pkg.root
        self.facts = self.pkg.facts
        self._local = threading.local()
        self._doc_lock = threading.Lock()

    @property
    def db(self):
        """One read-only connection per thread: cases play in parallel."""
        conn = getattr(self._local, "db", None)
        if conn is None:
            conn = sqlite3.connect(f"file:{self.root / 'world.sqlite'}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            self._local.db = conn
        return conn

    def one(self, sql, *a):
        return self.db.execute(sql, a).fetchone()

    def all(self, sql, *a):
        return self.db.execute(sql, a).fetchall()

    @cached_property
    def series_last(self):
        return {r[0]: r[1] for r in self.all("SELECT series_id, max(month) FROM index_values GROUP BY series_id")}

    def idx(self, series, month):
        r = self.one("SELECT id, value FROM index_values WHERE series_id=? AND month=?", series, month)
        return (r["id"], r["value"]) if r else (None, None)

    def index_change(self, series, months=12):
        """(change %, earlier id, later id, earlier month, later month) over the last `months` months."""
        last = self.series_last.get(series)
        if not last:
            return None
        y, m = int(last[:4]), int(last[5:7])
        m0 = m - months
        while m0 <= 0:
            y, m0 = y - 1, m0 + 12
        first = f"{y}-{m0:02d}"
        (i0, v0), (i1, v1) = self.idx(series, first), self.idx(series, last)
        if not v0 or not v1:
            return None
        return round((v1 / v0 - 1) * 100, 1), i0, i1, first, last

    def doc_text(self, doc_id):
        try:
            with self._doc_lock:  # DataPackage shares one SQLite connection
                return self.pkg.document(doc_id)
        except (FileNotFoundError, ValueError):
            return ""


def _hits(text, pattern):
    return len(re.findall(pattern, text, re.I)) if pattern else 0


class Dossier:
    """What we know about `supplier` in `case`. Cheap to build; build it once per case."""

    def __init__(self, world, case, supplier):
        self.w, self.case, self.sid = world, case, supplier
        self.no = supplier.split(":", 1)[1]
        self.part = case["parts"][0]
        self.kit = (case.get("local_meta") or {}).get("kit")
        b = case["buyer"]
        self.current = b.get("current_unit_price")
        self.need = b["need_per_year"]
        self.goal = b.get("category_goal")
        self.sup = world.one("SELECT * FROM suppliers WHERE id=?", supplier)
        self.intel = world.one("SELECT * FROM supplier_intel WHERE supplier_id=?", supplier)
        self.contract = world.one("SELECT * FROM contracts WHERE supplier_id=?", supplier)
        self.texts = self._texts()
        self.traits = self._traits()
        self.facts_case = case.get("facts") or {}

    # -- evidence ---------------------------------------------------------------------------------
    def _texts(self):
        out = []
        for d in self.w.all("SELECT id FROM documents WHERE supplier_id=? ORDER BY date", self.sid):
            out.append(self.w.doc_text(d["id"]))
        for r in self.w.all("SELECT remarks FROM quotes WHERE supplier_id=? AND remarks!=''", self.sid):
            out.append(r[0])
        for r in self.w.all("SELECT note FROM negotiation_history WHERE supplier_id=? AND note!=''", self.sid):
            out.append(r[0])
        for r in self.w.all("SELECT headline, body FROM news WHERE supplier_id=? AND topic!='noise'", self.sid):
            out.append(f"{r[0]} {r[1] or ''}")
        for f in (self.case.get("facts") or {}).values():  # hidden cases may carry their own letters
            if isinstance(f, dict) and f.get("supplier") == self.sid:
                out.append(json.dumps(f))
        return "\n".join(out)

    def _traits(self):
        t = {}
        for name, (strong, weak) in TRAITS.items():
            s, w = _hits(self.texts, strong), _hits(self.texts, weak)
            if s or w:
                t[name] = "strong" if s else "weak"
        for r in self.w.all("SELECT topic FROM news WHERE supplier_id=?", self.sid):
            if (name := NEWS_TRAITS.get(r[0])) and t.get(name) != "strong":
                t[name] = "strong" if name == "cash_short" else t.get(name, "weak")
        if self.intel and self.intel["credit_index"] and self.intel["credit_index_12m_ago"]:
            if self.intel["credit_index"] - self.intel["credit_index_12m_ago"] >= 40:
                t["cash_short"] = "strong"
        if self.sup and self.sup["skonto_pct"]:
            t.setdefault("cash_short", "weak")
        return t

    def has(self, trait, strong=False):
        v = self.traits.get(trait)
        return v == "strong" if strong else bool(v)

    # -- economics --------------------------------------------------------------------------------
    @cached_property
    def should_cost(self):
        r = self.w.one(
            "SELECT unit_cost, supplier_id FROM should_cost WHERE part_id=? ORDER BY (supplier_id=?) DESC LIMIT 1",
            self.part,
            self.sid,
        )
        if not r or self.kit:
            return None, None
        own = r["supplier_id"] == self.sid
        factor = 1.0 if own else COUNTRY_COST.get(self.sup["country"] if self.sup else "DE", 1.0)
        return round(r["unit_cost"] * factor, 2), ("own plant ±5%" if own else "German base ±5-15%")

    @cached_property
    def reference_price(self):
        meta = self.case.get("local_meta") or {}
        return self.current or meta.get("reference_price") or (self.should_cost[0] or 0) * 1.15

    @cached_property
    def full_cost(self):
        """Our estimate of its full cost per unit, and how sure we are (relative error)."""
        sc, how = self.should_cost
        if sc:
            return sc, 0.05 if how.startswith("own") else 0.10
        m = OWNER_MARGIN.get(self.intel["owner_type"] if self.intel else "", 0.09)
        if self.has("below_full_cost"):
            m = -0.04
        return round(self.reference_price * (1 - m), 2), 0.10

    @cached_property
    def cash_rate(self):
        if self.has("cash_short", strong=True):
            return 0.25
        if self.has("cash_short") or (self.sup and self.sup["skonto_pct"]):
            return 0.14
        return 0.07

    @cached_property
    def min_margin(self):
        owner = self.intel["owner_type"] if self.intel else "family"
        m = {"group": 0.06, "private": 0.04, "family": 0.03, "founder": 0.03}.get(owner, 0.04)
        if self.has("free_capacity"):
            m -= 0.02
        if self.has("cash_short", strong=True):
            m -= 0.01
        if self.has("capacity_tight"):
            m += 0.04
        if self.has("small_account") or self.has("price_frozen"):
            m += 0.02
        return min(max(m, 0.0), 0.15)

    @cached_property
    def standard_guess(self):
        """Its standard terms as far as the master data tells (the quote PDF wins once we have it)."""
        s = self.sup
        q = self.w.one(
            "SELECT payment_days, incoterm, lead_time_days FROM quotes WHERE supplier_id=? AND part_id=? "
            "ORDER BY quote_date DESC LIMIT 1",
            self.sid,
            self.part,
        )
        pay = min(x for x in [s["payment_days"] if s else 30, q["payment_days"] if q and q["payment_days"] else 999])
        return {
            "payment_days": pay,
            "incoterm": (s["incoterm"] if s else "DAP"),
            "lead_time_days": s["standard_lead_time_days"] if s else 42,
            "contract_months": 12,
        }

    # -- arguments --------------------------------------------------------------------------------
    @cached_property
    def drivers(self):
        """[(series, weight, change %, id0, id1, m0, m1)] of the part's cost drivers over 12 months."""
        out = []
        for p in [self.part] if not self.kit else self.case["parts"]:
            for r in self.w.all("SELECT series_id, weight FROM cost_drivers WHERE part_id=? ORDER BY weight DESC", p):
                ch = self.w.index_change(r[0])
                if ch:
                    out.append((r[0], r[1], *ch))
            if out:
                break
        return out

    @cached_property
    def justified_pct(self):
        return round(sum(w * ch for _, w, ch, *_ in self.drivers), 1)

    @cached_property
    def letter(self):
        trig = self.case.get("trigger_doc")
        if not trig:
            return None
        f = self.w.facts.get(trig) or self.facts_case.get(trig) or {}
        return {"id": trig, **f, "text": self.w.doc_text(trig)}

    @cached_property
    def notice(self):
        """(letter id, letter days, contract id, contract days) when a price letter gives less notice than agreed."""
        L = self.letter
        if not L or not L.get("days") or not self.contract:
            return None
        cdays = (self.w.facts.get(self.contract["id"]) or {}).get("days")
        if cdays and L["days"] < cdays:
            return L["id"], L["days"], self.contract["id"], cdays
        return None

    @cached_property
    def hedge_until(self):
        m = HEDGE_UNTIL.search(self.texts) if self.has("hedged_material") else None
        return date(int(m.group(2)), MONTHS[m.group(1)], 28) if m else None

    @cached_property
    def scorecard(self):
        r = self.w.one(
            "SELECT id, otd_pct, ppm, colour FROM scorecards WHERE supplier_id=? ORDER BY quarter DESC LIMIT 1",
            self.sid,
        )
        return dict(r) if r else None

    @cached_property
    def late_days(self):
        r = self.w.one(
            "SELECT avg(julianday(paid_date)-julianday(due_date)) FROM invoices WHERE supplier_id=? "
            "AND paid_date IS NOT NULL AND discount_taken=0",
            self.sid,
        )
        return round(r[0], 1) if r and r[0] is not None else 0.0

    @cached_property
    def forecast(self):
        fc = sorted((k, v) for k, v in self.w.facts.items() if k.startswith("fc:") and v.get("part") == self.part)
        return (fc[-1][0], fc[-1][1]["change_pct"]) if fc else None

    @cached_property
    def history(self):
        rows = self.w.all(
            "SELECT asked_pct, settled_pct FROM negotiation_history WHERE supplier_id=? AND type='annual renewal'",
            self.sid,
        )
        s = [r[1] for r in rows if r[1] is not None]
        return round(sum(s) / len(s), 1) if s else None

    def brief(self):
        """A compact text for the writer model."""
        sc, how = self.should_cost
        lines = [
            f"Supplier {self.sid} {self.sup['name'] if self.sup else ''} ({self.sup['country'] if self.sup else ''}, "
            f"{self.intel['owner_type'] if self.intel else '?'}, {self.intel['staff'] if self.intel else '?'} staff).",
            "Traits from our notes: " + (", ".join(f"{k} ({v})" for k, v in self.traits.items()) or "none"),
            f"Current price {self.current}; should-cost {sc} ({how}); 12-month cost-driver change {self.justified_pct:+.1f}%.",
        ]
        if self.scorecard:
            lines.append(
                f"Scorecard {self.scorecard['id']}: OTD {self.scorecard['otd_pct']}%, {self.scorecard['ppm']} ppm, {self.scorecard['colour']}."
            )
        if self.late_days > 3:
            lines.append(f"We have paid this supplier on average {self.late_days} days late.")
        if self.hedge_until:
            lines.append(f"Its material is hedged until {self.hedge_until:%B %Y}.")
        return "\n".join(lines)
