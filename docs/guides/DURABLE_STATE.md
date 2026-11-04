# Durable state: current scope in Tare

## Decision and reach

Dogfooding showed a real presentation/resume failure: the TUI removed
a backslash from the custody path and from the Continue (PowerShell) command. Journals and
receipts are intact. The minimal fix is to preserve the owner's structured reports
literally on completion, Review and reopening/recovery, reusing
the literal path Sequence already uses; the assistant's narrative text keeps
Markdown. There is no evidence here that justifies a transactional outbox or another
backend: keep the existing owners, journals and receipts, with no new architecture.
This does not prove the absence of failures at other cut points or in other environments.

The scope is to record intent, execute through the owner, record the result and
rebuild the TUI response. This change fixes the OS presentation and
reconciles the research item. It preserves sessions, model, budgets and cache
identity. It introduces no database, queue, engine, collector, roles or dependencies.
It does not promise universal crash/power-loss tolerance or universal exactly-once.

## FLOW-23 — conditional reconciliation (2026-10-22)

The [maintained decision](FLOW23_BACKEND_DECISION.md) records `PRESERVE_NATIVE` and
an external prototype `NOT_TRIGGERED`, with owners, a minimal reopening trigger and a total
cost that has not yet been measured for external alternatives. The
[maintainer's compact observation](FLOW23_NATIVE_REVIEW_2026-10-22.md)
preserves hashes and limits: historical PR131, 18 cases per platform; on main
`75ed084...`, 48 named native checks on Windows (1 replay + 47 negative/
recovery), plus a separate synthetic preparation. The batch interrupted by the
external 300 s limit has 17 unnamed markers and remains incomplete; it is not
part of the 48 and does not prove a complete current matrix.

Review/Resume in the public TUI returned `BLOCKED` because of an external dependency,
preserving the `SETTLED` Work, review, receipts, journals and usage, with no new calls.
The five fixture invocations were offline, not paid usage. The first pilot did not
create a `NativeSession` or observe a model call. The bounded fix to the
read order of Windows input (six tests passing), the pilot's dispatch ack
and the driver's budget/reporting are UI/qualification gaps, not evidence
of a missing backend. The console replay with the fix preserved the whole
demand and finished in five `gpt-6.1-sol` calls, within the ceiling of eight.
No native invariant was shown that would require an
external backend; this does not prove the absence of other gaps. `UNKNOWN` still
requires reconciliation by the owner, never an automatic retry.

## Owners and sources of truth

| Boundary | Owner and source of truth | What cannot be inferred |
| --- | --- | --- |
| Request and presentation | OS: saved demand, selection, events and request identity; `native_console.py:1753–1790` writes `execution_requested` before `run_command`. | Saved intent does not prove dispatch or a completed effect. The TUI snapshot does not replace the owner's receipt. |
| Session and tactical loop | The OS composes `NativeSession`; the Runtime owns the loop, tactical events and consumed limits. The OS binds `NativeJournal` and observations to the session in `native_agent.py:513–544,718–786`. | The composition call in the OS does not by itself demonstrate the Runtime's internal persistence implementation. |
| Effect and physical proof | Kernel: custody, journal and effect receipts used by the adapters; the OS asks for `tools.recover_effect(journal)` and the Runtime receives `session.reconcile_effect` on Resume (`native_agent.py:727–732`). | A caller receipt does not make an external operation atomically transactional. The properties of the Kernel and the Runtime depend on those owners, not on a new OS journal. |
| Session completion | OS: `outcome.json` followed by the invocation state in `native-agent.json` (`native_agent.py:877–906`); recovery requires a compatible identity, session, end and status. | The mere existence of `outcome.json`, releasing the writer or having an old result does not prove that this request completed. |
| Confirmed flows and mutations | OS: `flow_invocation.record/observe` and `console_mutation.record/observe`, with external custody, reserved identity, a `RUNNING` state and a `FINISHED` receipt. The called owner remains responsible for effects, cancellation and resumption. | These envelopes are not a queue of pending deliveries nor an authorization to repeat the action. |
| Work and DAG | OS: the Work owner keeps execution/settlement and Git/backlog reconciliation; `flow_recovery.observe` reads its receipt by identity. `workflow_reducer.reduce_workflow` only chooses; it does not execute. | `RECOVER` is not a new dispatch; a historical answer is not a new approval, the settlement of another Work or the completion of the whole demand. |
| TUI reconstruction | OS: `console_recovery.observe` queries receipts; `demand_console.reconcile_demand` validates demand/revision/selection and saves a reconciliation event. | Observing does not dispatch, repair an effect or renew an approval; a failure to save the observation restores the previous presentation state. |

The source of truth is specific to each boundary, not a single global status.
The hashes returned in the observation identify the receipts read; they are not equivalent
to a distributed transaction or to a new validation of the outside world.

## Why not add an outbox/backend now

The current criteria that the inspected design already meets are:

- **Identity before action:** the envelopes reserve an exclusive directory and
  write `RUNNING` before calling the owner. An interrupted attempt does not make
  the identity reusable (`flow_invocation.py:20–43`,
  `console_mutation.py:35–49`). There is no promise of automatic redelivery.
- **Confirmed result separate from intent:** only `FINISHED`, compatible bindings
  and a structured result allow recovery. Mutations also
  check the canonical hash of the result. An active writer produces `BUSY`; missing,
  incomplete or incompatible evidence stays `UNKNOWN`.
- **A lost ack does not require repeating the effect:** when there is a qualified receipt,
  `console_recovery.py:24–89` rebuilds the exact result of the request. In an interrupted
  session, it reports the saved results and points to an explicit Resume; it does not
  automatically turn a pending effect into a completion.
- **Resumption belongs to the owner:** the OS composes Kernel proof and Runtime reconciliation;
  Work keeps its candidate/landing/backlog reconciliation. Preserving
  identity, consumed calls and the deadline is a condition of resumption, not an occasion
  to switch models, restart the budget or invalidate the cache.
- **Uncertainty blocks undue progress:** `workflow_reducer.py:35–107` gives
  priority to existing recovery and lets `UNKNOWN` veto globally both
  recovery and a new dispatch. It does not promote a missing receipt to success.
- **Reopening is not executing:** `demand_console.py:277–334` avoids applying an observation
  to a demand/revision/selection that changed, deduplicates observations and removes approval
  revisions. The reopening test keeps the draft without calling the backend.

An outbox would need a current delivery/redelivery obligation and an idempotent
contract on the recipient's side. This review found no unmet
obligation that requires that mechanism. Duplicating the receipts in the OS does not remove the
window between an external effect and its confirmation by its owner.

## Evidence, limits and reopening

**Failure shown in dogfooding:** a real custody path of the form
`C:\<folder>\_archives\<run>\history\agent-sessions\<session-id>`
was displayed as
`C:\<folder>_archives\<run>\history\agent-sessions\<session-id>`.
The Continue (PowerShell) command suffered the same loss, which made the resume
guidance wrong, with no change to the journal or receipt files.

The regression `tests/test_console_owner_reports.py::test_completed_direct_session_keeps_exact_custody_path_and_resume_command`,
run by the maintainer, reached `PROPOSED` through `WorkConsole.execute` with a fixture
backend and failed on the comparison of the displayed path. The cause is the use of the
Markdown path of `WorkConsole.say`: the sequence `\_` is interpreted as an escape.
`plain` removes terminal controls, not that backslash; the owner's renderer provides
the path and the command, but the later Markdown interpretation corrupts them.
The fix in `native_console.py` and `demand_console.py` passes `literal=True` for
the structured reports, which also preserves identifiers and literal markup.
It does not change custody, dispatch, session, model, budget or cache.

The known persistence cut points stay separate from this UI failure;
their conservative result is not an additional defect:

- `tests/test_mutation_recovery.py:160–177`,
  `test_interrupted_mutation_never_becomes_completion_or_replays`: injects an error
  before the owner, after the effect or while writing the final receipt. It requires `UNKNOWN`,
  refusal to reuse the identity and at most one application.
- `tests/test_batch_recovery.py:82–95`,
  `test_lost_final_receipt_remains_unknown_even_when_owner_saved_cancellation`:
  the owner saves a cancellation, but the envelope loses the final write; the observer
  does not invent a completion or rerun the flow.

**Deliberate limit:** an owner effect or end without a qualified final receipt
can leave the TUI response unknown. The current contract prefers inspection and
reconciliation by the owner over repeating an uncertain action. This is not automatic
eventual delivery. A recovered result is historical; current readiness and approval
stay separate. Local cleanup does not prove a remote end or billed usage.

**Hypothesis not tested in this conversation:** a combination not yet covered could
violate the persistence invariants. The concrete presentation failure above
does not demonstrate such a violation; no second architecture track is opened here. Searches of the Kernel/Runtime satellite directories
returned no inspectable Python files: their internal guarantees were not
audited in this review; the attribution above relies on the OS interfaces, the integration
tests read and the contract documented in FLOW-04.

**Objective condition for reopening:** obtain a controlled offline reproduction in
which observing/reopening repeats an effect, accepts a receipt from another request, declares
completion without proof, or in which Resume changes the saved identity/limits. Before
editing the runtime, keep the exact cut point (before/after intent, effect, receipt
or TUI checkpoint), the request and bindings, the journal, receipts and expected versus
observed counters, in a temporary fixture with no provider/network/operator storage.
Acceptance invariant: observation does not dispatch; an incompatible receipt stays
`UNKNOWN`; a valid confirmation recovers the same result; resumption uses the owner and
the original limits without repeating an uncertain effect. A test that fails before and
passes after a minimal fix, in the responsible owner, is the requirement for changing
code. Only discuss an outbox if that evidence shows a need for persistent
delivery that the current owners do not meet, with explicit recipient semantics.

## Sources and reproducible checks

Local Windows qualification of 2026-10-21, run by the maintainer:

- 42 existing recovery checks: 41 in `test_console_recovery`,
  `test_mutation_recovery`, `test_flow_recovery` and `test_native_effect_recovery`,
  plus the lost-final-receipt cut point in `test_batch_recovery`.
- Four regressions in `test_console_owner_reports.py`: completion with the exact path and
  command, a real fixture receipt recovered without dispatch, Review and Inspect
  with literal identifiers and markup characters. Completion, recovery and
  Inspect failed on the previous code and passed after the fix.
- Another 22 regression results in `test_console_results`,
  `test_console_recovery`, `test_workflow_tui` and the defaults-adoption scenario
  of `test_console_permissions`. They overlap with the first list;
  do not add the numbers up as distinct tests. Narrative Markdown remains
  covered separately from the literal reports.
- The first Linux CI run exposed an old test observer that replaced
  `say` with `list.append`, incompatible with the existing literal option. The
  failure was reproduced locally; the research test now reads the real
  selectable output, and the activity observer forwards the option to the renderer.
  The 14 checks of `test_research_flow` and `test_console_flow_activity` passed.
  With the manifest checks, this delivery has 74 distinct local tests.

In the public TUI, driven by keyboard/mouse in ConPTY, Resume of the most
recent delivery kept **6 calls before and after**, with no new generation. The custody and the
resume command appeared with the correct backslashes. The saved identity, deployment,
configuration, limits and deadline stayed the same; the history only gained
the observation/resume records. A visual line wrap
was not mistaken for removed characters. This proof covers the public completion and
resume; Inspect also has its own specific offline regression.

The earlier documentation session, with 9 calls, refused Resume because the guide had
changed after its checkpoint (`native_resume_source_changed`); it made zero generations.
The refusal was preserved, without restoring old files or bypassing the guard.
The two deliveries and one blocked edit attempt consumed 9 + 6 + 5 = 20
model intents. The five from the blocked attempt count toward the fix's limit of 12;
the new configuration used six of the remaining seven and kept the earlier
deadline. No session was rewritten to widen its permissions. The local
automation now replaces the whole multiline field with F7 and checks the saved and
effective scopes before proceeding. These costs are not a quality
cohort, nor proof of a cache hit or monetary savings.

To reproduce the focused checks, with no real provider:

```sh
python -m pytest -q tests/test_console_owner_reports.py
python -m pytest -q tests/test_console_recovery.py tests/test_mutation_recovery.py tests/test_flow_recovery.py tests/test_native_effect_recovery.py
python -m pytest -q tests/test_batch_recovery.py::test_lost_final_receipt_remains_unknown_even_when_owner_saved_cancellation
```

See the Windows/Linux checks of this change for each platform's evidence.
The real TUI run above was on Windows; its result on Linux is not inferred
from it. The portable tests use no network or operator data.

The maintained destination is this guide and the versioned regressions. The raw
evidence of this run (original failures, JUnit, counters, hashes, keys/VT and the
cleanup confirmation) is not published. There is no collector or recurring paid campaign; the worktrees
that belong to this delivery are parked after green CI.

[FLOW04_RECOVERY](FLOW04_RECOVERY.md) and
[FLOW10_QUALIFICATION_2026-10-17](FLOW10_QUALIFICATION_2026-10-17.md) still own
the failure matrices already delivered. This rescoped research fixes the demonstrated
presentation/resume boundary and sets reopening on a reproducible failure.
