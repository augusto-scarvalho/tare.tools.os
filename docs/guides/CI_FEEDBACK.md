# CI feedback and regression scope

The OS keeps the existing Linux/Windows checks and applies ADR-058's proportional
evidence policy. Each run publishes its exact revision, selected scope, reason,
phase timings and JUnit reports. A narrow green run is not a full regression result.

## Selection

`scripts/ci_suite.py` reads the complete change range: the push's previous SHA
or the PR base against the checked-out candidate. No manual list of changed
files, shallow-diff guess or SpecGraph cache is treated as proof of low impact.

| Change | Required execution on both platforms |
| --- | --- |
| README, changelog and Markdown directly under `docs/operations` only | Diff whitespace and separate changelog policy; no Python environment or pytest suite |
| An isolated top-level `tests/test_*.py` file | Quick contracts plus the changed test files |
| A test module imported/referenced by other tests | Quick contracts plus every transitive consumer, including imports through helper modules |
| `relay/native_mcp.py` alone, optionally with documentation/tests | Quick contracts, including actual stdio and native-runtime integration |
| Other source code, new helpers, instruction files, schemas, dependencies, satellite pins, workflows or this selector | Full regression |
| Deleted/renamed files, missing diff or unavailable test dependency inspection | Full regression |
| Scheduled or manually dispatched CI | Full regression |

The source allowlist is intentionally small. Do not expand it based only on
filename similarity. An addition needs a reviewed consumer/contract map and
tests proving that mixed changes and unknown dependencies widen the scope.
Python dependency inspection covers static imports and literal module references;
it is not a general proof about computed dynamic imports. Tests with shared
setup should use explicit imports or `conftest.py`. Dependencies reaching
`conftest.py`, modified non-test helpers, or parse failures require full coverage.

## Execution and evidence

1. **Quick contract checks**, with a five-minute step ceiling: selector negatives,
   integrity manifest, native effects/limits, MCP wire protocol and simple CLI
   startup/model discovery, plus preparation cancellation and descendant cleanup.
   The catalog listing contracts, in text and JSON, and the frontier role-admission
   test run in this phase too. So does the package-data/export contract: the
   resources declared in `pyproject.toml` must be tracked by Git and inside the
   release include, checked against the product's real Git snapshot, and a
   simulated omission fails naming the omitted path. The rest of the release
   tests, including their negative cases and the real wheel build, stay in the
   regression. Provider inference uses fixtures. Real filesystem,
   process, cancellation and transport behavior remains exercised where relevant.
2. **Selected or full regression**, only after quick checks pass. Full regression
   excludes the files already executed by the quick phase, so there is no
   duplicate test collection between phases. Imported fixtures remain available.
3. **Summary and artifacts**, even after failure: phase exit codes and wall times,
   the 20 slowest test cases, and JUnit XML. Pytest logs also include its 30 slowest
   setup/call/teardown operations. Missing/interrupted phases are visible, not PASS.

**Sample observation (package-data/export quick contract).** This is one ordinary delivery attempt, not a benchmark, and it leaves the SPECGRAPH-COMPLETION-X5 observation below unchanged. This shipment moves the declared-package-data/export contract into its own module, `tests/test_native_release_package_data.py`, and adds only that file to the quick phase; the other release tests, including their negative cases and the real wheel build, remain in the full regression, which still excludes exactly the files the quick phase ran. The five-minute quick ceiling and the mandatory full regression on both platforms are unchanged, and a change to the selector still requires that regression. The contract compares the resources declared in `pyproject.toml` with the files Git tracks and with the `os` include of the release inputs, then exports the product snapshot; a simulated omission of a declared, tracked resource fails naming the omitted path. Locally measured facts are only those in the focused verification receipts written by `scripts/ops/run_focused_tests.py` for the three files this shipment touched (`tests/test_ci_suite.py`, `tests/test_native_release.py`, `tests/test_native_release_package_data.py`): a first attempt failed 1 selector case after 20.57 s (helper `seconds` 23.109), and the corrected attempt passed 81 tests with 1 skipped in 21.46 s (helper `seconds` 23.547); a repeat of the same three files passed 81 tests with 1 skipped in 32.45 s (helper `seconds` 36.031). Those numbers cover only those three files. The quick and regression wall times of this sample on each platform, review time and the monetary or API cost of the run were not captured; they stay unknown rather than zero, and a CI `elapsed_seconds` is a different measurement from the helper's local `seconds`. Revisions are the candidate commit the local run reported, not a published baseline. Failures are kept as the receipts report them; the later green attempt does not overwrite the earlier red one. This observation claims no paid comparison, no saving, no independent acceptance and no cohort conclusion.

**Sample observation (SPECGRAPH-COMPLETION-X5).** This is one ordinary delivery attempt, not a benchmark. This shipment adds the catalog listing contracts in both text and JSON form and the frontier role-admission test to the quick phase; the five-minute ceiling is unchanged, and the full regression still runs every file the quick phase does not. Locally measured facts are only those in the local focused verification receipts written by `scripts/ops/run_focused_tests.py` for the four files this shipment touched: a first attempt failed 3 selector cases after 18.04 s (helper `seconds` 20.078), and the corrected attempt passed 115 tests in 15.77 s (helper `seconds` 17.125, scratch removed). Those numbers cover only those four files. The quick and regression wall times of this sample on each platform, review time and the monetary or API cost of the run were not captured; they stay unknown rather than zero, and a CI `elapsed_seconds` is a different measurement from the helper's local `seconds`. Revisions are the candidate commit that CI and the focused run reported, not a published baseline. Failures are kept as the receipts report them; a later green attempt does not overwrite an earlier red one. This observation claims no paid comparison, no saving, no independent acceptance and no cohort conclusion, and it does not reopen FLOW-19.

