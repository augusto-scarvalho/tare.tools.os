# ADR-054: Official Round Table Structure and GLM 5.3

- **Status:** RATIFIED / CANONICAL_SSOT
- **Date:** 2026-08-23
- **Round Table Case:** `CASE-2026-08-21-ROUND-TABLE-ROSTER-CROSS-BIAS-AUDIT-V16`
- **Proposal Version:** `v001`
- **proposal_sha256:** `7bd0ffa975b4a39d05c628fe67b363666d43deaa017f4c20ddfc35319235d600`
- **round_table_sha256:** `d45a85efb7bfdfc161f662f07ecfc55cabb4617dbfec0327a50dde35cf833476`
- **human_ratification_sha256:** `becb1d0e4726059cdd7bdb64c66bd17b356c91c52164caa7f07e0bce361e9ef2`
- **roster_sha256:** `ec84b6581710c47668f57558556e4b4ae6a72a05cfa7d4000d84dd3da0f859c7`

---

## 1. Context

This ADR is the deterministic promotion of the intact, approved Round Table decision named above. The decision text was not reinterpreted by another model during promotion; it only received this traceability envelope so that it stays discoverable and verifiable in the repository.

## 2. Ratified decision

### V16 — successor audit of the official Round Table structure

#### Decision requested

Vote `APPROVE`, `REVISE` or `REJECT` on the current Round Table structure,
considering real lineage independence, operational viability,
frugality, decision promotion and the replacement of the discontinued GLM 5.2.

#### 1. Core Goals (In-Scope)

1. Keep exactly three logical chairs and one valid vote per chair. An
   alternative transport or another instance of the same model never creates an
   additional vote, quorum or independence.
2. Use `bias_domain` per model lineage as the independence rule.
   Provider, API, CLI and effort are operational attributes, not new opinions.
3. Keep the JSON roster as the single source of models, deployments, priorities,
   bias domains, quota and endpoint, with a generated human projection and
   mechanized validation.
4. Preserve the fixed chairs Fable 5, GPT-5.6 Sol and Gemini 3.7, but manually
   exclude `openai/gpt` from this audit because it is the domain that authored
   the work. The corresponding chair is taken by GLM 5.3, of domain
   `zai/glm-family`, through OpenRouter.
5. Fully replace the discontinued GLM 5.2 with GLM 5.3. It becomes a paid
   chair fallback after Grok and Kimi and before the local options, and a paid
   mediation fallback after local Qwen. It is not a scribe and may not
   mediate a case in which it voted.
6. Treat all repeated OpenRouter aliases as 1:1 fallbacks. They
   share credit and endpoint circuits; a global failure avoids redundant paid
   calls in the same round.
7. Make the external attestation policy explicit: chair votes are
   admitted only with the real status in the receipt; `unattested` and
   `request_pinned_unverified` may never be described as full attestation.
   A served-model mismatch fails closed. An external LLM mediator may only be
   tried on operator opt-in.
8. Use deterministic mediation compilation and scribe narrative by
   default. LLM mediator and LLM scribe remain opt-in. The absence of an
   independent mediator results in `HELD_UNAVAILABLE` or human review, never
   fabricated consensus.
9. Do not require an artificial minimum number of vendors from every user. The table
   tries the available independent composition; if it cannot reach the three chairs,
   it reports unavailability in a verifiable and resumable way.
10. Require a single final human ratification for self-referential decisions.
    Ordinary Round Table answers do not require repeated ratification.
11. Promote a ratified decision to an ADR only through an explicit,
    create-only and idempotent command. The ADR references the hashes of the
    proposal, decision and ratification and records its own hash in the ledger.
    There is no implicit promotion of every answer and no mutation of the
    immutable `DECISION.md`.
12. Forbid resuming cases whose frozen contract diverged from the current roster.
    V13, V14 and V15 remain historical evidence; corrections are audited in this
    new successor case.

#### Executed evidence

- Roster validation: `ROUND_TABLE_ROSTER = VALID`.
- Focused roster, runtime, engine, mediation and compliance tests: `107 passed`.
- Full suite: `265 passed, 1 skipped`.
- The authenticated OpenRouter catalog exposed `z-ai/glm-5.3` with a context of
  1,048,576 tokens and support for reasoning, tools and structured output.
- The direct canary returned `GLM53_OPENROUTER_OK`.
- The end-to-end Aider/OpenRouter canary returned `GLM53_AIDER_OK`, reported
  `openrouter/z-ai/glm-5.3`, received status
  `request_pinned_unverified` and cost US$ 0.00088.
- The OpenRouter benchmarks endpoint had not yet published a row for GLM 5.3;
  therefore no comparative result was invented.
- Explicit promotion was exercised on the ratified OpenRouter case and produced
  `docs/adr/ADR-053_ROUND_TABLE_OPENROUTER_FALLBACK_GOVERNANCE.md`, with a
  cryptographic link in the ledger.

#### Reasonableness and frugality policy

- A fallback only runs after a failure; it does not raise the normal cost.
- Deterministic mediation and writing are the default zero-cost path.
- GLM 5.3 takes a place previously reserved for GLM 5.2; it does not create a new role.
- The roster does not synchronize catalogs or benchmarks automatically.
- Promotion to an ADR is an explicit per-decision action, not per-message bureaucracy.
- Historical cases are not rerun out of inertia: they only get a successor when
  a material, unabsorbed decision still exists.

