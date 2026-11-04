# ADR-055: Round Table Decision Integrity

- **Status:** RATIFIED / CANONICAL_SSOT
- **Date:** 2026-08-23
- **Round Table Case:** `CASE-2026-08-23-ROUND-TABLE-INTEGRITY-SUCCESSOR`
- **Proposal Version:** `v002`
- **proposal_sha256:** `58c981375f741327a759ea313d16cb2c05ac34d951bc443a5692f244aeb246d8`
- **round_table_sha256:** `61acf2c41f23ae2f7fecc0355be6cc0ca891e42be83e4e374f7fabf51fee43cc`
- **human_ratification_sha256:** `109c17fa690fa111a1ff27d08d5a451030cce2f452dc820b831ab12751343c50`
- **roster_sha256:** `ec84b6581710c47668f57558556e4b4ae6a72a05cfa7d4000d84dd3da0f859c7`

---

## 1. Context

This ADR is the deterministic promotion of the intact, approved Round Table decision named above. The decision text was not reinterpreted by another model during promotion; it only received this traceability envelope so that it stays discoverable and verifiable in the repository.

## 2. Ratified decision

### Prospective Round Table integrity and prevention of forged consensus

#### Decision requested

Deliberate whether the current contract closes, with proportional controls, the material
blockers of the historical cases `AGENT-GOVERNANCE-INTEGRITY-AND-FSM-ENFORCEMENT`
and `ANTI-FAKE-CONSENSUS-AND-GOVERNANCE`, without creating rubber-stamp infrastructure.

#### 1. Core Goals (In-Scope)

1. Forbid agents and operators from synthesizing votes, tally, decision, ratification
   or promotion outside the engine's canonical operations. Artifacts declared
   official must originate from the FSM and from real calls to the registered deployments.
2. Preserve create-only per `case_id`, an exclusive non-stealable lock with monotonic
   fencing, atomic writes and a SHA-256 chained ledger.
3. Bind each vote to seat, nonce, deployment, logical model, bias
   domain, runner, attestation status and streaming artifact. Metadata is
   forensic evidence, not mathematical proof of the vendor's identity.
4. Record in the `TALLY_COMPLETED` event the votes, deployments and receipts that
   determined the outcome.
5. For new cases with `workflow_contract.schema_version=2`, make
   `DECISION_FINALIZED` record `proposal_sha256` and `decision_sha256`
   separately, together with the final version and deployments. The ledger chain
   links this event to the preceding tally.
6. Make `verify` recompute the hash of `DECISION.md` and require exactly one compatible
   terminal event. A later change to the file fails closed.
7. For a self-referential decision, require human ratification that also binds the
   decision hash. For canonical publication, require explicit create-only promotion
   that binds proposal, decision, ratification, ADR and the `ADR_PROMOTED` event.
8. Treat contracts earlier than schema 2 as historical evidence, without claiming
   the new guarantee retroactively. Do not break old cases or ADRs en masse.
   V16, created immediately before this change and not yet canonical, will have its
   decision hash bound by human ratification before any promotion.
9. Keep the threat described honestly: hashes make tampering detectable
   within the observed history; they do not prevent someone with unrestricted write access
   from destroying and rebuilding every file and the Git history. That threat requires
   repository controls and human review, not a local blockchain.
10. Absorb the SEALED case and the old v003 case into this single successor. Their
    directories remain immutable as history and do not count as approved
    decisions.


11. Make `workflow_contract.schema_version=2` the minimum and the default when creating cases: the engine refuses `create` with a lower or missing schema. The 'historical' classification of item 8 applies only to cases whose `CASE_CREATED` was already in the ledger before this contract was adopted; a case created afterwards without `decision_sha256` in the terminal event fails closed in `verify`. Cover it with a negative `create` test with schema 1.
#### Executed evidence

- Focused engine, mediation and compliance tests: `58 passed`.
- Full suite: `266 passed, 1 skipped`.
- An adversarial test changes bytes of `DECISION.md` after finalization and confirms that
  `verify_case_integrity` returns invalid for lack of a compatible binding.
- Existing tests cover create-only collision, concurrent lock, ledger
  tampering, wrong nonce, ratification, idempotent promotion and ADR modification.
- The real promotion of ADR-053 demonstrated the create-only path and the binding in the
  ledger; the canonical content was not rewritten by an LLM scribe.

#### Reasonableness and frugality

