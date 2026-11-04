# FLOW-23 conditional backend decision — 2026-10-22

**PRESERVE_NATIVE · external prototype NOT_TRIGGERED · backend adoption NOT_PROPOSED.**
Do not deploy an external durable backend in anticipation of a need. No native
recovery invariant requiring one was demonstrated by these bounded checks. This
completes the bounded decision, not external-backend delivery or proof of no gaps.
The maintainer reconciles the conditional item only after review and green CI.

## Owner seams

Scope: prepared Work DAG, `work.execute/v1`, serial incorporation and existing
owner recovery, not a general workflow compiler. The current OS owner remains the
integration point: `work_sequence` reserves dispatch identity and plan bindings;
`workflow_reducer` selects without executing; the Work lifecycle owns execution,
Git/backlog reconciliation and settlement. Runtime owns the tactical session;
Kernel owns effect custody and receipts. OS `native_agent` composes their recovery;
`console_recovery` observes identity-bound receipts and reconstructs presentation.
See [DURABLE_STATE](DURABLE_STATE.md) and [WORKFLOW_DISPATCH](WORKFLOW_DISPATCH.md).

`UNKNOWN` effects or mismatched receipts require owner reconciliation, never an
automatic retry or fresh execution. A UI wait/block, absent evidence, shell/API/Git
uncertainty or wrapper timeout is not by itself a missing durable backend. An
outbox needs an actual unmet delivery obligation and recipient idempotency contract;
duplicating OS records does not close the external-effect/receipt window.

## Measured evidence and limits

The [compact observation](FLOW23_NATIVE_REVIEW_2026-10-22.md) binds source hashes,
JUnit digests and limits, separating historical reports from checks run for this
review.

- Exact PR131 JUnit artifacts from [run 39089721808](https://github.com/augusto-scarvalho/tare.tools.os/actions/runs/39089721808)
  verify all six FAULT points × three schedules separately on Windows (18) and
  Linux (18). They qualify historical candidate
  `9410309c3586325a3e64024a64eebdf094751c0a` (merge `ba7bfe7e86249f58f5b2f9a66bbfc8f872b524da`),
  not 36 new/current trials.
- On current main `75ed084055dd7e72bb71765f9441b14c198a61f1`, the local Windows
  18-case attempt hit the outer 300s driver limit after 17 unlabeled passing
  markers, with no recorded assertion failure. That preserved run is incomplete:
  no final named per-case report, no complete current matrix. The private driver
  gained verbose reporting and a 1800s phase budget, retaining per-test timeouts.
  Only the interrupted `cancel-late-result/contended-resume` cut was replayed
  (1 pass). Another 47 named negative/recovery tests passed: **48 named native
  checks**, excluding the 17 markers. One synthetic fixture preparation passed
  separately; it is not a 49th native check.
- The real installed public TUI was driven via keys. Review and Resume of that
  isolated native sequence returned `BLOCKED` for an external dependency and
  preserved the other `SETTLED` Work, plan revision, real owner receipts, journals
  and recorded use. There were zero new provider calls. The fixture's five
  scripted offline provider invocations were not paid usage.
- The first pilot only typed a draft and raw escape-key text: no `NativeSession`
  was created and no model call was observed. The Windows input monitor applied
  parser idle timeout before consuming an already queued record batch, breaking
  split paste/mouse/modified-key escapes under scheduling delay. A real-parser,
  fake-console regression failed on the original; all six Windows input tests
  passed after the minimal OS reader ordering fix. The source-console replay
  persisted the complete request unchanged and finished in five model calls
  within the ceiling of eight, using `gpt-6.1-sol`. The helper now waits for the
  saved native invocation before acknowledging dispatch. The final input, paste,
  navigation, native-console and manifest suite passed 40 checks, including those
  six input tests. This input correction, the pilot's dispatch-ack
  correction and the outer-test-budget/reporting correction are UI/qualification-
  driver gaps, not evidence for an external durable backend.

Current checks are limited Windows observations, not renewed two-platform or
updated general compiler qualification. No universal no-gap or exactly-once claim
follows. External alternatives are **unmeasured**: neither free nor inferior by
assertion. No backend equivalence, price comparison or SDK-independent export was
executed. The earlier interrupted run remains explicitly incomplete.

## Frugal reopening trigger and cost boundary

Reopen only with **one reproducible native recovery failure or demonstrably unmet
delivery requirement**. Bind a compact receipt to the exact source,
identity, expected/observed outcome and replay steps; retain the failing trace and
replay it through the responsible owner. First assess the smallest owner fix. If
an unmet requirement remains, compare that fix against **one disposable external
candidate**, only then, on the same trace and explicit acceptance conditions.
Missing evidence alone calls for evidence/reconciliation, not a prototype.

Compare total cost without invented dollar figures: installation and packaging;
operations, storage, authentication, queue management and availability; migration
of history and identities; retry/cancel parity (including late results and unknown
effects); and SDK-independent history export, removal and exit. Record unknowns
rather than zeros, including native maintenance cost. A disposable comparison
must demonstrate export/removal and leave no permanent dependency if rejected.

Keep the native implementation and the compact decision in Git. There is no
external prototype to maintain. Revisit this choice when a concrete unmet need
appears, using the same failing case to compare the smallest native fix with one
external option.
