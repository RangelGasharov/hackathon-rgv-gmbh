# Retro M1+M2 — evidence and live A/B

- Workers: 2 Sonnet analysis tasks (T-001 214k tokens, T-004 198k), both first-try correct; both blocked from writing
  report files (subagent Write policy) and returned findings in NOTES instead. Then the user banned subagents (01:55).
- Most valuable: T-004's back-office decision rule and the other agent's LLM-led results; the twins misjudged both lean
  (+5 on the persona twin, a loss live) and the deep openings, so live A/B decided everything.
- Waste: 4 sessions lost to 429 (14 opened at once; limit 10 per key), 3 sessions stalled to 0 under local CPU load,
  one batch broken by editing ab.sh while it ran (zsh reads scripts as it goes).
- Rules adopted (memory.md): max 6 open sessions; nothing heavy while sessions run; never edit a running script; no
  subagents; check weekly usage (stop at 35 %) before each batch.
