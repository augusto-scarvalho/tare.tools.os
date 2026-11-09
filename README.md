# tare.tools.os

**Tare** is a terminal workspace where you talk to a coding agent in plain language and it gets the work done
through separate, accountable roles: it explores your project, plans a change, implements it in a private copy,
runs your checks, has an independent model review the result, and only then applies it.

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-brightgreen.svg)](https://python.org)

🇧🇷 [Leia em português](README.pt-BR.md)

---

## What it does

- **One conversation, separate roles.** A coordinator answers questions directly and routes real work: an explorer
  investigates, a planner writes the plan, an implementer edits a candidate copy, the host runs your project's
  checks, and an auditor from a different model family reviews it. You approve project writes once per task. See
  [one chat and its execution roles](docs/guides/CONVERSATION_ROLES.md).
- **Your models, your accounts.** Local models through an OpenAI-compatible gateway, the official Claude, Codex and
  AGY command-line tools with your own logins, or HTTP APIs (OpenRouter, Bedrock, OpenAI). Tare owns the agent
  loop, tools and permissions in every case. See [vendor CLI inference](docs/guides/VENDOR_CLI_INFERENCE.md) and
  [HTTP API providers](docs/guides/HTTP_API_PROVIDERS.md).
- **Evidence, not claims.** Every model call, tool effect, check and review is journaled. A change is applied only
  after its checks pass and an independent review accepts it, and an interrupted run is never silently replayed.
- **Durable history.** Conversations, plans and receipts live outside your projects and survive restarts and
  updates.

## Getting started

You need Windows 10 or 11 (Linux works too; see [product installation](docs/guides/PRODUCT_INSTALLATION.md#linux)),
64-bit Python 3.12, Git, and access to at least one model:

- a signed-in [Claude Code](https://docs.anthropic.com/en/docs/claude-code), Codex or AGY command-line tool;
- an API key for Anthropic, OpenAI, Google, OpenRouter or Amazon Bedrock; or
- a local server with an OpenAI-compatible API (llama.cpp, vLLM, LM Studio and similar).

Install from source into a virtual environment:

```powershell
git clone --recurse-submodules https://github.com/augusto-scarvalho/tare.tools.os.git
cd tare.tools.os
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install "./satellites/tare.tools.kernel[attachments]" ./satellites/tare.tools.agent-runtime `
  ./satellites/tare.tools.specgraph ./satellites/tare.tools.backlog-graph -e .
```

Open a project folder (any folder works; it does not need Git):

```powershell
tare C:/path/to/your-project
```

The first time, type `/setup` and connect one model: Tare finds the installed CLIs on its own (without spending a
call), and you can add an API key or a local server address there. Then ask anything about the project, or describe
the change you want. Everyday use is in the [TUI quickstart](docs/guides/TUI_DAILY_USE.md).

Conversations and receipts are kept outside your projects. A source install keeps them in `.reconciliation` next to
the clone; set `TARE_STORAGE_ROOT` to an absolute folder to keep them elsewhere.

To pick models per role:

```powershell
tare models --role coordinator     # lists what this machine can use, with a line to copy for each
tare C:/path/to/your-project --coordinator PROVIDER/DEPLOYMENT@LEVEL --planner PROVIDER/DEPLOYMENT@LEVEL
```

A managed installation (prebuilt bundles with automatic updates and rollback) is described in
[product installation](docs/guides/PRODUCT_INSTALLATION.md).

## Other ways in

| Entry point | Use it for |
|---|---|
| `tare` / `tare console` | The terminal UI: conversations, approvals, history, model and role choices. |
| `tare-ops agent` | A single native agent session from a script or another terminal. |
| `tare-ops run <WorkId>` | Executing and settling an explicit Work from a backlog graph ([guide](docs/guides/WORK_RUN.md)). |
| `tare serve`, CLI and MCP | Other frontends and editors ([CLI and MCP](docs/guides/CLI_MCP.md)). |

## Repository layout

`tare.tools.os` composes the runtime. Its building blocks are separate repositories pinned as Git submodules
([ADR-049](docs/adr/ADR-049_REPO_FEDERATION_AND_ANTI_DRIFT_GOVERNANCE.md)):

```
tare.tools.os/
├── relay/                                    # Sessions, the TUI, conversation flow, delegation, product releases
├── tests/                                    # Offline test suite (no provider, no network)
├── docs/                                     # Guides, decisions (ADRs) and specifications
├── node/                                     # GPU node service: model gateway, shared GPU lease (Linux)
├── satellites/
│   ├── tare.tools.kernel/        (@ 08851e4) # Execution boundaries, journals, terminal sessions
│   ├── tare.tools.agent-runtime/ (@ 133d9bb) # Bounded agent loop
│   ├── tare.tools.specgraph/     (@ cce9edd) # Project specification and code graph
│   ├── tare.tools.backlog-graph/ (@ 0fd1e7c) # Task graph with compare-and-swap updates
│   └── tare.tools.library/       (@ 6e9639e) # Technical library and knowledge records
└── scripts/                                  # Release, CI and maintenance tools
```

Clone with the satellites:

```bash
git clone --recurse-submodules https://github.com/augusto-scarvalho/tare.tools.os.git
# or, in an existing clone:
git submodule update --init --recursive
```

## Development

Read [AGENTS.md](AGENTS.md) first: it holds the rules every contributor, human or agent, follows here.

- Work on a branch. `main` is protected and takes changes only through pull requests that are up to date and pass
  CI on Windows and Linux plus the changelog policy. `tare ci pr` pushes the branch and opens the pull request with
  auto-merge.
- Enable the repository hooks once per clone with `git config core.hooksPath .githooks`. After each commit on a
  work branch the hook pushes it; it never pushes `main` or `dev`.
- Run only the tests of what you changed (`python scripts/ops/run_focused_tests.py`); CI runs what a change reaches
  and the whole suite nightly ([CI feedback](docs/guides/CI_FEEDBACK.md)).
- CI runs on GitHub-hosted Linux and Windows runners, pull requests from forks included (GitHub asks a maintainer to
  approve a first-time contributor's run). The repository variables `CI_RUNNER_LINUX` and `CI_RUNNER_WINDOWS` move
  it to your own runners; pull requests from forks then skip it.

The full documentation map is [docs/INDEX.md](docs/INDEX.md); notable changes are in [CHANGELOG.md](CHANGELOG.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
