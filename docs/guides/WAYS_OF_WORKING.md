# Ways of working

A session answers each request in one of four forms. Direct is the default; the other three are experiments, off
until a measurement says otherwise, per class of route (`local`, `quota`, `usd`).

| Form | What the session does | Turned on by |
| --- | --- | --- |
| direct | reads, edits and verifies itself | always available |
| explore | asks read-only explorers questions ([ASSISTED_EXPLORATION.md](ASSISTED_EXPLORATION.md)) | the delegation policy |
| plan | writes a plan of up to 8 units into its ledger (`work.json`), checked without a model | experiment `plan_tool` |
| implement | hands a unit (or several) to child implementers in worktrees; tare checks and applies | experiment `implement_child` |

`tare delegation --experiment quota.plan_tool=on` (or the `/delegation` panel) turns an experiment on for one class;
`--experiments` shows them and `--digest` summarizes the shadow (`work-form.jsonl`: which form each turn took, and
which one Jev would have chosen).

## Plan

Each unit names the paths it changes, its done criteria and what checks it: the session's verifiers by name, or
existing test files (with pytest, `file::test` names one test). The check is literal and model-free: paths exist,
no two units share a path, named checks exist, no dependency cycle. A plan written again keeps what its units already
went through.

## Implement

tare runs the unit's checks before and after the child works, in a worktree of the session's tree (uncommitted bytes
included). The verdict is tare's, never the child's:

- **VERIFIED**: red before and green after, or green before and after with a change; applied file by file only where
  each file still holds the bytes the child started from (else **CONFLICT**, nothing written), only inside the
  session's writable paths (else **OUTSIDE**), and only when no check of the session that was green before fails
  with it (D9b; else **HELD**). A check red before as well settles nothing; then the unit's tests must cover the
  change.
- **NOT_VERIFIED**: the checks fail after the change. **INCONCLUSIVE**: nothing changed, the checks themselves
  changed, or a check did not finish within 300 s.

A change that is not applied keeps its patch in the session's custody; `tare serve` names it in the answer.

Guards, each a refusal with its reason before any model call: **R0** no check tare can run, or a unit that only
changes documentation; **R1** a read-only session; **R3** a unit that failed its checks twice; **R5** at most 8
units, 3 routes tried per unit (cheapest first), a child only with at least 10 calls left for it, 4 calls kept for
the session to finish, and children never delegate.

Several units at once (`units`): in series, or in parallel when the session asks (`parallel`) or the
`implement_parallel` experiment is on: 2 children at a time (`TARE_PARALLEL_CHILDREN`, 1 to 4), one on a local route.
Changes are applied one at a time, each checked again on the tree the earlier ones left; a change that no longer fits,
or a unit that failed alone after another was applied, runs again once on that tree. Parallel pays only when each
unit's tests do not run another unit's new code.

## Three meanings of "plan" (D12)

1. **The session's plan**: the `plan` tool above, a working plan inside one session, kept in its ledger.
2. **The graph plan** (`/plan`, `tare plan`): turns a demand into proposed graph Works, reviewed by another model.
   A session's plan can feed it as reviewed findings:
   `python -m relay.planning_findings --kind session --source <session folder>` previews it,
   `--save <preview sha256>` keeps it, and `tare plan --findings <manifest> --findings-sha256 <sha256>` plans from it.
   A task's checks (`verifiers`, `test_paths`) become its Work's required evidence.
   Reviewed findings use the [ADR/SPEC/BDD intake template](RESEARCH_PROMOTION.md),
   included in new PLAN inputs and available in the TUI without a model call.
3. **`/plan` in `tare serve`**: a read-only answer in the chat; nothing is planned into the graph.

## Measurements so far

Phase 2 (one small task) and phase 3 (two multi-file tasks), 2026-10-05 and 06: the direct session was faster
(215 s and 325 s against about 667 s) and always delivered; units were cheaper in API-equivalent dollars (children on
a cheaper model) with 1.6 to 2 times the tokens, and parallel gained no time because one unit's tests ran the other's
code. One run per task: evidence, not a conclusion. The fixes those runs found are in the CHANGELOG.
