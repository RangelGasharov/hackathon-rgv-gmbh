"""Where the model reads instead of a regex: the persona read at case start, the strategist's reading of
each reply and its choice of attachments - with fake models, no network. Plus the decision guards found on
the twin and in the real sessions (signing past an in-flight rival, a deal dearer than the alternative)."""

import itertools
import json
import re
import time
from pathlib import Path

import pytest

from buyer.agent import BuyerAgent
from buyer.claims import Claim, candidates
from buyer.dossier import Dossier, World
from buyer.persona import parse, read_all
from buyer.writer import Plan, StrategistWriter


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def case(cid):
    return json.loads(Path(f"cases/real/{cid}.json").read_text())


def reading(**traits):
    return json.dumps({"traits": traits, "unsure": [], "wants": [], "hedge_until": None, "notes": "x"})


def msg(price, status="open", body="Our offer.", sid="q", **terms):
    return {
        "from": "sales",
        "status": status,
        "body": body,
        "term_sheets": [{"term_sheet_id": f"{sid}-{price}", "terms": {"unit_price": price, **terms}}],
    }


BUYER = {"from": "buyer", "body": "Our proposal.", "term_sheets": []}


# -- the persona read -------------------------------------------------------------------------------------
def test_read_merges_over_the_regex_traits(world):
    d = Dossier(world, case("P01"), "sup:70110")
    assert d.traits["reciprocity"] == "strong" and d.traits["stale_costing"] == "strong"
    rate = d.cash_rate
    read = parse(
        json.dumps(
            {
                "traits": {"reciprocity": "none", "stale_costing": "none", "cash_short": "strong", "bogus": "strong"},
                "unsure": ["stale_costing"],
                "wants": ["faster_payment", "a pony"],
                "hedge_until": "2027-13",
                "notes": "Family foundry.",
            }
        )
    )
    assert "bogus" not in read["traits"] and read["wants"] == ["faster_payment"] and read["hedge_until"] is None
    d.apply_read(read)
    assert "reciprocity" not in d.traits  # sure: the model decides, also against a regex hit
    assert d.traits["stale_costing"] == "strong"  # unsure: the regex stays
    assert d.traits["cash_short"] == "strong" and d.cash_rate > rate  # derived figures follow
    d.add_text("Thank you for your reply.")  # a later e-mail re-reads the regex, the read stays merged
    assert "reciprocity" not in d.traits and d.traits["cash_short"] == "strong"


def test_read_sets_the_hedge_end_and_the_clause_wish(world):
    d = Dossier(world, case("P08"), "sup:70710")
    d.apply_read(parse(json.dumps({"traits": {"hedged_material": "strong"}, "wants": ["index_clause"],
                                   "hedge_until": "2027-05", "notes": ""})))  # fmt: skip
    assert d.hedge_until.year == 2027 and d.hedge_until.month == 5
    assert "index_clause" in d.wants


def test_reads_run_in_parallel_and_a_late_one_is_dropped(world):
    c = case("P05")
    ds = [(s, Dossier(world, c, s), "") for s in c["suppliers"]]

    def complete(system, user, max_tokens=None, timeout=None):
        if "sup:70240" in user:
            time.sleep(3)  # late
        time.sleep(0.3)
        return reading(cash_short="strong")

    t0 = time.monotonic()
    out = read_all(complete, ds, timeout=1.0)
    assert time.monotonic() - t0 < 2.0
    assert set(out) == {"sup:70210", "sup:70250"}


def test_a_failing_read_leaves_the_regex_traits(world):
    c = case("P01")

    class Writer(StrategistWriter):
        pass

    def complete(system, user, max_tokens=None, timeout=None):
        raise RuntimeError("budget")

    agent = BuyerAgent(world, writer=Writer(complete))
    agent.start(c, {"sup:70110": [msg(138.55)]})
    assert agent.t["sup:70110"].d.traits == Dossier(world, c, "sup:70110").traits


