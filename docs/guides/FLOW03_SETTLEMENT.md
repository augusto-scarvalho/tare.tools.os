# FLOW-03: settlement, landing and backlog reconciliation

This map qualifies the settlement path that already exists. It adds no module and no second chain of snapshot,
test or receipt. The owners are unchanged: `relay/candidate_change.py` records, re-derives and lands a candidate
(the Git compare-and-swap is Kernel's `compute_plane.candidate_git.land_candidate_ref`), `relay/landing_policy.py`
and `relay/relay_mesh.py` admit and land, `relay/work_run.py` and `relay/work_resume.py` drive `tare-ops run` and
its resume, and the Backlog write happens inside `relay_mesh.land_train`.

Every row below was checked by mutation on 2026-11-12 (base `1f2a0102`): the named guard was removed or made to
always pass, the named cases were run, and at least one failed. A guard that other layers also cover is written as
such; "alone" means the case still passes when only that layer is removed. Names are exact pytest ids under `tests/`.
Two exits of `tare-ops run` share one tail (admission, `admit_train`, landing): the native path, and a path where
the caller injects the implementer, which only tests and embedders use. Rows say which path a case drives.

## C1. Exact candidate

Only the commit and tree recorded in `CANDIDATE_CHANGE.json` and re-derived from Git can be admitted or landed.
A different commit, tree, base, descriptor (criteria are part of it) or diff is refused.

| Guard | Cases that fail without it |
| --- | --- |
| `candidate_change.materialize_verify_and_record_candidate`: no receipt unless the verifiers pass on the detached commit (`CANDIDATE_VERIFICATION_FAILED`) | `test_candidate_change::test_failed_verifier_never_emits_receipt` |
| same function: the worktree must be unchanged after verification (`CANDIDATE_DRIFT_AFTER_VERIFICATION`) | `test_candidate_change::test_writer_after_verification_blocks_receipt_can11` (through the `qualify_ephemeral_git_worktree` call; the `head`/`clean` line after it is covered by that call alone) |
| `candidate_change.verify_candidate_receipt`, descriptor part (`CANDIDATE_DESCRIPTOR_MISMATCH`: train, descriptor sha, work id, repository, base) | `test_candidate_change::test_divergent_receipt_is_refused_by_verify_and_landing[base_commit]`, `[descriptor_sha256]`, `[work_id]`, `[other_descriptor]`, `[moved_base]` |
| same function, Git part (`CANDIDATE_IDENTITY_MISMATCH`: tree, diff, paths, entries, and a different commit) | same test, `[candidate_tree]`, `[diff_sha256]`, `[changed_paths]`, `[changed_entries]`, `[other_commit]` |
| `candidate_change.land_recorded_candidate`: verifies again, then lands the receipt's commit | same test, all ten parameters (they also assert that the target ref did not move) |
| `relay_mesh._strict_candidate_context`: admission (`admission_valid`, `materialize_admission_evidence`) and landing re-derive the candidate | `test_strict_cutover_qualification::test_disposable_strict_train_uses_one_candidate_through_admission_and_cas[human-audit-normal]` (and every other `human-audit-*` parameter, which all pass through the same swapped receipt) |
| `landing_policy.verify_admission_bundle`: the evidence binds the candidate sha (`EVIDENCE_CANDIDATE_BINDING_MISMATCH`) | `test_landing_policy::test_bundle_recomputes_exact_receipts_and_candidate_binding` |
| `native_settlement.candidate_claim`: the accepted native claim carries the candidate sha | `test_native_settlement::test_accepted_claim_does_not_authorize_a_different_candidate` |
| `work_run.run_work`: the receipt read after the native run must equal the accepted `candidate_sha256` (`NATIVE_SETTLEMENT_CANDIDATE_CHANGED`); native path | `test_work_resume::test_run_refuses_a_candidate_receipt_changed_after_native_acceptance` |

## C2. Audit is evidence, not authority

An audit verdict, including an accepted one, lands nothing by itself. Landing needs the admission verdict, and
when the policy requires an audit a missing or non-accepting one stops before landing.

| Guard | Cases that fail without it |
| --- | --- |
| `work_run.run_work`: a verdict other than `ADMITTED` ends as `ADMISSION_<verdict>` before `admit_train` (injected path) | `test_work_run::test_run_stops_before_landing_unless_admitted[hard_check_failed]`, `[audit_required_but_absent]` |
| same, native path, with an accepted independent code audit | `test_work_audit::test_accepted_audit_alone_lands_nothing_when_admission_refuses` |
| `work_resume._resume_admission`: same stop when resuming | `test_work_resume::test_resume_stops_before_landing_when_admission_is_refused` |
| `relay_mesh._evidence_gate` (`ADMISSION`) in `transition_train`: `admit_train` cannot reach `READY_FOR_LANDING` without a valid bundle; alone | the two `test_work_run::test_run_stops_before_landing_unless_admitted` cases (they call `admit_train` after the stop) |
| `relay_mesh._land_train_locked`: recomputes the admission bundle again at landing; alone | `test_strict_cutover_qualification::test_disposable_strict_train_uses_one_candidate_through_admission_and_cas[smart-optional-roster-changed]` |
| `landing_policy.evaluate_admission`: `required` without a counted audit is `INCONCLUSIVE`; a failed independent audit is `REFUSED`; a failed hard check (including `cas_ready`) is `REFUSED` | `test_landing_policy::test_required_audit_holds_without_provider_call`, `test_landing_policy::test_executed_independent_audit_failure_cannot_be_ignored_in_optional_mode`, `test_landing_policy::test_smart_checks_fail_closed_by_failure_kind[cas_ready-REFUSED]` |
| `work_run.run_work`: `required` audit with no eligible route stops before any paid stage (`INDEPENDENT_AUDITOR_ROUTE_REQUIRED`); native path | `test_work_audit::test_same_bias_and_missing_required_route_stop_before_paid_stages` |
| `work_audit.run`: an audit verdict other than `ACCEPT` ends as `AUDIT_<verdict>` and writes no `AUDIT_REPORT.md` | `test_work_audit::test_revise_is_preserved_on_resume_without_fallback_or_regeneration` |
| `audit_cases` (`tare-ops audit`): a `CLOSED` case reports `authority: NONE` | `test_audit_cases::test_accept_closes_without_authority_and_preserves_original_bytes` |

Nothing in the landing path reads an audit case: `relay_mesh`, `work_run`, `work_resume` and `landing_policy` do not
import `audit_cases`. The only reader outside the audit modules is `session_hooks`, which copies the case summary
into a hook report. That absence is checked by reading the imports, not by a test (see "Known limits").

## C3. Stale base

If the target ref moved between the candidate and the landing, the landing is refused. Nothing is rebased, merged
or rewound.

| Guard | Cases that fail without it |
| --- | --- |
| Kernel `land_candidate_ref`: pre-check `BASE_REF_DIVERGED` and the `update-ref` expected old value. Each layer alone is masked by the other; both removed (in a scratch copy of Kernel) fails | `test_candidate_change::test_landing_refuses_a_target_that_moved_after_the_candidate_and_changes_nothing`; `test_strict_cutover_qualification::test_disposable_strict_train_uses_one_candidate_through_admission_and_cas[human-audit-stale-base]`; `test_work_run::test_run_reports_landing_refused_when_the_target_moves_after_admission` |
| `relay_mesh._land_train_locked`: a refused CAS leaves graph, capability and worktree as they were | the `[human-audit-stale-base]` case above |
| `work_run.run_work`: a refused landing is `LANDING_REFUSED`, never `SETTLED` | the `test_work_run` case above |
| `landing_evidence.materialize_landing_evidence`: `cas_ready` is false when the target is not at the base | `test_landing_evidence::test_materializer_marks_cas_not_ready_when_the_target_moved_since_the_base` |
| `work_resume.resume_settlement`: a moved target stops a resume that is not reconciling a landing (`WORK_RESUME_TARGET_MOVED`) | `test_work_resume::test_public_resume_refuses_changed_evidence_without_landing[target]` |

## C4. Git landed, Backlog not updated

A crash between the Git landing and the Backlog write is completed on resume. The resume proves the commit is in
the target, calls no model and no verifier, and does not land twice; a replay after the graph was written returns
the saved result. Cases below drive the native path.

| Guard | Cases that fail without it |
| --- | --- |
| `work_resume.resume_settlement`, `reconcile_backlog`: a `READY_FOR_LANDING` train whose target already holds the candidate goes to `land_train` (Backlog completed once) | `test_work_resume::test_public_resume_recovers_same_candidate_without_model_or_verifier_reexecution[git_landed]`, `[graph_written]` (they check the node is `done`, has one `[RELAY_LANDED]` note, and that the model, verifier and custody bytes are untouched) |
| same function: acceptance on disk means no re-execution | the same two cases (they replace `NativeWorkRunner.run`, `native_catalog.select`, `CoordinatorAuthorityGate.acquire`, `work_audit.run` and admission with failures) |
| Kernel `land_candidate_ref`: `ALREADY_LANDED` for the candidate itself or a descendant of it (equality and ancestry both removed fails) | `test_candidate_change::test_receipt_binds_verified_commit_and_landing_uses_same_object`; `test_work_resume::test_public_resume_recovers_same_candidate_without_model_or_verifier_reexecution[git_landed]` |
| `relay_mesh._land_train_locked`: a graph write that survived is accepted only while the target retains the candidate (`SURVIVING_COMPLETION_TARGET_NOT_LANDED`); no second CAS, no rewrite | `test_strict_cutover_qualification::test_disposable_strict_train_uses_one_candidate_through_admission_and_cas[human-audit-replay-diverged]`; also `[human-audit-replay]`, `[human-audit-legacy-replay]`, `[human-audit-replay-descendant]` |
| `work_resume.resume_settlement`, `LANDED` branch: returns the saved result only if the candidate is in the target history and the Backlog completion is this train's | `test_work_resume::test_resume_after_confirmed_landing_returns_result_or_refuses_inconsistent_evidence[none]`, `[descendant]` (positive controls, not mutated: they assert the saved result is returned); `[not_in_history]`, `[missing_completion]`, `[foreign_completion]`, `[missing_node]`, `[forged_signoff]`, `[duplicate_node]`, `[missing_note]` (refuse) |

## C5. Landing receipt and Work binding

What the run records. The run receipt is `run.json` in `.reconciliation/work-runs/<run_id>/`, beside the OS repository directory: `work_id`, `run_id`, `train_id`, `candidate_commit`, `admission` (`verdict`, `reason_codes`,
`audit`), `landing` (`landed`, `target_ref`, and `checkout` or `already_landed`) and `status` (`SETTLED`,
`LANDING_REFUSED`, `ADMISSION_<verdict>`). The base commit is in the train's `EXECUTION_DESCRIPTOR.json` and in
`CANDIDATE_CHANGE.json` (`base_commit`, `candidate_commit`, `candidate_tree`, `diff_sha256`, `changed_paths`,
`worktree_id`, `descriptor_sha256`); the receipt does not repeat it. Landed means the target ref equals
`candidate_commit` or descends from it. The Backlog node carries the binding in `provenance` and
`completion.dod_evidence`: `relay-signoff:{train, readyRevision, authority, capability_sha256, artifacts,
descriptor_sha256, candidate_sha256, candidate_commit}`.

What refuses a resume when the Work binding or the evidence changed: the saved `WORK_RUN.json` (work id, descriptor
sha, repository root, lease, node, limits), the descriptor, the capability and the Backlog completion.

| Guard | Cases that fail without it |
| --- | --- |
| `work_run.run_work`: the receipt names Work, candidate, admission and landing, and is saved | `test_work_run::test_run_settles_one_work_with_the_existing_train_lifecycle` |
| `relay_mesh._land_train_locked`: the Backlog completion binds descriptor, candidate sha and commit (strict train) | `test_strict_cutover_qualification::test_disposable_strict_train_uses_one_candidate_through_admission_and_cas[human-audit-normal]` |
| `work_resume.resume_settlement`: `WORK_RESUME_IDENTITY_CHANGED` (saved binding no longer matches the descriptor and train) | `test_work_resume::test_public_resume_refuses_changed_evidence_without_landing[origin]` |
| same: `WORK_RESUME_GRAPH_CHANGED` (descriptor names another graph) | `test_work_resume::test_public_resume_refuses_changed_evidence_without_landing[descriptor_graph]` |
| same: `WORK_RESUME_REPOSITORY_CHANGED` (`--repo-root` differs from the original) | `test_work_resume::test_resume_refuses_another_repository_than_the_original` |
| same: `WORK_RESUME_LEASE_CHANGED` | `test_work_resume::test_public_resume_refuses_changed_evidence_without_landing[lease]` |
| same: `validate_descriptor_for_train` (criteria changed in the graph) | `test_work_resume::test_public_resume_refuses_changed_evidence_without_landing[criteria]`; `test_work_resume::test_resume_after_git_landing_refuses_changed_authority[criteria]` |
| `relay_mesh._land_train_locked`: capability bound to the train revision (`readyRevision`) | `test_work_resume::test_resume_after_git_landing_refuses_changed_authority[changed_capability]` |
| same: capability bound to the bytes it approved (`AUDIT_REPORT.md` and the other digests); alone | `test_strict_cutover_qualification::test_disposable_strict_train_uses_one_candidate_through_admission_and_cas[human-audit-report-changed]` |
| `work_resume.resume_settlement`: Backlog completion must be this train's | the `[missing_completion]`, `[foreign_completion]`, `[forged_signoff]` cases under C4 |

## C6. FAULT matrix

The 6 fault points, 3 schedules and 2 platforms of AC-08 are mapped in [FLOW04_RECOVERY.md](FLOW04_RECOVERY.md), and
all 36 cells passed in [CI run 43208024513](https://github.com/augusto-scarvalho/tare.tools.os/actions/runs/43208024513)
of `main` at `8682f2d9`. FLOW-03 reuses that map and adds no cell. Its "Landing before backlog" rows are
`test_work_resume::test_public_resume_recovers_same_candidate_without_model_or_verifier_reexecution[git_landed]`,
`[graph_written]` and
`test_work_resume::test_resume_after_confirmed_landing_returns_result_or_refuses_inconsistent_evidence[descendant]`,
which this guide's C4 also lists.

## What the spec named and what exists

[SPEC-OS-EXECUTION-SETTLEMENT-001](../specs/SPEC-OS-EXECUTION-SETTLEMENT-001.md) names `relay/work_settlement.py`,
`tests/test_work_settlement.py` and `tests/test_work_run_faults.py`, and tags tests with
`@pytest.mark.verifies("SPEC-OS-EXECUTION-SETTLEMENT-001", "AC-0n")`. None of the files exists. The `verifies` marker
is used by other specs, but no test carries this spec's id; the criteria are carried by the modules and tests above.

| Spec | What stands in its place |
| --- | --- |
| `relay/work_settlement.py` | No module. `work_run.run_work` and `work_resume.resume_settlement` call `relay_mesh.admit_train` and `land_train`. |
| `tare.tools/work-settlement/1` receipt | No such schema. The run receipt (`tare.tools/work-run/1`, `run.json`), `CANDIDATE_CHANGE.json` (`tare.tools/candidate-change/1`), `ADMISSION_VERDICT.json` and the Backlog `relay-signoff:` binding together hold the facts. |
| `run_id`, `work_id`, `candidate_commit` | In `run.json`. |
| `audit_case_id`, `audit_verdict` | No equivalent. The code audit of `tare-ops run` is `receipt.audit` (`deployment`, `session`, `verdict`, `observation`) and `AUDIT_REPORT.md`; its digest is in the admission verdict (`audit.sha256`). A `tare-ops audit` case id is never recorded in a run. |
| `admission` (`by`, `authority`, `at`) | `ADMISSION_VERDICT.json` (verdict, policy, evidence and audit digests) and the capability's `authoritySource` (`SMART_POLICY` or `HUMAN_OPERATOR`). There is no `by` or `at`; the Backlog note and `completion.completed_at` carry the time. |
| `landing.target_ref` | `run.json` `landing.target_ref`. |
| `landing.base_commit` | No field in `landing`. It is `base_commit` in `EXECUTION_DESCRIPTOR.json` and `CANDIDATE_CHANGE.json`. |
| `landing.landed_commit` or `CAS_CONFLICT` | `landing.landed` is a boolean and the landed commit is `candidate_commit`. A refused CAS is `status: LANDING_REFUSED` with the Kernel reason (`BASE_REF_DIVERGED`) printed by `land_train`, not stored in the receipt. `CAS_CONFLICT` is not a name in the code. |
| `backlog_update` (`applied`, `pending`, `reconciled_after_crash`) | No field. Applied is the node `done` with the `relay-signoff:` binding; reconciled is `recovery.backlog_reconciled` with `recovery.git_already_landed` in the resume receipt. `pending` has no marker: the train stays `READY_FOR_LANDING`. |
| `cost` per attempt | Not a settlement field. Usage and cost are recorded by the run's inference observations (`inference_calls`). |
| Terminal state `REJECTED`, `EFFECT_UNKNOWN`, `EXECUTION_FAILED`, `INCONCLUSIVE_WITH_REVISIT_TRIGGER` | Not a run status. The run ends as `SETTLED`, `LANDING_REFUSED`, `ADMISSION_<verdict>`, `AUDIT_<verdict>`, `IMPLEMENTATION_<status>`, `CANCELLED`, `FAILED` and others listed in [WORK_RUN.md](WORK_RUN.md). |

## Known limits, not covered

- **Test platform.** The cases in this guide were run locally on Windows. They use no platform path and no network;
  their Windows and Linux CI receipts are the first run of this change.
- **Kernel's CAS lives in a satellite.** The guard of C3 and the idempotence of C4 are in
  `compute_plane/candidate_git.py`, not in this repository. The mutation was applied to a scratch copy of Kernel at
  `3e0c2fb`; this repository's tests pin its behaviour from outside. The atomic `update-ref` old value is the only
  defence against a race between the pre-check and the write, and no deterministic test races it.
- **Redundant layers are masked.** Removing only the Kernel pre-check, only its expected old value, only the
  `ALREADY_LANDED` equality, `relay_mesh._strict_candidate_context`'s verification, or only the `READY_FOR_LANDING`
  gate in `transition_train` (when the `work_run` check stays) leaves every test green, because another layer
  refuses first. Each is covered only through the combination above.
- **Resume checks without a case.** In `work_resume.resume_settlement`, the `load_node != origin['node']` check
  (`WORK_RESUME_EXECUTION_CONFIGURATION_UNAVAILABLE_OR_CHANGED`) and the first `WORK_RESUME_TARGET_MOVED` check run
  only when a Work control file exists. Removing either leaves the named cases green, because
  `validate_descriptor_for_train` and the later target check refuse the same cases. A change to the Work node that
  the descriptor does not carry (a title, for example) is not shown to be refused. The target checks in
  `work_recovery.py`, `work_control.py` and `human_wait.py` (the pre-acceptance resume and the control checkpoints)
  were not mutated here.
- **Advisory `CLOSED` is not authority by construction, not by a test of the whole path.** The case reports
  `authority: NONE` (tested) and no landing module reads it (imports checked by hand). No test builds a run in which a
  closed `tare-ops audit` case exists and asserts that nothing lands.
- **Two run paths.** The cases of C2 and C3 that call `run_work` with an injected implementer exercise the shared
  tail (admission, `admit_train`, landing). Native-only guards (work control checkpoints, human wait, the claim
  check, code audit) are exercised only by the cases marked native. The prepared-implementation path
  (`--prepared`) was not mutation-checked here.
- **Human sign-off path versus smart policy.** `tare-ops run` always lands through the smart policy
  (`admit_train`, `SMART_POLICY`). The strict-cutover cases for the stale base, the changed audit report and the
  replays drive `land_train` with a human nonce, which `relay_mesh.py land` and the console use. The capability's
  digest check is isolated only on that path: on the smart path the same digests (`admissionDigest`,
  `landingEvidenceDigest`, `candidateDigest`) can be removed without a test failing, because the admission bundle
  recomputation refuses first.
- **The receipt does not store the landed commit or the base.** They are derivable (see above) but not a field of
  `landing`, and a refused landing stores no reason.
- **Not a claim about distributed atomicity.** Git and the Backlog are two stores. After a landing without an
  update the answer is to reconcile (C4), which the cases above show on one machine with real Git and a real
  coordinator database; concurrent hosts landing the same Work are outside it.
