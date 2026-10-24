"""Play a case with one worker per supplier thread, so no thread sits unanswered past 60 seconds
while another supplier is still replying (the kit's `play` sends one message at a time).

    uv run python -m buyer.runner play case:P01            # one case against $ARENA_URL
    uv run python -m buyer.runner run --set practice -p 8  # every hosted case
    uv run python -m buyer.runner run --set final          # the Live Final, wave by wave
"""

import argparse
import functools
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from arena_kit.cli import load_env
from arena_kit.client import ArenaClient, ArenaError
from arena_kit.play import RemoteTable, fetch_documents

from .agent import ENDED, BuyerAgent, latest
from .dossier import World

log = logging.getLogger("buyer")
MOVE_BUDGET_S = 45  # our own deadline per message, well inside the arena's 60 s


def play(table, case_id, agent_factory, deadline=None, case=None):
    """One case to its end. Returns the result dict."""
    opened_case, threads = table.open(case_id)
    case = opened_case or case
    if getattr(table, "done", None):
        return table.done
    for events in threads.values():
        for m in events:
            fetch_documents(table, m)
    agent = agent_factory(deadline)
    agent.start(case)
    lock, done, result = threading.Lock(), threading.Event(), {}

    def finish(res):
        if not done.is_set():
            result.update(res or {})
            done.set()

    def refresh(sid):
        """Re-read a thread from the arena (after a stale accept or a late move)."""
        try:
            th = table.client.thread(table.session_id, sid)
            threads[sid] = [fetch_documents(table, m) for m in th["messages"]]
        except Exception as e:  # noqa: BLE001 - keep playing on what we have
            log.warning("refresh %s failed: %s", sid, e)

    def worker(sid):
        while not done.is_set():
            cap = deadline() if callable(deadline) else deadline
            agent.deadline = cap
            t0 = time.monotonic()
            with lock:
                if done.is_set():
                    return
                decision = agent.decide(threads, sid)
                if decision and decision["message"].get("action"):
                    try:  # close while holding the lock: no other thread can change a standing sheet now
                        finish(table.close(decision["supplier_id"], decision["message"]))
                        return
                    except ArenaError as e:
                        log.warning("%s: close failed (%s); re-reading the threads", case_id, e)
                        if e.status == 409 and "ended" in str(e.detail):
                            finish(table.result())
                            return
                        for s in threads:
                            refresh(s)
                        continue
                if decision:
                    agent.t[decision["supplier_id"]].in_flight = True
            if decision is None:
                if latest(threads[sid]).get("status") in ENDED or agent.t[sid].dead:
                    return
                time.sleep(1.0)  # another thread decides this case; check back
                continue
            target, msg = decision["supplier_id"], agent.write(decision["message"])
            took = time.monotonic() - t0
            if took > MOVE_BUDGET_S:
                log.warning("%s %s: decision took %.0fs", case_id, sid, took)
            try:
                reply = table.send(target, msg)
            except ArenaError as e:
                if e.status != 409:
                    log.warning("%s %s: %s", case_id, sid, e)
                    agent.t[target].in_flight = False
                    if e.status == 422:  # invalid message: nothing happened; re-read and decide again
                        refresh(target)
                        continue
                    finish(table.result())
                    return
                reply = table.missed(target, e)
            if "result" in reply:
                finish(reply["result"])
                return
            with lock:
                threads[target] += [{**msg, "from": "buyer"}, fetch_documents(table, reply)]
                agent.t[target].in_flight = False
                agent.note_round(time.monotonic() - t0)
            if reply.get("status") in ENDED and all(latest(ev).get("status") in ENDED for ev in threads.values()):
                finish(table.result())
                return

    with ThreadPoolExecutor(max_workers=len(threads)) as pool:
        list(pool.map(worker, list(threads)))
    if not done.is_set():
        finish(table.result())
    result["agent_log"] = agent.log
    return result


def make_factory(world, writer_name="template"):
    from .writer import ClaudeWriter, TemplateWriter

    writer = TemplateWriter()
    if writer_name == "claude":
        from .llm import Claude

        writer = ClaudeWriter(Claude().complete)

    def factory(deadline=None):
        return BuyerAgent(world, writer=writer, deadline=deadline)

    return factory


