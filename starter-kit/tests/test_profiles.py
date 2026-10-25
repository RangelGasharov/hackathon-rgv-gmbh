"""Supplier profiles (`buyer.profiles`): facts from the data, the deep read checked against them, the files the
agent loads instead of a live read, and the round cap with its last-round decision."""

import json
from pathlib import Path

import pytest

from buyer import profiles
from buyer.agent import MAX_ROUNDS, BuyerAgent, round_cap
from buyer.dossier import World
from buyer.strategy import FAST_CLOSE, RECIPROCAL, Planner


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def _case(cid):
    return json.loads(Path(f"cases/real/{cid}.json").read_text(encoding="utf-8"))


def _quote(f):
    return " ".join(f["documents"][0]["text"].split()[:6])  # the document's own words


def _persona(sid, f, **over):
    p = {
        "supplier": sid,
        "source_hash": profiles.source_hash(f),
        "summary": "Family foundry on an old cost sheet; expects partnership.",
        "traits": {"stale_costing": "strong", "cash_short": "weak", "price_frozen": "none"},
        "unsure": [],
        "wants": ["faster_payment"],
        "evidence": [{"trait": "stale_costing", "doc": f["documents"][0]["id"], "quote": _quote(f)}],
        "playbook": {"opening": "moderate", "walk_risk": "low", "levers": ["binding volume"]},
    }
    p.update(over)
    return p


def test_the_46_detailed_suppliers(world):
    ids = profiles.supplier_ids(world)
    assert len(ids) == 46 and "sup:70110" in ids and "sup:70740" in ids


def test_facts_cover_what_a_buyer_needs(world):
    f = profiles.facts(world, "sup:70130")
    assert f["company"]["owner_type"] and f["our_weight"]["share_of_its_turnover_pct"] > 0
    assert f["parts"] and f["parts"][0]["current_price"] and "other_sources" in f["parts"][0]
    assert f["documents"] and all(d["id"].startswith("doc:") and d["text"] for d in f["documents"])
    assert f["negotiations"]["events"]
    assert profiles.source_hash(f) == profiles.source_hash(profiles.facts(world, "sup:70130"))  # deterministic


def test_a_persona_must_cite_its_own_documents(world):
    f = profiles.facts(world, "sup:70110")
    assert profiles.check_persona(_persona("sup:70110", f), f) == []
    bad = _persona(
        "sup:70110",
        f,
        traits={"stale_costing": "strong", "made_up": "strong", "cash_short": "maybe"},
        evidence=[{"trait": "x", "doc": "doc:20260101-70999-M1", "quote": "-"}],
    )
    errs = " ".join(profiles.check_persona(bad, f))
    assert "unknown trait made_up" in errs and "bad level cash_short" in errs
    assert "not a document of sup:70110" in errs and "strong trait stale_costing without evidence" in errs
    made_up = _persona("sup:70110", f, evidence=[{"trait": "stale_costing", "doc": f["documents"][0]["id"],
                                                    "quote": "we will go bankrupt next month"}])  # fmt: skip
    assert any("not found verbatim" in e for e in profiles.check_persona(made_up, f))


def test_build_writes_findable_files_and_flags_a_stale_read(world, tmp_path):
    f = profiles.facts(world, "sup:70110")
    (tmp_path / "persona").mkdir()
    (tmp_path / "persona" / "sup_70110.json").write_text(json.dumps(_persona("sup:70110", f)), encoding="utf-8")
    stale = _persona("sup:70120", profiles.facts(world, "sup:70120"), source_hash="0000")
    (tmp_path / "persona" / "sup_70120.json").write_text(json.dumps(stale), encoding="utf-8")
    problems = profiles.build(world, tmp_path, now="test")
    assert len(list(tmp_path.glob("sup_*.json"))) == 46 and len(list(tmp_path.glob("sup_*.md"))) == 46
    assert (tmp_path / "INDEX.md").read_text(encoding="utf-8").count("](sup_") == 46
    assert problems["sup:70110"] == []
    assert any("read it again" in p for p in problems["sup:70120"])
    p = profiles.load("sup:70110", tmp_path)
    assert profiles.read_of(p)["traits"]["stale_costing"] == "strong"
    assert profiles.read_of(profiles.load("sup:70120", tmp_path)) is None  # a stale read is not used
    b = profiles.brief(p)  # background for the strategist, not which package to pick
    assert "background only" in b and "Walk risk: low" in b and "Levers" not in b


