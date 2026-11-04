---
id: SPEC-OS-SPECGRAPH-COVERAGE-001
title: Operational OS code coverage in SpecGraph
status: ACCEPTED
admission: OPERATOR_AUTHORIZED
acceptance_criteria:
  AC-01: Index relay and tools code without changing governance or document discovery.
  AC-02: Recover the measured work_run, research and landing_policy code targets.
  AC-03: Measure full index and source-verified no-op on an immutable real corpus.
  AC-04: Publish small edits, deletion and rename without stale identities or hashes.
  AC-05: Preserve baseline governed observations and report limitations honestly.
---

# Operational OS code coverage

Owner Work: `SPECGRAPH-OS-COVERAGE-20260908`. This contract is accepted under
the operator's implementation instruction of 2026-09-08; no independent
Round Table approval is asserted. Decision: [ADR-069](../adr/ADR-069_SPECGRAPH_OPERATIONAL_CODE_SCOPE.md).

The OS has operational Python under `relay/` and `tools/`, while SpecGraph's
default code scope is `src/`. Add explicit retrieval roots and preserve Python
module roots `[src, '.']`; relay/tools resolve from `.` while existing src
identities retain their module root. Retain `src` in indexed roots because the explicit list replaces
default extraction; a fixture with governed code must retain its violations.
Keep the existing project identity, governed `src`, all default
specification, ADR, test, schema and API roots, and strict orphan checking.
Searching a symbol confers no execution, admission or governance authority.

Exclude existing default scratch/cache paths, relay historical/custody
directories, and the exact test-only paths under relay/tools. These exclusions
must remove no previously observed governed/document/test/schema inputs.
Documents under `docs/` remain discoverable through their existing roots.

Before qualification, freeze six queries: exact paths for `work_run`,
`research`, `landing_policy` and one descriptive query per module. Record
whether the intended file appears, its rank, source hash, response bytes and
duration. Descriptive questions are selected from known source intent, not a
blind relevance benchmark. Misses are measured results, not hidden by tuning.

Use pinned tracked repository bytes copied into native-filesystem disposable
directories. Compare default configuration against this configuration on the
same corpus and engine. Capture cold full publication, source-verified no-op,
single-file edit, rename and deletion. An edit must change the published source
hash; rename must remove the old path; deletion must remove all symbols and
file observations for that path. No-op must retain the snapshot revision.
Run two rounds and preserve raw receipts; this does not establish production
p95 latency or end-to-end agent savings.

Falsifiers live in `tests/test_specgraph_operational_scope.py`; the reproducible
real-corpus runner is `tools/qualify_specgraph_coverage.py`. Out of scope:
runtime/harness changes, expanding governance to all indexed symbols, model
calls, synonym/ranking changes and activating automatic host roles.
