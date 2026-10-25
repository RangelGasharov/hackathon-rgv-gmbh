# M3 retro — tuning search (read-only)
- Tasks 4 workers + 1 manager doc. First-try success 4/4, escalations 0, NEEDS_USER 0.
- Tokens (worker): T-010 haiku 123k (77 calls: script + long wait polling), T-012 sonnet 136k, T-014 sonnet 122k, T-011 sonnet 180k.
- Waste: T-010 kept waking on its own background job after DONE (3 extra notifications); T-010 card should say "run in the
  foreground with a long timeout". Manager read three 100-line findings files (needed for TUNING.md; acceptable).
- Billing: in-session workers billed the desktop subscription; user wants workers on the API key.
Changes adopted (copied to memory.md):
1. Workers run via .claude/api_worker.sh (API key), not in-session Agent.
2. Long-running commands in cards: foreground with a long timeout, no background jobs inside a worker.
