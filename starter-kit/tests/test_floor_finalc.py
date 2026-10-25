"""BUYER_FLOOR (chase a stepping supplier, tightest ppm) and BUYER_FINALC (cheapest sheet after a final offer)."""

import json
import time
from pathlib import Path

import pytest

from buyer import flags
from buyer.agent import APPROVAL_WORDS, FLOOR_WORDS, BuyerAgent, ThreadState
from buyer.dossier import World

SID = "sup:70140"


@pytest.fixture(scope="module")
def world():
    return World("data/package")


@pytest.fixture
def p11():
    return json.loads(Path("cases/real/P11.json").read_text())


def _sheets(price, ppm=300, charge=150):
    return [{"term_sheet_id": f"q-{price}", "terms": {"unit_price": price, "ppm_commitment": ppm,
                                                    "chargeback_eur_per_complaint": charge}}]


def _reply(price, status="open", body="Our offer.", sheets=None):
    return {"from": "sales", "status": status, "body": body, "term_sheets": sheets or _sheets(price)}


def _plan_after(world, case, prices):
    plan = ThreadState(world, case, SID).plan
    for p in prices:
        plan.observe_reply([s["terms"] for s in _sheets(p)], "open")
    return plan


def test_switches_read_the_environment(monkeypatch):
    for name, fn in (("BUYER_FLOOR", flags.floor), ("BUYER_FINALC", flags.finalc)):
        monkeypatch.delenv(name, raising=False)
        assert not fn()
        monkeypatch.setenv(name, "1")
        assert fn()


def test_stepping_needs_two_equal_down_moves(world, p11):
    assert _plan_after(world, p11, [47.0, 46.5, 46.0]).stepping()
    assert not _plan_after(world, p11, [47.0, 46.5]).stepping()  # one move only
    assert not _plan_after(world, p11, [47.0, 46.5, 46.5]).stepping()  # flat
    assert not _plan_after(world, p11, [47.0, 45.0, 44.9]).stepping()  # steps differ by far more than 25 %
    assert not _plan_after(world, p11, [47.0, 47.0, 46.99]).stepping()  # under 0.2 % of its opening


def test_chasing_only_with_the_switch(world, p11, monkeypatch):
    plan = _plan_after(world, p11, [47.0, 46.5, 46.0])
    monkeypatch.delenv("BUYER_FLOOR", raising=False)
    assert not plan.chasing()
    monkeypatch.setenv("BUYER_FLOOR", "1")
    assert plan.chasing()
    plan.said_floor = True  # "that is our lowest price"
    assert not plan.chasing()


def test_approval_reply_is_not_a_flat_round(world, p11, monkeypatch):
    monkeypatch.setenv("BUYER_FLOOR", "1")
    plan = _plan_after(world, p11, [47.0, 46.5])
    plan.soft_reply = True
    plan.observe_reply([s["terms"] for s in _sheets(46.5)], "open")
    plan.observe_reply([s["terms"] for s in _sheets(46.5)], "open")
    assert not plan.holding() and plan.chasing()
    plan.soft_reply = False
    plan.observe_reply([s["terms"] for s in _sheets(46.5)], "open")
    plan.observe_reply([s["terms"] for s in _sheets(46.5)], "open")
    assert plan.holding() and not plan.chasing()  # real flat replies end it
    monkeypatch.delenv("BUYER_FLOOR", raising=False)
    plan.soft[-2:] = [True, True]
    assert plan.holding()  # unchanged when off


def test_words():
    assert APPROVAL_WORDS.search("this needs approval from our pricing committee")
    assert FLOOR_WORDS.search("That is rock bottom for us.") and FLOOR_WORDS.search("We cannot go lower.")
    assert not FLOOR_WORDS.search("Thank you for the offer.")


def test_quality_sheets_use_the_tightest_ppm(world, p11, monkeypatch):
    monkeypatch.delenv("BUYER_FLOOR", raising=False)
    off = ThreadState(world, p11, SID).plan
    assert off.quality_case and {p.combo["ppm_commitment"] for p in off.packages} >= {300, 800, 1500}
    monkeypatch.setenv("BUYER_FLOOR", "1")
    on = ThreadState(world, p11, SID).plan
    assert {p.combo["ppm_commitment"] for p in on.packages} == {300}
    on.observe_reply([s["terms"] for s in _sheets(47.0, ppm=1500)], "open")
    assert all(t["ppm_commitment"] == 300 for t in on.sheets(1))


