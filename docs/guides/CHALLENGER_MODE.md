# Challenger mode

The operator closed FLOW-19's current scope with the approved
[Cheap / Intermediate / Smart implementer profiles](IMPLEMENTER_PROFILES.md).
This is an explicit policy decision, not a claim that the original AB8 campaign
was completed. Existing experiment results and limitations below remain historical;
the challenger capability stays optional and its default remains off.

`/challenger` (or **Challenger mode** in the command palette) configures automatic
experiments for a project. **Save** persists the toggle, sampling percentages,
variants and limits. Turning it off cooperatively cancels the active experiment.
The default is off. The earlier US$5 trial was not a recurring authorization.

Cancelling or interrupting the primary requests cooperative stop only for its
linked experiment, before publishing the reference that can release another
stage. Normal completion (`PROPOSED`) does not cancel it. Repeated notifications
are safe: they do not restart work or replace an existing reference, receipts,
results or patch. Sessions without a linked challenger do not start one on stop.
The worker checks the request before implementation and each paired audit, and
keeps monitoring during execution. This is a stop request, not instant termination
of a call already in flight; it does not kill other jobs' processes.

Reference snapshots, receipts and results already produced remain retained,
including when stop arrives after a result was recorded. A snapshot failure still
allows a stop request. If publishing the request fails, the reference is still
preserved and `stop-request-error.json` records the failed attempt; stop is not
confirmed. A later notification can retry without restarting or resnapshotting.

## What is compared

An arm is a frozen deployment plus reasoning, request parameters, expected serving
identity and task group. A different thinking budget, adapter, template, runtime
or endpoint is a different configuration. Up to eight explicitly named variants
can be offered; one challenger runs at a time across projects. Among sampled
eligible variants, the least attempted is tried next, including failed attempts.
This is bounded exploration, not a statistical bandit or a general model ranking.

New implementer sessions with one Git repository and named verifiers are eligible,
including managed Work implementation sessions. Conversations without checks,
attachments and resumed sessions are skipped with a reason in the primary
session's `challenger.json`. Set the task group explicitly; no classifier call is
added. Variant `task_types` restricts the groups it may sample.

The managed X5 pilot wrapper is incompatible with challenger worktrees: its
root guard deliberately binds execution to the original project. Only its
emitted eight-token signature is recognized:
`[absolute_python, "-m", "relay.test_selection_pilot", "run", "--store", absolute_store, "--pilot-id", lowercase_32_hex_id]`.
Extra flags, reordered arguments, lookalike modules, ordinary pytest commands
and custom commands are not classified as that managed wrapper. Classification
only inspects argv; it does not execute commands or resolve programs.

If any named verifier has that signature, only the optional comparison is
`SKIPPED`, with an incompatibility reason in the existing `challenger.json`
admission marker. This happens before variant realization, budget reservation,
run/worktree creation, source copying or worker dispatch, including managed Work.
The primary delivery, its original check and argv remain intact. Neither the
pilot's root guard nor the existing `pytest_scratch` handling is relaxed.
This skip is not measured savings or model qualification, and does not prevent
code delivery or independent audit.

The input is captured before the primary's first model call, including dirty and
untracked files and exact Windows line endings. The challenger gets an isolated
Git worktree, the original instruction, scope and checks, and its own context.
It receives no primary transcript. Terminal access and backlog mutation are not
granted. Named verifiers retain their existing project contract. Ignored build
dependencies are not copied; verifiers must work in the isolated checkout.

If primary and challenger both use local compute, the challenger waits for the
primary. Otherwise they can overlap. Local Labs owns loading, GPU admission,
templates and adapters; OS uses the existing live admission and count/generation
binding. The experiment does not change the running server configuration.

## Resources

| Route | Experimental allowance | Evidence |
| --- | --- | --- |
| Local models | Sample %, daily seconds, calls/time per session | Tokens, elapsed time, artifact/template/runtime identity when reported |
| OpenRouter, Bedrock and other APIs | Daily USD and per-paired-trial USD | Existing provider prices, returned charges, retained uncertain reservations |
| Codex CLI/Responses, Claude CLI, agy CLI | External sample %, daily model-call allowance, quota headroom | Existing quota diagnostics; CLI estimates stay estimates |

