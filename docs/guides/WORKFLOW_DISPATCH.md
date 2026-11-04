# Ready Work dispatch and recovery

Choose **Settings → Optional flow → SEQUENCE · selected Works**. Select prepared
Work IDs from the control graph and review them before starting. The selection
is compiled into a bounded, versioned Work DAG. Dependencies determine eligibility;
the displayed order breaks ties between independent ready Works. Incorporation
remains serial. If the first Work is blocked by an external prerequisite, another
independent ready Work can progress.

Each node uses the single contracted operation `work.execute/v1`. That operation
calls the existing Work lifecycle, including plan review, implementation,
verification, audit and settlement. The IR contains no model, provider, executable
command or generated code. It does not admit arbitrary new operations. The schema
is [workflow-ir.schema.json](../../contracts/v1/workflow-ir.schema.json).

The logical plan and its digest live in the existing `sequence.json`; there is no
new database, scheduler service or journal. The current frontier replaces its prior
observation. Existing Work receipts and journals retain execution evidence. The v1
limit is 128 selected nodes under the existing 4 MiB sequence-file budget.

## When execution stops

- **Completed:** all selected Works have successful, reconciled completion.
  Failed, cancelled, unknown and waiting executions never satisfy dependencies.
- **Paused:** a Work needs a response or operational intervention. Open the active
  Work, respond, then Resume the sequence. No model session polls while waiting.
- **Blocked:** no selected node can progress. The report names the unsatisfied
  prerequisite or readiness requirement. Resolve it, then Resume explicitly.
- **Needs review:** the plan, Work identity or completion proof changed, a result
  belongs to another dispatch/project/graph/train, or a prior effect is unknown.
  Inspect the active Work's owner receipts before retrying. Resume cannot turn an
  unknown external effect into a fresh execution.

Every dispatch saves its unique run ID and a fence binding the plan digest, Work
identity and train before calling the Work owner. The returned receipt must match
the dispatch and its project/graph. After a crash, a saved native Work origin goes
through Work's existing recovery and authority checks. A reservation whose unique
run directory was never created is known not to have entered the Work owner;
an existing directory without a recovery origin is not that proof.

Resume keeps completed Work results and model-session history. A recovery attempt
does not create a plan revision. Reviewed changes to the remaining selection do;
the existing preview digest, Work locks and revision history continue to apply.
Legacy sequences gain a logical plan only after their frozen identities,
dependencies and completed-node hashes are checked. Their saved serial order is
retained, including when an earlier item is blocked; independent ready-node
dispatch applies to new sequences. Completed items are not rerun.

## Qualification

`tests/test_workflow_reducer.py` verifies the pure contract and dependency
decisions. `tests/test_workflow_dispatch.py` exercises real Git, Work execution,
coordinator, native journals and settlement with a scripted provider. These are
orchestration tests, not model-quality or live-provider benchmarks.

The FAULT matrix has six cut points: reservation before dispatch; edit effect
before its receipt; Work receipt before the sequence transition; Git landing before
backlog acknowledgement; expired binding and rebind; cancellation with a late
result. Each runs with direct resume, cancellation before resume and contended
resume. Exact results and platform coverage belong to test reports; a pass on one
platform is not evidence of the other. Unknown effects and mismatched result
identities have separate negative cases.

The TUI dogfood implementation used a bounded GPT-6.1 Sol native conversation.
Small local terminal transcripts and receipts live in the dated qualification
archive; the versioned tests are the maintained destination for the failure cases.
See the [2026-10-17 qualification record](FLOW10_QUALIFICATION_2026-10-17.md).

## Finite budgets and measured composition

Explicit per-Work plan/implementation call/time limits are persisted and reused;
older releases refuse the new constraint format. The paired source-TUI pilot
measured 23 calls in each arm and one fewer explicit Start action in the sequence.
See [the comparison, limits and replayable evidence](WORK_COMPOSITION_COMPARISON.md).
