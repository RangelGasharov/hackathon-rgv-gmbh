"""The twin (buyer/twin.py): every practice case opens, scores and stays inside its yardstick; the
suppliers behave as their hidden profiles say. In-process (LocalClient) and over HTTP."""

import socket
import threading
import time

import pytest
from conftest import Clock

from arena_kit import ArenaClient, ArenaError, RemoteTable, play
from arena_kit.agents import NaiveAgent
from arena_kit.cost import must_haves_ok
from arena_kit.docs import quote_terms, read_document
from buyer.twin import ROOT, LocalClient, best_price, calibrate, make_twin, price_for, value

CASES = [f"case:P{i:02d}" for i in range(1, 20)]


@pytest.fixture(scope="module")
def twin():
    return make_twin(no_clock=True)


def opened(twin, case_id):
    client = LocalClient(twin)
    r = client.open_session(case_id)
    return client, r["session_id"], r


def standing(reply):
    return reply["term_sheets"][0]["terms"]


def test_every_case_opens_with_a_quote_that_parses(twin):
    for cid in CASES:
        client, sid, r = opened(twin, cid)
        for sup, (opening,) in r["threads"].items():
            p = twin.profile(r["case"], sup)
            att = opening["attachments"][0]
            terms = quote_terms(read_document(att["name"], client.attachment(sid, att["attachment_id"])))
            assert opening["status"] == "open" and opening["from"].endswith(sup[4:])
            assert terms["stated"] == standing(opening) == p["opening_sheet"]
            assert terms["standard"] == p["standard"]  # docs.standard_terms reads the same
            assert standing(opening)["unit_price"] >= p["floor_price"] - 0.01 or r["case"]["type"] == "price_increase"


def test_p01_opens_at_the_letter_price(twin):
    _, _, r = opened(twin, "case:P01")
    assert standing(r["threads"]["sup:70110"][0])["unit_price"] == 138.55


def test_traits_come_from_the_data(twin):
    expect = {
        ("case:P01", "sup:70110"): {"reciprocity", "stale_costing", "below_full_cost", "cash_short_weak"},
        ("case:P06", "sup:70150"): {"cash_short_hard", "free_capacity", "open_book"},
        ("case:P10", "sup:70410"): {"capacity_tight", "approval_ladder"},
        ("case:P14", "sup:70560"): {"hedged_material", "approval_ladder"},
        ("case:P03", "sup:70530"): {"price_frozen", "approval_ladder"},
    }
    for (cid, sup), traits in expect.items():
        assert traits <= set(twin.profile(twin.cases[cid], sup)["traits"]), (cid, sup)


def test_c_best_is_below_c_open_and_needs_more_than_price(twin):
    for cid in CASES:
        _, sid, r = opened(twin, cid)
        s, case = twin.sessions[sid], r["case"]
        c_open, c_best = twin.yardstick(case, s["threads"])
        assert c_best <= c_open + 0.01, cid
        for sup in case["suppliers"]:  # price alone, on the structure it quoted, never reaches c_best
            p = twin.profile(case, sup)
            if not must_haves_ok(case, p["opening_sheet"], p["standard"]):
                continue
            price_only, _ = best_price(case, p, {k: v for k, v in p["opening_sheet"].items() if k != "unit_price"})
            assert price_only > c_best + 1, (cid, sup)


def test_the_naive_agent_completes_every_case(twin, capsys):
    rows, text = calibrate(twin)
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "runs" / "twin_calibration.txt").write_text(text + "\n", encoding="utf-8")
    with capsys.disabled():
        print("\n" + text)
    scores = [r["score"] for r in rows if r["score"] is not None]
    assert len(scores) == len(CASES) and all(0 <= x <= 1 for x in scores)
    assert 0.05 <= sum(scores) / len(scores) <= 0.6  # neither trivially 0 nor 1
    assert all(r["c_best"] <= r["c_open"] + 0.01 for r in rows)


def test_agreed_when_the_buyer_offers_the_standing_sheet(twin):
    client, sid, r = opened(twin, "case:P13")
    sup = "sup:70230"
    low = {**standing(r["threads"][sup][0]), "unit_price": 14.0}
    counter = client.send_message(sid, sup, {"body": "We need a better price.", "term_sheets": [{"terms": low}]})
    assert counter["status"] == "open" and 14.0 < standing(counter)["unit_price"] < 17.44
    again = client.send_message(sid, sup, {"body": "Still too high.", "term_sheets": [{"terms": low}]})
    assert standing(again) == standing(counter)  # no move from the buyer: nothing given
    reply = client.send_message(sid, sup, {"body": "Agreed.", "term_sheets": [{"terms": standing(again)}]})
    assert reply["status"] == "agreed" and standing(reply) == standing(again)
    assert "quote.pdf" in [a["name"] for a in reply["attachments"]]
    result = client.send_message(
        sid, sup, {"action": {"type": "accept", "term_sheet_id": reply["term_sheets"][0]["term_sheet_id"]}}
    )["result"]  # noqa: E501
    assert result["deal"] and result["terms"]["unit_price"] == standing(again)["unit_price"]
    assert 0 <= result["score"] <= 1 and result["c_best"] <= result["c_deal"] + 0.01


