"""Summarise live A/B sessions (runs/ab/results.tsv from scripts/ab.sh): per session the score, our and the supplier's
prices per round, and the supplier's own calls from the back office (c = counter, h = hold, F = final_offer,
a = accept); then the mean score per label and case.

    uv run python scripts/ab_report.py [label ...]
"""

import glob
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def session_file(log, score=None):
    """The arena record a play log belongs to: the record of its case saved closest to the log's last write
    (the runner saves `runs/arena-<case>-<time>.json` when the session ends)."""
    case = re.search(r"-(P\d+)-\d{6}(?:-\d+)?\.log$", log)
    if not case:
        return None, None
    end = Path(log).stat().st_mtime
    files = sorted((abs(Path(f).stat().st_mtime - end), f) for f in glob.glob(f"runs/arena-{case.group(1)}-*.json"))
    for dt, f in files:
        if dt > 120:
            break
        rec = json.loads(Path(f).read_text(encoding="utf-8"))
        got = ((rec.get("state") or {}).get("result") or {}).get("score")
        if score is None or got is None or abs(got - score) < 0.0006:  # two sessions of a case may end together
            return f, rec
    return None, None


def calls(rec):
    out = []
    for b in rec.get("backoffice") or []:
        t = b.get("text") or ""
        if t.startswith("My call: "):
            out.append({"counter": "c", "hold": "h", "final_offer": "F", "accept": "a"}.get(t[9:].strip(" ."), "?"))
    return "".join(out)


def prices(rec):
    rows = []
    for th in (rec.get("threads") or {}).values():
        for m in th.get("messages") or []:
            sh = m.get("term_sheets") or []
            if sh:
                who = "B" if m.get("from") == "buyer" else "S"
                rows.append(f"{who}{min((x.get('terms') or {}).get('unit_price') or 0 for x in sh)}")
    return " ".join(rows)


def main(labels):
    rows = [ln.rstrip("\n").split("\t") for ln in open("runs/ab/results.tsv", encoding="utf-8") if ln.strip()]
    by = defaultdict(list)
    for r in rows:
        t, label, case, score = r[:4]
        if labels and label not in labels:
            continue
        log = r[-1]
        try:
            sc = float(score)
        except ValueError:
            sc = None
        _, rec = session_file(log, sc) if Path(log).exists() else (None, None)
        detail = f"{calls(rec):8} {prices(rec)}" if rec else ""
        print(f"{t} {label:10} {case} {score:>6}  {detail}")
        if sc is not None:
            by[(label, case)].append(sc)
    print()
    cases = sorted({c for _, c in by})
    for label in sorted({lab for lab, _ in by}):
        cells = [statistics.mean(by[(label, c)]) for c in cases if (label, c) in by]
        line = "  ".join(f"{c} {statistics.mean(by[(label, c)]):.3f}" for c in cases if (label, c) in by)
        print(f"{label:10} mean {statistics.mean(cells):.3f} over {len(cells)} cases | {line}")


if __name__ == "__main__":
    main(sys.argv[1:])
