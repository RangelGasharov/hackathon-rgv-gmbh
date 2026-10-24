"""The buyer agent against the local twin and the kit's scripted supplier: no crashes, valid messages,
no broken must-haves, never the first offer, and better than the naive agent on the twin."""

import json
from pathlib import Path

import pytest

from arena_kit.agents import NaiveAgent
from arena_kit.cost import must_haves_ok
from arena_kit.play import RemoteTable
from arena_kit.play import play as kit_play
from buyer.agent import CLAUSE_MANDATORY, BuyerAgent, ThreadState, eight_d_commitment, schedule_lead_time
from buyer.claims import Claim, candidates, check, kind_key, ration
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
    assert mean(ours) > mean(naive) + 0.1  # the old twin is self-confirming: a sanity margin only


def test_claims_are_true_and_never_support_a_rise(world):
    for c in CASES:
        case = json.loads(Path(f"cases/practice/{c}.json").read_text())
        d = Dossier(world, case, case["suppliers"][0])
        for claim in candidates(world, d, case):
            assert check(world, claim), (c, claim.sentence)
            if claim.kind == "idx" and d.letter and d.letter.get("asked_price") and d.current:
                assert claim.figure < 0, (c, claim.sentence)


def test_hedged_supplier_gets_no_index_argument(world):
    case = json.loads(Path("cases/practice/P14.json").read_text())
    d = Dossier(world, case, "sup:70560")
    assert d.hedge_until is not None
    assert all(c.kind != "idx" or c.weight < 0.2 for c in candidates(world, d, case))


def test_one_argument_per_kind_per_supplier():
    # arena v3: a second forecast (another kit part) or a second scorecard row came back `duplicate`
    def claim(kind, ids, weight, series=None):
        return Claim(kind, ids, 1.0, f"{kind} ({', '.join(ids)}).", weight, meta={"series": series} if series else {})

    pool = [
        claim("fc", ["fc:1@2026-09"], 0.9),
        claim("fc", ["fc:2@2026-09"], 0.85),
        claim("sc", ["sc:9@2026Q3"], 0.8),
        claim("sc_otd", ["sc:9@2026Q1", "sc:9@2026Q2"], 0.75),
        claim("idx", ["idx:A@1", "idx:A@2"], 0.7, "A"),
        claim("idx", ["idx:B@1", "idx:B@2"], 0.65, "B"),
    ]
    first = ration(pool, set(), 0)
    assert [c.kind for c in first] == ["fc", "sc", "idx"]
    heard = {kind_key(c) for c in first}
    used = {i for c in first for i in c.ids}
    second = ration(pool, used, 1, heard)
    assert [c.meta.get("series") for c in second] == ["B"]  # no second forecast or scorecard row


def test_price_letter_keeps_a_granted_later_date(world):
    case = json.loads(Path("cases/real/P01.json").read_text())
    ts = ThreadState(world, case, "sup:70110")
    plan = ts.plan
    plan.observe_reply([{"unit_price": 138.55, "effective_date": "2026-11-01"}], "open")
    plan.observe_reply(
        [{"unit_price": 137.0, "effective_date": "2027-04-01"}, {"unit_price": 134.3, "effective_date": "2027-01-01"}],
        "open",
    )
    assert plan.min_date == "2027-01-01"
    assert all(p.combo.get("effective_date", "9999") >= "2027-01-01" for p in plan.packages)


def _opening(price, status="open"):
    return {
        "from": "sales",
        "status": status,
        "body": "Our offer.",
        "term_sheets": [{"term_sheet_id": "q-0", "terms": {"unit_price": price}}],
    }


def test_fallback_after_an_error_never_signs_the_opening(world):
    # an internal error in round 0 used to accept the supplier's standing sheet when there was no alternative
    case = json.loads(Path("cases/real/P01.json").read_text())
    agent = BuyerAgent(world)
    agent.start(case)
    threads = {"sup:70110": [_opening(138.55)]}
    agent.ingest("sup:70110", threads["sup:70110"])
    first = agent.safe_fallback(threads, "sup:70110")
    assert first and "action" not in first["message"] and first["message"]["term_sheets"] == []
    assert agent.safe_fallback(threads, "sup:70110") is None  # one holding note per supplier reply


