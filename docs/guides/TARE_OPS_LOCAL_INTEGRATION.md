# tare-ops: local composition and frugality cuts

`tare_ops.py` lives in the **OS**. It is a facade over existing functions, not a
new repository, indexing engine, workflow or permission system.
It works with `py -B -m tare_ops` from the root of a source install (see "Getting
started" in the README); `tare-ops` is the same command. It uses the Python packages
installed explicitly in the environment; it does not look for alternative checkouts,
install packages, swap satellites or fall back to the Rust ports.

## Where each responsibility lives

| Responsibility | Owner and current integration |
|---|---|
| CLI, composite diagnostics, explicit Work selection | OS: `tare_ops.py` |
| Finite cases and custody of the reviews | OS: `relay/audit_cases.py`; `audit` subcommand |
| DAG, eligibility, ranking and Work projection | Backlog: existing APIs and CLI; `backlog` subcommand |
| Specs and ADRs a Work references (`repo:path`) | Their owner repositories, read from Git at a fixed commit (`relay/sources.py`) |
| Research and retrieval over its own corpus | Library: `tools.query` |
| Document context, federated ontologies and cache | SpecGraph: existing APIs and CLI; `specgraph` subcommand |
| Validation of envelopes, turns and tactical stop | Agent Runtime: existing contracts, reused by `context` |
| Policy and codecs of tool hooks | Kernel: `compute_plane.hook_cli`, forwarded by `hook` |
| Processes, providers and cancellation | Kernel: they stay in the current adapters; this facade starts no new process |

This composition did not require changes to Runtime, Kernel, Library, Backlog or SpecGraph.
Their existing mechanisms already cover the reused points.
The Rust prototype and its frozen candidate on the GPU workstation remain intact.

## What was reused and what was pruned

Inspection: the unpublished Rust prototype of `tare-ops` on the GPU workstation (its doctor, hooks,
maintenance and audit runner).

| Observed idea / implementation | Decision and falsifier |
|---|---|
| Doctor with operational health separate from qualification | Reused. A test requires `qualification_status: NOT_EVALUATED` and `authority: NONE`, even when the checks pass. |
| `run_doctor` uses `RepairMode::Apply` by default | Not imported. Diagnostics leave every byte of the fixture unchanged. |
| Context uses `frontier.first()` and only three criteria | Not imported. Explicit Work, all mandatory criteria of the original envelope; a missing Work does not select another one. |
| Stop requires Cargo, builds the workspace, validates the global graph and applies maintenance | Not imported as a universal rule. There is no new Stop hook, global scan or mandatory repair in order to stop. |
| Native hooks and neutral success, without granting permission | Reused the implementation that already exists in the Kernel; a test checks `{}` on pass-through. No other parser/policy was copied. |
| Memory and authorship result of the auditor | Kept in the existing importer, with no inherited approval. |
| Mandatory justification hash without reading the evidence | Pruned in the newly created local code. An optional reference is not treated as verification. |
| Recopy the whole case on every correction | Pruned. Resumption accepts only the case identity, the reason and the delta. |
| A missing context metric forces a successor case | Pruned. Explicit warning, with no fictitious zero cost; the attempt limit stays in force. |
| Reimplement three engines inside the integrated utility | Not imported. Calls to the owners, without forks of their algorithms. |
| Derived repairs from a closed list | Kept only the SpecGraph cache, with preview by default and explicit `--apply`. Other repairs were not anticipated. |

These are cuts of the **current integration**, not deletions in the remote project nor changes
to the ADRs or the canonical Backlog schema. The doctrine used is ADR-062: every
control must have a verifiable effect; simplicity does not remove independence,
candidate integrity, mandatory criteria or real limits.

## Usage

```powershell
# Observes only configured layers; does not fix or audit semantically.
py -B -m tare_ops doctor --root . --graph work-graph.json

# Queries the owner; the queue does not grant a binding or pick a Work implicitly.
py -B -m tare_ops backlog --graph work-graph.json next --profile planning --limit 3

# Operational context of a chosen Work, with the refs declared in it, read from their owner trees at HEAD.
py -B -m tare_ops context --graph work-graph.json --work <WORK_ID>

# Other queries stay in the owner's CLI, with no translation of semantics.
py -B -m tare_ops specgraph search derive_descriptor --format json

# Repairing is separate from diagnosing. Preview first; derived cache only.
py -B -m tare_ops maintain --root C:/path/to/your-project
py -B -m tare_ops maintain --root C:/path/to/your-project --apply

# Forwards stdin/stdout and the exit code to the existing hook; does not install hooks.
py -B -m tare_ops hook claude

# Same finite-case CLI, with no additional gate.
py -B -m tare_ops audit status --store C:/audit-state/os-cases --case <CASE_ID>
```

`CHECKS_PASSED` means only that the configured layers (the Backlog graph and the
SpecGraph cache) passed. An unconfigured layer shows as `NOT_CONFIGURED`, never PASS.

`context` reads each declared `repo:path` ref from Git, never from the working file:
`tare.tools.os` documents from the OS tree, another repository's from `satellites/<repo>/`
at the pinned gitlink. A ref that cannot be read stays missing.

`context` emits `READY_CONTEXT`, not an execution authorization. Without operational
eligibility or mandatory document refs, it emits `BLOCKED` and preserves the
owner's cause. A missing mapping that the Runtime contract allows
remains distinct from a missing mandatory document. No bindings are invented.

Composite outputs have a 16 KiB UTF-8 ceiling, adjustable between 1 and 64 KiB with
`--max-bytes`; if the mandatory context does not fit, the operation refuses instead of
truncating silently. Delegated subcommands keep their own output contracts,
arguments, limits and exit codes. `doctor` returns 1 when there are
problems or no configured layer; it is not a mandatory gate for all work.

## Verification and activation limits

Update of September 6: the session lifecycle and the opt-in audit launch were
connected later (that record, `docs/operations/SESSION_LIFECYCLE_INTEGRATION.md`,
was removed; Git history keeps it).
The decisions not to import automatic repairs and the mandatory Stop still stand.

`tests/test_tare_ops.py` uses the owners' real APIs on disposable fixtures:
diagnostics without writes, explicit Work, exact refs, derived cache, idempotency,
stop on output budget and hook forwarding. It does not call models.

This delivery does not install hooks globally, does not replace the incumbent worker,
does not register model aliases and does not run real audits. The remote
transport was not activated. There is now an explicit local transport in the Kernel; its state and limitations were
recorded in `docs/operations/FINITE_AUDIT_CASES.md`, since removed (Git history keeps it). There is no new requirement for a
Round Table to use each command or read documentation.
