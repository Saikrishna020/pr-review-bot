# Golden-set eval results

Run at: 2026-09-15T04:32:20.658843+00:00

**Overall: 8/12**

## Jira alignment

5/6 verdicts correct.

| Case | Ticket | Verdict OK | Extra checks OK |
|---|---|---|---|
| scrum1-fully-implemented | ❌ | ✅ |
| scrum2-unrelated-diff | ✅ | ✅ |
| scrum3-two-of-three | ✅ | ✅ |
| scrum4-buried-edge-case | ✅ | ✅ |
| scrum5-vague-ticket | ✅ | ✅ |
| scrum6-wrong-ticket-linked | ✅ | ✅ |

## Issue detection

3/6 cases passed.
False positives: clean-rename, correct-refactor
Missed real issues: style-nit

| Case | Passed |
|---|---|
| clean-rename | ❌ |
| off-by-one | ✅ |
| null-crash | ✅ |
| style-nit | ❌ |
| correct-refactor | ❌ |
| hardcoded-secret | ✅ |
