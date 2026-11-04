# Changelog

Notable changes to `tare.tools.os` are recorded here, newest first.

## Unreleased

- CI runs on GitHub-hosted Linux and Windows runners (`Tests (linux)`, `Tests (windows)`) instead of the maintainer's self-hosted ones: the satellites check out over HTTPS on both platforms (no deploy keys), Python comes from `actions/setup-python` with a pip cache, and the test map that focused runs read moves from the runner's tool cache to the Actions cache. The repository variables `CI_RUNNER_LINUX` and `CI_RUNNER_WINDOWS` move it to your own runners, and pull requests from forks then skip it. README and the CI feedback guide say so.
- `tools/specgraph_ci_dependencies.json` pins the satellites at the commits this repository pins; the commits it listed are not published.
- First public release. The repository starts from a single commit; earlier history is not published. It contains Tare
  as the README describes it: the runtime in `relay/` (sessions, the TUI, `tare serve`, Work runs and delegation), the
  GPU node service in `node/`, the satellites pinned under `satellites/`, the ADRs and guides, the backlog graph and
  the test suite.
