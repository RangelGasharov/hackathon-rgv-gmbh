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
    plain = _planner(world, "P10", "sup:70410", None)
    fast = _planner(world, "P10", "sup:70410", {"persona": {"playbook": {"walk_risk": "high"}}})
    assert fast.primary_strategy == FAST_CLOSE and plain.primary_strategy == RECIPROCAL
    assert fast.anchor_gap == plain.anchor_gap


def test_room_below_the_first_counter_opens_a_little_deeper(world):
    plain = _planner(world, "P05", "sup:70250", None)
    roomy = _planner(world, "P05", "sup:70250", {"arena": _arena(first_counter_vs_opening=0.80)})
    assert roomy.primary_strategy == RECIPROCAL and roomy.strategy_evidence["room"]
    assert plain.anchor_gap < roomy.anchor_gap <= plain.anchor_gap + 0.02
    # a committee or capacity-tight supplier keeps its cap (P10's 70410: approval ladder, capacity tight)
    capped = _planner(world, "P10", "sup:70410", {"arena": _arena(first_counter_vs_opening=0.80)})
    assert capped.anchor_gap == _planner(world, "P10", "sup:70410", None).anchor_gap


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
