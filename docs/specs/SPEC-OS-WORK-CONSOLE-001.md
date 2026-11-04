# SPEC-OS-WORK-CONSOLE-001: Run and settle an explicit Work through the tare facade

- **Status:** PROPOSED (awaiting an ADR via Round Table; not CANONICAL_SSOT)
- **Governing ADR:** pending; current normative basis [ADR-062](../adr/ADR-062_UNIVERSAL_FRUGAL_ENGINEERING_DOCTRINE_AND_SOVEREIGN_BYOC.md)
- **Target Repository:** `tare.tools.os`
- **Version:** 0.1.0
- **Derived from:** FLOW-01, FLOW-02, FLOW-07, FLOW-08, FLOW-22 of the work program v0.1 (2026-09-07) and its implementation plan

---

## 1. Context & Objective

Today a real demand from the graph is handled by opening a vendor's CLI or TUI and
carrying context by hand. The goal is for `tare run <WorkId>` to run and
settle a Work through the `tare_ops` facade, reusing the existing owners:
Backlog for Work and the frontier, SpecGraph for grounding, `TrainExecutionDescriptor`
for scope, `relay/aider_agent` (since retired) and the Agent Runtime for the session, the Kernel for
processes, `candidate_change.py` for the immutable candidate, finite audit cases
as evidence, and the admission owner to advance the Work.

Binary product criterion: **can I run and settle a real demand from the
graph without manually entering the vendor's CLI/TUI?**

Out of scope for this SPEC: dynamic planner, learned routing, full TUI,
workflow IR, external framework, restoring the frozen harness.

---

## 2. Interface Contract & Data Structures

```text
tare run <WorkId> [--deployment <role deployment>] [--dry-run] [--budget-usd X] [--max-seconds S]
tare status <RunId>
tare cancel <RunId>
tare explain <RunId>        # why this deployment; which stage; what is missing
```

```python
# relay/work_run.py (new, thin; no agent loop of its own)
def run_work(work_id: str, *, deployment: str = "local-qwen", dry_run: bool = False,
             budget: RunBudget | None = None) -> dict:
    """ground -> bind -> execute -> candidate -> audit -> admit -> land -> reconcile.
    Each stage reuses an existing owner and writes a receipt into custody.
    Returns {"status": ..., "run_id": ..., "work_id": ..., "stage": ..., "receipts": [...], "authority": "NONE"}."""
```

Run receipt, schema `tare.tools/work-run/1`: `run_id`, `work_id`,
`work_revision`, `descriptor_sha256`, `deployment`, `binding_id`,
`candidate_commit`, `audit_case_id`, `admission`, `landing`, `stage`,
`started_at`, `ended_at`, `budget`, `cost` (reported/estimated/unknown),
`effects_unknown: bool`, `authority: "NONE"`.

Identity: `run_id` is a UUID per attempt; never a hash of the demand text.
One `WorkId` can have several `run_id`s, each with its own candidate or absence.

---

## 3. Verifiable Acceptance Criteria

* **`AC-01` Recorded reconciliation (FLOW-01):** `docs/operations/WORK_RUN_RECONCILIATION.md` exists with the reuse → gap → owner map for each stage, pointing to path and revision; no new registry, canonical type or copy of the harness.
* **`AC-02` Explicit Work, no implicit selection (FLOW-02):** `tare run` requires a `WorkId`; a Work that does not exist, is not eligible by the frontier or has no sealed descriptor ends in `BLOCKED` with a reason, without starting a session.
* **`AC-03` Real binding before the executor:** the session starts only after binding with the descriptor's repository, Git base, paths and verifiers; the executor does not choose the Work or widen scope.
* **`AC-04` No manual transport operation:** in the PILOT (3 disposable + 5 real Work), the operator does not open a vendor CLI or copy context; the metric `transport interventions per Work` is recorded and equals zero in the accepted cases.
* **`AC-05` State queryable without a live session:** `tare status` and `tare explain` answer from custody after the executor process has died.
* **`AC-06` A host or adapter failure preserves the previous path:** any handler failure leaves the current operating mode intact; no Work state is changed before the admission stage.
* **`AC-07` Minimal console (FLOW-07):** light conversation, `status`, `run`, `cancel` and an explanation of the choice through the same facade; no new UI to add a new handler.
* **`AC-08` Reproducible installation (FLOW-08):** smoke test on a clean Windows and POSIX checkout with explicit configuration and documented rollback; a configuration failure becomes a named error, not a universal installer.
* **`AC-09` Observable beta (FLOW-22):** 3–5 users, 2 projects, real tasks; metrics of spontaneous adoption and human time saved are recorded with no additional paid inference to measure them.

Mandatory falsifiers (CONTRACT): Work without a descriptor; descriptor with
a divergent Git base; session dead before the candidate; `status` after a crash.

---

## 4. Causal Traceability Anchoring (SpecGraph)

* **Parent ADR:** pending (Round Table on this SPEC); doctrine [ADR-062](../adr/ADR-062_UNIVERSAL_FRUGAL_ENGINEERING_DOCTRINE_AND_SOVEREIGN_BYOC.md).
* **Graph nodes:** `flow-01-*`, `flow-02-*`, `flow-07-*`, `flow-08-*`, `flow-22-*` (tag `spec:SPEC-OS-WORK-CONSOLE-001`).
* **Pytest test markers:** `@pytest.mark.verifies("SPEC-OS-WORK-CONSOLE-001", "AC-02")` etc.
* **Target files:** `tare_ops.py` (subcommands `run`, `status`, `cancel`, `explain`), `relay/work_run.py`, `relay/train_execution.py` (read), `relay/aider_agent.py` (reuse; since retired), `docs/operations/WORK_RUN_RECONCILIATION.md` (since removed), `tests/test_work_run.py`.
* **Normative dependencies:** SPEC-OS-EXECUTION-SETTLEMENT-001 for the candidate → audit → admit → land stages; SPEC-OS-ROUTING-BASELINE-001 (retired) for the deployment choice.
