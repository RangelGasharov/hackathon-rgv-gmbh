"""Local benchmark: serve a simulator in-process and play every case with one or more agents.

    uv run python -m buyer.bench --sim kit  --agents naive,buyer            # the kit's scripted supplier
    uv run python -m buyer.bench --sim twin --agents naive,buyer,buyer-claude --cases P01,P05

Nothing here talks to the hosted arena.
"""

import argparse
import json
import socket
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from arena_kit.cli import load_env
from arena_kit.client import ArenaClient
from arena_kit.play import RemoteTable
from arena_kit.play import play as kit_play


def serve(sim):
    import uvicorn

    from arena_kit.sim import create_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(sim), port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}", server


def make_sim(kind, cases_dir, data_dir, clock, seed=0):
    from arena_kit.data import DataPackage

    if kind == "kit":
        from arena_kit.sim import Sim, load_cases

        return Sim(load_cases(cases_dir), DataPackage(data_dir).facts, no_clock=not clock)
    from .twin import make_twin

    return make_twin(cases_dir, data_dir, seed=seed, no_clock=not clock)


def agent_runner(name, world):
    if name == "naive":
        from arena_kit.agents import NaiveAgent

        return lambda client, cid: kit_play(RemoteTable(client), cid, NaiveAgent())
    if name == "kit-llm":
        from arena_kit.agents import LLMAgent

        from .llm import Claude

        complete = Claude().complete
        return lambda client, cid: kit_play(RemoteTable(client), cid, LLMAgent(complete, world.pkg))
    from .runner import make_factory, play

    factory = make_factory(world, "claude" if name == "buyer-claude" else "template")
    return lambda client, cid: play(RemoteTable(client), cid, factory)


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(prog="bench")
    ap.add_argument("--sim", default="twin", choices=["kit", "twin"])
    ap.add_argument("--agents", default="naive,buyer")
    ap.add_argument("--cases", default="")
    ap.add_argument("--cases-dir", default="cases/practice")
    ap.add_argument("--data", default="data/package")
    ap.add_argument("--clock", action="store_true", help="run with the 60-second rule (slower, realistic)")
    ap.add_argument("-p", "--parallel", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0, help="twin: hidden supplier parameters")
    a = ap.parse_args(argv)
    from .dossier import World
    from .llm import spent

    world = World(a.data)
    sim = make_sim(a.sim, a.cases_dir, a.data, a.clock, a.seed)
    url, server = serve(sim)
    ids = [f"case:{c}" for c in a.cases.split(",")] if a.cases else sorted(sim.cases)
    table, t0, usd0 = {}, time.time(), spent()
    for name in a.agents.split(","):
        client = ArenaClient(url, ArenaClient(url).register(f"bench-{name}-{time.time()}", "sim")["api_key"])
        run = agent_runner(name, world)

        def one(cid, run=run, client=client):
            try:
                return cid, run(client, cid)
            except Exception as e:  # noqa: BLE001 - report, keep benchmarking
                import traceback

                return cid, {"error": repr(e), "trace": traceback.format_exc()[-1500:]}

        with ThreadPoolExecutor(max_workers=a.parallel) as pool:
            table[name] = dict(pool.map(one, ids))
    server.should_exit = True
    names = list(table)
    print(f"\n{'case':10}" + "".join(f"{n:>14}" for n in names))
    for cid in ids:
        row = []
        for n in names:
            r = table[n][cid]
            row.append("ERR" if "error" in r else f"{r.get('score', 0):.2f}{'' if r.get('deal') else ' nd'}")
        print(f"{cid:10}" + "".join(f"{x:>14}" for x in row))
    means = {n: statistics.mean([table[n][c].get("score") or 0 for c in ids]) for n in names}
    print(f"{'mean×100':10}" + "".join(f"{100 * means[n]:>14.1f}" for n in names))
    print(f"\n{time.time() - t0:.0f}s, LLM spend this run ${spent() - usd0:.3f}, total ${spent():.3f}")
    Path("runs").mkdir(exist_ok=True)
    out = Path(f"runs/bench-{a.sim}-{int(time.time())}.json")
    out.write_text(json.dumps(table, default=str, indent=1))
    print("details:", out)


if __name__ == "__main__":
    main()
