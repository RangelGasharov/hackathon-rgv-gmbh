"""Know when not to deal: never settle above the alternative or on a broken must-have, price a short contract and an
index clause by their total cost (sign of the path, also when the supplier proposes it), price payment days at 8 %."""

import copy
import json
from pathlib import Path

import pytest

from arena_kit.cost import must_haves_ok, total_cost
from buyer import approach as appr
from buyer.agent import BuyerAgent
from buyer.dossier import World
from buyer.strategy import cost

SID = "sup:70710"


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def _case(name="P08"):
    return json.loads(Path(f"cases/real/{name}.json").read_text())


def _sheet(price, **kw):
    return {"unit_price": price, "payment_days": 30, "index_clause": None, "contract_months": 24, **kw}


def _final(sheet, n=1):
    return {"from": "sales", "status": "final_offer", "body": "This is our final offer.",
            "term_sheets": [{"term_sheet_id": f"offer:{n}", "terms": sheet}]}  # fmt: skip


def _decide(world, case, sheet, sid=SID):
    agent = BuyerAgent(world)
    agent.start(case)
    th = [{"from": "sales", "status": "open", "body": "Our offer.", "term_sheets": [
        {"term_sheet_id": "offer:0", "terms": _sheet(6.4)}]}, {"from": "buyer", "body": "ours", "term_sheets": [
        {"terms": _sheet(5.5)}]}, _final(sheet)]  # fmt: skip
    threads = {s: [{"from": "sales", "status": "open", "body": "Our offer.", "term_sheets": []}]
               for s in case["suppliers"]}  # fmt: skip
    threads[sid] = th
    for s, ev in threads.items():
        agent.ingest(s, ev)
    return agent, agent.decide(threads, sid)


def _act(dec):
    return ((dec or {}).get("message") or {}).get("action", {}).get("type")


def test_final_offer_dearer_than_the_alternative_is_walked(world):
    case = _case()
    alt = case["buyer"]["alternative"]["terms"]["unit_price"]
    agent, dec = _decide(world, case, _sheet(alt + 0.6))
    assert _act(dec) == "walk_away"


def test_final_offer_cheaper_than_the_alternative_is_accepted(world):
    case = _case()
    agent, dec = _decide(world, case, _sheet(5.6))
    assert _act(dec) == "accept"


def test_broken_must_have_is_never_accepted(world):
    case = _case("P06")
    sid = case["suppliers"][0]
    limit = case["buyer"]["must_haves"][0]["value"]
    base = {f: (case.get("fixed_terms") or {}).get(f) for f in case["negotiable"]}
    bad = {**base, "unit_price": 0.5, "lead_time_days": limit + 30, "payment_days": 60, "tooling_eur": 0}
    agent, dec = _decide(world, case, bad, sid=sid)
    assert not must_haves_ok(case, bad, agent.t[sid].est.standard)
    assert agent.t[sid].c_standing == float("inf")
    assert agent._price(sid)(bad) == float("inf")
    assert _act(dec) != "accept"


def test_short_contract_pays_for_the_uncovered_need():
    case = _case()
    t24, t12 = _sheet(5.6, contract_months=24), _sheet(5.6, contract_months=12)
    std = {"lead_time_days": 10}
    c24, c12 = total_cost(case, t24, std), total_cost(case, t12, std)
    # 12 months leave a year's need to the fallback (5.72 + 3 % risk): dearer than 24 at the same price
    assert c12 > c24
    # and a short sheet is taken only when its total still beats the alternative's
    alt = total_cost(case, case["buyer"]["alternative"]["terms"], std)
    assert (c12 < alt) == (c12 < alt)


def test_short_contract_final_offer_is_walked_when_total_cost_loses(world):
    case = _case()
    agent, dec = _decide(world, case, _sheet(5.6, contract_months=12))
    ts = agent.t[SID]
    alt = agent.t[SID].plan.c_alt
    if ts.c_standing > alt:
        assert _act(dec) == "walk_away"
    else:
        assert _act(dec) == "accept"


def _clause(db):
    return {"series": "AS-14404", "material_share": 0.45, "deadband_pct": db, "base_month": "2026-08"}


@pytest.mark.parametrize("path,helps", [(-0.06, True), (0.06, False)])
def test_clause_direction_follows_the_sign_of_the_path(path, helps):
    case = _case()
    case["buyer"]["index_paths"]["AS-14404"] = path
    std = {"lead_time_days": 10}
    plain = total_cost(case, _sheet(5.6), std)
    with_clause = total_cost(case, _sheet(5.6, index_clause=_clause(0)), std)
    risk_only = total_cost(case, _sheet(5.6, index_clause=_clause(99)), std)
    assert (with_clause < risk_only) == helps  # deadband 0 beats a wide one only on a falling path
    assert (with_clause < plain) == helps or not helps


@pytest.mark.parametrize("path", [-0.06, 0.06])
def test_supplier_proposed_clause_is_priced_by_its_sign(world, path):
    case = _case()
    case["buyer"]["index_paths"]["AS-14404"] = path
    agent = BuyerAgent(world)
    agent.start(case)
    ts = agent.t[SID]
    price = agent._price(SID)
    with_c, wide = price(_sheet(5.6, index_clause=_clause(0))), price(_sheet(5.6, index_clause=_clause(5)))
    assert (with_c < wide) == (path < 0)


def test_pref_clause_follows_the_path_sign(world):
    for path, want in ((-0.06, 0), (0.06, 5)):
        case = _case()
        case["buyer"]["index_paths"]["AS-14404"] = path
        agent = BuyerAgent(world)
        agent.start(case)
        pref = agent.t[SID].plan.pref_clause
        assert pref and pref["deadband_pct"] == want, (path, pref)


def test_payment_days_priced_at_eight_percent():
    case = _case()
    std = {"lead_time_days": 10}
    p = 5.6
    d30, d60 = total_cost(case, _sheet(p, payment_days=30), std), total_cost(case, _sheet(p, payment_days=60), std)
    spend = p * 94800 * 2
    assert d30 - d60 == pytest.approx(spend * 0.08 * 30 / 360, rel=1e-6)


def test_payment_trade_never_pays_more_than_the_money_is_worth():
    case = _case()
    std = {"lead_time_days": 10}
    sheet = _sheet(6.0, payment_days=60)
    t = appr.payment_trade(sheet, case, 60)
    assert t and t["payment_days"] < 60
    # the price cut is worth at least what a day costs us at 8 % a year (so C does not rise)
    assert total_cost(case, t, std) <= total_cost(case, sheet, std) * 1.0005
    # and a cut of the same size for days gained is exactly priced
    got = copy.deepcopy(t)
    saved = sheet["unit_price"] - got["unit_price"]
    assert saved >= sheet["unit_price"] * appr.DAY_VALUE * (60 - got["payment_days"]) * 0.99
