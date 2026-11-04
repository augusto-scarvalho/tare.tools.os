# Satellite federation runbook

How to inspect, update and verify the satellites that `tare.tools.os` pins as Git submodules
([ADR-049](../adr/ADR-049_REPO_FEDERATION_AND_ANTI_DRIFT_GOVERNANCE.md)).

## Rules

1. **Exact pins.** `tare.tools.os` never follows a moving branch. Each satellite under `satellites/` points at one
   commit, and both maintained READMEs (`README.md`, `README.pt-BR.md`) state that commit.
2. **Thin adapters.** The OS reaches a satellite through its public package API from `relay/`. A satellite's API
   change is absorbed there instead of spreading through the codebase
   ([ADR-050](../adr/ADR-050_APP_DECOMPOSITION_ACL_AND_DEPENDENCY_GOVERNANCE.md)).
3. **Change a satellite in its own repository first**, then move the pin here.

## Before you start

Clone with submodules, or initialize them in an existing clone:

```bash
git submodule update --init --recursive
```

## Check every satellite

```bash
python scripts/ops/bump_satellite.py status
```

It prints, for each of the six satellites (kernel, agent-runtime, specgraph, backlog-graph, library, node), the
pinned commit and the tip of the remote `main`.

## Move a pin

```bash
# To the tip of origin/main, committing the bump and checking it:
python scripts/ops/bump_satellite.py bump tare.tools.specgraph --commit

# To a tag or a commit:
python scripts/ops/bump_satellite.py bump tare.tools.kernel --ref <tag-or-commit> --commit
```

The command:

1. runs `git fetch origin` inside the submodule;
2. checks out the requested ref (default `origin/main`);
3. updates the pin in both READMEs;
4. with `--commit`, stages the gitlink and both READMEs and commits
   `chore(deps): bump <satellite> from <old> to @ <new>`;
5. unless `--no-test` is given, runs `python -m pytest tests/test_federation_submodules.py` against the committed
   gitlink. Without `--commit` it only reminds you to commit first, because the check reads committed gitlinks.

A failure stops the command and keeps the candidate checked out for you to repair; nothing is rolled back
automatically. Push the bump through a pull request like any other change.

## What the federation test checks

```bash
python -m pytest tests/test_federation_submodules.py -v
```

- `.gitmodules` lists only the canonical satellite URLs.
- Every satellite directory exists under `satellites/` and has a valid commit.
- `README.md` and `README.pt-BR.md` state exactly the committed pins.
