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
