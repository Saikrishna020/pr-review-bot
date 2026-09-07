# Real-PR eval batch

| PR | Ticket | Seeded mistake | Expected | Bot said | Match |
|---|---|---|---|---|---|
| [#4](https://github.com/Saikrishna020/pr-review-bot/pull/4) | SCRUM-10 | none — fully correct | satisfies | satisfies | ✅ |
| [#5](https://github.com/Saikrishna020/pr-review-bot/pull/5) | SCRUM-11 | cache key strips trailing digits (project prefix only), so different ticket keys in the same project collide and can return each other's cached result | satisfies | partial | ❌ |
| [#6](https://github.com/Saikrishna020/pr-review-bot/pull/6) | SCRUM-12 | implements core_remaining_pct only; omits search_remaining_pct, the one the ticket calls out as mattering most | partial | partial | ✅ |
| [#7](https://github.com/Saikrishna020/pr-review-bot/pull/7) | SCRUM-13 | warning logic is correct, but a hardcoded fallback for WEBHOOK_SECRET was added alongside it, committed to a public repo — a real security issue outside the ticket's literal ask | satisfies | partial | ❌ |
| [#8](https://github.com/Saikrishna020/pr-review-bot/pull/8) | SCRUM-14 | diff is entirely unrelated to the ticket (wrong branch pushed) — changes a local demo script's print limit, not the README | does_not_satisfy | does_not_satisfy | ✅ |

**3/5 by strict predetermined-verdict match.** The `expected_verdict` values above were locked in before any diff was written (same discipline as the golden sets) and were NOT revised after seeing the bot's output — the two mismatches are being reported honestly rather than adjusted to make the score look better.

Both mismatches are pre-registration miscalibration on my part, not bot misses — in both cases the bot caught the seeded bug and adjusted the verdict *more strictly* than my prediction:

- **SCRUM-11**: I expected `satisfies` because the caching feature itself was implemented. The bot said `partial` because it caught the actual seeded bug — the cache key collision — which directly violates the ticket's explicit "different keys must never collide" requirement. The bot's answer is arguably more correct than my prediction.
- **SCRUM-13**: I expected `satisfies` because the warning log itself was implemented correctly. The bot said `partial` because it caught the hardcoded fallback secret as a real behavior change the ticket didn't ask for (ticket text says current behavior is "skip verification when unset," and the fallback silently changes that). Again, the bot's stricter read is defensible.

Net result: **the bot caught all 5 seeded conditions** (1 clean, 2 code bugs with correct severity, 1 requirement gap, 1 ticket mismatch) — the raw match-rate undercounts this because two of my own ground-truth labels didn't anticipate how the bot would (correctly) weigh a code-quality finding against the Jira verdict.
