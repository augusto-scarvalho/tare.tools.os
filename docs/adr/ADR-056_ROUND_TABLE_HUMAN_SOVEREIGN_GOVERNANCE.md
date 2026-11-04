# ADR-056: Round Table Human-Sovereign Governance

- **Status:** RATIFIED / CANONICAL_SSOT
- **Date:** 2026-08-24
- **Round Table Case:** `CASE-2026-08-24-ROUND-TABLE-GOVERNANCE-AMENDMENT`
- **Proposal Version:** `v001`
- **proposal_sha256:** `3a7eee387e6495b40076d07d22060235ec830cb721c162eb38d57deab7dbd715`
- **round_table_sha256:** `b7f6f166aff9726531fd350fb1693f35d77f4543e28beebb1f79a30dd816d702`
- **human_ratification_sha256:** `666ee89188e85b40dca5140ceb961535a691da746b2b5faec9c36a0cbb805199`
- **roster_sha256:** `48e69872149f993f9ebe41967bc115374fe3a380980dca588be4e3bc13d68793`

---

## 1. Context

This ADR is the deterministic promotion of the intact, approved Round Table decision named above. The decision text was not reinterpreted by another model during promotion; it only received this traceability envelope so that it stays discoverable and verifiable in the repository.

## 2. Ratified decision

### Proposal — ADR-056 Round Table Structure and Mechanisms Amendment

**Case ID:** `CASE-2026-08-24-ROUND-TABLE-GOVERNANCE-AMENDMENT`

**Target ADR:** `ADR-056_ROUND_TABLE_HUMAN_SOVEREIGN_GOVERNANCE.md`

**Amends:** ADR-053, ADR-054, and ADR-055 without rewriting their immutable files.

**Self-referential:** `true`

#### 1. Core Goals (In-Scope)

1. **Preserve the tripartite execution shape.** Every deliberation has exactly three logical seats and at most one valid vote per seat. A fallback replaces one seat 1:1 and never adds quorum, weight, or independence.
2. **Define independence by lineage.** `bias_domain` is the governing identity boundary. Provider, transport, CLI/API, endpoint, quota source, region, or effort never creates another opinion from the same model lineage.
3. **Keep one executable roster source.** `relay/round_table_roster.json` remains the SSOT for models, deployments, priorities, bias domains, quota domains, endpoint domains, mediators, and scribes. Human-readable matrices remain generated projections.
4. **Make conflicts and exclusions case-bound.** Before initialization, the operator explicitly excludes the author's `bias_domain` and may exclude another owner-rejected domain. The engine deterministically selects independent replacements, persists the exclusions and initial deployments, and refuses a case that cannot form three distinct allowed domains.
5. **Retain honest attestation.** Local deployments require live health plus the exact served-model alias. External runners record their actual evidence level, including `unattested` or `request_pinned_unverified`; those labels may be admissible under roster policy but must never be presented as full attestation. A detected mismatch fails closed.
6. **Keep fallback behavior proportional.** Fixed deployments run first; fallbacks run only after classified failure. Global credit or endpoint failure may open a shared circuit. Alias-local absence, refusal, or model-specific throttling must not be misrepresented as global failure. Every attempted fallback still occupies only the original logical seat.
7. **Keep deterministic governance cheap.** Structured patch compilation and deterministic narrative are the defaults. LLM mediation and LLM scribing remain operator opt-ins and must use a `bias_domain` independent from every effective voting chair. Unavailable independent execution stays held; no substitute vote or consensus is invented.
8. **Use a finite review budget.** A case is configured for one to three rounds. The configured budget is trusted engine state within the declared local threat model; direct manual mutation is unsupported. At least one complete independent round must precede operator adjudication.
9. **Declare human sovereignty explicitly.** The repository owner/human gatekeeper is the final authority — an explicit operational dictatorship, not simulated democracy among models. Chairs are independent consultative red-team auditors: they identify concrete problems and minimal corrections but do not possess a political veto over an explicit lawful, in-scope human direction.
10. **Never fake consensus.** `APPROVED` means the Round Table actually reached its required approval result. Human authorization after a split review uses `AUTHORIZED_BY_OPERATOR_EXCEPTION`; the original tally, dissent, blockers, and chair receipts remain unchanged.
11. **Bind operator adjudication fail-closed.** `adjudicate` requires an exhausted configured round budget, `AWAITING_HUMAN_REVISION`, exact current proposal version/hash, exact authorized adjudication content hash, non-empty operator label/statement, the five required level-two sections, and explicit disposition of every recorded blocking issue inside `Blocker dispositions`.
12. **Bind the full artifact chain.** `OPERATOR_ADJUDICATION.md`, its content and byte hashes, proposal version/hash, tally, blocker IDs, operator statement, immutable `DECISION.md`, `OPERATOR_ADJUDICATED`, and `DECISION_FINALIZED` remain mutually verifiable. Repeating identical inputs is idempotent; divergent inputs fail closed.
13. **Keep exception authority bounded.** Every operator ruling states authorized scope, accepted risk, and explicit exclusions. An exception is terminal but is not eligible for `promote-adr`, because ADR promotion must not launder non-consensus authority into a consensus-backed canonical decision.
14. **Close self-approval circularity.** `adjudicate` must reject every `self_referential=true` case. A change to Round Table governance requires unanimous approval under the existing Round Table rule, stops at `APPROVED_PENDING_HUMAN_RATIFICATION`, and becomes `APPROVED` only after one exact human ratification bound to proposal and decision hashes.
15. **Amend by successor, never history rewrite.** ADR-053, ADR-054, and ADR-055 remain immutable evidence. ADR-056 supersedes only conflicting clauses and becomes the current discovery point for the combined structure and mechanisms.