def test_counter_is_at_most_one_of_its_steps_below_its_ask(world, p11, monkeypatch):
    plan = _plan_after(world, p11, [47.0, 46.5, 46.0])
    plan.last_sheets = [{"unit_price": 40.0, "ppm_commitment": 300, "chargeback_eur_per_complaint": 150}]
    monkeypatch.delenv("BUYER_FLOOR", raising=False)
    lo = plan.target_value(3)
    monkeypatch.setenv("BUYER_FLOOR", "1")
    hi = plan.target_value(3)
    assert hi >= lo
    # the jump is capped at 1.5 % of its opening above our last offer (twin P12: the uncapped one went 91 -> 98)
    cap = plan.last_value + 0.015 * plan.asks[0]
    assert hi >= min(plan.ask_value - (plan.asks[-2] - plan.asks[-1]), cap) - 1e-9


def _agent(world, case):
    agent = BuyerAgent(world)
    agent.start(case)
    agent.deadline = time.time() + 3600
    return agent


def _thread(prices):
    ev = [_reply(prices[0])]
    for p in prices[1:]:
        ev += [{"from": "buyer"}, _reply(p)]
    return ev


def test_floor_keeps_countering_a_stepping_supplier(world, p11, monkeypatch):
    ev = _thread([47.0, 46.5, 46.0])
    monkeypatch.setenv("BUYER_FLOOR", "1")
    agent = _agent(world, p11)
    ts = agent.t[SID]
    ts.sent, ts.k = 2, 2
    out = agent.decide({SID: ev}, SID)
    assert out["message"].get("term_sheets"), "a counter, not an accept"
    assert all(s["terms"]["ppm_commitment"] == 300 for s in out["message"]["term_sheets"])


def test_floor_chase_ends_at_the_last_message(world, p11, monkeypatch):
    ev = _thread([47.0, 46.5, 46.0])
    monkeypatch.setenv("BUYER_FLOOR", "1")
    agent = _agent(world, p11)
    ts = agent.t[SID]
    ts.sent, ts.k = 9, 5
    out = agent.decide({SID: ev}, SID)
    assert out["message"]["action"]["type"] == "accept"  # one message left: the chase is over, as today


def test_finalc_takes_the_cheaper_alternative(world, p11, monkeypatch):
    # a final offer: the standing sheet at 1500 ppm, an alternative at 300 ppm for a slightly higher price
    stand = {"unit_price": 46.0, "ppm_commitment": 1500, "chargeback_eur_per_complaint": 150}
    alt = {"unit_price": 46.2, "ppm_commitment": 300, "chargeback_eur_per_complaint": 150}
    sheets = [{"term_sheet_id": "q-a", "terms": stand}, {"term_sheet_id": "q-b", "terms": alt}]
    ev = [_reply(47.0), {"from": "buyer"}, _reply(46.0, "final_offer", sheets=sheets)]
    results = {}
    for flag in ("0", "1"):
        monkeypatch.setenv("BUYER_FINALC", flag)
        agent = _agent(world, p11)
        agent.t[SID].sent, agent.t[SID].k = 1, 1
        results[flag] = agent.decide({SID: ev}, SID)
    ts = agent.t[SID]
    c_stand, c_alt = (ts_cost(agent, ts, t) for t in (stand, alt))
    out = results["1"]
    if c_alt < c_stand:
        assert out["message"].get("term_sheets"), "alternative sent back exactly"
        assert out["message"]["term_sheets"][0]["terms"]["ppm_commitment"] == 300
        assert out["message"]["term_sheets"][0]["terms"]["unit_price"] == 46.2
    else:
        assert out["message"]["action"] == {"type": "accept", "term_sheet_id": "q-a"}


def ts_cost(agent, ts, terms):
    from buyer.strategy import cost

    return cost(agent.case, terms, ts.est)


@pytest.fixture(autouse=True)
def _strat_off(monkeypatch):
    """These tests cover the single switches / the case-level strategy; BUYER_STRAT (on by default) overrides them."""
    monkeypatch.setenv("BUYER_STRAT", "0")