class Recorder:
    """A model writer that records the live persona reads it is asked for."""

    def __init__(self):
        self.calls = []

    def complete(self, system, user, max_tokens=900, timeout=None):
        self.calls.append(user)
        return json.dumps({"traits": {}, "unsure": [], "wants": [], "hedge_until": None, "notes": ""})

    def write(self, plan, timeout=None):
        return "Thank you."


def test_profiled_suppliers_skip_the_live_read(world, tmp_path):
    f = profiles.facts(world, "sup:70210")
    (tmp_path / "persona").mkdir()
    (tmp_path / "persona" / "sup_70210.json").write_text(
        json.dumps(_persona("sup:70210", f, traits={"free_capacity": "strong"},
                            evidence=[{"trait": "free_capacity", "doc": f["documents"][0]["id"], "quote": _quote(f)}])),
        encoding="utf-8",
    )  # fmt: skip
    profiles.build(world, tmp_path, now="test")
    rec = Recorder()
    agent = BuyerAgent(world, writer=rec, profiles_root=tmp_path, read_timeout_s=5)
    agent.start(_case("P05"))  # three bidders: one with a profile, two without
    assert len(rec.calls) == 2 and not any("sup:70210" in c for c in rec.calls)
    d = agent.t["sup:70210"].d
    assert d.profile and d.has("free_capacity", strong=True) and "PROFILE sup:70210" in d.brief()


def test_without_profiles_every_supplier_gets_the_live_read(world, tmp_path):
    rec = Recorder()
    agent = BuyerAgent(world, writer=rec, profiles_root=tmp_path, read_timeout_s=5, use_profiles=False)
    agent.start(_case("P05"))
    assert len(rec.calls) == 3


def test_never_more_than_ten_rounds():
    assert MAX_ROUNDS == 10
    assert round_cap({"round_limit": 15}) == 10 and round_cap({"round_limit": 6}) == 6 and round_cap({}) == 10


def _msg(price, status="open"):
    return {
        "from": "sales",
        "status": status,
        "body": "Our offer.",
        "term_sheets": [{"term_sheet_id": "q-1", "terms": {"unit_price": price}}],
    }


def test_at_the_round_cap_it_decides_instead_of_countering(world):
    case = _case("P01")
    agent = BuyerAgent(world)
    agent.start(case)
    threads = {"sup:70110": [_msg(138.55), {"from": "buyer"}, _msg(136.0)]}
    agent.decide(threads, "sup:70110")  # read the thread
    ts = agent.t["sup:70110"]
    ts.sent, ts.k = round_cap(case), 3  # ten messages sent: none left, accepting is still allowed
    d = agent.decide(threads, "sup:70110")
    assert d and d["message"].get("action", {}).get("type") == "accept"


# -- the profile-chosen plan: one primary approach, at most one switch ------------------------------------------
def _planner(world, cid, sid, profile):
    agent = BuyerAgent(world, use_profiles=False)
    agent.start(_case(cid))
    est = agent.t[sid].est
    est.d.profile = profile
    return Planner(_case(cid), est)


def _arena(**over):
    return {"sessions": 10, "walked": 0, "agreed_round1": 0, "first_counter_vs_opening": 0.95, **over}


def test_agreeing_to_our_first_offer_is_no_reason_to_close_fast(world):
    # P02's 70510 agreed to our round-1 sheet 3 times in 10: our opening was too generous, not too deep
    p = _planner(world, "P02", "sup:70510", {"arena": _arena(agreed_round1=3, first_counter_vs_opening=1.0)})
    assert p.primary_strategy == RECIPROCAL


def test_fast_close_does_not_open_closer_to_the_ask(world):
    plain = _planner(world, "P07", "sup:70610", None)  # no warning signs in its record, news or letters
    fast = _planner(world, "P07", "sup:70610", {"persona": {"playbook": {"walk_risk": "high"}}})
    assert fast.primary_strategy == FAST_CLOSE and plain.primary_strategy == RECIPROCAL
    assert fast.anchor_gap == plain.anchor_gap


