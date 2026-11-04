# ADR-068: Universal Repository Branching, CI/CD Pipeline Standardization, and Legacy Alias Deprecation

* **Status:** RATIFIED (Tripartite Byzantine Consensus 3/3)
* **Superseded:** by [ADR-073](ADR-073_BRANCHES_OF_THE_TARE_TOOLS_REPOSITORIES.md) on 2026-11-04; kept as the record of the earlier decision.
* **Date:** 2026-08-21
* **Deliberation Case:** [`CASE-2026-08-21-RFC-009`](https://github.com/augusto-scarvalho/tare.tools.library/blob/main/cases/CASE-2026-08-21-RFC-009-UNIVERSAL-BRANCH-STANDARDIZATION-AND-CI-PIPELINES/DECISION.md)
* **Predecessors:** [ADR-049](ADR-049_REPO_FEDERATION_AND_ANTI_DRIFT_GOVERNANCE.md), ADR-065 (retired), [ADR-067](https://github.com/augusto-scarvalho/tare.tools.library/blob/main/docs/adr/ADR-067_CANONICAL_REPOSITORY_TAXONOMY_AND_GHOST_PURGE.md)

---

## 1. Context & Problem Statement

Across the `tare.tools` federated ecosystem, discrepancies in Git branch naming topologies (`main` vs `master`, `dev` vs `develop`) caused friction in multi-agent routing, stale GitHub branch displays, and fragmented CI/CD workflow configurations.

---

## 2. Decision & Universal Canonical Standard

```mermaid
flowchart TD
    subgraph Branches ["🌿 Universal Branch Topology Contract"]
        Main["🛡️ <b>main</b> (Default & Canonical Production)<br/>• Immutable production release surface<br/>• Protected branch; 100% green CI required"]
        Dev["⚡ <b>dev</b> (Canonical Integration)<br/>• Continuous integration across satellites<br/>• Shared verification baseline"]
        Work["🌱 <b>feat/* | fix/* | docs/* | agent/*</b><br/>• Ephemeral working branches<br/>• Deleted immediately upon PR merge"]
    end

    subgraph CI ["⚙️ Canonical CI/CD Pipeline Contract"]
        Trigger["🎯 Standard Triggers<br/><code>on: [push, pull_request] on [main, dev]</code>"]
        Gates["🚪 Mandatory Integrity Gates<br/>• Frugality Guard (&lt;50MB)<br/>• Strict Root Taxonomy (ADR-067)<br/>• Test Suite (158/158 Passing)"]
    end

    Work -->|PR + CI Pass| Dev
    Dev -->|Promotion PR + Falsifiers| Main
    Trigger --> Gates

    classDef mainStyle fill:#182820,stroke:#a6e3a1,stroke-width:2px,color:#a6e3a1;
    classDef devStyle fill:#2d1b4e,stroke:#cba6f7,stroke-width:2px,color:#cdd6f4;
    classDef workStyle fill:#1e1e2e,stroke:#89b4fa,stroke-width:2px,color:#cdd6f4;
    classDef ciStyle fill:#2d201b,stroke:#fab387,stroke-width:2px,color:#fab387;

    class Main mainStyle;
    class Dev devStyle;
    class Work workStyle;
    class Trigger,Gates ciStyle;
```

### Invariants:
1. **Zero-Master Invariant:** No repository in the `tare.tools` ecosystem shall retain `master` as a default branch.
2. **Zero-Develop Invariant:** All integration work across all microkernel and satellite engines is consolidated strictly under `dev`.
3. **CI Trigger Parity:** Every integrity gate and test falsifier runs identically on both `main` and `dev`.

---

## 3. Consequences

* **Positive:** Deterministic routing for autonomous AI coding agents, clean GitHub landing views, and zero wasted CI runs on divergent branch names.
* **Compliance:** Enforced by automated falsifiers in CI.