def test_attachment_parsers():
    assert eight_d_commitment("D8 Closure: after 3 months.\nCommitment: 1500 ppm.") == 1500
    assert schedule_lead_time("12 | =ROUND(15900/12,0) | =SUM(B$2:B13)\nLead time (days) | 56 | ") == 56
    assert eight_d_commitment("no commitment here") is None


def test_duplicate_claim_result_retires_that_kind(world):
    case = json.loads(Path("cases/real/P07.json").read_text())
    agent = BuyerAgent(world)
    agent.start(case)
    ts = agent.t["sup:70610"]
    ts.last_claims = [Claim("fc", ["fc:360012@2026-09"], 9.0, "x", 0.8)]
    reply = {**_opening(5.6), "claim_results": [{"index": 0, "result": "duplicate"}]}
    agent.ingest("sup:70610", [_opening(5.74), {"from": "buyer"}, reply])
    assert "fc" in ts.d.dead_kinds


def _thread(world, cid, sid):
    return ThreadState(world, json.loads(Path(f"cases/real/{cid}.json").read_text()), sid)


def test_distributor_gets_clause_sheets_only_beyond_twelve_months(world):
    # arena P08/P18: "we cannot offer a fixed price over a 36-month period"; a clause-free long sheet is only declined
    for cid, sid, deadband in (("P08", "sup:70710", 5), ("P18", "sup:70740", 5), ("P16", "sup:70720", 3)):
        ts = _thread(world, cid, sid)
        assert ts.plan.clause_required and ts.est.wants_clause  # its letter proposes a price adjustment clause
        long_ = [p for p in ts.plan.packages if ts.plan._months(p.combo) > 12]
        assert long_ and all(p.combo["index_clause"] for p in long_)
        assert {p.combo["index_clause"]["deadband_pct"] for p in long_} == {deadband}  # the clause that costs least
        ts.plan.observe_reply([{"unit_price": 6.05, "contract_months": 12, "payment_days": 30}], "open")
        assert all(t.get("index_clause") for t in ts.plan.sheets(0) if t["contract_months"] > 12)


def test_clause_choice_follows_the_index_path(world):
    plan = _thread(world, "P08", "sup:70710").plan  # path +6 %: the widest deadband passes the least on
    assert plan.pref_clause["deadband_pct"] == 5
    plan = _thread(world, "P16", "sup:70720").plan  # path +3 %: deadband 3 and 5 both pass nothing on; 3 protects more
    assert plan.pref_clause["deadband_pct"] == 3
    case = json.loads(Path("cases/real/P08.json").read_text())
    case["buyer"]["index_paths"] = {"AS-14404": -0.06}  # a falling index: the clause pays us, the lowest deadband most
    assert ThreadState(world, case, "sup:70710").plan.pref_clause["deadband_pct"] == 0


def test_required_clause_is_lifted_when_it_quotes_a_long_term_without_one(world):
    plan = _thread(world, "P08", "sup:70710").plan
    plan.observe_reply([{"unit_price": 5.9, "contract_months": 36, "payment_days": 30}], "open")
    assert not plan.clause_required
    assert any(not p.combo["index_clause"] and p.combo["contract_months"] > 12 for p in plan.packages)


def test_it_saying_it_fixes_no_long_price_requires_the_clause(world):
    case = json.loads(Path("cases/real/P14.json").read_text())  # a renewal: the clause is optional
    agent = BuyerAgent(world)
    agent.start(case)
    ts = agent.t["sup:70560"]
    assert not ts.plan.clause_required
    reply = {**_opening(342.0), "body": "We cannot offer a fixed price over a 36-month period, so we decline."}
    agent.ingest("sup:70560", [_opening(345.0), {"from": "buyer"}, reply])
    assert ts.plan.clause_required
    assert all(p.combo["index_clause"] for p in ts.plan.packages if ts.plan._months(p.combo) > 12)


