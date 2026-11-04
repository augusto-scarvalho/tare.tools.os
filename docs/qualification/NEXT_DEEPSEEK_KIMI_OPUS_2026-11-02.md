# One ordinary delivery: product-export checks run early

Backlog: `SPECGRAPH-COMPLETION-X5`. Task: `task-381229058897`. Base: `b6d7585f`.
This is one bounded delivery, not a model benchmark or completion of the X5 cohort.

## Delivered behavior

The declared-package-data/Git-export contract moves into
`tests/test_native_release_package_data.py`, which runs in the existing CI quick
phase. Its negative case names an omitted tracked resource. The other release
tests remain in regression. Selector tests cover the quick/full/focused partition;
the five-minute quick limit and mandatory full regression remain unchanged.

The public TUI was driven by terminal keyboard input, including its one-time scoped
write approval and reopening the saved conversation. The separate planner's saved
artifact has SHA-256 `f32ff80de4b21d6d4d8e38658a611b7b9551eb566f8013ed3383b371150eccba`.

| Role | Observed route / reasoning | Model calls | Provider-reported API USD |
| --- | --- | ---: | ---: |
| Coordinator | Strata Swift/Qwen Next Flash / low | 14 | Unknown: local energy |
| Planner | Kimi K3.1 Pro, OpenRouter / high | 5 | 0.1885024 |
| Implementer | DeepSeek V4.1 Flash 1017, OpenRouter / high | 51 | 0.034367774 |
| Auditor | Opus 5.5, Claude CLI / high | 6 + 4 | Unknown: subscription quota |

DeepSeek was also pinned for exploration/research; no separate call to either role
was needed. The recorded API subtotal is USD **0.222870174**, including the failed
local test attempt. It excludes auxiliary JeV, local electricity and CLI quota;
those costs are unknown. Provider usage reports 2,404,352 cached input tokens out
of 2,621,671 for DeepSeek, and 92,672 out of 135,936 for Kimi. These are observed
cache reads, not a promised hit rate or a cost/quality comparison.

## Root cause encountered and recovery

The implementer corrected one failing selector assertion and passed the scoped
checks (81 passed, one optional skip). The first independent review was inconclusive
because the host's verification projection retained only first/last checks and
failures, hiding successful intermediate runs whose real timings were documented.
The original hash-chained journal still contained those results.

The host now preserves all observations for ordinary runs, with an explicit
64-observation projection ceiling and visible omission counts for longer histories.
It distinguishes historical measurements from later before/after rechecks. A
blocked unchanged retry explicitly reports zero implementer/auditor dispatches.
Five regression cases reproduced these defects before the fix; the corrected
focused audit/recovery suite passed **147 tests**, with scratch removed.

After reopening the same conversation on the corrected development host, its review
packet contained all four original checks, including both previously hidden timing
pairs. No new planner or implementer session was started. The configured Opus CLI
reviewed the remaining evidence and returned **ACCEPT**; the host applied the
candidate as **VERIFIED**, without changing its code or historical measurements.
The original review and failed checks remain historical evidence.

CI, merge and installed-product rollout are later gates. Full X5 qualification,
cross-platform campaign evidence and total comparative savings remain open.
