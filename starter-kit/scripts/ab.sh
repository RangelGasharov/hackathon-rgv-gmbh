#!/bin/zsh
# Live A/B batch: one practice session per case with the given switches, in parallel; each session's result line is
# appended to runs/ab/results.tsv as: time  label  case  score  deal  supplier  log.
#   ./scripts/ab.sh <label> "<env, e.g. BUYER_OFF=menu>" P02 P08 P11
# Every case costs one practice session: count it against the budget in .orchestra/memory.md.
# The arena allows 10 open sessions per key (429 beyond, shared with the team): at most AB_MAX (6) run at once here,
# so run one ab.sh at a time.
set -u
max=${AB_MAX:-6}
label=$1; envs=$2; shift 2
mkdir -p runs/ab
for c in "$@"; do
  while (( $(jobs -r | wc -l) >= max )); do sleep 3; done
  (
    log="runs/ab/${label}-${c}-$(date +%H%M%S)-${RANDOM}.log"
    env ${=envs} uv run python -m buyer.runner play "case:${c}" --writer claude > "$log" 2>&1
    line=$(grep "^case:${c} {" "$log" | tail -1)
    json=${line#case:${c} }
    score=$(print -r -- "$json" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(round(d.get("score") or 0, 3), d.get("deal"), d.get("supplier_id"), sep="\t")' 2>/dev/null)
    printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%H:%M:%S)" "$label" "$c" "${score:-ERROR}" "$log" >> runs/ab/results.tsv
  ) &
done
wait
