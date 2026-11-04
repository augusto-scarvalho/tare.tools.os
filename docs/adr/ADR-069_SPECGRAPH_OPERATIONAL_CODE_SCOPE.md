---
id: ADR-069
title: Separate operational code retrieval scope from governance scope
status: ACCEPTED
admission: OPERATOR_AUTHORIZED
governed_specs: [SPEC-OS-SPECGRAPH-COVERAGE-001]
---

# ADR-069: SpecGraph operational code scope

Date: 2026-09-08. Authority: explicit operator implementation instruction;
not an independent audit or a replacement for ecosystem governance.

The default OS index contains document and schema observations but no
operational code symbols because code lives outside `src`. A previous
experimental corpus found code under relay/tools without changing the product
configuration. Make those roots explicit in `specgraph.yaml`, alongside `src`:
`indexed_paths` replaces the default extraction roots, so retaining `src`
prevents silently dropping governed symbols if that directory is populated.

Preserve `governed_paths: [src]`, project identity `tare.tools.project`, strict
checking and all discovery defaults. A namespace migration or expansion of
governance requires its own change; retrieval must not manufacture compliance
obligations or authority. Keep Python roots `[src, '.']`: relay/tools resolve
relative to `.`, while potential existing `src` identities retain their root.

Exclude relay history, train/round-table/approval/intake/inbox/lease custody,
and exact test-only relay files and tools/tests. These contain copied source,
evidence or tests rather than the current operational implementation. Do not
exclude `docs/history` or broaden exclusions to suppress existing doctor
findings. Baseline observation equality is checked directly.

Cost: the index grows and edits still incur relation assembly. Source-verified
no-op remains a full hash observation. Measure these costs instead of promising
constant-time freshness. Retrieval does not execute the indexed programs.

Rollback: remove `specgraph.yaml` and run `specgraph index`; that republishes
the default scope. Existing committed specs and ADRs remain discoverable.
The benchmark uses immutable tracked bytes and disposable mutations only.

Contract: [SPEC-OS-SPECGRAPH-COVERAGE-001](../specs/SPEC-OS-SPECGRAPH-COVERAGE-001.md).