- The new control adds fields to an existing event and a local hash
  check; it creates no service, database, remote signature or daemon.
- Applicability is prospective by contract field, not by date or an ambiguous
  ADR number range.
- `provider_response_id` is not a universal requirement because not every runner
  provides it. Its absence stays explicit in the receipt, without fictitiously invalidating a
  real run whose command and artifacts are preserved.
- Promotion to an ADR happens only for decisions that really need to become
  normative in the repository.

#### 2. Explicit Non-Goals (Via Negativa / Out of Scope)

- Do not create a blockchain, in-house PKI, governance gateway, new database or special
  CI for the Round Table.
- Do not promise cryptographic proof of a vendor's internal identity when the
  runner does not expose it.
- Do not require a fictitious or unavailable `provider_response_id`.
- Do not migrate, reclassify or rerun every historical ADR and case en masse.
- Do not make every Round Table answer a decision that requires ratification.
- Do not allow writing official artifacts directly as a shortcut.
- Do not change the number of chairs, quorum, independence model or maximum number of
  rounds in this decision.
- Do not use an LLM mediator or scribe in this run.
- Do not commit or push.

#### Falsifiers requested

Vote `REVISE` or `REJECT` only with a reproducible counterexample: tampering with
`DECISION.md` accepted for a schema 2 case; a terminal event without a binding to the tally;
two finalizations accepted; an ADR promoted without an intact decision or mandatory
ratification; an official artifact written directly and accepted by the verifier; or a
control that imposes disproportionate operational cost. Threat model limitations
already declared are not blockers.

## 3. Outcome of the deliberation

- **GOOGLE** via `chair-google-cli`: `APPROVE` (confidence `0.98`). Version v002 of the proposal closes the Round Table's integrity guarantees robustly and elegantly, adding strict enforcement of schema_version=2 at case creation to prevent applicability bypass. The proposed chain of custody proposal→tally→decision→ratification→ADR with SHA-256 bindings and fail-closed verification neutralizes synthesized-consensus vectors without introducing rubber-stamp overhead. The threat model is honest and pragmatically bounded, fully in line with the principles of frugality and technical reasonableness.
- **ANTHROPIC** via `chair-anthropic-cli`: `APPROVE` (confidence `0.86`). v002 closes the weak link pointed out in round 1: item 11 makes schema 2 the minimum and default at create, with a negative test, removing the opt-out by choice of field. The chain proposal→votes→tally→decision→ratification→ADR is locally verifiable at marginal cost (extra fields in an existing event + hash recomputation), and the threat model honestly states what hashes do not cover. I found no reproducible counterexample within the in-scope range; the only residue concerns operationalizing item 11 and is non-blocking.
- **OPENAI** via `chair-glm-openrouter`: `APPROVE` (confidence `0.87`). v002 incorporates the blocker identified in the previous round: item 11 makes schema 2 the minimum and default at `create`, with explicit refusal of a lower or missing schema, a 'historical' classification restricted to cases already in the ledger before adoption, and a negative test covering the regression. I tried to falsify the decision-tally-ledger-ADR chain through the requested vectors — tampering with DECISION.md in a schema 2 case, a terminal event orphaned from the tally, double finalization, promotion without ratification and a direct write accepted by the verifier — and found no reproducible counterexample within the declared threat model. The proposal stays frugal: additional fields in an existing event, local hash verification and fail-closed, with no new service, database or PKI.

## 4. Recorded reservations

- `ISS-ANTHROPIC-R2-01` — **non-blocking**: Item 11 defines 'historical' by temporal order ('CASE_CREATED was already in the ledger before adoption'), but does not say how verify determines that boundary. Ledgers are per case; without a marker in CASE_CREATED itself, verify needs a timestamp or an external list, both fragile.
- `ISS-OPENAI-R2-01` — **non-blocking**: The cut-off point between 'historical' and new cases is defined in prose ('cases whose CASE_CREATED was already in the ledger before this contract was adopted'), without a verifiable mechanical anchor — for example, the ledger height or the hash of the promotion event of the ADR that makes this contract official — recorded in the ledger itself.

## 5. Traceability and authority

- Semantic authority remains with the case's immutable `DECISION.md`.
- For self-referential cases, promotion requires a valid `HUMAN_RATIFICATION.json`.
- The hash of this ADR is recorded in the chained ledger as `ADR_PROMOTED`.
- Future changes require a new superseding ADR; this file is create-only.
