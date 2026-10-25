"""The 25 Oct review, without the data package: a stand-in dossier drives the planner, the agent and the runner.

Clause choice and cost, the payment term the supplier's cost of money favours, the stop at the sustainable margin,
the argument table and its replay tally, the RFQ leapfrog, and the error paths (none may sign, walk or go silent)."""

import copy
import json
import math
import threading
from pathlib import Path

import pytest

from arena_kit.client import ArenaError
from arena_kit.cost import total_cost
from buyer import argtable, bench
from buyer import runner as runner_mod
from buyer.agent import HOLD_BODY, BuyerAgent, ThreadState
from buyer.claims import Claim
from buyer.strategy import CLAUSE_CHEAP, Estimate, Planner, clause_share, cost
from buyer.writer import Plan, StrategistWriter


def case(cid):
    return json.loads(Path(f"cases/real/{cid}.json").read_text())


class FakeDossier:
    """What the planner and the agent read from a dossier, set by hand."""

    def __init__(
        self,
        case,
        sid,
        *,
        full_cost=None,
        err=0.15,
        cash_rate=0.07,
        min_margin=0.04,
        traits=None,
        drivers=(),
        standard=None,
    ):
        b = case["buyer"]
        self.case, self.sid = case, sid
        self.current = b.get("current_unit_price")
        self.reference_price = self.current or 100.0
        self.full_cost = (full_cost or round(self.reference_price * 0.9, 2), err)
        self.cash_rate, self.min_margin = cash_rate, min_margin
        self.traits = dict(traits or {})
        self.drivers = list(drivers)
        self.standard_guess = standard or {
            "payment_days": 30,
            "incoterm": "DAP",
            "lead_time_days": 42,
            "contract_months": 12,
        }
        self.letter = self.notice = self.scorecard = self.hedge_until = self.complaint_fee = None
        self.own_letter, self.late_days, self.justified_pct = None, 0, 0.0
        self.wants, self.dead_kinds, self.texts, self.sup = [], set(), "", {"name": "Test"}
        self.not_strategic = False

    def has(self, trait, strong=False):
        v = self.traits.get(trait)
        return v == "strong" if strong else bool(v)

    def hedge_covers(self, series):
        return series.startswith("AS-")

    def add_text(self, text):
        return False

    def note_traits(self, traits):
        return False

    def schedule_csv(self):
        return None

    def drivers_csv(self):
        return None

    def brief(self):
        return "A test supplier."


def planner(c, **kw):
    sid = c["suppliers"][0]
    est = Estimate(FakeDossier(c, sid, **kw))
    return Planner(c, est)


# -- clauses ------------------------------------------------------------------------------------------------------
def test_preferred_clause_counts_the_index_risk_whatever_the_order():
    c = case("P14")
    a = {"series": "CU-NOTATION", "material_share": 0.45, "deadband_pct": 3, "base_month": "2026-08"}
    b = {**a, "material_share": 0.8}
    c["buyer"]["index_paths"] = {"CU-NOTATION": 0.02}  # inside the deadband: only the risk differs
    c["buyer"]["index_risk_pct"] = 1
    for opts in ([None, a, b], [None, b, a]):
        c["term_options"]["index_clause"] = opts
        assert planner(c).pref_clause == a  # 0.45 % against 0.8 %
    assert clause_share(c, a) == pytest.approx(0.0045) and clause_share(c, b) == pytest.approx(0.008)


def test_a_dear_optional_clause_is_not_offered_unprompted():
    p14 = planner(case("P14"), drivers=[("CU-NOTATION", 0.55, -2.0, "i0", "i1", "m0", "m1")])
    assert clause_share(p14.case, p14.pref_clause) > CLAUSE_CHEAP  # 2.2 %
    assert not p14._clause_trial()
    assert not any(t.get("index_clause") for t in p14.sheets(0))
    p02 = planner(case("P02"), drivers=[("CU-NOTATION", 0.2, -2.0, "i0", "i1", "m0", "m1")])
    assert clause_share(p02.case, p02.pref_clause) < CLAUSE_CHEAP  # 0.8 %: the trial it took at once
    assert p02._clause_trial()


def test_raw_material_offers_the_clause_with_the_long_term_from_the_first_message():
    p = planner(case("P08"), traits={"wants_clause": "strong"})
    best = p.sheets(0)[0]
    assert best["index_clause"] and best["contract_months"] >= 24


