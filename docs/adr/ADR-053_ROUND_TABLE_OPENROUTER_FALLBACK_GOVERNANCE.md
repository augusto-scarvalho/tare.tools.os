# ADR-053: Governance of Round Table OpenRouter Fallbacks

- **Status:** RATIFIED / CANONICAL_SSOT
- **Date:** 2026-08-23
- **Round Table Case:** `CASE-2026-08-23-OPENROUTER-FALLBACK-AMENDMENT`
- **Proposal Version:** `v001`
- **proposal_sha256:** `1048675d0154ebd42eda0ab23b9e6b6b37caae3a0c3364ec1f0e899a9231194b`
- **round_table_sha256:** `d33d78fbd658c0ec67845d35cc3ce56316cc247794f1f279cce5c3f9447c8027`
- **human_ratification_sha256:** `5679309c0653f5a580367b479fb0f83cc670adcfad0feecd799078809fc4b558`
- **roster_sha256:** `4a5c86d20fab1f089e93094c97ac2823f5bab9f1c0157c51817268d58e0bfada`

---

## 1. Context

This ADR is the deterministic promotion of the intact, approved Round Table decision named above. The decision text was not reinterpreted by another model during promotion; it only received this traceability envelope so that it stays discoverable and verifiable in the repository.

## 2. Ratified decision

### Minimal amendment: OpenRouter as the Round Table's transport fallback

#### 1. Core Goals (In-Scope)

Deliberate whether the Round Table should keep the following minimal amendment to its official structure:

1. Add five OpenRouter deployments to the canonical roster, without creating chairs, votes, mediators, scribes or bias domains: Fable 5 (`anthropic/claude-fable-5`), GPT-5.6 Sol (`openai/gpt-5.6-sol`), Gemini 3.7 Flash (`google/gemini-3.7-flash`), Grok 4.6 (`x-ai/grok-4.6`) and Kimi K3 (`moonshotai/kimi-k3`).
2. Treat Fable, GPT and Gemini through OpenRouter strictly as 1:1 transport alternatives for the chairs of the same lineage. Transport, API, CLI and effort create neither independence nor an additional vote.
3. Treat Grok and Kimi through OpenRouter as quota alternatives for the same lineages already registered through Bedrock or CLI, also without additional independence.
4. Preserve the three logical chairs and the current quorum. In any run, a chair has at most one valid vote, even if multiple deployments were tried.
5. Resolve `OPENROUTER_API_KEY` from the environment or the existing keyring and inject it only into the child process environment. Receipts and artifacts do not serialize the credential.
6. Require an exact `openrouter/<vendor>/<model>` pin and the exact Aider banner. A missing or divergent reported model fails closed; there is no automatic `fallback-model`.
7. Share `quota_domain=openrouter-credits` and `endpoint_domain=openrouter-api` across the five deployments. Exhausted credit or an unavailable endpoint opens a single circuit and prevents redundant calls to the other aliases in the same round.
8. Use the existing headless Aider runner, with no browser, analytics, update checks or interactive prompts. The amendment only adds the required runner, credential, preflight and attestation mapping.
9. Keep mediation and scribe deterministic by default. An LLM mediator or scribe remains opt-in and is not needed for this deliberation.
10. Stay fail-closed: if no independent composition is available, the case must stay `HELD_UNAVAILABLE`, never fabricate consensus.
11. Require human ratification for this self-referential decision before making it canonical.

##### Order and operational frugality

- Fallbacks are only tried after the fixed chair fails; a normal run does not consume OpenRouter.
- To preserve the chair's lineage before switching bias domain: Fable tries Bedrock, the Anthropic API and OpenRouter; GPT tries Bedrock and OpenRouter; Gemini has OpenRouter.
- Next come the already authorized frontier alternatives: Grok through Bedrock/OpenRouter and Kimi through CLI/OpenRouter; existing local or free options remain available further down.
- Fable through OpenRouter remains a contingency, not a preferred route.
- The shared circuit avoids five useless charges when the OpenRouter account or endpoint is unavailable.

#### 1.2 Explicit Non-Goals (Via Negativa / Out of Scope)

- Do not create a chair, vote, quorum, mediator, scribe, bias domain or governance category.
- Do not register DeepSeek, Qwen through OpenRouter, Muse, Nova or any other model in this amendment.
- Do not create autonomous ranking, a catalog synchronizer, a daemon, a new provider framework, a new configuration layer or automatic model discovery.
- Do not promote models repeated across other vendors to independent evidence.
- Do not change the default behavior for LLM mediation or writing.
- Do not implement automatic discovery of the author's domain in this amendment; in this audit, `openai/gpt` is excluded manually.
- Do not change case V16, earlier decisions, history, local weights, the maximum number of rounds or the ratification rules.
- Do not turn occasional vendor availability into a minimum execution obligation for every user.
- Do not commit, push or publish as part of this decision.

#### 2. Implementation and evidence

##### Minimal change

- The SSOT roster received five deployments and five `aider-openrouter` runtime aliases.
- The validator recognizes the runner and rejects any OpenRouter deployment without the exact `openrouter/` prefix.
- The engine reuses the same headless Aider path, adding only key resolution, preflight and the pin receipt.
- `OPENROUTER_API_KEY` was registered in the masked key inventory.
- No OpenRouter deployment was added to the mediator or scribe lists.

##### Evidence run before this deliberation

