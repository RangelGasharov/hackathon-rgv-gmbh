"""BUYER_NIBBLE: one small counter below a close open offer before accepting it; then the best standing sheet."""

import json
from pathlib import Path

import pytest

from buyer.agent import BuyerAgent
from buyer.dossier import World

SID = "sup:70710"


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def _terms(price):
    return {"unit_price": price, "payment_days": 30, "index_clause": None, "contract_months": 12}


def _msg(price, status, n):
    return {"from": "sales", "status": status, "body": "Our offer.",
            "term_sheets": [{"term_sheet_id": f"offer:{n}", "terms": _terms(price)}]}  # fmt: skip


def _buyer(price):
    return {"from": "buyer", "body": "ours", "term_sheets": [{"terms": _terms(price)}]}


def _agent(world, case=None):
    agent = BuyerAgent(world)
    agent.start(case or json.loads(Path("cases/real/P08.json").read_text()))
    return agent


def _thread(prices, status="open"):
    """Its open at 6.05, then our price / its reply at that price, the last reply with `status`."""
    th = [_msg(6.05, "open", 0)]
    for i, p in enumerate(prices, 1):
        th += [_buyer(p), _msg(p, status if i == len(prices) else "open", i)]
    return th


def _nibble(agent, th, time_left=1000):
    agent.ingest(SID, th)
    return agent.nibble({SID: th}, SID, "offer:1", time_left, False)


def test_fires_on_an_open_offer(world, monkeypatch):
    monkeypatch.setenv("BUYER_NIBBLE", "1")
    agent = _agent(world)
    th = _thread([6.08])
    dec = _nibble(agent, th)
    sheet = dec["message"]["term_sheets"][0]["terms"]
    assert sheet == {**_terms(6.08), "unit_price": round(6.08 - 0.005 * 6.08, 2)}
    assert agent.t[SID].nibble == (6.08, sheet["unit_price"])
    assert any(x[1] == "nibble" for x in agent.log)
    assert _nibble(agent, th) is None  # once per thread


def test_half_last_step_when_larger(world, monkeypatch):
    monkeypatch.setenv("BUYER_NIBBLE", "1")
    agent = _agent(world)
    th = [_msg(6.50, "open", 0), _buyer(6.0), _msg(6.30, "open", 1)]
    dec = _nibble(agent, th)
    assert dec["message"]["term_sheets"][0]["terms"]["unit_price"] == 6.20  # 6.30 - 0.10


@pytest.mark.parametrize("status", ["final_offer", "agreed"])
def test_not_on_final_or_agreed(world, monkeypatch, status):
    monkeypatch.setenv("BUYER_NIBBLE", "1")
    agent = _agent(world)
    assert _nibble(agent, _thread([6.08], status)) is None


def test_not_in_a_tender(world, monkeypatch):
    monkeypatch.setenv("BUYER_NIBBLE", "1")
    case = json.loads(Path("cases/real/P05.json").read_text())
    agent = _agent(world, case)
    sid = case["suppliers"][0]
    th = [_msg(30.0, "open", 0), _buyer(29.0), _msg(29.5, "open", 1)]
    agent.ingest(sid, th)
    assert agent.nibble({sid: th}, sid, "offer:1", 1000, False) is None


def test_not_in_a_fair_price_case(world, monkeypatch):
    monkeypatch.setenv("BUYER_NIBBLE", "1")
    agent = _agent(world)
    monkeypatch.setattr(type(agent.t[SID].plan), "fair_price_case", property(lambda self: True))
    assert _nibble(agent, _thread([6.08])) is None


def test_not_on_the_last_message_or_the_last_seconds(world, monkeypatch):
    monkeypatch.setenv("BUYER_NIBBLE", "1")
    agent = _agent(world)
    assert _nibble(agent, _thread([6.08]), time_left=100) is None
    agent = _agent(world)
    th = _thread([6.08])
    agent.ingest(SID, th)
    agent.t[SID].sent = 99
    assert agent.nibble({SID: th}, SID, "offer:1", 1000, False) is None


def test_off_by_default(world, monkeypatch):
    monkeypatch.delenv("BUYER_NIBBLE", raising=False)
    agent = _agent(world)
    assert _nibble(agent, _thread([6.08])) is None


def test_after_a_hold_it_accepts_the_standing_sheet(world, monkeypatch):
    monkeypatch.setenv("BUYER_NIBBLE", "1")
    agent = _agent(world)
    th = _thread([6.08])
    assert _nibble(agent, th)
    # it holds at its price (open, same sheet), or sends a final offer at it: we sign its standing sheet
    for status in ("open", "final_offer"):
        th2 = th + [_buyer(6.05), _msg(6.08, status, 2)]
        agent.ingest(SID, th2)
        dec = agent.decide({SID: th2}, SID)
        assert dec["message"]["action"] == {"type": "accept", "term_sheet_id": "offer:2"}


def test_after_a_better_counter_it_accepts(world, monkeypatch):
    monkeypatch.setenv("BUYER_NIBBLE", "1")
    agent = _agent(world)
    th = _thread([6.08])
    assert _nibble(agent, th)
    th2 = th + [_buyer(6.05), _msg(6.06, "open", 2)]
    agent.ingest(SID, th2)
    dec = agent.decide({SID: th2}, SID)
    assert dec["message"]["action"] == {"type": "accept", "term_sheet_id": "offer:2"}