# -- the cost-of-money gap ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("rate", "days"), [(0.15, 14), (0.25, 14), (0.05, 90), (0.07, 90)])
def test_the_menu_carries_the_payment_term_its_cost_of_money_favours(rate, days):
    for cid in ("P04", "P01", "P19"):
        p = planner(case(cid), cash_rate=rate, err=0.04)
        assert p.money_days() == days
        p.observe_reply([{"unit_price": round(p.est.d.reference_price * 1.05, 2)}], "open")
        sheets = p.sheets(1)
        assert any(t.get("payment_days") == days for t in sheets), (cid, sheets)
        assert any(t.get("payment_days") == days for t, _dc in p.menu(sheets, 1))


def test_the_payment_sheet_costs_us_what_the_best_does():
    p = planner(case("P04"), cash_rate=0.25, err=0.04)
    p.observe_reply([{"unit_price": round(p.est.d.reference_price * 1.05, 2), "payment_days": 30}], "open")
    best = p.sheets(1)[0]
    t = p._money_sheet(best, cost(p.case, best, p.est), 90 if best.get("payment_days") == 14 else 14)
    assert t is not None and cost(p.case, t, p.est) == pytest.approx(cost(p.case, best, p.est), rel=2e-3)


def test_no_payment_sheet_when_the_rates_are_close():
    assert planner(case("P04"), cash_rate=0.08).money_days() is None


# -- relationship cases on a rough estimate ---------------------------------------------------------------------------
def test_no_offer_below_the_sustainable_margin_on_a_rough_estimate():
    c = case("P06")
    sid = c["suppliers"][0]
    est = Estimate(FakeDossier(c, sid, full_cost=230.0, err=0.15))
    p = Planner(c, est)
    p.observe_reply([{"unit_price": 262.0, "payment_days": 30, "lead_time_days": 56, "tooling_eur": 0}], "open")
    stop = p.stop_value()
    assert stop == pytest.approx(230.0 / 0.97)
    for k in range(4):
        for t in p.sheets(k):
            assert est.cash_value(c, t) >= stop * 0.999, t
        p.commit(p.sheets(k))
    sure = Planner(c, Estimate(FakeDossier(c, sid, full_cost=230.0, err=0.04)))
    assert sure.stop_value() is None  # a confident estimate: the expected charge in C places the price


def test_the_stop_never_offers_more_than_its_ask():
    c = case("P06")
    p = Planner(c, Estimate(FakeDossier(c, c["suppliers"][0], full_cost=250.0, err=0.15)))
    p.observe_reply([{"unit_price": 252.0, "payment_days": 30, "lead_time_days": 56, "tooling_eur": 0}], "open")
    for t in p.sheets(0):
        assert p.est.value(c, t) <= p.ask_value * 1.0001


# -- the argument table -------------------------------------------------------------------------------------------
def test_table_weight_blends_the_prior_with_the_replays():
    none = dict.fromkeys(argtable.OUTCOMES, 0)
    assert argtable.weight("fc", "any", {}) == 0.8  # no logs: the old rule
    t = {
        ("sc_otd", "any"): {**none, "no_effect": 6},
        ("sc_otd", "stale_costing"): {**none, "fair": 1},
        ("fc", "not_strategic"): {**none, "no_effect": 4},
        ("idx", "hedged_other"): {**none, "counted": 4},
    }
    assert argtable.weight("sc_otd", "any", t) < 0.15  # never sent: 0 of 6
    assert argtable.weight("sc_otd", "stale_costing", t) > 0.8  # a fair point counts more
    assert argtable.weight("fc", "not_strategic", t) < 0.15
    assert argtable.weight("idx", "hedged_other", t) == pytest.approx((2 * 0.54 + 4 * 0.9) / 6, abs=1e-3)
    dup = {("fc", "any"): {**none, "duplicate": 5, "rejected": 2}}
    assert argtable.weight("fc", "any", dup) == 0.8  # repeats and bad IDs say nothing about the persona


def test_the_committed_table_reads_and_keeps_the_rules_where_it_has_no_logs():
    rows = argtable.counts()
    assert rows[("sc_otd", "any")]["no_effect"] == 6
    assert rows[("po", "below_standing")]["counted"] == 1  # a manual entry
    assert argtable.weight("idx", "stale_costing") == 1.0 and argtable.weight("offer", "rfq") == 0.95


