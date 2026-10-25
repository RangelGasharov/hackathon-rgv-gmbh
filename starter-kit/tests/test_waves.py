"""The Live Final wave loop with a fake arena client: every case starts, at most 10 at once, most valuable first."""

import threading
import time

from arena_kit.client import ArenaError
from buyer.runner import MAX_OPEN, case_priority, play_waves


def make_case(i, wave=1, alt=True, price=10.0):
    buyer = {"current_unit_price": price, "fallback_unit_price": price, "need_per_year": 100, "horizon_years": 1}
    if alt:
        buyer["alternative"] = {"terms": {"unit_price": price * 1.2}}
    return {"case_id": f"case:F{i:02d}", "wave": wave, "open": True, "seconds_left": 480, "buyer": buyer,
            "quantity": 100, "negotiable": ["unit_price"], "term_options": {}}  # fmt: skip


class FakeClient:
    def __init__(self, cases):
        self._cases = cases

    def cases(self, set_):
        return list(self._cases)


def run(cases, duration=0.05, fail=None):
    lock, state = threading.Lock(), {"open": 0, "peak": 0, "order": [], "calls": {}}

    def fake_play(table, cid, factory, deadline=None, case=None):
        with lock:
            state["order"].append(cid)
            state["calls"][cid] = state["calls"].get(cid, 0) + 1
            state["open"] += 1
            state["peak"] = max(state["peak"], state["open"])
            n = state["calls"][cid]
        try:
            time.sleep(duration)
            if fail and fail(cid, n):
                raise ArenaError(429, "at most 10 open sessions per key")
            return {"deal": True}
        finally:
            with lock:
                state["open"] -= 1

    client = FakeClient(cases)
    done = {}
    t = threading.Thread(target=lambda: done.update(play_waves(client, None, waves=1, poll=0.01, play_fn=fake_play)))
    t.start()
    time.sleep(0.2)
    for c in cases:  # the wave closes
        c["open"] = False
    t.join(10)
    assert not t.is_alive()
    return state, done


def test_19_cases_all_start_within_ten_slots():
    cases = [make_case(i) for i in range(19)]
    state, done = run(cases)
    assert len(state["order"]) == 19 and set(done) == {c["case_id"] for c in cases}
    assert state["peak"] <= MAX_OPEN
    assert all(v == {"deal": True} for v in done.values())


def test_most_valuable_first_and_no_alternative_before_any():
    cases = [make_case(i, price=10 + i, alt=i != 17) for i in range(19)]
    state, _ = run(cases, duration=0.2)
    assert state["order"][0] == "case:F17"  # no alternative: scores 0 without a deal
    first = state["order"][1:10]
    assert first == [f"case:F{i:02d}" for i in (18, 16, 15, 14, 13, 12, 11, 10, 9)]


def test_priority_key():
    assert case_priority(make_case(1, alt=False)) < case_priority(make_case(2, alt=True))
    assert case_priority(make_case(1, price=50)) < case_priority(make_case(2, price=5))
    assert case_priority({"case_id": "x"})  # odd shape does not raise


def test_failed_start_is_retried_while_wave_open():
    cases = [make_case(i) for i in range(3)]
    state, done = run(cases, fail=lambda cid, n: cid == "case:F01" and n == 1)
    assert state["calls"]["case:F01"] == 2
    assert done["case:F01"] == {"deal": True}


# ---- Live Final rules: clock, margin, short-clock close, resume, denylist, budget, rate ----------------------------

import random  # noqa: E402
import types  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from arena_kit.client import ArenaClient, BannedEndpoint  # noqa: E402
from arena_kit.play import RemoteTable  # noqa: E402
from buyer import llm, runner  # noqa: E402

S = 0.01  # simulated seconds -> real seconds


