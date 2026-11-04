# Inspecting the known Work recipe

`tare workflow analyze` checks and prints the current recipe without creating a
Work, calling a model, running checks or changing the graph. Use `--format json`
for its compiled data, or `--plan recipe.json` to analyze explicit recipe data.
This command has no execute option. The TUI's Work inspection and `tare explain
WORK_ID` show the same recipe alongside the selected Work's readiness and checks.

The only recipe is `ground → implement → validate → audit → settle`. These are
logical phase groups of the existing `work_run` path: grounding, descriptor and
plan audit; scoped implementation; candidate verification; policy-controlled
audit; admission, landing and reconciliation. They are not independently
dispatchable new tasks. In particular, `validate` describes the verification
already performed while materializing a candidate, not a second verifier run.

The compiler accepts exactly one occurrence of each known versioned operator.
It checks fields, dependencies, phase order, upstream inputs, outputs, supported
capabilities, declared effects, obligations and settlement requirements. An
unknown operation, missing obligation or invalid dependency fails with a named
`WORKFLOW_*` error. JSON schema shape alone does not establish legality. Node
identities are preserved; there is no title-based merging or optimizer.

`tare.tools/work-recipe/1` is an explicit subplan contract, separate from the
frozen FLOW-10 `tare.tools/workflow-ir/1` Work DAG. Compilation produces
`tare.tools/compiled-work-recipe/1`, adapting the five phases to **one** existing
`work.execute/v1` operation. The outer DAG and historical saved sequences keep
their existing interpretation. The compiler introduces no execution store,
registry, dynamic imports, generated Python, evaluation or planner language.

On a new `tare run WORK_ID`, the owner compiles the fixed recipe before creating
run custody, a train or a provider session. The existing run receipt freezes the
compiled data and its canonical SHA-256. Execution still enters the same static
Work handler exactly once. Resuming a historical execution does not compile a
replacement recipe or replace its saved descriptor, context, cache, limits or
recovery contract.

Supported capabilities and effects describe what that static owner can do.
They do not prove machine availability or grant permission. The same descriptor,
confinement, verifier, audit, admission and landing owners still enforce the
real execution boundary. An analyzable recipe can therefore fail readiness,
verification or admission. Compilation never establishes DONE or approval.

This is the bounded FLOW-09 realization. It does not implement arbitrary flows,
physical execution classes, parallel phase scheduling, automatic planning or a
universal workflow language. Generalize only when another real flow needs it.

Contract tests live in `tests/test_workflow_compiler.py`; execution and public
facade checks live in `tests/test_workflow_integration.py`. Qualification records
must name the actual platform and result, including any fail-fast corrections.

Recorded qualification: [FLOW-09, 2026-10-17](FLOW09_QUALIFICATION_2026-10-17.md).
