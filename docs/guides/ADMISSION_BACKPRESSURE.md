# Admission and backpressure: bounded EXP-2 measurement

`res-exp-backpressure` measures the existing native exploration batch, not a new
scheduler or a provider load test. `EXPLORE` in the public TUI admits at most the
operator's selected number of read-only child sessions. Completed results with
unchanged sources are reused. Existing per-session budgets still apply.

## Reading a measurement

The TUI's Review and `tare parallel-explore --batch <path> --show` expose
`admission`, schema `tare.tools/exploration-admission/1`. This is one bounded
summary of the latest execution in the existing `batch.json`, not an event log.
An execution without these receipts reports unknown; it is never backfilled
from wall-clock timestamps or presented as zero.

| Metric | Meaning and boundary |
|---|---|
| `eligible_items` / `pending_items` | Items needing a dispatch at this execution's start / still undispatched. Completed, source-checked results are excluded. |
| `dispatches` / `resume_dispatches` | Child dispatches / the subset opening an existing native session. Neither counts physical model attempts. |
| `reused_results` | Already completed results skipped before dispatch. It is not a token-cache hit estimate. |
| `peak_admitted_sessions` | Peak outstanding child dispatches before observing their outcomes. Includes local preparation and result handling, not just inference. |
| `queue_delay_ms` | Count, total and maximum monotonic milliseconds from this execution's eligibility to dispatch. Undispatched items have no sample. Human time between executions is excluded. |
| `elapsed_ms` | Monotonic elapsed time within this execution, including scheduling and result handling. |
| `outcomes` / `active_sessions` | Observed terminal child outcomes / dispatched children without an observed outcome. After a crash this is custody evidence, not proof that a process or remote request is still running. |
| `complete` / `cancellation_requested` | Whether this execution returned / whether cancellation was requested. Complete does not mean all questions succeeded. |

For model calls, use the existing `execution_usage_sources.execution_usage`
reader over the child session directories. It deduplicates call identities and
keeps retry intents separate from session resumes. Intent amplification is
`generation_intents / (generation_intents - retry_intents)` only with complete
session coverage and a positive denominator. Item dispatch amplification is
dispatches divided by eligible items; it is undefined for an all-reused run.
Neither ratio measures hidden transport retries. `physical_model_attempts`
remains unknown; physical exchange bounds, when present in native usage, must
not be turned into exact attempt counts.

Missing token and price coverage stays unknown. Failure is not by itself a
valuation of wasted compute: a partial investigation may still be useful.
Provider capacity, GPU utilization, billing, subscription headroom and economic
benefit require their own receipts. Do not infer them from the worker count,
success rate or zero repeated dispatches.

## Qualification observed on 2026-10-18

The public Textual console ran in a real ConPTY. Only visible controls and
keyboard/mouse input selected `EXPLORE`, two concurrent sessions, Start and
Resume. Three actual source investigations covered cancellation/reuse, usage
coverage and the existing local explorer semaphore. They cited the owner source
files; this was not a mocked provider or an artificial throughput comparison.

| Observation | Cold execution | Warm Resume |
|---|---:|---:|
| Eligible / dispatched / reused | 3 / 3 / 0 | 0 / 0 / 3 |
| Peak admitted child sessions | 2 | 0 |
| Completed results / pending / active | 3 / 0 / 0 | 3 / 0 / 0 |
| Queue samples / maximum wait | 3 / 23,360 ms | 0 / unknown |
| Execution elapsed | 51,203 ms | See local receipt; no model dispatch |
| New model generation intents | 19 | 0 |
| Model retry intents | 0 | 0 |

The existing native usage reader recorded 127,104 input and 2,592 output tokens
across 19 calls, with zero missing token observations. Recorded cache reads were
zero. The native Responses adapter preserves a session-derived cache key;
keeping that key and the journal does not guarantee that the provider reports
a cache hit. Warm Resume left all 40 child files byte-for-byte unchanged.
There were 19 unpriced calls: money and remaining subscription quota are
unknown, not free. Observed intent amplification was 1.0; exact physical retry
amplification and wasted compute remain unknown.

The saved deployment was GPT-6.1 Sol medium through native Codex Responses,
16 calls / 300 seconds per child and zero model retries. The pilot initially
intended six calls / 120 seconds, but edited the profile file after the model
picker had loaded it. The saved execution correctly retained the loaded
deployment; measured children used 5, 7 and 7 calls. Record the actual contract,
do not relabel it or rerun completed work to manufacture the intended bound.
For a different budget, explicitly select/apply the revised profile before
starting; a file edit is not a change to an already selected deployment.

Offline regression tests cover synchronized two-slot overlap with a third item
waiting, failed-peer recovery, lost acknowledgements without new generations,
cancellation before/after dispatch, pending work, immutable completed peers,
legacy unknown data, public CLI/TUI rendering, invocation receipts and changed
source rejection. They establish failure behavior, not real provider capacity.

## Consequence for TCP-06 and resource leases

Keep the existing boundaries: OS selects batch parallelism; Runtime enforces
native session budgets; Kernel mediates execution; Node owns the actual shared
GPU queue and lease. Nested local exploration already has one process-local
slot per resource pool (`delegation_tools.slot`). This batch measurement does
not count all processes using a machine and cannot replace Node's lease.
See [the qualified shared GPU lease](SHARED_GPU.md#lease-qualification-res-ip-d).

This run shows bounded admission and a visible local queue. It supplies no
evidence for raising concurrency, adding a global scheduler, or claiming
deadlock freedom, Genesis activation or completion of TCP-06's wider protocol.
Keep completed work, surface partial outcomes, and inspect repeated queueing or
retry amplification in ordinary runs before considering a pool change.

## Custody and destination

The owner keeps this maintained definition and compact result. The Library
catalog receives a commit/path/SHA-256 pointer: the former `tare.tools.research`
repository is now `tare.tools.library`. There is one payload, not another report
copy, repo, publication service or telemetry daemon.

The raw evidence of this run is not published. It held three source sessions,
one implementation session, compact cold/warm/usage receipts and bounded terminal
traces (12 MiB ceiling per terminal). No snapshots
of whole repositories, model downloads or provider payloads enter Git. The
latest admission summary adds constant space per batch and overwrites its
previous measurement; invocation receipts retain their existing recovery role.
Keep the compact receipts, hashes and this guide. Raw traces and native pilot
custody are eligible for the operator's archive cleanup after 30 days, once the
source and federated pointer have landed green; do not silently delete active
or recovery-needed sessions. Further measurements come from ordinary work,
not an unattended paid benchmark.