def test_start_applies_the_read_before_the_packages_are_priced(world):
    c = case("P02")
    seen = []

    def complete(system, user, max_tokens=None, timeout=None):
        seen.append(user)
        return json.dumps({"traits": {"cash_short": "none"}, "wants": ["index_clause"], "notes": "Group."})

    agent = BuyerAgent(world, writer=StrategistWriter(complete))
    opening = msg(370.96, body="CU-NOTATION rose 47.7 % in twelve months and is 20 % of our cost.")
    agent.start(c, {"sup:70510": [opening]})
    assert "CU-NOTATION rose" in seen[0]  # the opening e-mail is part of the read
    assert agent.t["sup:70510"].est.wants_clause
    assert any(p.combo.get("index_clause") for p in agent.t["sup:70510"].plan.packages)  # priced, ranked by C


def test_template_writer_makes_no_model_call(world):
    agent = BuyerAgent(world)  # the benchmark's writer: no `complete`, regex traits only
    agent.start(case("P01"), {"sup:70110": [msg(138.55)]})
    assert agent.t["sup:70110"].d.read is None


# -- the strategist's reading of a reply ------------------------------------------------------------------
def test_signals_are_checked_against_what_we_sent(world):
    c = case("P08")
    plan = Plan(case=c, dossier=Dossier(world, c, "sup:70710"), round_no=1, sheets=[], claims=[], questions=[],
                levers=[], tone=[])  # fmt: skip
    plan.last_kinds = ["fc", "idx"]
    s = StrategistWriter.signals(
        plan,
        {
            "counted": ["fc", "notice"],
            "dead": ["idx"],
            "second_step": "yes",
            "keep_terms": ["index_clause", "unit_price", "colour"],
            "traits": {"capacity_tight": "strong", "hedged_material": "sure"},
            "asks_for": ["binding volume"],
        },
    )
    assert s["counted"] == ["fc"] and s["dead"] == ["idx"] and s["second_step"] is False
    assert s["keep_terms"] == ["index_clause"] and s["traits"] == {"capacity_tight": "strong"}


def test_signals_apply_at_the_next_ingest(world):
    c = case("P08")
    agent = BuyerAgent(world)
    agent.start(c)
    sid, ts = "sup:70710", agent.t["sup:70710"]
    clause = c["term_options"]["index_clause"][3]
    events = [msg(6.05), BUYER, msg(5.7, body="We checked the volume change data you cited; it holds, but it does not change our calculation.",
                                    index_clause=clause, payment_days=30, contract_months=24)]  # fmt: skip
    ts.last_claims = [Claim("fc", ["fc:x"], 5.0, "fc (fc:x).", 0.8)]
    agent.ingest(sid, events)
    assert "fc" in ts.d.dead_kinds  # the phrase regex
    ts.signals = {"counted": ["fc"], "dead": ["idx"], "second_step": False, "keep_terms": ["index_clause"],
                  "traits": {"capacity_tight": "strong"}, "asks_for": []}  # fmt: skip
    agent.ingest(sid, events + [BUYER, msg(5.68, index_clause=clause, payment_days=30, contract_months=24)])
    assert "fc" not in ts.d.dead_kinds and "idx" in ts.d.dead_kinds  # the reader overrules the phrase
    assert ts.plan.kept == {"index_clause": clause}
    assert all(p.combo.get("index_clause") == clause for p in ts.plan.packages)
    assert ts.d.has("capacity_tight", strong=True) and ts.signals is None


def fake_strategist(choice):
    """A model that picks S1 and writes its price into the e-mail."""

    def complete(system, user, max_tokens=None, timeout=None):
        price = re.search(r"^S1: €([\d.]+)", user, re.M).group(1)
        return json.dumps({**choice, "sheets": ["S1"], "email": f"Dear team,\nWe offer €{price}.\nBest regards"})

    return complete


def test_strategist_chooses_the_attachments_and_its_reading_is_kept(world):
    c = case("P01")
    reply = {"counted": [], "dead": [], "second_step": True, "keep_terms": [], "traits": {}, "asks_for": ["data"]}
    agent = BuyerAgent(world, writer=StrategistWriter(fake_strategist({"attach": ["A1"], "reply": reply})),
                       read_timeout_s=0)  # fmt: skip
    sid = "sup:70110"
    threads = {sid: [msg(138.55, effective_date="2026-11-01")]}
    agent.start(c, threads)
    d = agent.decide(threads, sid)
    plan = d["message"]["_plan"]
    assert plan.files, "every data file we could give is on the strategist's menu"
    out = agent.write(d["message"])
    assert [f["name"] for f in out["attachments"]] == [plan.files[0]["name"]]
    assert all("about" not in f for f in out["attachments"])
    ts = agent.t[sid]
    assert ts.signals["second_step"] and ts.plan.second_step  # a second step counts at once
    assert ts.sent_schedule or ts.sent_drivers  # never sent twice


