---
id: ADR-072
title: Ship carries landed work through remote CI/CD as a deterministic flow
status: ACCEPTED
admission: OPERATOR_AUTHORIZED
---

# ADR-072: Ship, delegated delivery through remote CI/CD

Date: 2026-11-04. Authority: operator decision, after a review of the proposal by an independent model at extra-high
effort. Contract: [SPEC-OS-SHIP-001](../specs/SPEC-OS-SHIP-001.md).

## Context

A Work ends at `settle`: admission, landing on a local branch and reconciliation ([WORK_RUN.md](../guides/WORK_RUN.md)).
What comes after (pushing, opening the pull request, watching CI, telling a regression from a flaky test or an
incident, fixing, merging, watching the default branch and the release) is done by hand, or by an agent session
following instructions. For tare's own repositories `relay/ci_watch.py` already pushes and opens a pull request with
auto-merge (`tare ci pr`), keeps it up to date (the shepherd), raises alerts on red runs and holds a Claude Code turn
while that session's alert is open (`tare ci stop-hook`).

Publishing the repositories on 2026-11-04 exercised that path and showed where it breaks:

- personal email addresses reached public repositories through local commits and GitHub web merges, and three
  repositories had to be recreated again;
- workflows pinned commits that no longer existed (`upload-pack: not our ref`);
- a GitHub Actions incident queued every job for about 40 minutes, and local DNS failures looked like CI failures;
- two timing-sensitive tests failed on slower runners and passed on rerun; each needed a root-cause fix;
- shepherds and alerts of pull requests in a deleted repository kept reviving and held the agent's turn until their
  files were edited by hand;
- a watcher process was killed when the machine ran low on memory;
- required check names changed with the workflows, and branch protection had to follow;
- permission prompts and blocked actions repeatedly handed work back to the operator.

`ci_watch` also assumes tare's own conventions: a `main` base, a CHANGELOG union merge, the disposition manifest,
`work-graph.json`, and `gh` calls that rely on the checkout instead of naming the repository.

## Decision

1. **Ship is a flow with a deterministic core, not a skill.** State, waiting, safety limits and evidence are code with
   receipts. A model is used only to diagnose a failure the rules cannot classify and to fix it through an ordinary
   Work. A prompt-only skill cannot enforce "no force-push" or "never retry to obtain green".

2. **It extends `tare ci` and adds no parallel machinery.** `ci_watch` is generalized to any GitHub repository: the
   default branch, merge methods, visibility and required checks come from the repository and its branch protection;
   tare's merge helpers (CHANGELOG union, disposition manifest, work graph) apply only when their files exist; every
   `gh` call names the repository. A fix is a Work (`tare run`) on the pull request's branch in its own worktree;
   `work.execute/v1` stays the only workflow operation, and Ship calls it the way `work_sequence` does.

3. **One state per pull request,** kept beside `alerts.json` under the same lock: `PREFLIGHT_REFUSED`, `OPENED`,
   `WAITING` (queued, running, behind, review, approval), `RED` (with a class), `FIXING` (Work, round n of N),
   `MERGED`, `GONE`, `GIVEN_UP`. `GONE` (the repository or pull request no longer exists or is archived, or its
   checkout is gone with no repository recorded) and `GIVEN_UP` are terminal: they remove the shepherd's beat and every
   alert of that pull request. An alert ends when its subject turns green, ends terminal, no longer exists, or the
   operator dismisses it.

