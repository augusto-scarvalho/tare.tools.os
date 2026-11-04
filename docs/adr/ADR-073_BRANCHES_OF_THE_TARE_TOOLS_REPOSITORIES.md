---
id: ADR-073
title: tare.tools repositories change main through short-lived branches and pull requests
status: ACCEPTED
admission: OPERATOR_AUTHORIZED
supersedes: [ADR-068]
---

# ADR-073: Branches of the tare.tools repositories

Date: 2026-11-04. Authority: operator decision, reviewing ADR-068 with the agent that published the repositories.
Supersedes [ADR-068](ADR-068_UNIVERSAL_BRANCH_STANDARDIZATION_AND_CI_PIPELINES.md).

## Context

ADR-068 (2026-08-21) made `main` every repository's default branch and `dev` the integration branch all work passes
through, required CI on both, said work branches are deleted when their pull request merges, and said automated
checks enforce all of it. When the repositories were published on 2026-11-04 the practice was different: no
repository had a `dev` branch, and every change went from a short-lived branch through a pull request into `main`;
tare.tools.local-labs still used `master`; merged branches stayed, because the setting that deletes them was off; and
no check enforced any of it. ADR-068 also links deliberation records that are no longer published.

## Decision

1. The default branch of every tare.tools repository is `main`. tare.tools.local-labs renamed `master` to `main` on
   2026-11-04; GitHub redirects links and fetches that use the old name.
2. A change reaches `main` only through a pull request from a short-lived branch, and only with the checks its
   branch protection requires. There is no integration branch: `dev` is no longer required. Workflows and hooks that
   also accept `dev` may keep doing so; no repository has to create it.
3. GitHub deletes a pull request's branch when it merges ("Automatically delete head branches"), in every repository
   whose settings can change (the archived tare.tools.harness cannot).
4. Branch names carry intent by prefix (`feat/`, `fix/`, `docs/`, `ci/`, `test/`, `chore/`) and a date when useful;
   nothing enforces them.
5. Projects other than the tare.tools repositories keep their own branches: Ship
   ([ADR-072](ADR-072_SHIP_DELEGATED_DELIVERY.md)) reads each repository's default branch from GitHub.

## Consequences

- ADR-068's invariants are replaced by the practice above; no automated enforcement is claimed. Branch protection
  and the pull request flow (`tare ci pr`) are what hold it.
- Clones made before the rename keep working through GitHub's redirect; a fetch shows `origin/main`.
- A merged branch disappears from GitHub; its commits stay in `main`.
