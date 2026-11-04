# ADR-057: Agent Runtime Bounded Context and Canonical Repository

- **Status:** RATIFIED / CANONICAL_SSOT
- **Date:** 2026-08-24
- **Round Table Case:** `CASE-2026-08-24-AGENT-RUNTIME-BOUNDARY`
- **Proposal Version:** `v002`
- **proposal_sha256:** `2324aefcb99d5f40cf93ee6ab9e8a9916ce74f2ba4a0b574bada26c8dd0a88d0`
- **round_table_sha256:** `9b1fa2be30cda9830411539d6689197f44ba5df2fc1332aaf4371ceb21323b19`
- **human_ratification_sha256:** `not-required`
- **roster_sha256:** `48e69872149f993f9ebe41967bc115374fe3a380980dca588be4e3bc13d68793`

---

## 1. Context

This ADR is the deterministic promotion of the intact, approved Round Table decision named above. The decision text was not reinterpreted by another model during promotion; it only received this traceability envelope so that it stays discoverable and verifiable in the repository.

## 2. Ratified decision

### PROPOSAL v002 — Bounded context and repository `tare.tools.agent-runtime`

- **Case:** `CASE-2026-08-24-AGENT-RUNTIME-BOUNDARY`
- **Date:** 2026-08-24
- **Class:** owner-directed architectural decision; adversarial consultative review
- **Antecedent:** `CASE-2026-08-24-HARNESS-RUNTIME-OWNERSHIP` `APPROVED v002`
- **Authoring domain to exclude:** `openai/gpt`
- **Profile:** `north_star`

#### 0. Binding owner direction

The owner determines that the agent loop is the responsibility of an entity distinct from the OS and the Kernel. The direction is to create a new canonical repository, `tare.tools.agent-runtime`, and to keep `tare.tools.harness` frozen and unchanged as a historical prototype.

The Round Table must red-team this direction and may demand proportional corrections to boundary, dependency, sequence, security and falsification. It must not replace the direction with reactivation of the legacy, coupling of the agent loop to the OS/Kernel, a generic platform or bureaucracy without a concrete falsifier.

#### 1. Core Goals (In-Scope)

1. **Recognize a distinct entity:** define `Agent Loop Execution` as a state machine distinct from Work/Workflow and from the execution substrate.
2. **Create a new canonical identity:** reserve `tare.tools.agent-runtime` for this bounded context, with a new history and without the monolith's Git lineage.
3. **Preserve the legacy:** keep `tare.tools.harness` frozen, with no rename, unfreezing, import or code copy.
4. **Separate owners:** keep Work, Workflow, Routing, Authority, Evidence and settlement in the OS; providers, processes, sandbox and mediated capability execution in the Kernel; turn loop, tactical context, budgets, retry, cancellation and terminal claim in the Agent Runtime.
5. **Enforce dependency inversion:** the Agent Runtime core consumes minimal injected ports and does not import private implementations of the OS or Kernel.
6. **Keep canonical exclusion with liveness:** preserve at most one active canonical loop per Work and require, before a rebind, an explicit handoff or a verifiable revocation of the previous binding. A crash, timeout or zombie process may neither deadlock the Work nor allow two canonical emitters.
7. **Preserve Aider:** keep Aider as incumbent and baseline, with no fork, reimplementation or behavioral change in the bootstrap.
8. **Contain Strands:** allow Strands only as an optional, removable adapter/laboratory.
9. **Preserve external authority:** the Agent Runtime may emit an intent/claim, but it does not grant a Permit, does not settle Work, does not create canonical Evidence and does not execute effects outside mediated capability.
10. **Promote on evidence:** add the new repo to the OS federation only after a qualified bootstrap, boundary tests and an explicit pin decision.

#### 1.2 Explicit Non-Goals (Via Negativa / Out of Scope)

