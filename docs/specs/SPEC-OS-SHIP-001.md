---
id: SPEC-OS-SHIP-001
title: Ship, delegated delivery through remote CI/CD
status: ACCEPTED
admission: OPERATOR_AUTHORIZED
acceptance_criteria:
  AC-01: tare ci pr and tare ship name the repository in every gh call and target its default branch read from GitHub, never a hard-coded main.
  AC-02: The CHANGELOG union, disposition manifest and work graph merge helpers run only when their files exist in the tree.
  AC-03: Preflight refuses a push to a public repository when an author or committer email in the pushed range is not a GitHub noreply address or an allowed identity, naming the commit; the pre-push hook uses the same check.
  AC-04: Preflight reads branch protection and repository settings (required checks, reviews, signed commits, linear history, merge methods, auto-merge) and either chooses a merge method (merge commit, then squash, never rebase) or refuses with the reason.
  AC-05: A pull request or repository that no longer exists or is archived ends GONE within one round; its shepherd beat and every alert of that pull request are removed.
  AC-06: Revive never restarts a beat whose checkout is gone and whose repository is unknown; tare ci dismiss removes one alert and tare ci check --prune removes alerts whose subject is gone.
  AC-07: A second tare ci pr or tare ship on a pull request with a live shepherd starts no second shepherd.
  AC-08: Triage writes a receipt with class, rule and evidence lines for pinned_ref_missing, superseded, infrastructure, queued, required_check_missing, permission, hook_refused, conflict, base_red, flaky and regression.
  AC-09: A failed log excerpt is captured when the failure is seen, bounded in size, stripped of token-like strings and stored marked as untrusted.
  AC-10: .tare/ship.toml is optional; known keys are validated, an unknown key is refused, and the verification command it declares is used for regression receipts of projects whose failures are not pytest ids.
  AC-11: Ship state lives on disk under the alerts lock; a restarted process resumes a pull request from its state without opening, pushing or rerunning twice.
  AC-12: The stop hook holds a session only for a decision Ship needs from it or for GIVEN_UP, never for WAITING or FIXING.
  AC-13: The rerun mode (strict or merge_then_fix) is chosen by the coordinator or Jev, recorded in the receipt and limited to one rerun per failure; a flaky class always opens or links a root-cause Work.
  AC-14: A fix runs as an ordinary Work in its own worktree; Ship pushes only to branches its receipts created and proposes a stacked pull request on anyone else's branch.
  AC-15: Fix rounds are bounded; the same class failing twice ends GIVEN_UP with a backlog item.
  AC-16: A push that finds the remote branch moved re-triages and never overwrites the remote.
  AC-17: Each actionable review comment becomes a Work and a reply in its thread; Ship never approves its own pull request or a fork's workflow run.
  AC-18: After a merge Ship watches the merge commit's runs and the workflows listed under after_merge and reports their outcome without deploying anything.
  AC-19: A default branch that turns red after a merge yields a revert proposal that needs operator approval.
  AC-20: The /ship TUI flow carries the TUI's invocation id through flow invocation and recovery and restores its selection through the demand console.
---

# Ship, delegated delivery through remote CI/CD

Decision: [ADR-072](../adr/ADR-072_SHIP_DELEGATED_DELIVERY.md). Owner Works: the `ship-*` items of the
`15 Flow program / Work Console` cluster in `work-graph.json`.

## Scope by phase

- **Phase 1, the generalized `ci_watch`** (AC-01 to AC-12, AC-20): `tare ci pr` for any GitHub repository, the alert
  and shepherd lifecycle, preflight, rule-based triage with receipts, `.tare/ship.toml`, the per-pull-request state,
  `tare ship` and the `/ship` panel. No model and no Work.
- **Phase 2, diagnosis and fixes** (AC-13 to AC-17): the explorer-role diagnosis of regressions and unclassified
  failures, the rerun modes, the fix Work loop, stacked pull requests on branches Ship does not own, and review
  comments.
- **Phase 3, after the merge** (AC-18, AC-19): watching the merge commit and the declared workflows, and revert
  proposals.

## Commands