def test_persona_rows_follow_the_old_rules_order():
    c = case("P08")
    d = FakeDossier(c, c["suppliers"][0], traits={"hedged_material": "strong", "cash_short": "strong"})
    assert argtable.persona("idx", d, c, series="AS-14404") == "hedge_covered"
    assert argtable.persona("idx", d, c, series="PPI-C2431") == "hedged_other"
    d.traits = {"cash_short": "strong", "stale_costing": "strong"}
    assert argtable.persona("idx", d, c, series="PPI-C2431") == "cash_short"
    d.traits = {"capacity_tight": "strong", "stale_costing": "strong"}
    assert argtable.persona("fc", d, c) == "capacity_tight"
    d.traits, d.not_strategic = {}, True
    assert argtable.persona("fc", d, c) == "not_strategic"
    assert argtable.persona("sc", d, case("P11")) == "quality_case"


def record():
    """A saved session in the arena's format: two buyer e-mails with arguments, the supplier's checks."""
    body1 = (
        "Dear team, Thank you for your quote. Labour costs in German manufacturing changed -0.3% from 2025-10 to "
        "2026-03 (idx:LCI-C@2025-10, idx:LCI-C@2026-03). Our demand forecast for this part has risen +1.9% "
        "(fc:370010@2026-09). Your on-time delivery was 88.0% against our 95% target (sc:70710@2026Q1, "
        "sc:70710@2026Q2). Our proposal: €5.10 per unit."
    )
    body2 = (
        "Thank you. We pay you €5.45 per unit today (po:1, po:2). "
        "Another supplier quoted €4.80 for the same part (quote:9)."
    )
    return {
        "state": {"case_id": "case:P08"},
        "threads": {
            "sup:70710": {
                "messages": [
                    {
                        "from": "sales",
                        "status": "open",
                        "body": "Our quote.",
                        "term_sheets": [{"terms": {"unit_price": 5.69}}],
                    },
                    {
                        "from": "buyer",
                        "body": body1,
                        "refs": [
                            "idx:LCI-C@2025-10",
                            "idx:LCI-C@2026-03",
                            "fc:370010@2026-09",
                            "sc:70710@2026Q1",
                            "sc:70710@2026Q2",
                        ],
                    },
                    {
                        "from": "sales",
                        "status": "open",
                        "term_sheets": [{"terms": {"unit_price": 5.62}}],
                        "body": "We checked the delivery record data you cited; it holds, "
                        "but it does not change our calculation.",
                        "claim_results": [
                            {"index": 0, "result": "verified"},
                            {"index": 1, "result": "verified"},
                            {"index": 2, "result": "verified"},
                        ],
                    },
                    {"from": "buyer", "body": body2, "refs": ["po:1", "po:2", "quote:9"]},
                    {
                        "from": "sales",
                        "status": "open",
                        "body": "Noted.",
                        "term_sheets": [{"terms": {"unit_price": 5.6}}],
                        "claim_results": [{"index": 0, "result": "duplicate"}, {"index": 1, "result": "irrelevant"}],
                    },
                ]
            }
        },
    }


def test_replay_tally_counts_each_argument_in_its_row():
    claims = argtable.parse_claims(
        record()["threads"]["sup:70710"]["messages"][1]["body"], record()["threads"]["sup:70710"]["messages"][1]["refs"]
    )
    assert [c.kind for c in claims] == ["idx", "fc", "sc_otd"] and claims[0].series == "LCI-C"
    assert len(claims[0].ids) == 2 and len(claims[2].ids) == 2
    table = argtable.tally([("r1", record())], {"case:P08": case("P08")}, lambda n, r, s, c: "any")
    assert table[("idx", "any")]["counted"] == 1
    assert table[("fc", "any")]["counted"] == 1
    assert table[("sc_otd", "any")]["no_effect"] == 1
    assert table[("po", "below_standing")]["duplicate"] == 1  # €5.45 below its €5.62
    assert table[("quote", "rival_far")]["rejected"] == 1  # €4.80, more than 10 % below


