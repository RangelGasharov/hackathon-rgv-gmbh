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

from . import flags

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
    "hedged_material": (
        r"bought (?:their |our )?(?:\w+ ){0,4}forward|covered at fixed prices|Material covered until|"
        r"at fixed prices with|(?:secured|covered|locked in)[^.]{0,60}(?:for|until|through) (?:\w+ )?20\d\d|"
        r"reaches our prices only from (?:\w+ )?20\d\d",
        None,
    ),
    "capacity_tight": (
        r"binding 12-month commitment|Order intake is very strong|cannot confirm your full forecast",
        None,
    ),
    "late_payment_grievance": (r"invoices are overdue", None),
    "wants_clause": (r"(?:price )?(?:adjustment|escalation|escalator|indexation|index|surcharge) clause", None),
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
    "customer_loss": "free_capacity",  # Sunday release: a customer cancelled its orders (70190 Pfaffenreuth)
    "chip_shortage": "capacity_tight",
}
# news topics that describe a supplier's situation now (the plan reads them; `NEWS_TRAITS` turns them into traits)
NEWS_SITUATION = {
    "restructuring": "distress",
    "credit_watch": "distress",
    "credit_downgrade": "distress",
    "short_time_work": "distress",
    "full_order_book": "capacity",
    "allocation": "capacity",
    "big_order": "capacity",
    "free_capacity": "slack",
    "new_plant": "slack",
    "customer_loss": "slack",  # it lost a customer and needs work (Sunday release: 70190)
    "chip_shortage": "capacity",
    "refocus": "refocus",
}
TRUSTED_YEARS = 8  # an active source of the part for this long is a partner, not a vendor
COUNTRY_COST = {"DE": 1.0, "AT": 1.05, "CH": 1.1, "CZ": 0.83, "PL": 0.83, "HU": 0.83, "TR": 0.75, "CN": 0.7}
OWNER_MARGIN = {"group": 0.12, "private": 0.10, "family": 0.09, "founder": 0.08}
MONTHS = {
    m: i
    for i, m in enumerate(
        "January February March April May June July August September October November December".split(), 1
    )
}
_MON = "|".join(MONTHS)
HEDGE_UNTIL = re.compile(
    rf"(?:until|through) (?:({_MON}) )?(\d{{4}})|(?:for|in) (?:the year )?(\d{{4}})|only from (?:({_MON}) )?(\d{{4}})"
)


def hedge_end(text):
    """The latest date a hedge in `text` runs to: "until March 2027" -> that month; "until 2027" / "for 2027" ->
    December of it; "reaches our prices only from 2028" -> the December before."""
    best = None
    for m in HEDGE_UNTIL.finditer(text):
        mon1, y1, y2, mon2, y3 = m.groups()
        if y1:
            d = date(int(y1), MONTHS.get(mon1, 12), 28)
        elif y2:
            d = date(int(y2), 12, 28)
        elif mon2:
            d = date(int(y3), MONTHS[mon2], 28)
        else:
            d = date(int(y3) - 1, 12, 28)
        best = max(best, d) if best else d
    return best
BEST_WINDOW_SLACK = 0  # index windows may end up to this many months before the latest month (None: 12 months only)


REPLY_SIGNALS = {  # phrases in a supplier's own e-mail that reveal what it needs
    "cash_short": r"faster payment|cash ?flow|pay(?:ment)? earlier|would help us more than anything|liquidity",
    # + what the two capacity-tight suppliers of the arena wrote (P10, P05 70240) and none of the other ten threads:
    # "cannot be accommodated given our current capacity utilization", "our machines are totally full", "rushing
    # production costs us extra", "not feasible; we must maintain 54 days to ensure stable supply"
    "capacity_tight": (
        r"binding (?:\d+-month )?commitment|firm,? long-term commitment|prioriti[sz]e customers|allocat|"
        r"capacity utili[sz]ation|(?:totally|completely|fully|pretty) (?:full|packed|booked)|"
        r"(?:super|very|really) busy right now|rushing production|squeeze (?:the )?production|"
        r"not feasible[^.]{0,40}(?:maintain|stable supply)"
    ),
    "price_frozen": r"frozen all price|no room on price|price list applies|cannot reduce our list",
    "approval_ladder": r"pricing committee|management approval|needs? (?:the )?approval",
}
# what one reply shows about a supplier stays true: arena "We did not know that - a fair point." came back to the two
# suppliers on old cost sheets (P01, P05-70210) and to no other; for them index, volume and delivery data count more
LASTING_SIGNALS = {"stale_costing": r"did not know that|fair point"}