def test_room_below_the_first_counter_opens_a_little_deeper(world):
    plain = _planner(world, "P05", "sup:70250", None)
    roomy = _planner(world, "P05", "sup:70250", {"arena": _arena(first_counter_vs_opening=0.80)})
    assert roomy.primary_strategy == RECIPROCAL and roomy.strategy_evidence["room"]
    assert plain.anchor_gap < roomy.anchor_gap <= plain.anchor_gap + 0.02
    # a committee supplier keeps its cap (P18's 70740: approval ladder, raw material 10 %)
    capped = _planner(world, "P18", "sup:70740", {"arena": _arena(first_counter_vs_opening=0.80)})
    assert (
        capped.strategy_evidence["room"] and capped.anchor_gap == _planner(world, "P18", "sup:70740", None).anchor_gap
    )
    # and a trusted supplier gets no deeper opening at all (P07's 70610: our source for 14 years)
    trusted = _planner(world, "P07", "sup:70610", {"arena": _arena(first_counter_vs_opening=0.80)})
    assert (
        trusted.strategy_evidence["trusted"]
        and trusted.anchor_gap == _planner(world, "P07", "sup:70610", None).anchor_gap
    )


def test_news_letters_and_past_deals_choose_the_plan(world):
    p10 = _planner(world, "P10", "sup:70410", None)  # "allocates volumes" (news, 2026-08)
    assert p10.primary_strategy == FAST_CLOSE and "capacity is its constraint" in p10.strategy_reason
    assert any("lead-time" in x for x in p10.strategy_evidence["lead"])
    p01 = _planner(world, "P01", "sup:70110", None)  # asks +9.5 %, drivers +2.3 %, 51 days' notice against 90
    ev = p01.strategy_evidence
    assert p01.primary_strategy == RECIPROCAL and "its letter asks +9.5 %" in p01.strategy_reason
    assert any("cost drivers that fell" in x for x in ev["lead"]) and any("notice" in x for x in ev["lead"])
    assert "Lead with:" in p01.est.d.strategy_plan and "Trusted supplier" in p01.est.d.strategy_plan
    p07 = _planner(world, "P07", "sup:70610", None)  # 9 negotiations: asked +2.5 %, settled +0.2 %
    assert "it gives way" in p07.strategy_reason
    # restructuring news: a strategic supplier (P06's 70150) is handled with care, a quality case only explained
    p06 = _planner(world, "P06", "sup:70150", None)
    assert p06.primary_strategy == FAST_CLOSE and "financial pressure" in p06.strategy_reason
    p11 = _planner(world, "P11", "sup:70140", None)
    assert p11.primary_strategy == RECIPROCAL and any("faster payment" in x for x in p11.strategy_evidence["lead"])


def test_who_is_trusted(world):
    trusted = {cid: _planner(world, cid, sid, None).strategy_evidence for cid, sid in
               [("P01", "sup:70110"), ("P18", "sup:70740"), ("P11", "sup:70140"), ("P13", "sup:70230")]}  # fmt: skip
    assert trusted["P01"]["trusted"] and "category goal stabilise" in trusted["P01"]["why_trusted"]
    assert trusted["P13"]["trusted"]  # leverage case, but our active source of the part for 12 years
    assert not trusted["P18"]["trusted"]  # active source only since 2021-2022
    assert not trusted["P11"]["trusted"] and trusted["P11"]["why_trusted"] == ["red scorecard"]


def test_one_switch_at_most_and_never_on_a_final_offer(world):
    p = _planner(world, "P10", "sup:70410", {"persona": {"playbook": {"walk_risk": "high"}}})
    p.asks = [100.0, 99.0]
    assert p._adapt_strategy("final_offer") is None and p.strategy == FAST_CLOSE
    p.asks = [100.0, 95.0]  # a 5 % step: it moves, so reciprocate
    change = p._adapt_strategy("open")
    assert change and change["to"] == RECIPROCAL and p.strategy == RECIPROCAL
    assert "switched once" in p.est.d.strategy_plan
    p.asks = [100.0, 95.0, 95.0, 95.0]  # it stalls afterwards: no second switch
    assert p._adapt_strategy("open") is None and p.strategy == RECIPROCAL


