"""Local benchmark: serve a simulator in-process and play every case with one or more agents.

    uv run python -m buyer.bench --sim kit  --agents naive,buyer            # the kit's scripted supplier
    uv run python -m buyer.bench --sim twin --agents naive,buyer,buyer-claude --cases P01,P05
    uv run python -m buyer.bench --sim real --agents buyer --seeds 0,1,2    # the calibrated twin, all 19 cases
    uv run python -m buyer.bench --sim real --agents buyer --seeds 0,1,2 --against runs/bench-real-<before>.json

Many constants were fitted on the 10 hosted cases, and the final plays hidden ones: every run reports the hosted
cases and the other 9 apart, per twin seed, and `--against` an earlier run judges a change on both. A change that
helps the hosted 10 but costs the other 9 is rejected.

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

    if kind == "real":
        from .twin_real import make_twin_real

        return make_twin_real(cases_dir, data_dir, seed=seed, no_clock=not clock)
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


def hosted_ids(cases_dir="cases/real"):
    """The cases the practice arena hosts (the ones most constants were fitted on)."""
    from arena_kit.sim import load_cases

    return {cid for cid, c in load_cases(cases_dir).items() if c.get("hosted")}


def split(scores, hosted):
    """{"hosted": mean, "other": mean} of {case: score} (a group without cases is left out)."""
    out = {}
    for name, keep in (("hosted", True), ("other", False)):
        xs = [v for c, v in scores.items() if (c in hosted) == keep]
        if xs:
            out[name] = statistics.mean(xs)
    return out


def verdict(before, after, hosted, tol=0.002):
    """(keep, lines): judge a change from two runs, {seed: {case: score}} each, on the hosted cases and on the
    others, seed by seed. It is rejected when the other cases lose more than `tol` on average or on most seeds,
    whatever it does to the hosted ones."""
    seeds = sorted(set(before) & set(after))
    if not seeds:
        return False, ["no seed in common"]
    lines, d_host, d_other = [], [], []
    for seed in seeds:
        common = set(before[seed]) & set(after[seed])
        b = split({c: before[seed][c] for c in common}, hosted)
        a = split({c: after[seed][c] for c in common}, hosted)
        dh, do = a.get("hosted", 0) - b.get("hosted", 0), a.get("other", 0) - b.get("other", 0)
        d_host.append(dh)
        d_other.append(do)
        lines.append(f"seed {seed}: hosted {b.get('hosted', 0):.3f} -> {a.get('hosted', 0):.3f} ({dh:+.3f}), "
                     f"other {b.get('other', 0):.3f} -> {a.get('other', 0):.3f} ({do:+.3f})")  # fmt: skip
    mh, mo = statistics.mean(d_host), statistics.mean(d_other)
    worse = sum(d < -tol for d in d_other)
    if mo < -tol or worse > len(seeds) / 2:
        why = "helps the hosted cases, hurts the others" if mh > tol else "hurts the other cases"
        lines.append(f"REJECT: {why} (hosted {mh:+.3f}, other {mo:+.3f} over {len(seeds)} seeds)")
        return False, lines
    if mh < -tol and mo <= tol:
        lines.append(f"REJECT: hurts the hosted cases and gains nothing elsewhere (hosted {mh:+.3f}, other {mo:+.3f})")
        return False, lines
    lines.append(f"KEEP: hosted {mh:+.3f}, other {mo:+.3f} over {len(seeds)} seeds")
    return True, lines


def scores_of(saved, agent):
    """{seed: {case: score}} of one agent from a saved bench file (also the older one-seed format)."""
    runs = saved.get("seeds") or {"0": saved}
    return {seed: {c: (r.get("score") or 0.0) for c, r in t[agent].items()} for seed, t in runs.items() if agent in t}


def run_seed(a, seed, world, ids=None):
    """{agent: {case: result}} for one simulator seed."""
    sim = make_sim(a.sim, a.cases_dir, a.data, a.clock, seed)
    url, server = serve(sim)
    ids = ids or ([f"case:{c}" for c in a.cases.split(",")] if a.cases else sorted(sim.cases))
    table = {}
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
    return table


def main(argv=None):
    load_env()
    ap = argparse.ArgumentParser(prog="bench")
    ap.add_argument("--sim", default="twin", choices=["kit", "twin", "real"])
    ap.add_argument("--agents", default="naive,buyer")
    ap.add_argument("--cases", default="")
    ap.add_argument("--cases-dir", default=None, help="default: cases/real for --sim real, else cases/practice")
    ap.add_argument("--data", default="data/package")
    ap.add_argument("--clock", action="store_true", help="run with the 60-second rule (slower, realistic)")
    ap.add_argument("-p", "--parallel", type=int, default=6)
    ap.add_argument("--seed", type=int, default=0, help="twin: hidden supplier parameters")
    ap.add_argument("--seeds", default="", help="several twin seeds, e.g. 0,1,2 (overrides --seed)")
    ap.add_argument("--against", default="", help="an earlier bench file: keep or reject the change")
    a = ap.parse_args(argv)
    a.cases_dir = a.cases_dir or ("cases/real" if a.sim == "real" else "cases/practice")
    from .dossier import World
    from .llm import spent

    world = World(a.data)
    seeds = [int(x) for x in a.seeds.split(",") if x.strip()] if a.seeds else [a.seed]
    hosted = hosted_ids()
    t0, usd0, runs = time.time(), spent(), {}
    for seed in seeds:
        table = runs[str(seed)] = run_seed(a, seed, world)
        names = list(table)
        ids = sorted(next(iter(table.values())))
        print(f"\nseed {seed}\n{'case':10}" + "".join(f"{n:>14}" for n in names))
        for cid in ids:
            row = []
            for n in names:
                r = table[n][cid]
                row.append("ERR" if "error" in r else f"{r.get('score', 0):.2f}{'' if r.get('deal') else ' nd'}")
            print(f"{cid:10}" + ("*" if cid in hosted else " ") + "".join(f"{x:>13}" for x in row))
        means = {n: statistics.mean([table[n][c].get("score") or 0 for c in ids]) for n in names}
        print(f"{'mean×100':10}" + "".join(f"{100 * means[n]:>14.1f}" for n in names))
        for n in names:
            parts = split({c: table[n][c].get("score") or 0.0 for c in ids}, hosted)
            print(f"  {n}: " + ", ".join(f"{k} (* = hosted) {100 * v:.1f}" if k == "hosted" else f"{k} {100 * v:.1f}"
                                         for k, v in parts.items()))  # fmt: skip
    print(f"\n{time.time() - t0:.0f}s, LLM spend this run ${spent() - usd0:.3f}, total ${spent():.3f}")
    Path("runs").mkdir(exist_ok=True)
    out = Path(f"runs/bench-{a.sim}-{int(time.time())}.json")
    out.write_text(json.dumps({"seeds": runs}, default=str, indent=1))
    print("details:", out)
    if a.against:
        before = json.loads(Path(a.against).read_text())
        for n in [x for x in a.agents.split(",") if x != "naive"]:
            keep, lines = verdict(scores_of(before, n), scores_of({"seeds": runs}, n), hosted)
            print(f"\n{n} against {a.against}:\n  " + "\n  ".join(lines))


if __name__ == "__main__":
    main()
