# ADR-058 — Proportional Delivery Evidence and Behavior-Relevant Coverage

- **Status:** RATIFIED / CANONICAL_SSOT
- **Date:** 2026-08-26
- **Round Table Case:** `CASE-2026-08-26-ECOSYSTEM-DELIVERY-EVIDENCE-POLICY`
- **Proposal Version:** `v003`
- **proposal_sha256:** `a58d1c649e507cd7738b8800dc90370258c1392b7e38ffbb66b5ddaf0de4959a`
- **round_table_sha256:** `4f0d848635302b523df7f6a3f90cc43dc15c1a3bbc314a07ebaaa8205f7a099b`
- **human_ratification_sha256:** `not-required`
- **roster_sha256:** `48e69872149f993f9ebe41967bc115374fe3a380980dca588be4e3bc13d68793`

---

## 1. Context

This ADR is the deterministic promotion of the intact, approved Round Table decision named above. The decision text was not reinterpreted by another model during promotion; it only received this traceability envelope so that it stays discoverable and verifiable in the repository.

## 2. Ratified decision

### HUMAN REVISION CANDIDATE v003 — proportional delivery evidence policy

- **Case:** `CASE-2026-08-26-ECOSYSTEM-DELIVERY-EVIDENCE-POLICY`
- **Date:** 2026-08-26
- **Base version:** `v002`
- **Base SHA-256:** `452d5d4a7d87c5d4ce1e43149c0b0b1271c6c30fad64ad02f034f17a2a60f746`
- **Class:** cross-ecosystem delivery quality and admission governance
- **Author bias domain to exclude:** `openai/gpt`
- **Profile:** `generalist`
- **Self-referential:** `false`

#### 1. Core Goals (In-Scope)

1. Define a vendor-neutral, repository-aware policy for the evidence required
   before a software change may be described as validated, admitted, shipped,
   or observed across the `tare.tools` ecosystem.
2. Require executable falsifiers for changed behavior while avoiding a single
   ecosystem-wide line-coverage percentage or a mandate to kill every mutant.
3. Scale test depth, coverage evidence, mutation analysis, platform testing,
   and delivery receipts according to bounded change risk.
4. Preserve explicit distinctions between local validation, CI validation,
   admission, publication/deployment, and post-delivery observation.
5. Define honest treatment of surviving mutants as missing coverage,
   equivalent mutation, out-of-scope platform behavior, or infrastructure
   failure; `ERROR` and `TIMEOUT` never count as killed.
6. Require mutation runners to be hermetic, bounded, source-integrity checked,
   and safe against process-tree or platform-specific side effects.
7. Keep pull-request gates fast through curated canaries while routing broader
   campaigns to bounded manual or scheduled execution.
8. Define a narrow, auditable exception path for urgent delivery without
   laundering incomplete evidence into `PASS` or `APPROVED`.
9. Produce a policy shape that each repository can instantiate without a new
   central service, daemon, database, or universal test framework.

#### 1.2 Explicit Non-Goals (Via Negativa / Out of Scope)

1. Do not require 100% line, branch, path, or mutation coverage everywhere.
2. Do not equate a coverage percentage with correctness or delivery readiness.
3. Do not require mutation testing for documentation-only, metadata-only, or
   other changes that cannot alter executable behavior.
4. Do not force identical thresholds, commands, frameworks, or CI durations on
   every repository and language.
5. Do not fabricate tests to kill equivalent mutants or hide survivors by
   excluding them without a recorded classification.
6. Do not make broad mutation campaigns part of every PR when stable canaries
   can protect the same critical contracts at lower recurring cost.
7. Do not create a central quality database, coverage SaaS dependency, daemon,
   policy server, plugin system, or new orchestration runtime.
8. Do not modify Round Table composition, quorum, mediation, ratification, or
   `bias_domain` governance in this case.
9. Do not retroactively relabel historical deliveries or block emergency
   recovery when the exception receipt states the missing evidence and risk.
10. Do not authorize implementation across all repositories merely by approving
    this policy; adoption remains staged and repository-specific.

#### 2. Context and observed evidence

The recent `tare.tools.backlog-graph` qualification provides a bounded example,
not a universal threshold:

- `grounding.py`: 34 mutants, 32 killed, 2 classified equivalent, 94.1%;
- `mutations.py`: 36 mutants, 36 killed, 100%;
- `jsonutil.py`: 38 mutants, 35 killed, 3 classified equivalent, 92.1%;
- 41 curated PR canaries protect selected critical contracts;
- the local canary gate completed in 43.95 seconds and GitHub CI completed in
  37 seconds;