def test_two_stalled_replies_switch_reciprocal_to_fast_close(world):
    p = _planner(world, "P01", "sup:70110", None)
    p.asks = [100.0, 96.0, 96.0]
    assert p._adapt_strategy("open") is None  # one stalled reply is not yet a pattern
    p.asks = [100.0, 96.0, 96.0, 96.0]
    assert p._adapt_strategy("agreed") is None  # it took our sheet: nothing left to adapt (live P18 rerun)
    p.asks = [100.0, 96.0, 96.0, 97.0]
    assert p._adapt_strategy("open") is None  # it raised its ask: resistance, not "negligible movement"
    p.asks = [100.0, 96.0, 96.0, 95.95]
    assert p._adapt_strategy("open")["to"] == FAST_CLOSE


# -- final offers and trusted suppliers: no risky trades ------------------------------------------------------------
CLAUSE = {"series": "AS-14404", "material_share": 0.45, "deadband_pct": 5.0, "base_month": "2026-08"}


def _final(*sheets):
    return {
        "from": "sales",
        "status": "final_offer",
        "body": "Our final offer.",
        "term_sheets": [{"term_sheet_id": i, "terms": t} for i, t in sheets],
    }


def test_an_alternative_in_a_final_offer_is_taken_by_sending_its_own_terms(world):
    agent = BuyerAgent(world, use_profiles=False)
    agent.start(_case("P18"))
    main = {"unit_price": 5.5, "payment_days": 60, "contract_months": 36, "index_clause": CLAUSE}
    alt = {**main, "payment_days": 90}  # live P18 rerun: the arena refused to accept "offer:70740-4-a" twice
    threads = {"sup:70740": [_msg(6.1), {"from": "buyer"}, _final(("offer:70740-4", main), ("offer:70740-4-a", alt))]}
    d = agent.decide(threads, "sup:70740")
    assert ("sup:70740", "take_alternative", 5.5) in agent.log
    assert not d["message"].get("action") and d["message"]["term_sheets"] == [{"terms": alt}]
    assert d["message"]["_plan"].menu == [(alt, 0.0)] and d["message"]["refs"] == []  # no other package, no claim
    d = agent.decide(threads, "sup:70740")  # it answers with the same final offer again: take the standing sheet
    assert d["message"]["action"] == {"type": "accept", "term_sheet_id": "offer:70740-4"}


def test_after_a_final_offer_no_changed_terms_go_out(world):
    agent = BuyerAgent(world, use_profiles=False)
    agent.start(_case("P18"))
    main = {"unit_price": 5.5, "payment_days": 60, "contract_months": 36, "index_clause": CLAUSE}
    threads = {"sup:70740": [_msg(6.1), {"from": "buyer"}, _final(("offer:70740-4", main))]}
    agent.decide(threads, "sup:70740")
    msg = agent.compose(threads, "sup:70740", sheets=[{**main, "unit_price": 5.4}])
    assert msg["term_sheets"] == [] and ("sup:70740", "final_guard", [5.4]) in agent.log


def test_a_trusted_supplier_hears_no_risky_arguments(world, monkeypatch):
    import buyer.agent as agent_mod
    from buyer.claims import Claim

    fake = [
        Claim("po", ["po:4500000001-10"], 130.0, "Our last order was at EUR 130.", 0.9),
        Claim("news", ["news:1"], None, "We read that you restructure.", 0.8),
        Claim("fc", ["fc:1"], 12.0, "Our forecast rises 12 %.", 0.7),
    ]
    monkeypatch.setattr(agent_mod, "candidates", lambda *a, **k: list(fake))
    kinds = {}
    for cid, sid in (("P01", "sup:70110"), ("P18", "sup:70740")):
        agent = BuyerAgent(world, use_profiles=False)
        agent.start(_case(cid))
        msg = agent.compose({sid: [_msg(100.0)]}, sid)
        kinds[cid] = {c.kind for c in msg["_plan"].claims} | {c.kind for c in msg["_plan"].menu_claims}
    assert kinds["P01"] == {"fc"}  # trusted: no price history, no news
    assert {"po", "news"} <= kinds["P18"]