def test_optional_clause_is_tried_next_to_a_clause_free_sheet(world):
    # arena P02 (copper, 12 months): it took the clause sheet over clause-free ones; on a twin that values no clause an
    # all-clause message came back as an all-clause deal (0.26 against 0.65)
    case = json.loads(Path("cases/real/P02.json").read_text())
    case["buyer"]["index_risk_pct"] = 1  # a cheap clause (the real P02 risk of 4 % makes it lose: see below)
    ts = ThreadState(world, case, "sup:70510")
    assert ts.est.clause_driven and not ts.est.wants_clause and not ts.plan.clause_required
    ts.plan.observe_reply([{"unit_price": 370.96, "payment_days": 30, "index_clause": None}], "open")
    sheets = ts.plan.sheets(0)
    assert sum(bool(t["index_clause"]) for t in sheets) == 1 and any(not t["index_clause"] for t in sheets)
    ts.plan.observe_reply(
        [{"unit_price": 365.0, "payment_days": 90, "index_clause": sheets[1]["index_clause"]}], "open"
    )
    assert ts.est.wants_clause  # it showed that it wants one: from now on the clause is worth something to it


def test_mandatory_clause_phrases_of_the_arena():
    said = [  # arena P08, P18 and the twin
        "Please note that we cannot offer a fixed price over a 36-month period, so we must decline your second option.",
        "As we cannot offer fixed prices for extended contract durations, we are focusing on your first option.",
        "we cannot fix prices over a 24-month period due to raw material volatility, making the AS-14404 index clause mandatory.",
        "As a fixed price over two years is not feasible, we have selected your third option.",
        "We cannot fix prices beyond 12 months due to raw material volatility; the AS-14404 index clause is mandatory.",
    ]
    assert all(CLAUSE_MANDATORY.search(t) for t in said)
    assert not CLAUSE_MANDATORY.search(
        "We can offer a Preisgleitklausel on AS-14404. We cannot reduce our price further."
    )


def test_best_index_window_is_one_the_arena_takes_and_ends_now(world):
    # an alloy surcharge that rose 5.9 % over twelve months but has fallen since its peak in June
    ch, i0, i1, m0, m1 = world.index_best_window("AS-14404")
    assert (m0, m1) == ("2026-06", "2026-09") and ch < -5
    assert i0 == "idx:AS-14404@2026-06" and i1 == "idx:AS-14404@2026-09"
    for series in ("PPI-D351", "PPI-C2431", "LCI-C", "CU-NOTATION"):
        ch, _, _, m0, m1 = world.index_best_window(series)
        assert 1 <= (int(m1[:4]) - int(m0[:4])) * 12 + int(m1[5:7]) - int(m0[5:7]) <= 12
        assert m1 == world.series_last[series]  # a recent end only
        assert ch <= world.index_change(series)[0]  # never worse than the plain twelve months


def test_recent_falls_of_a_rising_series_are_cited_and_true(world):
    case = json.loads(Path("cases/real/P05.json").read_text())
    d = Dossier(world, case, "sup:70250")
    idx = {c.meta["series"]: c for c in candidates(world, d, case) if c.kind == "idx"}
    assert world.index_change("AS-14404")[0] > 0  # up over twelve months ...
    assert idx["AS-14404"].figure < -5  # ... and down since June
    assert idx["PPI-D351"].figure < world.index_change("PPI-D351")[0]  # its best window, not the plain twelve months
    for c in candidates(world, d, case):
        assert check(world, c)


def test_a_reply_retires_only_the_kinds_it_says_do_not_count(world):
    from buyer.claims import dead_key, no_change_kinds

    body = (
        "We checked the rival quote data you cited; it holds and we took it into account. "
        "We checked the delivery record data you cited; it holds, but it does not change our calculation."
    )
    assert no_change_kinds(body) == {"sc_otd"}  # the rival quote counted
    both = "We checked the volume change data you cited; it holds, but it does not change our calculation."
    assert no_change_kinds(both) == {"fc"}
    index = "We checked the index change data you cited; it holds, but it does not change our calculation."
    assert no_change_kinds(index) == set()  # another series may still count
    assert dead_key(Claim("offer", ["offer:70210-1"], 30.5, "x", 0.9)) != dead_key(
        Claim("offer", ["offer:70210-2"], 29.5, "x", 0.9)
    )  # a later, lower rival offer is a new argument