def test_code_arguments_fill_the_slots_the_strategist_leaves(world):
    # real P18: Opus sent the two index arguments only and dropped the +1.9 % forecast the supplier had counted
    agent = BuyerAgent(world, writer=StrategistWriter(fake_strategist({"claims": ["C1"]})), read_timeout_s=0)
    sid = "sup:70110"
    threads = {sid: [msg(138.55, effective_date="2026-11-01")]}
    agent.start(case("P01"), threads)
    out = agent.write(agent.decide(threads, sid)["message"])
    assert "doc:20260911-70110-P01" in out["body"] and "(fc:310010@2026-09)" in out["body"]
    assert len(re.findall(r"\((?:idx|fc|doc):", out["body"])) == 3
    # an argument code itself rates weak stays out when the strategist skips it (P04: "not strategic")
    agent = BuyerAgent(world, writer=StrategistWriter(fake_strategist({"claims": []})), read_timeout_s=0)
    sid = "sup:70620"
    threads = {sid: [msg(4.61, payment_days=30, contract_months=12)]}
    agent.start(case("P04"), threads)
    assert "fc:" not in agent.write(agent.decide(threads, sid)["message"])["body"]


def test_raw_material_sends_one_argument_a_round_whatever_the_strategist_picks(world):
    # team P18: 5.60, then 5.50 after the forecast in round 2, 5.40 after PPI-C2431 in round 3; its first counter
    # is a fixed step, so more arguments in round 1 are spent for nothing
    sid = "sup:70740"
    for picks, first in ((["C2", "C1"], "idx:PPI-C2431@2026-06"), ([], "idx:AS-14404@2026-06")):
        agent = BuyerAgent(world, writer=StrategistWriter(fake_strategist({"claims": picks})), read_timeout_s=0)
        threads = {sid: [msg(6.10, payment_days=30, contract_months=12, index_clause=None)]}
        agent.start(case("P18"), threads)
        body = agent.write(agent.decide(threads, sid)["message"])["body"]
        assert len(re.findall(r"\((?:idx|fc|po):", body)) == 1 and first in body


def test_cheap_money_keeps_our_sheets_on_ninety_days(world):
    # P08 60a9bbb3: its final came on our 60-day sheet and we signed at 45 days (0.523, against 0.570 at 90): its money
    # costs ~7 %, ours 8 %, so the longest term is our cheapest package. P18's costs ~9 %: shorter sheets stay
    clause = {"series": "AS-14404", "material_share": 0.45, "deadband_pct": 5, "base_month": "2026-08"}
    for cid, sid, opening, pair, ninety_only in (
        ("P08", "sup:70710", 6.05, (5.65, 5.57), True),  # real P08 544cfcd8 round 2
        ("P18", "sup:70740", 6.10, (5.60, 5.50), False),  # real P18 6fdd12d9 round 1
    ):
        agent = BuyerAgent(world, read_timeout_s=0)
        first = msg(opening, payment_days=30, contract_months=12, index_clause=None)
        agent.start(case(cid), {sid: [first]})
        counter = msg(pair[0], payment_days=90, contract_months=36, index_clause=clause)
        alt = msg(pair[1], sid="alt", payment_days=14, contract_months=36, index_clause=clause)
        counter["term_sheets"] += alt["term_sheets"]
        agent.ingest(sid, [first, BUYER, counter])
        plan = agent.t[sid].plan
        assert (plan.est.rate_seen < 0.08) == ninety_only
        pays = {t["payment_days"] for t in plan.sheets(1)}
        assert (pays == {90}) == ninety_only and (plan.est.rate == 0.07)  # the rate prices nothing