def play_waves(client, factory, waves=5, parallel=10, poll=5.0):
    """The Live Final: wait for each wave, play its cases in parallel, stop after `waves` waves."""
    started, closed, caps = {}, set(), {}

    def one(case, deadline):
        try:
            return play(RemoteTable(client, case), case["case_id"], factory, deadline, case)
        except Exception as e:  # one broken case must not stop the others
            log.exception("%s failed", case["case_id"])
            return e

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        while len(closed) < waves or not all(f.done() for f in started.values()):
            try:
                cases = client.cases("final")
            except ArenaError as e:
                if e.status != 403:
                    raise
                cases = []
            for case in cases:
                if not case.get("open"):
                    closed.add(case["wave"])
                    continue
                if case.get("seconds_left") is not None:
                    caps[case["wave"]] = time.time() + case["seconds_left"]
                if case["case_id"] not in started and case.get("seconds_left"):
                    started[case["case_id"]] = pool.submit(one, case, functools.partial(caps.get, case["wave"]))
            time.sleep(poll)
    return {c: f.result() for c, f in started.items()}


def play_and_record(client, case_id, factory):
    """Play one case and keep the whole session (threads, result, back office) in runs/ for later study."""
    table = RemoteTable(client)
    result = play(table, case_id, factory)
    if table.session_id:
        try:
            state = client.session(table.session_id)
            threads = {s: client.thread(table.session_id, s) for s in state.get("threads", {})}
            try:
                back = client.backoffice(table.session_id)
            except ArenaError as e:
                back = {"error": str(e)}
            stamp = int(time.time())
            Path(f"runs/arena-{case_id.split(':')[-1]}-{stamp}.json").write_text(
                json.dumps({"result": result, "state": state, "threads": threads, "backoffice": back},
                           default=str, indent=1, ensure_ascii=False)
            )
        except Exception as e:  # noqa: BLE001 - the result counts, the record is a bonus
            log.warning("%s: could not save the record: %s", case_id, e)
    return result


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(prog="buyer")
    ap.add_argument("cmd", choices=["play", "run"])
    ap.add_argument("case_id", nargs="?")
    ap.add_argument("--set", default="practice")
    ap.add_argument("--cases", default="", help="run: only these case ids, e.g. P02,P03 (each played once)")
    ap.add_argument("--writer", default=os.environ.get("BUYER_WRITER", "template"), choices=["template", "claude"])
    ap.add_argument("--data", default=os.environ.get("ARENA_DATA", "data/package"))
    ap.add_argument("-p", "--parallel", type=int, default=8)
    ap.add_argument("--waves", type=int, default=5)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(asctime)s %(message)s")
    world = World(a.data)
    factory = make_factory(world, a.writer)
    client = ArenaClient()
    Path("runs").mkdir(exist_ok=True)
    if a.cmd == "play":
        out = {a.case_id: play_and_record(client, a.case_id, factory)}
    elif a.set == "final":
        out = play_waves(client, factory, a.waves, min(a.parallel, 10))
    else:
        ids = [c["case_id"] for c in client.cases(a.set) if c.get("hosted", True)]
        if a.cases:
            wanted = {f"case:{x.strip().removeprefix('case:')}" for x in a.cases.split(",")}
            ids = [c for c in ids if c in wanted]
        print("playing once each:", ", ".join(ids))

        def one(c):
            try:
                return c, play_and_record(client, c, factory)
            except Exception as e:  # noqa: BLE001 - one broken case must not stop the others
                return c, e

        with ThreadPoolExecutor(max_workers=min(a.parallel, 10)) as pool:
            out = dict(pool.map(one, ids))
    for cid, r in out.items():
        if isinstance(r, Exception):
            print(cid, "ERROR", r)
            continue
        print(cid, json.dumps({k: r.get(k) for k in ("deal", "supplier_id", "score", "c_open", "c_deal", "c_best")}))
    Path(f"runs/{a.cmd}-{int(time.time())}.json").write_text(json.dumps(out, default=str, indent=1))


if __name__ == "__main__":
    main()