#### 2. Explicit Non-Goals (Via Negativa / Out of Scope)

- Do not increase the number of chairs, votes, rounds or ratifications.
- Do not count the same model at different vendors as independent.
- Do not make OpenRouter, AWS, a paid API or a local model a universal prerequisite.
- Do not promote GLM 5.3 on marketing or a nonexistent benchmark; qualify it only
  by real access and by the explicitly authorized fallback position.
- Do not create a daemon, catalog synchronizer, autonomous ranking, dashboard or new
  provider abstraction layer.
- Do not yet automate discovery of the author domain; in this audit the exclusion
  is explicit, verifiable and limited to the process.
- Do not reopen V13–V15 with a derived contract nor change their historical artifacts.
- Do not use an LLM mediator or scribe in this run.
- Do not commit, push or publish externally.

#### Falsifiers requested

Issue `REVISE` or `REJECT` only in the face of a concrete falsifier, for example:
two chairs with the same `bias_domain`; a fallback creating a fourth vote; GLM 5.3
mediating its own vote; a receipt calling a request pin full attestation;
an accepted model mismatch; a total absence of chairs producing approval;
ADR promotion without an approved decision/required ratification; or the need for an LLM
on the default path. Recommendations without a falsifier must be non-blocking.

## 3. Outcome of the deliberation

- **GOOGLE** via `chair-google-cli`: `APPROVE` (confidence `0.98`). The V16 proposal establishes robust, frugal and logically consistent governance for the Round Table. Formalizing `bias_domain` per lineage, the pragmatic replacement of GLM 5.2 by GLM 5.3 and keeping deterministic zero-cost paths preserve integrity and the fail-closed principle without incurring operational hypertrophy.
- **OPENAI** via `chair-glm-openrouter`: `APPROVE` (confidence `0.86`). The V16 proposal replaces the discontinued GLM 5.2 with GLM 5.3 frugally and coherently, preserving the three logical chairs, independence by bias_domain lineage and fail-closed behavior. The external attestation policy, with an explicit distinction between real status and full attestation, combined with deterministic mediation/scribe by default, avoids unnecessary cost and fabricated consensus. The via negativa is well bounded and the requested falsifiers are concrete and verifiable, with no sign of technical hypertrophy or rubber-stamp bureaucracy.
- **ANTHROPIC** via `chair-anthropic-cli`: `APPROVE` (confidence `0.82`). The proposal is frugal and coherent: three fixed chairs, independence by bias_domain, 1:1 fallbacks without an extra vote, deterministic mediation/scribe by default and explicit, create-only ADR promotion. The replacement of GLM 5.2 by GLM 5.3 is qualified by real access (canaries, measured cost) and not by an invented benchmark. I found no concrete falsifier against the core goals; the remaining gaps are about textual precision, not regression, and are therefore non-blocking.

## 4. Recorded reservations

- `ISS-OPENAI-R1-01` — **non-blocking**: The qualification of GLM 5.3 rests only on real access and functional canaries, without a published comparative benchmark, which is honest but leaves the paid fallback position with limited performance evidence.
- `ISS-OPENAI-R1-02` — **non-blocking**: The manual exclusion of the author domain (openai/gpt) is verifiable, but depends on operator discipline in every successor audit, with no mechanized safeguard against omission.
- `ISS-ANTHROPIC-R1-01` — **non-blocking**: Goal 7 says chair votes are admitted 'only with the real status in the receipt' and that a model mismatch fails closed, but does not state whether a vote with `request_pinned_unverified` (exactly the status obtained in the GLM 5.3 canary through OpenRouter) COUNTS as a valid chair vote. With that status, a served-model mismatch is not detectable, so 'fails closed on mismatch' is empty for that route.
- `ISS-ANTHROPIC-R1-02` — **non-blocking**: In this very case, GLM 5.3 holds a voting chair and is at the same time the only paid mediation fallback after local Qwen. If local Qwen is unavailable, the rule 'may not mediate a case in which it voted' forces HELD_UNAVAILABLE by construction — correct behavior, but the proposal does not state that this outcome is expected and accepted for V16.
- `ISS-ANTHROPIC-R1-03` — **non-blocking**: Goal 6 opens a 'global circuit' on the failure of one OpenRouter alias. A model-specific failure (e.g. 404/model removed or a per-model 429) does not imply an endpoint/credit failure; the global circuit may suppress a fallback that would work in the same round.
- `ISS-ANTHROPIC-R1-04` — **non-blocking**: Goal 10 uses 'self-referential decisions' without a mechanical classification criterion. Without a definition, either everything becomes self-referential (repeated ratification, the bureaucracy the proposal wants to avoid) or nothing does (roster change without ratification).

## 5. Traceability and authority

- Semantic authority remains with the case's immutable `DECISION.md`.
- For self-referential cases, promotion requires a valid `HUMAN_RATIFICATION.json`.
- The hash of this ADR is recorded in the chained ledger as `ADR_PROMOTED`.
- Future changes require a new superseding ADR; this file is create-only.