The workflow keeps existing job names and does not change repository branch
protection. Job success requires every selected phase; quick success alone does
not satisfy a run whose plan requires full regression.

An evidence upload has one bounded retry, with a three-minute ceiling for each
attempt. The retry only uploads the same completed reports; it never repeats or
changes test results. Its artifact name ends in `-retry` so a partial first
upload cannot collide with it. Failed tests still fail the job, and if neither
upload succeeds the job fails. Cancellation does not start another upload.

The full scheduled run starts at **05:17 UTC daily**. Manual `workflow_dispatch`
also always requests full coverage. Event/ref concurrency cancels superseded
push or PR runs without letting a push cancel the scheduled regression. No
automatic retries hide flaky test failures.

## Pull requests into main

`main` is protected for everyone, administrators included: changes arrive only through pull requests that
are up to date with `main` and whose checks passed (`Test Suite & Falsifiers` on both platforms and the
changelog policy). `tare ci pr` pushes the current branch, opens or reuses its pull request, turns on
auto-merge with a merge commit and updates the branch when `main` moved. A merge keeps the branch's commits,
so a long-lived work branch opens its next pull request with only what is new.

Two pull requests open at once: when one merges, the other is behind `main`, and both added their CHANGELOG
entry on top. The shepherd `tare ci pr` leaves running (`tare ci shepherd <repo-dir> <number>`) merges `main`
into it locally, with CHANGELOG.md as a union (`.gitattributes`), and pushes with the operator's credentials
so CI runs again; GitHub's own update would conflict, and an update pushed with GITHUB_TOKEN runs no CI. A
conflict in another file becomes an alert. The first failed CI run becomes an alert with its failing tests;
the shepherd never automatically reruns tests. Diagnose and fix the cause before starting a new run.
If a protected repository disables GitHub auto-merge, `tare ci pr` keeps the PR open
and returns `AUTO_MERGE_DISABLED_AWAITING_INTEGRATION`. Integrate it only after
the required checks pass; the command does not change repository settings.
The watcher leaves pull request alerts to the shepherd. A shepherd beats in
`shepherds.json` every round; the end of an agent's turn (the stop hook), `tare ci check`, every push's watcher and
the daily check start again one silent for 15 minutes (the machine restarted or the process died), so a red pull
request never waits unseen. It has no time limit and stops when the pull request merges, closes or loses auto-merge. Pull requests behind main are
brought up to date one at a time, oldest first: the others keep their green run and wait, so a merge restarts
one CI run, not every open one. A stuck pull request holds the queue until its auto-merge is turned off. A conflict in the disposition manifest alone
is resolved by regenerating it from the merged tree (`scripts/sync_disposition_manifest.py`).

CI runs once per change. On the push to `main` that a merge makes, `scripts/ci_suite.py plan` checks that
the merge commit's tree is its second parent's tree, that `main`'s previous commit is an ancestor of that
parent and that `tare.tools CI` passed on that parent in its pull request: the bytes are the bytes CI
tested, so the scope becomes `docs` with reason `tested_in_pull_request`. Without the token, without
access or in any other shape CI runs as before. The auto-update installs a release only after its commit's
run on `main` is green (relay/native_product.py), which this keeps to minutes after the merge.

## Local use

For focused local checks, use the worktree's Python environment with
`python scripts/ops/run_focused_tests.py --output <new-evidence-folder> tests/test_example.py`.
It saves the log, JUnit report and exit receipt, while removing temporary test
repositories on success, failure or interruption. It requires explicit test
files or node IDs and never defaults to the whole suite. Keep evidence outside
pytest's temporary directory; expanded fixture repositories are not evidence.

The explicit phase commands below are for reproducing a CI phase. Their
`--basetemp` directories are caller-owned scratch and must be removed after
preserving the phase's logs and reports.

Run from the root of an activated source install (see "Getting started" in the
README). Put the outputs in a new directory outside the checkout:

```text
python scripts/ci_suite.py plan --output /external/experiment/ci
python scripts/ci_suite.py quick --output /external/experiment/ci --basetemp /external/experiment/pytest-quick
python scripts/ci_suite.py regression --output /external/experiment/ci --basetemp /external/experiment/pytest-regression
python scripts/ci_suite.py summary --output /external/experiment/ci
```

Outside GitHub, `plan` defaults to full coverage.

For a direct focused pytest command with a nested `--basetemp`, create its parent
directory first (`Path(...).parent.mkdir(parents=True, exist_ok=True)`). Pytest
creates the final directory only. Use a new path and JUnit receipt for each attempt;
preserve the original failure when correcting an invocation.
Fixtures that assume no Git repository ancestry must also live outside existing
checkouts. On Windows, keep Git worktree fixture paths short and use CI's process-local
`core.longpaths=true` setting; a deeply nested evidence folder can exceed Git's path limit.

## Next optimization, driven by measurements

The coordinator import isolation delivery
removes an observed shared-state mutation: collecting Work/coordinator tests no
longer opens or migrates the operator database. Explicit temporary databases in
fixtures remain necessary for tests that actually use coordinator operations.

Use the published timing reports to identify repeated expensive setup, UI waits
and slow integration groups. Split or parallelize only groups with proven state
isolation; both self-hosted platform runners (label `aaaaa`) currently run on the
same machine.
Do not remove process/cleanup or Windows-specific tests merely to improve a number.

This delivery improves selection and time-to-first-result. It does not claim
that the complete 2,000+ test regression is now fast or that dependency caching
solves its runtime cost.

References: [GitHub concurrency](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
and [pytest duration/JUnit reporting](https://docs.pytest.org/en/stable/how-to/usage.html).