##### Normative authority rule

The human-sovereign premise is fixed by the owner and is not itself a topic for chair preference. In this self-referential amendment, chairs must still audit whether the proposed implementation is internally coherent, fail-closed, honest about consensus, and proportionate. `REVISE` or `REJECT` requires a concrete falsifier against an in-scope objective and the smallest viable correction.

##### Required operator-adjudication sections

- `Operator ruling`
- `Blocker dispositions`
- `Authorized scope`
- `Accepted risk`
- `Explicit exclusions`

These sections live in the single canonical artifact `decision/OPERATOR_ADJUDICATION.md`. No additional approval packet or workflow is required.

#### 1.2 Explicit Non-Goals (Via Negativa / Out of Scope)

- Editing, squashing, deleting, or retroactively reclassifying ADR-053, ADR-054, ADR-055, their cases, votes, or ledgers.
- Adding a fourth chair, weighted vote, supermajority, second quorum, appeals court, committee, ethics board, or model veto over the owner.
- Treating two transports, providers, endpoints, efforts, or instances of one lineage as independent votes.
- Automatically discovering author identity from prose, Git history, telemetry, or model guesswork. Exclusion remains explicit and case-bound.
- Permanently banning any roster lineage globally because it was excluded from one case. Owner-rejected domains are excluded per case.
- Requiring a paid provider, local model, OpenRouter, Bedrock, or any named vendor as a universal prerequisite.
- Adding a daemon, service, database, blockchain, PKI, IAM system, policy DSL, risk taxonomy, workflow engine, ticket system, or new agent role.
- Requiring LLM mediation, LLM scribing, automatic catalog synchronization, benchmark ranking, or minimum vendor spending.
- Parsing semantic agreement from free-form prose or pretending structural blocker coverage proves the operator accepted a chair's reasoning.
- Allowing operator exceptions to become ADRs automatically or by the existing `promote-adr` command.
- Using the operator-exception mechanism to approve this amendment or any future self-referential governance change.
- Broad refactoring of the Round Table engine. After approval, the only required code change is the minimal self-referential adjudication guard plus focused regression coverage.

#### 2. Amendment effects on prior ADRs

1. **ADR-053 remains valid** for OpenRouter transport, credential isolation, exact pins, 1:1 fallback semantics, and global-failure circuits. ADR-056 adds persisted case exclusions and clarifies that alias-local failures are not global circuit failures.
2. **ADR-054 remains valid** for the roster SSOT, exactly three seats, lineage-based independence, GLM 5.3 placement, honest external evidence labels, deterministic defaults, and explicit ADR promotion. ADR-056 replaces any interpretation of fixed chairs as mandatory participants and any interpretation of the Round Table as co-sovereign with the human owner.
3. **ADR-055 remains valid** for the integrity threat model and proposal→vote→tally→decision→ratification→ADR chain. ADR-056 extends that chain to bounded operator adjudication while preserving the rule that only genuinely approved/ratified decisions are ADR-promotable.
4. Where predecessor prose conflicts with this amendment, ADR-056 governs prospectively. Historical artifacts retain their original meaning and guarantees.