def test_not_our_reality_only_retires_the_hedged_series():
    c = argtable.parse_claims(
        "A changed -5.0% (idx:AS-14404@1, idx:AS-14404@2).", ["idx:AS-14404@1", "idx:AS-14404@2"]
    )[0]
    body = "That is not our reality: our material is covered at fixed prices."
    assert argtable.outcome("verified", body, c, "hedge_covered") == "no_effect"
    assert argtable.outcome("verified", body, c, "hedged_other") == "counted"
    assert argtable.outcome("verified", "We did not know that - a fair point.", c, "any") == "fair"


# -- the strategist keeps the leapfrog citation -------------------------------------------------------------------
def test_the_strategist_cannot_drop_a_new_rival_offer():
    c = case("P05")
    d = FakeDossier(c, c["suppliers"][0])
    others = [
        Claim(
            "idx",
            [f"idx:S{i}@1", f"idx:S{i}@2"],
            -3.0,
            f"S{i} fell (idx:S{i}@1, idx:S{i}@2).",
            0.9 - i / 100,
            meta={"series": f"S{i}"},
        )
        for i in range(3)
    ]
    offer = Claim(
        "offer", ["offer:70250-2"], 30.5, "Another supplier offers €30.50 for the same part (offer:70250-2).", 0.95
    )
    plan = Plan(
        case=c,
        dossier=d,
        round_no=2,
        sheets=[{"unit_price": 29.0}],
        claims=[offer, others[0]],
        questions=[],
        levers=[],
        tone=[],
    )
    plan.menu, plan.menu_claims = [({"unit_price": 29.0}, 0.0)], others + [offer]
    choice = {
        "sheets": ["S1"],
        "claims": ["C1", "C2", "C3"],
        "points": [],
        "email": "Dear team, S0 fell (idx:S0@1, idx:S0@2). S1 fell (idx:S1@1, idx:S1@2). S2 fell (idx:S2@1, "
        "idx:S2@2). We offer €29.00 per unit. Best regards,\nHollerau Pumpenwerke GmbH, Purchasing",
    }
    StrategistWriter(lambda *a, **k: json.dumps(choice)).write(plan)
    assert offer in plan.claims and len(plan.claims) == 3


# -- the RFQ leapfrog -----------------------------------------------------------------------------------------------
def rfq_agent(monkeypatch):
    monkeypatch.setattr("buyer.agent.candidates", lambda *a, **k: [])
    c = case("P05")
    agent = BuyerAgent(world=None)
    agent.case = c
    agent.t = {s: ThreadState(None, c, s, FakeDossier(c, s, full_cost=26.0)) for s in c["suppliers"]}
    return agent, c


def sales(price, status="open", sid="x", n=0):
    return {
        "from": "sales",
        "status": status,
        "body": "Our offer.",
        "term_sheets": [{"term_sheet_id": f"offer:{sid}-{n}", "terms": {"unit_price": price}}],
    }


BUYER = {"from": "buyer", "body": "Our proposal.", "term_sheets": [{"terms": {"unit_price": 27.0}}]}


def test_a_lower_offer_is_shown_to_the_other_bidder_before_we_sign(monkeypatch):
    agent, c = rfq_agent(monkeypatch)
    a, b, z = c["suppliers"]
    threads = {
        a: [sales(36.0, sid=a), BUYER, sales(31.0, sid=a, n=1), BUYER, sales(30.5, "final_offer", sid=a, n=2)],
        b: [sales(34.0, sid=b), BUYER, sales(33.0, sid=b, n=1)],
        z: [sales(40.0, sid=z), BUYER, sales(39.0, "walked_away", sid=z, n=1)],
    }
    for s in (a, b):
        agent.ingest(s, threads[s])
        agent.t[s].k = agent.t[s].sent = 2
    agent.t[z].dead = True
    live = {s: x for s, x in agent.t.items() if not x.dead}
    assert agent.unshown(threads, a, live)  # b has not seen 30.50
    out = agent.decide(threads, a)
    assert not (out and (out["message"].get("action") or {}).get("type") == "accept")  # a hold, not a signature
    agent.offer_prices[f"offer:{a}-2"] = 30.5
    agent.t[b].used_kinds.add(("offer", f"offer:{a}-2"))  # b's next e-mail cited it
    assert not agent.unshown(threads, a, live)
    agent.t[b].sent = c.get("round_limit", 10) - 1  # no round left to answer: nothing to wait for
    agent.t[b].used_kinds.clear()
    assert not agent.unshown(threads, a, live)