LEGAL_FORMS = {"gmbh", "ag", "kg", "ek", "ohg", "gbr", "ug", "mbh", "se", "co", "und", "inh", "e", "k", "v", "kgaa"}


def _stem(name):
    """A supplier's name without its legal form: "Zerspanung Dralveck e.K." and "... GmbH" -> "zerspanung dralveck"."""
    words = re.sub(r"[^\w ]", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in LEGAL_FORMS)


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

    @cached_property
    def aliases(self):
        """{live supplier id: [old record ids]}: a supplier of the same name stem that is blocked, has no contract and
        is not in the full detail set is the old record of the live one (sup:71999 -> sup:70210, "renamed 2024")."""
        rows = self.all("SELECT id, name, detail, blocked, contract_id FROM suppliers")
        live = {}
        for r in rows:
            if not r["blocked"] and r["detail"] == "full":
                live.setdefault(_stem(r["name"]), []).append(r["id"])
        out = {}
        for r in rows:
            if r["blocked"] and not r["contract_id"] and len(live.get(_stem(r["name"]), [])) == 1:
                out.setdefault(live[_stem(r["name"])][0], []).append(r["id"])
        return out

    def ids(self, sid):
        """The supplier's live ID and the IDs of its old records: history, spend and scorecards fold under the live one."""
        return [sid, *self.aliases.get(sid, [])]

    def all_for(self, sql, sid, *a):
        """`all` for a query whose first parameter is `supplier_id=?`, over the supplier and its old records."""
        ids = self.ids(sid)
        return self.all(sql.replace("supplier_id=?", f"supplier_id IN ({','.join('?' * len(ids))})", 1), *ids, *a)

    def rows(self, table, supplier_id):
        """A supplier's rows of `table` (documents, news, po_lines, ...): the database plus the Sunday release's
        CSV rows, newest first by `date` where there is one. Dicts, so both sources read the same."""
        out = [dict(r) for r in self.all_for(f'SELECT * FROM "{table}" WHERE supplier_id=?', supplier_id)]
        if self.pkg.release:
            out += [
                dict(r)
                for r in self.pkg._ids.values()
                if r.get("root")
                and r.get("table") == table
                and supplier_id in (r.get("supplier_id"), r.get("supplier"))
            ]
        return sorted(out, key=lambda r: str(r.get("date") or r.get("order_date") or ""), reverse=True)

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

    def index_change_since(self, series, start_month):
        """(change %, ids, months) from `start_month` (or the first month after it with a value) to the
        latest month, if that window is at most 12 months: the arena checks windows up to 12 months."""
        last = self.series_last.get(series)
        if not last or not start_month or start_month >= last:
            return None
        if (int(last[:4]) - int(start_month[:4])) * 12 + int(last[5:7]) - int(start_month[5:7]) > 12:
            return None
        r = self.one(
            "SELECT id, month, value FROM index_values WHERE series_id=? AND month>=? ORDER BY month LIMIT 1",
            series,
            start_month,
        )
        (i1, v1) = self.idx(series, last)
        if not r or not v1 or r["month"] >= last:
            return None
        return round((v1 / r["value"] - 1) * 100, 1), r["id"], i1, r["month"], last

    def index_best_window(self, series, end_slack=0):
        """The most favourable (most negative) change over any window the arena accepts: 1-12 months long and
        ending at most `end_slack` months before the series' latest month (the arena takes windows that end in
        the last 12 months; a recent end keeps the argument current). (change %, id0, id1, month0, month1)."""
        rows = self.all("SELECT id, month, value FROM index_values WHERE series_id=? ORDER BY month", series)
        if not rows:
            return None
        ordinal = lambda mo: int(mo[:4]) * 12 + int(mo[5:7])  # noqa: E731
        by = {ordinal(r["month"]): r for r in rows}
        last = max(by)
        best = None
        for e in range(last - end_slack, last + 1):
            for s in range(e - 12, e):
                if e in by and s in by and by[s]["value"] and by[e]["value"]:
                    ch = round((by[e]["value"] / by[s]["value"] - 1) * 100, 1)
                    key = (ch, -e, s)  # most negative first, then the latest end, then the longest window
                    if best is None or key < best[0]:
                        best = (key, (ch, by[s]["id"], by[e]["id"], by[s]["month"], by[e]["month"]))
        return best[1] if best else None

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
        self.read = None  # the persona read by the model (`persona.read_all`), merged over the regex traits
        self.reply_hits = {}  # regex signals in its latest e-mail only (as calibrated on the arena)
        self.reply_traits = {}  # what the strategist read in its e-mails: kept for the rest of the case
        self.said = ""  # what the supplier has written to us so far
        self.profile = None  # its profile (`profiles/sup_<no>.json`): the deep read made once, set by the agent
        self.playbook = ""  # that profile's compact playbook, for the strategist
        self.strategy_plan = ""  # one profile-selected bargaining approach and at most one fallback
        self.traits = self._traits()
        self.dead_kinds = set()  # `claims.dead_key`s this supplier rejected (contradicted, irrelevant, "no change")
        self.facts_case = case.get("facts") or {}

    # -- evidence ---------------------------------------------------------------------------------
    def _texts(self):
        out = []
        for d in self.w.all("SELECT id FROM documents WHERE supplier_id=? ORDER BY date", self.sid):
            out.append(self.w.doc_text(d["id"]))
        for r in self.w.all("SELECT remarks FROM quotes WHERE supplier_id=? AND remarks!=''", self.sid):
            out.append(r[0])
        for r in self.w.all_for("SELECT note FROM negotiation_history WHERE supplier_id=? AND note!=''", self.sid):
            out.append(r[0])
        for r in self.w.all("SELECT headline, body FROM news WHERE supplier_id=? AND topic!='noise'", self.sid):
            out.append(f"{r[0]} {r[1] or ''}")
        for f in (self.case.get("facts") or {}).values():  # hidden cases may carry their own letters
            if isinstance(f, dict) and f.get("supplier") == self.sid:
                out.append(json.dumps(f))
        if len(self.case.get("suppliers") or []) == 1 and self.case.get("briefing"):
            out.append(self.case["briefing"])  # one supplier: the briefing describes it (hidden cases too)
        if self.w.pkg.release:  # the Sunday release: letters, e-mails and news only in its CSVs
            for ref, row in self.w.pkg._ids.items():
                if row.get("root") and (row.get("supplier_id") == self.sid or row.get("supplier") == self.sid):
                    if row.get("table") == "documents":
                        out.append(self.w.doc_text(ref))
                    elif row.get("table") == "news" and row.get("topic") != "noise":
                        out.append(f"{row.get('headline', '')} {row.get('body', '')}")
        return "\n".join(out)

    def add_text(self, text):
        """What the supplier writes during the negotiation updates its profile (arena: "faster payment
        would help us", "only against a binding commitment", "the index clause is mandatory")."""
        if not text:
            return False
        before = dict(self.traits)
        self.texts += "\n" + text
        self.reply_hits = {name: "strong" for name, p in REPLY_SIGNALS.items() if re.search(p, text, re.I)}
        self.said += "\n" + text
        for name, pattern in LASTING_SIGNALS.items():  # a "fair point" marks it stale-costing for good
            if re.search(pattern, self.said, re.I):
                self.reply_traits[name] = "strong"
        self.traits = self._traits()
        return self._changed(before)

    def apply_read(self, read):
        """Merge the model's persona read over the regex traits: where it is sure, it decides (also that a
        regex hit is not a trait); where it is unsure, the regex stays and the model can only add a hint."""
        before = dict(self.traits)
        self.read = read
        self.traits = self._traits()
        for prop in ("full_cost", "hedge_until"):
            self.__dict__.pop(prop, None)
        return self._changed(before)

    def note_traits(self, traits):
        """Traits the strategist read in its last e-mail ({name: "strong"|"weak"}): they stay, like regex hits."""
        before = dict(self.traits)
        for name, level in traits.items():
            if self.reply_traits.get(name) != "strong":
                self.reply_traits[name] = level
        self.traits = self._traits()
        return self._changed(before)

    def _changed(self, before):
        if self.traits != before:
            for prop in ("cash_rate", "min_margin"):
                self.__dict__.pop(prop, None)
            return True
        return False

    def _traits(self):
        """Regex traits from the documents and data, the model's persona read over them, its e-mails on top."""
        t = self._regex_traits()
        read = self.read or {}
        for name, level in (read.get("traits") or {}).items():
            if name in read.get("unsure", ()):
                if level != "none" and name not in t:
                    t[name] = "weak"
            elif level == "none":
                t.pop(name, None)
            else:
                t[name] = level
        for name, level in {**self.reply_traits, **self.reply_hits}.items():
            if t.get(name) != "strong":
                t[name] = level
        return t

    def _regex_traits(self):
        t = {}
        for name, (strong, weak) in TRAITS.items():
            s, w = _hits(self.texts, strong), _hits(self.texts, weak)
            if s or w:
                t[name] = "strong" if s else "weak"
        for r in self.w.rows("news", self.sid):
            if (name := NEWS_TRAITS.get(r.get("topic"))) and t.get(name) != "strong":
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
            if how.startswith("own"):
                # arena: the relationship charges on the P01 and P03 deals both put its full cost at 0.979 x ours
                # (to 0.1 %), so this estimate is good enough to price the relationship charge (P02 v3: 27.7k)
                return round(sc * 0.979, 2), 0.04
            return sc, 0.15  # arena: real floors 10-37 % off
        m = OWNER_MARGIN.get(self.intel["owner_type"] if self.intel else "", 0.09)
        if self.has("below_full_cost"):
            m = -0.04
        return round(self.reference_price * (1 - m), 2), 0.10

    @cached_property
    def cash_rate(self):
        if self.has("cash_short", strong=True):
            return 0.25
        if self.has("cash_short") or (self.sup and self.sup["skonto_pct"]):
            return 0.10  # a "pay in 10 days?" mail is a weak signal: 17 suppliers sent it
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
        weights, best = {}, {}
        parts = self.case["parts"]  # a kit has several parts: all of them count
        for p in parts:
            for r in self.w.all("SELECT series_id, weight FROM cost_drivers WHERE part_id=? ORDER BY weight DESC", p):
                windows = [self.w.index_change(r[0])]
                if self.price_since:  # also the window since the current price was agreed: often the fairer base
                    windows.append(self.w.index_change_since(r[0], self.price_since))
                if BEST_WINDOW_SLACK is not None:  # and the most favourable recent window of any length
                    windows.append(self.w.index_best_window(r[0], BEST_WINDOW_SLACK))
                windows = [w for w in windows if w]
                if windows:
                    weights.setdefault(r[0], []).append(r[1])
                    best[r[0]] = min(windows + ([best[r[0]]] if r[0] in best else []), key=lambda w: w[0])
        out = [(sr, round(sum(ws) / len(parts), 3), *best[sr]) for sr, ws in weights.items()]
        return sorted(out, key=lambda x: -x[1])

    @cached_property
    def price_since(self):
        """Month the current price of this part with this supplier took effect (YYYY-MM), if known."""
        r = self.w.one(
            "SELECT valid_from FROM price_conditions WHERE part_id=? AND supplier_id=? ORDER BY valid_from DESC LIMIT 1",
            self.part,
            self.sid,
        )
        return r[0][:7] if r and r[0] else None

    @cached_property
    def justified_pct(self):
        """Weighted 12-month change of the cost drivers (the fair yardstick, not the cherry-picked windows)."""
        out = 0.0
        for series, w, *_ in self.drivers:
            ch = self.w.index_change(series)
            out += w * (ch[0] if ch else 0.0)
        return round(out, 1)

    def schedule_csv(self):
        """Our latest monthly demand forecast per part, as a CSV a supplier can read (true data, no IDs). Quantities
        are per price unit, the unit the case need and our binding volume are in (a kit counts per kit)."""
        units = {
            (self.w.one("SELECT price_unit FROM parts WHERE id=?", part) or [None])[0] or 1 for part in self.case["parts"]
        }
        head = f"forecast_qty_per_{units.pop()}_units" if len(units) == 1 else "forecast_qty_per_price_unit"
        lines = [f"part,description,month,{head}"]
        for part in self.case["parts"]:
            fc = self.w.one("SELECT id FROM forecasts WHERE part_id=? ORDER BY snapshot_month DESC LIMIT 1", part)
            row = self.w.one("SELECT description, price_unit FROM parts WHERE id=?", part)
            desc = (row[0] if row else "-").replace(",", " ")
            unit = (row[1] if row else None) or 1
            if fc:
                for r in self.w.all(
                    "SELECT target_month, qty FROM forecast_lines WHERE fc_id=? ORDER BY target_month", fc[0]
                ):
                    qty = r[1] / unit
                    lines.append(f"{part.split(':')[1]},{desc},{r[0]},{round(qty, 1) if unit != 1 else r[1]}")
        return "\n".join(lines) + "\n" if len(lines) > 1 else None

    def drivers_csv(self):
        """The part's cost drivers with their latest index values and 12-month changes (true data)."""
        lines = ["series,description,weight_in_part_cost,month_from,value_from,month_to,value_to,change_pct"]
        for series, w, *_ in self.drivers:
            ch = self.w.index_change(series)
            if not ch:
                continue
            pct, i0, i1, m0, m1 = ch
            v0, v1 = self.w.facts[i0]["value"], self.w.facts[i1]["value"]
            name = (self.w.one("SELECT name FROM index_series WHERE id=?", series) or [series])[0].replace(",", " ")
            lines.append(f"{series},{name},{w},{m0},{v0},{m1},{v1},{pct}")
        return "\n".join(lines) + "\n" if len(lines) > 1 else None

    @cached_property
    def recent_orders(self):
        """The last 2-3 order lines of this part with this supplier: [(po id, price)]."""
        rows = self.w.all(
            "SELECT id, price FROM po_lines WHERE part_id=? AND supplier_id=? AND COALESCE(price_note,'')<>'stale_condition' "
            "ORDER BY order_date DESC LIMIT 3",
            self.part,
            self.sid,
        )
        return [(r[0], r[1]) for r in rows]

    @cached_property
    def news_items(self):
        """News about this supplier that speaks for us: [(id, topic, headline)]."""
        good = ("free_capacity", "energy_contract", "new_plant", "refocus")
        return [(r["id"], r["topic"], r["headline"]) for r in self.w.rows("news", self.sid) if r.get("topic") in good]

    @cached_property
    def letter(self):
        trig = self.case.get("trigger_doc")
        if not trig:
            return None
        f = self.w.facts.get(trig) or self.facts_case.get(trig) or {}
        return {"id": trig, **f, "text": self.w.doc_text(trig)}

    @cached_property
    def own_letter(self):
        """The trigger document is ours (e.g. our quality escalation), not the supplier's."""
        trig = self.case.get("trigger_doc")
        row = (self.w.pkg.lookup(trig) or {}) if trig else {}  # the database or the Sunday release
        return str(row.get("author") or "").startswith("Hollerau")

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

    def clause(self, kind):
        """The text of our framework agreement's clause of this type ("price", "quality", ...), or ""."""
        if not self.contract:
            return ""
        r = self.w.one("SELECT text FROM contract_clauses WHERE contract_id=? AND type=?", self.contract["id"], kind)
        return r[0] if r else ""

    @cached_property
    def status_quo(self):
        """What our framework agreement's own index clause makes the price, as of its latest quarterly adjustment:
        P = P0 x (1 - s + s x I(m-2) / I(base)), base = the index two months before the price year, adjusted when the
        change exceeds the clause's threshold (contract_clauses: series, material_share, deadband_pct); P0 = the first
        quarter's order price. Reproduces this year's order prices to the cent (P08: 5.24, 5.43, 5.45). A dict for the
        latest adjustment, or None."""
        if not self.contract:
            return None
        cl = self.w.one(
            "SELECT series, material_share, deadband_pct FROM contract_clauses WHERE contract_id=? AND type='index'",
            self.contract["id"],
        )
        if not cl or not cl["series"] or not cl["material_share"]:
            return None
        series, s, thr = cl["series"], cl["material_share"], cl["deadband_pct"] or 0
        last = self.w.series_last.get(series)
        if not last:
            return None
        year = int(last[:4])
        _, base = self.w.idx(series, f"{year - 1}-11")
        q1 = sorted(
            r[0]
            for r in self.w.all(
                "SELECT price FROM po_lines WHERE part_id=? AND supplier_id=? AND order_date>=? AND order_date<?",
                self.part,
                self.sid,
                f"{year}-01-01",
                f"{year}-04-01",
            )
        )
        if not base or not q1:
            return None
        p0 = q1[len(q1) // 2]
        price, prev, out = p0, f"{year - 1}-11", None
        for start, lag in ((f"{year}-04", f"{year}-02"), (f"{year}-07", f"{year}-05"), (f"{year}-10", f"{year}-08")):
            lag_id, v = self.w.idx(series, lag)
            if not v:
                break
            new = p0 * (1 - s + s * v / base)
            if abs(new / price - 1) * 100 > thr:
                prev_id, prev_v = self.w.idx(series, prev)
                out = {"from": start, "price": round(new, 2), "before": round(price, 2), "series": series,
                       "share": s, "ids": [prev_id, lag_id], "months": [prev, lag],
                       "change": round((v / prev_v - 1) * 100, 1)}
                price, prev = new, lag
        return out

    @cached_property
    def complaint_fee(self):
        """EUR per supplier-caused complaint that our quality agreement already gives us (every framework
        agreement in the package: "flat fee of EUR 150"), else None."""
        m = re.search(r"fee of EUR ([\d.,]+) per supplier-caused complaint", self.clause("quality"))
        return float(m.group(1).replace(",", "")) if m else None

    @cached_property
    def hedge_until(self):
        if not self.has("hedged_material"):
            return None
        if until := (self.read or {}).get("hedge_until"):  # the model reads any wording ("until end of Q1 2027")
            return date(int(until[:4]), int(until[5:7]), 28)
        return hedge_end(self.texts)

    @property
    def wants(self):
        """What the persona read says it wants from a deal (e.g. "index_clause", "faster_payment")."""
        return set((self.read or {}).get("wants") or ())

    @cached_property
    def scorecard(self):
        r = self.w.all_for(
            "SELECT id, otd_pct, ppm, colour FROM scorecards WHERE supplier_id=? ORDER BY quarter DESC LIMIT 1",
            self.sid,
        )
        return dict(r[0]) if r else None

    @cached_property
    def late_days(self):
        r = (self.w.all_for(
            "SELECT avg(julianday(paid_date)-julianday(due_date)) FROM invoices WHERE supplier_id=? "
            "AND paid_date IS NOT NULL AND discount_taken=0",
            self.sid,
        ) or [None])[0]
        return round(r[0], 1) if r and r[0] is not None else 0.0

    @cached_property
    def forecasts(self):
        """Latest forecast change per part of the case: [(fc id, change %)] (kits have several parts)."""
        out = []
        for part in self.case["parts"]:
            fc = sorted((k, v) for k, v in self.w.facts.items() if k.startswith("fc:") and v.get("part") == part)
            if fc:
                out.append((fc[-1][0], fc[-1][1]["change_pct"]))
        return out

    @cached_property
    def forecast(self):
        fc = sorted((k, v) for k, v in self.w.facts.items() if k.startswith("fc:") and v.get("part") == self.part)
        return (fc[-1][0], fc[-1][1]["change_pct"]) if fc else None

    @cached_property
    def history(self):
        rows = self.w.all_for(
            "SELECT asked_pct, settled_pct FROM negotiation_history WHERE supplier_id=? AND type='annual renewal'",
            self.sid,
        )
        s = [r[1] for r in rows if r[1] is not None]
        return round(sum(s) / len(s), 1) if s else None

    @cached_property
    def deals(self):
        """Its past negotiations with us (negotiation_history, the Sunday release included): (count, mean asked %,
        mean settled %), or None. Not a ratio: asks near or below zero make settled/asked meaningless."""
        rows = [r for r in self.w.rows("negotiation_history", self.sid) if r.get("asked_pct") is not None]
        rows = [r for r in rows if r.get("settled_pct") is not None]
        if not rows:
            return None
        n = len(rows)
        return n, round(sum(r["asked_pct"] for r in rows) / n, 1), round(sum(r["settled_pct"] for r in rows) / n, 1)

    @cached_property
    def current_news(self):
        """Its news that tells us its situation now, newest first: [(id, date, topic, headline)]."""
        return [
            (r.get("id"), r.get("date"), r.get("topic"), r.get("headline"))
            for r in self.w.rows("news", self.sid)
            if r.get("topic") in NEWS_SITUATION
        ]

    @cached_property
    def relationship(self):
        """(trusted, reasons). A trusted supplier is one whose goodwill is worth more than the last cent: a
        strategic category goal (the arena charges for a supplier left below a healthy margin), a supplier that
        kept supplying at the old price, or a long-standing active source of this part. A red scorecard is
        never trusted. Trusted suppliers get no risky trades (`Planner`, `BuyerAgent.compose`)."""
        if (self.scorecard or {}).get("colour") == "red":
            return False, ["red scorecard"]
        reasons = []
        if self.goal in ("stabilise", "secure_supply"):
            reasons.append(f"category goal {self.goal}")
        if self.has("reciprocity", strong=True):
            reasons.append("it kept supplying at the old price and expects partnership")
        years = []
        for p in self.case["parts"]:
            r = self.w.one("SELECT status, approved_since FROM sourcing WHERE part_id=? AND supplier_id=?", p, self.sid)
            if r and r["status"] == "active" and r["approved_since"]:
                years.append((date.today() - date.fromisoformat(r["approved_since"][:10])).days / 365.25)
        if years and max(years) >= TRUSTED_YEARS:
            reasons.append(f"our active source of this part for {int(max(years))} years")
        return bool(reasons), reasons

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
        sq = self.status_quo
        if sq and self.current and sq["price"] < self.current:
            lines.append(
                f"Status quo: the index clause of our framework agreement ({sq['series']}, share {sq['share']}) puts "
                f"the price at {sq['price']:.2f} from {sq['from']}-01 ({sq['series']} {sq['change']:+.1f}% from "
                f"{sq['months'][0]} to {sq['months'][1]}), below the current {self.current}: a new price basis starts there."
            )
        if self.read and self.read.get("notes"):
            lines.append("Our read of this supplier: " + self.read["notes"])
        if self.wants:
            lines.append("It wants: " + ", ".join(sorted(self.wants)) + ".")
        if self.playbook and flags.on("playbook"):
            lines.append(self.playbook)
        if self.strategy_plan and flags.on("plan"):
            lines.append(self.strategy_plan)
        return "\n".join(lines)
