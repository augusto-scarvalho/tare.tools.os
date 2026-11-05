# Scripts

Operator, CI and research scripts for `tare.tools.os`. Run them from the repository root. Each entry below is
taken from the script's own docstring or header; `python <script> --help` shows the full arguments where the
script has a CLI.

```
scripts/
├── *.py            # CI selection, manifest sync and a few qualification entry points
├── benchmarks/     # Local inference benchmarks and the local experiments runner
├── diagnostics/    # CPU-only probes of the slop.cpp chat template and argument parsing
├── harness_lib/    # Modules carried over from the frozen tare.tools.harness
├── labs/           # Bounded qualification labs (agent runtime, hook probes)
├── ops/            # Release, installation, CI runner, worktree and maintenance operations
├── research/       # Offline research comparators
├── round_table/    # Offline inference telemetry report
└── tools/          # Web page ingestion and worker-node cleanup
```

## Top level (`scripts/`)

- `ci_suite.py`: CI test selection and timed pytest phases, standard library only. Pushes and pull requests
  run the tests that reached the changed code; the nightly run executes everything and refreshes the map
  ([CI feedback](../docs/guides/CI_FEEDBACK.md)).
- `test_map.py`: pytest plugin (`-p scripts.test_map`) that records which repository functions each test file
  runs, for CI selection.
- `sync_disposition_manifest.py`: refreshes the size and SHA-256 of every relay module in the disposition
  manifest (`contracts/v1`) and adds new modules given on the command line. Run it after changing
  `relay/*.py`: `python scripts/sync_disposition_manifest.py .`
