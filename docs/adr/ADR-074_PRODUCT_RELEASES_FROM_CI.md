---
id: ADR-074
title: CI builds and publishes the product releases
status: ACCEPTED
admission: OPERATOR_AUTHORIZED
---

# ADR-074: CI builds and publishes the product releases

Date: 2026-11-04. Authority: operator decision after tare.tools.os became public.

## Context

Installed copies of tare update from the `product-*` releases of tare.tools.os. A scheduled task on the
maintainer's laptop (`Tare-Publish`) built and published them every two minutes with the maintainer's GitHub
credentials, then started the updaters of the maintainer's machines over ssh. The repository is public now: anyone
installs from these releases, and a release depended on one laptop being on, on its credentials, and on a build
nobody else could check.

## Decision

1. `.github/workflows/release.yml` publishes. It runs on a GitHub-hosted Windows runner (the bundle is for Python
   3.12 on Windows) when tare.tools CI completes green on `main`, for the commit CI tested
   (`publish_product.py --commit`), or by hand (`workflow_dispatch`, optionally `force`).
2. The publishing logic stays in `scripts/ops/publish_product.py`: mirrors, the release lock, the reproducible
   build, the trial installation, the CI check (`ci_passed`) and the `CI passed on main` line in the notes that
   installed copies look for. What the laptop kept on disk travels with the releases: each release carries
   `published.json` (the heads it was built from), and the next run reads it from the newest release to tell whether
   anything packaged changed.
3. The workflow attests the bundle's build provenance (`actions/attest-build-provenance`); anyone can check a
   downloaded bundle with `gh attestation verify bundle.zip -R augusto-scarvalho/tare.tools.os`.
4. Outside GitHub Actions the publisher publishes nothing unless told `--here`. The maintainers' scheduled task runs
   main's publisher, so it stops publishing by itself; it can then be deleted.
5. Machines are no longer told to update over ssh: each `Tare-Update` checks every five minutes.

## Consequences

- No release depends on a maintainer's machine or credentials; the workflow's token publishes.
- A release exists only for a commit CI passed on `main`, built where anyone can read the log.
- The first release from CI finds no `published.json` on the newest release and publishes.
