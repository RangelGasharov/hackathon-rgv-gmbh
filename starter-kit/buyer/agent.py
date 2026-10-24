"""The buyer agent: per supplier thread, read the reply, update the model, then accept, counter or walk.

Everything with a number is decided here and in `strategy`; the writer only words it. One
`BuyerAgent` plays one case; `decide(sid)` is called by the runner for each thread whenever that
thread's supplier has answered, under one lock, so RFQ threads can see each other's offers.
"""

import logging
import math
import re
import time

from arena_kit.cost import must_haves_ok
from arena_kit.docs import quote_terms

from .claims import candidates, ration
from .dossier import Dossier
from .strategy import Estimate, Planner, cost
from .writer import Plan, TemplateWriter, human_date

log = logging.getLogger("buyer")
ENDED = {"walked_away", "no_bid"}
COST_ROW = re.compile(r"^(material|conversion|overhead|margin|unit price)[^|]*\|\s*([-\d.]+)", re.I | re.M)


def latest(events):
    return next((m for m in reversed(events) if m.get("from") != "buyer"), {})


def standing(events, price=None):
    """(term_sheet_id, terms) of the supplier's standing offer. A supplier may send several
    alternatives (arena replay P01: two dates at two prices); with `price` (terms -> C for us)
    we take the one that is cheapest for us."""
    sheets = latest(events).get("term_sheets") or []
    if not sheets:
        return None, {}
    best = min(sheets, key=lambda s: price(s["terms"])) if price and len(sheets) > 1 else sheets[0]
    return best["term_sheet_id"], best["terms"]


def breakdown_cost(text):
    """Full cost per unit from a supplier's cost breakdown XLSX (material + conversion + overhead)."""
    rows = {
        m.group(1).lower(): float(m.group(2)) for m in COST_ROW.finditer(text or "") if m.group(2) not in ("-", ".")
    }
    parts = [rows.get(k) for k in ("material", "conversion", "overhead")]
    return round(sum(parts), 2) if all(p is not None for p in parts) else None


class ThreadState:
    def __init__(self, world, case, sid):
        self.sid = sid
        self.d = Dossier(world, case, sid)
        self.est = Estimate(self.d)
        rounds = max(3, min(6, case.get("round_limit", 10) - 3))
        self.plan = Planner(case, self.est, horizon_rounds=rounds)
        self.k = 0  # messages with term sheets sent
        self.sent = 0  # messages sent
        self.seen = 0  # supplier messages processed
        self.asked_breakdown = False
        self.has_breakdown = False
        self.contradicted = set()
        self.c_standing = math.inf  # its standing sheet's C for us, relationship estimate included
        self.c_norel = math.inf  # the same without the relationship estimate
        self.std = {}
        self.dead = False
        self.matched = False  # we have offered its own value level on our terms
        self.in_flight = False  # a message to this supplier is on its way: its standing sheet may change


