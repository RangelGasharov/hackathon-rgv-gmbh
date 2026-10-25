"""Pull every team practice session from the arena (read-only GETs only).

Saves runs/team/<case>-<session_id>.json per session (skips files that already exist, so a rerun
resumes) and writes runs/team/index.tsv sorted by case, then score descending.
"""

import json
import os
import pathlib
import sys

from arena_kit.client import ArenaClient, ArenaError

for line in open(".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

OUT = pathlib.Path("runs/team")
OUT.mkdir(parents=True, exist_ok=True)
AUTH_STOP = {401, 403, 429}  # not retried: credential / quota problems need the user
INDEX_COLS = ["case", "session_id", "score", "deal", "end_reason", "rounds", "created_at", "n_threads"]


def dump(obj):
    return json.dumps(obj, default=str, indent=1, ensure_ascii=False)


def case_tag(s):
    return str(s.get("case_id", "")).split(":")[-1]


def path_for(s):
    return OUT / f"{case_tag(s)}-{s['session_id']}.json"


def save(path, obj):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump(obj), encoding="utf-8")
    tmp.replace(path)


def cell(v):
    return "" if v is None else str(v).replace("\t", " ").replace("\n", " ")


def main():
    c = ArenaClient(rate=3)
    sessions = c.sessions("practice")
    total = len(sessions)
    print(f"{total} practice sessions listed", flush=True)
    stats = {"saved": 0, "skipped": 0, "backoffice_errors": 0, "thread_errors": 0}

    try:
        for i, s in enumerate(sessions, 1):
            p = path_for(s)
            if p.exists():
                stats["skipped"] += 1
            else:
                sid = s["session_id"]
                state = c.session(sid)
                threads = {}
                for sup in state.get("threads") or {}:
                    try:
                        threads[sup] = c.thread(sid, sup)
                    except ArenaError as e:
                        if e.status in AUTH_STOP:
                            raise
                        threads[sup] = {"error": str(e)}
                        stats["thread_errors"] += 1
                try:
                    back = c.backoffice(sid)
                except ArenaError as e:
                    if e.status in AUTH_STOP:
                        raise
                    back = {"error": str(e)}
                    stats["backoffice_errors"] += 1
                save(p, {"result": state.get("result"), "state": state, "threads": threads, "backoffice": back})
                stats["saved"] += 1
            if i % 20 == 0:
                print(f"progress {i}/{total} {stats}", flush=True)
    except ArenaError as e:
        print(f"STOPPED: ArenaError {e.status}: {e.detail}", flush=True)
        print(f"stats so far {stats}", flush=True)
        return 2

    rows = []
    for s in sessions:
        data = json.loads(path_for(s).read_text(encoding="utf-8"))
        rows.append({
            "case": case_tag(s),
            "session_id": s["session_id"],
            "score": s.get("score"),
            "deal": s.get("deal"),
            "end_reason": s.get("end_reason"),
            "rounds": s.get("rounds"),
            "created_at": s.get("created_at"),
            "n_threads": len(data.get("threads") or {}),
        })
    rows.sort(key=lambda r: (r["case"], -(r["score"] if isinstance(r["score"], (int, float)) else float("-inf"))))
    lines = ["\t".join(INDEX_COLS)] + ["\t".join(cell(r[k]) for k in INDEX_COLS) for r in rows]
    (OUT / "index.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"done: {total} sessions, {stats}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