# -- error paths ------------------------------------------------------------------------------------------------------
def single_agent(cid="P04", price=4.9):
    c = case(cid)
    sid = c["suppliers"][0]
    agent = BuyerAgent(world=None)
    agent.case = c
    agent.t = {sid: ThreadState(None, c, sid, FakeDossier(c, sid))}
    threads = {
        sid: [
            sales(price, sid=sid),
            {"from": "buyer", "body": "x", "term_sheets": [{"terms": {"unit_price": round(price * 0.9, 2)}}]},
            sales(price, sid=sid, n=1),
        ]
    }
    return agent, c, sid, threads


def test_an_error_gets_a_note_then_a_small_move_never_a_signature():
    agent, c, sid, threads = single_agent()
    first = agent.safe_fallback(threads, sid)
    assert first["message"]["body"] == HOLD_BODY and not first["message"].get("action")
    assert agent.safe_fallback(threads, sid) is None  # one answer per reply
    threads[sid] += [{"from": "buyer", "body": HOLD_BODY, "term_sheets": []}, sales(4.9, sid=sid, n=2)]
    move = agent.safe_fallback(threads, sid)["message"]
    price = move["term_sheets"][0]["terms"]["unit_price"]
    assert 4.41 < price <= round(4.9 * 0.997, 2)  # 1 % up on our 4.41, below its ask


def test_near_the_cap_an_error_takes_the_best_sheet_that_beats_the_alternative():
    agent, c, sid, threads = single_agent()
    agent.deadline = 1.0  # long past: the case is about to end
    out = agent.safe_fallback(threads, sid)
    alt = c["buyer"]["alternative"]["terms"]
    if total_cost(c, {"unit_price": 4.9}) <= total_cost(c, alt):
        assert out["message"]["action"]["type"] == "accept"
    else:
        assert out["message"]["action"]["type"] == "walk_away"


def test_a_failing_holding_note_still_goes_out(monkeypatch):
    agent, c, sid, threads = single_agent()
    monkeypatch.setattr(agent, "compose", lambda *a, **k: 1 / 0)
    out = agent.keep_alive(threads, sid)
    assert out["message"]["body"] == HOLD_BODY


def test_a_failing_writer_sends_the_codes_own_choice():
    agent, c, sid, threads = single_agent()

    class Broken:
        def write(self, plan, timeout=None):
            raise RuntimeError("model down")

    agent.writer = Broken()
    plan = Plan(
        case=c,
        dossier=agent.t[sid].d,
        round_no=1,
        sheets=[{"unit_price": 4.5}],
        claims=[],
        questions=[],
        levers=[],
        tone=["Thank you for your reply."],
    )
    msg = agent.write({"subject": "s", "body": None, "_plan": plan, "_sid": sid, "term_sheets": [], "refs": []}, 30)
    assert "4.50" in msg["body"]


# -- the runner against a flaky arena --------------------------------------------------------------------------------
class FlakyClient:
    def __init__(self, table):
        self.table = table

    def session(self, _sid):
        return {"status": "ended" if self.table.over else "open", "result": self.table.over}

    def thread(self, _sid, sup):
        return {"messages": copy.deepcopy(self.table.thread[sup])}


class FlakyTable:
    """One supplier that concedes 0.20 a round and agrees at 4.60; the arena fails as told by `faults`."""

    def __init__(self, c, faults=()):
        self.case, self.sid = c, c["suppliers"][0]
        self.session_id, self.over, self.client = "s1", None, FlakyClient(self)
        self.faults, self.lock, self.price = list(faults), threading.Lock(), 5.4
        first = sales(5.4, sid=self.sid)
        first["attachments"] = [{"attachment_id": "att:1", "name": "quote.pdf"}]
        self.thread = {self.sid: [first]}
        self.sent, self.closes = [], []

    def open(self, _cid):
        return self.case, copy.deepcopy(self.thread)

    def attachment(self, _aid):
        raise OSError("download failed")

    def result(self):
        return self.over or {"deal": False}

    def send(self, sup, msg):
        fault = self.faults.pop(0) if self.faults else None
        if fault == "502":
            raise ArenaError(502, "bad gateway")
        with self.lock:
            self.sent.append(msg)
            best = max((t["terms"]["unit_price"] for t in msg.get("term_sheets") or []), default=0)
            self.price = round(max(4.6, self.price - 0.2), 2)
            status = "agreed" if best >= self.price else "open"
            n = len(self.thread[sup])
            reply = sales(best if status == "agreed" else self.price, status, sid=sup, n=n)
            self.thread[sup] += [{**msg, "from": "buyer"}, reply]
        if fault == "drop-after":  # it arrived, the answer was lost
            raise ConnectionError("connection reset")
        return reply

    def close(self, sup, msg):
        self.closes.append(msg)
        self.over = {"deal": msg["action"]["type"] == "accept", "score": 0.5}
        return self.over