def test_irrelevant_index_series_does_not_retire_the_others(world):
    case = json.loads(Path("cases/real/P05.json").read_text())
    agent = BuyerAgent(world)
    agent.start(case)
    ts = agent.t["sup:70210"]
    a = Claim("idx", ["idx:AS-14404@2026-06", "idx:AS-14404@2026-09"], -5.5, "x", 0.9, meta={"series": "AS-14404"})
    b = Claim("idx", ["idx:PPI-D351@2025-08", "idx:PPI-D351@2026-08"], -3.7, "y", 0.9, meta={"series": "PPI-D351"})
    ts.last_claims = [a, b]
    results = [{"index": 0, "result": "irrelevant"}, {"index": 1, "result": "verified"}]
    reply = {**_opening(30.5), "claim_results": results}
    agent.ingest("sup:70210", [_opening(36.0), {"from": "buyer"}, reply])
    assert all(c.meta["series"] != "AS-14404" for c in candidates(world, ts.d, case) if c.kind == "idx")
    assert any(c.meta["series"] == "PPI-D351" for c in candidates(world, ts.d, case) if c.kind == "idx")


def test_a_fair_point_marks_a_supplier_on_an_old_cost_sheet_for_good(world):
    case = json.loads(Path("cases/real/P08.json").read_text())
    d = Dossier(world, case, "sup:70710")
    assert not d.has("stale_costing")
    assert d.add_text("We did not know that - a fair point.")
    assert d.has("stale_costing", strong=True)
    d.add_text("Thank you for your offer. Which volume can you commit to?")
    assert d.has("stale_costing", strong=True)  # one reply shows it; later replies do not take it back


def test_the_ways_suppliers_ask_for_our_numbers():
    from buyer.agent import ASKS_DATA, ASKS_VOLUME

    volume = [
        "please send us the actual volume data so we can see the numbers.",
        "could you share some data or a schedule to back up this volume?",
        "you mentioned 4,200 pieces a year - could you send us the forecast data so we can plan?",
        "please send over the actual figures for the binding volume so we can lock it in.",
        "could you also send over some proof for that 4,200 volume so we can plan for it?",
        "please provide the corresponding consumption data to support this volume projection.",
    ]
    assert all(ASKS_VOLUME.search(t) for t in volume)
    assert not ASKS_VOLUME.search("which volume can you commit to? a binding higher volume would help us.")
    assert not ASKS_VOLUME.search("our offer is a price of 5.60 per unit, payment 90 days net.")
    assert ASKS_DATA.search("for our records, which figures is that based on? please send us the data.")
    assert ASKS_DATA.search("please share the data behind your target price.")


def test_the_strategist_may_send_two_index_series_but_not_two_forecasts(world):
    from buyer.writer import Plan, StrategistWriter

    case = json.loads(Path("cases/real/P05.json").read_text())
    d = Dossier(world, case, "sup:70210")
    pool = {c.meta.get("series") or c.kind: c for c in candidates(world, d, case) if c.kind in ("idx", "sc")}
    a, b = pool["AS-14404"], pool["PPI-C2431"]
    fc2 = Claim("fc", ["fc:1@2026-09"], 9.0, "Our forecast has risen +9.0% (fc:1@2026-09).", 0.8)
    fc3 = Claim("fc", ["fc:2@2026-09"], 8.0, "Our other forecast has risen +8.0% (fc:2@2026-09).", 0.7)
    sheet = {"unit_price": 30.5, "payment_days": 30, "lead_time_days": 28}
    plan = Plan(case=case, dossier=d, round_no=0, sheets=[sheet], claims=[], questions=[], levers=[], tone=[])
    plan.menu, plan.menu_claims, plan.points, plan.thread = [(sheet, 0.0)], [a, b, fc2, fc3], [], []
    email = "\n".join(
        ["Dear team,", a.sentence, b.sentence, fc2.sentence, fc3.sentence, "We offer 30.50.", "Best regards"]
    )
    answer = json.dumps({"sheets": ["S1"], "claims": ["C1", "C2", "C3", "C4"], "points": [], "email": email})
    StrategistWriter(lambda *args, **kw: answer).write(plan)
    assert [c.kind for c in plan.claims] == ["idx", "idx", "fc"]  # the second forecast is the same kind


