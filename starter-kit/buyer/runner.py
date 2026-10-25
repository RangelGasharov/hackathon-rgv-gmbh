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
from arena_kit.docs import read_document
from arena_kit.play import RemoteTable

from .agent import ENDED, BuyerAgent, latest
from .dossier import World

log = logging.getLogger("buyer")
MOVE_BUDGET_S = 45  # our own deadline per message, well inside the arena's 60 s
RESENDS = 3  # a message a transient error stopped is sent again this often, once a re-read shows it did not arrive


def cache_attachments(table):
    """Download each attachment once per session: a refresh after a 409 or 422 re-reads every thread, and
    the saved record gets the texts too. The bytes stay in memory only (`table.files`)."""
    if getattr(table, "files", None) is not None:
        return
    get, table.files = table.attachment, {}

    def attachment(attachment_id):
        if attachment_id not in table.files:
            table.files[attachment_id] = get(attachment_id)
        return table.files[attachment_id]

    table.attachment = attachment


def with_timeouts(th):
    """A thread's messages with the supplier's answers to our late moves (`timeouts`) put in place: a
    timeout of round r stands where our round-r message would have been (arena b2 P05: an "agreed" and a
    withdrawal sat only in `timeouts`, so we kept accepting an offer that was gone)."""
    tos = sorted(th.get("timeouts") or [], key=lambda t: t.get("round") or 0)
    out, rnd, i = [], 0, 0
    for m in th.get("messages") or []:
        if m.get("from") == "buyer" and not (m.get("action") or {}).get("type"):
            while i < len(tos) and (tos[i].get("round") or 0) <= rnd + 1:
                out.append(_timeout_message(tos[i]))
                rnd, i = max(rnd, tos[i].get("round") or 0), i + 1
            rnd += 1
        out.append(m)
    out += [_timeout_message(t) for t in tos[i:]]
    return out


def _timeout_message(t):
    sheets = [{"term_sheet_id": t["offer_id"], "terms": t.get("terms") or {}}] if t.get("offer_id") else []
    sheets += [{"term_sheet_id": a.get("offer_id"), "terms": a.get("terms") or a} for a in t.get("alternatives") or []]
    return {
        "from": "timeout",
        "status": t.get("status"),
        "body": t.get("text") or "",
        "term_sheets": [x for x in sheets if x["term_sheet_id"]],
        "claim_results": t.get("claim_results") or [],
        "signals": t.get("signals"),
        "missing_fields": t.get("missing_fields") or [],
        "timeout_round": t.get("round"),
    }


def safe_fetch(table, message):
    """`fetch_documents`, except that an attachment which cannot be fetched or read gets an empty text: a broken
    PDF must not end the worker that was about to answer the supplier."""
    for att in message.get("attachments") or []:
        if att.get("attachment_id") and "text" not in att:
            try:
                att["text"] = read_document(att["name"], table.attachment(att["attachment_id"]))
            except Exception as e:  # noqa: BLE001
                log.warning("attachment %s unreadable: %s", att.get("name"), e)
                att["text"] = ""
    return message


def transient(e):
    """A server error, a rate limit or a dropped connection the client's own retries did not get past."""
    return not isinstance(e, ArenaError) or e.status >= 500 or e.status == 429


