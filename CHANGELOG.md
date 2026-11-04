# Changelog

Notable changes to `tare.tools.os` are recorded here, newest first.

## Unreleased

- ADR-073 supersedes ADR-068 with the branch practice the repositories follow: `main` everywhere (tare.tools.local-labs renamed `master`), changes through pull requests from short-lived branches, no required `dev`, merged branches deleted by GitHub. The locality guide links tare.tools.local-labs on `main`.
- ADR-072 and SPEC-OS-SHIP-001 decide Ship, delegated delivery through remote CI/CD: `tare ci` generalized to any GitHub repository, an alert and shepherd lifecycle that ends with its subject, an identity and protection preflight, rule-based triage with receipts, `.tare/ship.toml` for what GitHub cannot tell, a fix loop through ordinary Work, and after-merge watching without deployment. Ten `ship-*` backlog items carry it; the decisions index now lists ADR-071 and ADR-072.
- CI runs each platform in shards on separate runners, 4 on Linux and 6 on Windows: `scripts/ci_suite.py slice` gives each shard a share of the plan's test files balanced by their measured seconds, and `Tests (linux)` / `Tests (windows)` merge the shards into one verdict, the test map and the seconds (`ci_suite.py merge`). The full regression drops from about 28 (Linux) and 38 (Windows) minutes on one runner to about 7 per shard; required checks keep their names.
- The rename dialog tests that check a finished save wait for the save worker before asserting: the save runs in a worker thread, and one UI pause was not enough on a slower runner (`test_ui_rename_write_failure_ui_unchanged` saw the dialog still saving on the hosted Windows runner).
- The node service workflow describes the platform suites it complements without calling them self-hosted.
- CI runs on GitHub-hosted Linux and Windows runners (`Tests (linux)`, `Tests (windows)`) instead of the maintainer's self-hosted ones: the satellites check out over HTTPS on both platforms (no deploy keys), Python comes from `actions/setup-python` with a pip cache, and the test map that focused runs read moves from the runner's tool cache to the Actions cache. The repository variables `CI_RUNNER_LINUX` and `CI_RUNNER_WINDOWS` move it to your own runners, and pull requests from forks then skip it. README and the CI feedback guide say so.
- `tools/specgraph_ci_dependencies.json` pins the satellites at the commits this repository pins; the commits it listed are not published.
- First public release. The repository starts from a single commit; earlier history is not published. It contains Tare
  as the README describes it: the runtime in `relay/` (sessions, the TUI, `tare serve`, Work runs and delegation), the
  GPU node service in `node/`, the satellites pinned under `satellites/`, the ADRs and guides, the backlog graph and
  the test suite.