- 422 production mutants remain intentionally unqualified rather than being
  converted into an artificial ecosystem-wide completion target;
- a platform-guard mutant exercised `os.kill(..., 0)` on Windows and twice
  terminated the surrounding CLI flow before the falsifier was made safe and
  deterministic.

This evidence supports proportional gates and runner containment. It does not
prove that the same counts, scores, or time budgets fit another repository.

#### 2.1 Proposed risk classes

Every delivery declares one evidence class. A repository may refine names but
must preserve the ordering and minimum semantics.

| Class | Typical change | Minimum evidence |
|---|---|---|
| `E0_NON_EXECUTABLE` | prose, comments, generated index with no runtime effect | structural/lint check appropriate to the artifact; exact diff review |
| `E1_LOCAL_BEHAVIOR` | bounded helper, adapter, UI behavior, non-authoritative projection | focused positive and negative tests; changed contract directly asserted |
| `E2_OPERATIONAL` | persistence, parsing, graph mutation, reconciliation, non-authoritative CI configuration | E1 plus integration/regression evidence, failure-path falsifiers, coverage ratchet for touched scope, selected mutation analysis of the changed contract |
| `E3_CRITICAL` | authority, admission, security boundary, canonical identity/evidence, settlement, recovery, irreversible publication | E2 plus independent audit, platform/concurrency evidence or a concrete `N/A` rationale, critical mutation canaries on the changed contract, explicit rollback/recovery receipt |

Classification integrity is part of admission, not an author preference:

1. The effective class is the maximum class implied by every surface in the
   exact diff. A mixed change containing any `E3` surface is `E3`; a change
   containing an `E2` surface and no `E3` surface is at least `E2`.
2. Each adopting repository maintains a versioned, repository-local manifest
   mapping paths, modules, or contracts to a minimum evidence class. Authority,
   admission, security boundaries, canonical identity/evidence, settlement,
   recovery, and irreversible publication have an `E3` floor.
3. The declared and effective classes are recorded before gate execution. The
   admission gate computes `max(declared class, all applicable manifest floors)`.
4. A declaration below the effective class is `HELD` until reclassified or
   resolved through the explicit exception path. Risk cannot be lowered merely
   to bypass a failing gate; lowering requires new evidence that the higher-risk
   surface was not touched or a recorded exception.
5. A higher class may be chosen voluntarily; evidence requirements remain
   monotonic. This repository-local mechanism creates no central service.
6. The classification manifest is itself an `E3` admission surface. The gate
   evaluates floors against the manifest version at the candidate's parent or
   merge base. Removing an entry or lowering a floor is an `E3` delivery that
   requires independent-lineage review and a concrete rationale; the lowered
   floor cannot authorize any other surface in the same candidate.
7. Named `E3` surface types retain their policy floor even when omitted or
   remapped in the candidate manifest. An unmapped executable path is at least
   `E1`; when it participates in an `E2` or `E3` contract, it inherits that
   contract's floor until evidence demonstrates otherwise. Each repository may
   prove this with its own dependency evidence or review receipt; this policy
   mandates no universal graph analyzer.

#### 2.2 Baseline evidence contract

For every executable delivery:

1. state the changed contract and its failure modes;
2. run a passing baseline before mutation or adversarial qualification;
3. add deterministic falsifiers for material new behavior and repaired defects;
4. record exact commands, exit status, test counts, skips, platform, and commit;
5. distinguish targeted results from repository-wide results;
6. preserve sources and prove integrity when a test tool rewrites or mutates;
7. keep `ERROR`, `TIMEOUT`, unavailable runners, and unverified external
   execution separate from successful validation;
8. record CI evidence separately from local evidence;
9. never claim deployment or observation from a commit/push receipt alone.

#### 2.3 Coverage and mutation policy

Coverage is a diagnostic and ratchet, not a standalone acceptance oracle. The
policy prefers a smaller set of tests that challenge relevant behavior over
low-value tests written only to raise a numeric coverage score.

1. New or changed executable behavior must be reached by at least one direct
   assertion and one relevant failure-path falsifier when a failure path exists.
2. Repositories may set numeric line/branch floors, but a delivery cannot satisfy
   this policy solely by meeting a percentage.
3. Touched critical modules must not lose previously enforced coverage without
   an explicit exception receipt.
4. `E2` and `E3` changes use risk-selected mutants or equivalent adversarial
   transformations for the changed contract. Full-file campaigns are required
   only when their value and bounded cost are justified.
