"""Where the model reads instead of a regex: the persona read at case start, the strategist's reading of
each reply and its choice of attachments - with fake models, no network. Plus the decision guards found on
the twin and in the real sessions (signing past an in-flight rival, a deal dearer than the alternative)."""

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

    # the live read is the fallback for a supplier without a profile: test it without profiles
    agent = BuyerAgent(world, writer=StrategistWriter(complete), use_profiles=False)
    opening = msg(370.96, body="CU-NOTATION rose 47.7 % in twelve months and is 20 % of our cost.")
    agent.start(c, {"sup:70510": [opening]})
    assert "CU-NOTATION rose" in seen[0]  # the opening e-mail is part of the read
    assert agent.t["sup:70510"].est.wants_clause
    assert any(p.combo.get("index_clause") for p in agent.t["sup:70510"].plan.packages)  # priced, ranked by C


def test_template_writer_makes_no_model_call(world):
    agent = BuyerAgent(world, use_profiles=False)  # the benchmark's writer: no `complete`, regex traits only
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
    agent = BuyerAgent(world, use_profiles=False)  # its profile already knows it wants the clause
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


def test_walks_when_the_alternative_is_cheaper_and_c_is_exact(world):
    c = case("P07")  # routine: no relationship charge, C is exact; the current contract costs 379,916
    agent = BuyerAgent(world)
    agent.start(c)
    sid = "sup:70610"
    threads = {sid: [msg(6.60, payment_days=30, contract_months=24), BUYER,
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
