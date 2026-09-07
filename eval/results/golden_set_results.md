# Golden-set eval results

Run at: 2026-09-04T05:31:17.311487+00:00

**Overall: 10/12**

## Jira alignment

6/6 verdicts correct.

| Case | Ticket | Verdict OK | Extra checks OK |
|---|---|---|---|
| scrum1-fully-implemented | ✅ | ✅ |
| scrum2-unrelated-diff | ✅ | ✅ |
| scrum3-two-of-three | ✅ | ✅ |
| scrum4-buried-edge-case | ✅ | ✅ |
| scrum5-vague-ticket | ✅ | ✅ |
| scrum6-wrong-ticket-linked | ✅ | ✅ |

## Issue detection

4/6 cases passed.
False positives: clean-rename
Missed real issues: style-nit

| Case | Passed |
|---|---|
| clean-rename | ❌ |
| off-by-one | ✅ |
| null-crash | ✅ |
| style-nit | ❌ |
| correct-refactor | ✅ |
| hardcoded-secret | ✅ |
