"""No timeout by our doing: a slow writer, a busy sibling thread, the last message and the wave cap."""

import json
import time
from pathlib import Path

import pytest

from buyer import agent as agent_mod
from buyer.agent import BuyerAgent, round_cap
from buyer.dossier import World


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def _case(cid):
    return json.loads(Path(f"cases/real/{cid}.json").read_text())


def _reply(price, status="open", tid="q"):
    return {"from": "sales", "status": status, "body": "Our offer.",
            "term_sheets": [{"term_sheet_id": f"{tid}-{price}", "terms": {"unit_price": price}}]}  # fmt: skip


def _rfq(world):
    case = _case("P05")
    agent = BuyerAgent(world)
    agent.start(case)
    sids = list(case["suppliers"])
    threads = {s: [_reply(30.0 + i, tid=s)] for i, s in enumerate(sids)}
    for s in sids:
        agent.ingest(s, threads[s])
    return agent, case, threads, sids


class SlowWriter:
    def __init__(self, seconds):
        self.seconds = seconds

    def write(self, plan, timeout=None):  # ignores its timeout, like a hung connection
        time.sleep(self.seconds)
        return "late"


def test_slow_writer_is_replaced_by_the_template_in_time(world, monkeypatch):
    monkeypatch.setattr(agent_mod, "HARD_WRITE_S", 0.5)
    agent, case, threads, sids = _rfq(world)
    agent.writer = SlowWriter(5)
    msg = agent.decide(threads, sids[0])["message"]
    sheets = [t["terms"] for t in msg["term_sheets"]]
    t0 = time.monotonic()
    out = agent.write(msg, budget=40.0)
    assert time.monotonic() - t0 < 2.0
    assert out["body"] and out["body"] != "late"
    assert [t["terms"] for t in out["term_sheets"]] == sheets  # the same sheets go out
    assert any(e[1] == "write_fallback" for e in agent.log)


def test_failing_writer_falls_back_to_the_template(world):
    class Boom:
        def write(self, plan, timeout=None):
            raise RuntimeError("API down")

    agent, case, threads, sids = _rfq(world)
    agent.writer = Boom()
    out = agent.write(agent.decide(threads, sids[0])["message"], budget=40.0)
    assert out["body"]


def test_a_waiting_bidder_gets_a_note_while_another_is_busy(world):
    agent, case, threads, sids = _rfq(world)
    agent.t[sids[0]].in_flight = True  # one thread is busy
    note = agent.keep_alive(threads, sids[1])  # the idle one is not left silent (no switch needed)
    assert note and note["supplier_id"] == sids[1] and "action" not in note["message"]
    assert note["message"]["term_sheets"] == []


def test_the_last_message_is_a_close_not_a_note(world):
    agent, case, threads, sids = _rfq(world)
    agent.t[sids[1]].sent = round_cap(case) - 1
    assert agent.keep_alive(threads, sids[1]) is None  # the last message is for a decision
    d = agent.close_now(threads, sids[1])
    assert d and d["message"]["action"]["type"] in ("accept", "walk_away")


@pytest.mark.parametrize("floor", ["0", "1"])
def test_no_messages_left_closes_even_when_chasing(world, monkeypatch, floor):
    monkeypatch.setenv("BUYER_FLOOR", floor)
    case = _case("P01")
    agent = BuyerAgent(world)
    agent.start(case)
    sid = next(iter(case["suppliers"]))
    threads = {sid: [_reply(138.55, tid=sid)]}
    agent.ingest(sid, threads[sid])
    agent.t[sid].sent = agent.t[sid].k = round_cap(case)
    d = agent.decide(threads, sid)
    assert d is None or "action" in d["message"] or not d["message"].get("term_sheets")
    if d and "action" in d["message"]:
        assert d["message"]["action"]["type"] in ("accept", "walk_away")


def test_wave_cap_closes_even_with_a_reply_in_flight(world):
    agent, case, threads, sids = _rfq(world)
    agent.deadline = time.time() + 20  # inside close_margin
    for s in sids:
        agent.t[s].k = 2
        agent.t[s].in_flight = True
    d = agent.decide(threads, sids[0])
    assert d and d["message"]["action"]["type"] in ("accept", "walk_away")