1. Do not rename `tare.tools.harness` nor reuse its history, issues, PRs or branches.
2. Do not copy the legacy modules, including those classified `DISCARD_FROZEN`.
3. Do not create a daemon, database, public SDK, UI, registry or plugin framework.
4. Do not duplicate Work, Workflow, strategic Routing, Authority, Effects, Evidence, Evolution or settlement.
5. Do not make Strands a mandatory core dependency.
6. Do not replace Aider nor perform a cutover in this decision.
7. Do not turn the runtime's session/checkpoint into Work identity.
8. Do not allow two `active-canonical` loops for the same Work.
9. Do not add the repo to the federation before a qualified commit/release exists.
10. Do not create new planes, primitives, protocols or a fourth composition repository beyond the minimum demonstrated by F0.

#### 2. Proposed resolution

##### 2.1 Amendment to the antecedent case

Replace only the provision “Agency Execution as an internal responsibility of the Runtime, without a new bounded context” with:

> `Agent Loop Execution` is an independent bounded context, materialized in the canonical repository `tare.tools.agent-runtime`. It owns only the tactical mechanics of the loop and operates through injected ports. Work/Workflow/Authority/Evidence remain in the OS; inference/process/sandbox/capability execution remain in the Kernel.

The other provisions of `CASE-2026-08-24-HARNESS-RUNTIME-OWNERSHIP v002` remain in force.

##### 2.2 Dependency direction

```text
composition root
  ├── invokes OS control plane contracts
  ├── obtains a fenced canonical binding from the Authority owner
  ├── injects minimal ports into tare.tools.agent-runtime
  └── provides Kernel/provider adapters

agent-runtime core
  ├── no import from OS private modules
  ├── no import from Kernel private modules
  ├── no canonical ledger write authority
  ├── no adapter auto-discovery
  └── no dependency on tare.tools.harness
```

The composition root is a single host, identified in F0 before any executable entrypoint. This host is not the core of `tare.tools.agent-runtime`, does not implement turn, retry, cancellation or terminality policy in the OS, and does not live in the Kernel. It only invokes control plane contracts, obtains the canonical binding, injects ports and provides adapters. F0 must name the owner package of this host within the already authorized surfaces; this does not authorize a fourth repository, daemon or composition framework.

The canonical binding registry/gate belongs to the Authority owner in the OS. It grants, renews, revokes and fences the right to emit a canonical intent/claim, but it does not advance the agent loop. Resume preserves the same binding; rebind requires a handoff or an observable revocation of the previous owner. Late claims or claims emitted by a revoked binding are rejected.

Concrete protocols must emerge from F0. Names such as `AgentLoop`, `ModelPort`, `CapabilityPort` and `RuntimeEventSink` remain decomposition hypotheses, not a pre-ratified API.

##### 2.3 Minimal sequence

1. **F0 — Read-only reconciliation:** produce a versioned report with a map of owners, types, adapters, eliminated ports, composition root owner, binding gate owner, cancellation/timeout propagation and pin contract. Without this receipt, there is no executable bootstrap.
2. **Qualified bootstrap:** create the new repository with README/charter, changelog, CI, boundary tests and the smallest surface required by the F0 receipt.
3. **F1 — Laboratory:** run H0–H3 with Aider as baseline and Strands optional/removable from the first commit; the core build stays green without Strands.
4. **F2 — Shadow:** only if F1 passes; synthetic/forked identity, no writes to the live ledger or workspace and no eligibility promotion.
5. **Host and liveness gate:** the first entrypoint is only created after F0 names the single host and the binding gate. Crash/restart, timeout and zombie process must be exercised before F1; there is no second owner before a fenced handoff or revocation.
6. **Federation gate:** the receipt of the first pin records the version/hash of the ports contract and its compatibility tests. Shims may not implement loop policy in the OS or Kernel.

#### 3. Falsification & Traceability Matrix