def play(table, case_id, agent_factory, deadline=None, case=None):
    """One case to its end. Returns the result dict.

    No error path signs, walks or goes silent: a failed decision gets the agent's safe fallback (a holding note or a
    small move), a failed e-mail the template, a transient arena error a re-read and, if the message did not arrive,
    a resend; a worker never stops while its case is open. Only a closed case, or the arena refusing our key or the
    session (401, 403, 404), ends the play."""
    cache_attachments(table)
    opened_case, threads = table.open(case_id)
    case = opened_case or case
    if getattr(table, "done", None):
        return table.done
    for events in threads.values():
        for m in events:
            safe_fetch(table, m)
    reply_at = {sid: time.monotonic() for sid in threads}  # when each supplier last answered: the clocks run
    agent = agent_factory(deadline)
    try:
        agent.start(case, threads)  # with a model writer: the persona reads, at most `read_timeout` seconds
    except Exception:  # noqa: BLE001 - the persona read is a bonus; the dossiers are not
        log.exception("%s: start failed; starting again without the persona read", case_id)
        agent.read_timeout = 0
        agent.start(case, threads)
    lock, done, result = threading.Lock(), threading.Event(), {}

    def finish(res):
        if not done.is_set():
            result.update(res or {})
            done.set()

    def result_now():
        try:
            return table.result()
        except Exception as e:  # noqa: BLE001
            log.warning("%s: no result readable (%s)", case_id, e)
            return {"deal": False, "end_reason": f"result unreadable: {e}"}

    def ended():
        """The result if the case has ended, else None (also when the arena cannot be asked)."""
        try:
            state = table.client.session(table.session_id)
        except Exception:  # noqa: BLE001
            return None
        if state.get("status") != "ended":
            return None
        return state.get("result") or {"deal": False, "end_reason": state.get("end_reason"), **state}

    def refresh(sid):
        """Re-read a thread from the arena (after a stale accept or a late move)."""
        try:
            th = table.client.thread(table.session_id, sid)
            threads[sid] = [safe_fetch(table, m) for m in with_timeouts(th)]
        except Exception as e:  # noqa: BLE001 - keep playing on what we have
            log.warning("refresh %s failed: %s", sid, e)

    failed = {}  # (supplier, sheet id) -> failed accepts
    sending = {}  # worker -> the supplier its message in flight is for (to clear `in_flight` after an error)

    def close(decision):
        """Send an accept or a walk-away (under the lock). True: the case is over; None: decide again; else the
        decision to send instead (its own terms back, after the same accept failed twice)."""
        try:
            finish(table.close(decision["supplier_id"], decision["message"]))
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("%s: close failed (%s); re-reading the threads", case_id, e)
            if isinstance(e, ArenaError) and e.status == 409 and "ended" in str(e.detail):
                finish(result_now())
                return True
            if transient(e) and (res := ended()) is not None:  # it may have gone through
                finish(res)
                return True
            for s in threads:
                refresh(s)
            key = (decision["supplier_id"], (decision["message"]["action"] or {}).get("term_sheet_id"))
            failed[key] = failed.get(key, 0) + 1
            if failed[key] >= 2:  # still refused after a re-read: offer its own terms back instead
                return agent.reoffer(threads, decision["supplier_id"])
            return None

    def deliver(decision, t0):
        """Write and send one message, read the reply. True: the case is over."""
        target = decision["supplier_id"]
        # the 60 s per message run from the supplier's last reply: give the writer what is left of them
        budget = 60.0 - (time.monotonic() - reply_at.get(target, time.monotonic())) - 12.0
        msg = agent.write(decision["message"], budget)
        took = time.monotonic() - t0
        if took > MOVE_BUDGET_S:
            log.warning("%s %s: decision took %.0fs", case_id, target, took)
        before = sum(m.get("from") == "buyer" for m in threads[target])
        for attempt in range(RESENDS + 1):
            try:
                reply = table.send(target, msg)
                break
            except ArenaError as e:
                if e.status == 409:  # late or ended: read what the arena really holds
                    agent.t[target].in_flight = False
                    if (res := ended()) is not None:
                        finish(res)
                        return True
                    with lock:
                        refresh(target)
                        reply_at[target] = time.monotonic()
                    return False
                if e.status in (400, 422):  # invalid message: nothing happened; re-read and decide again
                    log.warning("%s %s: %s", case_id, target, e)
                    agent.t[target].in_flight = False
                    with lock:  # a value outside the options (the supplier's own standard) is taken back
                        if agent.invalid_message(target):
                            log.warning("%s %s: offering term options only from now on", case_id, target)
                    refresh(target)
                    time.sleep(0.5)
                    return False
                if not transient(e):  # our key or the session refused: nothing to play on
                    log.warning("%s %s: %s", case_id, target, e)
                    agent.t[target].in_flight = False
                    finish(result_now())
                    return True
                err = e
            except Exception as e:  # noqa: BLE001 - a dropped connection after the client's own retries
                err = e
            log.warning("%s %s: send failed (%s); checking whether it arrived", case_id, target, err)
            with lock:
                refresh(target)
                if sum(m.get("from") == "buyer" for m in threads[target]) > before:  # it did: the reply is read
                    agent.t[target].in_flight = False
                    reply_at[target] = time.monotonic()
                    return False
            if (res := ended()) is not None:
                finish(res)
                return True
            time.sleep(min(4.0, 2.0**attempt))
        else:
            agent.t[target].in_flight = False  # it never arrived: decide again on what the arena holds
            return False
        if "result" in reply:
            finish(reply["result"])
            return True
        with lock:
            threads[target] += [{**msg, "from": "buyer"}, safe_fetch(table, reply)]
            agent.t[target].in_flight = False
            reply_at[target] = time.monotonic()
            agent.note_round(time.monotonic() - t0)
        if reply.get("status") in ENDED and all(latest(ev).get("status") in ENDED for ev in threads.values()):
            finish(result_now())
            return True
        return False

    def step(sid):
        """One decision for thread `sid` and, if it sends something, the round trip. True: this worker is done."""
        cap = deadline() if callable(deadline) else deadline
        agent.deadline = cap
        t0 = time.monotonic()
        with lock:
            if done.is_set():
                return True
            try:
                decision = agent.decide(threads, sid)
            except Exception:  # noqa: BLE001 - one odd reply must not leave this supplier unanswered
                log.exception("%s %s: decide failed; using the safe fallback", case_id, sid)
                decision = agent.safe_fallback(threads, sid)
            if decision and decision["message"].get("action"):
                # close while holding the lock: no other thread can change a standing sheet now
                decision = close(decision)
                if decision is True:
                    return True
            if decision:
                agent.t[decision["supplier_id"]].in_flight = True
                sending[sid] = decision["supplier_id"]
        if decision is None:
            if latest(threads[sid]).get("status") in ENDED or agent.t[sid].dead:
                return True
            # nobody answers this supplier for now: before its 60 s run out, a short holding note (a
            # timeout repeats our last offer and costs patience; three end the thread - arena b2 P05)
            if time.monotonic() - reply_at.get(sid, time.monotonic()) > 45 and not agent.t[sid].in_flight:
                with lock:
                    decision = agent.keep_alive(threads, sid)
                    if decision:
                        agent.t[sid].in_flight = True
                        sending[sid] = sid
            if decision is None:
                time.sleep(1.0)  # another thread decides this case; check back
                return False
        over = deliver(decision, t0)
        sending.pop(sid, None)
        return over

    def worker(sid):
        errors = 0
        while not done.is_set():
            try:
                if step(sid):
                    return
                errors = 0
            except Exception:  # noqa: BLE001 - a worker never stops while its case is open: a silent thread times out
                errors += 1
                log.exception("%s %s: worker error %d; carrying on", case_id, sid, errors)
                if (target := sending.pop(sid, None)) in agent.t:
                    agent.t[target].in_flight = False
                time.sleep(min(5.0, 0.5 * errors))

    with ThreadPoolExecutor(max_workers=len(threads)) as pool:
        list(pool.map(worker, list(threads)))
    if not done.is_set():
        finish(result_now())
    result["agent_log"] = agent.log
    return result