- `ROUND_TABLE_ROSTER = VALID`.
- Focused roster, runtime, engine, compliance and mediation tests: `107 passed`.
- Full repository suite: `265 passed, 1 skipped`.
- Direct canaries answered for all five models now registered on OpenRouter.
- The end-to-end Aider/OpenRouter canary answered `AIDER_OPENROUTER_OK` with the pinned Gemini model, without opening a browser.
- The tests cover: pinned commands without automatic fallback; key through environment/keyring; failure before the runner without a key; secret absent from the receipt; exact banner; missing banner failing closed; bias domains preserved; shared quota/endpoint circuit.

#### 3. Falsification & Traceability Matrix

| Invariant | Objective falsifier | Evidence/control |
|---|---|---|
| OpenRouter does not create independence | The same model through two transports takes two votes | `bias_domain` stays per lineage; the fallback replaces a chair 1:1 |
| Exact pin | The provider serves a different alias or omits the banner | `SERVED_MODEL_MISMATCH` or `MODEL_ATTESTATION_MISSING` |
| The credential does not leak | The key appears in a receipt, canonical log or state | A test serializes the receipt and searches for the secret; injection only in the child env |
| Frugality | A credit failure triggers several OpenRouter calls in the round | One shared `quota_domain` and one shared `endpoint_domain` open the circuit |
| Viability | A user without OpenRouter is prevented from using available chairs | OpenRouter is a fallback; a missing key is classified as unavailability and routes onward |
| No hypertrophy | The amendment requires a service, daemon, new role or framework | Diff limited to the roster, runner/preflight, key registration and tests |
| No fictitious consensus | Every option fails and the case is approved | Mandatory state `HELD_UNAVAILABLE` |
| Self-reference | The table canonizes its own rule without a human | Maximum outcome `APPROVED_PENDING_HUMAN_RATIFICATION` |

#### 4. Decision requested

Vote `APPROVE` only if the amendment is small, reasonable, frugal, operationally viable and does not weaken existing guarantees. Vote `REVISE` or `REJECT` only with a concrete falsifier tied to the core goals. Out-of-scope recommendations may be recorded as non-blocking, but must not inflate this delivery.

## 3. Outcome of the deliberation

- **GOOGLE** via `chair-google-cli`: `APPROVE` (confidence `0.98`). The minimal amendment supporting OpenRouter as a fallback transport is technically consistent, frugal and strictly bounded. The proposal preserves the Round Table's independence invariant by treating the deployments as a 1:1 contingency without inflating votes or changing bias domains. The shared circuit breaker and strict banner validation provide operational protection and fail-closed behavior with broad test coverage.
- **ANTHROPIC** via `chair-anthropic-cli`: `APPROVE` (confidence `0.84`). The amendment is genuinely minimal: five deployments, an existing runner, a pinned prefix and a shared circuit, with no new chair, role or framework. The invariants that matter (1 chair = 1 vote, fail-closed on pin/banner, credential only in the child env, HELD_UNAVAILABLE, human ratification) have objective falsifiers and executed evidence (107 focused + 265 full suite + canaries). The residual risks are operational and about attestation fragility, not governance, and none regresses the core goals; users without OpenRouter keep operating because it is a fallback, not a prerequisite.
- **OPENAI** via `chair-xai-openrouter`: `APPROVE` (confidence `0.83`). The amendment is small, reasonable and aligned with frugality: OpenRouter comes in only as a 1:1 transport/quota, with no new chair, vote or ritual, and only after the fixed route fails. The invariants of one vote per chair, exact pin, fail-closed, shared circuit and human ratification cover the self-referential risk and the fictitious-consensus risk. Viability for those with few providers is preserved because a missing key is classified as unavailability and moves on, without making OpenRouter a minimum obligation. The only loose point is the granularity of the shared circuit, but the text already limits the trip to exhausted credit or an unavailable endpoint, which does not justify blocking the delivery.

## 4. Recorded reservations

- `ISS-ANTHROPIC-R1-01` — **non-blocking**: The shared circuit does not distinguish credit/endpoint exhaustion (legitimately global) from per-model rate limits or unavailability (local to one alias). A model-specific 429 may be classified as quota_domain and block the other four aliases in the round, reducing availability with no frugality gain.
- `ISS-ANTHROPIC-R1-02` — **non-blocking**: Attestation depends on the exact Aider banner, which is a CLI version detail, not a contract. An Aider upgrade that changes the banner format turns all five fallbacks into MODEL_ATTESTATION_MISSING at once — correct fail-closed behavior, but a silent fragility that only shows up when the fallback is actually needed.
- `ISS-ANTHROPIC-R1-03` — **non-blocking**: The end-to-end Aider/OpenRouter evidence covers only the Gemini pin; the other four models have only direct canaries. The runner + banner + receipt path for Fable/GPT/Grok/Kimi through OpenRouter is validated by fixture tests, not by a real run.
- `ISS-OPENAI-R1-01` — **non-blocking**: The shared openrouter-api domain is well motivated for credit and endpoint, but the specification does not state unequivocally that the failure of one alias (missing model, 404, one-off refusal of the slug) does not open the circuit for the others.

## 5. Traceability and authority

- Semantic authority remains with the case's immutable `DECISION.md`.
- For self-referential cases, promotion requires a valid `HUMAN_RATIFICATION.json`.
- The hash of this ADR is recorded in the chained ledger as `ADR_PROMOTED`.
- Future changes require a new superseding ADR; this file is create-only.