class StubAgent:
    """Just enough of BuyerAgent for runner.play: a few rounds per supplier, then accept; closes when the clock is short."""

    def __init__(self, deadline=None, rounds=5, margin=90 * S):
        self.deadline, self.close_margin, self.rounds = deadline, margin, rounds
        self.case, self.log, self.t, self.deadlines, self.calls = {}, [], {}, [], []

    def start(self, case, threads):
        self.case = case
        self.t = {s: types.SimpleNamespace(in_flight=False, dead=False, sent=0) for s in threads}

    def decide(self, threads, sid):
        ts = self.t[sid]
        if ts.in_flight:
            return None
        self.deadlines.append(self.deadline)
        left = (self.deadline - time.time()) if self.deadline else 1e9
        if ts.sent >= self.rounds or left < self.close_margin:
            return {"supplier_id": sid, "message": {"action": {"type": "accept", "term_sheet_id": "s"}}}
        ts.sent += 1
        return {"supplier_id": sid, "message": {"subject": "x", "body": "", "term_sheets": []}}

    def write(self, message, budget):
        time.sleep(7 * S)  # our compose time
        return dict(message)

    def keep_alive(self, threads, sid):
        self.calls.append("keep_alive")
        return None

    def close_now(self, threads, sid):
        self.calls.append("close_now")
        return {"supplier_id": sid, "message": {"action": {"type": "accept", "term_sheet_id": "s"}}}

    def safe_fallback(self, threads, sid):
        return None

    def note_round(self, took):
        pass

    def invalid_message(self, sid):
        return False

    def reoffer(self, threads, sid):
        return None


class StubTable:
    def __init__(self, cap, rng, suppliers=("a", "b", "c"), reply=(10, 30)):
        self.cap, self.rng, self.suppliers, self.reply = cap, rng, suppliers, reply
        self.session_id, self.files, self.case, self.done, self.closed_at = "sid", {}, None, None, None
        self.client = types.SimpleNamespace(session=lambda sid: {"status": "ended"}, thread=lambda *a: {})

    def open(self, case_id):
        return {}, {s: [{"from": "supplier", "status": "open", "term_sheets": []}] for s in self.suppliers}

    def attachment(self, i):
        return b""

    def send(self, sid, msg):
        time.sleep(self.rng.uniform(*self.reply) * S)
        if time.time() > self.cap:
            raise ArenaError(409, "wave over")
        return {"from": "supplier", "status": "open", "term_sheets": []}

    def close(self, sid, msg):
        if time.time() > self.cap:
            raise ArenaError(409, "wave over")
        self.closed_at = time.time()
        return {"deal": True, "supplier_id": sid}

    def result(self):
        return {"deal": False, "end_reason": "wave_cap"}


def test_simulated_19_case_wave_finishes_before_the_cap(monkeypatch, capsys):
    """19 cases x 3 suppliers, replies 10-30 s, compose 7 s, cap 480 s (1 simulated second = 10 ms)."""
    monkeypatch.setattr(runner, "MARGIN_FIXED_S", 20 * S)
    rng, tables = random.Random(7), {}
    t0 = time.time()
    cap = t0 + 480 * S
    cases = [dict(make_case(i), seconds_left=480 * S) for i in range(19)]

    def play_fn(table, cid, factory, deadline=None, case=None):
        tables[cid] = StubTable(cap, rng)
        return runner.play(tables[cid], cid, lambda d: StubAgent(d), deadline, case)

    out = {}
    th = threading.Thread(
        target=lambda: out.update(play_waves(FakeClient(cases), None, waves=1, poll=0.005, play_fn=play_fn))
    )
    th.start()
    while (len(tables) < 19 or any(t.closed_at is None for t in tables.values())) and time.time() < cap + 0.5:
        time.sleep(0.01)
    for c in cases:
        c["open"] = False
    th.join(10)
    assert not th.is_alive()
    wall = (max(t.closed_at for t in tables.values() if t.closed_at) - t0) / S
    closed = sum(1 for r in out.values() if isinstance(r, dict) and r.get("deal"))
    with capsys.disabled():
        print(f"\nSIM wave: {closed}/19 closed with a deal, last close at {wall:.0f} s of 480 s")
    assert closed == 19 and wall < 480