5. A surviving mutant must be classified with target, mutation, tests used, and
   reasoning. Equivalent status requires a concrete control-flow or semantic
   argument; it is not a generic waiver. For `E3`, the equivalence classification
   requires review by a lineage independent from the implementer or remains
   `HELD`. For `E2`, scheduled report review includes a documented spot-check of
   equivalence classifications.
6. Curated PR canaries protect stable critical contracts. Broader campaigns run
   manually or on schedule and publish a non-blocking report until reviewed.
7. Mutation runners execute against a shadow copy, enforce per-run timeout,
   capture infrastructure errors, and verify production-source hashes.
8. Platform-sensitive mutants must use deterministic fakes or isolated process
   boundaries; a mutant may not signal, terminate, overwrite, or delete outside
   its declared shadow workspace.
9. An `E3` delivery records platform/concurrency evidence for the changed
   contract or a concrete `N/A` rationale explaining why no interleaving,
   signal, shared I/O, or process boundary applies. A generic `N/A` is not
   sufficient.

#### 2.3.1 Ecosystem-assisted coverage selection

Existing ecosystem tools may inform which behavior deserves evidence without
becoming admission authorities:

1. Backlog Graph may provide the bounded work selection, criticality,
   evidence requirements, repositories, paths, symbols, and grounding
   references associated with the delivery.
2. SpecGraph may project the linked requirements, specifications, decisions,
   code symbols, tests, dependency impact, and stale relationships for that
   selection.
3. Execution coverage answers which code ran; traceability answers which
   promises are linked to code and tests; mutation analysis challenges whether
   relevant tests notice wrong behavior; delivery receipts record what was
   actually validated. These dimensions remain separate and may not be hidden
   inside one composite percentage.
4. The combined view guides risk-selected tests and mutants toward changed
   contracts, material failure modes, and critical dependency edges. It does
   not require full-file mutation or universal 100% coverage.
5. Missing, stale, ambiguous, or conflicting graph links are visible evidence
   gaps, never proof that a surface is irrelevant. Tool output cannot lower an
   evidence class, waive required review, or authorize admission.
6. Backlog Graph and SpecGraph remain independently usable. A pilot may exchange
   bounded repository/path/symbol/reference receipts; this policy requires no
   direct tool-to-tool call, central service, or universal dependency analyzer.

#### 2.4 Delivery state vocabulary

| State | Meaning |
|---|---|
| `PREPARED` | implementation or artifact exists; required evidence is incomplete |
| `VALIDATED_LOCAL` | declared local checks passed with receipts |
| `VALIDATED_CI` | required CI checks passed for the exact commit |
| `ADMITTED` | the repository's authority gate accepted the exact candidate |
| `SHIPPED` | exact artifact/ref was published or deployed to the declared destination |
| `OBSERVED` | declared post-delivery checks passed in the target environment |
| `HELD` | required evidence is unavailable, failed, stale, or inconclusive |

States are not inferred upward. In particular, `VALIDATED_CI` is not `SHIPPED`,
and `SHIPPED` is not `OBSERVED`.

`EXCEPTION` is an immutable overlay, not a mutually exclusive state and never
infers `ADMITTED`, `SHIPPED`, or `OBSERVED`. An exceptional delivery records the
overlay on the highest honestly reached state together with destination intent.
`SHIPPED+EXCEPTION` is allowed only when the exception receipt is complete, the
destination receipt exists, and repository authority accepts residual risk.
`OBSERVED` still requires its declared post-delivery checks. Outside the
emergency exception path, `SHIPPED` requires prior `ADMITTED` for the exact
candidate. An exceptional shipment may record the factual `SHIPPED` state but
cannot claim `ADMITTED` or satisfy missing `E3` review. Underlying
`PASS`/`FAIL`/`SKIP`/`ERROR`/`TIMEOUT` results remain immutable and visible.

#### 2.5 CI budget and exception policy

1. Each repository declares a PR critical-path budget based on its actual
   environment; the initial mutation-canary target is at most 45 seconds unless
   a repository records a different justified budget.
2. When the budget is exceeded, first reduce the canary set to non-redundant
   critical contracts or move broad campaigns out of the PR path; do not hide
   failures or weaken classification semantics.
3. An exception records owner, exact commit/artifact, missing evidence, reason,
   residual risk, containment, expiry or follow-up trigger, and whether rollback
   is possible.
4. Exceptions never rewrite failed or unavailable checks as passing and never
   satisfy an independent-audit requirement for `E3` changes.
5. An exception whose expiry passes without completed follow-up transitions to
   `HELD`; continuation requires a new, explicit renewal receipt. Renewal does
   not alter or erase the original results or receipt.

