# `tare-ops run <WorkId>` — run and settle an explicit Work

A managed Work accepts the command controls the conversation already uses:
`--command-review codex --command-isolation mxc --command-classifier jev
--command-review-policy on_demand`, with finite `--max-command-reviews` and
`--command-review-seconds`. In the TUI, save the selection in Permissions and
start a new Work. The implementer and the independent verification of the candidate
use the same policy and the same review budget, without widening `tracked_paths`.
The host keeps the full stdout/stderr and the process cleanup receipt.

The policy and the reviewer identity are frozen before the plan audit
and any human wait. Resumption uses these original values; it rejects
overrides and changes to the pinned executable. Custom paths or prepared
incorporation paths are refused when protection is selected.
Without a selection, the old paths remain compatible.

`--retry-plan` (New plan review on Work resume in the TUI) starts a separate
review after the previous one ends or exhausts its limits. It preserves the history
and reuses the saved limits; an ordinary resume does not renew the deadline. The new
review requires confirmed local cleanup and no pending or uncertain tool effect.

The qualified profile requires Windows, a local execution repository and named
checks without an interactive terminal. A file permission never opens its
folder: MXC may deny creating a file through a command. The edit tool
can create the declared file before verification; a refused command
does not trigger a fallback without isolation.

First slice of the Work Console (FLOW-02, [SPEC-OS-WORK-CONSOLE-001](../specs/SPEC-OS-WORK-CONSOLE-001.md)).
It is a composition of the train cycle that already exists; no new owner, no
new authority. The final landing goes through the `SMART_POLICY` capability of
`relay_mesh.admit_train`, exactly as before.

```bash
python tare_ops.py run <WorkId> [--deployment local-qwen] [--repo-root C:/repo] [--verify "unit=python -m pytest -q"] [--dry-run]
python tare_ops.py status <WorkId>
```

## Stages and who runs them

| Stage | Reused owner | What `run` adds |
|---|---|---|
| ground | node of `work-graph.json` | requires a single `repository_scopes` with `target_paths`, `exit_criteria` equal to the check names, specific `grounding_refs` (`repo:path`), and `metrics.verification_checks` or `--verify`; asks the Backlog whether the Work is `READY` (`completion.status` `NOT_DONE`, `work_status` `planned`) and stops with `WORK_NOT_EXECUTABLE` before any session |
| descriptor | `train_execution.create_execution_descriptor` | derives the payload from the node: repository base and branch, `tracked_paths`, checks (argv, no shell); writes the Work's `spec_ref` and `grounding_refs` to `SOURCES.json` in the train, read from Git where they live (the execution repository at the base commit, a satellite at its pinned gitlink), and names it in `library_manifest` |
| plan audit | managed `plan-auditor` session on the chosen deployment | projects the artifact into `PLAN_AUDIT_REPORT.md` with the marker that `audit_valid` requires; `REVISE` stops everything |
| workspace | `strict_workspace.provision_train_workspace` | ephemeral worktree under the run's custody |
| implement | managed `implementer` session | `writable` bounded by `tracked_paths`, the descriptor's verifiers as `verifiers`, `allow_exec` off |
| candidate | `candidate_change.materialize_verify_and_record_candidate` | runs the checks on the candidate's bytes and writes `CANDIDATE_CHANGE.json` |
| evidence | `relay_mesh.materialize_admission_evidence` | the implementer identity comes from the role deployment via `attest_identity` |
| admission | `relay_mesh.prepare_admission_verdict` | current policy (`audit_mode` defaults to `optional`) |
| landing | `relay_mesh.admit_train` | CAS on the ref, `work-graph.json` marked, train archived in `relay/history/` |

stdout carries only the receipt; the `relay_mesh` transition logs go to stderr.

The `tare.tools/work-run/1` receipt lives in
`<control-root-parent>/.reconciliation/work-runs/<run_id>/run.json` (workspace custody,
outside the repository). The control root is the OS checkout that `run` operates on, so
for a source install this is the `.reconciliation` folder next to the clone. The receipt
holds the stage, candidate, admission verdict, landing and the cost of each managed
session in its custody. `status` reads everything from custody; it does not need a
live session.

## Terminal states

`SETTLED`, `DRY_RUN`, `PLAN_REJECTED`, `IMPLEMENTATION_<status>`,
`ADMISSION_<verdict>`, `LANDING_REFUSED`, `FAILED` with a named `error`.
A failure before the workspace does not create a worktree; a second `run` of a Work whose train
exists is refused (`TRAIN_EXISTS_USE_RESUME`) and `--resume` continues it
([FLOW04_RECOVERY](FLOW04_RECOVERY.md), [FLOW03_SETTLEMENT](FLOW03_SETTLEMENT.md)).

## What it does not do

- It does not choose the Work: `WorkId` is mandatory.
- It does not run an independent code audit by default: `audit_mode` `optional`
  from the landing policy; `required` requires turning on the `tare-ops audit` track (G3
  of the reconciliation map).
