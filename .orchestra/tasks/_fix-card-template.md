# T-NNN-fix<k>: fix <specific failure> in T-NNN
Tier: <one above the failed attempt> · Escalated from: <tier> · Original card: tasks/T-NNN.md

## Already done — keep it
<What works now and must not be redone, rewritten or refactored.>

## The failure
<Exact error text / failing test name / wrong behaviour.>

## Already tried
<What the previous worker tried and why it didn't work.>

## Narrow goal
<Make the failing check pass without weakening what it asserts. Nothing else.>

## Files
<Every file the failure could live in, including the test's own setup, mocks, helpers and error handlers. Never mark a file "correct, do not edit" unless you checked it.>

## Done when
- [ ] `<the check that failed before>` → passes

## Report back
STATUS (DONE | BLOCKED | FAILED | NEEDS_USER) · CHANGED · CHECK · NOTES (include the root cause in one line) — max 10 lines.
NEEDS_USER = a permission, connection, credential or service problem. Stop at once: no retries, no workarounds.
