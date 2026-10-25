"""Regression gate: tests + six offline benchmarks, compared with scripts/gate_baseline.json.

Run from anywhere:  python scripts/gate.py            (exit 1 if anything got worse)
                    python scripts/gate.py --update   (rewrite the baseline from the current code)
Offline only: no arena, no Anthropic API. Child processes get defaults (BUYER_* stripped).
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "scripts" / "gate_baseline.json"
PY = sys.executable

MEAN_DROP = 0.2  # mean x100
CASE_WARN = 0.05  # score units, per case (warn only)
CATEGORY_DROP = 0.03  # score units, per category mean (fail)

BENCHES = [
    ("twin-s0", [PY, "-m", "buyer.bench", "--sim", "twin", "--agents", "buyer", "--seed", "0", "-p", "2"]),
    ("twin-s1", [PY, "-m", "buyer.bench", "--sim", "twin", "--agents", "buyer", "--seed", "1", "-p", "2"]),
    ("twin-s2", [PY, "-m", "buyer.bench", "--sim", "twin", "--agents", "buyer", "--seed", "2", "-p", "2"]),
    ("twin-s3", [PY, "-m", "buyer.bench", "--sim", "twin", "--agents", "buyer", "--seed", "3", "-p", "2"]),
    ("kit", [PY, "-m", "buyer.bench", "--sim", "kit", "--agents", "buyer", "-p", "2"]),
    ("twin_real", [PY, "-m", "buyer.twin_real", "bench"]),
]
TESTS = [PY, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]

# bench.py rows: "case:P01   0.03" or "case:P01   0.00 nd" or "case:P01   ERR"
BENCH_LINE = re.compile(r"^case:(P\d+)\s+(ERR|[\d.]+)(\s+nd)?\s*$")
# twin_real rows: "case:P01   0.603  -  0.60, 0.55  deal  <price>  <statuses>"
TWIN_LINE = re.compile(r"^(?:case:)?(P\d+)\s+([\d.]+)\s.*?\s(deal|no)\s")


def child_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("BUYER_")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(cmd):
    return subprocess.run(cmd, cwd=ROOT, env=child_env(), capture_output=True, text=True, encoding="utf-8", errors="replace")


def case_types():
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))["type"] for p in sorted((ROOT / "cases" / "practice").glob("P*.json"))}


def run_tests():
    r = run(TESTS)
    lines = (r.stdout or r.stderr).strip().splitlines()
    return r.returncode == 0, lines[-1] if lines else "no output"


def parse_rows(name, out):
    rows, errors = {}, []
    for line in out.splitlines():
        if name == "twin_real":
            m = TWIN_LINE.match(line)
            if m:
                rows[m.group(1)] = (float(m.group(2)), m.group(3) == "no")
            continue
        m = BENCH_LINE.match(line)
        if m:
            case, score, nd = m.group(1), m.group(2), bool(m.group(3))
            if score == "ERR":
                errors.append(case)
                rows[case] = (0.0, True)
            else:
                rows[case] = (float(score), nd)
    return rows, errors


def run_bench(name, cmd, types):
    t0 = time.time()
    r = run(cmd)
    if r.returncode != 0:
        return {"errors": [f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}"]}
    rows, errors = parse_rows(name, r.stdout)
    if not rows:
        return {"errors": ["no case rows parsed"]}
    cases = {c: s for c, (s, _) in rows.items()}
    cats = {}
    for c, s in cases.items():
        cats.setdefault(types.get(c, "unknown"), []).append(s)
    return {
        "mean100": 100 * statistics.mean(cases.values()),
        "nd": sum(1 for _, nd in rows.values() if nd),
        "cases": {c: round(s, 6) for c, s in sorted(cases.items())},
        "categories": {k: round(statistics.mean(v), 6) for k, v in sorted(cats.items())},
        "errors": errors,
        "seconds": round(time.time() - t0),
    }


def compare(cur, base):
    fails, warns = [], []
    for name, c in cur.items():
        if c["errors"]:
            fails.append(f"{name}: errors {c['errors']}")
            continue
        b = base.get(name)
        if b is None:
            fails.append(f"{name}: no baseline entry (run --update)")
            continue
        if b["mean100"] - c["mean100"] > MEAN_DROP:
            fails.append(f"{name}: mean x100 {b['mean100']:.2f} -> {c['mean100']:.2f}")
        if c["nd"] > b["nd"]:
            fails.append(f"{name}: no-deals {b['nd']} -> {c['nd']}")
        for cat, bm in b["categories"].items():
            cm = c["categories"].get(cat)
            if cm is None:
                fails.append(f"{name}: category {cat} missing")
            elif bm - cm > CATEGORY_DROP:
                fails.append(f"{name}: category {cat} {bm:.3f} -> {cm:.3f}")
        for case, bs in b["cases"].items():
            cs = c["cases"].get(case)
            if cs is not None and bs - cs > CASE_WARN:
                warns.append(f"{name} {case}: {bs:.2f} -> {cs:.2f}")
    return fails, warns


def print_table(cur, base):
    names = list(cur)
    print(f"\n{'benchmark':12}{'mean x100':>11}{'base':>9}{'delta':>8}{'no-deal':>9}{'base':>6}")
    for n in names:
        c = cur[n]
        if c["errors"]:
            print(f"{n:12}ERROR {c['errors']}")
            continue
        b = (base or {}).get(n)
        bm, bnd = (f"{b['mean100']:.2f}", str(b["nd"])) if b else ("-", "-")
        d = f"{c['mean100'] - b['mean100']:+.2f}" if b else "-"
        print(f"{n:12}{c['mean100']:>11.2f}{bm:>9}{d:>8}{c['nd']:>9}{bnd:>6}")
    cats = sorted({k for n in names for k in cur[n].get("categories", {})})
    print(f"\n{'category':16}" + "".join(f"{n:>12}" for n in names))
    for k in cats:
        row = [f"{cur[n]['categories'][k]:.3f}" if k in cur[n].get("categories", {}) else "-" for n in names]
        print(f"{k:16}" + "".join(f"{x:>12}" for x in row))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update", action="store_true", help="rewrite scripts/gate_baseline.json from this run")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time()

    tests_ok, tests_line = run_tests()
    print(f"tests: {'PASS' if tests_ok else 'FAIL'} — {tests_line}", flush=True)
    types = case_types()
    cur = {}
    for name, cmd in BENCHES:
        print(f"bench {name} ...", flush=True)
        cur[name] = run_bench(name, cmd, types)
    print_table(cur, json.loads(BASELINE.read_text(encoding="utf-8"))["benches"] if BASELINE.exists() and not a.update else None)

    broken = [n for n, c in cur.items() if c["errors"]]
    if a.update:
        if not tests_ok or broken:
            print("\nBASELINE NOT WRITTEN: tests or benchmarks failed")
            return 1
        data = {"created": date.today().isoformat(), "tests": tests_line, "benches": {
            n: {k: v for k, v in c.items() if k != "seconds"} for n, c in cur.items()}}
        BASELINE.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nbaseline written: {BASELINE.relative_to(ROOT)}")
        ok = True
    else:
        if not BASELINE.exists():
            print("\nno baseline: run python scripts/gate.py --update")
            return 1
        base = json.loads(BASELINE.read_text(encoding="utf-8"))["benches"]
        fails, warns = compare(cur, base)
        if not tests_ok:
            fails.insert(0, f"tests failed: {tests_line}")
        for w in warns:
            print(f"WARN case drop > {CASE_WARN}: {w}")
        for f in fails:
            print(f"FAIL: {f}")
        ok = not fails
    print(f"\n{'PASS' if ok else 'FAIL'}  ({time.time() - t0:.0f}s, per bench: "
          + ", ".join(f"{n} {c.get('seconds', '-')}s" for n, c in cur.items()) + ")")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
