# SPEC-OS-WORKFLOW-IR-COMPILER-001: Logical workflow IR, compiler, physicalization and durable reducer

- **Status:** PROPOSED (awaiting an ADR via Round Table; first requires the ownership reconciliation in section 1)
- **Governing ADR:** pending; based on ADR-045 and ADR-046 (both retired) and [ADR-062](../adr/ADR-062_UNIVERSAL_FRUGAL_ENGINEERING_DOCTRINE_AND_SOVEREIGN_BYOC.md)
- **Target Repository:** `tare.tools.os` (Workflow semantics); `tare.tools.backlog-graph` (per-call DAG algorithms)
- **Version:** 0.1.0
- **Derived from:** FLOW-09 to FLOW-17

---

## 1. Context & Objective

A compiler only exists after the static recipe `ground → implement →
validate → audit → settle` works through the facade (SPEC-OS-WORK-CONSOLE-001).
This SPEC generalizes that useful path. Strong rule: **the planner never produces
anything executable.** `LLM proposal → canonical normalization → deterministic
compilation → physicalization → execution`.

Ownership reconciliation, the first decision of this SPEC: the harness
disposition manifest marked `workflow_plan.py`, `workflow_lifecycle.py` and
`workflow_reduce.py` as `EVOLVE_SATELLITE` toward the Backlog. Here, the Backlog
provides DAG, frontier and validation algorithms per call; the OS owns
`WorkflowInstance`, `PlanRevision`, lifecycle and settlement. Semantics kept,
legacy runtime not restored. `workflow_run.py`, `async_runtime.py` and
`async_state.py` remain `DISCARD_FROZEN`.

Precedents as ideas only: HTN for decomposition, MLIR for IR levels and
legalization, Cascades for logical→physical, Workflow Nets for soundness, build
systems for incremental invalidation. None becomes a dependency or an owner.

---

## 2. Interface Contract & Data Structures

Logical node (no vendor, model or execution class):

```yaml
id: repair
intent: satisfy_requested_change      # known, versioned operation, never free text
dependsOn: [understand_failure]
requires: [failure_hypothesis]
produces: [candidate_change]
effects: [repository.modify]
settlement: {candidateProduced: true}
```

Physical step, produced by physicalization:

```yaml
step:
  id: repair
  executionClass: BUILTIN | CAPABILITY | AGENTIC | HUMAN | SUBFLOW
  requiredCapabilities: [filesystem.read, filesystem.write, shell, git]
  physicalProperties: {isolation: ephemeral-workspace}
```

Compiler pipeline, each stage failing closed with a named error:
`parse/normalize → type check → dependency check → capability check →
authority requirement check → input/output check → settlement check →
simplify → physicalize → CompiledWorkflow`.

Physicalization by the rule of least sufficient agency: a deterministic builtin
mechanism if it satisfies exactly; otherwise a capability; otherwise an agentic step;
irreversible ratification is human. The exact model is chosen just-in-time
when the node becomes READY, via SPEC-OS-ROUTING-BASELINE-001 (retired).

Lifecycle: `PlanRevision N` is a DAG. A retry is a new attempt of the same node.
A failure or a new fact produces `PlanRevision N+1` with the affected subgraph, preserving
settled nodes. The executor never rewrites the Workflow.

---

## 3. Verifiable Acceptance Criteria