#### 2.6 Bounded adoption

1. **P0 — Decision only:** deliberate this contract and required revisions.
2. **P1 — Two-repository pilot:** after operator acceptance, instantiate the
   policy in `tare.tools.backlog-graph` and one contrasting repository without a
   central runtime or mass backlog creation. Evaluate whether existing Backlog
   Graph work scopes and SpecGraph trace/impact outputs improve selection of
   behavior-relevant tests and mutants while preserving visible unknowns.
3. **P2 — Evidence review:** compare CI latency, defect discovery, false blocks,
   equivalent-mutant rate, exception use, classification-floor reductions,
   and operator burden; verify that independent pilot reviews use a
   `bias_domain` distinct from the implementer.
4. **P3 — Successor decision:** only then decide ecosystem-wide defaults,
   machine-readable schemas, or integration with admission policy.

Approval of this proposal authorizes no P1 implementation by itself.

#### 3. Falsification & Traceability Matrix

| ID | Invariant/claim | Falsifier | Required acceptance evidence |
|---|---|---|---|
| `DEP-01` | Evidence scales with delivery risk | Mixed diff touching admission is declared `E1` and admitted | Effective-class gate takes maximum of declared class and all manifest floors |
| `DEP-02` | Percentages are not correctness | High line coverage passes while changed failure behavior is unasserted | Direct contract and failure-path test required independently of percentage |
| `DEP-03` | Non-executable work stays frugal | Documentation-only change triggers mutation campaign | Classification test routes it to `E0_NON_EXECUTABLE` |
| `DEP-04` | Mutation results are honest | `ERROR` or `TIMEOUT` increases killed count or score | Result-classification tests and zero-infrastructure-error gate |
| `DEP-05` | Equivalent mutants are explicit and reviewable | Survivor disappears or an `E3` author self-approves equivalence | Recorded rationale plus independent-lineage receipt for `E3` and spot-check for `E2` |
| `DEP-06` | Production sources remain intact | Campaign changes any tracked source byte | Before/after SHA-256 equality over declared source set |
| `DEP-07` | Mutants are contained | Mutant affects parent CLI, real worktree, sibling process, or external path | Shadow-root and process-isolation adversarial test |
| `DEP-08` | Platform hazards are bounded | Windows/POSIX branch invokes a real destructive signal during qualification | Deterministic fake or isolated subprocess proves no external effect |
| `DEP-09` | PR cost remains bounded | Curated canaries exceed repository budget without explicit decision | CI timing receipt and non-redundant canary review |
| `DEP-10` | Broad campaigns do not become vanity gates | All discovered mutants are placed on every PR without risk triage | Separate PR and scheduled/manual profiles |
| `DEP-11` | Delivery states remain distinct | Commit/push or exception is reported as deployed or observed | State-transition contract plus overlay semantics bound to exact receipts |
| `DEP-12` | CI evidence binds exact code | Successful run belongs to another SHA or stale branch | SHA/ref equality check before `VALIDATED_CI` |
| `DEP-13` | Skips are visible | Required check is skipped but aggregate is reported simply as pass | Receipt reports pass/fail/skip counts and required-skip policy |
| `DEP-14` | Exceptions do not launder or persist failure | Exception rewrites failure, infers observation, or silently outlives expiry | Immutable underlying result, separate overlay receipt, and expiry-to-`HELD` transition |
| `DEP-15` | Critical delivery gets independent review | Implementer lineage provides the only `E3` audit | Independent `bias_domain` receipt or `HELD` |
| `DEP-16` | Adoption remains bounded | Approval creates mass repository changes or a central service | P0 produces decision artifacts only |
| `DEP-17` | Repository diversity is preserved | One language/framework command becomes universal policy | Two-repository pilot uses shared semantics with local commands |
| `DEP-18` | Risk cannot be downgraded opportunistically | Declared class is below a touched-surface floor or changes after failure | Pre-gate class record, versioned manifest, classification history, and explicit exception requirement |
| `DEP-19` | Classification authority cannot be weakened in-band | Candidate lowers or omits a floor, or adds an unmapped helper used by an `E3` contract, and admits itself at the lower class | Gate uses the parent manifest; named `E3` floors remain fixed; connected executable paths inherit contract floors; a floor reduction cannot authorize the same candidate |
| `DEP-20` | Ecosystem graphs guide coverage without becoming authority | Missing or stale graph link is treated as proof of low risk, or a tool score admits a delivery | Separate coverage dimensions, visible unknowns, bounded cross-tool receipts, and independent admission evidence |

