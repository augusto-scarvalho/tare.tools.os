---
id: SPEC-OS-SPECGRAPH-HOOKS-001
title: Event-driven project context and doctor within a budget
status: ACCEPTED
owner: tare.tools.os
work_id: SPECGRAPH-COMPLETION-C7
decision_authority: operator_instruction
delivery_status: NOT_QUALIFIED
acceptance_criteria:
  AC-01: Root, Work, source, snapshot and role are explicit; an event does not select a Work implicitly.
  AC-02: Registration, triggering and payload delivery are qualified separately per host and real tool name.
  AC-03: A search with no capable substitute, or after MISS/STALE/TIMEOUT, has a bounded fallback without blocking repetition; a known path does not require rediscovery.
  AC-04: Repeated context stays silent; mandatory units are preserved; zero network/LLM/full scan in the synchronous handler.
  AC-05: Post-edit coalesces the delta; Stop does not create a loop; delegation passes the handle/source and minimal evidence.
  AC-06: Doctor reports capabilities, coverage, index, governance, evidence and effectiveness; deep/repair are explicit actions.
  AC-07: Paired tasks measure correctness, total tokens, wall time, retries and intervention; absence of gain reduces the advisory handler.
---

# SPEC-OS-SPECGRAPH-HOOKS-001

The OS composes SpecGraph services, documents read from their owner repositories and explicit Work. The Kernel normalizes tools/vendors and holds policy. SpecGraph does not duplicate orchestration or attest execution. Each payload includes origin/revision and coverage limit.

The planner receives intent/AC/constraints; the implementer, relevant symbols/tests; the auditor, diff/claims/proofs/gaps; the explorer, map/candidates/coverage. The role can change during a task. Initial budgets are hypotheses, never a justification for silently cutting an AC.

Implement on top of `relay/session_hooks.py`, the existing doctor and adapters in the Kernel owner. A receipt error must identify the invalid reference; identical repetition is capped, with a usable partial result.

Decision: [ADR-OS-SPECGRAPH-001](../decisions/ADR-OS-SPECGRAPH-001.md). Canonical Work: `work-graph.json`, package `SPECGRAPH-COMPLETION-C7`. Qualification requires fixtures of real events, multi-root, branch/resume/subagent, unavailable service and bounded payload; isolated registration tests are not enough. No execution or release approval is attested by this document.