def test_an_argument_counted_after_its_first_counter_lowers_the_limit_estimate(world):
    agent = BuyerAgent(world, read_timeout_s=0)
    sid = "sup:70740"
    opening = msg(6.10, payment_days=30, contract_months=12, index_clause=None)
    threads = {sid: [opening]}
    agent.start(case("P18"), threads)
    clause = {"series": "AS-14404", "material_share": 0.45, "deadband_pct": 5, "base_month": "2026-08"}
    counter = msg(5.60, payment_days=90, contract_months=36, index_clause=clause)
    agent.ingest(sid, [opening, BUYER, counter])
    plan = agent.t[sid].plan
    before = plan.limit_hat
    agent.t[sid].last_claims = [c for c in candidates(world, agent.t[sid].d, case("P18")) if c.kind == "idx"][:1]
    took = {**msg(5.50, payment_days=90, contract_months=36, index_clause=clause,
                  body="We checked the index change data you cited; it holds and we took it into account."),
            "claim_results": [{"index": 0, "result": "verified"}]}  # fmt: skip
    agent.ingest(sid, [opening, BUYER, counter, BUYER, took])
    assert plan.late_args == 1 and plan.limit_hat < before - 0.004 * plan.asks[0]
    shrug = {**took, "body": "We checked the index change data you cited; it holds, but it does not change our "
             "calculation."}  # fmt: skip
    agent.ingest(sid, [opening, BUYER, counter, BUYER, took, BUYER, shrug])
    assert plan.late_args == 1  # no effect, no lower limit


def test_no_attachment_when_the_strategist_attaches_none(world):
    c = case("P01")
    agent = BuyerAgent(world, writer=StrategistWriter(fake_strategist({"attach": []})), read_timeout_s=0)
    sid = "sup:70110"
    threads = {sid: [msg(138.55, body="Which figures is that based on? Please send us the data.",
                         effective_date="2026-11-01")]}  # fmt: skip
    agent.start(c, threads)
    out = agent.write(agent.decide(threads, sid)["message"])
    assert "attachments" not in out  # the regex would have attached the cost drivers


# -- code reads the clause wish from its own words ---------------------------------------------------------
def test_supplier_naming_the_clause_series_wants_a_clause(world):
    c = case("P02")
    agent = BuyerAgent(world)
    agent.start(c)
    ts = agent.t["sup:70510"]
    assert not ts.est.wants_clause
    agent.ingest("sup:70510", [msg(370.96, body="CU-NOTATION rose 47.7 % in twelve months and is 20 % of our cost.",
                                    payment_days=30, index_clause=None)])  # fmt: skip
    assert ts.est.wants_clause


# -- decision guards -------------------------------------------------------------------------------------
def test_no_signature_while_a_rival_reply_is_in_flight(world):
    c = case("P05")
    agent = BuyerAgent(world)
    agent.start(c)
    threads = {
        "sup:70210": [msg(36.0, sid="a", payment_days=30, lead_time_days=28), BUYER,
                      msg(30.34, sid="a", payment_days=60, lead_time_days=28)],
        "sup:70240": [msg(31.5, sid="b", payment_days=30, lead_time_days=52)],
        "sup:70250": [msg(38.5, sid="c", payment_days=30, lead_time_days=28), BUYER,
                      msg(30.26, "final_offer", sid="c", payment_days=60, lead_time_days=28)],
    }  # fmt: skip
    agent.t["sup:70240"].sent = 1  # written to already (an untouched bidder also holds the signature: agent07)
    agent.t["sup:70210"].in_flight = True  # its next counter is on its way
    d = agent.decide(threads, "sup:70250")
    assert d and not d["message"].get("action") and d["message"]["term_sheets"] == []  # a hold note
    agent.t["sup:70210"].in_flight = False
    agent.t["sup:70250"].held = False
    d = agent.decide(threads, "sup:70250")
    assert d["message"]["action"]["type"] == "accept"  # nothing pending: its final offer is the best