class StubAgent:
    """Offers 4.70, 4.80, ... and accepts what the supplier agreed to; raises where told."""

    def __init__(self, raise_on=()):
        self.raise_on, self.calls, self.read_timeout = set(raise_on), 0, 1
        self.t, self.log, self.deadline, self.fallbacks = {}, [], None, 0

    def start(self, c, threads):
        self.case = c
        self.t = {s: type("T", (), {"in_flight": False, "dead": False})() for s in threads}

    def decide(self, threads, sid):
        self.calls += 1
        if self.calls in self.raise_on:
            raise RuntimeError("bug")
        last = next(m for m in reversed(threads[sid]) if m.get("from") != "buyer")
        if last.get("status") == "agreed":
            return {
                "supplier_id": sid,
                "message": {"action": {"type": "accept", "term_sheet_id": last["term_sheets"][0]["term_sheet_id"]}},
            }
        mine = sum(m.get("from") == "buyer" for m in threads[sid])
        return {
            "supplier_id": sid,
            "message": {
                "subject": "s",
                "body": "b",
                "term_sheets": [{"terms": {"unit_price": round(4.7 + 0.1 * mine, 2)}}],
            },
        }

    def safe_fallback(self, threads, sid):
        self.fallbacks += 1
        return {"supplier_id": sid, "message": {"subject": "s", "body": HOLD_BODY, "term_sheets": []}}

    def keep_alive(self, threads, sid):
        return None

    def write(self, msg, budget=None):
        return msg

    def note_round(self, s):
        pass

    def invalid_message(self, sid):
        return False

    def reoffer(self, threads, sid):
        return None


@pytest.mark.parametrize("faults", [[], ["502"], ["drop-after"], ["502", "502"]])
def test_the_runner_rides_out_a_flaky_arena(faults, monkeypatch):
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)
    table = FlakyTable(case("P04"), faults)
    agent = StubAgent(raise_on={2})
    res = runner_mod.play(table, "case:P04", lambda deadline: agent)
    assert res.get("deal") is True  # signed only on its agreement, never on an error
    assert [c["action"]["type"] for c in table.closes] == ["accept"]
    assert agent.fallbacks == 1
    assert len({id(m) for m in table.sent}) == len(table.sent)  # a message that arrived is never sent again
    assert len(table.sent) == 3  # an offer, the note after the error, the offer it agreed to


# -- the overfitting gate ---------------------------------------------------------------------------------------------
def test_a_change_that_helps_the_hosted_cases_but_hurts_the_others_is_rejected():
    hosted = {"case:P01", "case:P02"}
    before = {
        "0": {"case:P01": 0.8, "case:P02": 0.6, "case:P06": 0.5, "case:P09": 0.5},
        "1": {"case:P01": 0.8, "case:P02": 0.6, "case:P06": 0.4, "case:P09": 0.6},
    }
    worse = {s: {c: v + (0.05 if c in hosted else -0.03) for c, v in r.items()} for s, r in before.items()}
    keep, lines = bench.verdict(before, worse, hosted)
    assert not keep and "helps the hosted cases, hurts the others" in lines[-1]
    better = {s: {c: v + 0.01 for c, v in r.items()} for s, r in before.items()}
    assert bench.verdict(before, better, hosted)[0]
    assert bench.scores_of({"seeds": {"0": {"buyer": {"case:P01": {"score": 0.7}}}}}, "buyer") == {
        "0": {"case:P01": 0.7}
    }
    assert math.isclose(bench.split({"case:P01": 1.0, "case:P06": 0.0}, hosted)["hosted"], 1.0)
