"""Wave 2 (ANALYSIS A2, B4): payment days at their efficient level, and every supplier ask answered first.
A2a no sheet of ours pays sooner than our best one unless it asked twice in its sheets, A2b a "shorter payment" ask
needs its own sheets or words, A2c a lock needs its own choice in both replies, A2d the cash rate per day band."""

import json
from pathlib import Path

import pytest

from buyer.agent import BuyerAgent
from buyer.dossier import World
from buyer.writer import Plan, TemplateWriter, guard

SID = "sup:70710"


@pytest.fixture(scope="module")
def world():
    return World("data/package")


def _agent(world):
    agent = BuyerAgent(world)
    agent.start(json.loads(Path("cases/real/P08.json").read_text()))
    return agent


def _t(price, days, months=12):
    return {"unit_price": price, "payment_days": days, "index_clause": None, "contract_months": months}


def test_a2d_cash_rate_kept_per_day_band(world):
    est = _agent(world).t[SID].est
    est.standard["payment_days"] = 60
    assert est.learn_cash_rate([_t(432.29, 90), _t(430.82, 60)])
    r_long = est.rate_at(90)
    assert est.learn_cash_rate([_t(429.36, 30), _t(427.62, 14)])
    # the second pair (P03: 9.2 %) no longer overwrites the first (4.1 %): each band keeps its own rate
    assert abs(est.rate_at(90) - r_long) < 1e-9 and 0.035 < r_long < 0.047
    assert est.rate_at(14) > 0.07 and r_long < est.rate_at(30) < est.rate_at(14)
    assert len(est.cash_pairs) == 2 and est.rate == est.rate_learnt


def test_a2a_no_shorter_payment_than_our_best_sheet(world):
    plan = _agent(world).t[SID].plan
    best = _t(4.6, 90)
    assert plan.pay_ok(_t(4.5, 90), best) and plan.pay_ok(_t(4.7, 120), best)
    assert not plan.pay_ok(_t(4.5, 30), best)
    for sheets in (plan.sheets(0), plan.sheets(2)):
        assert all(t["payment_days"] >= sheets[0]["payment_days"] for t in sheets)
        menu = plan.menu(sheets, 0)
        assert all(t["payment_days"] >= sheets[0]["payment_days"] for t, _dc in menu)
    plan.ask_sheets = [[_t(5.0, 30)]]  # one reply asking 30 days is not insisting
    assert not plan.pay_ok(_t(4.5, 30), best)
    plan.ask_sheets.append([_t(4.9, 30), _t(4.8, 45)])  # two replies, all sheets sooner than our 90: down to 45
    assert plan.pay_ok(_t(4.5, 45), best) and not plan.pay_ok(_t(4.5, 30), best)


def test_a2c_no_lock_from_our_own_dropped_alternative(world):
    plan = _agent(world).t[SID].plan
    plan.commit([_t(4.40, 90), _t(4.35, 30)])
    plan.observe_reply([_t(5.00, 30)], "open")  # it chose 30 days among ours
    plan.commit([_t(4.45, 30)])  # our strategist dropped the 90-day sheet
    plan.observe_reply([_t(4.95, 30)], "open")
    assert "payment_days" not in plan.locked
    plan.commit([_t(4.50, 90)])
    plan.observe_reply([_t(4.90, 30)], "open")  # it countered 30 against our 90, but the reply before was ours
    assert "payment_days" not in plan.locked
    plan.commit([_t(4.55, 90)])
    plan.observe_reply([_t(4.88, 30)], "open")  # its own choice in both replies: now it insists
    assert plan.locked.get("payment_days") == 30


def test_a2b_shorter_payment_ask_needs_its_own_sheets_or_words(world):
    agent = _agent(world)
    ts = agent.t[SID]
    ts.last_sent = [_t(4.6, 60)]
    ts.plan.ask_sheets = [[_t(5.0, 90)]]
    ts.last_body = "We can offer payment within 90 days at EUR 5.00."  # it OFFERED longer payment (live P03)
    assert not agent.faster_pay_asked(ts)
    ts.last_body = "We would need payment within 30 days."
    assert agent.faster_pay_asked(ts)
    ts.last_body = "Faster payment would help our cash position."
    assert agent.faster_pay_asked(ts)
    ts.last_body = "Our offer stands."
    ts.plan.ask_sheets = [[_t(5.0, 30)]]  # its sheet pays sooner than ours
    assert agent.faster_pay_asked(ts)


def test_b4_every_ask_gets_a_concrete_answer(world):
    agent = _agent(world)
    ts = agent.t[SID]
    ts.sent = 1
    ts.last_body = (
        "Which payment terms do you propose? What contract length do you have in mind? "
        "Could you share your call-off schedule? Which figures is that based on?"
    )
    sheet = _t(4.6, 90, months=24)
    out = " ".join(agent.answers(ts, [sheet], []))
    assert "pays within 90 days" in out and "runs 24 months" in out
    assert "units a month" in out and "To your question on our figures" in out
    ts.sent_drivers = True
    assert "cost_drivers.csv" in " ".join(agent.answers(ts, [sheet], []))
    ts.last_body = "Thank you for your offer."
    assert agent.answers(ts, [sheet], []) == []


def test_b4_answers_come_first_and_survive_the_model(world):
    agent = _agent(world)
    d = agent.t[SID].d
    ans = ["To your question on payment: our proposal pays within 90 days net."]
    sheet = _t(4.6, 90)
    plan = Plan(case=agent.case, dossier=d, round_no=1, sheets=[sheet], claims=[], questions=[],
                levers=ans + ["We want a price that keeps this part healthy."], tone=["Thank you."], answers=ans)  # fmt: skip
    lines = TemplateWriter().write(plan).splitlines()
    assert lines.index(ans[0]) < lines.index("We want a price that keeps this part healthy.")
    assert sum(x == ans[0] for x in lines) == 1
    body = guard("Dear team,\nOur proposal: €4.60 per unit.\n\nBest regards,\nHollerau", plan)
    assert body.splitlines()[1] == ans[0]


def test_b4_compose_puts_answers_into_the_plan(world):
    agent = _agent(world)
    threads = {SID: [{"from": "sales", "status": "open", "body": "What payment terms can you offer us?",
                      "term_sheets": [{"term_sheet_id": "offer:1", "terms": _t(5.2, 30)}]}]}  # fmt: skip
    agent.ingest(SID, threads[SID])
    msg = agent.compose(threads, SID)
    plan = msg["_plan"]
    assert any(a.startswith("To your question on payment") for a in plan.answers)
    assert all(a in plan.levers for a in plan.answers)
