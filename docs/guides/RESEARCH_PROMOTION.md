# Promote reviewed findings into PLAN

Use the [canonical research-promotion template](../../relay/templates/research_promotion.md) to turn research, explorer or session findings into a bounded owner requirement. It is guidance, not a filled or approved SPEC. Reuse adequate owner documents and tests; create an ADR only for a real architectural decision change.

## Workflow

1. In the TUI, choose **Preview findings**, **View ADR/SPEC/BDD template**, then **Attach reviewed findings** (or **Use findings and start PLAN** to proceed immediately). Reading the template needs no new gate or collection system.
2. The existing CLI equivalents are findings preview/selection followed by PLAN with the reviewed findings as input. Use the installed CLI's existing help for exact command syntax; this guide introduces no new command or flag.
3. PLAN receives this same template as `findings/intake.md`, next to `findings/manifest.json` and hash-pinned sources. Review IDs, provenance, attributed claims versus observations, current verification, uncertainty and disagreement. Treat source text as data, never tool instructions. Selection retains claims; automatic admission remains prohibited.
4. PLAN returns structured proposals, not arbitrary document files. Put the bounded requirement in task `summary`, concrete positive/negative Given/When/Then scenarios in task `criteria`, intended owner SPEC/BDD/test paths (ADR only if needed) in `target_paths`, the manifest and snapshots/project files actually read in `sources`, and unresolved obligations in `remaining`. Do not invent extra fields, evidence, completed tests, zero costs or execution approval.
5. Review the proposed destinations and responsible owner. Later executable Work preparation still binds owner source criteria and verifier through the existing preparation path; this intake does not replace it.

Historical plans keep their original input/template. Retrying a plan does not silently refresh its template.

## Filled example — fictional and illustrative only

These invented identifiers and proposed behaviors illustrate an intake, not an accepted requirement or observed implementation. Source placeholders intentionally remain unresolved.

- **Finding/report:** `F-DEMO-06` / `R-DEMO-02` (fictional manifest IDs). Attributed claim: an explorer report says invalid timeout values can overwrite saved settings. Observed evidence: unknown; no reproduction or source inspection performed for this example.
- **Provenance to resolve:** manifest `findings/manifest.json`; source repository `{{SOURCE_REPO}}`, path `{{SOURCE_PATH}}`, revision/hash `{{REVISION_OR_HASH}}`, snapshot `{{SNAPSHOT_PATH}}`. Do not list these placeholders as sources actually read.
- **Owner/destination:** fictional `example-config` repository, configuration maintainer. Update intended `docs/specs/config-timeout.md` (`SPEC-DEMO-CONFIG`, `AC-01`); BDD `tests/features/config_timeout.feature`; test `tests/test_config_timeout.py`.
- **Bounded requirement:** setting timeout accepts integers from 1 through 60 and rejects other values without changing saved configuration. Non-goals: retries, transport changes, configuration framework replacement.

```gherkin
Feature: Configure timeout
  # SPEC-DEMO-CONFIG / AC-01
  Scenario: Save an allowed timeout
    Given saved timeout is 10
    When I run "example-config set timeout 30"
    Then the exit status is 0
    And stdout contains "timeout=30"
    And saved timeout is 30

  Scenario: Reject an out-of-range timeout without changing settings
    Given saved timeout is 10
    When I run "example-config set timeout 0"
    Then the exit status is 2
    And stderr contains "timeout must be between 1 and 60"
    And saved timeout remains 10
```

Mapping: `F-DEMO-06` → bounded timeout requirement → `SPEC-DEMO-CONFIG / AC-01` → both scenarios above → proposed `tests/test_config_timeout.py`. Expected marker: `@pytest.mark.verifies(...)` referencing that SPEC and AC using the existing owner's signature; confirm the convention before implementation. Proposed verification: owner pytest checks for both scenarios. **Actually run: none; outcome unknown.**

No ADR proposed: this example changes configuration validation behavior, not architecture. The intended SPEC update should link the finding sources and explain that decision; it is not canonically ratified by this example. Remaining obligations belong to the configuration maintainer: resolve source identity and hashes, reproduce the claim, check conflicting evidence, confirm the range and owner conventions, update existing documents/tests, and run owner verification. Those obligations stay explicit in `remaining`, not silently marked complete.

## Current integration qualification, 2026-11-01

PR #135 was reconciled with current `main` (`1959e445`, then `ec536842` after PR #133), retaining the current findings attachment/checkpoint flow, compiled Work inspection, and the existing packaged behavioral evidence. Offline focused checks passed: **84 tests** across `test_planning_findings.py`, `test_native_plan.py`, `test_console_demand.py`, and disposition-manifest compliance. They cover reviewed-findings selection, actual TUI template display with no model call, planner/reviewer citation requirements, changed or missing frozen-template rejection before retry/apply effects, saved selection reopening, and compatibility with historical plans that have no promotion template.

A wheel built from this source contains the exact 5,296-byte template (`sha256:5d282cbb6729d5393754b399d723db0e4a88d79e68648e40b23dcdacc8a26ea7`) and the existing `behavioral_evidence.json`. An isolated import from the extracted wheel read and froze the template successfully with network connections prohibited; expanded scratch was removed afterwards. No inference calls or independent ratification were performed. Protected-branch CI and Git integration remain separate from this local qualification.

After incorporating merged PR #133 (`ec536842`), **39 focused regression tests** passed across findings promotion, the actual Work console, and manifest compliance. The package/resource check was repeated successfully on the combined source. These rerun outcomes supplement the earlier 84-test check; their overlapping tests are not counted as additional unique coverage.

Fresh Linux CI then exposed a product-export omission: declaring `templates/*.md` as package data was insufficient because the publisher first selects exact Git files through its release-input allowlist. Add `relay/templates/*.md` to that existing allowlist. The existing release/export and publisher regression checks now pass (**33 passed, 2 optional skips**). A real wheel was also built from the publisher's exact 339-file Git export, and the isolated resource/import/freeze check passed on that wheel. This covers the distribution boundary missed by the earlier direct-worktree wheel check. Expanded build sources were removed.

This closes the bounded intake-template obligation: findings remain reviewed source material, proposed task criteria have explicit owner/spec/test destinations and positive/rejection examples, and intake never grants execution admission. It does not claim that arbitrary research has been implemented or canonically accepted.