def test_no_signature_while_a_rivals_first_counter_is_in_flight(world):
    # arena P05 97df70e8 and 2c7b1fe9 (0.665 twice): 70250 agreed to our 29.50 while 70210's first reply (36.00 ->
    # 30.00, "we will come back to you on price") was on its way; its standing sheet still read its 36.00 opening
    agent = BuyerAgent(world, read_timeout_s=0)
    agent.start(case("P05"))
    terms = {"payment_days": 14, "lead_time_days": 28}
    threads = {
        "sup:70210": [msg(36.0, sid="a")],
        "sup:70240": [msg(31.5, sid="b", lead_time_days=52)],
        "sup:70250": [msg(38.5, sid="c"), BUYER, msg(30.5, sid="c", **terms), BUYER,
                      msg(29.5, "agreed", sid="c", **terms)],
    }  # fmt: skip
    agent.t["sup:70240"].sent = 1
    agent.t["sup:70210"].sent, agent.t["sup:70210"].in_flight = 1, True
    d = agent.decide(threads, "sup:70250")
    assert not d["message"].get("action") and d["message"]["term_sheets"] == []  # a hold note
    # its first counter came with a second step to follow, and our next message to it is out: hold once more
    threads["sup:70210"] += [BUYER, msg(30.0, sid="a", body="We need to clarify this internally - we will come back "
                                        "to you on price.", **terms)]  # fmt: skip
    threads["sup:70250"] += [BUYER, msg(29.5, "agreed", sid="c", **terms)]
    agent.t["sup:70210"].sent += 1
    d = agent.decide(threads, "sup:70250")
    assert agent.t["sup:70210"].plan.second_step
    assert not d["message"].get("action") and d["message"]["term_sheets"] == []
    threads["sup:70250"] += [BUYER, msg(29.5, "agreed", sid="c", **terms)]
    d = agent.decide(threads, "sup:70250")
    assert d["message"]["action"]["type"] == "accept"  # two holds at most: then it signs the agreed sheet


def test_raw_material_steps_stay_visible(world):
    # arena back office: a step under 0.5 % of its opening got "My call: hold" and its final offer at its price
    # (P18 5db7f885, round 4: 5.15 -> 5.17 at 14 days, final 5.30). There the arguments it counted after its first
    # counter had brought our limit estimate down to our own offer, and the old pacing stepped 0.12 % there
    sid = "sup:70740"
    clause = {"series": "AS-14404", "material_share": 0.45, "deadband_pct": 5, "base_month": "2026-08"}
    long = {"contract_months": 36, "index_clause": clause}
    threads = {sid: [msg(6.10, payment_days=30, contract_months=12, index_clause=None)]}
    agent = BuyerAgent(world, read_timeout_s=0)
    agent.start(case("P18"), threads)
    plan, values = agent.t[sid].plan, []
    r1, r2 = msg(5.60, payment_days=90, **long), msg(5.50, payment_days=90, **long)  # its real replies, both sheets
    r1["term_sheets"] += msg(5.50, sid="b", payment_days=30, contract_months=24, index_clause=clause)["term_sheets"]
    r2["term_sheets"] += msg(5.40, sid="b", payment_days=14, **long)["term_sheets"]
    for reply in (r1, r2, msg(5.30, payment_days=14, **long), None):
        plan.late_args = 3 * max(0, len(plan.asks) - 2)
        out = agent.write(agent.decide(threads, sid)["message"])
        values.append(plan.last_value)
        if reply:
            threads[sid] = threads[sid] + [{**out, "from": "buyer"}, reply]
    steps = [b - a for a, b in itertools.pairwise(values)]
    assert min(steps) >= 0.005 * plan.asks[0]


def test_a_term_it_varies_between_its_own_sheets_is_no_condition(world):
    # arena P18 5db7f885: "we welcome your proposal of 14-day payment terms", with its own 90-day sheet beside the
    # 14-day one, came back from the strategist as keep_terms payment_days; every later sheet was at 14 days
    sid = "sup:70740"
    clause = {"series": "AS-14404", "material_share": 0.45, "deadband_pct": 5, "base_month": "2026-08"}
    opening = msg(6.10, payment_days=30, contract_months=12, index_clause=None)
    reply = msg(5.50, payment_days=90, contract_months=36, index_clause=clause)
    alt = msg(5.40, sid="alt", payment_days=14, contract_months=36, index_clause=clause)
    reply["term_sheets"] += alt["term_sheets"]
    agent = BuyerAgent(world, read_timeout_s=0)
    agent.start(case("P18"), {sid: [opening]})
    ts = agent.t[sid]
    ts.signals = {"counted": [], "dead": [], "second_step": False, "keep_terms": ["payment_days", "index_clause"],
                  "traits": {}}  # fmt: skip
    agent.ingest(sid, [opening, BUYER, reply])
    assert ts.plan.kept == {"index_clause": clause}  # the clause is on both of its sheets: that one stays


