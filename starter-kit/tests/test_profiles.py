"""Supplier profiles (`buyer.profiles`): facts from the data, the deep read checked against them, the files the
agent loads instead of a live read, and the round cap with its last-round decision."""

import json
from pathlib import Path

import pytest

from buyer import profiles
from buyer.agent import MAX_ROUNDS, BuyerAgent, round_cap
from buyer.dossier import World


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


def test_the_last_round_decides_instead_of_countering(world):
    case = _case("P01")
    agent = BuyerAgent(world)
    agent.start(case)
    threads = {"sup:70110": [_msg(138.55), {"from": "buyer"}, _msg(136.0)]}
    agent.decide(threads, "sup:70110")  # read the thread
    ts = agent.t["sup:70110"]
    ts.sent, ts.k = round_cap(case) - 1, 3  # nine messages sent: one left
    d = agent.decide(threads, "sup:70110")
    assert d and d["message"].get("action", {}).get("type") == "accept"