def make_factory(world, writer_name="template"):
    from .writer import StrategistWriter, TemplateWriter

    writer = TemplateWriter()
    if writer_name == "claude":
        from .llm import Claude

        llm = Claude()
        try:  # warm up before any clock runs: connection, and whether the fallback beta is accepted
            t0 = time.monotonic()
            llm.complete("Reply with OK.", "OK", max_tokens=50)
            log.warning("Claude %s ready (%.1fs warm-up)", llm.model, time.monotonic() - t0)
        except Exception as e:  # noqa: BLE001 - the template writer still works
            log.warning("Claude warm-up failed (%s): e-mails fall back to the template", e)
        writer = StrategistWriter(llm.complete)  # Opus chooses among the code's options and writes the e-mail

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
                    log.warning("case list failed (%s); retrying", e)
                cases = []
            except Exception as e:  # noqa: BLE001 - a network drop must not end the final
                log.warning("case list failed (%s); retrying", e)
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
            for th in threads.values():  # keep the attachment texts (8D reports, schedules) for later study
                for m in th.get("messages", []):
                    for att in m.get("attachments") or []:
                        if (data := (table.files or {}).get(att.get("attachment_id"))) is not None:
                            att["text"] = read_document(att["name"], data)
            try:
                back = client.backoffice(table.session_id)
            except ArenaError as e:
                back = {"error": str(e)}
            stamp = int(time.time())
            Path(f"runs/arena-{case_id.split(':')[-1]}-{stamp}.json").write_text(
                json.dumps(
                    {"result": result, "state": state, "threads": threads, "backoffice": back},
                    default=str,
                    indent=1,
                    ensure_ascii=False,
                ),
                encoding="utf-8",  # Windows would write cp1252, which load_runs cannot read
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
    ap.add_argument("-p", "--parallel", type=int, default=10)
    ap.add_argument("--waves", type=int, default=5)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(asctime)s %(message)s")
    if not (Path(a.data) / "facts.json").is_file():
        raise SystemExit(f"no data package at {Path(a.data).resolve()}: run `uv run arena data` or set ARENA_DATA")
    world = World(a.data)
    if world.pkg.release:
        log.warning("Sunday release loaded from %s", world.pkg.release)
    elif time.time() > 1792904400:  # 2026-10-25 07:00 UTC: the release should be there by now
        log.warning("no Sunday release found next to %s: run `uv run arena data` again", a.data)
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
    Path(f"runs/{a.cmd}-{int(time.time())}.json").write_text(json.dumps(out, default=str, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
