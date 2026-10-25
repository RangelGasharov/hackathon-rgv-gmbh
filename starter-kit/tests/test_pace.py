"""BUYER_PACE: 2 claims in the first message with a sheet, then 1 fresh claim (weight >= 0.2); off: unchanged."""

import pytest

from buyer import flags
from buyer.claims import Claim, kind_key, ration


def claim(kind, ids, weight, series=None):
    return Claim(kind, ids, 1.0, f"{kind} ({', '.join(ids)}).", weight, meta={"series": series} if series else {})


POOL = [
    claim("fc", ["fc:1@2026-09"], 0.9),
    claim("sc", ["sc:9@2026Q3"], 0.8),
    claim("idx", ["idx:A@1", "idx:A@2"], 0.7, "A"),
    claim("sc_otd", ["sc:9@2026Q1"], 0.3),
    claim("news", ["news:1"], 0.1),
]


def test_switch_reads_the_environment(monkeypatch):
    monkeypatch.delenv("BUYER_PACE", raising=False)
    assert not flags.pace()
    monkeypatch.setenv("BUYER_PACE", "1")
    assert flags.pace()


def test_default_ration_is_unchanged():
    assert [c.kind for c in ration(POOL, set(), 0)] == ["fc", "sc", "idx"]
    assert ration(POOL, set(), 1, limit=3) == ration(POOL, set(), 1)  # the weak ones stay out after message 1


def test_first_message_takes_the_two_strongest():
    assert [c.kind for c in ration(POOL, set(), 0, limit=2, floor=0.2)] == ["fc", "sc"]


def test_later_messages_take_one_each_down_to_the_floor():
    used, heard, kinds = set(), set(), []
    for k in range(6):
        got = ration(POOL, used, k, heard, 2 if k == 0 else 1, 0.2)
        assert len(got) <= (2 if k == 0 else 1)
        for c in got:
            used |= set(c.ids)
            heard.add(kind_key(c))
            kinds.append(c.kind)
    assert kinds == ["fc", "sc", "idx"]  # sc_otd shares the scorecard kind, news (0.1) is below the floor


def test_floor_blocks_weak_claims_after_the_first_message():
    weak = [claim("news", ["news:1"], 0.1)]
    assert ration(weak, set(), 1, (), 1, 0.2) == []
    assert len(ration(weak, set(), 0, (), 2, 0.2)) == 1  # message 1 has no floor, as today


def test_p18_has_a_fresh_fact_in_messages_two_to_four(monkeypatch):
    """P18's pool is two index windows plus weak true facts (forecast +1.9 %, delivery record 90.3 %, order lines):
    message 1 takes the two windows, messages 2-4 one each of the others (live run: they cited none)."""
    import json
    import time
    from pathlib import Path

    from buyer.agent import BuyerAgent
    from buyer.dossier import World

    monkeypatch.setenv("BUYER_PACE", "1")
    sid = "sup:70740"
    case = json.loads(Path("cases/practice/P18.json").read_text())
    agent = BuyerAgent(World("data/package"))
    agent.start(case)
    agent.deadline = time.time() + 3600

    def reply(price):
        terms = {"unit_price": price, "payment_days": 30, "contract_months": 12, "index_clause": None}
        return {"from": "sales", "status": "open", "body": "Our offer.", "term_sheets": [{"term_sheet_id": f"q-{price}", "terms": terms}]}

    events, refs, kinds = [reply(6.10)], [], []
    for price in (6.08, 6.05, 6.02, 6.00):
        out = (agent.decide({sid: events}, sid) or {}).get("message") or {}
        assert out.get("term_sheets"), "a counter with a sheet"
        refs.append(out["refs"])
        kinds.append([c.kind for c in agent.t[sid].last_claims])
        events += [{"from": "buyer"}, reply(price)]
    assert kinds == [["idx", "idx"], ["fc"], ["sc_otd"], ["po"]]
    ids = sum(refs, [])
    assert len(ids) == len(set(ids))  # every ID once per case


@pytest.fixture(autouse=True)
def _strat_off(monkeypatch):
    """These tests cover the single switches / the case-level strategy; BUYER_STRAT (on by default) overrides them."""
    monkeypatch.setenv("BUYER_STRAT", "0")