def test_the_clause_sheet_sits_at_its_cheapest_price_for_us(world):
    from buyer.strategy import cost

    case = _case("P02")  # stabilise: below its healthy margin the relationship charge rises, so no clause price is
    agent = BuyerAgent(
        world
    )  # equal-cost; the fallback discount sent 357.12 (live: taken at once, charge 66,941, 0.63)
    agent.start(case)
    p, est = agent.t["sup:70510"].plan, agent.t["sup:70510"].est
    base = p._complete({**p.packages[0].combo, "unit_price": 360.0})
    pair = p._clause_pair(base, cost(case, base, est))
    assert pair and pair["index_clause"] and pair["unit_price"] > 357.12  # the trial stays (clause-free limit ~372)
    for d in (-1.0, 1.0):  # and no nearby price costs us less
        assert cost(case, {**pair, "unit_price": pair["unit_price"] + d}, est) >= cost(case, pair, est)


@pytest.fixture(autouse=True)
def _strat_off(monkeypatch):
    """These tests cover the single switches / the case-level strategy; BUYER_STRAT (on by default) overrides them."""
    monkeypatch.setenv("BUYER_STRAT", "0")


def test_every_situation_topic_of_the_data_is_read():
    """Each news topic in the data that says what a supplier needs maps to a situation (the Sunday release added
    customer_loss; chip_shortage was unmapped)."""
    from buyer.dossier import NEWS_SITUATION, NEWS_TRAITS

    for topic in ("customer_loss", "chip_shortage", "short_time_work", "credit_downgrade", "free_capacity"):
        assert topic in NEWS_SITUATION and topic in NEWS_TRAITS


def _p11_after_first_reply(world):
    agent = BuyerAgent(world, use_profiles=False)
    agent.start(_case("P11"))
    p = agent.t["sup:70140"].plan
    opening = {"unit_price": 47.0, "ppm_commitment": 6400}
    reply = [{"unit_price": 47.0, "ppm_commitment": 300, "chargeback_eur_per_complaint": 0.0},
             {"unit_price": 46.5, "ppm_commitment": 1500, "chargeback_eur_per_complaint": 0.0}]  # fmt: skip
    p.observe_reply([opening], "open")
    p.observe_reply(reply, "open")
    return p


def test_a_ppm_trade_is_no_price_step_on_like_terms(world, monkeypatch):
    p = _p11_after_first_reply(world)
    old = p.first_move()
    assert old[0] - old[1] > 1.0  # the model reads 47.00 at 300 ppm as 1.18 below 47.00 at 6400 ppm
    monkeypatch.setenv("BUYER_LIKE", "1")
    v0, a1 = p.first_move()
    assert v0 - a1 == pytest.approx(0.5, abs=0.05)  # on like terms it moved only its 1500-ppm sheet, by 0.50


def test_quality_steps_keep_it_countering(world, monkeypatch):
    p = _p11_after_first_reply(world)
    p.last_value, p.last_sheets = 43.0, []
    monkeypatch.setenv("BUYER_QSTEP", "0")
    small = p.target_value(2)
    monkeypatch.setenv("BUYER_QSTEP", "1")
    big = p.target_value(2)
    assert big >= 43.0 + 0.0055 * 47.0 - 1e-9 and big >= small  # at least 0.55 % of its opening while 2 % apart
    assert p.ask_value - big >= 0.02 * 47.0 - 1e-9  # and never into the last 2 % above its ask


def test_rfq_two_step_holds_our_exact_sheets(world):
    """After a rival's quote a bidder gives half its step and checks the rest internally (P05 back office): we
    resend our exact sheets instead of rising, and the strategist gets no other package."""
    agent = BuyerAgent(world, use_profiles=False)
    agent.start(_case("P05"))
    sid = "sup:70250"
    threads = {s: [_msg(p)] for s, p in (("sup:70210", 36.0), ("sup:70240", 31.5), (sid, 38.5))}
    first = agent.decide(threads, sid)["message"]
    sent = [t["terms"] for t in first["term_sheets"]]
    reply = {**_msg(30.5), "body": "We need to clarify this internally - we will come back to you on price."}
    threads[sid] += [{**first, "from": "buyer"}, reply]
    d = agent.decide(threads, sid)
    assert d and not d["message"].get("action")
    assert [t["terms"] for t in d["message"]["term_sheets"]] == sent  # the same sheets, not a step up
    assert [t for t, _ in d["message"]["_plan"].menu] == sent
    assert any(k == "two_step_hold" for _, k, _ in agent.log)