4. **Rules first, model second.** Triage classifies a red run from the run, its jobs, structured reports (JUnit
   artifacts) when the project publishes them, and an excerpt of the failed log. Each classification is a receipt
   naming the class, the rule and the evidence lines. Classes: `pinned_ref_missing`, `superseded`, `infrastructure`
   (startup failure, DNS or TLS, a GitHub incident), `queued` (no runner, an offline self-hosted runner, the
   concurrency limit), `required_check_missing` (no workflow produces a required check), `permission` (403, a missing
   token scope), `hook_refused`, `conflict`, `base_red` (the same failure on the base branch, so not this change's),
   `flaky` (the same bytes passed before) and `regression`. Only `regression` and unclassified failures reach a model:
   an explorer-role session with no authority, given the excerpt and the diff, that returns a hypothesis and the checks
   that would verify a fix. It never changes state.

5. **CI logs, review comments, commit messages and fork output are untrusted input.** They reach a model only as quoted
   data; nothing they contain is executed or followed. Excerpts are captured when the failure is seen (logs and
   artifacts expire), bounded in size and stripped of token-like strings before they are stored.

6. **The diagnostic rerun has two modes, chosen per demand.** `strict` turns auto-merge off for the rerun and back on
   after; `merge_then_fix` lets a green rerun merge and opens the root-cause Work afterwards. The coordinator or Jev
   chooses from the failure class, the branch protection and the work's criticality, and the receipt records the
   choice; `.tare/ship.toml` sets the default. In both modes the code allows one rerun per failure, and a `flaky` class
   always opens a root-cause Work (or links the open one when the same test fails again).

7. **Ship pushes only to branches it created.** On a branch someone else is working on, it proposes the fix as a pull
   request stacked on that branch, or asks. A push that finds the remote branch moved re-triages; it never overwrites.

8. **Parallel by default.** Ship runs detached from the session that started it, outlives that session and the
   process (state on disk, idempotent resumption through the existing revive), and notifies on the transitions that
   need someone: red, merged, given up, decision required (the TUI `/ship` panel, a desktop notification, an event the
   coordinator consumes). `tare ship --wait` blocks until a terminal state, for scripts. Work that depends on a merge
   declares the dependency in the backlog instead of waiting. The stop hook holds a session only in states that need
   that session (a decision, `GIVEN_UP`), never in `WAITING` or `FIXING`.

9. **Identity: prevent, never rewrite.** Before a push to a public repository, every author and committer email in the
   pushed range must be a GitHub noreply address or one `.tare/ship.toml` allows; otherwise Ship refuses and names the
   commit. The same check ships as a `pre-push` hook, so pushes by hand are covered too. Ship never rewrites published
   history: on a public repository a force-push does not erase a leaked commit (the pull request timeline, the events
   API and the commit's SHA keep it), so a leak found after a push stops the flow and becomes an operator decision.
   Merges made by GitHub carry the account's commit email, so the account keeps it private.

10. **Discover what GitHub knows; declare only the rest.** GitHub holds visibility, the default branch, protection,
    required checks, merge methods and environments, and Ship reads them again every round (they can change while a
    pull request is open). What discovery cannot find goes in `.tare/ship.toml`, versioned with the project and read
    by code: the verification command for projects whose failures are not pytest test ids, the default rerun mode,
    the preferred merge method, allowed commit identities and an optional `[after_merge]` list of workflows to watch.
    The project's `AGENTS.md` points to it in one line.

11. **Deployment stays outside, with a path open.** After a merge Ship watches the merge commit's runs and the
    workflows `[after_merge]` names, and reports. Deployment approval stays in GitHub Environments; tare's product
    release keeps its own gate (`scripts/ops/publish_product.py`, `relay/native_product.py`). A default branch that
    turns red after a merge becomes a revert proposal that needs operator approval.

12. **Hard limits are code:** no force-push; no bypassing branch protection or hooks (`--no-verify`); no deleting what
    Ship did not create; no handling of secrets or tokens (expired credentials or a missing scope stop the flow as
    `AUTH_REQUIRED`); no approving its own pull request or a fork's workflow run; no deployment. Every `git` and `gh`
    invocation goes through one allowlisted function, which also gives permission prompts one stable surface.

13. **Branch names:** tare.tools repositories change `main` through short-lived branches
    ([ADR-073](ADR-073_BRANCHES_OF_THE_TARE_TOOLS_REPOSITORIES.md)); for any other project Ship uses the repository's
    own default branch.

## Situations considered

Phase 1 is the generalized `ci_watch` with its lifecycle, preflight, rule triage, project configuration, state and
commands; phase 2 is the model diagnosis with the fix and review loops; phase 3 is after-merge watching.

| Situation | Handling | Phase |
| --- | --- | --- |
| A required check is never reported (path filter, renamed or disabled workflow, protection changed) | `required_check_missing`, from the protection's contexts against the head's check runs, statuses and workflows | 1 |
| Checks from other services (commit statuses from deployments, coverage, external CI) | read with the check runs; without logs, the status and its link are reported | 1 |
| A fork's workflow waits for approval | `WAITING` (approval) and a notification; Ship never approves | 1 |
| Required reviews, code owners, conversation resolution | `WAITING` (review) and a notification naming the requested reviewers | 1 |
| Review asks for changes | one Work per actionable comment, a reply in its thread; never approves itself | 2 |
| Auto-merge disabled in the repository, or a merge queue | merge when green if policy allows, else wait; merge-queue runs are watched like others | 1 |
| Signed commits, linear history, allowed merge methods | preflight reads protection; merge commit first, then squash, never rebase; unsigned commits are refused with the reason | 1 |
| Draft pull request | watched; never merged until its owner marks it ready | 1 |
| A run cancelled by concurrency | `superseded` when a newer run exists on the same ref, else infrastructure or operator | 1 |
| A GitHub incident or a local network failure | the GitHub status page is checked; local DNS/TLS errors are reported as local; backoff | 1 |
| API rate limits | backoff, never tight loops; the receipt notes the wait | 1 |
| Queued runs (offline self-hosted runner, concurrency limit, minutes budget of private repositories) | `queued` with the cause; budgets are reported, not managed | 1 |
| Logs and artifacts expire | the excerpt is captured into the receipt when the failure is seen | 1 |
| A merge conflict the merge helpers cannot resolve | `conflict`; a resolution Work on Ship's own branch, a question on anyone else's | 2 |
| The base branch is already red | `base_red`: the pull request waits and the base failure gets its own item, never a fix inside an unrelated change | 1, item in 2 |
| A scheduled run fails on the default branch | an item and a notification; no pull request is involved | 1 |
| Two green pull requests turn the default branch red | a revert proposal with operator approval, and a fix Work | 3 |
| Someone pushes to the branch while Ship is fixing it | the push is refused as non-fast-forward and Ship re-triages; nothing is overwritten | 2 |
| Two sessions or agents ship the same pull request | one owner, by the shepherd's beat key; a second command attaches to it | 1 |
| Stacked pull requests | retargeted to the base when the lower one merges | 2 |
| A change spans repositories (a satellite merges, the OS pin must move) | a chained ship: the merge in one repository creates the follow-up Work in the other | later |
| Dependency-update bots | shepherded under the same policy; their runs have no secrets, which classifies as `permission` | 2 |
| Secret scanning push protection refuses a push | stop; the secret is never echoed; an operator decision | 1 |
| Instructions planted in logs, comments or fork output | untrusted data only (decision 5) | 1 |
| Expired credentials or a missing scope (`workflow` to push workflow files) | `AUTH_REQUIRED`; the operator signs in again; Ship never handles tokens | 1 |
| A repository hook refuses a push (a changelog guard) | `hook_refused` with the hook's message, then a Work; never `--no-verify` | 1, fix in 2 |
| Commit message and trailer conventions (sign-off, conventional commits, no session links) | the project's hooks and `.tare/ship.toml`; Ship adds no trailer of its own | 1 |
| Self-hosted runners on a public repository | a preflight warning: a fork's code could run there | 1 |
| A failure on one platform only (paths, line endings) | the receipt names the platform; triage proceeds as usual | 1 |
| The repository is archived, renamed or transferred, or its default branch is renamed | archived ends `GONE`; renames follow GitHub's redirect and update the recorded name; the default branch is read again every round | 1 |
| The session ends or is compacted, or the machine sleeps or runs out of memory | state on disk; Ship resumes; notifications wait for a reader | 1 |
| The operator is away | notifications queue and decisions wait; nothing is decided on their behalf | 1 |
| An urgent fix | ordered first among waiting pull requests; protection is never bypassed | 2 |
| Release and deployment | `[after_merge]` workflows are watched; approvals in GitHub Environments; a rollback only through a declared procedure with approval | 3 |
| Hosts other than GitHub | out of scope now; provider calls stay behind the allowlisted function | later |

## Consequences

- Delivery becomes state with receipts that the operator, the TUI and the coordinator can read, instead of a session
  waiting on `gh` or an operator reading logs.
- `tare ci pr` remains the command for tare's own repositories and becomes the generalized one for any project.
- The alert and shepherd lifecycle is fixed first: Ship adds states, and a state that cannot end would trap sessions.
- Model cost is bounded to regressions and unclassified failures, with bounded rounds.
- Only GitHub is supported at first; everything provider-specific stays behind one function.
