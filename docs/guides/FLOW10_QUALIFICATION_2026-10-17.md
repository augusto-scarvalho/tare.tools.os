# FLOW-10 bounded Work DAG qualification — 2026-10-17

The v1 implementation represents prepared Works as a versioned dependency DAG
and dispatches one eligible Work at a time through the existing Work owner.
An unrelated blocked Work no longer hides an independent ready Work. Existing
sequences retain their saved serial order. This does not implement a general
planner, arbitrary operators or agentic map-reduce.

## Local evidence

Windows qualification records 151 distinct passing cases after fail-fast replay:

| Test module | Cases | Evidence |
| --- | ---: | --- |
| `test_workflow_reducer.py` | 71 | Pure IR validation and deterministic readiness decisions |
| `test_workflow_dispatch.py` | 33 | Existing Work/Git/settlement integration, recovery and negative cases |
| `test_sequence_revision.py`, `test_sequence_membership.py`, `test_workflow_tui.py` | 44 | Compatibility, reviewed revisions and literal TUI reports |
| `test_disposition_manifest_compliance.py` | 3 | Staged source fingerprints, ownership inventory and frozen-runtime barrier |

The integration cases include all 18 combinations of six FAULT cuts and three
resume schedules described in [WORKFLOW_DISPATCH](WORKFLOW_DISPATCH.md).
They use a scripted provider with real Work stores, Git effects, coordinator
bindings, journals and settlement. This is orchestration evidence; it measures
neither model quality nor live-provider behavior. Linux coverage must be read
from its own CI run, independently of these Windows results.

The earlier sequence compatibility run also passed 80 cases. The final targeted
rerun above covers the revision and membership paths changed subsequently.
Failed fault-injection setup and a wording assertion were corrected and replayed;
the archived initial failures remain visible rather than being overwritten.

## TUI dogfood

The public terminal was driven through keys and mouse, including model and
permission selection, implementation, Review and Resume. A bounded GPT-6.1 Sol
native Responses conversation produced the pure modules and their tests. The
host integrated the sequence owner and independently checked its behavior.

The final public Resume used a blocked two-Work fixture: one independent Work was
already settled and the other required an unfinished external prerequisite.
Resume returned `BLOCKED`, preserved both items and all native journals, created
no Work receipt and made no model call. The report displayed the exact Windows
path, separate Work rows and the unresolved prerequisite. This replay exposed
Markdown altering paths and row boundaries; structured sequence reports now use
literal text, with a regression test.

The implementation conversations made 21 model calls: four in the initial
read-only setup and 17 in the scoped implementation conversation. The latter's
provider usage reported zero cached tokens; the preserved session history and
stable cache key are not evidence of a cache hit or a monetary saving.

## Evidence custody and completion boundary

The raw evidence of this run is not published (local JUnit reports, compact TUI
frames, controller cleanup receipts and `public-tui-result.json`). The versioned tests are the
maintained destination for the failure cases; no new collector or recurring
model session is installed. All three dogfood terminal processes were closed
with cleanup verified. The managed product release was not modified.

Completion is bounded to the `work.execute/v1` operator and existing custody,
recovery and settlement owners. Unknown effects require reconciliation and
never authorize a fresh execution. Recovery adds an attempt to the same plan;
only the existing reviewed revision path changes the DAG. Full compiler,
arbitrary operation admission and parallel dispatch are separate contracts.


## Dated reconciliation — 2026-10-22

The original independent platform reports are now verified from exact PR131
JUnit artifacts: all six FAULT points × three schedules passed separately on
Windows (18) and Linux (18), for candidate
`9410309c3586325a3e64024a64eebdf094751c0a`. This historical two-platform result
is not 36 new/current trials and does not revise the earlier numbers above.
See the [compact observation](FLOW23_NATIVE_REVIEW_2026-10-22.md) and
[conditional FLOW-23 decision](FLOW23_BACKEND_DECISION.md).

On main `75ed084055dd7e72bb71765f9441b14c198a61f1`, the Windows 18-case attempt
was interrupted at the outer 300s driver limit after 17 unlabeled passing markers,
with no recorded assertion failure. It remains preserved and incomplete, not a
named matrix. After private driver budget/verbose-reporting correction, only the
interrupted `cancel-late-result/contended-resume` cut was replayed (1 pass).
Another 47 named negative/recovery tests passed: 48 named native checks total,
excluding those 17 markers. One synthetic fixture preparation passed separately.
These limited Windows checks are not updated general compiler qualification.

The installed public TUI's keyed Review/Resume returned `BLOCKED` for an external
dependency while preserving the other `SETTLED` Work, plan revision, real owner
receipts, journals and recorded use, with zero new provider calls. The fixture's
five scripted offline invocations were not paid usage. The first pilot only typed
a draft/raw escape text, created no `NativeSession` and observed no model call.
A minimal Windows reader ordering fix consumes queued input before parser idle
timeout; a real-parser/fake-console regression failed before the fix and all six
Windows input tests pass after it. The source-console replay with that patch
persisted the complete request unchanged and finished in five model calls under
an eight-call ceiling, using `gpt-6.1-sol`.
This bounded input fix and the pilot dispatch-ack/outer-test-budget corrections
are UI/qualification-driver gaps, not reasons for an external durable backend.
No native invariant requiring one was demonstrated; `PRESERVE_NATIVE`, external
prototype `NOT_TRIGGERED`, remains conditional, not proof of no possible gaps.
