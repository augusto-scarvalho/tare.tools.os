---
id: ADR-071
title: The GPU node service lives in tare.tools.os as node/
status: ACCEPTED
admission: OPERATOR_AUTHORIZED
supersedes_in_part: [ADR-049]
---

# ADR-071: The GPU node service lives in tare.tools.os

Date: 2026-11-04. Authority: explicit operator decision before the repositories are published.

## Context

[ADR-049](ADR-049_REPO_FEDERATION_AND_ANTI_DRIFT_GOVERNANCE.md) makes a satellite an autonomous, reusable library that
the OS consumes, and keeps what is specific to infrastructure, the local machine or the cluster in the OS. The node
service (model gateway, shared GPU lease and its `gpu` command, ComfyUI hook, installer and updater) left
tare.tools.local-labs on 2026-10-09 and became the satellite tare.tools.node. It never was a library of the OS:
`relay/` talks to a running node over HTTP and imports none of it. It is the GPU side of the topology ADR-049 draws,
that is infrastructure. Its boundary with tare is a process boundary, the `gpu-lease` contract, which the kernel
owns (`docs/GPU_LEASE_CONTRACT.md` there). Keeping it apart cost a repository, its CI and protection, and pin bumps
that changed nothing the OS runs.

## Decision

- The service lives in tare.tools.os as `node/`, a package of its own (`tare-tools-node`, standard library only,
  its own `pyproject.toml`). The submodule `satellites/tare.tools.node` is removed; the satellites are the kernel,
  the agent runtime, SpecGraph, the backlog graph and the library.
- Independence is kept by rule and by test: nothing in `node/tare_node` imports `relay/` or a satellite, and `relay/`
  never imports `tare_node` (`node/tests/test_independence.py`).
- Its tests need Linux `flock` and run in `.github/workflows/node.yml` on a GitHub-hosted Ubuntu runner. The
  self-hosted suites skip `node/` (`scripts/ci_suite.py`).
- The node installs and updates from a plain clone of tare.tools.os at `~/src/tare.tools.os` (no submodules):
  `tare-node-update` rebuilds only when `node/tare_node/` or `node/pyproject.toml` changed, and a recorded commit the
  checkout does not know (the move, a rewritten history) counts as a change.
- The contract stays in the kernel, between the two sides.

## Consequences

- One repository fewer to publish, protect and pin. tare.tools.node is not recreated; its history goes with the
  other pre-publication history into the maintainer's bundles.
- The node is versioned by the tare.tools.os commit it runs, not by tags of its own.
- A change to the node follows the OS pull-request flow and changelog.
- If the node gains consumers outside tare, or a release life of its own, it can leave again as a repository; the
  package boundary makes that a move, not a rewrite.