#### 3. Falsification & Traceability Matrix

| ID | Invariant | Operational falsifier | Required evidence |
|---|---|---|---|
| `RT56-01` | Three logical seats | A fallback produces a fourth vote or duplicate seat vote | Tally and tests prove exactly one vote per seat |
| `RT56-02` | Lineage independence | Same `bias_domain` occupies two effective seats | Initialization/runtime-contract validation rejects composition |
| `RT56-03` | Case-bound exclusion | Excluded author/owner-rejected domain votes, mediates, or scribes | Persisted exclusions plus receipts and negative tests |
| `RT56-04` | Honest attestation | `unattested` or request pin is labeled fully attested | Receipt assertions and mismatch fail-closed tests |
| `RT56-05` | Proportional fallback | Alias-local 404/429 opens a global credit/endpoint circuit | Failure classifier/circuit tests distinguish local from global |
| `RT56-06` | Deterministic default | Ordinary mediation or scribing requires another LLM call | Tests prove zero text-role calls by default |
| `RT56-07` | Finite review | Case accepts zero or more than three rounds | Initialization and deliberation reject outside `1..3` |
| `RT56-08` | Human authority without fake consensus | Human exception changes a `2–1` tally to `3–0` or status `APPROVED` | Decision preserves tally and uses exception status |
| `RT56-09` | Exact adjudication binding | Wrong proposal/adjudication hash, version, operator, or statement succeeds | Fail-closed zero-mutation tests |
| `RT56-10` | Complete blocker disposition | Missing blocker ID outside/inside wrong section succeeds | Validator requires every ID inside `Blocker dispositions` |
| `RT56-11` | Artifact integrity | Proposal, adjudication, decision, or ledger changes without verify failure | Mutation tests break the corresponding hash binding |
| `RT56-12` | Idempotence | Exact retry creates another decision or ledger event | Retry returns idempotent and event counts remain fixed |
| `RT56-13` | No ADR laundering | Exception status passes `promote-adr` | Promotion rejects non-approved status |
| `RT56-14` | No self-approval | `self_referential=true` reaches operator adjudication | New guard rejects before artifact/state mutation |
| `RT56-15` | Human ratification | Self-referential `3/3` becomes final without exact human binding | Pending status and ratification tests remain mandatory |
| `RT56-16` | Frugality | Compliance requires new service, role, database, committee, or schema family | Diff limited to existing guard/test/docs/promotion paths |
| `RT56-17` | Immutable predecessors | ADR-053/054/055 files are edited to apply the amendment | Git diff shows only new ADR-056 and discovery metadata |

#### 4. Evidence and implementation boundary

- Submitted Git snapshot: `36c3afc24b9e7c40f0bf08fffb78c9e11205ced8`.
- Engine SHA-256: `1df93e5950249175fc57a20bd369263976ab461b0319828aabb9c085f284098c`.
- Roster SHA-256: `e104aaf711b2df99d1893ecf792bf18df52cfbfa0daa61c5cef6543ea4d9e843`.
- Roster validator SHA-256: `d9f5670b000104d3824c6a8b86cc966d5e504a84f35e8a5eacbc939d0bd00eb4`.
- Existing operator-adjudication implementation passed `276 passed, 1 skipped`; current focused Round Table tests passed `48 passed`.
- The only known normative gap in the submitted engine is `RT56-14`. It must be implemented after approval and before ADR-056 promotion.
- No other chair recommendation becomes implementation work unless it demonstrates a concrete in-scope falsifier.

#### 5. Decision requested from the Round Table

Approve ADR-056 as the create-only successor amendment that consolidates the renewed Round Table structure and mechanisms, preserves honest independent problem-finding, recognizes final human authority, closes self-approval circularity, and refuses bureaucratic or technical hypertrophy.

The owner wants this structure approved. Chairs are present to find genuine defects, not to renegotiate human sovereignty or inflate the mechanism. Residual hardening advice that does not falsify `RT56-*` must be recorded as non-blocking.


## 3. Outcome of the deliberation