| ID | Invariant/claim | Falsifier | Acceptance evidence |
|---|---|---|---|
| `ARB-01` | Agent Loop Execution is a distinct machine | The same state/type has to represent the Work lifecycle or the process sandbox | State and ownership map without overlap |
| `ARB-02` | The OS does not absorb the loop | Turn loop/context/budget code is added to the OS | Boundary test and diff with no loop implementation in the OS |
| `ARB-03` | The Kernel does not absorb agency policy | The Kernel decides the next turn, semantic terminality or strategy | Kernel limited to substrate/adapters, plus negative tests |
| `ARB-04` | The core uses dependency inversion | Import of a private OS/Kernel module or a checkout path | Import graph with no private/cyclic dependency |
| `ARB-05` | The legacy stays frozen | Code, history, a fixture or a module is copied/imported from the harness | Independent genesis, bootstrap source inventory and provenance audit |
| `ARB-06` | A Work has at most one canonical loop | Two active bindings emit a canonical intent/claim for the same Work | The gate rejects a second owner; fenced handoff/revocation tested |
| `ARB-07` | The runtime does not grant authority | A mutating intent produces an effect without a Permit/mediated capability | Fail-closed negative test and observable denial |
| `ARB-08` | Loop state does not become Work state | A session/checkpoint/binding token is needed to identify or settle Work | Work identity and settlement survive a runtime swap |
| `ARB-09` | Aider stays incumbent | The bootstrap changes Aider's command, prompt, workspace or artifacts | Parity of the current binding before/after |
| `ARB-10` | Strands is removable | Removal requires migrating canonical state or breaks the core | Optional adapter and green build/test without the dependency |
| `ARB-11` | The new repo does not become a platform | The bootstrap requires its own daemon, DB, registry, UI or routing | Minimal inventory and absence of the forbidden surfaces |
| `ARB-12` | The federation only receives a qualified release | The OS pins a commit without green CI/boundary/contract gates | Qualification receipt before the first pin |
| `ARB-13` | Shadow does not contaminate production | F2 writes an ActionRequest, Evidence, EffectReceipt or completion to the live ledger | Isolated shadow namespace and sink; write denial fixture |
| `ARB-14` | The repo reduces real coupling | Composition requires a circular dependency between OS, runtime and Kernel | Acyclic dependency DAG and substitution by fake ports |
| `ARB-15` | The composition host is single and external to the core | The OS/Kernel advances turn/retry/cancel/claim; the core auto-discovers adapters; or a fourth composition repo appears | The F0 receipt names one host in an authorized surface; tests forbid loop policy and auto-wiring outside the runtime |
| `ARB-16` | The canonical binding survives a crash without deadlock or a second owner | A crash without handoff blocks the Work indefinitely; restart/retry/zombie emits in parallel; a revoked claim is accepted | Observable lease/heartbeat/revocation with fencing; rebind after revocation; rejection of late emission |
| `ARB-17` | F0 is an auditable gate | The bootstrap starts without a versioned map/receipt or without a host/binding gate owner | A versioned F0 receipt precedes the first entrypoint |
| `ARB-18` | A pin does not launder contract drift | The OS pins a release with incompatible semantics and adds loop policy in a shim | Ports hash/version, contract tests and shims limited to structural adaptation |

#### 4. Deliberation criterion

`APPROVE` when the proposal materializes the owner's direction without reverse coupling, resurrection of the legacy or a premature platform.

`REVISE` or `REJECT` requires a concrete blocker tied to `ARB-*`, a reproducible scenario and a proportional correction. A preference for another name, a wish for the status quo or a demand for infrastructure outside the non-goals is not a valid blocker.

#### 5. Required output from the Round Table

1. concrete risks of the split into its own repo;
2. minimal corrections to dependency direction and composition;
3. missing falsifiers;
4. an `APPROVE`, `REVISE` or `REJECT` decision preserving the real tally;
5. an indication of the ADR succeeding ADR-048 and the runtime ownership case.


## 3. Outcome of the deliberation