Limits count experiments across projects on the UTC day, including both reviews.
Local seconds and subscription calls reserve the configured ceilings; they are
not billed GPU time or provider quota units. Different models on the same endpoint
share a resource-pool identity. An observed quota/rate limit pauses that pool's
challengers for 15 minutes, followed by fresh admission. Unknown quota requires
the explicit bounded-probe checkbox. API spending requires a complete tariff, or a
complete models.dev reference price ([HTTP providers, Prices](HTTP_API_PROVIDERS.md#prices));
missing/conditional prices stop dispatch. A private paid gateway can explicitly
set `accounting: "usd"` instead of the private-endpoint local default.

Hardware, subscriptions and quota opportunity cost are not zero. The existing
delivery-cost allocation snapshot can price these explicitly. Missing amounts
permit experiments but prevent an economic preference. A ceiling is a local
reservation based on known tariffs, not a provider billing guarantee.

The storage limit includes retained experiments across projects and input/output
snapshot estimates. At the limit, new experiments pause and evidence remains.
It is not a disk quota for arbitrary output generated by project verifiers.
There is no automatic deletion of evidence behind a saved preference.

The seconds-per-model-session setting bounds each inference call as well as the
whole session; a call also stops at the remaining session deadline. There is no
separate hidden 90-second cutoff and no automatic retry of an interrupted CLI
exchange. Local process cleanup does not establish remote completion or cost.

New trials keep their candidate and reference workspaces under the shallow
`challengers/wt` directory in that same storage area. This leaves room for nested
Windows test fixtures without changing configured check arguments. Receipts stay
in the trial directory; older trials retain their original workspace locations.

**Retry failed stage** explicitly starts a new bounded trial only when the saved
reference finished with matching producer evidence and the original Git input
is unchanged. It retains that real producer and its costs; it does not run the
reference implementer again or reopen an old terminal record. Managed Works and
external graph inputs use their own recovery path. Qualified verdicts are not
resampled. Retried references produce separate one-case descriptive reports;
reusing a delivery does not add independent cases to a recurring cohort.

When the challenger also finished and its checks passed, retry reuses both saved
workspaces and the real completed implementation. Only the unfinished reviews
are dispatched and reserved; no new implementation calls or source copies are
created. Producer receipts, source bytes and model identity are checked again
before review. A changed candidate or producer stops recovery without a model call.

Saved records, including legacy records without an execution-contract version,
are revalidated for X5 incompatibility during public retry, before repository
rebinding (even to the same root), and at worker entry and dispatch. This also
blocks reuse of a completed implementation: retry raises an explicit error
without new work, copies, reservations or model calls. A rejected active worker
persists a failure reason without consuming reused output or recalculating old
reservations. Existing receipts, completed implementations and historical
reservations remain available; do not bypass the pilot root guard to recover.

**Stop experiment** requests cooperative cancellation for the selected trial.
An in-flight provider call may still finish; existing receipts remain. Failed
implementations or checks do not dispatch paired model reviews, and an incomplete
first paired review stops before the next one spends more quota.

### Bounded paired replay, 2026-10-29

One real four-file linked-stop change completed paired review through the public
TUI. Both arms used the same captured input/instruction, delegated posture and
30-call/900-second limits; a shared Windows fixture repair was disclosed.

| Completed implementation | Logical calls | Actor time | Focused checks | Independent Argon high review |
| --- | ---: | ---: | --- | --- |
| GPT-6.1 Sol high, API reference retained from the earlier run | 21 | 606.1 s | 106 passed | ACCEPT |
| Claude Haiku 5.5 high, Claude CLI | 14 | 746.5 s | 30 passed | REVISE |

Haiku omitted cooperative `CANCELLED` handling, several stop boundaries and
required fault coverage. Passing its tests did not qualify the implementation.
The two valid Argon CLI reviews used seven and six logical calls, respectively.
Failed transport/schema attempts remain recorded separately; the final review
replays called neither implementer and created no new source workspaces.

This establishes one bounded paired quality observation and recovery path, not
the eight-case campaign, complete delivery economics or a model preference.
CLI token/cache categories and dollar cost remain unknown. No saving or routing
promotion is inferred from fewer calls, and the earlier API reference is not
counted as a new API call during recovery.

## Configure a variant

The advanced editor accepts profile names or objects. Profiles are the same
named deployments used by the ordinary model selector and `profiles import`.

```json
[
  {
    "profile": "local-qwen",
    "label": "Qwen budget 512",
    "parameters": {"reasoning_budget_tokens": 512},
    "task_types": ["implementation"]
  },
  {
    "profile": "my-codex-cli",
    "reasoning": "low",
    "resource_pool": "codex-developer-account",
    "task_types": ["implementation"]
  }
]
```

`context_projection` compares context policies on the same model. For example,
`{"profile": "agy-cli", "label": "AGY elide edited reads", "context_projection":
{"elide_edited_reads": true}}` sends reads of files changed by a later successful
edit as a short marker with range and hash; the journal keeps the full text. The
arm is distinct from the plain profile, so paired trials measure tokens, calls,
rereads and acceptance of the policy itself. When a sampled variant differs from the
primary only in context policy, it is preferred over variants that also change the
model; a variant identical to the primary arm is never dispatched (measurement).

The reviewer field also accepts a profile name or a JSON variant. Request
parameters use the provider/runtime's existing schema, not a universal invented
reasoning parameter. `accounting` can explicitly describe a local, quota or USD
route. `resource_pool` groups aliases for the same account/compute resource.

For fleet variants, `identity` pins fields returned by the serving owner, such as
`template_sha256` or `runtime_configuration_sha256`. Requested LoRA parameters
require an observed `adapter_sha256` or `adapter_configuration_sha256` binding.
The current fleet does not emit these adapter fields: such a request stops before
generation rather than qualifying an unconfirmed adapter. Experiments with a
preconfigured adapter need an owner profile that exposes its effective identity;
this toggle does not train, upload or hot-swap LoRAs. Template/runtime hashes
are recorded, with the owner's stated provenance, not presented as a fresh hash
of the loaded model file. A changed serving fingerprint invalidates qualification.

## Review, compare and reuse

An optional independent reviewer inspects both results and runs the named checks.
Unattested reviewer independence leaves the result unknown. Failures, timeouts,
unfinished dispatch and changed source sessions stay visible. No reviewer means
exploratory checks only. Automatic studies retain all matched attempts; **Compare**
opens the existing `/delivery` panel once at least two cases exist. A tariff or
effective-profile change prevents mixed evidence from producing a winner.

The study covers the implementation sessions and paired experimental reviews.
For managed Work, refreshing the panel also reads the entire recorded production
journey (planning, implementation, resumed attempts and reviews), including its
archived train. These totals stay separate from experimental overhead. A managed
study leaves additional costs unknown until the complete strategies are matched;
an implementation-only comparison cannot automatically claim a full-delivery win.
Human effort and admission overhead are not automatically priced.
If the primary conversation continues, its paired verdict becomes unknown until
the broader journey is reconciled. Use the existing delivery-study format to add
correction, rescue and other session receipts before claiming full delivery ROI.
Exported preferences remain frozen while new experiments accumulate.
The live study uses up to the last 100 paired attempts, including every failure
in that window, and shrinks the window to fit the existing study byte limit.
Older receipts remain available; this prevents indefinite growth from stopping
the comparison. The full retained history still counts for sampling and budgets.

**View patch** exposes the candidate. **Use changes** requires a passed independent
review, fresh checks and explicit replacement confirmation. It replaces only the
recorded primary changes, refuses later edits and can resume after partial file
application. It does not commit, admit or land the patch. Use the project's normal
review and incorporation flow. A removed managed worktree cannot be overwritten;
the retained patch remains available.

Worker death retains receipts and reservations and never automatically replays
paid calls or effects. The primary task continues when experiments are skipped,
out of resources or unavailable.

## Qualification on 2026-09-24

A real TUI journey used the resident local Qwen twice on an isolated `clamp`
implementation: budget 2048 for the primary and 512 for the automatic challenger.
Both passed the same verifier in four model calls. The challenger took 11.148s;
artifact, template, runtime and request configuration were retained. No paid API
was called. Independent review and hardware allocation were absent, so acceptance
and economic superiority remain unqualified. The pilot toggle was then disabled.

The raw evidence of this run is not published.
Automated tests additionally exercise paired review, failed attempts, quota and
USD reservations, subprocess execution, crash/reuse recovery and 80x24/130x44 TUI
controls. The visual check found and corrected horizontally clipped budget inputs.

The ongoing capability and its evidence remain on FLOW-19. New configurations
are experimental records, not a new backlog item for every model or budget.

## Installed delivery

OS **0.1.55**, source commit `ad63b905e8c6ec95e79180ef87306b8919884468`.
All five project wheels were identical across two clean builds. The bundle's 238
OS source entries match Git; 191 installed OS files match those source hashes.
The installed TUI opened the real pilot and patch in 80x24, retained the disabled
toggle and explained why a comparison without an independent reviewer remains
unqualified. No new model call was needed for installation validation.

Actual rollback to **0.1.54** and return to **0.1.55** preserved all ten checked
pilot receipt hashes. The previous stable release remains the rollback target.

`tare-tools-0.1.55-win-py312.zip`: 36,139,055 bytes, SHA256
`c3b04e454941e9212a7894545b11abe6be8f2fe3b40eb26c25139a05f4764e3f`.
Dependencies are pinned in the bundle lock, including psutil 7.2.2; runtime
packages retain Kernel 0.9.21, Runtime 0.14.0, SpecGraph 0.7.3 and Backlog 1.2.0.

Pilot and release evidence.
Local verification passed 142 focused tests, three manifest checks, and the
additional live-window and changed-instruction cases. Final source CI passed on
Linux (2,620 passed, 7 skipped) and Windows (2,612 passed, 15 skipped), plus 105
quick contract checks on each platform:
[run 36044127120](https://github.com/augusto-scarvalho/tare.tools.os/actions/runs/36044127120).
The verified bundle and checksum are published in
[release v0.1.55](https://github.com/augusto-scarvalho/tare.tools.os/releases/tag/v0.1.55).

## FLOW-19 — documentation closure and recoverable evidence

Documentation observation: 2026-10-28T17:55:29+00:00. This closure does not activate
experiments, run models or requalify the historical installation.
It preserves the operator's admission: a negative/inconclusive result, **no
qualified economic preference**. The baseline stays; bindings already started
are not changed. The pilot toggle is still reported as disabled,
without reusing the one-off US$5 authorization.

### Shadow, real execution and OPE

* **Shadow recommendation:** shows only what the policy would recommend. It does not measure
  the alternative that was not run, and attributes no performance, savings or quality to it.
  `SUGGESTED` is not dispatch, execution, acceptance or landing authority.
* **Real execution:** compares only arms that actually ran, with matching contracts,
  sessions, checks and receipts. Failures, retries, corrections, rescues
  and reviews are part of the total-cost obligation, together with hardware and the complete
  journey. Missing values stay unknown, not zero. Unmeasured acceptance
  does not become qualified acceptance or an economic preference.
* **OPE is outside the delivered, demonstrated scope.** The trace below covers selection
  through a study of executed deliveries and revalidation; it does not demonstrate a
  counterfactual estimator. Any future OPE requires support/overlap of the evaluated actions and
  genuine propensities from the logging policy. For deterministic selection,
  conditioned on the real inputs, the propensity represents the real selection: **1 for
  the selected action and 0 for the others**, including the baseline when there is a fallback;
  never confidence, a heuristic score or an invented probability. Without overlap, the
  outcome of the alternative that was not run cannot be estimated. The recovered excerpts
  **do not prove numeric propensity logging** in the receipts. The challenger's sampling
  percentage does not replace that evidence.

### Recovered implementation trace

The SHA-256 values below are of the complete files returned by this
revision's read; the ranges identify the support for each claim, not historical
runs. Recover the file in the repository and check its hash before
reusing the trace; a mismatch requires a new revision.

| Locator | File SHA-256 | Support and limit |
| --- | --- | --- |
| `relay/route_strategy.py:103-200` and `:201-219` | `4a0df368fd73cf7ec86455df295ffc365351843d4a6e0b23c804e2623cc83f72` | `measured_delivery` validates evidence/eligibility and at 190-200 returns `SUGGESTED` before `assess()` (213). Records the `KEEP_BASELINE` fallback and the absence of counterfactual savings; recommending an alternative does not run it. |
| `relay/adaptive_routes.py:162-219` | `21a39ad275eeb4aa2da4321c4d37d0577680fc2a6756d3ce6a022e6fe71365ff` | Revalidates policy, Work, profiles, facts and evidence before a new automatic dispatch. A failure keeps the baseline implementer/audit; resume does not call it. Does not modify a binding already started. |
| `relay/delivery_comparison.py:201-288` | `de8d6508284ef03b512555f7e3bca9e134ec9ca3b7eff5669d7ca32fe0cbe0fd` | Aggregates executed trials/receipts; issues prevent a winner. Exports a `measured_delivery` policy bound to study/evidence hashes and revalidates the comparison. The declared scope includes continuations/corrections/additional costs; unknown is not zero. Not OPE. |
| `relay/challenger.py:201-414` | `81bfaea9ed9233444fd6fc430b5e64efc9dfb116805e9150ff01996f5b94cbd0` | Links real sessions and verdicts, downgrades the verdict to unknown when the journey changes, selects an eligible/sampled variant, reserves budgets and freezes the reference result. Toggle off returns without starting. Reservations do not prove complete costs; sampling does not prove recorded propensities. |

### Coverage: the FLOW-19 pilot is not FLOW20 AB8

The recovered source of this guide, section **Qualification on 2026-09-24** (lines
144-159 before this addition; original SHA-256
`60a32efc9df1d9a9086403b24e45d22e4cfe3c37c651755349f23610a5a9ed0f`),
reports **one TUI journey, two local Qwen arms on `clamp`, budgets 2048/512 and
four calls**, with 11.148s for the challenger and the same verifier passing. It is not
a 16-run FLOW-19 campaign. The node's history also contains claims
of four calls per arm / eight in total: they are preserved as history, not
used to widen the coverage the guide reports. The recovered original
`CHALLENGER_MODE_2026-09-24.json:6-57` records four calls by the primary and
four by the challenger; that clarifies the per-arm count, and does not create eight journeys
or AB8. The guide's historical report of four calls is kept, with this
distinction stated explicitly. There is no new measurement in this revision.

The record [FLOW20_AB8_2026-10-18.json](FLOW20_AB8_2026-10-18.json), SHA-256
`533db89c13bb6ddc2d69aed4133ee007c8c16699fc11a13d13ac3e1d88995e58`,
is **FLOW20**, not FLOW-19: eight cases of historical regressions, eight A trials and
eight B trials, one run per condition, four AB pairs and four BA pairs. The decision is
`KEEP_BASELINE_NO_DEMONSTRATED_ADVANTAGE` (`/sample`, `/decision`, `/totals`).
The 16 trials are neither four calls nor 16 FLOW-19 runs. In both arms,
`monetary_cost`, `physical_model_attempts` and `subscription_quota` are `null`.
Its 98 compared generation intents and the excluded pilot do not widen FLOW-19.
The five limitations in `/limitations` (1517-1523) are:

1. Selected historical repairs, not eight complete ordinary Works.
2. One run per condition, one model and one source audit by the maintainer;
   no statistical or vendor inference.
3. Money, physical attempts and remaining subscription quota are unknown.
4. Offline tests of the production path validate obligations; they do not simulate
   provider capacity.
5. Conditional acceptance and internal correction are distinct from terminal
   material defects.

Neither the pilot nor FLOW20 qualifies models in general or picks an economic winner.
Independent review of the pilot, hardware, complete costs and the complete journey
remain absent/unknown; that does not invalidate the admitted negative result,
nor does it authorize new calls or a mandatory benchmark.

### Obligations, sources and gaps

| Closure obligation | Recovered source | Preserved gap |
| --- | --- | --- |
| Shadow/OPE/real selection | Policy, selection and revalidation trace above | There is no OPE estimator and no proof of numeric propensity logging; no measurement of the alternative that was not run. |
| Total cost of failures and corrections | `delivery_comparison.py:201-288`, `challenger.py:201-228`; DELIVERY_COST:64-85 and DELIVERY_EXPERIMENTS:3-45, originals identified below | The obligation includes failures/retries/rework/audit; the partial costs recovered do not settle hardware or the complete journey. |
| Canary decision and receipts | Historical qualification, CHALLENGER_MODE:42-111 and DELIVERY_ECONOMICS:10-32, originals below; admission in `work-graph.json#/nodes/210/metrics/admission` | Verdict unknown; comparison without a winner. The recovered hash of the originals is not a current independent audit of the session receipts. Toggle off and baseline kept. |
| Paired/prospective report | Historical guide; FLOW20 record with a separate scope | The 16 AB8 runs requested in FLOW-19's `evidence_required` are not demonstrated. Do not transfer FLOW20 trials. |
| Limited configuration or negative result | `[OPERATOR_ADMITTED]` note and history of the same node | A negative result is valid; the historical DONE does not certify full proof and stays in the history, with its date and admission preserved. The current closure is `PARTIAL`, `work_status=IN_PROGRESS` and `dod_satisfied=false` while full proof is missing, following the host's authoring contract (DONE requires a satisfied DoD). The stale derived revision is removed under the same contract, without inventing a replacement hash. This fixes the DONE_DOD/REVISION_MISMATCH diagnostics, not the historical gaps or the pending audit. |

The initial graph of this revision has SHA-256
`94dde000afab34ca179137c13db5b2fa51cb10950eadc6987fa40c7855d7394c`;
the JSON locators above identify preserved historical facts, not an
independent attestation. The closure/checks stay in the same node. The old copy of
FLOW-19 in the proposed work-program backlog (cited in `source_details`) is not changed;
the revision/hash of that copy were not recovered here.

### Recovered originals, with no additional copies

The read-origin manifest (SHA-256
`df904a689f7986d13ee3c8603dda64aacdd73e220bf304384e7a57909d432ac9`) is not published;
it identifies original bytes, not a new attestation; `missing=[]`. The five files
are reachable through the Git locators below. Keep the eight historical
`dod_evidence` entries; the original relative locators are resolved by the full
Git locators below. The SHA-256 is of the complete file, not of the excerpt.

| Original / recoverable locator | Git blob | SHA-256 / bytes |
| --- | --- | --- |
| `git:7050360c097834a7cf6a04fbdaba79671067a495:docs/operations/GUARDED_DECISION_ROUTING_2026-09-21.md` | `4bdb80ef4acc20d083574f9c1a872dbc7237c34a` | `5d906154dbbc6c9065413443e9cccba33770c7ec19d1693f4af7641ec79f50d9` / 5612 |
| `git:7050360c097834a7cf6a04fbdaba79671067a495:docs/operations/DELIVERY_COST.md` | `2dc20d99e0d8d37cc9aab7e7bc1bd7288dd43b99` | `edd62f582e8054c9156f09975f3c989879ac9c0ceb98f5db884162ea5f8509c7` / 14237 |
| `git:ad63b905e8c6ec95e79180ef87306b8919884468:docs/operations/DELIVERY_EXPERIMENTS_2026-09-24.md` | `4bd1a10a9bfc3e08c2712aa173da25fb8f839459` | `b5a97fd75d8568d1aeba73a8ca6768ba282fa03e64246c6494c651e1fe01ce3f` / 8543 |
| `git:ad63b905e8c6ec95e79180ef87306b8919884468:docs/operations/evidence/CHALLENGER_MODE_2026-09-24.json` | `074c2546c24ab628da545640a0a9fd03cff37598` | `78196ade11b9a7e19a8584683be73b3577d6c22ae2f7fab1fcb267b45ffc4910` / 6079 |
| `git:ad63b905e8c6ec95e79180ef87306b8919884468:docs/operations/evidence/DELIVERY_ECONOMICS_2026-09-24.json` | `37fe611563682b35cd80de0b6db486ea7597b9d1` | `e17f8e1fa68814cc92bc591cff2b48c3f1899cdc281841fb38a72783119d2b47` / 36084 |

* GUARDED_DECISION_ROUTING:12-30,49-65,82-92: deterministic guarded mode;
  at least two eligible pairs with complete evidence and a matching cohort.
  Unknown cost is never zero; raw probabilities give no execution
  authority. Without real cohorts, `TASK_ROUTING_COHORT_MISSING` or
  `DELIVERY_EVIDENCE_MISSING` keeps the baseline. 0.832s/43.264s are transport and
  resources, not routing quality.
* DELIVERY_COST:64-85,153-165: all calls, including failures, retries,
  rework and audit, divided by accepted deliveries; a candidate without acceptance
  never wins. A subscription is not zero. Qualification of 2026-09-23: 36 controlled local
  calls and zero commercial ones; US$2.88 versus US$1.80 use **synthetic** tariffs and
  qualify the flow/accounting, not commercial models.
* DELIVERY_EXPERIMENTS:3-45: keep the current reference, 45 commercial calls
  including failures/corrections/audits. Contrast: US$0.11305357512 known +
  US$0.230434 reserved/uncertain = US$0.34348757512. Administrative record of
  US$0.0067437 outside the contrast; including it, the journey is US$0.11979727512 confirmed
  and US$0.35023127512 with reservations. A reservation is not an invoice. Both strategies are
  `COST_INCOMPLETE:snapshot`; no preference activated. Electricity,
  preparation and the coordinating subscription are not allocated; do not certify a complete cost.
* CHALLENGER_MODE:42-111: verdict `unknown`; `known_usd="0"` is local
  accounting, not a total cost of zero; 11.148s, `independent_review=not_run`, hardware
  unknown, preference unchanged, toggle disabled. Its `/receipt_hashes`
  (112-122) are historical references, not a current reread of the session files.
* DELIVERY_ECONOMICS:5-32 and `/comparison`: `winner=null`,
  `INCOMPLETE_EVIDENCE`, incomplete costs. Declared historical acceptances
  do not qualify the local pilot without independent review.

A literal/regex search for `propensity|propensities|off-policy|OPE` in the five originals
found no text. That is limited support for excluding OPE from the demonstrated
scope, not universal proof of absence nor proof of numeric logging.

**Remaining gaps:** the unpublished raw evidence of the 2026-09-24 run and the
session files it references were not reread; their declared hashes
are not an independent revalidation. This revision also did not recover the
bytes/hash of `https://github.com/augusto-scarvalho/tare.tools.os/actions/runs/36044127120`
and `https://github.com/augusto-scarvalho/tare.tools.os/releases/tag/v0.1.55`.
The absence of a current independent review, hardware and the complete journey remains
explicit. No new artifact is written in `docs/operations/`; every addition stays
in the two targets, without editing `.gitignore` or forcing tracking.

### Checks and candidate audit

The focused checks are `pytest-tests` (only `tests/test_route_strategy.py`,
`tests/test_guarded_routes.py`, `tests/test_delivery_comparison.py` and
`tests/test_challenger.py`) and `tare-backlog-validate`. Their results, limits and
link to the candidate are recorded in `completion.closure_review` of the node
`flow-19-canary-de-routing-orientado-por`, not inferred from the historical CI above.
Green tests do not certify historical execution, savings or every criterion.
Independent audit of the candidate is a host step after the proposal; the
implementer did not run it. It must bind an independent identity/executor, the
hashes of the two targets, the diff, a structural comparison and check receipts to the same
candidate. Later changes invalidate the earlier binding and require rechecking.
The Backlog preview/apply CLI, `git diff --check`, inventory/tracking and automated
structural comparison must be run by the host when they are not available in the
catalog; unavailability is not approval. No auditor is simulated here.

### Ordinary observation of 2026-10-29

The [descriptive record](CHALLENGER_ORDINARY_X5_2026-10-29.json) keeps one real case
with the same instruction, scope and ceilings of 30 calls/900 seconds: Sol high proposed
the change; Haiku xhigh passed 69 tests but hit the deadline before completing the
contract. The paired reviews were not started. This keeps FLOW-19's representative
qualification open; it establishes no winner or per-delivery savings.
The Codex CLI reported 71,424 tokens read from cache; the Claude CLI's token coverage
remains unknown. Monetary costs were not inferred from this data.

The separate X5 pilot covered two configured tests and was closed explicitly
with `keep-full`. There was no observation after the conversational delivery, which was
left unapplied after the divergence between the requested and the selected auditor. The record
preserves that gap and the cost of the earlier attempts. The decision keeps full
verification; the two tests do not certify that the general selection is adequate.

### Executed route decision

Each route receipt returned by `select_route` carries the `executed_decision` field
(`tare.tools/executed-route-decision/1`). It is persisted in the `routes.json` of the
plan directory. It records the deployment actually executed (propensity 1) and the
known eligible alternatives (0); on fallback, `first_ranked`, `executed` and
`skipped`. Unknown alternatives are not recorded. OPE is not supported, and there is
no observed performance or savings for models that did not run. FLOW-19's representative
qualification remains open, with no claim of model superiority.

#### Records per dispatched role

Real dispatches reuse the same `executed_decision` and add, for each
executed attempt, a compact `tare.tools/executed-attempt/1` record. It carries
the deployment, model, protocol and reasoning present in the deployment handed to the
dispatch, and the outcome. Missing reasoning is `UNKNOWN`, never an invented
default. For the researcher CLI, the reasoning actually sent is recorded, not the
profile value. The shared helper reads `native_auto_selection` and `native_selection`:
`position` is the effective ranking, while the visiting order proves neither ranking
nor fallback; inconsistent provenance is `UNKNOWN`.
There are no prompts, private arguments, keys, full profiles or code.

- Conversation plan (`delegate_plan`): `planning/*/receipt.json`, with
  `executed_decision` (`EXACT_TEMPORARY_PIN`, `TEMPORARY_POLICY`, `CONFIGURED` or
  `AUTOMATIC`, only visited candidates and known exclusions) and `attempts`. The
  durable intent records the attempt as `DISPATCHING`; an interruption keeps that
  state (unknown outcome) and a reused receipt does not create a new attempt.
- Delivery audit (`turn_engine.run_audit`): the returned record carries
  `executed_decision` and `attempts`, one entry per auditor actually
  dispatched, including failures, limits and interruptions; visited candidates that hit a
  usage limit appear as `USAGE_LIMITED`.
- Explorer: `delegations/*/receipt.json` receives `executed_decision` and `attempts`
  at dispatch. The outcome of the first attempt is persisted before recovery;
  resume appends and persists a separate attempt, marked `resume`, before the
  second dispatch. An interruption keeps that attempt as `DISPATCHING`
  (unknown outcome), without overwriting the earlier outcome.
- Research: in `/research` through the Codex CLI, the flow record registers the
  `HELPER_ROUTES` routes visited when the helper provides the optional trace. A
  legacy helper without a trace records neither `executed_decision` nor `attempts`: the
  provenance of the execution stays unknown, and the answer is unchanged. In the snapshot flow, each item of `attempts` in
  `research.json` receives `executed_decision` and `dispatch`; the origin must be proven
  by the saved selection or by explicit configuration, otherwise it is `UNKNOWN`, and an
  existing analyst session appends `RESUME_SAVED_ROUTE` without rewriting attempts.
  In `web-research/*/receipt.json`, `attempts` and `executed_decision` record only
  launched processes: the model/effort actually passed to the CLI (`medium` by default,
  the pin's reasoning when applied and the AGY model resolved from the effort), not the
  profile's defaults. Each attempt is persisted as `DISPATCHING` before the process, and
  only that attempt is closed; fallback and resumption keep the earlier ones, and unknown
  costs stay unknown. The fallback to API search also keeps the `attempts` and
  `executed_decision` of launched processes that returned `not_started`, without creating
  an execution when no process was launched. Cache without inference and refused preparation create no
  attempt or decision; the freeze proves the pin/configured policy or is `UNKNOWN`.
  Search without a model produces no decision.
- Implementer Work: the custody's `host.json` records the decision from the
  already saved selection (`native_auto_selection` or `native_selection`) and the attempt.
  During `PAUSED`, the last attempt stays open (`DISPATCHING`), with no
  closing or rewriting of `host.json` or `attempt.json` after the pause is
  sealed, including on resumes that stay paused. The pause appears in the
  `run.json` receipt and in the control record. Only the terminal outcome, outside `PAUSED`,
  closes the attempt with `FINISHED` and `ACCEPTED` or `NOT_ACCEPTED`, preserving the
  earlier ones. This path is distinct from the conversation implementer and does not cover it.
- Conversation implementer (`work_units.run_unit`): each rung actually
  dispatched in `units/*/receipt.json` receives `executed_decision` (`EXACT_TEMPORARY_PIN`,
  `CONFIGURED`, `AUTOMATIC` or `RESUME_SAVED_ROUTE`; earlier rungs that ended appear as
  `EARLIER_RUNG_ENDED`) and `attempts`, without overwriting earlier rungs. The
  implementer's temporary pin is recorded only when it matches the deployment
  actually sent, including its reasoning; it does not change the selection. Resuming
  the waiting worker takes precedence over the pin and marks the attempt with `resume`. An interruption keeps
  `DISPATCHING` (unknown outcome). `review_only`, a refusal in the auditor's
  preflight and failures before dispatch record no attempt. In the waiting
  worker, both funding validations (calls and active time) precede the
  executed record; a refusal preserves the earlier history without a fictitious attempt.
- Rechecking a retained candidate does not start another implementer. In the OS, if only
  hashes and sizes of the generated manifest changed, the host requires the original
  Git blob anchored at the captured hash and equality of all authored fields. It records
  that requalification, regenerates the fingerprints and requires new checks and audit.
  Code, patch, producer and earlier receipts stay preserved; any
  other mismatch is refused with the affected paths.
- `--unlimited time` also removes the dedicated ceiling for the host's checks. The Work
  deadline, cancellation, isolation, mediation and the limits on ordinary commands still
  apply. Model transport timeouts stay finite; the planner
  keeps the normal 600 s timeout when the stage's total time is unlimited.
- Conversation audit (`conversation_review.review`): `reviews/*/receipt.json`
  first records the durable intent (`RUNNING`, `request_sha256`, invocation and
  reservations), without claiming dispatch. Only after preparing `input/`, `diff.patch`,
  views, `request.json` and config, immediately before `run_session`, does it receive
  `executed_decision` (unavailable routes already visited as `UNAVAILABLE`;
  `PINNED_RETRY` on retries of the same route; `EXACT_TEMPORARY_PIN`, `CONFIGURED`
  or `AUTOMATIC` in other cases) and an attempt with the deployment after
  `require`/`with_effort`. An OSError in that preparation persists the terminal state
  `FINISHED` with outcome `INCONCLUSIVE`/`PREPARATION_FAILED`, zero calls and
  no executed decision or attempt; the original exception still propagates
  to the caller. The preserved intent protects against resampling. Reused
  decisions, absence of candidates and insufficient budget create no record.

An explicit continuation of a read-only audit can rebuild the disposable view
at the original address and resume the saved session. The restore revalidates the
observed hashes, the route, the permissions, the remaining limits and local cleanup;
interrupted calls stay counted, with unknown remote consumption.
The earlier receipt is preserved, with its hash, in `continuations/`.

The audit phase uses up to 1,200 s and each call up to 600 s, respecting narrower
limits set by the parent. A completed partial review reports the missing coverage;
it does not demonstrate a lack of quota. After the proven terminal hang of AGY was
fixed, the host can grant one final call within the parent's remaining balance. The grant
is explicit in the Runtime journal and does not renew other limits. This recovery
avoids opening a new session or reserving an unnecessary compaction.

Legacy receipts without these fields remain readable, with an unknown decision. The
propensity is deterministic (1/0), not a probability or a confidence. OPE is not
supported. These records do not prove performance, savings or remote execution,
and they do not complete the broad qualification of FLOW-19, which remains open.
