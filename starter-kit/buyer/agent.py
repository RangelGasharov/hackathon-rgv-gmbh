"""The buyer agent: per supplier thread, read the reply, update the model, then accept, counter or walk.

Everything with a number is decided here and in `strategy`; the writer only words it. One
`BuyerAgent` plays one case; `decide(sid)` is called by the runner for each thread whenever that
thread's supplier has answered, under one lock, so RFQ threads can see each other's offers.
"""

import copy
import logging
import math
import os
import re
import threading
import time

from arena_kit.cost import RELATIONSHIP_GOALS, must_haves_ok
from arena_kit.docs import quote_terms

from . import flags, profiles
from .claims import candidates, dead_key, kind_key, no_change_kinds, ration
from .dossier import Dossier
from .persona import opening_text, read_all
from .strategy import Estimate, Planner, cost
from .approach import LEVERAGE, QUICK_CLOSE, granted
from .writer import Plan, TemplateWriter, asks_for, human_date

log = logging.getLogger("buyer")
ENDED = {"walked_away", "no_bid"}
# arena P08, P18: "we cannot offer fixed prices for extended contract durations", "we cannot fix prices over a 24-month
# period ..., making the AS-14404 index clause mandatory", "a fixed price over two years is not feasible"; the twin's words
APPROVAL_WORDS = re.compile(r"pricing committee|management approval|needs? (?:the )?approval", re.I)
FLOOR_WORDS = re.compile(
    r"lowest (?:price|we can|possible)|rock[- ]bottom|(?:cannot|can't|can not|unable to) go (?:any )?lower|"
    r"bottom (?:line|price)|no (?:more|further) room",
    re.I,
)
CLAUSE_MANDATORY = re.compile(
    r"cannot (?:offer|fix|agree) (?:a )?(?:fixed )?prices? (?:over|beyond|for)|"
    r"fixed prices? (?:over|beyond|for) [^.]{0,40}(?:not feasible|not possible)|"
    r"(?:index|adjustment|escalation) clause (?:is |as )?(?:mandatory|required)|"
    r"only (?:with|against) an? (?:index|price adjustment) clause",
    re.I,
)
# how the real suppliers ask for our numbers (arena P05, P07, P08, P10, P18): "please send us the actual volume data",
# "the forecast data so we can plan", "figures for the binding volume", "proof for that volume", "release schedules",
# "consumption data", "which figures is that based on", "provide the specific data", "the data behind your target price"
ASKS_VOLUME = re.compile(
    r"schedule|call-?off|monthly quantit|release|consumption|"
    r"(?:volume|forecast|quantit\w+|planning)\b.{0,60}\b(?:data|figures|proof|numbers|evidence|back up|substantiate)|"
    r"(?:data|figures|proof|numbers|evidence)\b.{0,60}\b(?:volume|forecast|planning)",
    re.S,
)
ASKS_DATA = re.compile(
    r"which figures|send us the data|provide (?:the )?(?:specific )?data|data behind|calculation|cost analysis",
    re.I,
)
LEARN_OPENING_LEAD = True  # a supplier's opening lead time is its standard (see `learn_opening_standard`)
MAX_ROUNDS = 10  # the arena's round limit: never plan for more messages per thread, whatever a case file says
# argument kinds a trusted supplier does not hear: our own price history never moved an arena supplier, and citing
# its news reads as prying. (The delivery record stays: `claims` offers it only to stale-costing owners, the one
# kind of supplier it moved.)
RISKY_KINDS = {"po", "news"}


HARD_WRITE_S = 20.0  # no e-mail takes longer than this to write: then the template sends the same sheets
HOLD_AFTER_S = 30.0  # a supplier whose reply nobody has answered for this long gets a note or a close, never silence


def round_cap(case):
    """Messages we may send per thread: the case's round limit, at most `MAX_ROUNDS`."""
    return min(int(case.get("round_limit") or MAX_ROUNDS), MAX_ROUNDS)


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


def eight_d_commitment(text):
    """The defect rate a supplier commits to in its 8D report ("Commitment: 1500 ppm")."""
    m = re.search(r"Commitment:\s*([\d.,]+)\s*ppm", text or "", re.I)
    return int(float(m.group(1).replace(",", ""))) if m else None


def schedule_lead_time(text):
    """The lead time on a supplier's delivery schedule ("Lead time (days) | 56")."""
    m = re.search(r"Lead time \(days\)\s*\|\s*(\d+)", text or "", re.I)
    return int(m.group(1)) if m else None


class ThreadState:
    def __init__(self, world, case, sid, dossier=None):
        self.sid = sid
        self.d = dossier or Dossier(world, case, sid)
        self.est = Estimate(self.d)
        rounds = max(3, min(6, round_cap(case) - 3))
        self.plan = Planner(case, self.est, horizon_rounds=rounds)
        self.k = 0  # messages with term sheets sent
        self.sent = 0  # messages sent
        self.took_alt = False  # we sent back an alternative it offered, to take it: once
        self.seen = 0  # supplier messages processed
        self.asked_breakdown = False
        self.opened_lead = None  # the lead time of its opening quote: the standard it prices a shorter one against
        self.snap = None  # our state before the last message was composed, to undo it if the arena refuses it
        self.composed = []  # the sheets of that message
        self.has_breakdown = False
        self.contradicted = set()
        self.c_standing = math.inf  # its standing sheet's C for us, relationship estimate included
        self.c_norel = math.inf  # the same without the relationship estimate
        self.std = {}
        self.dead = False
        self.matched = False  # we have offered its own value level on our terms
        self.in_flight = False  # a message to this supplier is on its way: its standing sheet may change
        self.held = False  # RFQ: we asked an agreeing bidder to wait while the others answer
        self.last_sent = []  # our last sheets: never resend them unchanged
        self.sent_claims = []  # claim sentences already sent: to answer "which figures?"
        self.used_kinds = set()  # kinds of argument this supplier has heard: it counts each kind once
        self.sent_schedule = self.sent_drivers = False
        self.last_claims = []
        self.last_body = ""
        self.reply_seen_at = time.monotonic()  # when we last read a new message from it (hold-note timer)
        self.fallback_seen = -1  # the reply we already answered with a fallback holding note
        self.phrase_dead = set()  # kinds retired only by the "does not change our calculation" phrase
        self.signals = None  # the strategist's reading of its last e-mail, applied at the next `ingest`
        self.asks = []  # BUYER_STRAT: what its last e-mail asked us for (`writer.asks_for`), each priced as a trade
        self.said_main_source = False
        self.agreed_holds = 0  # Leverage in a tender: times we held our price after an "agreed" above our limit
        self.band_waited = -1  # the reply (`seen`) we last waited on inside `CLOSE_BAND` for a rival's reply