def _p10_opening(lead=54, volume=19890):
    terms = {"unit_price": 32.2, "lead_time_days": lead, "volume_per_year": volume, "contract_months": 12}
    sheet = {"term_sheet_id": "q-0", "terms": terms}
    return {"from": "sales", "status": "open", "body": "Our offer.", "term_sheets": [sheet]}


def test_opening_lead_time_is_the_standard_it_prices_against(world):
    # arena P10: 54 days quoted, 42 in the master data. Its MESO pair 33.6 @ 42 / 31.7 @ 54 is iso-value only if 54
    # is the standard: with 42 the agent bought a premium it never saw, and could not offer the supplier's own 54.
    case = json.loads(Path("cases/real/P10.json").read_text())
    agent = BuyerAgent(world)
    agent.start(case)
    ts = agent.t["sup:70410"]
    assert ts.est.standard["lead_time_days"] == 42
    agent.ingest("sup:70410", [_p10_opening()])
    assert ts.est.standard["lead_time_days"] == 54
    sheet = {"unit_price": 31.7, "volume_per_year": 23400, "contract_months": 36}
    v = {lead: ts.est.value(case, {**sheet, "lead_time_days": lead}) for lead in (42, 54, 56)}
    assert v[42] < v[54] == v[56]
    assert abs(ts.est.value(case, {**sheet, "unit_price": 33.6, "lead_time_days": 42}) / v[54] - 1) < 0.01
    assert any(p.combo["lead_time_days"] == 54 for p in ts.plan.packages)  # allowed: the supplier's standard value
    # a delivery schedule carries the lead time of the sheet on the table (56 after we offered it), not its standard
    schedule = {"name": "delivery-schedule.xlsx", "text": "Lead time (days) | 56 | "}
    agent.ingest("sup:70410", [_p10_opening(), {**_p10_opening(), "attachments": [schedule]}])
    assert ts.est.standard["lead_time_days"] == 54


def test_an_off_option_value_the_arena_refuses_is_taken_back(world):
    case = json.loads(Path("cases/real/P10.json").read_text())
    agent = BuyerAgent(world)
    agent.start(case)
    sid, events = "sup:70410", [_p10_opening()]
    ts = agent.t[sid]
    first = agent.decide({sid: events}, sid)["message"]
    assert any(s["terms"]["lead_time_days"] == 54 for s in first["term_sheets"])  # 54 is not one of 35, 42, 56
    assert ts.sent == 1 and ts.k == 1
    assert agent.invalid_message(sid)  # the arena said 422
    assert (ts.sent, ts.k, ts.plan.last_value) == (0, 0, None) and ts.plan.strict_options
    again = agent.decide({sid: events}, sid)["message"]
    options = case["term_options"]["lead_time_days"]
    assert again["term_sheets"] and all(s["terms"]["lead_time_days"] in options for s in again["term_sheets"])
    assert not agent.invalid_message(sid)  # options only now: a second 422 is a different problem


def test_one_binding_volume_sentence_a_message(world):
    case = json.loads(Path("cases/real/P10.json").read_text())
    agent = BuyerAgent(world)
    agent.start(case)
    sid = "sup:70410"
    events = [_p10_opening()]
    agent.decide({sid: events}, sid)
    reply = {**_p10_opening(56, 23400), "body": "Which volume can you commit to? We can support the volume."}
    plan = agent.decide({sid: events + [{"from": "buyer"}, reply]}, sid)["message"]["_plan"]
    assert sum("binding volume" in x for x in plan.levers) == 1
    # its own lead time (54, we need 42) is offered as room for its capacity planning
    assert any("lead time of 54 days" in x and "plan your capacity" in x for x in plan.levers)


