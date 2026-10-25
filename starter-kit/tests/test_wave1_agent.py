"""Wave 1a message and decision fixes: B1 promise, B2 holds after a final offer, B3 boilerplate, A3 earlier cheaper
sheet at a final offer, C7 ask sentences, C8 weak floor words."""

import json
from pathlib import Path

import pytest

from buyer.agent import ASKS_DATA, ASKS_VOLUME, WEAK_FLOOR, BuyerAgent, requests_of
from buyer.dossier import World

SID = "sup:70710"


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def _agent(world):
    agent = BuyerAgent(world)
    agent.start(json.loads(Path("cases/real/P08.json").read_text()))
    return agent


def _sheet(price, n=0):
    return {"term_sheet_id": f"offer:{n}", "terms": {
        "unit_price": price, "payment_days": 30, "index_clause": None, "contract_months": 12}}  # fmt: skip


def _msg(price, status="open", n=0, body="Our offer."):
    return {"from": "sales", "status": status, "body": body, "term_sheets": [_sheet(price, n)]}


def _mine(price):
    return {"from": "buyer", "body": "ours", "term_sheets": [{"terms": _sheet(price)["terms"]}]}


def test_b1_promise_only_when_an_agreeing_reply_would_be_accepted(world):
    agent = _agent(world)
    ts = agent.t[SID]
    ts.sent = 2
    sheet = _sheet(6.08)["terms"]
    ts.promise = None
    assert "clear answer" in " ".join(agent.levers(ts, [sheet]))
    ts.promise = {"cap": 1e9, "rival": float("inf"), "hold": False, "rel": False}
    assert "we accept it in our next message" in " ".join(agent.levers(ts, [sheet]))
    ts.promise = {"cap": 1e9, "rival": float("inf"), "hold": True, "rel": False}  # another bidder still to hear
    assert "we accept it" not in " ".join(agent.levers(ts, [sheet]))
    ts.promise = {"cap": 0.0, "rival": float("inf"), "hold": False, "rel": False}  # dearer than our limit
    assert "we accept it" not in " ".join(agent.levers(ts, [sheet]))


def test_b2_no_hold_after_a_final_offer_without_a_pending_bidder(world):
    agent = _agent(world)
    th = [_msg(6.05), _mine(6.0), _msg(6.30, "final_offer", 1)]
    agent.ingest(SID, th)
    dec = agent.decide({SID: th}, SID)
    levers = " ".join(((dec or {}).get("message") or {}).get("_plan").levers) if dec and "_plan" in dec.get("message", {}) else ""
    assert "reviewing all offers" not in levers
    assert agent.t[SID].final_holds == 0


def test_b3_standing_commitments_are_said_once_per_thread(world):
    agent = _agent(world)
    ts = agent.t[SID]
    assert agent.once(ts, "We will pay every invoice on the agreed due date.") == [
        "We will pay every invoice on the agreed due date."
    ]
    assert agent.once(ts, "We will pay every invoice on the agreed due date.") == []
    first = agent.once(ts, "We can pay within 30 days, which should help your cash flow.", key="cash_flow")
    assert first and agent.once(ts, "We can pay within 20 days, which should help your cash flow.", key="cash_flow") == []


def test_a3_earlier_cheaper_sheet_is_sent_back_once_at_a_final_offer(world):
    agent = _agent(world)
    th = [_msg(6.05), _mine(6.0), _msg(6.10, "open", 1), _mine(6.0), _msg(6.30, "final_offer", 2)]
    agent.ingest(SID, th)
    ts = agent.t[SID]
    got = agent.earlier_cheaper(th, ts, float("inf"), False)
    assert got and got["unit_price"] == 6.05
    dec = agent.decide({SID: th}, SID)
    sent = dec["message"].get("term_sheets") or []
    assert [t["terms"]["unit_price"] for t in sent] == [6.05]
    assert ts.took_earlier
    assert agent.earlier_cheaper(th, ts, 0.0, False) is None  # nothing within the limit


def test_c7_only_request_sentences_count():
    for ack in (
        "We checked the data you cited; it does not change our calculation.",
        "The volume schedule you provided is noted.",
        "Thank you for the data you attached.",
    ):
        assert not ASKS_DATA.search(requests_of(ack)) and not ASKS_VOLUME.search(requests_of(ack).lower())
    assert ASKS_DATA.search(requests_of("Please send us the data behind your target price."))
    assert ASKS_VOLUME.search(requests_of("Can you send the volume schedule?").lower())
    assert requests_of("Please provide proof of your payment record.")


def test_c7_payment_proof_gets_our_record(world):
    agent = _agent(world)
    ts = agent.t[SID]
    ts.sent, ts.last_body = 1, "Can you give us proof of your payment reliability?"
    assert any("payment record" in a for a in agent.answers(ts, [_sheet(6.0)["terms"]], []))
    ts.last_body = "Thank you for your offer."
    assert not any("payment record" in a for a in agent.answers(ts, [_sheet(6.0)["terms"]], []))


def test_c8_weak_floor_words_need_a_mail_without_a_price_move(world):
    for text in ("We cannot give up any margin right now.", "Our flexibility remains severely limited.",
                 "This limits our ability to reduce the price."):  # fmt: skip
        assert WEAK_FLOOR.search(text)
    agent = _agent(world)
    weak = "We cannot give up margin right now."
    th = [_msg(6.05), _mine(6.0), _msg(6.05, "open", 1, weak)]
    agent.ingest(SID, th)
    assert agent.t[SID].plan.said_floor and agent.t[SID].plan.soft_reply
    agent2 = _agent(world)
    th = [_msg(6.05), _mine(6.0), _msg(5.95, "open", 1, weak)]  # it moved: no signal
    agent2.ingest(SID, th)
    assert not agent2.t[SID].plan.said_floor