def ladder_strategist(level):
    """A model that sets its own pace: every sheet of ladder level `level`, their prices in the e-mail."""

    def complete(system, user, max_tokens=None, timeout=None):
        ids = re.findall(rf"^  (L{level}\.\d+): €([\d.]+)", user, re.M)
        prices = " or ".join(f"€{p}" for _i, p in ids)
        return json.dumps({"read": "x", "reply": {}, "sheets": [i for i, _p in ids], "claims": [], "points": [],
                           "attach": [], "email": f"Dear team,\nWe offer {prices}.\nBest regards"})  # fmt: skip

    return complete


def test_the_strategist_sets_the_pace_inside_the_guardrails(world):
    # the team's LLM-led negotiator set its own prices and holds the team bests on P02, P08, P11 and P18
    sid = "sup:70740"
    clause = {"series": "AS-14404", "material_share": 0.45, "deadband_pct": 5, "base_month": "2026-08"}
    opening = msg(6.10, payment_days=30, contract_months=12, index_clause=None)
    counter = msg(5.60, payment_days=90, contract_months=36, index_clause=clause)
    agent = BuyerAgent(world, writer=StrategistWriter(ladder_strategist(1)), read_timeout_s=0)
    threads = {sid: [opening]}
    agent.start(case("P18"), threads)
    first = agent.write(agent.decide(threads, sid)["message"])
    plan = agent.t[sid].plan
    opened = plan.last_value
    threads[sid] = [opening, {**first, "from": "buyer"}, counter]
    d = agent.decide(threads, sid)
    ladder, menu = d["message"]["_plan"].ladder, d["message"]["_plan"].menu
    assert ladder and all(0.85 * 5.11 < t["unit_price"] < 5.60 for _l, sh in ladder for t, _dc in sh)
    out = agent.write(d["message"])
    sent = [x["terms"] for x in out["term_sheets"]]
    assert sent == [t for t, _dc in ladder[0][1]]  # its pick goes out, every sheet of that level
    assert ("sup:70740", "level", "L1") in agent.log
    assert opened * 1.003 <= plan.last_value <= plan.ask_value * 0.997  # committed: next round builds on it
    assert all(0.85 * sent[0]["unit_price"] < t["unit_price"] < 1.15 * sent[0]["unit_price"] for t, _dc in menu)


def test_a_term_it_took_from_our_own_sheets_is_no_lock(world):
    # P07 ccc8ec17: we offered 14 and 45 days, it answered at 14 twice, and every later sheet was 14 days (0.727; the
    # team's 0.769 closed at 60 days). Its money (~7.7 %) is cheaper than ours: our sheets go to 90 days
    sid = "sup:70610"
    agent = BuyerAgent(world, read_timeout_s=0)
    opening = msg(5.74, payment_days=30, contract_months=12)
    agent.start(case("P07"), {sid: [opening]})
    plan = agent.t[sid].plan
    agent.ingest(sid, [opening])
    ours = [{"unit_price": 5.03, "payment_days": 14, "contract_months": 36},
            {"unit_price": 5.06, "payment_days": 45, "contract_months": 36}]  # fmt: skip
    plan.commit(ours)
    r1, r2 = (msg(p, payment_days=14, contract_months=36) for p in (5.49, 5.44))
    agent.ingest(sid, [opening, BUYER, r1])
    plan.commit(ours)
    agent.ingest(sid, [opening, BUYER, r1, BUYER, r2])
    assert "payment_days" not in plan.locked
    assert {t["payment_days"] for t in plan.sheets(2)} == {90}
    # it insists on a term we did not offer: that one is a given, and stays one while it repeats it
    plan.commit([{"unit_price": 5.17, "payment_days": 90, "contract_months": 36}])
    r3 = msg(5.39, payment_days=14, contract_months=36)
    agent.ingest(sid, [opening, BUYER, r1, BUYER, r2, BUYER, r3])
    assert plan.locked.get("payment_days") == 14 and plan.sheets(3)  # it beats our 90-day preference
    plan.commit(plan.sheets(3))
    agent.ingest(sid, [opening, BUYER, r1, BUYER, r2, BUYER, r3, BUYER, msg(5.35, payment_days=14, contract_months=36)])
    assert plan.locked.get("payment_days") == 14