```text
tare ship [--repo-dir DIR] [-R OWNER/REPO] [--pr N] [--wait]   push, open or adopt the pull request, follow it
tare ship status [--json]                                       every pull request Ship follows, with its state
tare ci pr ...                                                  unchanged surface, generalized (AC-01, AC-02, AC-07)
tare ci dismiss KEY                                             end one alert (AC-06)
tare ci check --prune                                           drop alerts whose subject is gone (AC-06)
```

`tare ship` without `--wait` returns once the pull request is open or adopted and its shepherd runs. With `--wait`
it returns when the pull request reaches `MERGED`, `GONE`, `GIVEN_UP` or `PREFLIGHT_REFUSED`, with that state's
exit code.

## State

One record per pull request in `ships.json`, beside `alerts.json` and under the same lock:

```json
{"repo": "owner/name", "number": 12, "branch": "fix/x", "owned": true, "state": "RED",
 "class": "flaky", "round": 1, "rounds": 3, "rerun_mode": "strict", "decided_by": "coordinator",
 "receipts": ["<run>-<attempt>.json"], "updated_at": 1793800000}
```

States: `PREFLIGHT_REFUSED`, `OPENED`, `WAITING` (`queued`, `running`, `behind`, `review`, `approval`), `RED`,
`FIXING`, `MERGED`, `GONE`, `GIVEN_UP`. `MERGED`, `GONE`, `GIVEN_UP` and `PREFLIGHT_REFUSED` are terminal; a terminal
state removes the shepherd beat and every alert of the pull request (AC-05). Only `RED` and `FIXING` involve a model
(phase 2).

## Triage rules (phase 1)

| Class | Rule (evidence the receipt quotes) |
| --- | --- |
| `pinned_ref_missing` | `upload-pack: not our ref` in a checkout or submodule step |
| `superseded` | the run was cancelled and a newer run exists for the same workflow and ref |
| `infrastructure` | `startup_failure`; a failure in `Set up job`; DNS or TLS errors (`Could not resolve host`, `schannel`, `SSL`); an incident on the GitHub status page covering Actions |
| `queued` | queued past a limit, with the cause when visible: no matching runner, offline self-hosted runners, the concurrency limit |
| `required_check_missing` | a context branch protection requires that no check run, status or workflow on the head produces |
| `permission` | `403`, `Resource not accessible by integration`, a missing token scope, a run without secrets (dependency bots, forks) |
| `hook_refused` | a local `pre-push` or remote pre-receive refusal, with the hook's own message |
| `conflict` | the pull request is not mergeable and the merge helpers cannot resolve it |
| `base_red` | the failing test or job also fails on the base branch's latest run |
| `flaky` | the same tree passed this check before (an earlier attempt or the pull request run) |
| `regression` | anything else with failed test ids; the receipt carries `verification_checks` built from them (`python -m pytest <ids> -q`, or the declared command) |

Rules run in the table's order; the first match wins. An unmatched failure is `unclassified`.

## Receipts

One JSON file per settled run attempt: repository, pull request, run, attempt, head SHA, class, rule, evidence lines
(at most 40, token-like strings replaced by `[redacted]`), the excerpt's size and SHA-256, `untrusted: true`, the
state transition and the time. Receipts are evidence; they never grant authority.

## `.tare/ship.toml`

Optional, in the project's repository. Every key is optional; an unknown key is an error (AC-10).

```toml
# What tare's Ship cannot discover from GitHub.
[verify]
command = ["npm", "test", "--"]      # failed test ids are appended; pytest projects need none

[rerun]
default = "strict"                    # or "merge_then_fix"; the coordinator or Jev may choose per demand

[merge]
method = "merge"                      # "merge" or "squash"; never "rebase"

[identity]
allow = ["release-bot@example.org"]   # besides GitHub noreply addresses

[after_merge]
watch = ["release.yml"]               # workflows to follow on the merge commit (phase 3)
```

## Boundaries

- No force-push, no `--no-verify`, no bypassing branch protection, no deleting what Ship did not create, no approving
  its own pull request or a fork's workflow run, no deployment, no handling of secrets or tokens.
- Every `git` and `gh` invocation goes through one allowlisted function.
- CI logs, review comments, commit messages and fork output reach a model only as quoted, untrusted data.

## Verification

Offline fixtures only: a scripted `gh` (as `tests/test_ci_watch.py` does), fixture repositories and recorded run,
job and log payloads. No test reaches GitHub. Each acceptance criterion has at least one test that fails without
its behavior.
