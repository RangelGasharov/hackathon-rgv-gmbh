"""A supplier that answers "agreed" to our sheet: accept it within our limit, never send a dearer one above it.

P08 (arena aa5ae846ff6981fc): ten "agreed" in a row while our price rose 6.08 -> 6.35, then it withdrew. Above the
limit the concession path stepped up (minimum step) instead of settling."""

import json
from pathlib import Path

import pytest

from buyer.agent import BuyerAgent
from buyer.strategy import cost
from buyer.dossier import World

SID = "sup:70710"


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def _case():
    return json.loads(Path("cases/real/P08.json").read_text())


def _msg(price, status, n):
    return {"from": "sales", "status": status, "body": "Our offer.",
            "term_sheets": [{"term_sheet_id": f"offer:{n}", "terms": {
                "unit_price": price, "payment_days": 30, "index_clause": None, "contract_months": 12}}]}  # fmt: skip


def _buyer(price):
    return {"from": "buyer", "body": "ours", "term_sheets": [{"terms": {
        "unit_price": price, "payment_days": 30, "index_clause": None, "contract_months": 12}}]}  # fmt: skip


def _decide(world, prices):
    """Thread: its open at 6.05, then our price / its "agreed" at that price, for each price."""
    agent = BuyerAgent(world)
    agent.start(_case())
    th = [_msg(6.05, "open", 0)]
    for i, p in enumerate(prices, 1):
        th += [_buyer(p), _msg(p, "agreed", i)]
    agent.ingest(SID, th)
    return agent, agent.decide({SID: th}, SID), th


def test_agreed_within_the_limit_is_accepted(world):
    _, dec, _ = _decide(world, [6.08])
    assert dec["message"]["action"] == {"type": "accept", "term_sheet_id": "offer:1"}


def test_agreed_over_the_limit_never_sends_a_dearer_sheet(world):
    prices = [6.08, 6.11, 6.14, 6.17, 6.20, 6.23, 6.26, 6.29]
    agent, dec, th = _decide(world, prices)
    ts = agent.t[SID]
    msg = (dec or {}).get("message") or {}
    for sheet in msg.get("term_sheets") or []:
        assert cost(agent.case, sheet["terms"], ts.est) <= ts.c_standing * 1.0005
        assert sheet["terms"]["unit_price"] <= 6.29


def test_agreed_far_over_the_limit_settles(world):
    prices = [6.08, 6.11, 6.14, 6.17, 6.20, 6.23, 6.26, 6.29, 6.32]
    _, dec, _ = _decide(world, prices)
    msg = dec["message"]
    assert msg.get("action", {}).get("type") in ("accept", "walk_away")
    assert not msg.get("term_sheets")


def _tender(world, name, **kw):
    """P05 (three bidders) with the first bidder's approach set to `name`."""
    from dataclasses import replace

    case = json.loads(Path("cases/real/P05.json").read_text())
    agent = BuyerAgent(world, deadline=None)
    agent.start(case)
    sid = case["suppliers"][0]
    ts = agent.t[sid]
    ts.plan.approach = replace(ts.plan.approach, name=name, leverage_ok=kw.get("leverage_ok", True))
    return agent, sid, ts


def _open(sid):
    return [{"from": "sales", "status": "open", "body": "Our offer.", "term_sheets": []}]


def test_tender_leverage_holds_after_agreed_quick_close_settles(world):
    from buyer.approach import LEVERAGE, QUICK_CLOSE

    agent, sid, ts = _tender(world, LEVERAGE)
    threads = {s: _open(s) for s in agent.t}
    for x in agent.t.values():
        x.c_standing = 100.0
    live = dict(agent.t)
    assert agent.tender_hold(threads, sid, live, 6, float("inf"))
    ts.agreed_holds = 1
    assert not agent.tender_hold(threads, sid, live, 6, float("inf"))  # once
    ts.agreed_holds = 0
    assert not agent.tender_hold(threads, sid, live, 2, float("inf"))  # no messages left: settle
    ts.plan.approach.name = QUICK_CLOSE
    assert not agent.tender_hold(threads, sid, live, 6, float("inf"))  # Quick-close keeps T-040's settle


def test_tender_holding_notes_by_approach(world):
    from buyer.approach import LEVERAGE, QUICK_CLOSE

    agent, sid, ts = _tender(world, LEVERAGE)
    ts.agreed_holds = 2
    assert agent.hold_again(ts, 6, float("inf"))  # Leverage: up to 3
    ts.agreed_holds = 3
    assert not agent.hold_again(ts, 6, float("inf"))
    ts.plan.approach.name = QUICK_CLOSE
    ts.agreed_holds = 1
    assert agent.hold_again(ts, 6, float("inf"))  # Quick-close: 2
    ts.agreed_holds = 2
    assert not agent.hold_again(ts, 6, float("inf"))
    ts.agreed_holds = 0
    assert not agent.hold_again(ts, 2, float("inf"))  # rounds are short


def test_single_supplier_never_holds_after_agreed(world):
    agent, _, _ = _decide(world, [6.08, 6.11])
    assert not agent.tender_hold({}, SID, {SID: agent.t[SID]}, 6, float("inf"))