def test_a_weak_cash_squeeze_read_alone_does_not_price_its_money(world):
    # P07: the persona read called Gazelle weakly cash-short although its notes said "finances stable"; its money went
    # to 10 % and our sheets to 14 days. Document evidence (P01's and P05's "pay in 10 days?") still counts
    weak = {"traits": {"cash_short": "weak"}, "unsure": []}
    for cid, sid, rate in (("P07", "sup:70610", 0.07), ("P01", "sup:70110", 0.10), ("P05", "sup:70250", 0.10)):
        d = Dossier(world, case(cid), sid)
        d.apply_read(weak)
        assert d.has("cash_short") and d.cash_rate == rate


def test_never_above_its_next_counter(world):
    # P11 3a8baec0: at 45.50 on its 0.50 grid it agreed to our 45.22, where its next counter was 45.00
    sid = "sup:70140"
    agent = BuyerAgent(world, read_timeout_s=0)
    opening = msg(47.0, ppm_commitment=6400)
    agent.start(case("P11"), {sid: [opening]})
    plan = agent.t[sid].plan
    events = [opening]
    for p in (46.5, 46.0, 45.5):
        events += [BUYER, msg(p, ppm_commitment=300, chargeback_eur_per_complaint=0)]
    agent.ingest(sid, events)
    plan.commit([{"ppm_commitment": 300, "chargeback_eur_per_complaint": 0, "unit_price": 44.9}])
    assert plan.increment() == pytest.approx(0.5)
    top = max(t["unit_price"] for t in plan.sheets(4) if t["ppm_commitment"] == 300)
    assert top <= 45.0


def test_quality_case_opens_ten_percent_below_a_cash_short_supplier(world):
    # 5 % put our offers within 2 % of P11's price by round 2, where it holds; our team's 0.794 opened 7.5 % below
    sid = "sup:70140"
    threads = {sid: [msg(47.0, ppm_commitment=6400)]}
    agent = BuyerAgent(world, read_timeout_s=0)
    agent.start(case("P11"), threads)
    agent.write(agent.decide(threads, sid)["message"])
    plan = agent.t[sid].plan
    assert plan.anchor_gap == pytest.approx(0.10)
    assert plan.last_value == pytest.approx(0.90 * plan.asks[0], rel=0.005)


def test_walks_when_the_alternative_is_cheaper_and_c_is_exact(world):
    c = case("P07")  # routine: no relationship charge, C is exact; the current contract costs 379,916
    agent = BuyerAgent(world)
    agent.start(c)
    sid = "sup:70610"
    threads = {sid: [msg(5.74, payment_days=30, contract_months=24), BUYER,
                     msg(6.35, "final_offer", payment_days=30, contract_months=24)]}  # fmt: skip
    d = agent.decide(threads, sid)
    assert agent.t[sid].c_standing > agent.t[sid].plan.c_alt  # 1 % dearer: the old 2 % band signed it
    assert d["message"]["action"]["type"] == "walk_away"


# -- contract facts in our sentences ------------------------------------------------------------------------
def test_notice_sentence_says_what_the_contract_says(world, monkeypatch):
    c = case("P01")
    d = Dossier(world, c, "sup:70110")
    (notice,) = [x for x in candidates(world, d, c) if x.kind == "notice"]
    assert "calendar year" in notice.follow and "calendar year" in d.clause("price")
    monkeypatch.setattr(Dossier, "clause", lambda self, kind: "Prices as per the price annex.")
    (notice,) = [x for x in candidates(world, Dossier(world, c, "sup:70110"), c) if x.kind == "notice"]
    assert "calendar year" not in notice.follow


def test_quality_agreement_fee_is_named_when_a_sheet_waives_it(world):
    c = case("P11")
    agent = BuyerAgent(world)
    agent.start(c)
    ts = agent.t["sup:70140"]
    assert ts.d.complaint_fee == 150
    sheet = {"ppm_commitment": 1500, "chargeback_eur_per_complaint": 0, "unit_price": 45.0}
    assert any("EUR 150" in line for line in agent.levers(ts, [sheet]))
    assert not any("EUR 150" in line for line in agent.levers(ts, [{**sheet, "chargeback_eur_per_complaint": 150}]))