def test_lead_time_premium_is_a_capacity_tight_thing_and_learnt_from_the_suppliers_pair(world):
    # arena P10: 33.6 @ 42 days and 31.7 @ 54 days in one reply: 0.49 % of the quote per day below its 54
    p10 = json.loads(Path("cases/real/P10.json").read_text())
    agent = BuyerAgent(world)
    agent.start(p10)
    ts = agent.t["sup:70410"]
    assert ts.est.lead_prem == 0.005  # prior of a capacity-tight supplier
    terms = {"volume_per_year": 23400, "contract_months": 24}
    pair = [
        {"term_sheet_id": "offer:70410-1", "terms": {**terms, "unit_price": 33.6, "lead_time_days": 42}},
        {"term_sheet_id": "offer:70410-1-a", "terms": {**terms, "unit_price": 31.7, "lead_time_days": 54}},
    ]
    reply = {"from": "sales", "status": "open", "body": "Alternatively, with Lieferzeit 54 days.", "term_sheets": pair}
    agent.ingest("sup:70410", [_p10_opening(), {"from": "buyer"}, reply])
    assert ts.est.lead_prem == pytest.approx(0.0049, abs=0.0004)
    # a supplier with idle capacity starts at nothing: no late cost is paid to avoid a premium nobody charges
    p07 = json.loads(Path("cases/real/P07.json").read_text())
    other = BuyerAgent(world)
    other.start(p07)
    assert other.t["sup:70610"].est.lead_prem == 0.0


def test_a_twin_that_refuses_off_option_values_does_not_stall_the_case(world, monkeypatch):
    # worst case for offering the supplier's own standard (54 days): the arena answers 422 to it. The agent must take
    # the value back and still close; it used to re-decide the same refused sheets until the wave's cap.
    from arena_kit import sim
    from buyer.twin_real import make_twin_real

    def strict(self, case, terms, th):
        for f, v in terms.items():
            if f != "unit_price" and case["term_options"].get(f) and v not in case["term_options"][f]:
                raise sim.Fail(422, f"{f}={v!r} is not one of {case['term_options'][f]}")
        return terms

    monkeypatch.setattr(sim.Sim, "_check", strict)
    twin = make_twin_real(no_clock=True)
    result = play(RemoteTable(LocalClient(twin)), "case:P10", make_factory(world))
    assert result.get("deal") and result["score"] > 0.6
    assert any(entry[1] == "strict_options" for entry in result["agent_log"])


def test_a_supplier_that_says_its_plant_is_full_turns_capacity_tight_and_gets_the_lead_premium(world):
    case = json.loads(Path("cases/real/P07.json").read_text())
    agent = BuyerAgent(world)
    agent.start(case)
    ts = agent.t["sup:70610"]
    assert not ts.d.has("capacity_tight") and ts.est.lead_prem == 0.0
    # P10 (real): "a 42-day lead time cannot be accommodated given our current capacity utilization"
    reply = {
        **_opening(5.6),
        "body": "A 42-day lead time cannot be accommodated given our current capacity utilization.",
    }
    agent.ingest("sup:70610", [_opening(5.74), {"from": "buyer"}, reply])
    assert ts.d.has("capacity_tight", strong=True) and ts.est.lead_prem == 0.005
    # a supplier that wants to keep its machines busy is not tight
    free = {**_opening(5.6), "body": "With your volume we can keep our machines busy and fill the second shift."}
    other = BuyerAgent(world)
    other.start(case)
    other.ingest("sup:70610", [_opening(5.74), {"from": "buyer"}, free])
    assert not other.t["sup:70610"].d.has("capacity_tight", strong=True)


def test_clause_trial_on_a_copper_price_letter(world):
    # arena: without the clause the P02 supplier ended at 371.88 (b6 zip run, 0.51); its clause deals scored 0.67-0.72
    ts = _thread(world, "P02", "sup:70510")
    ts.plan.observe_reply([{"unit_price": 370.96, "payment_days": 30, "index_clause": None}], "open")
    assert any(t["index_clause"] for t in ts.plan.sheets(0))