def test_deadline_is_reread_on_every_poll_and_follows_an_extension():
    seen, polls = [], [0]
    case = make_case(1)

    class Ext(FakeClient):
        def cases(self, set_):
            polls[0] += 1
            case["seconds_left"] = 480 + 120 * min(polls[0] // 5, 1)  # the host adds two minutes after a while
            return [case]

    def play_fn(table, cid, factory, deadline=None, case=None):
        t = time.time() + 1.5
        while time.time() < t:
            seen.append(deadline() - time.time())
            time.sleep(0.01)
        return {}

    done = {}
    th = threading.Thread(target=lambda: done.update(play_waves(Ext([case]), None, waves=1, poll=0.02, play_fn=play_fn)))
    th.start()
    time.sleep(1.7)
    case["open"] = False
    th.join(10)
    assert min(seen) < 485 and max(seen) > 595


def test_margin_scales_with_observed_reply_latency(monkeypatch):
    monkeypatch.setattr(runner, "MARGIN_PER_LATENCY", 100.0)
    monkeypatch.setattr(runner, "MARGIN_FIXED_S", 0.0)
    agent = StubAgent(rounds=3, margin=0.0)
    cap = time.time() + 1000
    table = StubTable(cap, random.Random(1), suppliers=("a",), reply=(30, 30))
    runner.play(table, "c", lambda d: agent, lambda: cap)
    # first look: nothing seen yet -> the cap; later looks: 100 x 0.3 s = 30 s before the cap
    assert agent.deadlines[0] == cap and agent.deadlines[-1] < cap - 25


def test_short_clock_skips_holding_notes_and_closes(monkeypatch):
    monkeypatch.setattr(runner, "HOLD_AFTER_S", 0.0)
    agent = StubAgent(margin=100.0)
    agent.decide = lambda threads, sid: None  # nobody answers for now
    cap = time.time() + 30  # less than the 100 s margin
    res = runner.play(StubTable(cap, random.Random(1), suppliers=("a",)), "c", lambda d: agent, lambda: cap)
    assert res["deal"] and "close_now" in agent.calls and "keep_alive" not in agent.calls


def test_a_running_session_is_resumed_via_409():
    class C:
        def open_session(self, cid):
            raise ArenaError(409, "one session per case in the final: sess-1")

        def session(self, sid):
            return {"status": "running", "threads": {"a": {}}}

        def thread(self, sid, s):
            return {"messages": [{"from": "supplier"}]}

    case = make_case(1)
    case_, threads = RemoteTable(C(), case).open("case:F01")
    assert case_ is case and list(threads) == ["a"]


@pytest.mark.parametrize("path", ["/events", "/events/stream", "/screen", "/results", "/results/team"])
def test_banned_endpoints_raise_before_any_request(path):
    def handle(request):
        raise AssertionError("a banned call reached the network")

    c = ArenaClient("http://arena.test", "k", transport=httpx.MockTransport(handle))
    with pytest.raises(BannedEndpoint):
        c._call("GET", path)
    with pytest.raises(BannedEndpoint):
        c.leaderboard("final")


def test_practice_leaderboard_and_cases_stay_allowed():
    c = ArenaClient("http://arena.test", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    assert c.leaderboard("practice") == [] and c.cases("final") == []


def test_final_budget_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "SPEND", tmp_path / "spend.json")
    monkeypatch.setenv("BUYER_BUDGET_USD", "14")
    assert any("below" in w for w in llm.warn_final_budget())
    assert not llm.warn_final_budget(budget=60)
    (tmp_path / "spend.json").write_text('{"usd": 3.2, "calls": 90}')
    assert any("earlier spend" in w for w in llm.warn_final_budget(budget=60))


def test_budget_cut_is_logged(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(llm, "SPEND", tmp_path / "spend.json")
    (tmp_path / "spend.json").write_text('{"usd": 20, "calls": 1}')
    claude = llm.Claude.__new__(llm.Claude)
    claude.budget = 14.0
    with caplog.at_level("ERROR", logger="buyer"), pytest.raises(llm.BudgetExceeded):
        claude.complete("s", "u")
    assert "BUDGET USED UP" in caplog.text


def test_call_rate_across_threads_stays_under_ten_per_second():
    stamps, lock = [], threading.Lock()

    def handle(request):
        with lock:
            stamps.append(time.monotonic())
        return httpx.Response(200, json={})

    c = ArenaClient("http://arena.test", "k", transport=httpx.MockTransport(handle))  # default rate: 9/s
    ths = [threading.Thread(target=lambda: [c.session("x") for _ in range(3)]) for _ in range(30)]  # 10 cases x 3
    [t.start() for t in ths]
    [t.join() for t in ths]
    stamps.sort()
    assert len(stamps) == 90
    assert all(stamps[i + 10] - stamps[i] >= 0.95 for i in range(len(stamps) - 10))  # never 11 calls inside 1 s