- It does not call a vendor CLI; the roles come from `relay/role_deployments.json`.
- It does not treat Git and the graph as one transaction: a crash between the landing and the
  graph write is completed by `--resume`, which reconciles and never implements again
  ([FLOW03_SETTLEMENT](FLOW03_SETTLEMENT.md), C4). After landing, it refreshes in the live checkout only the
  `tracked_paths` without local edits (`landing.checkout.refreshed`); a locally
  edited path stays as it is and appears in `landing.checkout.stale`.
  Refresh threat model: `actor` the local operator; `window` the duration of the
  `run` itself, between the CAS and the refresh; `access` the worktree and the index of the
  Work's `tracked_paths`. A "local edit" is whatever git tells apart from the base by
  content semantics (index, filtered bytes, entry type, executable
  bit with `core.filemode`, an untracked entry where the landing creates a
  file or directory). A line-ending-only difference that the filters
  normalize back to the base blob is not an edit and is not protected. States
  outside this window or this access are residual, not defects.

Tests: `tests/test_work_run.py`, with an injected plan-auditor, implementer and evidence;
the real evidence path requires the `backlog` and `specgraph` CLIs.

## First settled Work (dogfood, 2026-09-07)

`research-shared-explorer-lock` landed on `main` as `ad25ce8` on the eighth
attempt, implemented by the local Qwen at zero cost; the history of the seven failures
and the fixes was in `docs/operations/HANDOFF_DELEGATION_2026-09-07.md`, section 3d, since
removed (Git history keeps it). Lessons that
became contract:

- the node needs specific `grounding_refs`, `exit_criteria` equal to the check
  names and a concrete instruction (files, goal, verifiers);
- on Windows, custody in `<control-root-parent>/.reconciliation/work-runs/<run>/workspaces/`
  plus long repository paths goes over 260 characters; the Kernel turns on
  `core.longpaths` in the isolated git (`compute_plane/workspace.py`).

## Compatibility with SpecGraph admission

The default evidence now requires a match of the source and criteria bytes
and a real observation of the executor. The composition tests with injected evidence
do not replace this verification; isolated READY metadata can produce a refusal.
The implementer uses resolve when available, with recovery limited to the
authorized paths on MISS/STALE/TIMEOUT/UNAVAILABLE.


## Descriptive review of completed Works (FLOW-05)

`python scripts/round_table/report_round_table_telemetry.py --source <custody-or-receipt> --review-completed-works`
reuses the offline projection of up to ten distinct runs; an explicit `--source` is mandatory.
A resume belongs to the original run and does not count as a new delivery. Bounded historical
project/runtime declarations are preserved when consistent; a missing value, a malformed
payload or a conflict stays unknown. SETTLED confirms closure, not quality.

The existing `/usage` route keeps the conversation and flow selection and adds a summary
of the control-owner's saved receipts, which may span several projects. It reads only
`<control-root-parent>/.reconciliation/work-runs/<run>/run.json` and the stage `usage.json`
snapshots of finished runs, following the explicit resume lineage.
It does not read journals/settings of active sessions, workspaces or experiments, nor
call models or write receipts. While busy, it keeps
the panel unavailable; an optional failure does not remove the other usage data.

Declared project/runtime metadata is opt-in in this review; the default telemetry
exporter still omits private project paths.

It shows Work names, attempts/status, project/model when recorded, attributed
calls and coverage, and reported USD separately from additional estimates. Full costs,
subscription quota, control/tools/infrastructure, human work, quality and independent
audit remain unknown without their own evidence. Fewer than ten real deliveries
(including 6/10) keep the cohort partial and **neither block nor authorize** new Works.

## Review of five Works across providers (FLOW-06)

The same `/usage` shows **Provider pilot**, a descriptive projection of up to five
distinct Works in the recent window of ten finished runs. Resumes and new
runs of the same `work_id`/`train_id` do not complete the count again. The
recent window helps find candidates; it does not prove representativeness.

To review an explicit set, use:

```powershell
python scripts/round_table/report_round_table_telemetry.py --source <custody-or-receipt> --review-provider-pilot
```

Repeat `--source` to delimit existing receipts. Choose five ordinary Works
with comparable paths and difficulty, justify the selection and review the recorded results
and audits. Include failed attempts and unknown costs. Do not
duplicate tasks or run a campaign only to fill the count. Fixtures
and adapter smoke tests do not replace this cohort.

New receipts record `task_class` only when it is explicitly declared in
the Work's `metrics.task_class`, using the free-form field the Backlog already accepts.
`runtime_identity` contains only the deployment, requested model and protocol
of the frozen profile, with no endpoint, credentials or free-form parameters. It is a
declaration of the requested route, not an attestation of the provider/model actually
served. Resumes use the frozen origin, without querying the current catalog.
`recorded_audit_outcome` tells apart the recorded plan, code and
admission verdicts; it does not attest the quality or independence of the audit. Missing
historical data stays unknown; the review does not rewrite old receipts.

Counting five closures does not qualify FLOW-06. Representativeness, equivalence
across providers and quality remain unknown without a review grounded in the
real Works. No model preference, authorization or block derives from this
summary. Review again when there is enough new ordinary evidence.

The destination is the existing panel and the explicit CLI query: the projection shares
the receipts/snapshots FLOW-05 already reads and creates no collector, schedule, journal
copy or automatic file. JSON/HTML are exported only on request; since
project metadata is opt-in, keep these exports outside the repository.