class BuyerAgent:
    def __init__(self, world, writer=None, deadline=None, close_margin_s=90):
        self.world = world
        self.writer = writer or TemplateWriter()
        self.deadline, self.close_margin = deadline, close_margin_s
        self.case = None
        self.t = {}
        self.used_ids = set()
        self.c_open = None
        self.log = []
        self.round_seconds = 30.0  # our message + the supplier's reply; updated from what we measure

    def note_round(self, seconds):
        self.round_seconds = 0.6 * self.round_seconds + 0.4 * seconds if self.log else seconds

    # -- setup ------------------------------------------------------------------------------------
    def start(self, case):
        self.case = case
        self.t = {sid: ThreadState(self.world, case, sid) for sid in case["suppliers"]}

    # -- reading ----------------------------------------------------------------------------------
    def ingest(self, sid, events):
        ts = self.t[sid]
        incoming = [m for m in events if m.get("from") != "buyer"]
        for m in incoming[ts.seen :]:
            for att in m.get("attachments") or []:
                text, name = att.get("text") or "", att.get("name", "")
                if name.startswith("quote") or name.startswith("order"):
                    std = quote_terms(text).get("standard", {})
                    if std:
                        ts.std.update(std)
                        ts.est.set_standard(std)
                        ts.plan.refresh()
                elif "breakdown" in name and (c := breakdown_cost(text)):
                    ts.has_breakdown = True
                    ts.est.learn_cost(c)
                    ts.plan.refresh()
            for r in m.get("claim_results") or []:
                if r.get("result") == "contradicted":
                    ts.contradicted.add(r.get("index"))
            if m.get("status") in ENDED:
                ts.dead = True
            _, terms = (
                (m["term_sheets"][0]["term_sheet_id"], m["term_sheets"][0]["terms"])
                if m.get("term_sheets")
                else (None, {})
            )
            if terms:
                ts.plan.observe(terms, m.get("status"))
        ts.seen = len(incoming)
        _, terms = standing(events, self._price(sid))
        if terms.get("unit_price") is not None and must_haves_ok(self.case, terms, ts.est.standard):
            ts.c_standing = cost(self.case, terms, ts.est)
            ts.c_norel = cost(self.case, terms, ts.est, rel=False)
        else:  # no price, or a must-have broken (scores 0)
            ts.c_standing = ts.c_norel = math.inf

    def _price(self, sid):
        ts = self.t[sid]

        def c(terms):
            if terms.get("unit_price") is None or not must_haves_ok(self.case, terms, ts.est.standard):
                return math.inf
            return cost(self.case, terms, ts.est)

        return c

    # -- deciding ---------------------------------------------------------------------------------
    def decide(self, threads, sid):
        """The next message for thread `sid`, an action that closes the case, or None (leave it)."""
        for s, ev in threads.items():
            self.ingest(s, ev)
        if self.c_open is None:  # the opening quotes, in both views of C
            self.c_open = min((x.c_norel for x in self.t.values()), default=math.inf)
            self.c_open_rel = min((x.c_standing for x in self.t.values()), default=math.inf)
        ts = self.t[sid]
        live = {s: x for s, x in self.t.items() if not x.dead}
        if not live:
            return None
        best_sid = min(live, key=lambda s: live[s].c_standing)
        best = live[best_sid]
        # with a confident cost estimate the relationship charge is real money: limits use full C
        confident = all(x.plan.confident for x in live.values())
        reservation = min(x.plan.reservation(self.c_open_rel if confident else self.c_open) for x in live.values())
        measure = (lambda x: x.c_standing) if confident else (lambda x: x.c_norel)
        status = latest(threads[sid]).get("status")
        time_left = (self.deadline - time.time()) if self.deadline else math.inf

        def accept(s):
            if self.t[s].in_flight:
                return None  # its reply is still coming: decide again then
            sheet_id, _ = standing(threads[s], self._price(s))
            self.log.append((s, "accept", round(self.t[s].c_standing)))
            return {"supplier_id": s, "message": {"action": {"type": "accept", "term_sheet_id": sheet_id}}}

        def walk():
            self.log.append((sid, "walk", None))
            return {"supplier_id": sid, "message": {"action": {"type": "walk_away"}}}

        def settle():
            """Out of moves: the best standing sheet, unless the alternative is clearly cheaper. With a
            rough cost estimate the relationship part of C is a guess, so then the alternative must also
            win without it."""
            c_alt = min(x.plan.c_alt for x in live.values())
            if not math.isfinite(best.c_standing):
                return walk()
            if (
                math.isfinite(c_alt)
                and c_alt < best.c_standing * 0.98
                and (best.plan.confident or c_alt < best.c_norel * 0.98)
            ):
                return walk()
            return accept(best_sid)

        for x in live.values():
            x.plan.cap_c = reservation * 1.02
        # the wave's cap is near: take the best acceptable sheet, else the alternative
        if time_left < self.close_margin:
            return settle()
        if ts.dead:
            return None
        if status == "agreed" and measure(ts) <= reservation * 1.02 and ts.c_standing <= best.c_standing * 1.001:
            return accept(sid)  # it agreed to our own sheet
        rounds_left = self.case.get("round_limit", 10) - ts.sent
        if self.deadline:  # rounds that still fit before we must close, at the pace measured so far
            pace = max(10.0, 1.3 * self.round_seconds)
            rounds_left = min(rounds_left, int(max(0, time_left - self.close_margin) // pace))
        if status == "final_offer":
            # wait for another bidder only if it can still move (not on its own final offer)
            others = [x for s, x in live.items() if s != sid and math.isfinite(x.c_standing)
                      and latest(threads[s]).get("status") not in ("final_offer",) + tuple(ENDED)]
            if not ts.matched and rounds_left >= 1 and (m := self.safe_match(threads, sid)):
                return m  # its last word, on our terms, at its exact price
            if measure(ts) <= reservation and ts.c_standing <= best.c_standing * 1.001:
                return accept(sid)
            if others:
                return None  # leave it standing; another bidder is better
            return settle()
        # in an RFQ, keep going while another bidder's next package is clearly cheaper than this sheet
        rival_better = any(
            s != sid
            and x.sent < self.case.get("round_limit", 10) - 1
            and (nxt := x.plan.sheets(x.k, n=1))
            and cost(self.case, nxt[0], x.est) < ts.c_standing * 0.98
            for s, x in live.items()
        )
        if (
            ts.k >= 1
            and sid == best_sid
            and measure(ts) <= reservation
            and not rival_better
            and not ts.in_flight
            and ts.plan.accept_now(ts.c_standing, ts.k, math.inf, rounds_left)
        ):
            return accept(sid)  # never the first offer: one exchange first, for its documents and its move
        if rounds_left <= 0:
            return settle()
        if rounds_left <= 2 and not ts.matched and (m := self.match(threads, sid)):
            return m
        sheets = ts.plan.sheets(ts.k)
        if not sheets:  # nothing better left to offer within our limits
            if not ts.matched and (m := self.match(threads, sid)):
                return m
            if ts.sent == 0:  # first contact: ask our questions before deciding anything
                return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=[])}
            return settle()
        msg = self.compose(threads, sid, sheets=sheets)
        if ts.plan.ask_value and ts.plan.last_value and ts.plan.last_value >= ts.plan.ask_value * 0.99:
            ts.matched = True  # the concession path has reached its own offer
        return {"supplier_id": sid, "message": msg}

    def match(self, threads, sid):
        """Offer its own value level with our package, if that is cheaper for us than its sheet."""
        ts = self.t[sid]
        sheets = ts.plan.match_sheets()
        if not sheets or cost(self.case, sheets[0], ts.est) >= ts.c_standing * 0.999:
            return None
        ts.matched = True
        return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=sheets)}

    def safe_match(self, threads, sid):
        ts = self.t[sid]
        _, terms = standing(threads[sid], self._price(sid))
        sheet = ts.plan.safe_match(terms)
        if not sheet:
            return None
        ts.matched = True
        return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=[sheet])}

    # -- writing ----------------------------------------------------------------------------------
    def compose(self, threads, sid, sheets=None):
        ts, case, d = self.t[sid], self.case, self.t[sid].d
        sheets = ts.plan.sheets(ts.k) if sheets is None else sheets
        ts.plan.commit(sheets)
        rivals = {}
        for s, x in self.t.items():  # other bidders' standing offers, citable in an RFQ
            if s != sid and math.isfinite(x.c_standing):
                oid, terms = standing(threads[s], self._price(s))
                if oid and terms.get("unit_price"):
                    rivals[oid] = terms["unit_price"]
        allc = candidates(self.world, d, case, rivals)
        claims = ration(allc, self.used_ids, ts.sent)
        for c in claims:
            self.used_ids.update(i for i in c.ids if not i.startswith(("offer:", "quote:")))
        plan = Plan(
            case=case,
            dossier=d,
            round_no=ts.sent,
            sheets=sheets,
            claims=claims,
            questions=self.questions(ts),
            levers=self.levers(ts, sheets),
            tone=self.tone(ts, threads[sid]),
            last_reply=latest(threads[sid]).get("body", ""),
        )
        ts.k += 1 if sheets else 0
        ts.sent += 1
        # the body is written by `write()` outside the case lock, so a slow model never holds up other threads
        msg = {
            "subject": plan.subject,
            "body": None,
            "_plan": plan,
            "term_sheets": [{"terms": t} for t in sheets],
            "refs": sorted({i for c in claims for i in c.ids})[:20],
        }
        self.log.append((sid, "send", [t["unit_price"] for t in sheets]))
        return msg

    def write(self, msg):
        """Fill in the e-mail body of a message from `decide` (call it without holding the case lock)."""
        plan = msg.pop("_plan", None)
        if plan is not None and msg.get("body") is None:
            msg["body"] = self.writer.write(plan)
        return msg

    def tone(self, ts, events):
        d, first = ts.d, ts.sent == 0
        out = []
        if first:
            trig = "letter" if (d.letter and d.letter.get("type") == "letter") else "quote"
            out.append(f"Thank you for your {trig}.")
            if d.has("reciprocity"):
                out.append(
                    "We have not forgotten that you held your prices for a quarter in 2022, and we want an outcome that is fair to both sides."
                )
            elif d.has("reference_customer"):
                out.append("We value our cooperation and want to continue it on a sound basis.")
            if d.late_days > 5 or d.has("late_payment_grievance"):
                out.append("We have also put our payments to you on a fixed schedule; you will be paid on time.")
        else:
            out.append("Thank you for your reply.")
            if latest(events).get("status") == "final_offer":
                out.append("We understand this is close to your final position.")
        return out

    def levers(self, ts, sheets):
        d, out = ts.d, []
        if not sheets:
            return out
        s = sheets[0]
        if d.has("cash_short") and s.get("payment_days") is not None and s["payment_days"] <= 30:
            out.append(f"We can pay within {s['payment_days']} days, which should help your cash flow.")
        if d.hedge_until and s.get("effective_date"):
            out.append(
                f"As your material is covered until {d.hedge_until:%B %Y}, a new price from "
                f"{human_date(s['effective_date'])} follows your actual costs."
            )
        months = s.get("contract_months") or (self.case.get("fixed_terms") or {}).get("contract_months")
        need = self.case["buyer"]["need_per_year"]
        if d.has("capacity_tight"):
            out.append(
                f"We can commit to a binding volume of {need:,} a year" + (f" for {months} months." if months else ".")
            )
        elif (d.has("free_capacity") or d.has("small_account")) and months and months >= 24:
            out.append(f"We are ready to place the full annual volume of {need:,} with you for {months} months.")
        if d.has("price_frozen"):
            out.append(
                "We understand that your list price is fixed; our proposals therefore work through the other terms."
            )
        if self.case["buyer"].get("category_goal") in ("stabilise", "secure_supply") and d.has("below_full_cost"):
            out.append("We want a price that keeps this part healthy for both of us.")
        return out

    def questions(self, ts):
        out, d = [], ts.d
        ctype = self.case["type"]
        wants_cost = (
            d.has("open_book")
            or self.case["buyer"].get("category_goal") in ("stabilise", "secure_supply")
            or ctype == "price_increase"
        )
        if wants_cost and not ts.asked_breakdown and not ts.has_breakdown:
            out.append("Could you send us your cost breakdown for this part?")
            ts.asked_breakdown = True
        if ts.sent == 0 and ctype == "capacity":
            out.append("Which monthly quantities can you confirm for the next twelve months?")
        if ts.sent == 0 and ctype == "quality":
            out.append("Please send us your 8D report with the corrective actions and their dates.")
        return out
