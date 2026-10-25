"""The relationship charge in the agent: what it counts as the supplier's margin (cash only), how it believes
the supplier's full cost (an expectation, capped by the opening quote), and an RFQ race that signed the cheap
bidder's opening quote. Checked against the real deals in runs/arena-P0*.json where they exist."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arena_kit.cost import closing_terms, relationship_cost
from arena_kit.sim import load_cases
from buyer.agent import BuyerAgent
from buyer.dossier import Dossier, World
from buyer.strategy import Estimate, Planner, cost, rel_charge
from buyer.twin_real import LocalClient, make_twin_real

CASES = load_cases(Path("cases/real"))


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def estimate(world, cid, sup):
    case = CASES[cid]
    return case, Estimate(Dossier(world, case, sup))


def test_security_terms_are_not_cash(world):
    case, est = estimate(world, "case:P03", "sup:70530")
    est.d.traits["free_capacity"] = "strong"
    est.d.traits["capacity_tight"] = "strong"
    est.wants_clause = True
    long = {"unit_price": 430.0, "contract_months": 36, "index_clause": {"series": "X", "material_share": 0.2}}
    assert est.extra(long) > 0  # it signs for them ...
    assert est.extra(long, cash=True) == 0  # ... but its margin does not count them (cost.py)
    short = {"unit_price": 430.0, "contract_months": 12, "lead_time_days": 10, "ppm_commitment": 1}
    assert est.extra(short, cash=True) == est.extra(short)  # a faster delivery is real money either way


def test_charge_matches_the_real_deals(world):
    """The arena's relationship charge of 17 real deals (P01, P02, P03), as the agent prices it from the supplier's
    own-plant should-cost (the point estimate; `rel_charge` adds the expectation over our error). With the
    contract-length and clause credit it was 0 for the 24 and 36 month P03 deals and half the P02 clause charge."""
    runs = sorted(Path("runs").glob("arena-P0[123]-*.json"))
    if not runs:
        pytest.skip("no saved real sessions")
    checked = 0
    for f in runs:
        res = json.loads(f.read_text())["state"]["result"]
        if not res.get("terms"):
            continue
        case = CASES[json.loads(f.read_text())["state"]["case_id"]]
        est = Estimate(Dossier(world, case, res["supplier_id"]))
        est.wants_clause = True
        t = closing_terms(case, res["terms"], est.standard)
        mine = relationship_cost(case, t, supplier=est.model(case, t, cash=True))  # the point estimate
        real = res["cost_breakdown"].get("relationship", 0)
        assert mine == pytest.approx(real, abs=1500, rel=0.25), (f.name, mine, real)
        checked += 1
    assert checked >= 10


def test_belief_is_capped_by_the_opening_quote(world):
    case, est = estimate(world, "case:P06", "sup:70150")
    assert est.err > 0.05  # a generic should-cost
    nodes = est.f_nodes()
    assert len(nodes) == 5 and sum(w for w, _ in nodes) == pytest.approx(1)
    assert max(f for _, f in nodes) > est.full_cost  # an error band both ways
    est.open_value = 150.84
    cap = 150.84 * (1 - case["buyer"]["sustainable_margin_pct"] / 100)
    assert max(f for _, f in est.f_nodes()) <= cap + 1e-9
    assert min(f for _, f in est.f_nodes()) < est.full_cost  # only a cap


def test_squeezing_costs_more_than_it_saves_on_a_generic_estimate(world):
    """P06: a flat 10 % weight on the charge let the agent offer 138.7 (5 % under the full cost); the expected
    charge puts the cheapest price well above that."""
    case, est = estimate(world, "case:P06", "sup:70150")
    est.open_value = 154.13
    plan = Planner(case, est)
    plan.observe_reply([{"unit_price": 150.84, "payment_days": 60, "lead_time_days": 70}], "open")
    cheap = {"unit_price": 138.7, "payment_days": 14, "lead_time_days": 70, "contract_months": 24}
    fair = {**cheap, "unit_price": 145.0}
    assert cost(case, cheap, est) > cost(case, fair, est)
    assert rel_charge(case, cheap, est) > 50_000
    best = plan.packages[0]
    assert best.p_opt > 140.0


def test_rfq_does_not_sign_the_best_bidder_before_negotiating(world):
    """Under load the expensive bidder's thread decided twice before the cheap bidder's thread decided once, and
    the second decision settled on the cheap bidder's opening quote (P06 scored 0 in 6 of 24 identical runs)."""
    twin = make_twin_real()
    opened = LocalClient(twin).open_session("case:P06")
    threads = {sid: list(ev) for sid, ev in opened["threads"].items()}
    agent = BuyerAgent(world)
    agent.start(opened["case"])
    first = agent.decide(threads, "sup:70120")  # a first contact: our questions
    assert first and "action" not in first["message"]
    threads["sup:70120"] += [{"from": "buyer", **first["message"]}, threads["sup:70120"][0]]
    second = agent.decide(threads, "sup:70120")
    assert second is None or "action" not in second["message"]


def test_stabilise_rfq_on_the_twin_does_not_squeeze_a_cash_short_bidder(world):
    """P06 on TwinReal: the cheap bidder agrees to our first sheet whatever it is, so that sheet must not sit
    below its full cost (the twin's true one is 147.9; the agent used to offer 138.7 and scored 0)."""
    from arena_kit.play import RemoteTable
    from buyer.runner import make_factory, play

    res = play(RemoteTable(LocalClient(make_twin_real())), "case:P06", make_factory(world))
    assert res["deal"] and res["terms"]["unit_price"] > 140
    assert res["cost_breakdown"]["relationship"] < 0.05 * res["c_deal"]
    assert res["score"] > 0.5


def test_a_bidder_heading_lower_holds_back_the_signature():
    ts = SimpleNamespace(c_standing=99.0)
    x = SimpleNamespace(plan=SimpleNamespace(limit_hat=28.45, ask_value=30.34), c_standing=100.0)
    assert BuyerAgent.heading_below(x, ts)  # 100 x 28.45 / 30.34 = 93.8 < 98: its final offer will be cheaper
    x.plan.limit_hat = 30.0
    assert not BuyerAgent.heading_below(x, ts)  # no clear gain left
    x.plan.limit_hat = None
    assert not BuyerAgent.heading_below(x, ts)  # one reply is not a trend


def test_tight_estimate_prices_the_charge_and_allows_a_bid_above_the_ask(world):
    """P17: letter 37.10 is below its full cost over (1 - sustainable margin); the opening-quote cap used to price the
    charge at 0 and the ask caps forbade any bid above the letter."""
    case = json.loads(json.dumps(CASES["case:P17"]))
    case["buyer"]["sustainable_margin_pct"] = 8  # the twin's draw; the case file says 0.5
    est = Estimate(Dossier(world, case, case["suppliers"][0]))
    assert est.err <= 0.05
    est.open_value = 37.10
    assert max(f for _, f in est.f_nodes()) > est.open_value * (1 - case["buyer"]["sustainable_margin_pct"] / 100)
    plan = Planner(case, est)
    plan.observe_reply([{"unit_price": 37.10, "payment_days": 30, "lead_time_days": 14}], "open")
    assert plan.fair_price_case
    assert plan.ask_cap == float("inf")
    assert plan.packages[0].p_opt > 37.10
