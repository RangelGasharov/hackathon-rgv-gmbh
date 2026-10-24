"""The buyer agent against the local twin and the kit's scripted supplier: no crashes, valid messages,
no broken must-haves, never the first offer, and better than the naive agent on the twin."""

import json
from pathlib import Path

import pytest

from arena_kit.agents import NaiveAgent
from arena_kit.cost import must_haves_ok
from arena_kit.play import RemoteTable
from arena_kit.play import play as kit_play
from buyer.claims import candidates, check
from buyer.dossier import Dossier, World
from buyer.runner import make_factory, play
from buyer.twin import LocalClient, make_twin

CASES = sorted(p.stem for p in Path("cases/practice").glob("P*.json"))
pytestmark = pytest.mark.skipif(not CASES, reason="run `python -m buyer.cases` first")


@pytest.fixture(scope="module")
def world():
    return World("data/package")


@pytest.fixture(scope="module")
def results(world):
    twin = make_twin(no_clock=True)
    client = LocalClient(twin)
    factory = make_factory(world)
    ours = {c: play(RemoteTable(client), f"case:{c}", factory) for c in CASES}
    naive = {c: kit_play(RemoteTable(LocalClient(twin)), f"case:{c}", NaiveAgent()) for c in CASES}
    return twin, ours, naive


def test_every_case_ends_and_scores(results):
    _, ours, _ = results
    for c, r in ours.items():
        assert r.get("score") is not None, c
        assert 0 <= r["score"] <= 1, c


def test_no_deal_breaks_a_must_have(results):
    _, ours, _ = results
    for c, r in ours.items():
        if r.get("deal"):
            case = json.loads(Path(f"cases/practice/{c}.json").read_text())
            assert must_haves_ok(case, r["terms"]), c


def test_never_accepts_the_first_offer(results):
    _, ours, _ = results
    for c, r in ours.items():
        log = r.get("agent_log") or []
        if log and log[0][1] == "accept":
            pytest.fail(f"{c} accepted before sending anything")


def test_beats_naive_on_the_twin(results):
    _, ours, naive = results
    mean = lambda rs: sum(r.get("score") or 0 for r in rs.values()) / len(rs)  # noqa: E731
    assert mean(ours) > mean(naive) + 0.3


def test_claims_are_true_and_never_support_a_rise(world):
    for c in CASES:
        case = json.loads(Path(f"cases/practice/{c}.json").read_text())
        d = Dossier(world, case, case["suppliers"][0])
        for claim in candidates(world, d, case):
            assert check(world, claim), (c, claim.sentence)
            if claim.kind == "idx" and d.letter and d.letter.get("asked_price") and d.current:
                asked = (d.letter["asked_price"] / d.current - 1) * 100
                assert claim.figure < 0, (c, claim.sentence)


def test_hedged_supplier_gets_no_index_argument(world):
    case = json.loads(Path("cases/practice/P14.json").read_text())
    d = Dossier(world, case, "sup:70560")
    assert d.hedge_until is not None
    assert all(c.kind != "idx" or c.weight < 0.2 for c in candidates(world, d, case))
