# SPEC-OS-EXECUTION-SETTLEMENT-001: Settlement, recovery and observation of an executed Work

- **Status:** PROPOSED (awaiting an ADR via Round Table)
- **Governing ADR:** pending; based on [ADR-062](../adr/ADR-062_UNIVERSAL_FRUGAL_ENGINEERING_DOCTRINE_AND_SOVEREIGN_BYOC.md)
- **Target Repository:** `tare.tools.os` (decision), `tare.tools.kernel` (Git and processes), `tare.tools.backlog-graph` (graph mutation)
- **Version:** 0.1.0
- **Derived from:** FLOW-03, FLOW-04, FLOW-05, FLOW-21

---

## 1. Context & Objective

The OS already materializes an immutable candidate, verifies its bytes and lands it
by CAS (`relay/candidate_change.py`). Audit cases are finite and
advisory: `CLOSED` is not `Permit`, `ADMITTED` or `DONE`. This SPEC connects
these pieces into a Work settlement without creating a second chain of snapshot,
test or receipt, and fixes what happens on stop, crash, late result and
unknown effect. Measurement is fail-open; Authority, the candidate and essential
evidence are not.

---

## 2. Interface Contract & Data Structures

```text
candidate (CANDIDATE_CHANGE.json, existing)
   → audit case (tare-ops audit, existing)  →  evidence
   → admission (Work owner; decision recorded with explicit Authority)
   → CAS landing (land_recorded_candidate, existing)
   → Backlog reconciliation (backlog land / complete-node; retryable)
```

Terminal run states: `SETTLED`, `REJECTED`, `CANCELLED`,
`EFFECT_UNKNOWN`, `EXECUTION_FAILED`, `INCONCLUSIVE_WITH_REVISIT_TRIGGER`.
None is implicit; `EFFECT_UNKNOWN` requires a diagnosis before repeating
any effect.

Settlement receipt, `tare.tools/work-settlement/1`: `run_id`, `work_id`,
`candidate_commit`, `audit_case_id`, `audit_verdict`, `admission`
(`by`, `authority`, `at`), `landing` (`target_ref`, `base_commit`,
`landed_commit` or `CAS_CONFLICT`), `backlog_update` (`applied` | `pending` |
`reconciled_after_crash`), `cost` per attempt, including failed ones.

---

## 3. Verifiable Acceptance Criteria

* **`AC-01` Exact candidate:** only the commit verified by `verify_candidate_receipt` can be admitted; a divergent commit ends in `REJECTED` with `CandidateChangeRejected`.
* **`AC-02` Audit is evidence, not authority:** admission references the audit case and its verdict; a `CLOSED` case without an admission decision does not advance the Work.
* **`AC-03` CAS landing:** a base change between candidate and landing produces `CAS_CONFLICT`; no silent rebase.
* **`AC-04` Git↔Backlog reconciliation:** a crash between landing and the Backlog update is completed by an idempotent reconciliation that proves the commit was incorporated; it never reimplements.
* **`AC-05` Honest cancellation (FLOW-04):** requesting cancellation records `CANCEL_REQUESTED`; only the Kernel's confirmation that the process ended records `CANCELLED`; a result that arrives later is discarded with a receipt, without regaining authority.
* **`AC-06` Unknown effect:** a process interrupted or a response lost after an external effect ends in `EFFECT_UNKNOWN`; resuming requires a recorded diagnosis before repeating.
* **`AC-07` Budget:** the `budget` of turns, time and USD is enforced by the Agent Runtime; exceeding the budget produces `INCONCLUSIVE_WITH_REVISIT_TRIGGER`, never approval.
* **`AC-08` Fault injection (FAULT):** 6 points × 3 schedules × 2 platforms, up to 36 traces: reservation before spawn; effect before receipt; receipt before transition; landing before Backlog; expiry/rebind; cancellation/late result. Zero duplicated effects and zero late claims accepted.
* **`AC-09` Passive observation (FLOW-05):** each run and each model call enters `tare-ops telemetry` with identity, UTC start, duration, usage, reported versus estimated cost and the attempt→result link; a missing counter is unknown, never zero; a telemetry failure does not block the run.
* **`AC-10` Audit frugality (FLOW-21):** a blocker states the obligation, evidence, impact and smallest remedy; one review plus one focused recheck by default; reopening only with a new candidate, a material defect or a fallen premise; audit minutes, adjudicated false blocks and reopenings without new information are measured every 10 Work.

---

## 4. Causal Traceability Anchoring (SpecGraph)

* **Parent ADR:** pending; ADR-061 (retired) for advisory audit; [ADR-062](../adr/ADR-062_UNIVERSAL_FRUGAL_ENGINEERING_DOCTRINE_AND_SOVEREIGN_BYOC.md).
* **Graph nodes:** `flow-03-*`, `flow-04-*`, `flow-05-*`, `flow-21-*`.
* **Pytest test markers:** `@pytest.mark.verifies("SPEC-OS-EXECUTION-SETTLEMENT-001", "AC-0n")`.
* **Target files:** `relay/candidate_change.py` (reuse), `relay/audit_cases.py` and `relay/audit_execution.py` (reuse), `relay/work_settlement.py` (new, thin), `scripts/round_table/report_round_table_telemetry.py` (`work-run` source), `tests/test_work_settlement.py`, `tests/test_work_run_faults.py`.
