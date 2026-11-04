# Hosted Jev mode qualification

The OS now explicitly requests `jev-1.14.0` for command triage, initial routing,
tier shadows, Work-form shadows, final-answer checks, audit materiality, novelty
and proportionality, explorer tier advice, relevance ranking, instruction and
contradiction flags, citation support, audit-passage support and report sufficiency.
Conversation intent was qualified separately. Shadow measurements remain shadows;
no decision gains execution, admission or approval authority through this change.

The [model documentation](https://docs.typesafe.ai/models) listed 1.14 as stable
and both latest/preview aliases as 1.14 on inspection. Explicit pins avoid alias
movement. Existing persisted command policies pinned to 1.13 remain resumable
with that model; new policies default to 1.14. Local judges and explicitly supplied
external worker commands retain their own model selection. Kernel's generic
adapter default is unchanged; every OS-hosted call supplies the qualified pin.
Setup displays the OS's hosted choice rather than the generic adapter default.

## Method and observations

Forty-one focused synthetic controls compare the same state and question on 1.13
and 1.14: nine command cases and 32 controls across the other purposes, including
Portuguese/English requests, positive/negative source support, quoted instructions,
injection, explicit review, unknown executable behavior, promises versus results,
scope creep and unresolved exploration gaps. Proposed commands are text only and
are never executed by this qualification. Existing controller thresholds remain
unchanged; normalized/rounded probabilities are not measured success rates.

The run stopped on a style-finding control that used generic `request/finding/diff`
fields. Its 1.13 answer was `material`. Inspection showed that this did not reproduce
the actual materiality envelope. With the production fields
`user_request/audit_finding/change`, both versions answered `residual`. The failed
packet and result remain recorded; this is a corrected control, not evidence of a
production regression or of a model-only accuracy gain. Two sufficiency labels
were aligned with the actual API options before those controls were submitted.

All final controls passed with both versions. No new few-shot examples or question
retuning were needed. These are bounded compatibility controls, not a representative
accuracy benchmark, ordinary-delivery cohort or proof of multilingual superiority.

Including superseded fixture packets, 85 API calls used 39,342 input tokens. The
published input tariff of USD 0.042/Mtok gives **USD 0.001652364**; this is a tariff
estimate, not an observed invoice. Unchanged payload/model pairs reuse saved results.
The raw evidence of this run is not published: the full synthetic packets, outputs
and qualification helpers, and a `results.json` with model identities, input usage
and latency. No credentials or raw operational transcripts
are committed with this report.

## Request and recovery boundaries

Inspection also found prefix cuts in Work-form/tier shadows, final-answer checks,
fix novelty/proportionality, cited claims and candidate passages. Those paths now
preserve the subject. Choice input beyond the existing 8 KiB adapter bound is an
explicit unavailable observation before transport; it cannot escalate a reply,
demote a finding or spend a classification call. Batched state/questions have a
64 KiB UTF-8 bound; overflow leaves evidence unverified and existing rule fallback
in charge. Intent's previously qualified input behavior is preserved.

Kernel 0.9.68 verifies the returned checkpoint before exposing batched readouts,
including missing identity. Offline regressions exercise full tail requirements,
oversized multilingual input, preserved findings, no-call overflow, pinned batch
dispatch, resumed 1.13 policy compatibility and local-only/disabled launch policy.
Permissions, cancellation, binding fences, sandboxing, independent audit and saved
decision integrity remain separate host controls. Qualification of live workload
quality beyond these controls remains ordinary-use evidence work.

## Windows CI follow-up

The first OS candidate passed Linux CI. Windows run `40684045608` exhausted the
30-second unit-test default in `test_revision_after_real_settlement_preserves_results_and_only_runs_remaining`,
while its first real Work was acquiring its lease and updating the board. This
offline fixture does not call hosted Jev. All 21 tests in that module passed on a
focused Windows rerun, with the two-Work test taking 12.99 seconds on the idle
machine. It now has the same bounded 90-second integration allowance as the
equivalent workflow-dispatch recovery test. The test's real Git settlements,
cancellation, retained-result and no-reimplementation assertions are unchanged;
no product deadline or global test limit was increased.
