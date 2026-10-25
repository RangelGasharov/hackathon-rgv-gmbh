"""Wave 1b: input bugs and estimate fixes (ANALYSIS.md C1-C5, C11, A5, A6, A1-open)."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from arena_kit.sim import load_cases
from buyer import approach as appr
from buyer import claims
from buyer.dossier import TRAITS, Dossier, World, hedge_end
from buyer.profiles import facts
from buyer.strategy import Estimate, Planner

CASES = load_cases(Path("cases/real"))


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def test_c1_schedule_is_per_price_unit(world):
    case = CASES["case:P14"]  # one part priced per 100
    d = Dossier(world, case, "sup:70560")
    csv = d.schedule_csv().splitlines()
    unit = world.one("SELECT price_unit FROM parts WHERE id=?", case["parts"][0])[0]
    assert csv[0].endswith(f"forecast_qty_per_{unit}_units")
    raw = world.all(
        "SELECT qty FROM forecast_lines WHERE fc_id=(SELECT id FROM forecasts WHERE part_id=? "
        "ORDER BY snapshot_month DESC LIMIT 1) ORDER BY target_month",
        case["parts"][0],
    )
    assert float(csv[1].split(",")[-1]) == pytest.approx(raw[0][0] / unit, abs=0.06)


def test_c2_hedge_wording_of_the_release_letter():
    text = (
        "we have secured our steel sheet and coil for 2027, bought forward at fixed prices with our mill. "
        "Whatever the steel market does reaches our prices only from 2028."
    )
    assert re.search(TRAITS["hedged_material"][0], text)
    assert hedge_end(text).year == 2027
    assert hedge_end("Material covered until March 2027.").month == 3
    assert hedge_end("Material covered until May 2026").month == 5
    assert not re.search(TRAITS["hedged_material"][0], "Order intake is very strong")


def test_c3_old_record_is_folded_under_the_live_id(world):
    assert world.ids("sup:70210") == ["sup:70210", "sup:71999"]
    assert world.ids("sup:70110") == ["sup:70110"]
    w = facts(world, "sup:70210")["our_weight"]
    assert w["spend_trend_pct"] < 0  # was +8 % on the live record alone
    assert w["spend_eur_by_year"]["FY2024"] > 340_000


def test_c4_the_free_contract_give_is_the_longest_option_whatever_the_horizon():
    """Months beyond the horizon neither cost nor earn (C counts min(M/12, H) years), the supplier values them."""
    case = {"term_options": {"contract_months": [12, 24, 36]}, "negotiable": ["contract_months"],
            "buyer": {"horizon_years": 2}}
    assert appr.free_trade({"contract_months": 12}, case, "contract_months")["contract_months"] == 36
    assert appr.free_trade({"contract_months": 36}, case, "contract_months") is None


def test_c5_news_claim_cites_the_newest_weak_item(world):
    assert "customer_loss" in claims.WEAK_NEWS and "restructuring" in claims.WEAK_NEWS
    assert "energy_contract" not in claims.WEAK_NEWS
    case = CASES["case:P07"]
    d = Dossier(world, case, "sup:70190")  # customer_loss (newest, weak) and an older energy_contract (not weak)
    assert [t for _i, _d, t, _h in d.current_news][:1] == ["customer_loss"]
    out = [c for c in claims.candidates(world, d, case, news_boost=True) if c.kind == "news"]
    assert out and out[0].ids == ["news:20261015-S14"] and out[0].weight >= 0.6


def test_c11_stale_condition_orders_are_skipped(world):
    row = world.one("SELECT supplier_id, part_id FROM po_lines WHERE price_note='stale_condition' AND part_id='part:310010'")
    case = next(c for c in CASES.values() if c["parts"][0] == row["part_id"])
    d = Dossier(world, case, row["supplier_id"])
    stale = {r[0] for r in world.all("SELECT id FROM po_lines WHERE price_note='stale_condition'")}
    assert not stale & {i for i, _p in d.recent_orders}


def test_c11_share_falls_back_to_the_range_midpoint():
    profile = {"our_weight": {"share_of_its_turnover_pct": None, "share_of_its_turnover_range_pct": [6.0, 14.0]}}
    a = appr.choose(profile, CASES["case:P01"])
    assert "we are 10.0 % of its turnover" in a.reason
    profile["our_weight"]["share_of_its_turnover_range_pct"] = [0.2, 0.6]
    assert "we are only 0.4 % of its turnover" in appr.choose(profile, CASES["case:P01"]).reason


def test_a5_tight_estimate_keeps_a_narrow_spread(world):
    """A point estimate matched the arena's charge better but lost P01 on the persona twin: the narrow spread stays."""
    est = Estimate(Dossier(world, CASES["case:P01"], "sup:70110"))
    if est.err <= 0.05:
        spread = [f / est.full_cost for _, f in est.f_nodes()]
        assert len(spread) == 5 and max(spread) - min(spread) < 0.06
    wide = Estimate(Dossier(world, CASES["case:P06"], "sup:70150"))
    assert wide.err > 0.05 and len(wide.f_nodes()) == 5


def test_a6_clause_slack_is_tied_to_the_room(world):
    case = CASES["case:P02"]
    est = Estimate(Dossier(world, case, "sup:70510"))
    plan = Planner(case, est)
    assert plan._clause_slack(1000.0) == pytest.approx(12.0)  # no opening seen yet: 1.2 % of C
    plan.ask_sheets = [[{"unit_price": 1e9}]]
    assert 0 < plan._clause_slack(1000.0) <= 12.0
    room = plan.ask_sheets[0][0]
    opened = min(__import__("buyer.strategy", fromlist=["cost"]).cost(case, t, est) for t in [room])
    assert plan._clause_slack(opened - 10.0) <= 4.0 + 1e-9  # a 10 room: at most 40 % of it
    plan.ask_sheets = [[]]
    assert plan._clause_slack(1000.0) == pytest.approx(12.0)


def test_a1_tender_ignores_a_rival_driven_first_drop(world):
    case = json.loads(json.dumps(CASES["case:P05"]))
    est = Estimate(Dossier(world, case, "sup:70250"))
    plan = Planner(case, est)
    plan.asks = [100.0, 85.0, 84.0]  # a 15 % round-1 drop in a tender
    plan.est.err = 0.5  # no own-cost floor to fall back on
    assert plan.f_hat is None
    plan.asks = [100.0, 97.0, 96.0]
    assert plan.f_hat is not None