class BuyerAgent:
    def __init__(self, world, writer=None, deadline=None, close_margin_s=90, read_timeout_s=15.0, profiles_root=None,
                 use_profiles=None):  # fmt: skip
        self.world = world
        self.writer = writer or TemplateWriter()
        self.read_timeout = read_timeout_s  # live persona read, only for suppliers without a profile
        self.profiles_root = profiles_root
        # BUYER_PROFILES=0 or BUYER_OFF=profiles plays without them (A/B)
        if use_profiles is None:
            use_profiles = os.environ.get("BUYER_PROFILES", "1") != "0" and flags.on("profiles")
        self.use_profiles = use_profiles
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
    def start(self, case, threads=None):
        """Build each supplier's dossier with its profile (`profiles/`: the deep read made once, no clock running).
        Only a supplier without a usable profile gets the live persona read (all in parallel, `read_timeout`
        seconds at most: the 60 s for our first message are running)."""
        self.case = case
        dossiers = {sid: Dossier(self.world, case, sid) for sid in case["suppliers"]}
        live = []
        for sid, d in dossiers.items():
            prof = profiles.load(sid, self.profiles_root) if self.use_profiles else None
            read = profiles.read_of(prof)
            if not read:
                live.append(sid)
                continue
            before = dict(d.traits)
            d.profile, d.playbook = prof, profiles.brief(prof)
            d.apply_read(read)
            self.log.append((sid, "profile", {"merged": d.traits, "regex": before}))
        complete = getattr(self.writer, "complete", None)
        if complete and self.read_timeout and live and flags.on("persona"):
            items = [(sid, dossiers[sid], opening_text((threads or {}).get(sid))) for sid in live]
            for sid, read in read_all(complete, items, self.read_timeout).items():
                before = dict(dossiers[sid].traits)
                dossiers[sid].apply_read(read)
                self.log.append((sid, "persona", {**read, "merged": dossiers[sid].traits, "regex": before}))
        # BUYER_STRAT: each supplier's own quote and the rivals' quotes on the same part choose its approach
        quotes = {}
        for sid, events in (threads or {}).items():
            _, terms = standing(events or [])
            if terms.get("unit_price"):
                quotes[sid] = terms["unit_price"]
        for sid, d in dossiers.items():
            d.quote_now = quotes.get(sid)
            d.rival_quotes = {s: q for s, q in quotes.items() if s != sid}
        self.t = {sid: ThreadState(self.world, case, sid, dossiers[sid]) for sid in case["suppliers"]}
        for sid, ts in self.t.items():
            a = ts.plan.approach
            self.log.append(
                (
                    sid,
                    "strategy_selected",
                    {
                        "primary": ts.plan.primary_strategy,
                        "fallback": ts.plan.fallback_strategy,
                        "reason": ts.plan.strategy_reason,
                        **(
                            {"approach": a.name, "gap": a.opening_gap, "tactics": a.tactics, "why": a.reason}
                            if a
                            else {}
                        ),
                    },
                )
            )

    # -- reading ----------------------------------------------------------------------------------
    def ingest(self, sid, events):
        ts = self.t[sid]
        if ts.signals:
            self.apply_signals(ts, events)
        incoming = [m for m in events if m.get("from") != "buyer"]
        if len(incoming) > ts.seen:
            ts.reply_seen_at = time.monotonic()
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
                elif "8d" in name.lower() and (ppm := eight_d_commitment(text)):
                    ts.plan.set_ppm_floor(ppm)  # what it commits to in its 8D: no package asks for less
                elif "schedule" in name.lower() and (lead := schedule_lead_time(text)):
                    # the schedule carries the lead time of the sheet on the table (arena P10: 56 days after we
                    # offered 56), not its standard: the opening quote's lead time wins
                    if not ts.std.get("lead_time_days") and not ts.opened_lead:
                        ts.std["lead_time_days"] = lead
                        ts.est.set_standard({"lead_time_days": lead})
                        ts.plan.refresh()
            body = m.get("body") or ""
            for r in m.get("claim_results") or []:  # arguments that failed are not repeated in kind
                i, res = r.get("index"), r.get("result")
                if res == "contradicted":
                    ts.contradicted.add(i)
                # arena v3: `duplicate` came back for a second argument of a kind it had already counted
                if res in ("contradicted", "irrelevant", "duplicate") and i is not None and i < len(ts.last_claims):
                    ts.d.dead_kinds.add(dead_key(ts.last_claims[i]))
            # "we checked the <data> you cited; it holds, but it does not change our calculation": retire the kinds
            # it names, not the ones in the same reply that counted (arena P05: a rival quote that counted next to a
            # delivery record that did not); the strategist's "counted" can undo it (`phrase_dead`)
            ts.phrase_dead = {c.kind for c in ts.last_claims if c.kind in no_change_kinds(body)}
            ts.d.dead_kinds.update(ts.phrase_dead)
            if self.clause_series and any(s in body for s in self.clause_series):
                ts.plan.want_clause()  # its costs follow the clause's own series (P02: "CU-NOTATION rose 47.7 %")
            ts.plan.second_step = bool(
                re.search(r"come back to you on (?:the )?price|clarify (?:this|it) internally", body, re.I)
            )
            ts.plan.soft_reply = bool(ts.plan.second_step or APPROVAL_WORDS.search(body))
            if FLOOR_WORDS.search(body):
                ts.plan.said_floor = True
            if CLAUSE_MANDATORY.search(body):
                ts.plan.require_clause()
            if ts.d.add_text(body):  # its own words update the profile and the packages
                ts.est.rate, ts.est.min_margin = ts.est.rate_learnt or ts.d.cash_rate, ts.d.min_margin
                if ts.d.has("capacity_tight"):
                    ts.est.tight_capacity_seen()
                ts.plan.refresh()
            if m.get("status") in ENDED:
                ts.dead = True
            if m.get("term_sheets"):  # every alternative it sent, not just the first
                self.learn_opening_standard(ts, m)
                change = ts.plan.observe_reply([x.get("terms") or {} for x in m["term_sheets"]], m.get("status"))
                if change:
                    self.log.append((sid, "strategy_switch", change))
            ts.last_body = m.get("body") or ""
            ts.asks = asks_for(ts.last_body)
        ts.seen = len(incoming)
        _, terms = standing(events, self._price(sid))
        if terms.get("unit_price") is not None and must_haves_ok(self.case, terms, ts.est.standard):
            ts.c_standing = cost(self.case, terms, ts.est)
            ts.c_norel = cost(self.case, terms, ts.est, rel=False)
        else:  # no price, or a must-have broken (scores 0)
            ts.c_standing = ts.c_norel = math.inf

    @property
    def clause_series(self):
        """The index series the case's clause options are on."""
        if "index_clause" not in (self.case or {}).get("negotiable", []):
            return set()
        return {o["series"] for o in self.case.get("term_options", {}).get("index_clause") or [] if o}

    def apply_signals(self, ts, events):
        """The strategist's reading of the supplier's last e-mail, applied under the case lock before its next
        one is read. It overrules the phrase regex where they disagree; claim results stay the arena's word."""
        s, ts.signals = ts.signals, None
        for kind in s.get("counted") or ():
            if kind in ts.phrase_dead:  # "does not change our calculation" was about another argument
                ts.d.dead_kinds.discard(kind)
        ts.d.dead_kinds.update(s.get("dead") or ())
        for ask in asks_for(" ".join(s.get("asks_for") or ())):  # what it asked for, in the strategist's words
            if ask not in ts.asks:
                ts.asks.append(ask)
        _, terms = standing(events, self._price(ts.sid))
        for field in s.get("keep_terms") or ():
            if field in terms:
                ts.plan.keep(field, terms[field])
        if s.get("traits") and ts.d.note_traits(s["traits"]):
            ts.est.rate, ts.est.min_margin = ts.est.rate_learnt or ts.d.cash_rate, ts.d.min_margin
            ts.plan.refresh()

    def learn_opening_standard(self, ts, m):
        """A supplier's opening quote states the lead time it works to (arena P10: 54 days quoted, 42 in the master
        data, and its MESO pair 33.6 @ 42 days / 31.7 @ 54 is iso-value only at 0.5 % a day below 54): that is the
        standard it prices a shorter lead time against, and the value the arena lets us offer even when it is not
        one of the options. With the master-data guess the agent bought 42 days at a premium it never saw."""
        if ts.opened_lead is not None or "lead_time_days" not in self.case["negotiable"]:
            return
        leads = [(x.get("terms") or {}).get("lead_time_days") for x in m["term_sheets"]]
        leads = [x for x in leads if isinstance(x, int | float)]
        ts.opened_lead = max(leads) if leads else 0
        prices = [(x.get("terms") or {}).get("unit_price") for x in m["term_sheets"]]
        ts.est.quote_price = next((x for x in prices if x), None)
        if ts.opened_lead and not ts.std.get("lead_time_days") and LEARN_OPENING_LEAD:
            ts.est.set_standard({"lead_time_days": ts.opened_lead})
            ts.plan.refresh()

    def invalid_message(self, sid):
        """The arena refused our last message with a 422 ("nothing happens, no round is used"). If it carried a value
        outside `term_options` (the supplier's own standard), take that back for good: undo the message, offer
        options only and decide again. True if that is the remedy; else the old behaviour (re-read, decide again)."""
        ts = self.t[sid]
        opts = self.case.get("term_options") or {}
        sheets = ts.composed
        off = any(
            v is not None and opts.get(f) and v not in opts[f]
            for t in sheets
            for f, v in t.items()
            if f != "unit_price"
        )
        if not off or ts.plan.strict_options or ts.snap is None:
            return False
        for x in self.t.values():
            x.plan.set_strict()
        self.log.append((sid, "strict_options", None))
        (ts.k, ts.sent, ts.last_sent, ts.last_claims, ts.sent_claims, ts.sent_schedule, ts.sent_drivers,
         ts.asked_breakdown, ts.matched, ts.plan.last_sheets, ts.plan.last_value, used, kinds) = ts.snap  # fmt: skip
        self.used_ids, ts.used_kinds = set(used), set(kinds)
        ts.snap = None
        return True

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
            mine = [m for m in ev if m.get("from") == "buyer"]
            x = self.t[s]
            if len(mine) > x.sent:  # resumed after a crash: count what was already sent
                x.sent, x.k = len(mine), max(x.k, sum(bool(m.get("term_sheets")) for m in mine))
                sent = [t.get("terms") or t for m in mine for t in (m.get("term_sheets") or [])]
                if sent and not x.plan.last_value:
                    x.plan.commit(sent[-3:])
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
        # C is exact without a relationship charge (it is the published formula on known terms): a deal dearer
        # than the alternative then scores less than walking, so no slack above it (api.md: no deal scores
        # the alternative). With the charge, our estimate of it keeps a 2 % band.
        exact = self.case["buyer"].get("category_goal") not in RELATIONSHIP_GOALS
        c_alt = min(x.plan.c_alt for x in live.values())
        slack = 1.0 if exact and reservation >= c_alt else 1.02
        measure = (lambda x: x.c_standing) if confident else (lambda x: x.c_norel)
        status = latest(threads[sid]).get("status")
        time_left = (self.deadline - time.time()) if self.deadline else math.inf

        def accept(s, force=False):
            if self.t[s].in_flight and not force:
                return None  # its reply is still coming: decide again then (never when the clock or messages ran out)
            sheet_id, terms = standing(threads[s], self._price(s))
            main_id, _ = standing(threads[s])  # its standing offer: the first sheet of its last message
            if main_id and sheet_id != main_id:
                # the arena accepts only the standing sheet (api.md; P18 rerun: two 422s on "offer:70740-4-a"). An
                # alternative it sent is ours by sending its exact terms back: agreed 2 of 2 (P08, P18), also after
                # a final offer. Without a message or the time left for that, its standing sheet.
                x = self.t[s]
                if not x.took_alt and round_cap(self.case) - x.sent > 0 and time_left > self.close_margin:
                    x.took_alt = True
                    self.log.append((s, "take_alternative", terms.get("unit_price")))
                    return {"supplier_id": s, "message": self.compose(threads, s, sheets=[dict(terms)])}
                sheet_id = main_id
            self.log.append((s, "accept", round(self.t[s].c_standing)))
            return {"supplier_id": s, "message": {"action": {"type": "accept", "term_sheet_id": sheet_id}}}

        def walk():
            self.log.append((sid, "walk", None))
            return {"supplier_id": sid, "message": {"action": {"type": "walk_away"}}}

        def settle(force=False):
            """Out of moves: the best standing sheet, unless the alternative is clearly cheaper. With a
            rough cost estimate the relationship part of C is a guess, so then the alternative must also
            win without it."""
            if not math.isfinite(best.c_standing):
                # no priced sheet that keeps the must-haves: walking scores the alternative, and without one
                # it scores 0 - so then keep the threads open for a priced sheet instead
                return walk() if math.isfinite(c_alt) else None
            band = 1.0 if exact else 0.98
            if (
                math.isfinite(c_alt)
                and c_alt < best.c_standing * band
                and (best.plan.confident or c_alt < best.c_norel * band)
            ):
                return walk()
            return accept(best_sid, force)

        for x in live.values():
            x.plan.cap_c = reservation * slack
        # the wave's cap is near: take the best acceptable sheet, else the alternative
        if time_left < self.close_margin:
            return settle(force=True)
        if ts.dead:
            return None
        rounds_left = round_cap(self.case) - ts.sent
        # in an RFQ, keep going while another bidder's next package is clearly cheaper than this sheet
        rival_better = any(  # another bidder's real standing offer is clearly cheaper and it can still move,
            # or it said it will come back on price: its second step is coming (b3 P05: we signed 29.50 with one
            # bidder while the other's "rest of the step" had brought it to 28.00 the batch before), or its reply to
            # our last message is still on its way (twin P05: we signed 30.26 while 28.38 was arriving)
            s != sid
            and (
                x.c_standing < ts.c_standing * 0.98
                or ((x.plan.second_step or x.in_flight) and x.c_standing < ts.c_standing * 1.06)
                or self.heading_below(x, ts)
                or self.short_of_must_have(threads, s, ts)
            )
            and x.sent < round_cap(self.case) - 1
            and latest(threads[s]).get("status") not in ("final_offer",) + tuple(ENDED)
            for s, x in live.items()
        )
        # RFQ: another bidder that can still move has a reply on its way and stands within reach of this sheet:
        # its standing sheet is a round old (twin P05, base code too: we signed 70250's final at 30.26 while
        # 70210's reply - its final at 28.38 - was in flight, and 70210's standing sheet still read 30.34)
        pending = any(
            s != sid
            and x.in_flight
            and x.c_standing < ts.c_standing * 1.08
            and latest(threads[s]).get("status") not in ("final_offer",) + tuple(ENDED)
            for s, x in live.items()
        )
        # RFQ: a bidder we have not written to yet may still beat this sheet - it has its own concession path (P05: we
        # signed one bidder's final offer at 32.50 while another, never contacted, would have ended near 28)
        untouched = any(
            s != sid and x.sent == 0 and latest(threads[s]).get("status") not in ("final_offer",) + tuple(ENDED)
            for s, x in live.items()
        )
        rival_better = rival_better or untouched
        if status == "agreed" and measure(ts) <= reservation * slack and ts.c_standing <= best.c_standing * 1.001:
            # RFQ: let the other bidders answer before we sign; how long depends on the supplier's approach (Quick-close,
            # a walk risk: one note; else up to 3 while rounds and time remain, twin P05: its rivals' finals came later)
            if (rival_better or pending) and (not ts.held or self.hold_again(ts, rounds_left, time_left)):
                ts.held = True
                ts.agreed_holds += 1
                return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=[], hold=True)}
            return accept(sid)  # it agreed to our own sheet
        if self.deadline:  # rounds that still fit before we must close, at the pace measured so far
            pace = max(10.0, 1.3 * self.round_seconds)
            rounds_left = min(rounds_left, int(max(0, time_left - self.close_margin) // pace))
        if status == "final_offer":
            # wait for another bidder only if it can still move (not on its own final offer)
            others = [
                x
                for s, x in live.items()
                if s != sid
                and math.isfinite(x.c_standing)
                and latest(threads[s]).get("status") not in ("final_offer",) + tuple(ENDED)
            ]
            # arena replays P08, P10, P18: any change to a final offer (even "neutral" ones like dropping
            # its index clause or a shorter lead time) made the supplier walk. Its last word is take-or-leave.
            # (never this bidder's final sheet while another bidder's standing sheet is cheaper: live P05 signed
            # 70240's final 31.50 at 42 days while 70250 stood at 31.08 and 28 days)
            if (
                ts.plan.sw("finalc")
                and not ts.in_flight
                and not pending
                and not untouched
                and ts.c_standing <= best.c_standing * 1.001
            ):
                # BUYER_FINALC: of its sheets (standing or alternatives) the one with the lowest C for us
                sheets = latest(threads[sid]).get("term_sheets") or []
                priced = [
                    (cost(self.case, x["terms"], ts.est, rel=confident), i)
                    for i, x in enumerate(sheets)
                    if must_haves_ok(self.case, x["terms"], ts.est.standard)
                ]
                priced = [p for p in priced if math.isfinite(p[0]) and p[0] <= reservation * slack]
                if priced:
                    _, i = min(priced)
                    if i == 0:
                        self.log.append((sid, "accept", round(ts.c_standing)))
                        return {
                            "supplier_id": sid,
                            "message": {"action": {"type": "accept", "term_sheet_id": sheets[0]["term_sheet_id"]}},
                        }
                    if not ts.took_alt and round_cap(self.case) - ts.sent > 0 and time_left > self.close_margin:
                        ts.took_alt = True
                        self.log.append((sid, "take_alternative", sheets[i]["terms"].get("unit_price")))
                        return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=[dict(sheets[i]["terms"])])}
            if (
                measure(ts) <= reservation
                and ts.c_standing <= best.c_standing * 1.001
                and not pending
                and not untouched
            ):
                return accept(sid)
            if others:
                # a holding note now, and again whenever its last reply is 40 s old (a silent thread times
                # out after 60 s: "your last offer repeated", and three timeouts end it) while rounds remain
                quiet = time.monotonic() - ts.reply_seen_at > 40
                if not ts.in_flight and (not ts.held or (quiet and rounds_left > 2)):
                    ts.held = True
                    return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=[], hold=True)}
                if quiet and rounds_left <= 1 and not ts.in_flight:
                    # no note left to send: one timeout after a final offer and it walks (arena b2 P05: 70210's
                    # final at 28.00 lost so) - close on the best standing sheet while it still stands
                    return settle()
                return None  # leave it standing; another bidder is better
            return settle()
        # BUYER_FLOOR: it still steps (or only said it needs approval): keep countering while a message and time remain,
        # but not inside `CLOSE_BAND` of our next sheet, where a counter risks its walk for less than 1 % of the price
        chase = (
            ts.plan.chasing()
            and rounds_left >= 2
            and time_left > 120
            and not ts.plan.close_enough(ts.c_standing, ts.k, rounds_left)
        )
        if (
            not chase
            and ts.k >= 1
            and sid == best_sid
            and measure(ts) <= reservation
            and not rival_better
            and not ts.in_flight
            and ts.plan.accept_now(ts.c_standing, ts.k, math.inf, rounds_left)
        ):
            return accept(sid)  # never the first offer: one exchange first, for its documents and its move
        if (
            not chase
            and ts.k >= 1
            and sid == best_sid
            and measure(ts) <= reservation
            and not rival_better
            and not ts.in_flight
            and (ts.plan.near_floor() or ts.plan.holding())
            # Leverage-or-chase in a tender: a bidder that still steps in even amounts has not reached its floor (the
            # floor estimate read twin P05's 70240 as done at its third step)
            and not (len(live) > 1 and ts.plan.approach and ts.plan.stepping() and rounds_left >= 3)
        ):
            return accept(sid)  # close to its estimated floor, or it stopped moving
        if (
            ts.plan.approach
            and ts.plan.approach.name == QUICK_CLOSE
            and ts.k >= 2
            and ts.plan.ask_value
            and ts.plan.last_value
            and ts.plan.ask_value <= ts.plan.last_value * 1.005
            and sid == best_sid
            and measure(ts) <= reservation
            and not rival_better
            and not ts.in_flight
        ):
            self.log.append((sid, "quick_close", ts.k))
            return accept(sid)  # BUYER_STRAT Quick-close: within 0.5 % of our own offer from round 2, no chasing
        # RFQ: this thread has nothing to offer, but the best bidder's thread has not been negotiated yet. Signing its
        # opening quote now would skip its whole concession path. Which thread decides first depends on timing and on
        # dict order: P06 ended at the cheap bidder's opening quote (score 0) in 6 of 24 identical runs under load,
        # and P05 scored 0.40, 0.63 or 0.80 depending on PYTHONHASHSEED (0.80 with this guard, every seed).
        wait_best = (
            sid != best_sid
            and best.k == 0
            and not best.dead
            and latest(threads[best_sid]).get("status") not in ("final_offer",) + tuple(ENDED)
        )
        if rounds_left <= 0:
            # no message left: decide. Our last message may still be a counter - the supplier answers it, and after
            # the round limit we may still accept (api.md: 409 "you reached the round limit (you may still accept)")
            return None if wait_best else settle()
        if rounds_left <= 2 and not ts.matched and self._may(ts, "match") and (m := self.match(threads, sid)):
            return m
        if ts.plan.second_step and ts.last_sent and len(self.case.get("suppliers") or []) > 1 and status == "open":
            if rounds_left > 2:
                # the RFQ two-step: after a rival's quote it gives half the step now and the rest after its internal
                # check (back office, P05). Hold our exact sheets ("resend the same sheet"): the planner rose 0.3 % a
                # round meanwhile, and a sheet on its way up is one it can accept instead of giving the rest
                self.log.append((sid, "two_step_hold", [t.get("unit_price") for t in ts.last_sent]))
                held = [dict(t) for t in ts.last_sent]
                return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=held, exact=True)}
        if (
            len(live) > 1
            and sid == best_sid
            and status == "open"
            and measure(ts) <= reservation
            and not ts.in_flight
            and any(x.in_flight for s, x in live.items() if s != sid)
            and ts.plan.close_enough(ts.c_standing, ts.k, rounds_left)
        ):
            # a tender: the best sheet sits within `CLOSE_BAND` of ours and only a rival's reply on its way kept us from
            # taking it. Wait for that reply instead of a sliver counter (live P05: 28.93 against 70250's 29.00 while
            # 70210's reply was coming; it came at 30.50); the runner's holding note keeps the 60 s
            if ts.band_waited != ts.seen:
                ts.band_waited = ts.seen
                self.log.append((sid, "band_wait", ts.k))
            return None
        sheets = ts.plan.sheets(ts.k, rounds_left=rounds_left)
        if sheets and ts.last_sent and all(t in ts.last_sent for t in sheets):
            sheets = []  # never resend the same sheets: suppliers read that as no move
        if not sheets and ts.k < 2 and rounds_left > 2 and math.isfinite(reservation) and self._may(ts, "probe"):
            # every package looks dearer than our walk-away, but that rests on a guess of its floor: put up to two
            # real offers just under the walk-away before settling (accepting still needs a sheet that beats it)
            sheets = [t for t in ts.plan.probe(reservation * 0.97) if not ts.last_sent or t not in ts.last_sent]
        if not sheets and ts.sent <= 1 and not ts.last_sent and rounds_left > 2 and status not in ("agreed", "final_offer"):
            # always procure: a real offer before any walk, in every approach, one step up from our last (it only
            # stays under our walk-away if it ends cheaper than the alternative - settle() checks that)
            sheets = [t for t in ts.plan.cheapest(ts.last_sent) if not ts.last_sent or t not in ts.last_sent]
        met = bool(ts.plan.last_value and ts.plan.ask_value and ts.plan.ask_value <= ts.plan.last_value * 1.001)
        if sheets and (status == "agreed" or met) and math.isfinite(ts.c_standing):
            # never a sheet dearer for us than its own standing sheet once it agreed to our sheet or repeated our
            # price as its offer (`met`; any earlier and twin P06 settled on a dear bidder after round 1): the path
            # would step UP (minimum step, "no step back") - P08 went 6.08 -> 6.35 over ten "agreed", live P05
            # 31.05 -> 31.09 a cent a round; nothing left means we settle (walk, or its sheet if that beats the
            # alternative), or hold while other bidders still move.
            sheets = [t for t in sheets if cost(self.case, t, ts.est, rel=confident) <= ts.c_standing * 1.0005]
        if status == "agreed" and not sheets and self.tender_hold(threads, sid, live, rounds_left, time_left):
            # Leverage in a tender with live rivals: do not settle at once and give up their leverage. Hold the price
            # it agreed to (never dearer), cite the best rival once, keep the other threads going. Quick-close, a
            # single supplier and a trusted supplier keep the settle below.
            ts.agreed_holds += 1
            sheets = [dict(standing(threads[sid], self._price(sid))[1])]
            self.log.append((sid, "agreed_hold", sheets[0].get("unit_price")))
            return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=sheets, rival=True)}
        if not sheets:  # nothing better left to offer within our limits
            if not ts.matched and self._may(ts, "match") and (m := self.match(threads, sid)):
                return m
            if ts.sent == 0:  # first contact: ask our questions before deciding anything
                return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=[])}
            if (
                (rival_better or pending)
                and (status == "agreed" or met)
                and status != "final_offer"
                and not ts.in_flight
                and (not ts.held or self.hold_again(ts, rounds_left, time_left))
            ):
                # RFQ: it stands at our own price, but another bidder still moves: a holding note, not a signature
                ts.held = True
                ts.agreed_holds += 1
                return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=[], hold=True)}
            return None if wait_best else settle()
        msg = self.compose(threads, sid, sheets=sheets, trade=True)
        if ts.plan.ask_value and ts.plan.last_value and ts.plan.last_value >= ts.plan.ask_value * 0.99:
            ts.matched = True  # the concession path has reached its own offer
        return {"supplier_id": sid, "message": msg}

    @staticmethod
    def _may(ts, gate):
        """probe / match: on (BUYER_OFF) and, under BUYER_STRAT, allowed by the thread's approach."""
        return flags.on(gate) and ts.plan.allows(gate)

    def hold_again(self, ts, rounds_left, time_left):
        """Another holding note to an agreeing bidder while the others answer: not for Quick-close or a thread that
        cannot take Leverage (walk risk, capacity, trusted), at most 3 in all, only with messages and time to spare."""
        a = ts.plan.approach
        if not a or ts.plan.strategy_evidence.get("trusted"):
            return False
        cap = 3 if a.name != QUICK_CLOSE and a.leverage_ok else 2
        return ts.agreed_holds < cap and rounds_left >= 3 and time_left > self.close_margin + 60 and not ts.in_flight

    def tender_hold(self, threads, sid, live, rounds_left, time_left):
        """After `agreed` above our limit: hold once (Leverage, a tender, a rival that can still move, a trusted-free
        supplier, messages and time left) instead of settling."""
        ts = self.t[sid]
        a = ts.plan.approach
        if not a or a.name != LEVERAGE or ts.agreed_holds >= 1 or len(self.case.get("suppliers") or []) < 2:
            return False
        if ts.plan.strategy_evidence.get("trusted") or ts.in_flight or rounds_left < 3 or time_left < self.close_margin + 60:
            return False
        return any(
            s != sid
            and math.isfinite(x.c_standing)
            and x.sent < round_cap(self.case) - 1
            and latest(threads[s]).get("status") not in ("final_offer",) + tuple(ENDED)
            for s, x in live.items()
        )

    def short_of_must_have(self, threads, s, ts):
        """Another bidder whose standing sheet still misses a must-have (its quoted lead time, say) but is clearly
        cheaper on its own terms: it is negotiating, not out (twin P06: its 167.10 at 70 days read as no offer and we
        signed the 217.07 bidder after one counter; that bidder took our 162.63 at a lead time we can use)."""
        x = self.t[s]
        if math.isfinite(x.c_standing) or not math.isfinite(ts.c_standing):
            return False
        _, terms = standing(threads[s], None)
        if terms.get("unit_price") is None:
            return False
        return cost(self.case, terms, x.est) < ts.c_standing * 0.98

    @staticmethod
    def heading_below(x, ts):
        """Another bidder that has only made its first moves: where they point (`limit_hat`) is more than 1 % under
        this sheet. Twin P05 under load: we signed one bidder's 30.26 while the other, at 30.34 after its first
        counter, was on its way to its final offer at 28.38."""
        lim, ask = x.plan.limit_hat, x.plan.ask_value
        return bool(lim and ask and lim < ask and x.c_standing * lim / ask < ts.c_standing * 0.99)

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
    def compose(self, threads, sid, sheets=None, hold=False, trade=False, rival=False, exact=False):
        """The next message: code's default choice of sheets, arguments and points, plus the menu the
        strategist (Opus) may choose from instead. State is committed in `write`, once the choice is made.
        `trade` (BUYER_STRAT, our own concession sheets only): each ask of its last e-mail gets a priced sheet."""
        ts, case, d = self.t[sid], self.case, self.t[sid].d
        ts.snap = (ts.k, ts.sent, list(ts.last_sent), list(ts.last_claims), list(ts.sent_claims), ts.sent_schedule,
                   ts.sent_drivers, ts.asked_breakdown, ts.matched, list(ts.plan.last_sheets), ts.plan.last_value,
                   set(self.used_ids), set(ts.used_kinds))  # fmt: skip
        sheets = ts.plan.sheets(ts.k) if sheets is None else sheets
        _, own = standing(threads[sid], self._price(sid))
        # after its final offer only its own terms go back (taking an alternative it sent): changed terms after a
        # final offer ended in a walk 3 times in 5 (P08, P10, P18), its own terms were agreed 2 of 2
        final = latest(threads[sid]).get("status") == "final_offer"
        if final and sheets:
            mine = [x.get("terms") or {} for x in latest(threads[sid]).get("term_sheets") or []]
            keep = [t for t in sheets if any(m and all(t.get(f) == v for f, v in m.items()) for m in mine)]
            if keep != sheets:
                self.log.append((sid, "final_guard", [t.get("unit_price") for t in sheets if t not in keep]))
            sheets = keep
        rivals = {}
        for s in self.t:  # other bidders' standing offers, cited only when cheaper than this one's (even
            # when their other terms do not suit us: the arena checks the price, the supplier hears "cheaper").
            # Their standing sheet, not an alternative: an unknown ID comes back "contradicted", which costs trust
            if s != sid and latest(threads[s]).get("status") not in ENDED:
                oid, terms = standing(threads[s])
                if (
                    oid
                    and terms.get("unit_price")
                    and (not own.get("unit_price") or terms["unit_price"] < own["unit_price"])
                ):
                    rivals[oid] = terms["unit_price"]
        approach, pace = ts.plan.approach, ts.plan.sw("pace")
        allc = candidates(
            self.world, d, case, rivals, own_price=own.get("unit_price"), pace=pace, news_boost=bool(approach)
        )
        # a tender bidder quoting far above the cheapest rival hears that rival in our first mail: there it moves most
        # (team P05, 9 of 9 sessions over 0.66 cited it to the 36.00 and 38.50 bidders against 31.50: first counters
        # 30.50-31.00 and finals 28.00-29.50; held back, 70210 countered 32.00 and stopped at 30.50)
        far = (
            ts.k == 0
            and len(case.get("suppliers") or []) > 1
            and rivals
            and own.get("unit_price")
            and min(rivals.values()) <= own["unit_price"] * 0.92
        )
        if approach and not rival and not far and not (ts.k >= 1 and ts.plan.stalled()):
            # rival prices and our alternative count once per supplier: hold them until it starts holding
            allc = [c for c in allc if c.kind not in ("offer", "quote")]
        if approach and approach.name == LEVERAGE and not approach.active("paced_facts"):
            allc = [c for c in allc if c.kind in ("offer", "quote")]  # the fact tactic got no movement: dropped
        if ts.plan.strategy_evidence.get("trusted"):  # no risky arguments to a partner: kinds that never moved a
            allc = [c for c in allc if c.kind not in RISKY_KINDS]  # supplier and only read as nagging
        if final:
            allc = []  # no new arguments after its last word: we only take what it offered
        if far:
            # the far bidder's rival quote takes one of the first mail's slots: under a pace cap of 2 two index facts
            # outweighed it (live P05, 4 of 4: 70210 countered 31.50-32.00 and stopped at 29.00-31.00; with the quote,
            # 3 of 3: "a much lower rival quote. I give half the step now", then the rest: 36.00 -> 30.00 -> 27.50)
            lead = [c for c in allc if c.kind in ("offer", "quote")][:1]
            allc = lead + [c for c in allc if c not in lead]
        cap = flags.claims_per_message()
        if approach and approach.name == QUICK_CLOSE:
            cap = min(cap, 2 if ts.k == 0 else 1)  # one MESO pair, nothing else: few arguments
        if pace:  # 2 in the first message with a sheet, then 1 fresh one; a message without a sheet uses none
            cap = min(cap, 2 if ts.k == 0 else 1)
            claims = ration(allc, self.used_ids, ts.k, ts.used_kinds, cap, 0.1) if sheets else []
        else:
            claims = ration(allc, self.used_ids, ts.sent, ts.used_kinds)[:cap]
        trades = []
        if trade and approach and sheets and not final and not hold and ts.asks:
            trades = ts.plan.trade_sheets(ts.asks, sheets[0], case["buyer"].get("need_per_year"))
            trades = [(a, t) for a, t in trades if t not in sheets][:2]
            if trades:
                sheets = sheets[: max(1, 3 - len(trades))] + [t for _a, t in trades]
                self.log.append((sid, "trade", [(a, t.get("unit_price")) for a, t in trades]))
        answered, lever_lines = self.answers(ts, sheets, threads[sid]), self.levers(ts, sheets)
        if trade and approach and sheets and not final and not hold:  # what it asked for and already has: say so
            done = [a for a in ts.asks if a not in {x for x, _t in trades} and granted(a, sheets[0], case)]
            lever_lines = self.granted_lines(done, sheets[0]) + lever_lines
        lever_lines = self.trade_lines(trades) + lever_lines
        if (
            approach
            and ts.plan.allows("main_source")
            and len(case.get("suppliers") or []) > 1
            and sheets
            and ts.k >= 1
            and not ts.said_main_source
            and not final
        ):
            ts.said_main_source = True  # Leverage in a tender: moving the rivals' volume is a text trade
            need = case["buyer"].get("need_per_year")
            lever_lines.append(f"The best package becomes our main source for the full annual volume of {need:,}.")
        if any("binding volume" in a for a in answered):  # one volume sentence a message (arena replays: it was twice)
            lever_lines = [x for x in lever_lines if "binding volume" not in x and "annual volume" not in x]
        plan = Plan(
            case=case,
            dossier=d,
            round_no=ts.sent,
            sheets=sheets,
            claims=claims,
            questions=self.questions(ts),
            levers=answered
            + lever_lines
            + (["Thank you, we are reviewing all offers and will confirm shortly."] if hold else []),
            tone=self.tone(ts, threads[sid]),
            last_reply=latest(threads[sid]).get("body", ""),
        )
        files = self.attachments(ts)  # what its last e-mail asked for, by keyword
        if sheets:  # the strategist's options: priced by code, inside our limits
            # after a final offer exactly what we take, no other package (the strategist could pick changed terms)
            only = final or exact or not flags.on("menu")  # exact: these sheets and no other package
            plan.menu = [(t, 0.0) for t in sheets] if only else ts.plan.menu(sheets, ts.k)
            plan.max_claims = cap
            fresh = [c for c in allc if not any(i in self.used_ids for i in c.ids) and kind_key(c) not in ts.used_kinds]
            if pace and ts.k:  # the same floor as `ration`, or the strategist re-picks a weak claim
                fresh = [c for c in fresh if c.weight >= 0.1]
            plan.menu_claims = fresh[:8]
            # it reads what the supplier asked for itself: every answer and every data file we could give
            more = [a for a in self.answers(ts, sheets, threads[sid], asked=False) if a not in answered]
            plan.points = plan.tone + plan.levers + more + plan.questions
            plan.files = self.attachments(ts, asked=False)
            plan.thread = threads[sid]
            plan.used_kinds = {k[0] if k[0] != "idx" else f"idx {k[1]}" for k in ts.used_kinds}
            plan.last_kinds = sorted({c.kind for c in ts.last_claims})
        ts.k += 1 if sheets else 0
        ts.sent += 1
        # the body is written by `write()` outside the case lock, so a slow model never holds up other threads
        msg = {"subject": plan.subject, "body": None, "_plan": plan, "_sid": sid, "term_sheets": [], "refs": []}
        if files:
            msg["attachments"] = files
            plan.levers.append("Attached: " + " and ".join(f["name"] for f in files) + " with the data you asked for.")
        self._finalize(msg, plan)  # the default choice is in place at once; `write` may replace it
        return msg

    def _finalize(self, msg, plan):
        """Commit the sheets and arguments that go out: our concession path, the IDs used, the kinds heard."""
        ts, sheets, claims = self.t[msg["_sid"]], plan.sheets, plan.claims
        for c in ts.last_claims if msg.get("_committed") else []:  # undo the default choice's claims
            self.used_ids.difference_update(i for i in c.ids if not i.startswith(("offer:", "quote:")))
            ts.used_kinds.discard(kind_key(c))
            if c.sentence in ts.sent_claims:
                ts.sent_claims.remove(c.sentence)
        ts.plan.commit(sheets)
        ts.composed = [dict(t) for t in sheets or []]
        if sheets:
            ts.last_sent = [dict(t) for t in sheets]
        ts.last_claims = list(claims)
        for c in claims:
            self.used_ids.update(i for i in c.ids if not i.startswith(("offer:", "quote:")))
            ts.used_kinds.add(kind_key(c))
            ts.sent_claims.append(c.sentence)
        msg["term_sheets"] = [{"terms": t} for t in sheets]
        msg["refs"] = list(dict.fromkeys(i for c in claims for i in c.ids))[:20]  # strongest first: 3 are checked
        if not msg.get("_committed"):
            self.log.append((msg["_sid"], "send", [t["unit_price"] for t in sheets]))
        msg["_committed"] = True

    def write(self, msg, budget=None):
        """Fill in the e-mail body of a message from `decide` (call it without holding the case lock).
        `budget`: seconds left for writing; too little and the template writes it at once."""
        plan = msg.pop("_plan", None)
        default = (list(plan.sheets), list(plan.claims)) if plan is not None else None
        if plan is not None and msg.get("body") is None:
            if budget is not None and budget < 8:
                msg["body"] = TemplateWriter().write(plan)
            else:
                msg["body"] = self._write_capped(msg["_sid"], plan, HARD_WRITE_S if budget is None else min(HARD_WRITE_S, budget))
            if (list(plan.sheets), list(plan.claims)) != default:  # the strategist chose differently
                self._finalize(msg, plan)
                self.log.append((msg["_sid"], "strategist", [t["unit_price"] for t in plan.sheets]))
            if plan.read:
                self.log.append((msg["_sid"], "read", plan.read))
            self._take_reading(msg, plan)
        msg.pop("_sid", None)
        msg.pop("_committed", None)
        for f in msg.get("attachments") or []:
            f.pop("about", None)
        return msg

    def _write_capped(self, sid, plan, cap):
        """The writer's e-mail, or the template's when it takes longer than `cap` seconds or fails: a slow model
        (the strategist's call and then the plain writer's, 25 s each) must never cost the 60 s."""
        work, out = copy.copy(plan), {}

        def run():
            try:
                try:
                    out["body"] = self.writer.write(work, timeout=cap)
                except TypeError:  # a writer without a timeout parameter
                    out["body"] = self.writer.write(work)
            except Exception as e:  # noqa: BLE001 - the template writes it
                out["err"] = e

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(cap)
        if not out.get("body"):
            self.log.append((sid, "write_fallback", cap))
            log.warning("writer gave nothing in %.0fs (%s): template", cap, out.get("err"))
            return TemplateWriter().write(plan)
        plan.__dict__.update(work.__dict__)  # the strategist's choices (sheets, claims, reading) come back
        return out["body"]

    def _take_reading(self, msg, plan):
        """What the strategist decided beyond sheets and claims: the files to attach, and its reading of the
        supplier's last e-mail - applied at the next `ingest`, under the case lock (a second step coming
        counts at once: another bidder's thread may be deciding whether to wait for it)."""
        ts = self.t[msg["_sid"]]
        if plan.attach is not None:
            files = [f for f in plan.files if f["name"] in plan.attach]
            msg.pop("attachments", None)
            if files:
                msg["attachments"] = files
        for f in msg.get("attachments") or []:
            ts.sent_schedule |= f["name"] == "volume_schedule.csv"
            ts.sent_drivers |= f["name"] == "cost_drivers.csv"
        if plan.signals:
            ts.signals = plan.signals
            ts.plan.second_step = ts.plan.second_step or plan.signals["second_step"]
            self.log.append((msg["_sid"], "reply_read", plan.signals))

    def keep_alive(self, threads, sid):
        """A short holding note to a supplier nobody answers for now, before its 60 s run out."""
        ts = self.t[sid]
        if ts.dead or latest(threads[sid]).get("status") in ENDED:
            return None
        if round_cap(self.case) - ts.sent <= 1:
            return None  # the last round is for a decision, not for a note (`close_now` decides)
        self.log.append((sid, "keep_alive", None))
        return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=[], hold=True)}

    def reoffer(self, threads, sid):
        """Its standing sheet was refused as an accept (gone or changed): send its own terms back as our
        sheet, so it agrees and we can accept the new sheet."""
        _, terms = standing(threads[sid], self._price(sid))
        if not terms.get("unit_price") or self.t[sid].dead:
            return None
        self.log.append((sid, "reoffer", terms.get("unit_price")))
        return {"supplier_id": sid, "message": self.compose(threads, sid, sheets=[dict(terms)])}

    def safe_fallback(self, threads, sid):
        """After an internal error. Not a reason to sign: while time and rounds remain, one short holding note
        per supplier reply keeps the thread alive. When the case is about to end, the best live sheet across
        all bidders that keeps the must-haves and beats the alternative (as `settle`), else the alternative."""
        try:
            ts = self.t[sid]
            time_left = (self.deadline - time.time()) if self.deadline else math.inf
            rounds_left = round_cap(self.case) - ts.sent
            if time_left > self.close_margin and rounds_left > 1:
                if ts.in_flight or ts.fallback_seen == ts.seen or latest(threads[sid]).get("status") in ENDED:
                    return None
                ts.fallback_seen, ts.sent = ts.seen, ts.sent + 1
                body = (
                    "Thank you for your message. We are reviewing it together with the other information we have "
                    "and will come back to you shortly.\n\nBest regards,\nHollerau Pumpenwerke GmbH, Purchasing"
                )
                return {
                    "supplier_id": sid,
                    "message": {"subject": "Re: our negotiation", "body": body, "term_sheets": []},
                }
            return self.close_now(threads, sid)
        except Exception:  # noqa: BLE001 - nothing more to do safely
            log.exception("safe fallback failed")
        return None

    def close_now(self, threads, sid):
        """No message or no time left for this thread: close the best standing sheet across all live bidders that keeps
        the must-haves and beats the alternative, else walk to the alternative, else None (never a timeout by choice)."""
        best = None
        for s, x in self.t.items():
            if x.dead or latest(threads[s]).get("status") in ENDED:
                continue
            sheet_id, terms = standing(threads[s])
            if sheet_id and terms.get("unit_price") and must_haves_ok(self.case, terms, x.est.standard):
                c = cost(self.case, terms, x.est, rel=False)
                best = min(best or (c, s, sheet_id), (c, s, sheet_id))
        c_alt = min((x.plan.c_alt for x in self.t.values()), default=math.inf)
        if best and best[0] <= c_alt:
            self.log.append((best[1], "close_now", round(best[0])))
            return {"supplier_id": best[1], "message": {"action": {"type": "accept", "term_sheet_id": best[2]}}}
        if math.isfinite(c_alt):
            self.log.append((sid, "close_now", "walk"))
            return {"supplier_id": sid, "message": {"action": {"type": "walk_away"}}}
        return None

    def tone(self, ts, events):
        d, first = ts.d, ts.sent == 0
        out = []
        if first:
            if d.own_letter:
                out.append("Thank you for your reply to our letter.")
            else:
                trig = "letter" if (d.letter and d.letter.get("type") == "letter") else "quote"
                out.append(f"Thank you for your {trig}.")
            if d.has("reciprocity"):
                out.append(
                    "We have not forgotten that you held your prices for a quarter in 2022, and we want an outcome that is fair to both sides."
                )
            elif d.has("reference_customer"):
                out.append("We value our cooperation and want to continue it on a sound basis.")
            if d.late_days > 5 or d.has("late_payment_grievance"):
                out.append("We will pay every invoice on the agreed due date.")
        else:
            out.append("Thank you for your reply.")
            if d.has("cash_short", strong=True) or d.has("late_payment_grievance"):
                out.append("We will pay every invoice on the agreed due date.")
        return out

    @staticmethod
    def trade_lines(trades):
        """One sentence per priced trade (BUYER_STRAT): what it asked for and what it costs."""
        out = []
        for ask, t in trades:
            if ask == "payment":
                out.append(f"On faster payment: we can pay within {t['payment_days']} days at €{t['unit_price']:.2f}.")
            elif ask == "volume" and t.get("volume_per_year"):
                out.append(f"On volume: we can make {t['volume_per_year']:,} a year binding at €{t['unit_price']:.2f}.")
            elif ask == "contract" and t.get("contract_months"):
                out.append(f"On the term: we can sign for {t['contract_months']} months at €{t['unit_price']:.2f}.")
        return out

    @staticmethod
    def granted_lines(asks, t):
        """One sentence per ask our first sheet already meets (BUYER_STRAT): it is priced in."""
        out = []
        for ask in asks:
            if ask == "payment":
                out.append(f"On faster payment: our proposal already pays within {t['payment_days']} days, "
                           f"priced into €{t['unit_price']:.2f}.")  # fmt: skip
            elif ask == "volume":
                out.append(f"On volume: our proposal already makes {t['volume_per_year']:,} a year binding.")
            elif ask == "contract":
                out.append(f"On the term: our proposal already runs {t['contract_months']} months.")
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
            lead, std = s.get("lead_time_days"), ts.est.standard.get("lead_time_days")
            if lead and std and lead >= std and lead > self.case["buyer"]["need_lead_time_days"]:
                # a lead time at its own standard is what it wants to hear (arena P10: "this extended lead time
                # allows us to confirm the required monthly quantities"); we pay late cost for it, it costs it nothing
                out.append(f"We can work with a lead time of {lead} days, which gives you room to plan your capacity.")
        elif (d.has("free_capacity") or d.has("small_account")) and months and months >= 24:
            out.append(f"We are ready to place the full annual volume of {need:,} with you for {months} months.")
        if d.has("price_frozen") and d.current and s.get("unit_price", 0) >= d.current:
            out.append(
                "We understand that your list price is fixed; our proposals therefore work through the other terms."
            )
        fee = d.complaint_fee
        if fee and "chargeback_eur_per_complaint" in s and (s["chargeback_eur_per_complaint"] or 0) < fee:
            # every framework agreement already gives us a flat fee per complaint (§ 6.1): a sheet without it
            # gives up a right we hold - say so (the supplier's quote lists 0 as its standard)
            out.append(
                f"Our framework agreement already provides a flat fee of EUR {fee:.0f} per supplier-caused "
                "complaint; we keep the price low in return for not charging it."
            )
        if self.case["buyer"].get("category_goal") in ("stabilise", "secure_supply") and d.has("below_full_cost"):
            out.append("We want a price that keeps this part healthy for both of us.")
        if d.has("approval_ladder") and ts.sent >= 1:
            out.append(
                "We understand that larger moves go to your pricing committee: please submit this package; "
                "we hold our offer until its decision."
            )
        if ts.sent >= 2:
            out.append("If you confirm this package, we accept it in our next message.")
        return out

    def attachments(self, ts, asked=True):
        """Arena: suppliers asked 11 times for data, schedules or calculations and got none. Attach true
        data (our monthly forecast, the part's cost drivers) once, when it is asked for. `asked=False`: every
        file not sent yet, for the strategist to choose from (it reads the request itself)."""
        import base64

        body, out = (ts.last_body or "").lower(), []
        asks_volume = not asked or bool(ASKS_VOLUME.search(body))
        asks_data = not asked or bool(ASKS_DATA.search(body))
        if asks_volume and not ts.sent_schedule and (csv := ts.d.schedule_csv()):
            out.append(
                {
                    "name": "volume_schedule.csv",
                    "type": "csv",
                    "content_b64": base64.b64encode(csv.encode()).decode(),
                    "about": "our monthly demand forecast per part (call-off schedule)",
                }
            )
        if asks_data and not ts.sent_drivers and (csv := ts.d.drivers_csv()):
            out.append(
                {
                    "name": "cost_drivers.csv",
                    "type": "csv",
                    "content_b64": base64.b64encode(csv.encode()).decode(),
                    "about": "the part's cost-driver indices with their latest values and 12-month changes",
                }
            )
        return out

    def answers(self, ts, sheets, events, asked=True):
        """Answer what the supplier asked in its last e-mail (arena: "which volume can you commit to?",
        "which figures is that based on? please send us the data"). `asked=False`: every answer we could
        give, for the strategist to choose from."""
        body, out = (ts.last_body or "").lower(), []
        need = self.case["buyer"]["need_per_year"]
        s = sheets[0] if sheets else {}
        months = s.get("contract_months") or (self.case.get("fixed_terms") or {}).get("contract_months")
        vol = s.get("volume_per_year") or (self.case.get("fixed_terms") or {}).get("volume_per_year") or need
        if not asked or ("volume" in body and ("commit" in body or "which" in body or "release" in body)):
            out.append(
                f"To your question: we commit to a binding volume of {vol:,} a year"
                + (f" for {months} months." if months else ".")
            )
        asks_data = not asked or re.search(r"which figures|send us the data|which data|what data|on which basis", body)
        if asks_data and ts.sent and ts.sent_claims:  # not "we trust you understand these figures"
            # the IDs were checked once already: citing them again only takes an argument slot ("duplicate")
            out.append("To your question on our figures: the data references are in our earlier message.")
        return out

    def questions(self, ts):
        out, d = [], ts.d
        ctype = self.case["type"]
        wants_cost = d.has("open_book")  # arena: 0 of 7 non-open-book suppliers sent a breakdown
        if wants_cost and not ts.asked_breakdown and not ts.has_breakdown:
            out.append("Could you send us your cost breakdown for this part?")
            ts.asked_breakdown = True
        if ts.sent == 0 and ctype == "capacity":
            out.append("Which monthly quantities can you confirm for the next twelve months?")
        if ts.sent == 0 and ctype == "quality":
            out.append("Please send us your 8D report with the corrective actions and their dates.")
        return out