- **OPENAI** via `chair-xai-openrouter`: `APPROVE` (confidence `0.86`). v002 materializes the owner's direction with its own bounded context, dependency inversion, frozen legacy and a falsification matrix sufficient to govern the cut. The design is viable without resurrecting the harness, without absorbing the loop into the OS/Kernel and without a premature platform, provided F0 names a single host and a fenced binding before any entrypoint. The real risks of splitting into a repository — contract drift, orphan composition and late emission after a partition — are already anchored in ARB-14/15/16/18 and do not justify reopening the direction. Residually, the ordering in 2.3 and the fencing of in-flight Kernel results deserve proportional reinforcement in F0/the ADR, not a retreat from the boundary.
- **GOOGLE** via `chair-google-cli`: `APPROVE` (confidence `0.98`). Proposal v002 establishes an impeccable and mathematically consistent separation of bounded contexts, isolating the Agent Loop in the canonical repository 'tare.tools.agent-runtime' without reverse coupling or authority contamination. The technical design ensures rigorous dependency inversion through injected ports, strict infrastructure containment (no daemons or 4th repository) and robust protection against split-brain/zombies through binding fencing at the OS Authority gate. The phased execution plan (F0 to F2) together with the 18-criterion falsification matrix gives immediate safety and operational viability.
- **ANTHROPIC** via `chair-anthropic-cli`: `APPROVE` (confidence `0.87`). v002 materializes the owner's direction with real architectural discipline: its own bounded context with genuine dependency inversion (injected ports, zero private imports), canonical exclusion with liveness resolved through observable lease/fencing/revocation, and an F0→F1→F2 sequence gated by auditable receipts before any executable entrypoint. The ARB-01..18 matrix covers the structural failure modes — split-brain, post-crash deadlock, legacy resurrection, premature platform and contract drift through a shim — with concrete operational falsifiers. Two gaps remain, in fencing depth and cancellation propagation, both addressable in F0 without changing the resolution, and therefore non-blocking.

## 4. Recorded reservations

- `ISS-OPENAI-R2-01` — **non-blocking**: The minimal sequence lists the host/liveness gate after F2, although the text itself requires exercising crash/timeout/zombie before F1. This does not break the invariant, but creates operational ambiguity about ordering.
- `ISS-OPENAI-R2-02` — **non-blocking**: ARB-16/07 cover a late claim and an effect without a Permit, but do not explicitly falsify the result of a capability already admitted in the Kernel after a cancel, timeout or binding revocation.
- `ISS-ANTHROPIC-R2-01` — **non-blocking**: The ARB-16 fencing is checked at intent/claim ingestion, but no invariant covers an in-flight effect: a Permit granted before the binding revocation may have its capability executed by the Kernel after the revocation, producing a canonical effect from an already deposed emitter — split-brain in the effects plane, not in the claims plane.
- `ISS-ANTHROPIC-R2-02` — **non-blocking**: Cancellation/timeout propagation is required in the F0 receipt (§2.3.1) but has no corresponding invariant in the matrix: no ARB-* falsifies the scenario in which a cancel issued by the OS never ends the loop in the runtime nor the processes in the Kernel, leaving the guarantee mapped but not testable.
- `ISS-ANTHROPIC-R2-03` — **non-blocking**: The ARB-16 lease/heartbeat mechanism presupposes a time authority, but F0 is not required to name the binding gate's time source; with the composition host and the Authority owner potentially on different machines, clock skew can expire valid leases or keep dead ones alive, and monotonic-token fencing only mitigates this if the receipt explicitly requires it.

## 5. Traceability and authority

- Semantic authority remains with the case's immutable `DECISION.md`.
- For self-referential cases, promotion requires a valid `HUMAN_RATIFICATION.json`.
- The hash of this ADR is recorded in the chained ledger as `ADR_PROMOTED`.
- Future changes require a new superseding ADR; this file is create-only.
