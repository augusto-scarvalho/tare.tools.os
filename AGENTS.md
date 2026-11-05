# AGENTS.md

What every agent working in this repository needs, and nothing more. Claude Code reads it through `CLAUDE.md`;
Codex, AGY and tare read it directly. Guidance for one role (explorer, implementer, planner, auditor) comes from
that role's prompt capsule (`relay/prompt_capsules.json`); guidance for a project tare works on lives in that
project's own `AGENTS.md`; rules of one machine live in that machine's global instructions.

## The repository

- `tare.tools.os` composes the tare runtime: sessions, the TUI, `tare serve`, Work runs and delegation. Code is
  in `relay/`, tests in `tests/`, decisions in `docs/adr/`, manuals in `docs/guides/`.
- The kernel, agent runtime, SpecGraph, backlog graph and library are satellites (`satellites/`, pinned
  commits). Change them in their own repositories, then move the pin.
  Synchronize both maintained README pins with `scripts/ops/bump_satellite.py`
  and run `tests/test_federation_submodules.py` against the committed gitlinks before pushing.
- `node/` is the GPU node service, a package of its own that the node installs (`node/README.md`). `relay/` talks
  to it over HTTP and never imports it, and it imports nothing outside the standard library. Its tests need Linux
  `flock` and run in `node.yml`, not in the Windows suite.
- The backlog is `work-graph.json`; read and change it with the backlog CLI
  (`python -m graph_backlog.cli --graph work-graph.json next | show <id> | add-node ...`).
- `tare.tools.harness` is frozen: never import, copy or resume it.
- Read a web page with `python scripts/tools/fetch_clean_url.py "<URL>"` (clean text, not raw HTML; ADR-050).
- The relay mesh (trains, the round table) has its own roles and commands:
  [`docs/guides/RELAY_MESH.md`](docs/guides/RELAY_MESH.md).

## Changing it

- Work on a branch. `main` takes changes only through pull requests that merge themselves once CI is green:
  `tare ci pr` pushes the branch, opens the pull request with auto-merge and leaves a shepherd that keeps it up
  to date with `main`.
- Write code, comments, prompts and documentation in English; agents still reply in the user's language.
  Portuguese versions for Brazilian readers live only in `README.pt-BR.md` and `docs/pt-BR/`: update them with
  their English source.
- A material change adds a bullet under `## Unreleased` in `CHANGELOG.md` (the push hook checks it). Earlier
  entries are never rewritten.
- A change to `relay/*.py` regenerates the disposition manifest: `python scripts/sync_disposition_manifest.py .`
  Its compliance test compares against the Git index. Stage only this change's relay files and the regenerated
  manifest before running that check; regenerating the working copy alone leaves the staged hashes stale.
- A compound flow must carry the TUI's `--invocation-id` through `flow_invocation` and recovery, restore its
  selection through `demand_console`, and expose its knowledge outcome. Test the actual public CLI arguments
  and saved-demand reopening; adding a mode label alone does not establish the TUI path.
- Tests are offline fixtures: no provider, no network, no operator storage (`tests/conftest.py`). Run the tests
  of what you changed, never the whole suite on a laptop (it freezes the machine); CI runs what the change
  reaches, and the whole suite every night ([`docs/guides/CI_FEEDBACK.md`](docs/guides/CI_FEEDBACK.md)).
- Experiment in a `git worktree`, never in a copy of the repository, and park it when done
  (`python scripts/ops/park_worktree.py <path>`, then `--apply`). Stop bulk work below 5 GB of free disk.
- Run local focused checks with `python scripts/ops/run_focused_tests.py <test-files>`: it keeps logs/JUnit in a
  new folder under `temporary-evidence/`, which Git ignores, and removes temporary test repositories on success or
  failure. Those files are receipts of one run: never commit or edit them; rerun instead. Do not put pytest scratch
  trees in permanent evidence folders. Keep Git revisions, patches and new files instead of expanded checkouts.