#### 4. Required Round Table decision

Each chair must return `APPROVE`, `REVISE`, or `REJECT` and answer:

1. Does the parent-manifest rule now prevent in-band weakening and opportunistic underclassification?
2. Are admission and authority correctly fixed at `E3`?
3. Are independent review and platform/concurrency requirements for `E3`
   specific enough without imposing universal infrastructure?
4. Does the `EXCEPTION` overlay prevent overclaiming while preserving an honest
   urgent-delivery path?
5. Are the three round-one blocking issues substantively resolved?
6. Do Backlog Graph work scopes and SpecGraph trace/impact evidence improve
   relevant coverage selection without creating a circular source of authority?
7. Which concrete falsifier, if any, still blocks a two-repository pilot?

A blocking objection must identify a `DEP-*` failure or propose one concrete,
reproducible falsifier. Requests for central infrastructure or universal 100%
coverage are outside scope unless an in-scope invariant is impossible without
them.


## 3. Outcome of the deliberation

- **OPENAI** via `chair-xai-openrouter`: `APPROVE` (confidence `0.88`). v003 closes the proportional evidence contract with an honest state vocabulary, E3 floors for authority/admission and adoption strictly limited to P0. The parent-manifest rule, class monotonicity and the EXCEPTION overlay prevent evidence laundering and in-band weakening without requiring a central service. The residual risk of new unmapped paths is a pilot instantiation risk, not a core failure of the policy; nothing blocks the deliberation or a two-repository pilot under local receipts.
- **GOOGLE** via `chair-google-cli`: `APPROVE` (confidence `0.98`). Proposal v003 reaches exemplary architectural and systems maturity by resolving the in-band weakening vectors elegantly and deterministically through evaluation against the parent manifest and inheritance of risk floors. The rigorous ontological separation between delivery states and the immutable EXCEPTION overlay removes the risk of evidence laundering without creating dependencies on central daemons or bureaucratic overhead. The strategy of fast curated canaries (<45s) and use of the Backlog Graph/SpecGraph as heuristic guides — not admission authorities — ensures high economic efficiency, execution safety and immediate viability for the P1 pilot.
- **ANTHROPIC** via `chair-anthropic-cli`: `APPROVE` (confidence `0.85`). v003 resolves the round-one blockers: the parent-manifest evaluation rule (2.1.6) plus fixed named E3 floors and contract-floor inheritance (2.1.7) close the in-band weakening and opportunistic underclassification paths that DEP-18/DEP-19 target, and the EXCEPTION overlay with immutable underlying results prevents laundering while keeping an honest emergency path. Admission, authority, and the manifest itself are correctly pinned at E3, and the ecosystem-graph integration (2.3.1) is properly advisory-only, with missing links treated as visible gaps rather than proof of low risk. The economics are sound: bounded canary budgets, scheduled broad campaigns, E0 frugality, and no central runtime. No remaining falsifier blocks a two-repository pilot; the residual gaps below are operational clarifications, not invariant failures.

## 4. Recorded reservations

- `ISS-OPENAI-R3-01` — **non-blocking**: The floor 'unmapped ⇒ at least E1' still leaves the operationalization of inheritance ambiguous when a new path takes part in an E2/E3 contract and the repository shows no dependency evidence. This does not authorize lowering existing floors, but it can delay detecting underclassification of a new surface if the pilot treats E1 as the final classification instead of a minimum floor.
- `ISS-ANTHROPIC-R3-01` — **non-blocking**: Section 2.5.5's expiry-to-HELD transition is operationally underspecified for an artifact that already reached SHIPPED+EXCEPTION. SHIPPED is a factual, irreversible state; when the exception expires without follow-up and rollback is impossible, the policy says the overlay becomes HELD but mandates no containment or re-acceptance action for the artifact that remains live at the destination.
- `ISS-ANTHROPIC-R3-02` — **non-blocking**: Two bootstrap ambiguities remain for P1: (a) a single-maintainer repository has no independent lineage for E3 review, so its E3 changes are structurally permanently HELD, since 2.5.4 correctly forbids exceptions from satisfying the audit requirement; (b) the initial classification manifest has no parent version to evaluate against, so the 2.1.6 parent-manifest rule is undefined for the first delivery that introduces it.

## 5. Traceability and authority

- Semantic authority remains with the case's immutable `DECISION.md`.
- For self-referential cases, promotion requires a valid `HUMAN_RATIFICATION.json`.
- The hash of this ADR is recorded in the chained ledger as `ADR_PROMOTED`.
- Future changes require a new superseding ADR; this file is create-only.
