"""BUYER_STRAT (`buyer.approach`): one approach per supplier from its profile, the case and the rivals; corrected once;
news and rivals as leverage; each ask of its mail priced as a trade; at most 3 tactics."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from buyer import approach as appr
from buyer.agent import BuyerAgent, ThreadState
from buyer.claims import candidates
from buyer.dossier import Dossier, World
from buyer.writer import asks_for


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def _case(cid):
    return json.loads(Path(f"cases/practice/{cid}.json").read_text(encoding="utf-8"))


def _dossier(traits=(), news=(), price=10.0, full=8.0, trusted=False):
    return SimpleNamespace(
        has=lambda t, strong=False: t in traits,
        full_cost=(full, 0.1),
        min_margin=0.08,
        reference_price=price,
        letter=None,
        justified_pct=0.0,
        current_news=[(f"news:{i}", "2026-09-01", t, f"headline {t}") for i, t in enumerate(news)],
        relationship=(trusted, ["15 years"] if trusted else []),
        deals=None,
        standard_guess={},
    )


def _gives_way(n=6, asked=1.0, settled=-2.0):
    return {"negotiations": {"events": [{"asked_pct": asked, "settled_pct": settled}] * n}}


RAW = {"type": "raw_material", "suppliers": ["sup:1"], "parts": ["part:1"], "negotiable": ["unit_price", "index_clause"],
       "buyer": {"category_goal": "leverage"}}  # fmt: skip
LETTER = {"type": "price_increase", "suppliers": ["sup:1"], "parts": ["part:1"], "negotiable": ["unit_price"],
          "buyer": {"category_goal": "stabilise"}}  # fmt: skip
QUALITY = {"type": "quality", "suppliers": ["sup:1"], "parts": ["part:1"],
           "negotiable": ["unit_price", "ppm_commitment"], "term_options": {"ppm_commitment": [500, 1500]},
           "buyer": {"category_goal": "quality_first"}}  # fmt: skip


def test_choice_follows_its_record_the_case_and_room():
    lev = appr.choose(_gives_way(), RAW, {}, _dossier(price=10.0, full=8.0))
    assert lev.name == appr.LEVERAGE and 0.16 <= lev.opening_gap <= 0.19
    assert lev.switches == {"pace", "finalc"} and lev.sheets_n == 2
    keeps = appr.choose(_gives_way(asked=2.0, settled=2.0), LETTER, {}, _dossier(price=10.0, full=9.3), base_gap=0.05)
    assert keeps.name == appr.QUICK_CLOSE and 0.03 <= keeps.opening_gap <= 0.08 and keeps.switches == {"finalc"}
    chase = appr.choose(_gives_way(), QUALITY, {}, _dossier(price=10.0, full=8.0), base_gap=0.10)
    assert chase.name == appr.FLOOR_CHASE and chase.switches == {"floor", "finalc", "pace"} and chase.opening_gap >= 0.07
    # a capacity-tight supplier never gets the deep opening, whatever its record
    tight = appr.choose(_gives_way(), RAW, {}, _dossier(traits={"capacity_tight"}, price=10.0, full=7.0))
    assert tight.name != appr.LEVERAGE and not tight.leverage_ok


def test_rival_quotes_set_the_opening():
    alone = appr.choose({}, {**RAW, "suppliers": ["sup:1", "sup:2"]}, {}, _dossier(price=10.0, full=9.0))
    rival = appr.choose({}, {**RAW, "suppliers": ["sup:1", "sup:2"]}, {"sup:2": 8.5}, _dossier(price=10.0, full=9.0))
    assert rival.score > alone.score and "rival offers" in rival.reason
    assert rival.opening_gap >= alone.opening_gap


def test_news_that_weakens_it_deepens_the_opening(world):
    base = appr.choose(_gives_way(), RAW, {}, _dossier())
    weak = appr.choose(_gives_way(), RAW, {}, _dossier(news=["short_time_work"]))
    strong = appr.choose(_gives_way(), RAW, {}, _dossier(news=["full_order_book"]))
    assert weak.score > base.score > strong.score
    assert weak.opening_gap > base.opening_gap or weak.opening_gap == 0.19
    # and it is cited: weight 0.6 instead of 0.1 (P11's supplier restructures)
    d = Dossier(world, _case("P11"), "sup:70140")
    news = [c for c in candidates(world, d, _case("P11"), news_boost=True) if c.kind == "news"]
    assert news and news[0].weight == 0.6
    assert not [c for c in candidates(world, d, _case("P11")) if c.kind == "news"]  # 0.1: below the cut


def test_correction_once_after_two_replies():
    a = appr.choose(_gives_way(), RAW, {}, _dossier())
    assert appr.correct(a, [100.0, 99.0]) is None  # one reply only
    qc = appr.correct(a, [100.0, 99.0, 98.5])  # 1.5 % in total
    assert qc.name == appr.QUICK_CLOSE and qc.corrected and qc.switches == {"finalc"}
    assert appr.correct(qc, [100.0, 90.0, 89.0]) is None  # once
    fc = appr.choose(_gives_way(), QUALITY, {}, _dossier(), base_gap=0.10)
    lev = appr.correct(fc, [100.0, 92.0, 91.0])  # first drop 8 %
    assert lev.name == appr.LEVERAGE and lev.corrected


def test_correction_in_the_planner(world, monkeypatch):
    monkeypatch.setenv("BUYER_STRAT", "1")
    plan = ThreadState(world, _case("P18"), "sup:70740").plan
    terms = lambda p: {"unit_price": p, "contract_months": 12, "payment_days": 30, "index_clause": None}  # noqa: E731
    for p in (6.10, 6.08):
        assert plan.observe_reply([terms(p)], "open") is None
    change = plan.observe_reply([terms(6.07)], "open")
    assert change and change["to"] == appr.QUICK_CLOSE and plan.approach.corrected
    assert plan.observe_reply([terms(5.50)], "open") is None or plan.approach.name == appr.QUICK_CLOSE


def test_max_three_tactics_and_sheets_by_approach(world, monkeypatch):
    monkeypatch.setenv("BUYER_STRAT", "1")
    for cid in ("P02", "P05", "P08", "P10", "P11", "P18"):
        case = _case(cid)
        agent = BuyerAgent(world, use_profiles=True)
        agent.start(case, {})
        for ts in agent.t.values():
            a = ts.plan.approach
            assert a and len(a.tactics) <= appr.MAX_TACTICS
            assert ts.plan.anchor_gap == a.opening_gap
            assert len(ts.plan.sheets(0)) <= a.sheets_n


def test_zero_keeps_todays_behaviour(world, monkeypatch):
    monkeypatch.setenv("BUYER_STRAT", "0")
    plan = ThreadState(world, _case("P18"), "sup:70740").plan
    assert plan.approach is None and plan.anchor_gap == plan._base_gap() and plan.allows("probe")


def test_stale_tactic_is_dropped_not_swapped():
    a = appr.choose(_gives_way(), RAW, {}, _dossier())
    assert not appr.drop_stale(a, [100.0, 95.0, 94.99])  # it moved in the round before
    assert appr.drop_stale(a, [100.0, 95.0, 94.99, 94.98])
    assert a.dropped == [a.tactics[-1]] and len(a.tactics) == 3


def _thread(sid, price, body="Our offer.", status="open", n=1):
    return [{"from": "sales", "status": status, "body": body,
             "term_sheets": [{"term_sheet_id": f"offer:{sid[4:]}-{n}", "terms": {"unit_price": price}}]}]  # fmt: skip


def test_rival_price_held_until_it_holds(world, monkeypatch):
    monkeypatch.setenv("BUYER_STRAT", "1")
    case = _case("P05")
    threads = {"sup:70210": _thread("sup:70210", 36.0), "sup:70240": _thread("sup:70240", 30.0),
               "sup:70250": _thread("sup:70250", 33.0)}  # fmt: skip
    agent = BuyerAgent(world, use_profiles=True)
    agent.start(case, threads)
    sid = "sup:70210"
    agent.decide(threads, sid)
    kinds = lambda msg: {c.kind for c in msg["_plan"].claims}  # noqa: E731
    first = agent.compose(threads, sid, sheets=[{"unit_price": 30.0}])
    assert not kinds(first) & {"offer", "quote"}  # message 1: held back
    ts = agent.t[sid]
    for p in (35.9, 35.85):  # it holds: under 0.5 % a round
        ts.plan.observe_reply([{"unit_price": p}], "open")
    assert ts.plan.stalled()
    later = agent.compose(threads, sid, sheets=[{"unit_price": 30.5}])
    assert kinds(later) & {"offer", "quote"}


def test_asks_get_a_priced_trade(world, monkeypatch):
    monkeypatch.setenv("BUYER_STRAT", "1")
    assert asks_for("Could you consider faster payment? A binding volume would help us plan.") == ["payment", "volume"]
    assert asks_for("We would welcome a 36-month agreement.") == ["contract"]
    case = _case("P11")
    plan = ThreadState(world, case, "sup:70140").plan
    best = {"unit_price": 44.0, "payment_days": 60, "ppm_commitment": 500, "chargeback_eur_per_complaint": 150}
    trades = dict(plan.trade_sheets(["payment"], best))
    t = trades["payment"]
    days = best["payment_days"] - t["payment_days"]
    assert days > 0 and t["unit_price"] < best["unit_price"] * (1 - appr.DAY_VALUE * days)  # > 0.0222 %/day
    assert plan.trade_sheets(["payment"], {**best, "payment_days": 14}) == []  # nothing faster to give
    # end to end: every ask of its mail is answered in our next message, as a priced sheet or as already priced in
    body = "Could you commit to a binding volume and a longer contract? Faster payment would also help."
    threads = {"sup:70410": _thread("sup:70410", 33.6, body=body)}
    agent = BuyerAgent(world, use_profiles=True)
    agent.start(_case("P10"), threads)
    plan = agent.decide(threads, "sup:70410")["message"]["_plan"]
    assert agent.t["sup:70410"].asks == ["payment", "volume", "contract"]
    for head in ("On faster payment", "On volume", "On the term"):
        assert any(x.startswith(head) for x in plan.levers), head
    assert len(plan.sheets) <= 3