* **`AC-01` Current recipe representable (FLOW-09):** the flow `ground → implement → validate → audit → settle` is expressed in the IR with no vendor-specific escape and compiles to the same path that SPEC-OS-WORK-CONSOLE-001 executes.
* **`AC-02` Known operations only:** an `intent` outside the versioned catalog is rejected; natural language enters only as node context, never as code.
* **`AC-03` Verified legality (OFFLINE):** 12 representative valid and invalid plans plus small seed-generated graphs; cycles, unproducible inputs, unrealizable capability, undeclared effect and missing settlement produce named errors; no transformation changes obligations, effects or observable order (differential test).
* **`AC-04` No presumed semantic equivalence:** the compiler does not merge or eliminate investigations by title similarity; simplification only over structure proven equivalent.
* **`AC-05` Durable reducer (FLOW-10):** ready nodes progress serially with identity and attempt recorded; after a crash, valid work is preserved and no descriptor is released improperly; without importing the legacy runtime.
* **`AC-06` Limited interpreter and planner (FLOW-11):** grounding and templates first; planner only on the semantic residual; activation per task class after AB8 with a gain in human minutes or cost to an audited result; negative examples preserved.
* **`AC-07` Localized replan (FLOW-12):** `PlanRevision N+1` changes only the affected subgraph; reused settled work is counted; conservative invalidation when the analysis does not prove safety.
* **`AC-08` Limited parallelism (FLOW-13):** two independent executions, starting with reads and tests; effect classes and resources limit concurrency; a detected conflict returns to serial in that class.
* **`AC-09` Small trains (FLOW-14):** two or three compatible Work in the same run keep per-item criteria and settlement; no fictitious atomicity between Git and Backlog.
* **`AC-10` Horizon planning (FLOW-15):** the planner produces a bounded `GraphPatch` with a preview; plan audit and application by the Backlog owner; no automatic apply.
* **`AC-11` Federated research and ideation (FLOW-16):** explicit recipes over `tare-ops research` and the Round Table; isolated participants, frozen outputs, sources and disagreements preserved in the finding's settlement.
* **`AC-12` Human wait (FLOW-17):** pause, answer and resume through the correct state without a permanent LLM session; tokens consumed during the wait are zero.

---

## 4. Causal Traceability Anchoring (SpecGraph)

* **Parent ADR:** pending; the ownership reconciliation (section 1) must appear in the ADR.
* **Graph nodes:** `flow-09-*` to `flow-17-*`.
* **Pytest test markers:** `@pytest.mark.verifies("SPEC-OS-WORKFLOW-IR-COMPILER-001", "AC-nn")`.
* **Target files:** `relay/workflow_ir.py`, `relay/workflow_compiler.py`, `relay/workflow_reducer.py` (new, in the OS), `graph_backlog.algorithms` (per-call reuse), `contracts/v1/workflow-ir.schema.json`, `tests/test_workflow_compiler.py`.

### Bounded realization of FLOW-10

Reducer v1 compiles the selected prepared Works into a DAG with the contracted
operator `work.execute/v1`. The operator reuses the Work recipe and owners;
it does not generalize arbitrary operators or replace the full compiler
proposal of FLOW-09. The Backlog provides dependencies and readiness per call; the OS
owns the logical revision, the serial decision and the link from attempt to receipt.

The plan and its hash live in `sequence.json`, next to the existing state.
Success, waiting, failure and unknown result have distinct semantics. A ready
independent node can progress despite another blocked one; unknown effects
prevent new dispatch. Recovery belongs to the Work owner and preserves the plan
revision. The FAULT matrix and the negative cases are in `tests/test_workflow_dispatch.py`;
the pure contract, in `tests/test_workflow_reducer.py`. Per-platform coverage must
be cited from verified runs, not presumed from these files.

Usage and limits: [Ready Work dispatch and recovery](../guides/WORKFLOW_DISPATCH.md).

### Bounded realization of FLOW-09

The OS owns the recipe, its compilation and the adaptation to the Work handler. The
structural DAG validation reuses `workflow_ir.compile_workflow`; reading
Work dependencies still comes from the Backlog algorithms per call.
There is no transfer of lifecycle or settlement to the Backlog, nor a resumption of the
frozen harness runtime.

`tare.tools/work-recipe/1` describes only the five logical groups of the
static path: `ground/v1`, `implement/v1`, `validate/v1`, `audit/v1`, `settle/v1`.
Inputs, outputs, capabilities, effects, obligations and authority requirements
belong to the closed catalog. The compiler rejects uncontracted removals and additions,
even when the JSON passes the schema. Identities are not
merged and no obligation is optimized out of the plan.

The explicit adaptation produces `tare.tools/compiled-work-recipe/1` with a single
existing physical operator, `work.execute/v1`. The groups do not become five
sessions, five descriptors or a new executor. The closed descriptor does not change.
The FLOW-10 v1 DAG and the interpretation of historical sequences are preserved.

Analyze mode produces no effects. The existing explicit execution command
compiles the fixed recipe before effects and keeps the result/hash in the Work
receipt; recovery reuses the original path and contracts. The compiled
result declares requirements, without granting authority or attesting tests,
availability, quality, admission or landing. General physical classes and
other recipes from the broad proposal are not needed for this realization and
are not declared delivered.

Usage: [Inspecting the known Work recipe](../guides/WORKFLOW_COMPILER.md).