def test_claims_are_checked_by_id_and_figure(twin):
    client, sid, r = opened(twin, "case:P01")
    sup, series = "sup:70110", twin.world.series["PPI-D351"]
    change = (series["2026-08"] / series["2025-08"] - 1) * 100
    pair = "(idx:PPI-D351@2025-08, idx:PPI-D351@2026-08)"
    sheet = standing(r["threads"][sup][0])

    def cite(body):
        return client.send_message(sid, sup, {"body": body, "term_sheets": [{"terms": sheet}]})["claim_results"]

    assert cite(f"Electricity prices changed {change + 3:.1f}% in twelve months {pair}.") == [
        {"index": 0, "result": "contradicted"}  # a wrong figure
    ]
    assert cite(f"Electricity prices changed {change:.1f}% {pair}. See idx:NOPE@2026-01.") == [
        {"index": 0, "result": "duplicate"},  # each ID counts once per case
        {"index": 1, "result": "contradicted"},  # an unknown ID
    ]
    labour = (twin.world.series["LCI-C"]["2026-06"] / twin.world.series["LCI-C"]["2025-06"] - 1) * 100
    ok = cite(f"Labour costs rose {labour:.1f}% (idx:LCI-C@2025-06, idx:LCI-C@2026-06). "
              "Copper idx:WB-CU@2025-09 and idx:WB-CU@2026-09 moved.")  # fmt: skip
    assert ok == [{"index": 0, "result": "verified"}, {"index": 1, "result": "irrelevant"}]  # not a cost driver


def test_lost_trust_ends_the_thread(twin):
    client, sid, r = opened(twin, "case:P13")
    sup, sheet = "sup:70230", standing(r["threads"]["sup:70230"][0])
    statuses = [client.send_message(sid, sup, {"body": f"See idx:NOPE{i}@2026-01.", "term_sheets": [{"terms": {
        **sheet, "unit_price": 15.0}}]})["status"] for i in range(3)]  # fmt: skip
    assert statuses[-1] == "walked_away"
    state = client.session(sid)
    assert state["status"] == "ended" and not state["result"]["deal"]


def test_suppliers_trade_what_they_value(twin):
    case = twin.cases["case:P11"]  # a cash-short foundry: paid in 14 days it takes less than in 60
    p = twin.profile(case, "sup:70140")
    terms = {"ppm_commitment": 1500, "chargeback_eur_per_complaint": 0}
    fast, slow = (price_for(p, case, {**terms, "payment_days": d}, p["floor_value"]) for d in (14, 60))
    assert slow - fast > 0.035 * fast  # 46 days at 30 %/yr: 3.8 %
    case = twin.cases["case:P03"]  # head office froze price reductions: never below the list
    client, sid, r = opened(twin, "case:P03")
    sheet = {**standing(r["threads"]["sup:70530"][0]), "unit_price": 380.0}
    for _ in range(4):
        reply = client.send_message(sid, "sup:70530", {"body": "Please improve.", "term_sheets": [{"terms": sheet}]})
        sheet = {**standing(reply), "unit_price": standing(reply)["unit_price"] - 1}
        assert standing(reply)["unit_price"] >= 412.74


def test_an_open_book_supplier_sends_its_cost_breakdown(twin):
    client, sid, _ = opened(twin, "case:P13")
    reply = client.send_message(sid, "sup:70230", {"body": "Could you send a cost breakdown?", "term_sheets": []})
    att = next(a for a in reply["attachments"] if a["name"] == "cost-breakdown.xlsx")
    text = read_document(att["name"], client.attachment(sid, att["attachment_id"]))
    assert "Margin" in text and "=SUM(B2:B5)" in text
    assert reply["status"] == "open"  # a question: answered, the quote stands


def test_value_is_what_the_cost_function_says(twin):
    case = twin.cases["case:P16"]
    p = twin.profile(case, "sup:70720")
    sheet = p["opening_sheet"]
    assert value(p, case, sheet) > value(p, case, {**sheet, "unit_price": sheet["unit_price"] - 1})


@pytest.fixture
def served():
    """The twin over HTTP on a free port, with a hand-moved clock: (url, twin, clock)."""
    pytest.importorskip("fastapi")
    import uvicorn

    from arena_kit.sim import create_app

    clock = Clock()
    state = make_twin(clock=clock)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(state), port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}", state, clock
    server.should_exit = True
    thread.join()


def test_over_http_with_the_kit_client(served):
    url, state, clock = served
    with pytest.raises(ArenaError) as e:
        ArenaClient(url).register("x", "wrong")
    assert e.value.status == 403
    key = ArenaClient(url).register("twin-test", "sim")["api_key"]
    client = ArenaClient(url, key, rate=1000)
    assert len(client.cases()) == len(CASES)
    for cid in ("case:P05", "case:P13"):
        result = play(RemoteTable(client), cid, NaiveAgent())
        assert result["ended"] and 0 <= result["score"] <= 1
    sid = client.open_session("case:P01")["session_id"]
    clock.now += 61  # the 60-second rule, unchanged
    with pytest.raises(ArenaError) as e:
        client.send_message(sid, "sup:70110", {"term_sheets": []})
    assert e.value.status == 409 and "late" in e.value.detail
    clock.now += 125
    assert client.session(sid)["end_reason"] == "timeout"