- **GOOGLE** via `chair-google-cli`: `APPROVE` (confidence `0.98`). The ADR-056 proposal establishes a consistent governance and deliberation architecture, clearly bounding human sovereignty without resorting to simulated consensus or infrastructure hypertrophy. The falsification matrix RT56-01..RT56-17 strictly ties together the invariants of tripartite seats by lineage (`bias_domain`), immutability of prior decisions and closing the circularity for self-referential cases. No blocking in-scope falsifiers were identified in the proposed logical contracts.
- **ANTHROPIC** via `chair-anthropic-cli`: `APPROVE` (confidence `0.86`). ADR-056 is internally coherent, fail-closed by construction, and honest about consensus: it preserves the tripartite seat model, lineage-based independence, and the full hash-bound artifact chain while making human sovereignty explicit without laundering exceptions into ADRs. The falsification matrix RT56-01..RT56-17 is concrete and testable, and the via negativa correctly refuses bureaucratic and technical hypertrophy. The single declared gap (RT56-14 guard not yet implemented) has a sequencing weakness, but defense-in-depth (unanimity requirement for self-referential approval plus RT56-13 promotion rejection) prevents any laundering outcome, so residual findings are non-blocking.
- **OPENAI** via `chair-kimi-cli`: `APPROVE` (confidence `0.9`). The ADR-056 amendment is internally coherent, fail-closed and proportional: it consolidates human sovereignty without simulating democracy, closes the self-approval circularity through unanimity + human ratification, and forbids laundering consensus through promote-adr. The RT56-01..17 matrix covers the invariants with concrete operational falsifiers, and the negative scope explicitly blocks bureaucratic and technical hypertrophy. The only declared normative gap (RT56-14 guard not yet implemented) is acknowledged, bounded and made a condition of the ADR promotion, and is therefore non-blocking.

## 4. Recorded reservations

- `ISS-GOOGLE-R1-01` — **non-blocking**: Validation of the 'Blocker dispositions' section must ensure that blocker ID matching is deterministic and insensitive to trivial markdown formatting variations (such as sub-items or extra line breaks).
- `ISS-GOOGLE-R1-02` — **non-blocking**: The normative gap declared in RT56-14 (guard rejecting adjudication on `self_referential=true`) requires negative mutation test coverage before the final promotion of ADR-056.
- `ISS-ANTHROPIC-R1-01` — **non-blocking**: Section 4 sequences the RT56-14 guard as 'after approval and before ADR-056 promotion', leaving a window in which the unguarded engine could technically accept an adjudicate call on this self_referential case, contradicting the letter of objective 14 even though no laundering outcome is reachable.
- `ISS-ANTHROPIC-R1-02` — **non-blocking**: The Git snapshot, engine, roster, and validator SHA-256 values and test counts in Section 4 are asserted in the proposal text and are not independently verifiable from the deliberation payload alone, so chair approval binds to the claimed evidence rather than verified evidence.
- `ISS-ANTHROPIC-R1-03` — **non-blocking**: The RT56-10 operational falsifier wording ('Missing blocker ID outside/inside wrong section succeeds') is grammatically ambiguous about the two failure cases it intends to cover (ID absent entirely vs. ID disposed outside the Blocker dispositions section), which could let a test author implement only one of the two negative cases.
- `ISS-OPENAI-R1-01` — **non-blocking**: The evidence hashes declared in section 4 (Git snapshot, engine, roster, validator) and the test results (276 passed / 48 passed) are not verifiable from the proposal text itself; the chair depends on external attestation.
- `ISS-OPENAI-R1-02` — **non-blocking**: The RT56-14 gap (missing guard that rejects adjudicate on self_referential=true cases) exists in the submitted engine; although declared and bounded, until it is implemented the engine admits a path the amendment forbids.
- `ISS-OPENAI-R1-03` — **non-blocking**: Objective 8 declares the round budget trusted engine state within the local threat model, with direct manual mutation 'unsupported' — a guarantee by convention, not by mechanism.

## 5. Traceability and authority

- Semantic authority remains with the case's immutable `DECISION.md`.
- For self-referential cases, promotion requires a valid `HUMAN_RATIFICATION.json`.
- The hash of this ADR is recorded in the chained ledger as `ADR_PROMOTED`.
- Future changes require a new superseding ADR; this file is create-only.
