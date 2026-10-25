"""Total cost, not price: every lever that moves C is priced and in the package grid, and a tender is awarded on C."""

import json
from pathlib import Path

import pytest

from arena_kit.cost import total_cost
from buyer.agent import BuyerAgent
from buyer.dossier import World
from buyer.strategy import grid


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def _case(name):
    return json.loads(Path(f"cases/practice/{name}.json").read_text())


def _sheet(price, **kw):
    return {"unit_price": price, "payment_days": 30, "lead_time_days": 42, "incoterm": "DAP", "tooling_eur": 0, **kw}


def test_grid_offers_every_option_of_every_negotiable_term():
    for name in ("P05", "P09", "P08", "P11", "P03"):
        case = _case(name)
        combos = list(grid(case, {}))
        for f in case["negotiable"]:
            if f == "unit_price":
                continue
            seen = {c[f] if not isinstance(c[f], dict) else json.dumps(c[f], sort_keys=True) for c in combos}
            want = {o if not isinstance(o, dict) else json.dumps(o, sort_keys=True) for o in case["term_options"][f]}
            assert want <= seen, (name, f)


def test_cost_prices_freight_lateness_and_tooling():
    case = _case("P05")
    base = total_cost(case, _sheet(30.0, lead_time_days=case["buyer"]["need_lead_time_days"]))
    n = case["buyer"]["need_per_year"] * case["buyer"]["horizon_years"]
    exw = total_cost(case, _sheet(30.0, incoterm="EXW", lead_time_days=case["buyer"]["need_lead_time_days"]))
    assert exw - base == pytest.approx(n * case["buyer"]["freight_eur_per_unit"]["EXW"], abs=1)
    tool = total_cost(case, _sheet(30.0, tooling_eur=6000, lead_time_days=case["buyer"]["need_lead_time_days"]))
    assert tool - base == pytest.approx(6000, abs=1)
    late = total_cost(case, _sheet(30.0, lead_time_days=case["buyer"]["need_lead_time_days"] + 14))
    assert late >= base


def test_quality_levers_ppm_and_chargeback_are_priced():
    case = _case("P11")
    b = case["buyer"]
    o = case["term_options"]
    ppm, cb = max(o["ppm_commitment"]), max(o["chargeback_eur_per_complaint"])
    lo = total_cost(case, {"unit_price": 10.0, "ppm_commitment": min(o["ppm_commitment"]), "chargeback_eur_per_complaint": cb})
    hi = total_cost(case, {"unit_price": 10.0, "ppm_commitment": ppm, "chargeback_eur_per_complaint": 0})
    assert b["cost_per_defect"] > 0 and hi > lo


def test_tender_is_awarded_on_total_cost_not_unit_price(world):
    case = _case("P05")
    agent = BuyerAgent(world)
    agent.start(case)
    a, b, c = case["suppliers"]
    cheap_price_exw = _sheet(30.00, incoterm="EXW")  # lowest price, freight 1.31 a unit on top
    dear_price_dap = _sheet(30.60, incoterm="DAP")  # 0.60 dearer, no freight: cheaper in C
    threads = {
        a: [{"from": "sales", "status": "open", "body": "o", "term_sheets": [{"term_sheet_id": "o:a", "terms": cheap_price_exw}]}],
        b: [{"from": "sales", "status": "open", "body": "o", "term_sheets": [{"term_sheet_id": "o:b", "terms": dear_price_dap}]}],
        c: [{"from": "sales", "status": "open", "body": "o", "term_sheets": []}],
    }
    for s, ev in threads.items():
        agent.ingest(s, ev)
    dec = agent.close_now(threads, a)
    action = dec["message"]["action"]
    assert dec["supplier_id"] == b and action["type"] == "accept" and action["term_sheet_id"] == "o:b"