- `configure_vendor_models.py`: expands existing local vendor connections from explicit offline catalogues.
- `qualify_laya_routes.py`: runs a frozen, CPU-only route-choice screen; never changes routing policy.
- `heads_up_calibration.py`: measures the heads-up funnel (`relay/heads_up.py`) on turns that already happened.
  `claude` uses Claude Code transcripts (`~/.claude/projects`) as labels: the turns after which the user accepted a
  side-agent note, beside quiet turns of the same sessions. With `--variant`/`--brief` it scores judge questions and
  briefs, with `--claims v2` the narrow Noul questions and code counts (AUC per feature and a cross-validated
  logistic composite, the source of the filter's weights), and with `--writer ROUTE` a writer's notes beside the side
  agent's. `replay --limit N [--writer ROUTE]` sends saved tare sessions through the funnel; `report` reads the log in
  use: the threshold's trade, notes dropped as already said, those sent with `/heads-up`, Jev's shadow and writers.
  Every mode except `report` spends judge or writer calls.
- `preflight_native_verifier_linux.py`: Linux/WSL verifier preflight using the published Kernel and the
  existing argv seam.
- `native_experiment_limits.json`: native execution and context-compaction limits for experiments (all limits
  unset, zero model retries).

## Benchmarks (`scripts/benchmarks/`)

- `local_lmm_benchmark_suite.py`: empirical benchmark and qualification harness for local models served by
  `slop.cpp` (time to first token, throughput, VRAM headroom, cold versus warm cache).
- `run_local_lab_experiments.py`: entry point for the local experiments backlog (`EXP-01` to `EXP-05`) on the
  GPU workstation, written under the retired ADR-048; `--mock` runs a hermetic offline validation. Heavy GPU
  work goes through the shared-GPU queue described in
  [SHARED_GPU.md](../docs/guides/SHARED_GPU.md).

## Diagnostics (`scripts/diagnostics/`)

- `native_template_probe.py`: probes an explicitly selected `llama-server` renderer; never requests generation.
- `native_argument_probe.cpp` with `native_argument_probe.mk`: CPU-only probes against the selected `slop.cpp`
  sources, with no model or HTTP. The makefile requires `SLOP_ROOT` (the `slop.cpp` checkout) and an explicit
  build root.
- `native_template_roundtrip.cpp` with `native_template_roundtrip.mk`: real Jinja template → auto-detection →
  grammar/parser → mapper round trip, without inference. Same `SLOP_ROOT` and `BUILD` requirements.

## Harness library (`scripts/harness_lib/`)

About 190 modules from the frozen `tare.tools.harness` runtime (workflow, gates, routing, UI, security and
telemetry helpers). A few relay modules and tests still import some of them; the disposition of each module
(keep, evolve into a satellite, archive or discard) is recorded in `contracts/v1/harness_disposition_manifest.json`.
Do not extend the frozen harness through this folder.

## Labs (`scripts/labs/`)

- `run_agent_runtime_f1_qualification.py`: executes the ADR-057 H0–H3 Aider baseline in disposable Git clones.
- `run_agent_runtime_liveness_lab.py`: cross-repository F1 liveness lab for the ADR-057 agent runtime boundary.
- `canonical_hook_probe.py`: bounded Probe A, one pure prompt policy over two installed CLI adapters.
- `run_canonical_hook_probe_e2e.py`: exercises Probe A deny semantics through disposable Claude and Codex
  configurations.

## Operations (`scripts/ops/`)

Release and installation:

- `build_native_release.py`: builds native wheels twice from exact Git objects, without checkout overlays.
- `pack_native_release.py`: resolves dependencies once and preserves all wheel bytes for offline product
  installs.
- `release_inputs/`: pinned release input records (`PRODUCT_RELEASE_INPUTS_WIN312_*.json`) for past Windows
  Python 3.12 product builds.
- `publish_product.py`: publishes a product release when the OS-qualified sources move (a scheduled task on the
  build machine).
- `install_product_tasks.ps1`: one-time, idempotent setup of self-updating tare.tools on a Windows machine
  (`Tare-Serve`, `Tare-Update` and, on the build machine, `Tare-Publish` scheduled tasks).
- `qualify_product_cohort.py`: bounded, read-only cohort observation
  ([PRODUCT_INSTALLATION.md](../docs/guides/PRODUCT_INSTALLATION.md)).
- `model_product_transition.py`: I2I-06 bounded, installation-local transition qualification, standard library
  only ([PRODUCT_TRANSITION_MODEL.md](../docs/guides/PRODUCT_TRANSITION_MODEL.md)).

Development and CI:

- `run_focused_tests.py`: runs explicit test files, keeps logs and JUnit, and removes generated pytest
  workspaces.
- `run_candidate_checks.py`: prepares only the admitted OS candidate files, then uses the focused runner.
- `park_worktree.py`: keeps only what an experiment worktree adds to Git, then removes the worktree
  (`python scripts/ops/park_worktree.py <path>`, then `--apply`).
- `bump_satellite.py`: satellite federation manager (ADR-049): audit, status and pin bumps of the satellite
  submodules, keeping both maintained README pins in sync.
- `add_os_runners_linux.sh`: adds two more `tare.tools.os` CI runners in the GPU workstation's WSL, as copies
  of the original one; the registration token is piped in and never written to disk.
- `add_os_runners_windows.ps1`: adds two more `tare.tools.os` CI runners on the GPU workstation as Windows
  services; the registration token is read from stdin.

Legacy maintenance:

- `restart_coordinator.py`: kills any running `relay/cluster_coordinator.py` and starts a fresh one (Windows,
  uses `wmic` and `taskkill`).

## Research (`scripts/research/`)

- `compare_evidence_scheduling.py`: EXP-5 bounded offline closed-batch replay; never a Work dispatcher
  ([EVIDENCE_SCHEDULING.md](../docs/guides/EVIDENCE_SCHEDULING.md)).

## Round table (`scripts/round_table/`)

- `report_round_table_telemetry.py`: reads final numeric receipts and renders an offline inference report; no
  LLM calls. Its HTML template is `telemetry_report.html` ([INFERENCE_TELEMETRY.md](../docs/guides/INFERENCE_TELEMETRY.md)).

## Tools (`scripts/tools/`)

- `fetch_clean_url.py`: extracts clean Markdown from a web page through `relay/web_reader.py` (Trafilatura
  first, Firecrawl when `FIRECRAWL_API_KEY` is set or `--firecrawl` forces it):
  `python scripts/tools/fetch_clean_url.py <URL> [--output file.md] [--json] [--firecrawl]`.
